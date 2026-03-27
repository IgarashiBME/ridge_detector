# Reference RidgeDetector コードベース調査

## 概要

`reference/RidgeDetector` は、**農業用畝（ridge）リアルタイム検出システム**の Python アプリケーション。
Jetson Orin Nano（Ubuntu 22.04, Python 3.10）上で、ZED 2 ステレオカメラと YOLO11 セマンティックセグメンテーションを用いて畝を検出し、シリアル通信（UBX-NAV-RELPOSNED 形式）で結果を出力する。

## ファイル構成

```
RidgeDetector/
├── main.py                     # エントリポイント、引数パーサー (127行)
├── gui/
│   └── main_window.py          # PySide6 GUI (648行)
├── workers/
│   ├── camera_worker.py        # ZED カメラ制御、録画 (405行)
│   ├── inference_worker.py     # YOLO 推論、シリアル送信 (322行)
│   └── gpio_worker.py          # GPIO 監視 (214行)
├── core/
│   ├── ridge_detection.py      # 畝検出アルゴリズム本体 (144行)
│   ├── ubx_protocol.py         # UBX プロトコル エンコード (119行)
│   └── visualization.py        # 検出結果の描画 (68行)
├── Reference/
│   ├── zed_recoder_gui.py      # 旧録画スクリプト
│   ├── serial_ridge_detector_zed.py  # 旧検出スクリプト
│   └── ridge-yolo11s-seg.pt    # 学習済みモデル (~20.5MB)
├── ridge-detector.service      # systemd サービスファイル
├── CLAUDE.md                   # 開発ガイド
└── initial_prompt.txt          # 日本語要件仕様
```

## アーキテクチャ

### 4スレッド構成

| スレッド | クラス | 役割 |
|---------|--------|------|
| UI スレッド | `MainWindow` | PySide6 GUI、モード状態管理、シグナルルーティング |
| カメラスレッド | `CameraThread` (QThread) | ZED カメラからフレーム取得、SVO2 録画、IMU CSV 記録 |
| 推論スレッド | `InferenceThread` (QThread) | YOLO-seg 推論、畝検出、シリアル通信 |
| GPIO スレッド | `GpioWatcherThread` (QThread) | GPIO ピン監視（録画/検出トリガー） |

### データフロー

```
CameraThread ──[queue.Queue(maxsize=2)]──> InferenceThread
    │                                          │
    └─[sig_frame@15Hz]──> MainWindow <──[sig_frame]──┘
                              ↑            │
                              │         [UBX bytes]──> Serial Port
                              │
GpioWatcherThread ──[sig_rec/det_trigger]──┘
```

- カメラ→推論間は `queue.Queue(maxsize=2)` でフレーム受け渡し（キュー満杯時は古いフレームを破棄）
- スレッド間通信は Qt Signal/Slot で安全に実装
- プレビューは monotonic time 比較で 15 FPS にデシメーション

## 動作モード（排他制御）

3 つのモードが排他的に管理される：

```
         ┌──────────┐
         │   IDLE   │
         └──┬───┬───┘
            │   │
   録画開始 │   │ 検出開始
            ↓   ↓
┌───────────┐   ┌───────────┐
│ RECORDING │   │ DETECTING │
│           │   │           │
│ SVO2録画  │   │ YOLO推論  │
│ IMU CSV   │   │ 直線検出  │
│           │   │ シリアルTX │
└───────────┘   └───────────┘
```

- RECORDING ↔ DETECTING の直接遷移は禁止（IDLE を経由する必要あり）
- GUI ボタン・GPIO トリガーの両方で同じ排他ルールを適用
- モードフラグ (`self._mode`) は Worker メソッド呼び出し前に設定

## 畝検出アルゴリズム

`core/ridge_detection.py` に実装された検出パイプライン：

### Step 1: YOLO-seg 推論

- モデル: `ridge-yolo11s-seg.pt` (21.1 MB)
- 信頼度閾値: 0.25（デフォルト）
- FP16 推論対応（Jetson GPU で高速化）

### Step 2: マスク処理

- ターゲットクラスでフィルタ（オプション）
- 面積最大のマスクを選択
- 最近傍補間でフレームサイズにリサイズ
- 閾値 0.5 で二値化

### Step 3: スキャンライン解析

```python
y_coords = linspace(y_start, y_end, num_lines)  # デフォルト: 20本
for y in y_coords:
    row = target_mask[y, :]
    runs = get_runs(row)               # 連続する1のランを取得
    valid_runs = filter(len >= min_run) # 最小長でフィルタ
    best_run = max(valid_runs)          # 最長ランを選択
    cx = (start + end) / 2             # 中心x座標
    centers.append((cx, y))
```

### Step 4: 直線フィッティング

2 つのモードを提供：

- **RANSAC**（デフォルト）: `sklearn.linear_model.RANSACRegressor` を使用。外れ値に強い。最小サンプル数 2、残差閾値 10.0px。sklearn が無い場合は polyfit にフォールバック。
- **Polyfit**: `numpy.polyfit(Y, X, degree=1)` による最小二乗法。

出力: `((top_x, 0), (bottom_x, H))` の直線端点

### Step 5: (a, b) パラメータ算出

```python
a = (x2 - x1) / (y2 - y1)         # 畝の傾き (dx/dy)
b = x1 - a * y1                   # x切片
b_centered = b - (frame_width / 2) # 画像中心基準の水平位置
```

- **a**: 畝の傾き（正=右傾き、負=左傾き）
- **b_centered**: 画像中心からの水平位置（正=右側、負=左側）

