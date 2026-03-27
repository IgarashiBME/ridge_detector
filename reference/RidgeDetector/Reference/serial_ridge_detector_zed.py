import cv2
import numpy as np
import argparse
import time
import signal
import sys
import threading
import queue

import math
import struct

from ultralytics import YOLO

import pyzed.sl as sl
import vpi
import torch

# シリアル通信用
try:
    import serial
    serial_available = True
except ImportError:
    serial_available = False
    print("Warning: pyserial not installed. Serial communication disabled.")

# RANSAC用
try:
    from sklearn.linear_model import RANSACRegressor
    sklearn_available = True
except ImportError:
    sklearn_available = False

# グローバル変数（シグナルハンドラ用）
zed = None
out = None
camera_thread = None
serial_sender = None
stop_event = None

UBX_HEADER1 = 0xB5
UBX_HEADER2 = 0x62

UBX_CLASS_NAV = 0x01
UBX_ID_RELPOSNED = 0x3C
UBX_PAYLOAD_LEN = 64


def signal_handler(sig, frame):
    """Ctrl+Cで適切にリソースを解放"""
    print("\n終了処理中...")
    if stop_event is not None:
        stop_event.set()
    if camera_thread is not None:
        camera_thread.join(timeout=2.0)
    if serial_sender is not None:
        serial_sender.stop()
    if zed is not None:
        zed.close()
    if out is not None:
        out.release()
    cv2.destroyAllWindows()
    sys.exit(0)


class SerialSender:
    """
    別スレッドでシリアル通信を行うクラス
    キューを使ってデータを非同期送信
    """
    def __init__(self, port, baudrate=115200, timeout=1.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial_conn = None
        self.data_queue = queue.Queue(maxsize=10)  # 最大10個までバッファ
        self.stop_event = threading.Event()
        self.thread = None
        self.send_count = 0
        self.error_count = 0
        
    def start(self):
        """シリアル接続を開始してスレッドを起動"""
        try:
            self.serial_conn = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                timeout=self.timeout
            )
            print(f"Serial port opened: {self.port} @ {self.baudrate}bps")
            
            self.thread = threading.Thread(target=self._send_loop, daemon=True)
            self.thread.start()
            return True
        except serial.SerialException as e:
            print(f"Failed to open serial port {self.port}: {e}")
            return False
    
    def stop(self):
        """シリアル通信スレッドを停止"""
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
        if self.serial_conn is not None and self.serial_conn.is_open:
            self.serial_conn.close()
            print(f"Serial port closed. Sent: {self.send_count}, Errors: {self.error_count}")
    
    def send_data(self, msg):
        """
        データをキューに追加（非ブロッキング）
        angle: 角度 [degrees]
        offset: オフセット [pixels]
        """
        try:
            # キューが満杯の場合は古いデータを捨てて新しいデータを追加
            if self.data_queue.full():
                try:
                    self.data_queue.get_nowait()
                except queue.Empty:
                    pass
            self.data_queue.put_nowait(msg)
        except queue.Full:
            pass  # それでも満杯なら諦める
    
    def _send_loop(self):
        """シリアル送信ループ（別スレッドで実行）"""
        while not self.stop_event.is_set():
            try:
                # タイムアウト付きでデータを取得
                msg = self.data_queue.get(timeout=0.1)
                
                if self.serial_conn is not None and self.serial_conn.is_open:
                    self.serial_conn.write(msg)
                    self.send_count += 1
                
            except queue.Empty:
                continue
            except serial.SerialException as e:
                self.error_count += 1
                if self.error_count % 10 == 1:  # 10回に1回だけエラー表示
                    print(f"Serial error: {e}")
            except Exception as e:
                self.error_count += 1
                print(f"Unexpected error in serial send: {e}")


class ZEDCameraThread:
    """
    別スレッドで常に最新フレームを取得・保持するクラス
    """
    def __init__(self, zed_camera):
        self.zed = zed_camera
        self.latest_frame = None
        self.frame_id = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        
        # ZED画像取得用オブジェクト
        self.image_zed = sl.Mat()
        self.runtime_params = sl.RuntimeParameters()
    
    def start(self):
        """カメラスレッドを開始"""
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        return self.thread
    
    def stop(self):
        """カメラスレッドを停止"""
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2.0)
    
    def _capture_loop(self):
        """常に最新フレームを取得し続けるループ"""
        while not self.stop_event.is_set():
            if self.zed.grab(self.runtime_params) == sl.ERROR_CODE.SUCCESS:
                self.zed.retrieve_image(self.image_zed, sl.VIEW.LEFT)
                frame_data = self.image_zed.get_data()  # BGRA形式
                frame_bgr = frame_data[:, :, :3].copy()  # BGRにコピー（重要）
                
                with self.lock:
                    self.latest_frame = frame_bgr
                    self.frame_id += 1
    
    def get_latest_frame(self):
        """最新フレームを取得（コピーを返す）"""
        with self.lock:
            if self.latest_frame is None:
                return None, -1
            return self.latest_frame.copy(), self.frame_id


