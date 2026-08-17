# Futures Pro — 軟體架構設計文件

## 1. 系統總覽

本系統採用 **三層分離架構**：最底層的 Core Service 永久持有券商連線與商品訂閱，
與任何 browser 無關；中間的 Gateway 層負責 FastAPI + WebSocket，把 Core 的資料廣播給前端；
最上層是 Web UI，可任意開關、重整、多開分頁，都不會影響 Core 的連線狀態。

```
┌──────────────────────────────────────────────────────────────┐
│  Web UI  (React JSX，多分頁 / 可隨時重整)                       │
│  ┌──────────────────────┬──────────────────────────────┐     │
│  │   技術分析            │  閃電下單 / 右邊下單 / 倉位     │     │
│  │   K線 + 成交量 + 指標  │  Ladder + 條件單 + Orders     │     │
│  └──────────────────────┴──────────────────────────────┘     │
└───────────────────────────┬──────────────────────────────────┘
                            │  WebSocket / REST
                            │  (只讀資料 + 送出操作指令)
┌───────────────────────────▼──────────────────────────────────┐
│  Gateway Layer  (FastAPI + WebSocket，ui/server.py)           │
│                                                              │
│  • 每個 browser 連線是一個「消費者」，無狀態                    │
│  • forward_tick / forward_bar → broadcast 給所有 client       │
│  • get_history → 讀 DB 回傳（不觸發券商 API）                  │
│  • place_order / cancel_order → 轉呼叫 Core 的 TradeModule     │
│  • 認證 (JWT)、連線管理 (ConnectionManager)                    │
└───────────────────────────┬──────────────────────────────────┘
                            │  EventBus (Pub/Sub, in-process)
┌───────────────────────────▼──────────────────────────────────┐
│  Core Service  (server 啟動即運行，與 browser 生命週期無關)     │
│                                                              │
│  ┌────────────┐  ┌────────────┐  ┌──────────┐  ┌──────────┐ │
│  │QuoteModule │  │TradeModule │  │BarBuilder│  │ScriptEng │ │
│  │ 問價 (永久) │  │ 交易 (永久) │  │ tick→bar │  │ 指標/策略 │ │
│  └─────┬──────┘  └─────┬──────┘  └────┬─────┘  └────┬─────┘ │
│        │  訂閱管理       │              │             │        │
│        │  (去重、單一真相) │              ▼             ▼        │
│        │               │         ┌──────────────────────┐    │
│        │               │         │  Database (SQLite)   │    │
│        │               │         │  歷史 K 線 / Tick     │    │
│        │               │         └──────────────────────┘    │
└────────┼───────────────┼──────────────────────────────────────┘
         │               │
┌────────▼───────────────▼──────────────────────────────────────┐
│              Broker Adapter Layer (券商抽象層)                   │
│  ┌──────────┬──────────┬──────────┬──────────┐                │
│  │ 永豐金    │ 元大期貨   │ 富邦期貨   │ 元富期貨   │                │
│  │ SinoPac  │ Yuanta   │ Fubon    │Masterlink│                │
│  └──────────┴──────────┴──────────┴──────────┘                │
└────────────────────────────────────────────────────────────────┘
```

## 2. 三層職責界線

| 層級 | 職責 | 生命週期 | 狀態 |
|------|------|---------|------|
| **Core Service** | 持有券商連線、訂閱商品、聚合 K 棒、執行 Script、寫入 DB | 隨 server 啟動，永久運行 | 有狀態（單一真相來源） |
| **Gateway Layer** | WebSocket 廣播、REST 查詢、指令路由、認證 | 隨 server 啟動 | 無狀態（每個 client 平等） |
| **Web UI** | 繪圖、下單介面、訂閱請求 | 隨 browser 開關 | 純前端狀態（localStorage） |

**關鍵原則：資料流向單向。** 這裡的「連線」有兩種，別混淆：

- **連線 A — Browser ↔ Gateway (WebSocket)**：每個分頁各一條，隨開關/重整而斷開重連，斷了不影響任何人。
- **連線 B — Server ↔ 券商 (SinoPac API)**：由 Core Service 唯一持有，server 啟動到關閉全程維護。

