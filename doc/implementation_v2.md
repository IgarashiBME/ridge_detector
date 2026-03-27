# Ridge Detector v2 実装ドキュメント

## 概要

ROS2 を使わないマルチスレッド Python アーキテクチャで、reference の PySide6 単体アプリを再構成。
スマートフォン連携（PWA）、アノテーション、エッジ学習機能を追加。

- **プラットフォーム**: Jetson Orin Nano, Ubuntu 22.04, Python 3.10, ZED SDK 5.x
- **仮想環境**: `~/zed_yolo_venv`

---

## アーキテクチャ

```
                               SmartPhone (PWA)
                                    │
                               WiFi / HTTP
                                    │
                         ┌── FastAPI Server ──┐
                         │  REST   WebSocket  │  (daemon thread)
                         └────────┬───────────┘
                                  │
                         ┌────────▼───────────┐
                         │    SharedState      │  (threading.Lock)
                         │  mode, frames,      │
                         │  detection, training │
                         └──┬─────┬──────┬────┘
                            │     │      │
                   ┌────────┘     │      └──────────┐
                   │              │                  │
            CameraThread   InferenceThread    TrainingManager
            (threading)     (threading)       (subprocess.Popen)
                   │              │
            ZED Camera      YOLO Model
                   │              │
            queue.Queue(2)───────┘
                                  │
                           Serial Port (UBX)

            Optional:
            ┌────────────────────┐
            │  DisplayWindow     │
            │  (PySide6, QTimer) │
            │  SharedState 読取  │
            └────────────────────┘
```

---

## ファイル構成

```
ridge_detector/
├── main.py                          # エントリポイント（argparse + 全コンポーネント起動）
├── core/                            # reference からコピー（変更なし）
│   ├── __init__.py
│   ├── ridge_detection.py           # 畝検出アルゴリズム（スキャンライン + RANSAC）
│   ├── ubx_protocol.py              # UBX-NAV-RELPOSNED メッセージ構築
│   └── visualization.py             # 検出結果の描画（マスク、ライン、パラメータ）
├── state/
│   ├── __init__.py
│   ├── shared_state.py              # SharedState: スレッドセーフ状態管理
│   └── mode_manager.py              # ModeManager: 排他モード遷移
├── workers/
│   ├── __init__.py
│   ├── camera_thread.py             # CameraThread: ZED grab/録画/フレーム配信
│   └── inference_thread.py          # InferenceThread: YOLO推論/EMA/シリアル
├── server/
│   ├── __init__.py
│   ├── app.py                       # FastAPI アプリファクトリ
│   ├── routes_api.py                # REST API エンドポイント（15個）
│   ├── routes_ws.py                 # WebSocket エンドポイント
│   └── runner.py                    # uvicorn バックグラウンドスレッド起動
├── training/
│   ├── __init__.py
│   ├── manager.py                   # TrainingManager: subprocess 管理
│   └── train_process.py             # 独立学習スクリプト（別プロセスで実行）
├── display/
│   ├── __init__.py
│   └── display_window.py            # PySide6 表示専用（オプション）
├── web/                             # PWA 静的ファイル
│   ├── index.html                   # PWA シェル（4画面）
│   ├── manifest.json                # PWA マニフェスト
│   ├── sw.js                        # Service Worker
│   ├── style.css                    # ダークテーマ、モバイルファースト
│   └── app.js                       # SPA ロジック
├── reference/RidgeDetector/         # 既存（変更なし）
└── doc/
    ├── reference_analysis.md        # reference コードベース調査
    └── implementation_v2.md         # 本ドキュメント
```

---

## コンポーネント詳細

### 1. SharedState (`state/shared_state.py`)

Qt Signal の代わりとなるスレッドセーフな状態コンテナ。

**設計方針**:
- 単一 `threading.Lock`（デッドロック回避）
- `threading.Event` による変更通知（WebSocket/Display がポーリング）
- Workers は `set_*()` で書き込み → `Event.set()`
- Server/Display は `get_*()` で読み取り + `Event.wait(timeout)`

**管理する状態**:

| 状態 | 型 | 用途 |
|------|-----|------|
| `mode` | `Mode` enum | IDLE / RECORDING / DETECTING / TRAINING |
| `preview_frame` | `np.ndarray` (BGR) | カメラからのプレビュー |
| `annotated_frame` | `np.ndarray` (BGR) | 推論結果付きフレーム |
| `detection` | `DetectionResult` | a, b, fps, serial_status, serial_count |
| `recording_start` | `float` | monotonic タイムスタンプ |
| `recording_session_dir` | `str` | 現在の録画セッションディレクトリ |
| `training` | `TrainingStatus` | running, epoch, loss, phase |
| `log_entries` | `deque(maxlen=200)` | ログリングバッファ |

