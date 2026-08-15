"""
tests/test_sinopac_kbars.py — 測試永豐 API 能否抓到一整天的 K 棒

執行方式:
    # python tests/test_sinopac_kbars.py
"""
import sys
import yaml
import shioaji as sj
from datetime import date, timedelta
from collections import defaultdict

# ── 設定 ─────────────────────────────────────────────
SYMBOL    = "TX"         # 要測試的商品
SJ_CODE   = "TXF"        # Shioaji 代碼
TEST_DATE = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")  # 昨天

# ── 讀取 credentials ──────────────────────────────────
with open("config/brokers.yaml", encoding="utf-8") as f:
    creds = yaml.safe_load(f)["sinopac"]

print(f"=== 永豐 API K棒測試 ({TEST_DATE}) ===\n")

# ── 登入 ──────────────────────────────────────────────
api = sj.Shioaji()
sessions = api.login(
    api_key=creds["api_key"],
    secret_key=creds["secret_key"],
)
print(f"登入成功，帳號數: {len(sessions)}\n")

# ── 取得近月合約 ──────────────────────────────────────
try:
    contract = api.Contracts.Futures[SJ_CODE + "R1"]  # 近月主力, e.g. TXFR1
    print(f"合約: {contract.code}  {contract.name}  到期: {contract.delivery_date}\n")
except Exception as e:
    print(f"ERROR 取得合約失敗: {e}")
    api.logout()
    sys.exit(1)

# ── 測試 1: kbars() 日K ────────────────────────────────
print("── 測試 1: kbars() 日K（最近 5 天）──")
try:
    start = (date.today() - timedelta(days=8)).strftime("%Y-%m-%d")
    end   = date.today().strftime("%Y-%m-%d")
    kbars = api.kbars(contract=contract, start=start, end=end)
    print(f"  回傳筆數: {len(kbars.ts)}")
    if kbars.ts:
        from datetime import datetime
        for i in range(min(5, len(kbars.ts))):
            dt = datetime.fromtimestamp(kbars.ts[i] / 1e9)
            print(f"  [{i}] {dt.strftime('%Y-%m-%d')}  O={kbars.Open[i]}  H={kbars.High[i]}  L={kbars.Low[i]}  C={kbars.Close[i]}  V={kbars.Volume[i]}")
    print()
except Exception as e:
    print(f"  ERROR: {e}\n")

# ── 測試 1b: kbars() 轉成 DataFrame，檢查逐日（分K）筆數 ──
print("── 測試 1b: kbars() 轉成 min DataFrame（逐日筆數）──")
try:
    import pandas as pd

    df = pd.DataFrame(kbars.dict())
    df["ts"] = pd.to_datetime(df["ts"])
    df["date"] = df["ts"].dt.strftime("%Y-%m-%d")

    print(f"  DataFrame 筆數: {len(df)}  欄位: {list(df.columns)}")
    print(df.head(3).to_string(index=False))
    print()

    counts = df.groupby("date").size()
    print("  逐日筆數統計:")
    print(counts.to_string())
    print()

    if TEST_DATE in counts.index:
        print(f"  {TEST_DATE} 在 DataFrame 裡有 {counts[TEST_DATE]} 筆")
    else:
        print(f"  [警告] {TEST_DATE} 完全沒有出現在 DataFrame 的任何一列")
    print()
except Exception as e:
    print(f"  ERROR: {e}\n")

# ── 測試 1c: kbars() ──────────────
print("── 測試 1c: kbars() 不填 ──")
try:
    kbars_noend = api.kbars(contract=contract)
    print(f"  回傳筆數: {len(kbars_noend.ts)}")
    if kbars_noend.ts:
        df2 = pd.DataFrame(kbars_noend.dict())
        df2["ts"] = pd.to_datetime(df2["ts"])
        df2["date"] = df2["ts"].dt.strftime("%Y-%m-%d")
        counts2 = df2.groupby("date").size()
        print("  逐日筆數統計:")
        print(counts2.to_string())
        print()
        if TEST_DATE in counts2.index:
            print(f"  {TEST_DATE} 在這次結果裡有 {counts2[TEST_DATE]} 筆")
        else:
            print(f"  [警告] {TEST_DATE} 還是沒有出現")
    print()
except Exception as e:
    print(f"  ERROR: {e}\n")

# ── 測試 2: ticks() 抓昨天整日資料 ────────────────────
print(f"── 測試 2: ticks() 昨天整日 ({TEST_DATE}) ──")
try:
    ticks = api.ticks(contract=contract, date=TEST_DATE)
    total = len(ticks.ts) if ticks and ticks.ts else 0
    print(f"  回傳 tick 筆數: {total}")

    if total > 0:
        from datetime import datetime
        # 顯示頭尾各 3 筆
        print("  前 3 筆:")
        for i in range(min(3, total)):
            dt = datetime.fromtimestamp(ticks.ts[i] / 1e9)
            print(f"    {dt.strftime('%H:%M:%S')}  price={ticks.close[i]}  vol={ticks.volume[i]}")
        print("  後 3 筆:")
        for i in range(max(0, total-3), total):
            dt = datetime.fromtimestamp(ticks.ts[i] / 1e9)
            print(f"    {dt.strftime('%H:%M:%S')}  price={ticks.close[i]}  vol={ticks.volume[i]}")

        # ── 聚合成 M1 K棒 ──────────────────────────────
        print()
        print("── 測試 3: ticks 聚合成 M1 K棒 ──")
        TF_SEC = 60
        buckets = {}
        for i in range(total):
            price = float(ticks.close[i])
            vol   = int(ticks.volume[i])
            if price <= 0:
                continue
            aligned = (int(ticks.ts[i] / 1e9) // TF_SEC) * TF_SEC
            if aligned not in buckets:
                buckets[aligned] = [price, price, price, price, vol]
            else:
                b = buckets[aligned]
                b[1] = max(b[1], price)
                b[2] = min(b[2], price)
                b[3] = price
                b[4] += vol

        m1_bars = sorted(buckets.items())
        print(f"  聚合後 M1 K棒數: {len(m1_bars)}")
        if m1_bars:
            print("  前 3 根 M1:")
            for ts, b in m1_bars[:3]:
                dt = datetime.fromtimestamp(ts)
                print(f"    {dt.strftime('%H:%M')}  O={b[0]}  H={b[1]}  L={b[2]}  C={b[3]}  V={b[4]}")
            print("  後 3 根 M1:")
            for ts, b in m1_bars[-3:]:
                dt = datetime.fromtimestamp(ts)
                print(f"    {dt.strftime('%H:%M')}  O={b[0]}  H={b[1]}  L={b[2]}  C={b[3]}  V={b[4]}")
    print()
except Exception as e:
    print(f"  ERROR: {e}\n")

# ── 登出 ──────────────────────────────────────────────
api.logout()
print("=== 測試完成 ===")
