# 政策變更檔案館（Policy Change Archive）

監測三個不同來源：

1. **HEART 政策島** — `https://puma.taipei/heart-island`；另抓官方 `heart-island-assets/policies.json`
2. **臺北安可大舞台** — `https://encore.taipei/`；另抓公開 GitHub 的 `src/data/platform.json` 與 `src/data/achievements.json`
3. **臺北施政島** — `bounce12340/chiang-wanan-policy-island` 公開原始碼（首頁 HTML、政策 JSON、2022 政見 JSON）。第三站目前採用可確認的 GitHub 原始資料，而非猜測未確認的正式網域。它是獨立創作者專案，**非臺北市政府官網**。

## 執行

```bash
python3 monitor.py
python3 -m http.server 8000
# 開啟 http://localhost:8000/site/
```

不需額外 Python 套件。GitHub Actions `.github/workflows/policy-watch.yml` 每天台灣時間約 09:17 執行（GitHub cron 可能延遲或偶爾漏執行，也可手動觸發）。執行前必須確認 GitHub Actions 已對此儲存庫啟用，工作流程必須允許 `contents: write`。每次執行會新增 `data/runs/` 紀錄，並更新 `data/history.json`、`data/summary.json`。

## 完整性／證據範圍

- `raw_sha256` 計算 **HTTP 回傳原始內容**；`text_sha256` 計算擷取的**主要文字**（略過 HTML script/style；JSON 正規化鍵順序），可區分版型調整與可讀內容修改。
- `data/snapshots/<sha>.gz` 保存壓縮原始內容，`data/texts/<sha>.txt` 保存文字；相同 Hash 僅存一次。`data/runs/<timestamp>.json` 記錄 UTC 時間、HTTP 狀態、ETag、Last-Modified、兩種 Hash、diff、擷取失敗原因。資料不會因新一次失敗而抹除前一次成功版本。
- 每一次抓取記錄一次觀察結果，**只能證明當下觀察到什麼**，不能保證真正修改時間、發布時間、作者身份，也不能單靠文字重合程度認定抄襲。
- 第三站透過 `raw.githubusercontent.com` 的 `main` 分支抓取原始碼；這是追蹤 GitHub 最新公開內容，而非某個未驗證部署的網站。若之後找到可驗證的部署網址，可額外加入監測資源。
- 文字相似度是「最新版政策 JSON 中的文字片段 Jaccard」（排除來源網址、日期與 metadata），只用來提示可能重合的文字，不能當作內容來源鑑定。
- 純前端 SPA 網站主要文字可能在 JavaScript bundle/API 中，不一定包含在首頁 HTML；原始 HTML 變化仍可被 Hash 監測，但文字比較覆蓋率有限。HTML 抓取不等於整站內容封存，也不含動態渲染後畫面與第三方資產。
- Git 本身並非防竄改的權威時間戳：有寫入權限的人可改寫 Git 歷史。需搭配第三方封存、可驗證簽章或透明日誌才能提高不可抵賴性。
- 每次執行會追加記錄，就算內容不變或 HTTP 請求失敗也保留紀錄；若全部來源無法抓取，Actions 會標示失敗但會先提交錯誤記錄。

## 部署公開查詢頁面

`site/index.html` 是純靜態網頁，從 `../data/summary.json` 讀取資料。GitHub Actions 會發布 `site/` 與 `data/`，主頁自動導向 `/site/`。

**GitHub Pages 尚需啟用**：進入儲存庫 `Settings → Pages → Build and deployment → Source`，選 **GitHub Actions**。`GITHUB_TOKEN` 無法自行啟用尚不存在的 Pages 設定；若儲存庫為 private，帳號必須符合 GitHub Pro / Team / Enterprise 的 Pages 權限。待 GitHub deployment 成功後才可將其視為公開網站。

公開獨立儲存庫：<https://github.com/kakapo1933/policy-change-archive>。先前的 UTC 觀測紀錄和 SHA-256 會原樣保留；遷移提交時間不等同原始擷取時間。