**Event 一覧**: `mode_changed`, `detection_updated`, `training_updated`, `frame_updated`, `log_updated`

**主要メソッド**:
- `snapshot() -> dict`: API `/api/status` 向けの全状態スナップショット
- `get_display_frame()`: DETECTING 時は annotated_frame、それ以外は preview_frame を返す

---

### 2. ModeManager (`state/mode_manager.py`)

reference の `MainWindow._request_*` メソッド（L373-421）を抽出した排他モード遷移管理。

**遷移ルール**:

```
         ┌──────────┐
         │   IDLE   │
         └─┬──┬──┬──┘
           │  │  │
           ↕  ↕  ↕
    RECORDING DETECTING TRAINING
```

- IDLE ↔ RECORDING、IDLE ↔ DETECTING、IDLE ↔ TRAINING のみ許可
- 非 IDLE 間の直接遷移は拒否（エラーメッセージ返却）

**コールバック方式**:
```python
mm.register_callbacks(
    start_recording=camera.start_recording,
    stop_recording=camera.stop_recording,
    start_detecting=lambda: (camera.start_detecting(), inference.start_detecting()),
    stop_detecting=lambda: (inference.stop_detecting(), camera.stop_detecting()),
    start_training=None,   # 学習はAPIからパラメータ付きで直接開始
    stop_training=lambda: training_manager.stop(),
)
```

**注意**: `start_training` は `None`。学習開始は `routes_api.py` でパラメータ（epochs, batch_size, img_size）を受け取って直接 `training_manager.start()` を呼ぶため。

**API**: `request_mode(target: Mode, source: str) -> (bool, str)`

---

### 3. CameraThread (`workers/camera_thread.py`)

reference `camera_worker.py` を `threading.Thread` に変換。

**reference からの変更点**:

| 変更内容 | 詳細 |
|----------|------|
| QThread → `threading.Thread(daemon=True)` | join() で停止 |
| `sig_frame` → `state.set_preview_frame()` | BGR numpy のまま（QImage 変換不要） |
| `sig_status` → `state.append_log()` | ログリングバッファに追加 |
| `_recording`/`_detecting` フラグ | そのまま維持（ModeManager コールバックで制御） |
| セッション単位のディレクトリ | `~/zed_records/{timestamp}/recording.svo2` |
| ランダムフレーム保存 | 録画中に確率的に JPEG 保存 |

**録画セッション構造**:
```
~/zed_records/
├── 20260327_143022/
│   ├── recording.svo2              # SVO2 録画データ
│   ├── imu.csv                     # IMU データ
│   ├── frames/                     # ランダム抽出フレーム
│   │   ├── frame_000312.jpg
│   │   └── frame_001847.jpg
│   └── labels/                     # アノテーション（PWAから追加）
│       ├── frame_000312.txt        # YOLO polygon 形式
│       └── frame_001847.txt
```

**ランダムフレーム保存**:
- パラメータ: `--capture-probability 0.02`（デフォルト 2%）
- 30fps で平均 1.7 秒に 1 枚保存
- 録画中の `grab()` ループ内で `random.random() < probability` で判定

**流用するロジック（ほぼそのまま）**:
- ZED 初期化、grab ループ
- SVO2 録画（enable/disable_recording）
- IMU CSV 書き込み
- inference queue push（BGRA→BGR変換 + リサイズ）
- プレビューデシメーション（タイムスタンプベース、15Hz）

---

### 4. InferenceThread (`workers/inference_thread.py`)

reference `inference_worker.py` を `threading.Thread` に変換。

**reference からの変更点**:

| 変更内容 | 詳細 |
|----------|------|
| QThread → `threading.Thread(daemon=True)` | join() で停止 |
| `sig_frame` → `state.set_annotated_frame()` | BGR numpy のまま |
| `sig_result` → `state.set_detection()` | a, b, fps, serial_status, serial_count |
| `sig_status` → `state.append_log()` | ログリングバッファ |
| QImage 変換削除 | 不要（SharedState は BGR numpy） |
| モデルリロード追加 | `reload_model(path)` で次ループでリロード |

**モデルリロード機能**:
```python
def reload_model(self, model_path: str):
    with self._reload_lock:
        self._pending_model_path = model_path
# 次のメインループ先頭で _check_model_reload() が実行
```

**流用するロジック（そのまま）**:
- YOLO ロード、推論ループ
- EMA フィルタ
- シリアル通信（UBX-NAV-RELPOSNED）
- レート制限（inference_fps）

---

### 5. FastAPI Server (`server/`)

**構成**:
- `app.py`: アプリファクトリ。SharedState 等を `app.state` に格納。PWA 静的ファイルをマウント。
- `routes_api.py`: REST API 15 エンドポイント
- `routes_ws.py`: WebSocket エンドポイント
- `runner.py`: uvicorn を `daemon thread` で起動

