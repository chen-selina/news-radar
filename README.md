# 台股新聞雷達

每天爬取台股新聞，用「關鍵字＋情緒詞典」判斷對哪些個股是利多或利空，並在事後用真實股價驗證預測準不準。
需求與規則見 [`PROJECT_BRIEF.md`](PROJECT_BRIEF.md)；畫面與分析規則以原型 [`news-radar.html`](news-radar.html) 為準。

## 目前進度

- [x] 第 1 階段：專案骨架、資料庫、載入全部上市櫃公司
- [x] 第 2 階段：分析引擎移植＋單元測試
- [x] 第 3 階段：五個來源的爬蟲（Google新聞、天下雜誌暫停，見下方「爬取新聞」）
- [x] 第 4 階段：預測鎖定存檔
- [x] 第 5 階段：股價抓取＋驗證計算
- [x] 第 6 階段：API
- [x] 第 7 階段：前端改接 API
- [x] 第 8 階段：排程與部署說明

## 快速開始（第一次在新電腦上）

依序執行（每條指令的說明在下面各節）：

```bat
cd "news radar"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set PYTHONIOENCODING=utf-8
python -m radar.stocks.load
python -m radar.crawl
python -m radar.prices update
start_site.bat
```

`start_site.bat` 會啟動網站並自動打開瀏覽器（http://127.0.0.1:8000），關掉那個黑色視窗就停止。
最後設定每天自動執行（見「排程」）：

```bat
tools\schedule_windows.bat
```

## 安裝（Windows，cmd）

需要 Python 3.11 以上。

```bat
cd "news radar"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set PYTHONIOENCODING=utf-8
```

`PYTHONIOENCODING=utf-8` 是為了讓 cmd 能正常顯示中文訊息，每開一個新視窗都要設一次。

## 載入上市櫃公司

```bat
python -m radar.stocks.load
```

會從證交所與櫃買中心的官方 OpenAPI 下載公司基本資料與收盤價，寫進 `data/radar.db`（約 2,000 家）。
資料庫不進版控，每位隊友在自己電腦跑一次即可；公司有新上市、下市時重跑一次。

| 資料 | 來源 |
|---|---|
| 上市公司基本資料 | `openapi.twse.com.tw/v1/opendata/t187ap03_L` |
| 上櫃公司基本資料 | `www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O` |
| 上市收盤價 | `openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL` |
| 上櫃收盤價 | `www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes` |

其他選項：

- `--save-raw data\raw`：順便把官方原始 JSON 存檔
- `--from-raw data\raw`：不連網，用存檔的 JSON 載入
- `--db 路徑`：改用別的資料庫檔

### 公司詞典怎麼組成

| 欄位 | 來源 |
|---|---|
| 名稱、全名、上市／上櫃、官方產業別、股數 | 官方資料 |
| 市值 | 股數 × 最近收盤價。**產業連動每個族群取市值最大的前 4 檔** |
| 別名 | 自動去掉「-KY」「*」後綴（臻鼎-KY → 臻鼎），加上 `config/stock_overrides.json` 的人工別名 |
| 細分族群（記憶體、AI伺服器、CPO…） | `config/stock_overrides.json`，人工維護 |

`stock_overrides.json` 目前是原型裡的 59 檔，要補其他公司的族群或別名，直接在這個檔加一行，例如：

```json
"2337": {"name": "旺宏", "aliases": [], "sectors": ["記憶體"]},
```

改完重跑 `python -m radar.stocks.load`。如果寫的代號不存在，或 `name` 跟官方簡稱不同，載入時會顯示「注意」提醒。

## 爬取新聞

```bat
python -m radar.crawl
```

依 [`config/crawl.json`](config/crawl.json) 的開關爬取各來源，每個來源最多 30 則，存進 `data/radar.db`。

- `--source cw`：只抓某個來源，可重複指定（例如 `--source bw --source cw`）
- `--limit 5`：每個來源最多幾則
- `--dry-run`：只列出會收錄的新聞，不存檔

