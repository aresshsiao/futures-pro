# Futures Pro

台灣期貨交易平台，支援多券商、技術分析、策略腳本與回測。

## 功能

- **多券商支援**：永豐金 (Shioaji)、元大、富邦、元富，問價與交易可使用不同券商
- **即時報價**：訂閱商品逐筆 Tick，自動聚合為各週期 K 棒
- **技術分析**：K 線圖 + 成交量 + 倉位視覺化
- **閃電下單**：Price Ladder 快速下單介面
- **策略腳本**：載入自訂 indicator / strategy，可自動觸發下單
- **回測引擎**：使用歷史資料驗證策略
- **資料管理**：SQLite 儲存歷史 K 棒，支援期交所 CSV 匯入

## 安裝

```bash
pip install -r requirements.txt
```

券商 SDK 按需安裝（預設不安裝）：

```bash
pip install shioaji          # 永豐金
# pip install fubon-neo      # 富邦期貨
```

## 啟動

```bash
python main.py
```

預設在 `http://localhost:8888` 提供 Web UI。

## 券商設定

複製範本並填入帳號資訊（此檔案已加入 .gitignore）：

```bash
cp config/brokers.yaml.example config/brokers.yaml
```

## 目錄結構

```
futures-pro/
├── main.py              # 程式進入點
├── config/              # 全域設定、券商帳號
├── core/                # 事件匯流排、問價/交易模塊、資料模型
├── brokers/             # 券商抽象層與各券商 Adapter
├── data/                # SQLite 資料庫、BarBuilder、資料來源
├── scripts/             # Script 引擎、內建指標與策略
├── backtest/            # 回測引擎與績效報告
├── ui/                  # FastAPI + WebSocket 伺服器、React UI
└── utils/               # 日誌、工具函式
```

詳細架構設計請參閱 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 技術棧

| 層級 | 技術 |
|------|------|
| Web Server | FastAPI + uvicorn |
| 即時通訊 | WebSocket |
| 前端 | React JSX |
| 資料處理 | pandas + numpy |
| 資料庫 | SQLite (內建) |
| 設定 | PyYAML |