Web UI **不擁有連線 B 的狀態** — 它不負責建立或維護「Server↔券商」這條連線，
只能「請求訂閱商品」與「讀資料」。連線 B 的建立與維護完全屬於 Core Service，
browser 自己的連線 A 斷線重連完全不影響它。

## 3. 目錄結構

```
futures-pro/
├── main.py                  # 程式進入點 — 啟動 Core Service + Gateway
├── config/
│   ├── __init__.py
│   ├── settings.py          # 全域設定 (ports, paths, 自動連線, 預設商品)
│   ├── brokers.yaml         # 券商帳號設定 (gitignore)
│   └── script_states.json   # Script 啟用/停用狀態持久化 (執行期產生)
│
├── core/                    # ═══ Core Service ═══
│   ├── __init__.py
│   ├── event_bus.py         # 事件匯流排 (Pub/Sub) — 層間唯一溝通管道
│   ├── models.py            # 共用資料模型 (Tick, Bar, Order, Position, ...)
│   ├── quote_module.py      # 問價模塊 — 持有連線 + 訂閱去重
│   ├── trade_module.py      # 交易模塊 — 獨立於問價模塊
│   ├── fill_ledger.py       # 成交明細推算 (新倉/平倉別、已實現損益)
│   └── condition_module.py  # 條件單引擎 (右邊下單) — 見 §7，尚未實作
│
├── brokers/
│   ├── __init__.py
│   ├── base.py              # 抽象基底類 (QuoteAdapter / TradeAdapter)
│   └── adapters/
│       ├── __init__.py
│       └── sinopac.py       # 永豐金 Shioaji (Quote + Trade adapter)
│
├── data/
│   ├── __init__.py
│   ├── database.py          # SQLite 資料庫管理 + 交易日曆
│   ├── bar_builder.py       # Tick → Bar 聚合器
│   └── sources/
│       ├── __init__.py
│       ├── taifex.py        # 期交所 CSV 下載 & 轉換
│       └── broker_sync.py   # 從券商 API 同步歷史資料
│
├── scripts/                 # ═══ Script 引擎 (Plugin) ═══
│   ├── __init__.py
│   ├── engine.py            # Script 執行引擎 + ScriptContext API
│   └── builtin/             # 自動掃描載入，無需改 main.py
│       ├── ma.py            # 內建指標: 移動平均
│       ├── rsi.py           # 內建指標: RSI
│       ├── kd.py            # 內建指標: KD
│       ├── volume_alert.py  # 內建指標: 成交量爆量水平線
│       ├── window_price.py  # 內建指標: 滾動 N 棒高低水平線
│       └── breakout.py      # 內建策略: 突破
│
├── backtest/
│   ├── __init__.py
│   └── engine.py            # 回測引擎
│
├── ui/                      # ═══ Gateway Layer ═══
│   ├── __init__.py
│   ├── server.py            # FastAPI + WebSocket 伺服器 + 訊息路由
│   ├── auth.py              # JWT 認證
│   └── static/              # 前端
│       └── trading-platform.jsx
│
├── utils/
│   ├── __init__.py
│   └── logging_setup.py     # 全系統日誌設定（唯一碰 logging 設定的地方）
│
├── tests/                   # 依「被測的那一層」分類，對應上面的結構
│   ├── conftest.py          # 專案根目錄加進 sys.path（測試檔自己不必處理）
│   ├── core/                # 交易模塊、成交明細推算
│   ├── brokers/             # 券商 adapter
│   ├── data/                # SQLite、期交所匯入
│   ├── gateway/             # main.py 的 handler、送給前端的 payload
│   ├── utils/               # 日誌設定
│   └── manual/              # 手動執行的診斷腳本（會連真實 API），pytest 不收集
│
└── logs/                    # 執行期產生 (gitignore)
    ├── futures.log          # 全部訊息
    ├── error.log            # WARNING 以上
    ├── trade.log            # 下單/成交/倉位
    └── shioaji.log          # 永豐 API 自己寫的檔
```

