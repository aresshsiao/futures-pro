"""
core/fill_ledger.py — 成交明細的新倉/平倉判定與已實現損益推算

券商的成交回報只有「買 / 賣」，看不出這一筆是進場還是出場；已實現損益要等券商
結算完才查得到（list_profit_loss），而且是「已平倉部位」的彙總，對不回單筆成交。
成交當下畫面就該看得到自己平掉幾口、賺賠多少，所以這裡自己按成交順序推算一次。
券商結算好的數字之後會覆蓋上來（見 main._merge_fills_with_pnl）。
"""
from __future__ import annotations
import math
from typing import Iterable, Optional

from core.models import (
    Direction, Fill, Position, PositionSide,
    commission_per_lot, point_value, transaction_tax,
)

# oc_type → 中文，記 log 與前端顯示共用同一套說法
OC_TEXT = {"new": "新倉", "cover": "平倉", "cover_new": "平倉反手"}


class _Lot:
    """某商品目前的部位：正數=多單，負數=空單。avg=None 代表成本不明。

    tax_avg 是進場那幾口「每口」繳的交易稅（加權平均、未進位的原始金額），平倉時
    按平掉的口數比例分攤回來，跟這筆平倉自己的稅合計後才一次無條件進位——法定
    稅額是「這次交易」（進場+出場）合計後才進位到整元，不是兩筆各自捨入再相加。
    跟 avg 一樣，成本不明（留倉單）時是 None，不能拿別筆的稅基冒充。

    手續費不进这里算：券商只在平倉收一次（新倉免收），不用像價格、稅額一樣
    加權平均分攤進場成本，見 apply() 平倉那段直接用 commission_per_lot() 算。
    """

    __slots__ = ("qty", "avg", "tax_avg")

    def __init__(self, qty: int = 0, avg: Optional[float] = None, tax_avg: Optional[float] = None):
        self.qty = qty
        self.avg = avg
        self.tax_avg = tax_avg