**REST API エンドポイント**:

| メソッド | パス | 機能 |
|---------|------|------|
| GET | `/api/status` | システム状態スナップショット |
| POST | `/api/mode` | モード変更 `{mode: "IDLE"\|"RECORDING"\|"DETECTING"\|"TRAINING"}` |
| GET | `/api/sessions` | 録画セッション一覧（日時, フレーム数, アノテーション済数, SVO2サイズ） |
| DELETE | `/api/sessions/{name}` | セッション削除 |
| GET | `/api/sessions/{name}/frames` | セッション内フレーム一覧（アノテーション有無付き） |
| GET | `/api/sessions/{name}/frames/{frame}` | フレーム画像取得（JPEG） |
| GET | `/api/sessions/{name}/frames/{frame}/annotation` | アノテーション取得 |
| PUT | `/api/sessions/{name}/frames/{frame}/annotation` | 4点アノテーション保存 `{points: [[x,y]...]}` |
| DELETE | `/api/sessions/{name}/frames/{frame}/annotation` | アノテーション削除 |
| GET | `/api/models` | モデルファイル一覧 |
| POST | `/api/training/start` | 学習開始 `{epochs, batch_size, img_size}` |
| POST | `/api/training/stop` | 学習中止 |
| GET | `/api/training/status` | 学習進捗 |
| GET | `/api/logs` | ログ取得 |

**WebSocket** (`/ws`):

| 方向 | メッセージ | 内容 |
|------|-----------|------|
| Server→Client | `{type: "status", data: {...}}` | モード変更時にスナップショット送信 |
| Server→Client | `{type: "detection", data: {...}}` | a, b, fps, serial 情報 |
| Server→Client | `{type: "training", data: {...}}` | epoch, loss, phase |
| Server→Client | `{type: "log", data: [...]}` | 新着ログエントリ |
| Server→Client | `{type: "frame", data: "base64"}` | JPEG base64（~5FPS） |
| Client→Server | `{type: "subscribe", channels: [...]}` | 購読チャンネル選択 |
| Client→Server | `{type: "ping"}` | 接続確認 |

**実装詳細**:
- ~50Hz でイベントをポーリング（`asyncio.sleep(0.02)`）
- フレームは ~5FPS に制限（`frame_interval = 0.2s`）
- NaN 値は JSON 互換のため `null` に変換

**セキュリティ**:
- パス名は `Path(name).name` でサニタイズ（パストラバーサル防止）

---

### 6. TrainingManager (`training/manager.py` + `train_process.py`)

**subprocess を使う理由**: GPU メモリ完全分離（プロセス終了で CUDA コンテキスト解放）

**manager.py の役割**:
1. 全セッションの `frames/` + `labels/` を走査し、ラベルが存在するフレームを収集
2. dataset.yaml を一時ディレクトリに生成（シンボリックリンク使用）
3. `subprocess.Popen` で `train_process.py` を起動
4. 2 秒間隔で `progress.json` をポーリング → SharedState 更新
5. 完了時に `result.json` から新モデルパスを取得

**train_process.py の役割**:
1. argparse で設定受取
2. `ultralytics.YOLO` でモデルロード
3. `on_train_epoch_end` コールバックで `progress.json` に書き込み
4. `model.train()` 実行
5. 完了時に `result.json` に新モデルパスを書き込み

**データセット構築**:
```
{run_dir}/dataset/
├── dataset.yaml
├── images/train/
│   ├── 20260327_143022_frame_000312.jpg → (symlink)
│   └── ...
└── labels/train/
    ├── 20260327_143022_frame_000312.txt → (symlink)
    └── ...
```
- セッション名をプレフィックスに付けて重複回避
- val は train と同じデータを使用（小規模データセット想定）

**アノテーション形式**:
```
0 x1 y1 x2 y2 x3 y3 x4 y4
```
- クラスID `0`（ridge）
- 座標は正規化（0.0〜1.0）
- YOLO polygon セグメンテーション形式

---

### 7. DisplayWindow (`display/display_window.py`)

PySide6 表示専用。ボタンなし（モード制御は PWA のみ）。

- `QTimer` で SharedState を 15Hz ポーリング
- プレビュー表示（`get_display_frame()` → BGR→RGB→QImage→QPixmap）
- モード表示（色付きバッジ: 赤=RECORDING, 緑=DETECTING, 橙=TRAINING, 灰=IDLE）
- 検出パラメータ（a, b, FPS, Serial）
- 録画タイマー / 学習進捗

`--no-display` 時は import すらしない（PySide6 が不要）。

---

### 8. PWA (`web/`)

