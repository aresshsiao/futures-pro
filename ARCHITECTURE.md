# Futures Pro — 軟體架構設計文件

## 1. 系統總覽

本系統採用 **三層分離架構**：最底層的 Core Service 永久持有券商連線與商品訂閱，
與任何 browser 無關；中間的 Gateway 層負責 FastAPI + WebSocket，把 Core 的資料廣播給前端；
最上層是 Web UI，可任意開關、重整、多開分頁，都不會影響 Core 的連線狀態。

```
┌──────────────────────────────────────────────────────────────┐
│  Web UI  (React JSX，多分頁 / 可隨時重整)                       │
│  ┌──────────────────────┬──────────────────────────────┐     │
│  │   技術分析            │  閃電下單 / 倉位 / 成交         │     │
│  │   K線 + 成交量 + 指標  │  Price Ladder + Orders        │     │
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
│   └── trade_module.py      # 交易模塊 — 獨立於問價模塊
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
└── utils/
    ├── __init__.py
    └── helpers.py           # 工具函式
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

## 5. 事件驅動架構

Core / Gateway 兩層透過同一個 in-process `EventBus` (Pub/Sub) 溝通：

| 事件名稱 | 發送者 | 接收者 | 說明 |
|---------|--------|--------|------|
| `tick` | QuoteModule | BarBuilder, Gateway | 即時逐筆 |
| `bar` | BarBuilder | ScriptEngine, Gateway | K棒更新（含 live 未收完棒） |
| `quote_update` | QuoteModule | Gateway (OrderPanel) | 五檔更新 |
| `indicator_output` | ScriptEngine | Gateway | 指標繪圖資料 |
| `order_placed` | TradeModule | Gateway | 委託送出 |
| `order_filled` | TradeModule | Gateway | 委託成交 |
| `order_cancelled` | TradeModule | Gateway | 委託取消 |
| `position_update` | TradeModule | Gateway | 倉位變動 |
| `script_signal` | ScriptEngine | TradeModule | 策略訊號 |
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
```

## 7. 已知邊界與注意事項

- **Shioaji `subscribe_trade`**：登入時預設會訂閱委託回報頻道；若帳號無 FOP 完整權限會回 406，
  可在 `brokers.yaml` 設 `subscribe_trade: false` 避開（代價：收不到即時委託回報）。
- **單 process 架構**：目前 Core Service 與 Gateway 在同一 Python process、共用 event loop。
  分離為獨立 process（跨機器 / 跨語言）是未來可選的演進方向，屆時 EventBus 需換成跨 process 的訊息佇列。
