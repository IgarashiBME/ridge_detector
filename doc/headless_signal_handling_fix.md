# Headless モードのシグナル処理・終了処理の修正

## 概要

`--no-display`（headless）モードで起動した場合、ターミナルからの Ctrl+C が効かない、またはシャットダウン時に C++ 例外でクラッシュする問題を修正した。

## 発生した症状

1. **Ctrl+C が効かない**: Detect（AI検出）→ Stop 後に限らず、headless モードでは Ctrl+C でプロセスを終了できないことがあった
2. **シャットダウン時のクラッシュ**: Ctrl+C が受け付けられた場合でも `terminate called without an active exception` が発生し、約1分間操作不能になった

`--compact` モードでは Qt のイベントループがメインスレッドで動作するため、同じ症状は発生しなかった。

## 原因

### 問題1: `threading.Event.wait()` のシグナル非応答

`main.py` の `_run_headless()` で使用していた `shutdown_event.wait()`（タイムアウトなし）が原因。

CPython の実装では、`Event.wait()` をタイムアウトなしで呼ぶと、内部で `Lock.acquire()` が C 言語レベルでブロックし、GIL を解放しない。そのため SIGINT シグナルハンドラが実行される機会がなく、Ctrl+C が無視される。

```python
# 修正前
shutdown_event.wait()  # C レベルでブロック、シグナル処理不可

# 修正後
while not shutdown_event.wait(timeout=1.0):  # 1秒ごとに GIL 解放
    pass
```

`wait(timeout=1.0)` はイベントがセットされれば即座に `True` を返すため、シャットダウンの応答性に影響はない。タイムアウト時は 1 秒間スリープするだけなので CPU 負荷もほぼゼロ。

### 問題2: デーモンスレッド内の C++ デストラクタクラッシュ

`_shutdown()` で `join(timeout=3.0)` 後、Python インタプリタが終了する際にデーモンスレッド内の C++ ネイティブライブラリ（ZED SDK、PyTorch）のデストラクタが呼ばれ、`std::terminate()` が発生していた。

```python
# 修正: _shutdown() の末尾に追加
os._exit(0)
```

正常なシャットダウン処理（モード停止、ワーカー停止、join 待機）はすべて `os._exit(0)` の前に実行されるため、データ損失のリスクはない。

## 修正対象ファイル

- `main.py`
  - `_run_headless()`: `shutdown_event.wait()` → タイムアウト付きループ
  - `_shutdown()`: 末尾に `os._exit(0)` 追加

## 補足

- `--compact` / 通常表示モードでは Qt イベントループがシグナルを正しく処理するため、この問題は発生しない
- `os._exit()` は `atexit` ハンドラや `finally` ブロックを実行しないが、本アプリケーションではそれらに依存する処理はない