| 來源 | 抓取方式 | 模組 |
|---|---|---|
| Yahoo奇摩股市 | 台股 RSS `tw.stock.yahoo.com/rss?category=tw-market` | `radar/crawl/sources/yahoo.py` |
| Google新聞 | 「台股」搜尋 RSS（**預設關閉**，見下方） | `radar/crawl/sources/google.py` |
| 經濟日報 | 證券 RSS `money.udn.com/rssfeed/news/1001/5590/5607` | `radar/crawl/sources/udn.py` |
| 商業周刊 | 財經頻道列表頁（只收 `/business/blog/`，`/indep/` 是廣編稿不收） | `radar/crawl/sources/bw.py` |
| 天下雜誌 | 「今日最新」列表頁（**預設關閉**，見下方；帶 `?rec=` 的推薦舊文不收） | `radar/crawl/sources/cw.py` |

網站改版時，列表的網址與規則改各來源的模組；文章內文擷取的區塊改 `radar/crawl/extract.py` 的 `BODY_SELECTORS`。

**爬取規則**

- 每個網址先檢查 robots.txt（`radar/crawl/robots.py`），不允許的不讀；同一網站兩次請求至少間隔 1.5 秒。
- 沒有原文網址的不收錄。網址會先去掉追蹤參數（`utm_`、`rec` 等）再當唯一鍵。
- 去重：網址相同，或 3 天內標題相同（忽略空白與標點）視為同一則。
- 內文最多存 3,000 字，只供分析。讀不到內文時改用 RSS／網頁摘要，再不行就只用標題分析；
  `news.body_from` 記錄是哪一種（page／summary／title）。網頁標示非免費閱讀時 `paywalled = 1`。
- 文章頁暫時連不上又沒有摘要時先不存，下次爬取會重試。

**Google新聞為什麼暫停**：news.google.com 的 robots.txt 不允許一般程式讀取 `/rss/`；而 Google 的連結
是轉址，2024 年後的新格式必須再向 Google 查詢才能得到原始網址，同樣被 robots.txt 禁止。
程式已經寫好（舊格式可以不連網解出原始網址），要啟用請先決定如何處理 robots.txt，再改 `crawl.json`。

**天下雜誌為什麼暫停**：robots.txt 允許爬取，但網站對本爬蟲回應 403（拒絕存取），在兩台電腦上結果相同。
專案決定不改 User-Agent 繞過網站阻擋；天下的文章可以用「貼連結分析」手動加入。

## 公司短名稱的排除規則

全市場有一千多家公司的簡稱只有兩個字，很容易撞到一般用詞或跨詞誤判，
例如「大量賣單」→ 3167 大量、「投信大買」→ 1109 信大、「東南亞」→ 1303 南亞、「工商時報」→ 8923 時報。

所以**兩個字的公司名要有佐證才算**，任一項成立即可：

| 佐證 | 例子 |
|---|---|
| 旁邊有股票代號 | 能率（5392）、2468 華經 |
| 用括號標出 | 【精測】、「欣銓」 |
| 用頓號與其他公司並列 | 旺宏、力成下午舉行法說會 |
| 後面緊接股市用語（營收、股價、法說、漲停、逆勢…） | 台玻逆勢、尖點 9 月營收 |
| 前面緊接股市用語（點名、調節、買超、○○廠、外資…） | 調節友達、鑽針廠尖點 |
| 同一則新聞已確認過這家公司 | 前面有「南紡（1440）」，後面的「南紡」也算 |

不需佐證：三個字以上的名稱、`trusted` 名單（市值較大且不易混淆的 158 家，例如鴻海、聯電、華碩）、
`stock_overrides.json` 的人工別名（台積、虎航）。`exclude_phrases` 裡的片語（東南亞、統一化…）一律不算。

全部設定在 [`config/name_rules.json`](config/name_rules.json)，程式在 `radar/analysis/names.py`。
這套規則是原型沒有的：與原型比對的測試不使用它，正式流程會使用（見下一節的 `production_analyzer()`）。

## 產業連動只看關鍵字所在句子

