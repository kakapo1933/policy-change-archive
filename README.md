# 政策變更檔案館 · Policy Change Archive

每日公開監測 **HEART 政策島**、**臺北安可大舞台**、**臺北施政島** 的公開頁面、資料檔及各筆政策內容。每個實體網址與 JSON 政策條目都各自保存 SHA-256、文字快照及首次／修改／消失等觀測紀錄。專案與三站沒有從屬關係；**臺北安可與臺北施政島均為非官方創作，不能當成臺北市政府官方網站**。

## 每日監測範圍

| 網站 | 逐頁／逐條方式 | 限制 |
|---|---|---|
| HEART 政策島 | `/heart-island`、`/policies` 的 Sitemap、內部連結與官方政策 JSON 的政策原文網址；`islands[].buildings[]` 每項獨立保存 | 不追蹤整個 `puma.taipei` 與本任務無關的頁面 |
| 臺北安可大舞台 | 首頁、`/2026/`、`/tower/`、`/submit/`、`/club/` 及同網域可發現的 HTML、JS、CSS、JSON；另自動追蹤公開 GitHub 原始碼中的 TS/TSX、JS、CSS、HTML、JSON；`platform.json` 與 `achievements.json` 每項獨立保存 | 不進入登入、管理、投稿 API，不擷取影片或圖片二進位檔 |
| 臺北施政島 | 公開 GitHub 專案的網站 HTML、JS、CSS、JSON（由 repository tree 自動發現），以及政策資料與政見追蹤 JSON 中的每一條 | 尚未有可確認的獨立部署網域；只宣稱監測公開原始檔，不假裝監測其線上動態畫面 |

### 蒐集技術與誠實的覆蓋率

- 每日採集時會尋找 `robots.txt`、Sitemap、同網域連結及政策 JSON 清單；可新增的頁面在下一次掃描自動納入，不需手動修改網址。
- 網址只接受 HTTPS、同網域、公開 GET 及無 query 變體，拒絕跨域跳轉、管理／登入路徑。遵守 `robots.txt`；無法確認 robots（除 404）時停止該站的網頁探索，並留下錯誤資訊。
- 預設站點頁數上限 HEART 120、安可 100，政策紀錄每組最多 1,000 筆，安可原始碼最多 190 個、第三站程式碼最多 130 個；對網站逐頁限速約 0.2 秒，單檔大小上限 5 MiB。
- 超過上限、Sitemap 失敗、HTTP 錯誤、JSON 解析錯誤等會在 `data/summary.json` 的 `coverage` 標示。**「探索完成」僅代表此次設定範圍的程序完成，不等於數學上證明全站沒有漏頁。**
- 只有實際 URL 回 HTTP 404／410（曾成功抓取過），或之前存在的某筆政策從**成功讀取**的新 JSON 消失時，才標記 `removed`。僅從站內連結消失但頁面仍可開啟時標記 `unlisted`；抓取失敗不會誤判移除。
- SPA 3D 場景、單一網址下的 hash route、登入後內容、不可見 API、圖片／影片及 JavaScript 動態生成但不在公開資料中的狀態，**無法保證完整覆蓋**。對這類 SPA，我們改以政策 JSON 中**每一筆條目**當成「邏輯頁」逐項觀測，但不是聲稱有真的公開 URL。公開介面會標記這些是從 JSON 拆出的條目。

## 證據可驗證性

- `data/history.json`：依抓取時間追加的完整紀錄（含抓取失敗）；`data/runs/<UTC>.json`：單次執行結果。
- `data/snapshots/<sha256>.gz`：原始 HTTP 回應內文 gzip 快照，內容相同只保留一份。
- `data/texts/<sha256>.txt`：可讀文字快照，JSON 會規範化格式；政策邏輯頁使用單筆 JSON 內容來計算原始 Hash。
- `raw_sha256`：原始內容指紋；`text_sha256`：可讀文字指紋；`diff`：前後版文字差異。每項來源有自己的 URL/項目 ID、UTC 時間、HTTP 狀態與標頭。
- `data/summary.json`：公開查詢頁使用的最新狀態、每站探索品質、三站政策文字重合指標；該指標**不代表政策剽竊**。
- 這些紀錄能說明「在什麼時間觀察到哪個版本」，不能當作網站實際發布時間或作者身分的權威證明。Git 歷史可由有權限的人重寫，關鍵證據仍應搭配第三方封存。

## 開發、測試與部署

Python 3.12 以上，僅使用標準函式庫：

```bash
python3 -m unittest discover -s tests -v
python3 monitor.py
python3 -m http.server 8000
# http://localhost:8000/site/
```

`.github/workflows/policy-watch.yml` 每天台灣時間約 **09:17** 執行（GitHub 排程可能延遲或跳過），也可在 Actions 手動執行。每次擷取會保留所有原始紀錄，並以 Git commit 保存快照。請保持 Repository Actions 的 `contents: write` 權限。

靜態查詢網頁為 `site/index.html`，資料存放於 `data/`；GitHub Pages 需在 `Settings → Pages → Build and deployment → Source` 選擇 **GitHub Actions**。未成功部署前，請勿將預期的 Pages 網址視為已上線。

- 原始碼：<https://github.com/kakapo1933/policy-change-archive>
- Actions：<https://github.com/kakapo1933/policy-change-archive/actions>
- 預期公開查詢網址（以實際部署狀態為準）：<https://kakapo1933.github.io/policy-change-archive/>

原有 2026-10-29 開始的歷史紀錄已保留；**逐頁探索是後來才加上的功能，不能回推舊頁先前的修改歷史。**
