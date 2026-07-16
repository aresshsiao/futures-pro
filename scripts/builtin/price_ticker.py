__meta__ = {
    "name": "Price_Ticker",
    "description": "每 10 秒報價（語音播報目前價位）",
    "type": "indicator",
    "enabled": True,
    "params": {},
    # 除了 M1 棒收完時執行一次，系統也會依此秒數額外定時觸發 calc()
    # （見 main.py _script_timer_loop），讓報價不用等到棒收完才播。
    "interval_sec": 10,
}

from scripts.engine import ScriptContext

def calc(ctx: ScriptContext):
    """
    每 10 秒報價

    有 interval_sec 時 calc() 會被系統依此秒數定時呼叫（見 main.py
    _script_timer_loop），ctx.close 最後一筆會是當下尚未收完的 live 棒價位。
    """
    price = ctx.close.iloc[-1]
    ctx.alert(f"指數 {price:.0f}")
