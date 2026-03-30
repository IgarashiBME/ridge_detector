# PWA キャッシュ問題のトラブルシューティング

## 概要

PWA（Progressive Web App）のフロントエンドを更新した際に、ブラウザに変更が反映されない問題が発生した。原因の特定に時間を要したため、ナレッジとして記録する。

## 発生した症状

- HTMLに追加した要素（モデル選択ドロップダウン）がページ表示時に出ない
- Ctrl+F5（スーパーリロード）では表示されるが、F5（通常リロード）では表示されない
- サーバー側のファイルは正しく更新済み

## 原因の階層構造

ブラウザがWebページをキャッシュする仕組みは複数の層があり、それぞれ独立して動作する。今回は3層すべてが関与していた。

```
[リクエスト] → [Service Worker] → [ブラウザHTTPキャッシュ] → [サーバー]
                  ↑ 第3層            ↑ 第2層                    ↑ 第1層
                  (根本原因)
```

### 第1層: JS/CSSファイルのHTTPキャッシュ

- **症状**: HTMLは新しいが、JS/CSSが古いバージョンのまま
- **試した対策**: `app.js?v=2` のようなクエリパラメータ（キャッシュバスター）をHTMLに追記
- **結果**: JS/CSSには効くが、HTML自体のキャッシュには対応できない
- **問題点**: ファイル更新のたびに手動で `v=` の数値を上げる運用が必要

### 第2層: HTMLファイルのHTTPキャッシュ

- **症状**: `index.html` 自体が古いバージョンで表示される（キャッシュバスター付きの新しい参照が読まれない）
- **試した対策**: FastAPIにミドルウェアを追加し、HTMLレスポンスに `Cache-Control: no-cache` ヘッダーを付与
- **結果**: 効果なし
- **理由**: Service Workerがリクエストをインターセプトするため、HTTPヘッダーが評価される前にキャッシュ済みレスポンスが返される

### 第3層: Service Worker のキャッシュ（根本原因）

- **症状**: 上記すべての対策が無効。Ctrl+F5のみ有効
- **原因**: `sw.js` が **cache first** 戦略を採用していた

```javascript
// 旧: cache first（キャッシュにあればサーバーに問い合わせない）
caches.match(event.request).then(cached => cached || fetch(event.request))
```

- **解決策**: **network first** 戦略に変更

```javascript
// 新: network first（常にサーバーから取得、失敗時のみキャッシュ）
fetch(event.request)
  .then(response => {
    const clone = response.clone();
    caches.open(CACHE_NAME).then(cache => cache.put(event.request, clone));
    return response;
  })
  .catch(() => caches.match(event.request))
```

## Service Worker のキャッシュ戦略

| 戦略 | 動作 | 適するケース |
|------|------|-------------|
| Cache First | キャッシュ優先、なければネットワーク | 変更が少ない静的アセット（フォント、アイコン） |
| Network First | ネットワーク優先、失敗時キャッシュ | 頻繁に更新されるアプリのHTML/JS/CSS |
| Stale While Revalidate | キャッシュを即返し、裏でネットワーク更新 | 速度と鮮度のバランスが必要な場合 |
| Network Only | 常にネットワーク | API呼び出し |
| Cache Only | 常にキャッシュ | インストール時に完結するアセット |

## 教訓

1. **PWAではService Workerが最優先のキャッシュ層である。** HTTPヘッダーやHTMLメタタグでのキャッシュ制御はService Workerの後段であり、Service Workerがリクエストをインターセプトしている限り無意味。

2. **開発中のPWAはnetwork first戦略が安全。** cache firstは本番環境でオフライン対応が重要な場合にのみ採用すべき。ローカルネットワーク内で使うアプリ（本プロジェクトのようなケース）ではnetwork firstが適切。

3. **CACHE_NAMEのバージョンを上げることで古いキャッシュを破棄できる。** Service Worker自体が更新された場合、`activate` イベントで旧バージョンのキャッシュを削除する仕組みを入れておく。

4. **Ctrl+F5で直るがF5で直らない場合、Service Workerを疑う。** Ctrl+F5はService Workerをバイパスするため、この挙動の差はService Workerが原因であることを示す強い指標。

5. **キャッシュバスター（`?v=N`）はService Workerの前では無力。** Service Workerがcache firstでリクエストをインターセプトする場合、HTMLファイル自体がキャッシュから返されるため、HTMLに書かれたキャッシュバスターは読まれない。

## 関連ファイル

- `web/sw.js` — Service Worker（キャッシュ戦略の定義）
- `web/manifest.json` — PWAマニフェスト
- `server/app.py` — FastAPIアプリケーション（Cache-Controlミドルウェア）