## 4. 核心設計原則

### 4.1 Core Service 擁有連線，UI 不擁有
券商連線的**擁有權**屬於 Core Service，但 UI 仍保留**主動操作**與**唯讀查詢**的能力。
關鍵是區分「使用者主動操作」與「頁面自動副作用」：

**UI 保留的能力（使用者主動觸發）**
- 券商選擇與登入：使用者在券商面板選券商 → 按「連線」→ 送 `broker_config {action:"connect"}`，後端嘗試登入並回報成功/失敗。
- 斷線：使用者按「斷線」→ 送 `broker_config {action:"disconnect"}`。
- 查詢連線狀態：隨時送 `broker_status`（唯讀）→ 後端回 `{quote:{connected}, trade:{connected}}`，供狀態燈顯示。

**UI 不做的事（避免副作用）**
- 頁面**載入 / 重整**時**不自動** connect，只查狀態；確認後端確實未連線，才依 localStorage 記住的偏好嘗試連線。
- 後端另加 guard：券商已連線時，`broker_config connect` 直接回「已連線」，**不走 disconnect→reconnect**，避免把其他分頁的連線踢掉。

**結論**：多分頁 / 重整 browser 都不會重新登入券商或踢掉現有連線；
「連線券商」是使用者手指觸發的管理操作，不是頁面載入的自動行為。

### 4.2 訂閱去重 — 單一真相來源
- `QuoteModule` 持有 `_subscriptions` 集合，同一商品只會向券商訂閱一次。
- 不同 browser 分頁看同一商品，共用 Core 的同一條訂閱，避免重複訂閱與重複計費。
- 商品的 tick/bar 透過 `EventBus` 廣播，Gateway 再 fan-out 給所有連線的 client。

### 4.3 問價與交易完全分離
- `QuoteModule` 和 `TradeModule` 各自持有獨立的 broker adapter。
- 可以同時連接不同券商：例如用永豐金問價、用元大下單。
- 兩個模塊透過 `EventBus` 溝通，不直接耦合。

### 4.4 券商抽象層 (Adapter Pattern)
- 所有券商實作統一介面 `QuoteAdapter` / `TradeAdapter`。
- 新增券商只需新增一個 adapter 檔案，無需改動核心邏輯。
- 同一 process 內共用單一 Shioaji session（`_SHARED_API`），避免重複登入。

### 4.5 Script 引擎 (Plugin Architecture)
- `scripts/builtin/` 目錄 **自動掃描載入**，新增 Script 只需放入 .py 檔，無需改 `main.py`。
- 分為兩類：`indicator`（計算並回傳繪圖資料）、`strategy`（可觸發交易訊號）。
- Script 透過 `ScriptContext` 存取市場資料與繪圖 (`ctx.plot`, `ctx.vol_plot`, `ctx.sub_plot`)。
- 啟用/停用狀態持久化於 `config/script_states.json`，重啟 server 後保留。

### 4.6 資料層 — DB 優先
- 歷史查詢 **優先讀 SQLite**：Core 一直在跑並持續寫入最新 K 棒，browser 重整時直接讀 DB（毫秒級），不觸發券商 API。
- 只有 DB 無該商品資料時（首次載入 / 換商品）才打券商 API，抓回後寫入 DB。
- 券商 API 的同步阻塞呼叫（如 `kbars()`）一律以 `run_in_executor` 移出 event loop，避免凍結廣播。
- `BarBuilder` 負責將 Tick 即時聚合為各週期 K 棒。

### 4.7 日誌 — 單一設定點
- 只有 `utils/logging_setup.py` 會動 logging 設定（`main.py` 啟動時呼叫一次，
  刻意排在其他 import 之前）；其他模組一律 `logging.getLogger(__name__)`。
- 三個檔案各有分工：`futures.log` 全部、`error.log` 只有 WARNING 以上（出事先看這個）、
  `trade.log` 只有下單/成交/倉位（交易紀錄不該被報價洗掉）。
