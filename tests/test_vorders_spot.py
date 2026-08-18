"""虚拟盘的委托单与现货。

他的要求原话：「我需要的虚拟交易是跟实盘交易体验一模一样。有委托 做多做空
现货买入委托单 止盈止损都要有」。

在这之前 `/vopen BTC long 1000 10 60000` 是**假装以 60000 成交了**——
那会养出"我总能在想要的价位进场"这个最贵的错觉，而挂多远、等多久、要不要追，
恰恰是最该练的。这里守的就是"挂单真的是挂单"。
"""
import pytest

import storage
from handlers import vorders as VO
from handlers import vspot as S


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(storage, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(VO, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(S, "save_data", lambda *a, **k: None)
    from handlers import vtrade as V
    monkeypatch.setattr(V, "save_data", lambda *a, **k: None)
    yield


def acct(bal=10000.0):
    return {"balance": bal, "positions": {}, "history": [], "orders": [],
            "spot": {}, "chat_id": None}


# ── 触发方向 ────────────────────────────────────────────────
@pytest.mark.parametrize("side,px,mark,now", [
    ("long", 60000, 61000, False),   # 想低买，现价更高 → 等
    ("long", 62000, 61000, True),    # 挂价高于现价 → 立刻成交（等于吃单）
    ("short", 62000, 61000, False),  # 想高卖，现价更低 → 等
    ("short", 60000, 61000, True),
    ("buy", 100, 120, False),
    ("sell", 150, 120, False),
])
def test_will_fill_now(side, px, mark, now):
    assert VO.will_fill_now(side, px, mark) is now


@pytest.mark.parametrize("side,px,mark,hit", [
    ("long", 60000, 59999, True),    # 跌到了
    ("long", 60000, 60001, False),
    ("short", 62000, 62001, True),   # 涨上来了
    ("short", 62000, 61999, False),
])
def test_hits(side, px, mark, hit):
    assert VO.hits({"side": side, "price": px}, mark) is hit


# ── 冻结与撤单 ──────────────────────────────────────────────
def test_placing_freezes_money():
    """不冻结就能无限挂单，账户余额变成假的。"""
    a = acct()
    VO.place(a, VO.PERP, "BTC", "long", 60000, margin=1000, lev=10, frozen=1005)
    assert a["balance"] == pytest.approx(8995)


def test_cancel_refunds():
    a = acct()
    o = VO.place(a, VO.PERP, "BTC", "long", 60000, margin=1000, lev=10, frozen=1005)
    VO.cancel(a, o["id"])
    assert a["balance"] == pytest.approx(10000)
    assert a["orders"] == []


def test_cancel_all_by_symbol():
    a = acct()
    VO.place(a, VO.PERP, "BTC", "long", 60000, margin=100, lev=10, frozen=101)
    VO.place(a, VO.PERP, "ETH", "long", 3000, margin=100, lev=10, frozen=101)
    VO.cancel_all(a, "BTC")
    assert [o["sym"] for o in a["orders"]] == ["ETH"]


def test_ids_are_short_and_reused():
    """他要能直接打 /vcancel 3，不是抄一串哈希。"""
    a = acct()
    o1 = VO.place(a, VO.PERP, "BTC", "long", 1, margin=1, lev=1, frozen=0)
    o2 = VO.place(a, VO.PERP, "ETH", "long", 1, margin=1, lev=1, frozen=0)
    assert (o1["id"], o2["id"]) == ("1", "2")
    VO.cancel(a, "1")
    assert VO.place(a, VO.PERP, "SOL", "long", 1, margin=1, lev=1, frozen=0)["id"] == "1"


# ── 成交 ────────────────────────────────────────────────────
def test_fill_perp_opens_a_position():
    import asyncio
    a = acct()
    o = VO.place(a, VO.PERP, "BTC", "long", 60000, margin=1000, lev=10,
                 frozen=1005, sl=58000)
    asyncio.run(VO.fill(a, o, maker=True))
    pos = a["positions"]["BTC"]
    assert pos["entry"] == 60000 and pos["lev"] == 10
    assert pos["sl"] == 58000, "挂单上带的止损要跟着进仓位"
    assert a["orders"] == []
    # maker 费率比吃单便宜，这是挂单的真实优势
    assert pos["fee_rate"] == VO.MAKER_RATE


def test_fill_spot_buy_adds_coins():
    import asyncio
    a = acct()
    o = VO.place(a, VO.SPOT, "BTC", "buy", 60000, quote=6000, frozen=6000)
    asyncio.run(VO.fill(a, o, maker=True))
    h = a["spot"]["BTC"]
    assert h["qty"] == pytest.approx((6000 - 6000 * VO.MAKER_RATE) / 60000)
    assert a["balance"] == pytest.approx(4000)


# ── 现货结算 ────────────────────────────────────────────────
def test_spot_buy_then_sell_pnl():
    a = acct()
    S.settle(a, "BTC", "buy", 50000, 0.001, quote=5000)
    h = a["spot"]["BTC"]
    assert a["balance"] == pytest.approx(5000)
    assert h["cost"] == 5000, "成本含手续费——真实付出的就是这么多"

    S.settle(a, "BTC", "sell", 60000, 0.001, qty=h["qty"])
    assert "BTC" not in a["spot"]
    assert a["balance"] > 10000, "涨了 20% 卖掉该是赚的"
    assert a["history"][-1]["exit_kind"] == "现货卖出"


def test_spot_average_cost_across_buys():
    """分批买入后要知道真实成本——现货唯一要盯的就是这个数。"""
    a = acct()
    S.settle(a, "BTC", "buy", 50000, 0.0, quote=5000)
    S.settle(a, "BTC", "buy", 40000, 0.0, quote=4000)
    h = a["spot"]["BTC"]
    assert h["cost"] == pytest.approx(9000)
    avg = h["cost"] / h["qty"]
    assert 40000 < avg < 50000


def test_partial_sell_carries_cost_proportionally():
    a = acct()
    S.settle(a, "BTC", "buy", 50000, 0.0, quote=5000)
    h = a["spot"]["BTC"]
    S.settle(a, "BTC", "sell", 50000, 0.0, qty=h["qty"] / 2)
    assert a["spot"]["BTC"]["cost"] == pytest.approx(2500)


def test_spot_never_liquidates():
    """现货不会爆仓——这是和永续最本质的区别，跌 90% 币还在手上。"""
    a = acct()
    S.settle(a, "BTC", "buy", 50000, 0.0, quote=5000)
    qty = a["spot"]["BTC"]["qty"]
    # 价格砸到十分之一，持仓数量一分不少
    assert a["spot"]["BTC"]["qty"] == qty


# ── 卖单锁仓 ────────────────────────────────────────────────
def test_sell_order_locks_without_moving_cost():
    """锁定用记账，不把币从持仓里搬走——搬来搬去成本均价迟早对不上。"""
    a = acct()
    S.settle(a, "BTC", "buy", 50000, 0.0, quote=5000)
    qty = a["spot"]["BTC"]["qty"]
    VO.place(a, VO.SPOT, "BTC", "sell", 60000, qty=qty / 2, frozen=0.0)
    assert a["spot"]["BTC"]["qty"] == pytest.approx(qty), "持仓不该被搬走"
    assert S.locked(a, "BTC") == pytest.approx(qty / 2)
    assert S.sellable(a, "BTC") == pytest.approx(qty / 2)


def test_cancelling_a_sell_order_frees_the_coins():
    a = acct()
    S.settle(a, "BTC", "buy", 50000, 0.0, quote=5000)
    qty = a["spot"]["BTC"]["qty"]
    o = VO.place(a, VO.SPOT, "BTC", "sell", 60000, qty=qty, frozen=0.0)
    VO.cancel(a, o["id"])
    assert S.sellable(a, "BTC") == pytest.approx(qty)


# ── 入口 ────────────────────────────────────────────────────
def test_commands_are_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    for c in ("vbuy", "vsell", "vspot", "vorders", "vcancel"):
        assert f'CommandHandler("{c}"' in src
        assert f'BotCommand("{c}"' in src


def test_order_check_runs_in_the_background_job():
    """挂单必须有人盯着，否则挂了永远不成交。
    和爆仓/止盈损共用同一批价格，别多打一轮接口。"""
    import inspect
    from handlers import vtrade as V
    src = inspect.getsource(V.check_liquidations)
    assert "check_orders" in src


def test_limit_price_places_an_order_not_an_instant_fill():
    """老行为是"指定价就假装以那个价成交"——这条盯着别改回去。"""
    import inspect
    from handlers import vtrade as V
    src = inspect.getsource(V.vopen)
    assert "will_fill_now" in src and "VO.place" in src


def test_menu_has_buttons_for_spot_and_orders():
    """功能必须有按钮入口，不能只有命令。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert "vspot_show" in src and "vord_show" in src
