"""
tests/conftest.py — pytest 全域設定

目錄依「被測的那一層」分類，對應專案本身的結構：

    tests/core/      核心模塊（交易/倉位、成交明細推算）
    tests/brokers/   券商 adapter
    tests/data/      資料層（SQLite、期交所匯入）
    tests/gateway/   Gateway 層（main.py 的 handler、送給前端的 payload）
    tests/utils/     共用工具（日誌設定）
    tests/manual/    要手動執行的診斷腳本 —— 會連真實 API，pytest 不收集
                     （見專案根目錄的 pytest.ini）

這裡把專案根目錄加進 sys.path，各測試檔才能直接 `from core... import`。
放在 conftest 是因為 pytest 會先載入它；測試檔自己 sys.path.insert 的話，
檔案一搬進子目錄，相對層數就錯了。
"""
import sys
from pathlib import Path

# 確保專案根目錄在 import 路徑中
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