- **每日 06:00 換檔**而非午夜：夜盤跑到隔天 05:00，午夜換檔會把同一個交易日切成兩個檔案。
  保留天數與等級見 `config/settings.py` 的 `LOG_*`。
- 檔案等級（預設 DEBUG）可比 console（預設 INFO）詳細 —— 螢幕上看不下的細節，事後翻檔案還在。
- `sys.excepthook` / `threading.excepthook` 都接進 log：券商 callback 跑在子執行緒，
  沒接的話那裡爆掉只會看到畫面停止更新、log 一片安靜。
- uvicorn 以 `log_config=None` 啟動，讓它的 HTTP 存取紀錄走同一套 handler 進同一批檔案。

## 5. 事件驅動架構

Core / Gateway 兩層透過同一個 in-process `EventBus` (Pub/Sub) 溝通：

| 事件名稱 | 發送者 | 接收者 | 說明 |
|---------|--------|--------|------|
| `tick` | QuoteModule | BarBuilder, Gateway | 即時逐筆 |
| `bar` | BarBuilder | ScriptEngine, Gateway | K棒更新（含 live 未收完棒） |
| `quote_update` | QuoteModule | Gateway (OrderPanel) | 五檔更新 |
| `indicator_output` | ScriptEngine | Gateway | 指標繪圖資料 |
| `order_placed` | TradeModule | Gateway | 委託送出 |
| `order_update` | TradeModule | Gateway | 委託狀態變更（成交進度、對帳修正） |
| `order_filled` | TradeModule | Gateway | 單筆成交（新倉/平倉與損益為本地推算） |
| `order_cancelled` | TradeModule | Gateway | 委託取消 |
| `positions_update` | TradeModule | Gateway | 倉位變動（整份清單） |
| `fills_update` | TradeModule | Gateway | 對帳後的完整成交明細（損益已用券商結算值覆蓋） |
| `script_signal` | ScriptEngine | TradeModule | 策略訊號 |
| `condition_update` | ConditionModule | Gateway | 條件單新增/狀態變更/刪除（整筆條件）— §7，尚未實作 |
| `quote_connected` / `quote_disconnected` | QuoteModule | Gateway | 連線狀態變更 |
| `trade_connected` / `trade_disconnected` | TradeModule | Gateway | 連線狀態變更 |

**event loop 保護**：`bar` 事件在每個 tick 都會發出（live 未收完棒），
`on_bar_complete` 對 live 棒直接 early-return，只有收完的棒才寫 DB / 跑 Script，
避免每個 tick 都阻塞 asyncio event loop。

## 6. 關鍵資料流

### 6.1 即時報價（Core → UI，單向廣播）
```
SinoPac tick callback (子執行緒)
  → EventBus.emit_sync("tick")  → call_soon_threadsafe 排進主 loop
  → BarBuilder 聚合 → emit("bar")
  → Gateway forward_bar → broadcast → 所有 browser 分頁
```

### 6.2 歷史 K 線（UI → Gateway → DB，DB 優先）
```
UI: get_history(symbol, timeframe, count)
  → DB 有足夠資料？ → 是 → 直接回傳（不打券商 API）
                    → 否 → 券商 API (run_in_executor) → 寫 DB → 回傳
```

### 6.3 下單（UI → Gateway → Core）
```
UI: place_order → Gateway → TradeModule.place_order → SinoPac adapter
  → 委託回報 callback → EventBus → Gateway broadcast → 所有分頁更新
                     ↘ 下單/成交後排一次跟券商對帳（refresh_from_broker）
```
- 拿不到委託序號 = 券商沒收下這張單 → `REJECTED`，**不進委託簿**（否則畫面會有一張刪不掉的幽靈單），
  拒絕原因由 adapter 的 `last_error` 帶到前端顯示。
- 倉位不能只信本地推算：成交回報漏接時畫面會停在舊數字，使用者反覆按平倉等於反覆送真實市價單。
  因此下單／成交後都會排一次 `refresh_from_broker()`（同一時間只留一個待辦），
  券商端的庫存整份覆蓋本地；UI 的倉位面板也有「⟳ 同步」可手動觸發。