原型的做法是：產業關鍵字所在的句子沒有情緒時，改用**整則新聞的總分**。結果生活新聞裡隨口一提的詞也會連動，
例如甜甜圈店新聞「有人拿起**手機**拍下來」讓手機族群 +1.0、ETF 新聞「績效集中在**半導體**產業」讓晶圓代工族群 +6.5。

正式版改成：**只用關鍵字所在句子的情緒；句子沒有情緒就不連動。只有關鍵字出現在標題時，才退回用整則總分**
（標題通常就是整篇的主題，例如「機器人、記憶體題材熱夯」）。

以約 240 則新聞試算，產業連動從 37 組降到 27 組，原型的 15 則範例新聞不受影響。
設定在 `radar/analysis/engine.py` 的 `sector_fallback`（`"total"`＝原型、`"title"`＝正式版）。

正式流程（爬蟲建立預測、`python -m radar.analysis`）用 `production_analyzer()` 建立分析器，
會同時開啟這項修正與公司短名稱排除規則；與原型比對的測試兩者都不開。

## 預測鎖定

`python -m radar.crawl` 每存一則新聞，就**立刻**用當時的詞典分析，把結果存進 `predictions`
（總分、利多／利空／中性、詞典版本）與 `prediction_stocks`（每檔個股的分數、方向、直接提及或產業連動、判斷理由）。

- 每則新聞只有一筆原始預測。資料庫觸發器會擋住修改、刪除，也不能事後追加個股（`radar/db.py`）。
- 之後改詞典，舊預測完全不受影響；預測記錄的詞典版本可以用 `lexicon.get(conn, 版本)` 還原。
- 爬取時沒能建立預測的新聞（例如還沒載入公司資料），下次爬取或執行下面指令時會補建：

```bat
python -m radar.predict
```

想看新詞典對舊新聞的效果，用「重算」，結果另外存在 `recalcs`／`recalc_stocks`，不碰原始預測：

```bat
python -m radar.predict --recalc
```

## 收盤價

```bat
python -m radar.prices update
```

從最早一則新聞的前一個月抓到今天，已抓過的日子會跳過；每天收盤後跑一次即可補上當天。

| 資料 | 來源 |
|---|---|
| 交易日曆＋加權指數 | 證交所 `FMTQIK`（一次一個月；只列實際開市日，國定假日、颱風休市自然排除） |
| 上市個股收盤價 | 證交所 `MI_INDEX`（一次一天、全部上市股票） |
| 上櫃個股收盤價 | 櫃買中心 `afterTrading/otc`（一次一天、全部上櫃股票） |

證交所請求太密集會暫時封鎖，所以同網站請求間隔 3.5 秒（`radar/config.py` 的 `HOST_INTERVALS`），
第一次抓一個多月的資料約需 3 分鐘。官方當天資料還沒公布時會顯示「下次再試」。

- `--from 2026-09-01`、`--to 2026-10-27`：指定日期範圍
- 也可以匯入自己的收盤價 CSV（欄位：日期, 代號, 收盤價；日期可用民國年；大盤代號寫 `TAIEX`、加權指數或大盤）：

```bat
python -m radar.prices import 收盤價.csv
```

## 預測驗證

```bat
python -m radar.validate
```

預設看第 3 個交易日、扣除大盤、持平門檻 ±1%。選項：`--horizon 1/3/5/10`、`--raw`（個股漲跌）、
`--threshold 2`（門檻 2%）、`--direct`（只看直接提及的個股）、`--recalc 2`（改用第 2 版詞典的重算結果）。

| 規則 | 說明 |
|---|---|
| 基準價 | 13:30 前發布取前一個交易日收盤；13:30 後發布取當日收盤（當日休市取之前最後一個交易日）；沒有時間視為盤前 |
| 第 N 日 | 以證交所實際交易日計算 |
| 實際方向 | 報酬（預設扣除加權指數報酬）高於門檻為漲、低於負門檻為跌，其餘持平 |
| 命中 | 預測利多且實際漲，或預測利空且實際跌；中性預測不計入 |
| 待驗證 | 未滿 N 個交易日顯示「等待 N 日」；新聞當天收盤價還沒出來顯示「等待基準價」 |