class FillLedger:
    """依成交順序重播部位變化，替每筆成交標上 oc_type / closed_qty / pnl。

    可以逐筆餵（成交回報進來時 apply 一筆），也可以整份重播（跟券商對帳後
    replay 整天的成交）。兩條路徑走同一套邏輯，結果才會一致。
    """

    def __init__(self):
        self._lots: dict[str, _Lot] = {}

    # ── 開盤前部位 ────────────────────────────────────

    def seed(self, opening: dict[str, int]) -> None:
        """設定「今日第一筆成交之前」的部位（口數為正=多、負=空）。

        留倉單昨天就建立了，成本不在今日成交明細裡，所以只記口數不記成本：
        平掉留倉單的那幾口會標成平倉但損益留 None，等券商結算的數字補上。
        沒有這一步的話，第一筆平倉會被當成新倉，之後整天的新倉/平倉全部反過來。
        """
        self._lots = {s: _Lot(q, None) for s, q in opening.items() if q}

    @staticmethod
    def opening_from(positions: Iterable[Position], fills: Iterable[Fill]) -> dict[str, int]:
        """由「券商目前部位」扣掉「今日成交淨額」反推開盤前部位。

        兩者要取自同一次對帳才對得起來（見 TradeModule._refresh_from_broker）。
        """
        net: dict[str, int] = {}
        for f in fills:
            net[f.symbol] = net.get(f.symbol, 0) + _signed(f)

        opening: dict[str, int] = {}
        for p in positions:
            sign = 1 if p.side == PositionSide.LONG else -1
            opening[p.symbol] = sign * p.qty - net.pop(p.symbol, 0)
        # 今日交易過、但現在已經平光的商品：目前部位 0，開盤前部位就是淨額的反向
        for symbol, delta in net.items():
            opening[symbol] = -delta
        return {s: q for s, q in opening.items() if q}

    # ── 推算 ──────────────────────────────────────────

    def apply(self, fill: Fill) -> Fill:
        """把一筆成交套進帳上，就地寫回 oc_type / closed_qty / pnl / fee / tax / net_pnl。"""
        lot = self._lots.setdefault(fill.symbol, _Lot())
        delta = _signed(fill)

        # 交易稅不管新倉還是平倉都要課，先算這筆自己的原始金額（未進位）；
        # 手續費不一樣——券商只在平倉收一次、新倉免收，所以先預設 0，等平倉分支
        # 再用 commission_per_lot() 算，不能像稅一樣兩種分支都先扣一次。
        leg_tax = transaction_tax(fill.symbol, fill.price, fill.qty)
        unit_tax = leg_tax / fill.qty if fill.qty else 0.0
        fill.fee = 0.0

        # 同向（或原本空手）＝ 加碼開倉，用加權平均更新成本（價格、每口稅基都要）
        if lot.qty == 0 or (lot.qty > 0) == (delta > 0):
            fill.oc_type = "new"
            fill.closed_qty = 0
            fill.pnl = None
            fill.net_pnl = None
            fill.tax = 0.0   # 新倉免收手續費，稅額也還沒到平倉合計進位的時候，不单独显示
            if lot.qty and lot.avg is not None:
                lot.avg = (lot.avg * abs(lot.qty) + fill.price * fill.qty) / (abs(lot.qty) + fill.qty)
                lot.tax_avg = (lot.tax_avg * abs(lot.qty) + unit_tax * fill.qty) / (abs(lot.qty) + fill.qty)
            elif lot.qty == 0:
                lot.avg = fill.price
                lot.tax_avg = unit_tax
            # lot.avg is None（留倉部位成本不明）時繼續留 None，不拿新倉價/稅基冒充
            lot.qty += delta
            return fill

        # 反向 ＝ 先平倉，平完還有剩才是反手開新倉
        closed = min(abs(lot.qty), fill.qty)
        fill.closed_qty = closed
        if lot.avg is None:
            fill.pnl = None   # 平的是留倉單，進場成本不在今日成交裡
            fill.net_pnl = None
            fill.tax = 0.0
        else:
            # 平多單賺 (出場 - 進場)，平空單反過來
            side = 1 if lot.qty > 0 else -1
            fill.pnl = round(side * (fill.price - lot.avg) * closed * point_value(fill.symbol), 2)
            # 手續費只在這裡收一次（平掉幾口收幾口的錢）；稅則是進場+出場合計後
            # 才無條件進位到整元——法定稅額是「這次交易」整筆算的，不是兩筆各自
            # 捨入再相加，兩種算法在邊界值上會差 1 元。
            fill.fee = round(commission_per_lot(fill.symbol) * closed, 2)
            entry_tax = (lot.tax_avg or 0.0) * closed
            exit_tax = unit_tax * closed
            fill.tax = math.ceil(entry_tax + exit_tax)
            fill.net_pnl = round(fill.pnl - fill.fee - fill.tax, 2)

        remaining = fill.qty - closed
        lot.qty += delta
        if remaining:
            fill.oc_type = "cover_new"
            lot.avg = fill.price          # 反手後的成本就是這筆成交價
            lot.tax_avg = unit_tax
        else:
            fill.oc_type = "cover"
            if lot.qty == 0:
                self._lots.pop(fill.symbol, None)
        return fill

    def replay(self, fills: Iterable[Fill], opening: Optional[dict[str, int]] = None) -> list[Fill]:
        """整份重播（時間排序後逐筆 apply），回傳排序後的成交清單。

        對帳拿回來的成交是券商的原始資料，沒有推算欄位；而且順序不保證，
        算損益前一定要先照時間排好，不然平倉會對到還沒建立的部位。
        """
        self.seed(opening or {})
        ordered = sorted(fills, key=lambda f: f.timestamp)
        for f in ordered:
            self.apply(f)
        return ordered


def _signed(fill: Fill) -> int:
    return fill.qty if fill.direction == Direction.BUY else -fill.qty