- **成交明細分兩段送**：單筆 `fill` 立刻推（讓畫面馬上看到成交），對帳完成後再推一次完整的 `fills`。
  前端不自己重拉：一張市價單可能分成上百筆成交回報，每筆各拉一次就是逼後端連打上百發券商 API。
- **新倉/平倉與已實現損益是自己推算的**（`core/fill_ledger.py`）：券商的成交回報只有買/賣，
  看不出這一筆是進場還出場；`list_profit_loss` 要等結算才查得到，而且按「已平倉部位」彙總、
  對不回單筆成交。`FillLedger` 依成交順序重播部位，替每筆標上 `oc_type` / `closed_qty` / `pnl`，
  成交當下畫面就有數字（標 `pnl_estimated`，未扣手續費）；對帳時 `_merge_fills_with_pnl()`
  再用券商結算好的數字覆蓋。留倉單的進場成本不在今日成交裡，那幾口標平倉但損益留空等券商補。
  重播前要先由「券商倉位 − 今日成交淨額」反推開盤前部位，否則平留倉單會被判成新倉，
  之後整天的新倉/平倉全部反過來。
- **委託狀態不能只認委託回報**：市價單成交後券商送的是**成交回報**，不保證再補一次委託回報。
  成交回報的 `order_id` 就是委託序號，`_apply_fill_to_order()` 用它累加成交口數並結成
  `partial` / `filled`，否則市價單會永遠卡在畫面的「委託中」。對帳時再用券商的委託狀態覆蓋一次。

## 7. 條件單引擎（右邊下單）

> **實作狀態**：UI 已完成（`RightSideOrderPanel`），**後端引擎尚未實作**。
> 本節是動工前的設計定稿；目前條件只存在瀏覽器 localStorage，不會觸發、不會送單。

「右邊下單」是條件單機：在**壓力價掛空、支撐價掛多**，價格碰到就自動追價進場，
進場後由系統管理停利／停損／保本／移動停損。它跟閃電下單的差別是「先設好、後自動執行」，
使用者設完條件就不必盯著畫面按滑鼠。

### 7.1 為什麼引擎必須放後端

前端 JS 也能監控報價並送單，但不能這樣做：

- **瀏覽器不是常駐程式**：分頁關掉、電腦休眠、手滑重整，監控就停了 —— 使用者卻以為單還在等觸發。
  條件單的壽命必須跟 Core Service 一樣長，而不是跟某個分頁一樣長。
- **多分頁會重複送單**：開兩個分頁 = 兩份監控 = 同一個條件觸發時送出兩張單。
- **前端拿到的是廣播後的 tick**，比 Core 慢一段，追價的意義被延遲吃掉。

因此 `ConditionModule` 是 Core Service 的模塊，與 `TradeModule` 並列，
條件的**單一真相在後端**；前端只負責顯示與 CRUD 指令 —— 這是 §4.1「Core 擁有狀態」原則的延伸。

### 7.2 條件的資料模型

| 欄位 | 型別 | 說明 |
|------|------|------|
| `id` | str | uuid[:8] |
| `symbol` | str | TX / MTX / TMF |
| `side` | `buy` / `sell` | UI 的「支撐多」= buy、「壓力空」= sell |
| `trigger_price` | float | 觸發價 |
| `chase` | int | 追點 — 進場單穿價的點數 |
| `qty` | int | 口數 |
| `take_profit` | int | 利點（0 = 不設停利） |
| `stop_loss` | int | 損點，UI 以負數輸入（0 = 不設停損） |
| `cost_guard` | bool | 成本防線 |
| `trail` | bool | 觸後跟隨 |
| `status` | enum | 見 §7.3 |
| `entry_order_id` / `entry_price` / `entry_filled_qty` | | 進場結果 |
| `exit_order_id` | | 出場委託 |
| `peak_price` | float | 觸後跟隨用：進場後最有利價 |
| `created_at` / `updated_at` | datetime | |