Vanilla HTML/JS/CSS。フレームワークなし（Jetson 上でビルド不要）。

**4画面（ハッシュルーティング）**:

1. **ダッシュボード** (`#/`):
   - モード表示（色付きバッジ）
   - 制御ボタン（Record / Detect / Stop）
   - ライブプレビュー（WebSocket base64 JPEG）
   - 検出パラメータ（a, b, FPS, Serial TX）
   - ログ表示

2. **セッション一覧** (`#/sessions`):
   - 録画セッション一覧（日時・フレーム数・アノテーション済数・SVO2サイズ）
   - セッション削除ボタン

3. **アノテーション** (`#/sessions/{name}`):
   - セッション内フレームサムネイル一覧（アノテーション済みマーク付き）
   - フレーム選択 → Canvas 上で 4 点タップ → ポリゴン描画
   - 保存 / クリア / 削除ボタン

4. **学習** (`#/training`):
   - データセット統計（全セッション合計フレーム数・アノテーション数）
   - パラメータ設定（epochs, batch_size, img_size）
   - 開始 / 停止ボタン
   - 進捗バー（epoch / loss 表示）

**PWA 機能**:
- Service Worker によるオフラインキャッシュ（静的ファイルのみ）
- `manifest.json` でホーム画面追加対応
- WebSocket 自動再接続（2秒間隔）
- REST API フォールバック（3秒間隔でステータスポーリング）

---

## reference からの主な変更点まとめ

| 変更内容 | 詳細 |
|----------|------|
| QThread → `threading.Thread` | `daemon=True`、`join()` で停止 |
| Qt Signal → SharedState | `set_*()`/`get_*()` + `threading.Event` |
| QImage 変換削除 | BGR numpy のまま SharedState に格納 |
| 録画ディレクトリ構造変更 | セッション単位（`{timestamp}/recording.svo2, frames/, labels/`） |
| ランダムフレーム保存追加 | `--capture-probability 0.02`（録画中に約1.7秒に1枚） |
| モード遷移ロジック抽出 | `MainWindow._request_*` → `ModeManager` クラス |
| TRAINING モード追加 | IDLE↔TRAINING 遷移、subprocess 実行 |
| モデルリロード追加 | `InferenceThread.reload_model()` |
| GPIO 削除 | 初期実装では対象外（後から追加可能） |
| FastAPI サーバー追加 | REST 15 エンドポイント + WebSocket |
| PWA 追加 | 4画面 SPA（ダッシュボード/セッション/アノテーション/学習） |

---

## コマンドライン引数

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--save-dir` | `~/zed_records` | 録画保存ディレクトリ |
| `--camera-fps` | 30 | カメラ FPS |
| `--camera-resolution` | HD720 | VGA/HD720/HD1080/HD2K |
| `--model` | 自動検出 | YOLO モデルパス |
| `--conf` | 0.25 | 信頼度閾値 |
| `--half` / `--no-half` | 有効 | FP16 推論 |
| `--process-width` | 640 | 推論入力幅 |
| `--target-class` | None | フィルタ対象クラス ID |
| `--fitting-mode` | ransac | polyfit / ransac |
| `--num-lines` | 20 | スキャンライン数 |
| `--inference-fps` | 30 | 最大推論 FPS |
| `--serial-port` | `/dev/ttyTHS1` | シリアルポート |
| `--serial-baud` | 19200 | ボーレート |
| `--ema-alpha` | 0.3 | EMA フィルタ係数 |
| `--capture-probability` | 0.02 | 録画中のフレーム保存確率 |
| `--no-display` | 無効 | ヘッドレスモード |
| `--compact` | 無効 | コンパクト UI モード |
| `--port` | 8000 | FastAPI サーバーポート |
| `--host` | 0.0.0.0 | FastAPI サーバーホスト |

---

## 起動例

```bash
# 仮想環境をアクティベート
source ~/zed_yolo_venv/bin/activate

# 標準起動（ディスプレイ + サーバー）
python main.py

# ヘッドレス起動（サーバーのみ）
python main.py --no-display

# コンパクトモード（7インチディスプレイ向け）
python main.py --compact

# フルオプション例
python main.py \
  --save-dir ~/zed_records \
  --serial-port /dev/ttyTHS1 \
  --serial-baud 19200 \
  --inference-fps 30 \
  --capture-probability 0.02 \
  --port 8000 \
  --compact
```

---

## 依存関係

### 追加パッケージ（reference に加えて必要）

| パッケージ | 用途 |
|-----------|------|
| `fastapi` | REST API + WebSocket サーバー |
| `uvicorn` | ASGI サーバー |

### インストール

```bash
source ~/zed_yolo_venv/bin/activate
pip install fastapi uvicorn
```
