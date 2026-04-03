"""
tests/conftest.py — pytest 全域設定
將專案根目錄加入 sys.path，使 import 正常運作。
"""
import sys
from pathlib import Path

# 確保專案根目錄在 import 路徑中
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