### 7.3 狀態機

```
主線
  waiting ─觸及觸發價─► triggered ─送出追價單─► sent ─成交回報─► filled ─停利/停損─► exited
  等待觸發              已觸發                  已送單           已成交            已出場
                                                                  │
                                            浮盈 ≥ |損點| 時插入   ▼
                                                              guarded 已守成本
                                                          （停損移到進場價，續走出場）

支線
  使用者刪除（任何階段） → cancelled
  進場單被券商拒絕        → failed      （不自動重試，UI 顯示拒絕原因）
  重啟後對不上倉位        → orphaned    （停住等人工確認，見 §7.8）
```

主線這六個狀態正好對應 UI 狀態圖例的六個燈號。

**離開 `waiting` 必須是觸發判斷的第一件事**。現有觸價單踩過這個坑（見 `_check_stop_orders`）：
送單是 async 的，在它完成前每一筆 tick 都會再判一次，狀態沒有立刻改掉就會送出好幾張單。

### 7.4 觸發與追價（進場）

觸發方向 —— 注意這跟現有觸價單**相反**，右邊下單是在壓力/支撐**逆勢**接單：

| 條件 | 觸發判斷 | 意義 |
|------|---------|------|
| 壓力空 (`sell`) | `tick.price >= trigger_price` | 漲到壓力價 → 放空 |
| 支撐多 (`buy`) | `tick.price <= trigger_price` | 跌到支撐價 → 作多 |

觸發後**不送市價單**，而是送一張穿價限價單：

```
賣：limit_price = trigger_price − chase   （掛得比市價低 → 立即成交）
買：limit_price = trigger_price + chase   （掛得比市價高 → 立即成交）
```

**為什麼不用市價單**：台指期市價單的滑價無上限，急殺急拉時成交價可能離觸發價很遠；
穿價限價單一樣會立刻成交，但把最差成交價鎖在 `chase` 點以內 —— 追點的本質是「可接受的滑價上限」，
不是「掛單偏移量」。這也是截圖裡 17059 觸發、掛 17049 賣單的原因。

代價：極端行情下穿不過去就掛在那裡不成交。這時條件停在 `sent`，
由使用者決定要不要手動處理（**不自動改價重送** —— 追價迴圈在跳空時會一路追到底）。

### 7.5 出場管理

進場成交後以 `entry_price`（實際成交均價，不是觸發價）為基準，四種出場規則同時運作：

- **停利／停損**：`entry_price ± take_profit` / `∓ |stop_loss|`。兩者是 OCO，
  一邊觸及就送出場單並把條件推進 `exited`，另一邊立即失效。
- **成本防線**（`cost_guard`）：浮動獲利 ≥ `|stop_loss|` 時，把停損價移到 `entry_price`（保本），
  狀態轉 `guarded`。門檻用損點而非利點，是為了讓「賺到夠賠的量」就先立於不敗。
- **觸後跟隨**（`trail`）：進場後記錄 `peak_price`（多單取最高、空單取最低），
  停損維持在 `peak_price ∓ |stop_loss|`，**只往有利方向移動、不回退**。
- **收盤清倉**：見 §7.6。

出場單同樣用穿價限價單（`chase` 沿用同一個值），理由同 §7.4。
停利/停損**不掛在券商端**，由本模塊監控 tick 後才送單 —— 與現有觸價單的設計一致：
券商不一定支援 OCO 或條件單，本地管理才能跨券商行為一致。
代價要寫清楚：**Core 沒在跑就沒有保護**，這與「掛在券商端的真實停損單」不同。

### 7.6 全域開關、當沖與收盤清倉

- **啟動交易／暫停交易**：暫停時**不觸發新的進場**（條件留在 `waiting`），
  但**已進場部位的停利停損照常運作**。暫停不能連出場保護一起關掉，否則按下暫停等於裸倉。
- **當沖**：影響下單的新倉/平倉別 —— 進場 `octype="new"`、出場 `octype="cover"`，
  並把「收盤清倉」預設打開。
