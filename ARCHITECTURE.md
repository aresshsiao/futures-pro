# Futures Pro — 軟體架構設計文件

## 1. 系統總覽

```
┌─────────────────────────────────────────────────────────┐
│                    Web UI (React JSX)                    │
│  ┌──────────────────────┬──────────────────────────────┐ │
│  │   技術分析            │  閃電下單 / 倉位 / 成交       │ │
│  │   K線 + 成交量 + 倉位  │  Price Ladder + Orders       │ │
│  └──────────────────────┴──────────────────────────────┘ │
├─────────────────────────────────────────────────────────┤
│                  WebSocket / REST API                     │
│                 (FastAPI + WebSocket)                     │
├──────────┬──────────┬───────────┬───────────────────────┤
│  Quote   │  Trade   │  Script   │   Backtest             │
│  Module  │  Module  │  Engine   │   Engine               │
│ (問價模塊) │ (交易模塊) │ (腳本引擎)  │  (回測引擎)             │
├──────────┴──────────┴───────────┴───────────────────────┤
│              Broker Adapter Layer (券商抽象層)              │
│  ┌──────────┬──────────┬──────────┬──────────┐          │
│  │ 永豐金    │ 元大期貨   │ 富邦期貨   │ 元富期貨   │          │
│  │ SinoPac  │ Yuanta   │ Fubon    │Masterlink│          │
│  └──────────┴──────────┴──────────┴──────────┘          │
├─────────────────────────────────────────────────────────┤
│                   Data Layer (資料層)                      │
│  ┌───────────────┬────────────────┬────────────────┐    │
│  │  SQLite DB     │  期交所 CSV     │  券商即時資料    │    │
│  │  (歷史K線/Tick) │  (手動下載轉換)  │  (API 同步)     │    │
│  └───────────────┴────────────────┴────────────────┘    │
└─────────────────────────────────────────────────────────┘
```

## 2. 目錄結構

```
futures-pro/
├── main.py                  # 程式進入點
├── config/
│   ├── __init__.py
│   ├── settings.py          # 全域設定 (ports, paths, defaults)
│   └── brokers.yaml         # 券商帳號設定 (gitignore)
│
├── core/
│   ├── __init__.py
│   ├── event_bus.py         # 事件匯流排 (Pub/Sub)
│   ├── models.py            # 共用資料模型 (Tick, Bar, Order, Position, etc.)
│   ├── quote_module.py      # 問價模塊 — 獨立於交易模塊
│   ├── trade_module.py      # 交易模塊 — 獨立於問價模塊
│   └── order_manager.py     # 委託管理 (限價/市價/觸價單)
│
├── brokers/
│   ├── __init__.py
│   ├── base.py              # 抽象基底類 (QuoteAdapter / TradeAdapter)
│   └── adapters/
│       ├── __init__.py
│       ├── sinopac.py       # 永豐金 Shioaji
│       ├── yuanta.py        # 元大期貨
│       ├── fubon.py         # 富邦期貨
│       └── masterlink.py    # 元富期貨
│
├── data/
│   ├── __init__.py
│   ├── database.py          # SQLite 資料庫管理
│   ├── bar_builder.py       # Tick → Bar 聚合器
│   └── sources/
│       ├── __init__.py
│       ├── taifex.py        # 期交所 CSV 下載 & 轉換
│       └── broker_sync.py   # 從券商 API 同步歷史資料
│
├── scripts/
│   ├── __init__.py
│   ├── engine.py            # Script 執行引擎 (沙箱)
│   ├── api.py               # Script 可用的 API (ctx.buy, ctx.sell, ...)
│   ├── loader.py            # Script 載入 / 管理
│   └── builtin/
│       ├── ma_cross.py      # 內建指標: 均線交叉
│       ├── rsi.py           # 內建指標: RSI
│       ├── macd.py          # 內建指標: MACD
│       └── breakout.py      # 內建策略: 突破
│
├── backtest/
│   ├── __init__.py
│   ├── engine.py            # 回測引擎
│   ├── portfolio.py         # 虛擬投資組合
│   └── report.py            # 回測績效報告
│
├── ui/
│   ├── __init__.py
│   ├── server.py            # FastAPI + WebSocket 伺服器
│   ├── ws_handler.py        # WebSocket 訊息路由
│   └── static/              # React build output
│       └── trading-platform.jsx
│
└── utils/
    ├── __init__.py
    ├── logger.py            # 統一日誌
    └── helpers.py           # 工具函式
```

## 3. 核心設計原則

### 3.1 問價與交易完全分離
- `QuoteModule` 和 `TradeModule` 各自持有獨立的 broker adapter
- 可以同時連接不同券商：例如用永豐金問價、用元大下單
- 兩個模塊透過 `EventBus` 溝通，不直接耦合

### 3.2 券商抽象層 (Adapter Pattern)
- 所有券商實作統一介面 `QuoteAdapter` / `TradeAdapter`
- 新增券商只需新增一個 adapter 檔案，無需改動核心邏輯
- 支援同時登入多家券商

### 3.3 Script 引擎 (Plugin Architecture)
- Script 不內建於主架構，採用匯入式
- 分為兩類：`indicator`（計算並回傳繪圖資料）、`strategy`（可觸發交易訊號）
- Script 在受限沙箱中執行，透過 `ScriptAPI` 存取市場資料與下單

### 3.4 資料層雙軌來源
- 歷史資料：期交所 CSV 手動下載 → 解析 → 轉換 → SQLite
- 即時資料：券商 API → 統一格式 → 合併至資料庫
- `BarBuilder` 負責將 Tick 即時聚合為各週期 K 棒

## 4. 事件驅動架構

系統核心採用事件匯流排，所有模塊透過事件溝通：

| 事件名稱 | 發送者 | 接收者 | 說明 |
|---------|--------|--------|------|
| `tick` | QuoteModule | BarBuilder, UI | 即時逐筆 |
| `bar` | BarBuilder | ScriptEngine, UI | K棒完成 |
| `quote_update` | QuoteModule | UI (OrderPanel) | 五檔更新 |
| `order_placed` | TradeModule | OrderManager, UI | 委託送出 |
| `order_filled` | TradeModule | OrderManager, UI | 委託成交 |
| `order_cancelled` | TradeModule | OrderManager, UI | 委託取消 |
| `position_update` | OrderManager | UI | 倉位變動 |
| `script_signal` | ScriptEngine | TradeModule | 策略訊號 |
| `stop_triggered` | OrderManager | TradeModule | 觸價單觸發 |