### Step 6: EMA フィルタ

```python
filtered_a = alpha * a + (1 - alpha) * prev_a  # alpha=0.3
filtered_b = alpha * b + (1 - alpha) * prev_b
```

時間方向の平滑化。alpha が大きいほど応答性が高く、小さいほど滑らか。

## シリアル通信（UBX プロトコル）

`core/ubx_protocol.py` で UBX-NAV-RELPOSNED メッセージを構築：

| フィールド | 値 | 説明 |
|-----------|-----|------|
| `relPosN_cm` | a × 100 | 畝の傾き（cm 換算） |
| `relPosE_cm` | b × 100 | 水平位置（cm 換算） |
| `gnssFixOK` | 0 or 1 | 検出の有効性 |
| `carrSoln` | 0/1/2 | 解の品質 |

- ペイロード: 64 バイト
- デフォルトポート: `/dev/ttyTHS1`、ボーレート: 19200

## 主要クラス詳細

### MainWindow (`gui/main_window.py`)

- UI 構築、モード遷移の排他制御
- Worker スレッドの制御メソッドを直接呼び出し（Signal 経由ではない）
- Compact モード対応（7 インチ 800x480 ディスプレイ向け）
- ログ表示（QPlainTextEdit、2000 行制限）
- シャットダウン時は inference → GPIO → camera の順で停止

### CameraThread (`workers/camera_worker.py`)

- `zed.grab()` をカメラ FPS（デフォルト 30）で連続実行
- RECORDING 時: SVO2 エンコード + IMU CSV 書き込み
- DETECTING 時: BGRA→BGR 変換 + リサイズ + キューに push
- プレビュー用 QImage を 15 FPS で emit

### InferenceThread (`workers/inference_worker.py`)

- キューから非ブロッキングでフレーム取得（timeout 0.1s）
- YOLO 推論 → 畝検出 → EMA フィルタ → UBX 送信
- 推論 FPS 上限制御（デフォルト 10 FPS）
- 検出停止時に EMA フィルタリセット + キュー排出

### GpioWatcherThread (`workers/gpio_worker.py`)

- libgpiod (`gpioget` CLI) で GPIO ピンを 20 Hz ポーリング
- Pin A (BOARD 31): 録画トリガー
- Pin B (BOARD 33): 検出トリガー
- デバウンス: 500ms

## コマンドライン引数

### カメラ設定

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--save-dir` | `~/zed_records` | SVO2 録画ディレクトリ |
| `--camera-fps` | 30 | カメラ FPS |
| `--camera-resolution` | HD720 | VGA/HD720/HD1080/HD2K |

### 推論設定

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--model` | `ridge-yolo11s-seg.pt` | YOLO モデルパス |
| `--conf` | 0.25 | 信頼度閾値 (0.0-1.0) |
| `--half` / `--no-half` | 有効 | FP16 推論 |
| `--process-width` | 640 | 推論入力幅 |
| `--target-class` | None (全クラス) | フィルタ対象クラス ID |
| `--fitting-mode` | ransac | polyfit / ransac |
| `--num-lines` | 20 | スキャンライン数 |
| `--inference-fps` | 30 | 最大推論 FPS |
| `--ema-alpha` | 0.3 | EMA フィルタ係数 (0.0-1.0) |

### シリアル設定

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--serial-port` | `/dev/ttyTHS1` | シリアルポート |
| `--serial-baud` | 19200 | ボーレート |

### GPIO 設定

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--gpio-rec-pin` | 31 | 録画トリガー BOARD ピン |
| `--gpio-det-pin` | 33 | 検出トリガー BOARD ピン |
| `--debounce-ms` | 500 | デバウンス時間 (ms) |

### UI 設定

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--compact` | 無効 | 7 インチディスプレイ向けコンパクトモード |

## 依存関係

### Python ライブラリ

| パッケージ | 用途 |
|-----------|------|
| `pyzed.sl` | ZED SDK Python バインディング |
| `ultralytics` | YOLO モデル |
| `opencv-python` | 画像処理 |
| `numpy` | 数値計算 |
| `pyserial` | シリアル通信 |
| `scikit-learn` | RANSAC（オプション） |
| `PySide6` | Qt GUI |

### システム依存

| 依存 | 用途 |
|------|------|
| libgpiod | GPIO アクセス (`gpioget` CLI) |
| ZED SDK 5.x | カメラドライバ |
| CUDA | YOLO GPU 推論 |

## 耐障害設計

依存関係が欠けた場合でもアプリケーション全体はクラッシュせず、該当機能のみが無効化される：

| 欠損する依存 | 動作 |
|-------------|------|
| pyserial | シリアル送信が無効、推論は継続 |
| libgpiod | GPIO が無効、GUI ボタンは動作 |
| scikit-learn | RANSAC → numpy polyfit にフォールバック |
| シリアルポート接続失敗 | 検出は継続 |
| IMU 未検出 | SVO2 録画は継続、CSV スキップ |
| カメラオープン失敗 | エラー終了 |
| YOLO モデル未発見 | エラー終了 |

## 起動例

```bash
python main.py \
  --save-dir ~/zed_records \
  --gpio-rec-pin 31 \
  --gpio-det-pin 33 \
  --serial-port /dev/ttyTHS1 \
  --serial-baud 19200 \
  --inference-fps 10 \
  --model ./ridge-yolo11s-seg.pt \
  --compact
```

systemd サービスとしてデプロイ可能（`ridge-detector.service`）。