- **收盤清倉**：到指定時間（日盤 13:44、夜盤 04:59，可設定）平掉本引擎產生的部位，
  並停用所有未觸發條件。只清本引擎自己的部位，不碰使用者手動下的單。

### 7.7 資料流與事件

```
UI: add_condition / update_condition / delete_condition / set_condition_trading
  → Gateway 路由 → ConditionModule（寫 DB）
  → emit("condition_update") → Gateway broadcast → 所有分頁同步

tick (EventBus)
  → ConditionModule._check_conditions()      # 與 TradeModule 各自獨立判斷
  → 觸發 → TradeModule.place_order(限價, 穿價)  # 共用同一個下單入口
  → order_filled (EventBus) → 記錄進場均價 → 開始管理出場
  → 每次狀態變更都 emit("condition_update")
```

新增的 WebSocket action：`get_conditions`、`add_condition`、`update_condition`、
`delete_condition`、`set_condition_trading`。前端改為**不再用 localStorage 存條件**，
一律以後端推來的清單為準（多分頁才會一致）。

### 7.8 持久化與重啟對帳

條件寫進 SQLite（新表 `conditions`），server 重啟後載回記憶體。
**重啟後最危險的是「進行中」的條件**：本地以為還有部位，實際上可能已被券商端平掉（或反之），
自動送出場單就是憑空多一筆交易。

處理方式：啟動時先 `refresh_from_broker()` 取真實倉位，再與 `filled` / `guarded` 的條件比對 ——
對得上就繼續管理；對不上一律標成 `orphaned` 停在那裡等人工確認，**絕不自動送單**。

### 7.9 與現有觸價單的關係

現有 `OrderType.STOP_BUY / STOP_SELL`（`trade_module._check_stop_orders`）是單層觸價：
觸發後補送一張市價單，沒有追價、沒有括號單、沒有移動停損，方向也是順勢突破。
條件單引擎**不重用它**（狀態機與出場管理差太多），但兩者共用 `TradeModule.place_order` 送單，
且各自獨立訂閱 tick。閃電下單的觸買/觸賣維持原行為不變。

### 7.10 實作分期

每一期都能獨立上線（UI 已經在那裡了）：

| 期別 | 範圍 | 完成後可用的狀態 |
|------|------|-----------------|
| **P1** | 條件 CRUD + SQLite 持久化 + 觸發 + 追價進場 | waiting → triggered → sent → filled |
| **P2** | 停利／停損（OCO 出場） | + exited |
| **P3** | 成本防線 + 觸後跟隨 | + guarded |
| **P4** | 收盤清倉 + 當沖旗標 + 重啟對帳 | + orphaned |

P1 的驗收標準：模擬帳戶設一個貼近市價的條件，觸發後在 `trade.log` 看到追價單送出、
成交後條件停在 `filled`，且**同一個條件只送出一張單**。

## 8. 已知邊界與注意事項

- **`OrderState` 的值是全大寫**：`FDEAL` / `FORDER` / `SDEAL` / `SORDER`，不是 `FuturesDeal`。
  委託與成交共用同一個 callback，靠這個值分派 —— 用 `endswith("Deal")` 比對永遠不會中，
  成交回報會整批被當成委託回報解析（欄位全空），結果就是畫面看不到成交、倉位不動。
  `_is_deal_report()` 除了忽略大小寫，還會用訊息結構兜底（委託是巢狀 order/status，成交是平坦 trade_id/ordno）。
- **Shioaji `subscribe_trade`**：登入時預設會訂閱委託回報頻道；若帳號無 FOP 完整權限會回 406，
  可在 `brokers.yaml` 設 `subscribe_trade: false` 避開（代價：收不到即時委託回報）。
- **單 process 架構**：目前 Core Service 與 Gateway 在同一 Python process、共用 event loop。
  分離為獨立 process（跨機器 / 跨語言）是未來可選的演進方向，屆時 EventBus 需換成跨 process 的訊息佇列。
