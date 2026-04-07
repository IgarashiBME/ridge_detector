# アノテーションマスク表示の競合状態（Race Condition）

## 概要

PWAのアノテーション画面で、ラベル付き画像に表示されるべき半透明赤マスクが表示されない問題が発生した。Ctrl+F5では必ず表示されるが、画像間を移動すると消失する再現性のある不具合であった。

## 発生した症状

- アノテーション済みフレームを選択しても、赤いマスク領域が表示されない場合がある
- Ctrl+F5（スーパーリロード）すると正しく表示される
- 別の画像に移動して戻ると、再び表示されなくなる
- 初回アクセス時（キャッシュなし）は正常に表示されることが多い

## 原因

`selectFrame()` 内で2つの非同期処理が並行して実行されており、完了順序の保証がないことが原因。

```javascript
async selectFrame(filename) {
  // (1) 画像の読み込み — img.onload で drawAnnotation() を呼ぶ
  const img = new Image();
  img.onload = () => {
    this.annImage = img;
    this.resizeCanvas();
    this.drawAnnotation();   // ← ここでしか描画されない
  };
  img.src = `/api/sessions/.../frames/${filename}`;

  // (2) アノテーションの取得 — await で完了を待つ
  const res = await fetch(`.../annotation`);
  const data = await res.json();
  // annLeftLine, annRightLine, annLoadedPoly をセット
  // ← この後に drawAnnotation() が呼ばれていなかった
}
```

### タイムライン比較

**正常に表示されるケース（画像がキャッシュにない場合）:**

```
時間 →
fetch(annotation)  |====|  完了 → データセット
img.onload               |======|  完了 → drawAnnotation() ← データあり → マスク描画 ✓
```

**表示されないケース（画像がブラウザキャッシュにある場合）:**

```
時間 →
img.onload         |=|  即完了 → drawAnnotation() ← データ未取得 → マスクなし ✗
fetch(annotation)  |====|  完了 → データセット（しかし再描画されない）
```

## pwa_cache_troubleshooting.md との関連

前回のService Workerキャッシュ問題とは直接のメカニズムは異なるが、共通するパターンがある。

| 項目 | 前回（Service Worker） | 今回（Race Condition） |
|------|----------------------|----------------------|
| 症状 | Ctrl+F5で直るがF5で直らない | Ctrl+F5で直るが画像移動で再発 |
| 根本原因 | Service Workerのcache-first戦略 | 画像キャッシュによるonloadタイミング変化 |
| キャッシュの影響 | HTMLがキャッシュから返される | 画像がキャッシュから即座に返される |
| 共通点 | **キャッシュの有無が処理の実行順序を変え、想定外の挙動を引き起こす** |

## 修正内容

`web/app.js` の `selectFrame()` で、アノテーションのfetch完了後にも `drawAnnotation()` を呼ぶように修正。

```javascript
// 修正前
} catch (e) { /* no annotation */ }
this._updateStepIndicator();

// 修正後
} catch (e) { /* no annotation */ }
this.drawAnnotation();        // ← 追加: fetch完了後に再描画
this._updateStepIndicator();
```

これにより、画像の読み込み完了時とアノテーションの取得完了時の両方で `drawAnnotation()` が呼ばれるため、どちらが先に完了しても最終的にマスクが正しく描画される。

## 教訓

1. **非同期処理の完了順序に依存しない設計にする。** `img.onload`（イベントベース）と `await fetch()`（Promiseベース）が混在する場合、実行順序はネットワーク状態やキャッシュに依存する。描画の更新は両方の完了時点で行うべき。

2. **「Ctrl+F5で直る」はキャッシュ関連の問題の強い指標。** ただし原因はService Workerだけではなく、ブラウザの画像キャッシュが非同期処理のタイミングを変えるケースもある。

3. **キャッシュはタイミングを変える。** キャッシュの主な影響は「古いデータが返される」ことだが、「レスポンスが高速になることで非同期処理の実行順序が変わる」という二次的影響もある。後者は見落としやすい。

## 関連ファイル

- `web/app.js` — `selectFrame()`, `drawAnnotation()`（修正箇所）
- `doc/pwa_cache_troubleshooting.md` — 前回のキャッシュ関連問題の記録