輸出包含：整體／利多／利空命中率與筆數、多空報酬差、低／中／高影響程度各自的命中率、
新聞前 3 日到後 10 日的平均累積報酬（事件研究圖的資料）、逐筆結果。
網頁透過 `GET /api/validation` 取得同一份結果（`radar/validate.py` 的 `validate()`）。

## 開啟網站

```bat
python -m radar.api
```

用瀏覽器打開 http://127.0.0.1:8000 就是網站。按 Ctrl+C 停止。
平常最快的方式：直接點兩下 `start_site.bat`，會啟動網站並在 3 秒後自動打開瀏覽器；網站已經開著時，再點一次只會打開瀏覽器，不會重複啟動。
想更方便：在 `start_site.bat` 按右鍵 →「顯示其他選項」→「傳送到」→「桌面（建立捷徑）」，之後點桌面圖示即可。
排程（爬新聞、抓收盤價）不需要網站開著。
預設只有自己的電腦連得到；要讓同一個網路的隊友連進來，加 `--host 0.0.0.0`。

畫面沿用原型 `news-radar.html`，但所有資料都來自資料庫（透過下面的 API）：

| 分頁 | 和原型不同的地方 |
|---|---|
| 最新新聞 | 顯示資料庫裡的新聞與**鎖定的預測**（不會因為改詞典而變）。多了期間選擇（今天／近 3、7、30 天／全部）、「載入更多」；搜尋也會比對受影響個股的代號與名稱。「重新爬取新聞」真的會去爬（開發用，約需數分鐘） |
| 貼連結分析 | 由伺服器讀網頁（不再借用 Claude 桌面版的瀏覽器），任何瀏覽器都能用。讀不到時說明原因，可手動貼上內文。「加入新聞列表」會存進資料庫並鎖定預測 |
| 情緒詞典 | 新增、改分數、刪除會改 `config/lexicon.json` 並建立新版本，所有人共用；顯示版本紀錄 |
| 預測驗證 | 用真實收盤價，拿掉模擬的示範資料與新聞 CSV 匯入（新聞來自爬蟲）。可切換「原始預測」或「第 N 版詞典重算」比較詞典修改的效果；可抓最新收盤價或匯入收盤價 CSV |

網頁檔案在 `web/`（`index.html`、`app.js`、`style.css`），改完重新整理瀏覽器即可，不需要重新啟動。

## 排程

PROJECT_BRIEF 要求每天 **08:00（開盤前）** 與 **14:30（收盤後）** 各跑一次。我們用 **Windows 工作排程器**，
每次執行 `run_jobs.bat`，也就是 `python -m radar.jobs`：

1. 爬新聞，存檔的同時鎖定預測（等於 `python -m radar.crawl`）
2. 抓收盤價，只抓還沒有的交易日（等於 `python -m radar.prices update`）

預測驗證在網站打開時即時計算，收盤價一進資料庫，驗證結果就跟著更新，不需要另外排程。

### 設定（一次就好）

```bat
tools\schedule_windows.bat
```

會建立兩個工作「TWNewsRadar 0800」「TWNewsRadar 1430」。可以在「工作排程器」程式裡看到，或用指令確認：

```bat
schtasks /query /tn "TWNewsRadar 0800"
```

取消排程：

```bat
tools\unschedule_windows.bat
```

想立刻手動跑一次：

```bat
run_jobs.bat
```

### 要注意的地方

- **電腦要開著、不能睡眠**。錯過的那次不會補跑；不過下一次會補上缺的收盤價，
  RSS 和列表頁通常也保留一兩天的新聞，偶爾錯過一次影響不大。
- **時間以電腦的時鐘為準**，電腦時區要設成台北（UTC+8）。
- **14:30 那次常抓不到當天收盤價**：證交所、櫃買中心當天的資料通常下午才公布完整，
  抓不到會記為「部分完成」並註明「下次再試」，隔天 08:00 那次會補上。
- **執行紀錄**：
  - 輸出存在 `data/logs/jobs-年-月.log`。
  - 網站上方的「上次排程」顯示最近一次的結果，滑鼠移上去可以看摘要。
  - 「部分完成」代表有某個來源或某天的收盤價沒抓到，細節看 log。