def parse_args():
    parser = argparse.ArgumentParser(description="ZED2カメラ版: YOLO-seg畦畔検出 (シリアル通信対応)")
    
    # ZEDカメラ設定
    parser.add_argument('--resolution', type=str, default='HD720',
                        choices=['VGA', 'HD720', 'HD1080', 'HD2K'],
                        help='ZEDカメラ解像度 (default: HD720)')
    parser.add_argument('--zed-fps', type=int, default=30,
                        help='ZEDカメラFPS (default: 30)')
    
    # 出力・モデル設定
    parser.add_argument('--output', type=str, default=None, help='出力動画パス')
    parser.add_argument('--model', type=str, default='yolo11s-seg.pt', help='モデルパス')
    parser.add_argument('--conf', type=float, default=0.25, help='信頼度閾値')
    parser.add_argument('--half', action='store_true', help='FP16推論 (Jetsonでは必須)')
    
    # 処理解像度
    parser.add_argument('--process-width', type=int, default=640,
                        help='処理を行う横幅 (推奨: 640 or 480)')

    # 畦畔検出パラメータ
    parser.add_argument('--num-lines', type=int, default=20, help='スキャン本数')
    parser.add_argument('--y-margin', type=float, default=0.1, help='上下マージン')
    parser.add_argument('--min-run', type=int, default=5, help='最小幅')
    parser.add_argument('--mask-alpha', type=float, default=0.4, help='透明度')
    parser.add_argument('--target-class', type=int, default=None, help='クラスID')
    parser.add_argument('--fitting-mode', type=str, default='polyfit',
                        choices=['polyfit', 'ransac'])
    
    # シリアル通信設定
    parser.add_argument('--serial-port', type=str, default=None,
                        help='シリアルポート (例: /dev/ttyUSB0, COM3)')
    parser.add_argument('--serial-baud', type=int, default=19200,
                        help='ボーレート (default: 19200)')
    parser.add_argument('--serial-timeout', type=float, default=0.01,
                        help='シリアル通信タイムアウト [秒] (default: 1.0)')
    
    return parser.parse_args()


def get_resolution_enum(resolution_str):
    """解像度文字列をsl.RESOLUTIONに変換"""
    resolution_map = {
        'VGA': sl.RESOLUTION.VGA,
        'HD720': sl.RESOLUTION.HD720,
        'HD1080': sl.RESOLUTION.HD1080,
        'HD2K': sl.RESOLUTION.HD2K,
    }
    return resolution_map.get(resolution_str, sl.RESOLUTION.HD720)


def get_runs(row_data):
    row_data = row_data.astype(np.int32)
    padded = np.pad(row_data, (1, 1), mode='constant')
    diff = np.diff(padded)
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    return list(zip(starts, ends))


def calculate_line_polyfit(centers, height):
    if len(centers) < 2:
        return None
    pts = np.array(centers)
    Y, X = pts[:, 1], pts[:, 0]
    slope, intercept = np.polyfit(Y, X, 1)
    top_x = int(slope * 0 + intercept)
    bottom_x = int(slope * height + intercept)
    return (top_x, 0), (bottom_x, height)


def calculate_line_ransac(centers, height):
    if not sklearn_available or len(centers) < 3:
        return calculate_line_polyfit(centers, height)
    pts = np.array(centers)
    Y, X = pts[:, 1].reshape(-1, 1), pts[:, 0]
    ransac = RANSACRegressor(min_samples=2, residual_threshold=10.0)
    try:
        ransac.fit(Y, X)
        line_x = ransac.predict(np.array([[0], [height]]))
        return (int(line_x[0]), 0), (int(line_x[1]), height)
    except:
        return None


