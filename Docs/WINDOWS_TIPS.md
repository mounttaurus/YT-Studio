# WINDOWS_TIPS.md — Windows環境特有の注意事項

> ⚠️ このファイルに記載されている問題は **Windows環境特有** のものです。
> Linux / macOS では発生しません。作業環境を確認してから参照してください。

---

## 1. curl の日本語JSON送信問題

### 症状
Windows の `curl`（System32 または Git Bash 同梱版）で日本語を含む JSON ボディを送信すると、
FastAPI 側で以下のエラーが返る：

```json
{"detail": "There was an error parsing the body"}
```

### 原因
Windows の `curl.exe` はデフォルトで CP932（Shift-JIS）エンコーディングを使用する。
`-d` オプションに日本語文字列を渡すと文字化けし、JSONパースに失敗する。

### 確認方法
ASCII文字のみで同じリクエストを送って成功すれば、この問題が原因。

```bash
# NG（日本語を含む）
curl -X POST http://localhost:8002/projects/new \
  -H "Content-Type: application/json" \
  -d "{\"title\": \"AIニュース\"}"

# OK（ASCII のみ）
curl -X POST http://localhost:8002/projects/new \
  -H "Content-Type: application/json" \
  -d "{\"title\": \"AI_News_Test\"}"
```

### 解決策

**① PowerShell の `Invoke-RestMethod` を使う（推奨）**
```powershell
Invoke-RestMethod -Method POST -Uri "http://localhost:8002/projects/new" `
  -ContentType "application/json; charset=utf-8" `
  -Body ([System.Text.Encoding]::UTF8.GetBytes('{"title": "AIニュース", "channel": "main"}'))
```

**② Python でテストする**
```bash
python -c "
import urllib.request, json
data = json.dumps({'title': 'AIニュース', 'channel': 'main'}).encode('utf-8')
req = urllib.request.Request('http://localhost:8002/projects/new', data=data, headers={'Content-Type': 'application/json'})
print(urllib.request.urlopen(req).read().decode('utf-8'))
"
```

**③ Docker コンテナ内から curl を実行する**
```bash
docker exec -it scripting-agent-scripting-agent-1 \
  curl -s -X POST http://localhost:8002/projects/new \
  -H "Content-Type: application/json" \
  -d '{"title": "AIニュース", "channel": "main"}'
```

**④ ブラウザの DevTools コンソールから fetch を使う**
```javascript
fetch('/projects/new', {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify({ title: 'AIニュース', channel: 'main' })
}).then(r => r.json()).then(console.log)
```

### 本番動作への影響
**なし。** この問題はコマンドラインでのテスト時のみ発生する。
WebUI（Alpine.js の `fetch`）や Python httpx は UTF-8 で送信するため影響を受けない。

---

## 2. その他（随時追記）

<!-- 今後 Windows 特有の問題が見つかった場合、このセクションに追記する -->

---

*作成: 2026-06-06 | 対象環境: Windows 10/11 + Docker Desktop*