- **同時只跑一個**：排程執行中會留下 `data/jobs.lock`。這時候再觸發排程會直接略過，
  網站的「重新爬取新聞」也會回覆「排程正在執行」。程式異常中斷留下的鎖檔，超過 2 小時會自動失效。
- 公司清單（新上市、市值）不在排程裡，建議每個月手動重跑一次 `python -m radar.stocks.load`。

### 其他作業系統

macOS／Linux 的隊友可以用 cron，例如（`crontab -e`，路徑換成自己的）：

```
0 8  * * * cd /path/to/news-radar && PYTHONIOENCODING=utf-8 .venv/bin/python -m radar.jobs
30 14 * * * cd /path/to/news-radar && PYTHONIOENCODING=utf-8 .venv/bin/python -m radar.jobs
```

### 為什麼不用 GitHub Actions 或 APScheduler

- **GitHub Actions**：資料庫 `data/radar.db` 存在自己電腦、不進版控，GitHub 的機器拿不到。
  如果改成每次把資料庫 commit 回 repo，鎖定的預測就會變成可以被任何人改寫的檔案，也會讓 repo 越來越大。
- **APScheduler**（排程寫在網站程式裡）：網站要一直開著才會跑。工作排程器不需要網站開著，電腦重開後也還在。

## 部署與協作

### 在自己電腦上使用

照「快速開始」做完即可。網站沒有登入功能，所以：

- 預設只有自己的電腦連得到（127.0.0.1）。
- 要讓**同一個網路**的隊友連進來，用 `start_site.bat --host 0.0.0.0` 啟動，隊友打開 `http://你的電腦IP:8000`。
  第一次會跳出 Windows 防火牆詢問，選「私人網路」。
- **不要開放到網際網路**：任何連得到的人都能改詞典、觸發爬蟲。

### 和隊友用 GitHub 共同開發

| 進版控（大家共用） | 不進版控（每個人自己的） |
|---|---|
| 程式（`radar/`、`web/`、`tests/`、`tools/`） | 資料庫 `data/radar.db`（新聞、鎖定的預測、收盤價） |
| 設定（`config/`：詞典、公司別名、爬取開關、排除規則） | 排程紀錄 `data/logs/`、鎖檔 |
| `.bat` 檔、README | 虛擬環境 `.venv/` |

- 每個人都有自己的資料庫，各自爬、各自鎖定預測。建議**指定一台電腦當正式資料**：只有它設定排程，
  命中率也以它為準。其他人的資料庫只用來開發、測試。
- **詞典版本號是各資料庫自己編的**：同一份 `config/lexicon.json`，在你的資料庫可能是第 3 版，在隊友那邊是第 5 版。
  兩邊內容是否相同，看 `lexicon_versions` 資料表裡的 `checksum` 欄位。
  在網站改詞典會修改 `config/lexicon.json`，記得 commit，隊友 pull 之後下次分析時會自動建立新版本。
- 改了程式的人請先跑 `python -m pytest`，全部通過再推上去。
- `.bat` 檔只能用英文（ASCII）並用 Windows 換行（CRLF），否則 cmd 會讀錯。

### 備份

最重要的資料是 `data/radar.db` 裡**鎖定的預測**：它們是「當下」的判斷，事後無法重建。
請定期把 `data/radar.db` 複製到別的地方（例如雲端硬碟）。複製前先關掉網站，並確認排程沒有在執行。

## 網站後端 API

網站啟動後，打開 http://127.0.0.1:8000/docs 可以看到所有 API 的說明，也能直接在網頁上按「Try it out」試用。