def calculate_steering_info(p1, p2, frame_width, frame_height):
    dx = p1[0] - p2[0]
    dy = p1[1] - p2[1]
    angle_deg = np.degrees(np.arctan2(dx, -dy))
    
    if dy == 0:
        dy = 1e-5
    slope = dx / dy 
    target_x = slope * (frame_height - p2[1]) + p2[0]
    offset = target_x - (frame_width / 2)
    return angle_deg, offset

    
def line_points_to_ab(p1, p2, frame_width):
    """Convert two points on the line into (a, b_centered) for x_centered = a*y + b_centered.
    - a: dx/dy
    - b_centered: x at y=0, where x is centered so that image center is 0 (right positive).
    """
    if p1 is None or p2 is None:
        return None
    x1, y1 = p1
    x2, y2 = p2
    dy = (y2 - y1)
    if dy == 0:
        return None
    a = (x2 - x1) / dy  # dx/dy
    b = x1 - a * y1     # x at y=0 in pixel coords (0..W)
    b_centered = b - (frame_width / 2.0)
    return float(a), float(b_centered)


def ubx_checksum(data: bytes) -> bytes:
    """
    UBX checksum over: CLASS, ID, LENGTH(2), PAYLOAD
    Returns CK_A, CK_B
    """
    ck_a = 0
    ck_b = 0
    for b in data:
        ck_a = (ck_a + b) & 0xFF
        ck_b = (ck_b + ck_a) & 0xFF
    return bytes([ck_a, ck_b])


def build_ubx_nav_relposned(
    relPosN_cm: int,
    relPosE_cm: int,
    gnssFixOK: int,
    carrSoln: int,
    refStationId: int = 0,
    iTOW_ms: int = 0,
    relPosD_cm: int = 0,
    relPosValid: int = 1,
) -> bytes:
    """
    Build UBX-NAV-RELPOSNED (0x01 0x3C) message.

    relPosN/E/D are I4 in cm.
    flags:
      bit0 gnssFixOK
      bit2 relPosValid
      bits4..3 carrSoln (0,1,2)
    """
    # Validate ranges
    if not (0 <= refStationId <= 4095):
        raise ValueError("refStationId must be in 0..4095")
    if not (0 <= iTOW_ms <= 0xFFFFFFFF):
        raise ValueError("iTOW must fit in U4")
    if carrSoln not in (0, 1, 2):
        raise ValueError("carrSoln must be 0, 1, or 2")
    if gnssFixOK not in (0, 1):
        raise ValueError("gnssFixOK must be 0 or 1")
    if relPosValid not in (0, 1):
        raise ValueError("relPosValid must be 0 or 1")

    version = 0x01
    reserved0 = 0x00

    # relPosLength (cm) and relPosHeading (1e-5 deg) - fill reasonable defaults
    relPosLength_cm = 0
    relPosHeading_1e5deg = 0

    reserved1 = bytes(4)

    # High-precision parts (0.1mm units, range -99..+99) -> set 0
    relPosHPN = 0
    relPosHPE = 0
    relPosHPD = 0
    relPosHPLength = 0

    # Accuracies (0.1mm) -> set 0
    accN = 0
    accE = 0
    accD = 0
    accLength = 0
    accHeading = 0

    reserved2 = bytes(4)

    # flags (X4)
    flags = 0
    flags |= (gnssFixOK & 0x1) << 0
    flags |= (relPosValid & 0x1) << 2
    flags |= (carrSoln & 0x3) << 3  # bits 4..3

    payload = struct.pack(
        "<BBH I i i i i i 4s b b b b I I I I I 4s I",
        version,                 # U1
        reserved0,               # U1
        refStationId,            # U2
        iTOW_ms,                 # U4
        relPosN_cm,              # I4
        relPosE_cm,              # I4
        relPosD_cm,              # I4
        relPosLength_cm,         # I4
        relPosHeading_1e5deg,    # I4 (1e-5 deg)
        reserved1,               # U1[4]
        relPosHPN,               # I1
        relPosHPE,               # I1
        relPosHPD,               # I1
        relPosHPLength,          # I1
        accN,                    # U4
        accE,                    # U4
        accD,                    # U4
        accLength,               # U4
        accHeading,              # U4
        reserved2,               # U1[4]
        flags,                   # X4
    )

    if len(payload) != UBX_PAYLOAD_LEN:
        raise RuntimeError(f"payload length mismatch: {len(payload)} != {UBX_PAYLOAD_LEN}")

    header_wo_sync = struct.pack("<BBH", UBX_CLASS_NAV, UBX_ID_RELPOSNED, UBX_PAYLOAD_LEN)
    chk = ubx_checksum(header_wo_sync + payload)

    msg = bytes([UBX_HEADER1, UBX_HEADER2]) + header_wo_sync + payload + chk
    return msg
    

def main():
    global zed, out, camera_thread, serial_sender, stop_event
    
    args = parse_args()
    
    # シグナルハンドラ設定
    signal.signal(signal.SIGINT, signal_handler)
    
    # ========== シリアル通信初期化 ==========
    if args.serial_port is not None:
        if not serial_available:
            print("Error: pyserial is not installed. Install with: pip install pyserial")
            return
        
        serial_sender = SerialSender(
            port=args.serial_port,
            baudrate=args.serial_baud,
            timeout=args.serial_timeout
        )
        if not serial_sender.start():
            print("Failed to start serial communication. Continuing without serial output.")
            serial_sender = None
    else:
        print("Serial communication disabled (no --serial-port specified)")
    
    # ========== ZEDカメラ初期化 ==========
    print("ZED2カメラを初期化中...")
    zed = sl.Camera()
    
    init_params = sl.InitParameters()
    init_params.camera_resolution = get_resolution_enum(args.resolution)
    init_params.camera_fps = args.zed_fps
    init_params.depth_mode = sl.DEPTH_MODE.NONE  # 深度計算を無効化（高速化）
    init_params.coordinate_units = sl.UNIT.METER
    
    status = zed.open(init_params)
    if status != sl.ERROR_CODE.SUCCESS:
        print(f"ZEDカメラのオープンに失敗: {status}")
        return
    
    # カメラ情報取得
    camera_info = zed.get_camera_information()
    orig_w = camera_info.camera_configuration.resolution.width
    orig_h = camera_info.camera_configuration.resolution.height
    actual_fps = zed.get_camera_information().camera_configuration.fps
    
    print(f"ZED Camera Resolution: {orig_w}x{orig_h} @ {actual_fps}fps")
    
    # 処理解像度計算
    process_w = args.process_width
    scale_factor = process_w / orig_w
    process_h = int(orig_h * scale_factor)
    
    print(f"Processing Resolution: {process_w}x{process_h} (Scale: {scale_factor:.2f})")
    
    # ========== カメラスレッド開始 ==========
    cam_thread = ZEDCameraThread(zed)
    camera_thread = cam_thread  # グローバル参照用
    stop_event = cam_thread.stop_event
    cam_thread.start()
    
    # 最初のフレームを待つ
    print("カメラスレッド開始、最初のフレームを待機中...")
    while True:
        frame, _ = cam_thread.get_latest_frame()
        if frame is not None:
            break
        time.sleep(0.01)
    print("フレーム取得開始")
    
    # ========== モデル読み込み ==========
    print(f"Loading model: {args.model}")
    model = YOLO(args.model)
    
    # ========== 出力設定 ==========
    if args.output is not None:
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        out = cv2.VideoWriter(args.output, fourcc, actual_fps, (process_w, process_h))
        print(f"Output video: {args.output}")
    
    prev_time = time.perf_counter()
    frame_count = 0
    last_frame_id = -1
    skipped_frames = 0
    
    print("畦畔検出を開始します... (q または ESC で終了)")
    
    startTime = time.perf_counter()
    # ========== メインループ ==========
    while not cam_thread.stop_event.is_set():
        curr_time = time.perf_counter()
        
        # 最新フレームを取得
        raw_frame, frame_id = cam_thread.get_latest_frame()
        if raw_frame is None:
            continue
        
        # スキップしたフレーム数をカウント（デバッグ用）
        if last_frame_id >= 0:
            skipped = frame_id - last_frame_id - 1
            if skipped > 0:
                skipped_frames += skipped
        last_frame_id = frame_id
        
        # リサイズ
        frame = cv2.resize(raw_frame, (process_w, process_h))
        H, W = frame.shape[:2]

        # 推論
        results = model(frame, verbose=False, conf=args.conf, half=args.half)        
        result = results[0]
        infer_time = result.speed['inference']

        vis_frame = frame.copy()
        target_mask = None
        ab = None
        
        if result.masks is not None:
            if args.target_class is not None:
                class_ids = result.boxes.cls.cpu().numpy().astype(int)
                target_indices = [i for i, c in enumerate(class_ids) if c == args.target_class]
                masks = result.masks.data[target_indices] if target_indices else []
            else:
                masks = result.masks.data

            if len(masks) > 0:
                areas = masks.sum(dim=(1, 2))
                max_idx = areas.argmax().item()

                raw_mask = masks[max_idx].cpu().numpy()
                target_mask = cv2.resize(raw_mask, (W, H), interpolation=cv2.INTER_NEAREST)
                target_mask = (target_mask > 0.5).astype(np.uint8)

        # マスク描画と制御情報計算
        control_info = "No Line"
        angle, offset = 0.0, 0.0  # デフォルト値
        relPosN_a = 0  # デフォルト値
        relPosE_b = 0  # デフォルト値
        detectionOK = 0  # デフォルト値

        if target_mask is not None:
            color_mask = np.zeros_like(frame)
            color_mask[:, :, 2] = 255 
            mask_indices = target_mask == 1
            
            vis_frame[mask_indices] = cv2.addWeighted(
                vis_frame[mask_indices], 1.0 - args.mask_alpha,
                color_mask[mask_indices], args.mask_alpha, 0
            ).reshape(-1, 3)

            # 中心点の群を算出、緑点として表示
            centers = []
            y_start = int(H * args.y_margin)
            y_end = int(H * (1.0 - args.y_margin))
            
            if args.num_lines > 0:
                y_coords = np.linspace(y_start, y_end, args.num_lines, dtype=int)
                for y in y_coords:
                    row = target_mask[y, :]
                    runs = get_runs(row)
                    valid_runs = [r for r in runs if (r[1]-r[0]) >= args.min_run]
                    
                    if valid_runs:
                        best_run = max(valid_runs, key=lambda x: x[1]-x[0])
                        cx = int((best_run[0] + best_run[1]) / 2)
                        centers.append((cx, y))

            for (cx, cy) in centers:
                cv2.circle(vis_frame, (cx, cy), 3, (0, 255, 0), -1)

            # 近似直線
            if args.fitting_mode == 'ransac':
                line_points = calculate_line_ransac(centers, H)
            else:
                line_points = calculate_line_polyfit(centers, H)
            
            if line_points:
                p1, p2 = line_points
                cv2.line(vis_frame, p1, p2, (255, 0, 0), 2)
                angle, offset = calculate_steering_info(p1, p2, W, H)
                #control_info = f"Ang: {angle:.1f} | Off: {offset:.0f}"
                cv2.line(vis_frame, (W//2, H), (W//2, H-30), (0, 255, 255), 1)
                
                ab = line_points_to_ab(p1, p2, W)
                if ab is not None:
                    a, b = ab
                    control_info = f"a: {a:.4f} | b: {b:.1f}"
                    relPosN_a = int(a * 100)
                    relPosE_b = int(b * 100)
                    detectionOK = 1
                else:
                    control_info = f"a: NaN | b: NaN"

        # relPosNEDを作成
        # 未検出時、relPosN = 0, relPosE = 0, gnssFixOK = 0
        elapsedTime = int((time.perf_counter() - startTime) * 1000)
        msg = build_ubx_nav_relposned(
            relPosN_cm=relPosN_a,
            relPosE_cm=relPosE_b,
            gnssFixOK=detectionOK,
            carrSoln=0,
            refStationId=0,
            iTOW_ms=elapsedTime,
            relPosD_cm=0,
            relPosValid=0,
        )
                    
        # シリアル送信（非同期）
        if serial_sender is not None:
            serial_sender.send_data(msg)

        # FPS計算
        loop_time = curr_time - prev_time
        fps_disp = 1 / loop_time if loop_time > 0 else 0
        prev_time = curr_time

        # 情報表示
        perf_text = f"Inf:{infer_time:.1f}ms FPS:{fps_disp:.1f} Skip:{skipped_frames}"
        serial_status = ""
        if serial_sender is not None:
            serial_status = f" | TX:{serial_sender.send_count}"
        
        cv2.putText(vis_frame, perf_text + serial_status, (10, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.putText(vis_frame, control_info, (10, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (100, 255, 100), 1)

        # 出力
        if out is not None:
            out.write(vis_frame)
        
        cv2.imshow('ZED2 Ridge Detection', vis_frame)
        
        key = cv2.waitKey(1)
        if key in [ord('q'), 27]:  # q or ESC
            break
        
        frame_count += 1

    # ========== 終了処理 ==========
    print(f"\n処理完了: {frame_count} frames processed, {skipped_frames} frames skipped")
    cam_thread.stop()
    if serial_sender is not None:
        serial_sender.stop()
    zed.close()
    if out is not None:
        out.release()
    cv2.destroyAllWindows()

if __name__ == '__main__':
    main()