| API | 用途 |
|---|---|
| `GET /api/news?label=&source=&q=&stock=&since=` | 新聞列表，附上鎖定的原始預測與受影響個股；`label` 為 pos／neg／neu，`source` 多一個 `other`（其他網站）；`highlight=true` 附上原文要標色的關鍵詞位置 |
| `GET /api/news/summary?since=&source=` | 情緒分布與個股影響排行（網頁側欄） |
| `GET /api/news/{id}` | 單則新聞；另附原文上要標色的關鍵詞位置（用鎖定時的詞典版本重新標示，分數仍以鎖定的預測為準） |
| `POST /api/news` | 「加入新聞列表」：存檔並立刻鎖定預測。必須有原文網址；同一網址已存在回傳 409 |
| `POST /api/analyze-text` | 分析標題與內文，不存檔 |
| `POST /api/analyze-url` | 貼連結分析：後端讀網頁、擷取標題與內文後分析，不存檔 |
| `GET /api/validation?horizon=3&benchmark=excess&threshold=0.01&scope=all` | 預測驗證；加 `lexicon_version=` 改驗證該版詞典的重算結果 |
| `POST /api/recalc` | 用目前的詞典重算所有新聞（另存，不改原始預測） |
| `GET／POST／DELETE /api/lexicon` | 看詞典與版本紀錄／新增或改分數／刪除（`?term=`）；每次修改都會建立新版本 |
| `POST /api/crawl` | 手動觸發爬蟲（開發用，會花幾分鐘；同時只能跑一個） |
| `POST /api/prices/update`、`/api/prices/import` | 抓收盤價／匯入 CSV（開發用） |
| `GET /api/stocks?q=`、`GET /api/meta` | 搜尋公司；來源清單、詞典版本、資料筆數與價格資料範圍 |

錯誤一律回傳 `{"detail": "白話說明"}`。

**貼連結分析**讀不到內容時不會出錯，而是回傳 `ok: false` 與原因，讓網頁請使用者手動貼上內文：

| 狀況 | 回傳的說明 |
|---|---|
| robots.txt 不允許 | 網站不允許程式讀取，請手動貼上 |
| HTTP 401／403 | 網站拒絕讀取，可能需要登入或阻擋程式（目前天下雜誌就是這樣） |
| HTTP 404 | 找不到這個網頁 |
| 逾時、網路問題 | 連不上網站 |
| 不是網頁（PDF 等） | 這個網址不是網頁 |
| 網頁上沒有文字 | 可能需要登入或內容由 JavaScript 載入 |

讀到了但只有摘要（`partial`）或網站標示付費（`paywalled`）時仍會分析，並附上提醒。
網址不在五個指定來源內時 `source_name` 為「其他網站」。網址指向本機或內部網路（127.0.0.1、192.168.x.x）會拒絕，
避免這個功能被拿來讀內部服務。

## 分析一則新聞

```bat
python -m radar.analysis "標題" "內文"
```

會顯示利多／利空、總分、命中的詞、受影響個股與判斷理由。調整詞典時可以用來快速檢查。

分析規則與原型 `news-radar.html` 的 `analyze()` 完全相同（見 `radar/analysis/engine.py` 檔頭）。
產業連動每個族群取**市值最大的前 4 檔**。

## 情緒詞典與版本

正式詞典是 [`config/lexicon.json`](config/lexicon.json)，一個詞一行：

- `sentiment`：情緒詞與分數，正數利多、負數利空、0 代表停用
- `sectors`：產業關鍵字 → 族群（例如 `"HBM": "記憶體"`）

詞典會進版控，隊友共用同一份。資料庫會存每一版的完整快照：
程式每次讀詞典時，只要發現檔案內容跟最新版本不同，就自動建立新版本（第 1 版、第 2 版…）。
之後鎖定預測時，會記錄當時用的是第幾版，事後改詞典也不會影響舊預測。

否定詞（未、不、無…）、加強詞（大幅、強勁…）屬於規則，寫在 `radar/analysis/engine.py` 開頭。

## 測試

```bat
python -m pytest
```

測試不需要連網：

| 測試檔 | 內容 |
|---|---|
| `test_engine_parity.py` | 與原型 `analyze()` 逐項比對（15 則範例新聞＋22 個邊界案例） |
| `test_engine_rules.py` | PROJECT_BRIEF 第 5 節的規則，一條一條寫成看得懂的例子；產業連動的標題例外 |
| `test_lexicon.py` | 詞典版本：改檔案會產生新版本、舊版本能完整還原 |
| `test_predict.py` | 預測鎖定：存了不能改、改詞典不影響舊預測、重算另外存 |
| `test_names.py` | 公司短名稱的排除規則：一般用詞不算、有佐證才算 |
| `test_prices.py` | 官方股價資料解析、只抓缺的日子、CSV 匯入 |
| `test_validate.py` | 基準價（盤前／盤後、颱風休市）、扣除大盤、門檻、命中、事件研究圖 |
| `test_crawl.py` | robots.txt、網址整理、各來源解析、內文擷取、去重 |
| `test_api.py` | 每個 API：分析、貼連結的各種失敗原因、加入列表後預測鎖定、詞典版本、驗證參數 |
| `test_jobs.py` | 排程：某一項失敗不影響下一項、執行紀錄、同時只跑一個 |
| `test_stocks.py`、`test_db.py` | 公司載入、資料庫格式升級、預測鎖定 |

原型的分析規則如果有更新，用 Node.js 重新產生標準答案，再跑一次測試：

```bat
node tools\make_golden.js
```

## 專案結構

```
radar/            後端程式
  config.py       路徑、時區（台北 UTC+8）、爬蟲 User-Agent 與請求間隔
  db.py           SQLite 資料表；預測與詞典版本建立後鎖定，不能修改或刪除
  http.py         共用的 HTTP 用戶端（固定 User-Agent、同網站請求間隔、重試）
  stocks/         公司詞典：官方資料抓取（sources.py）、產業別代碼（industries.py）、載入（load.py）、查詢（query.py）
  analysis/       分析引擎（engine.py）、詞典與版本（lexicon.py）、公司短名稱排除規則（names.py）
  crawl/          爬蟲：robots.txt、網址整理、內文擷取、存檔去重、各來源模組（sources/）、貼連結讀單篇網址（single.py）
  predict.py      預測鎖定與重算（建立預測時套用公司短名稱排除規則）
  prices/         交易日曆與收盤價：官方資料（sources.py）、抓取與 CSV 匯入（update.py）
  validate.py     預測驗證（回測）
  api/            網站後端（FastAPI）：app.py 是所有 API
  jobs.py         排程要做的事：爬新聞→抓收盤價，寫 log 與執行紀錄
config/           人工維護的設定（stock_overrides.json、lexicon.json、crawl.json、name_rules.json）
data/             本機資料庫 radar.db、排程 log（都不進版控）
tests/            單元測試與測試資料
web/              網站（index.html、app.js、style.css），由 python -m radar.api 提供
news-radar.html   原型（規格參考，不再使用）
tools/            開發工具（make_golden.js：從原型產生標準答案）、排程設定（schedule_windows.bat、unschedule_windows.bat）
run_jobs.bat      排程執行的檔案
start_site.bat    啟動網站並打開瀏覽器
```

## 已知限制

- 公司短名稱排除規則（見「公司短名稱的排除規則」）是依約 240 則新聞設計的，實際新聞會更複雜；
  發現新的混淆詞時，補進 `config/name_rules.json`。已知會漏掉沒有佐證的冷門兩字公司（例如「信義0.92元」）。
- 產業連動改成只看關鍵字所在句子（見「產業連動只看關鍵字所在句子」）後，像「電子權值股、PCB 族群走強」
  這種句子裡沒有情緒詞的合理連動也會被拿掉；長句子裡剛好有不相干的情緒詞時，仍可能誤觸。
- 2026-10-28 實作排除規則前建立的 6 筆預測已鎖定，保留舊結果；用 `python -m radar.predict --recalc` 可比較。
- 資料表格式有變動時，程式會自動升級舊資料庫（`radar/db.py` 的 `MIGRATIONS`）。
- 天下「今日最新」包含健康、生活等非財經文章，會被收錄但通常分析為中性。
- 經濟日報與 Yahoo 的 robots.txt 允許一般爬蟲，但禁止 AI 爬蟲；這兩個來源由團隊成員在本機試跑確認（2026-10-28）。
  檢查擷取結果可用 `python tools\check_extract.py 網址`。
- 證交所的收盤價 API 偶爾會落後幾天（例如颱風休市後），市值只用來排序，影響不大。
