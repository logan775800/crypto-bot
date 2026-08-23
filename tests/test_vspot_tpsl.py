"""现货止盈止损。

永续早就有（`/vtpsl`，后台 60 秒轮询触及自动平），现货一直是空的——
当时的取舍是"现货不会爆仓，紧迫性低一档"。但不会被强制带走 ≠ 不该有计划，
拿着不动跌 60% 还安慰自己"又不会爆仓"，正是练手阶段最该被纠正的习惯。

这里守三件事：
  1. **方向判据用现价，不用成本均价**——涨了 50% 之后把止损抬到成本之上
     正是该做的动作，用成本卡会把最该设的那个止损拒掉；
  2. **触发时撤掉挂着的限价卖单**，否则被锁的币出不来，止损成了半个止损；
  3. **只玩现货的人也要被检查到**——60 秒任务原来只按合约持仓收集币种，
     现货止盈损会静默地一次都不触发。
"""
import asyncio

import pytest

import storage
from handlers import vorders as VO
from handlers import vspot as S
from handlers import vtrade as V


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(storage, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(VO, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(S, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(V, "save_data", lambda *a, **k: None)
    # 这批测试直接读写全局 storage.data['vtrade']，用完必须清干净：
    # 服务器上 pytest 是随机顺序跑的，留下的账户会污染别的测试
    storage.data["vtrade"] = {}
    yield
    storage.data["vtrade"] = {}


def acct(bal=10000.0):
    return {"balance": bal, "positions": {}, "history": [], "orders": [],
            "spot": {}, "chat_id": 42}


def bought(a, sym="BTC", price=50000.0, quote=5000.0):
    S.settle(a, sym, "buy", price, 0.0, quote=quote)
    return a["spot"][sym]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class FakeCtx:
    def __init__(self):
        self.bot = FakeBot()


def run(coro):
    return asyncio.run(coro)


# ── 方向校验 ────────────────────────────────────────────────
def test_stop_above_spot_price_is_rejected():
    """挂上去 60 秒内就会卖掉——那不是止损，是一个绕远路的市价单。"""
    h = bought(acct())
    changed, err = S.apply_tpsl(h, 50000, [("sl", 51000)])
    assert err and "止损" in err
    assert "sl" not in h, "被拒绝的值不能留在持仓上"


def test_take_profit_below_spot_price_is_rejected():
    h = bought(acct())
    changed, err = S.apply_tpsl(h, 50000, [("tp", 49000)])
    assert err and "止盈" in err
    assert "tp" not in h


def test_stop_above_cost_is_allowed():
    """这条是这次最容易被"修"坏的地方。

    买在 50000、现价涨到 75000，把止损设在 60000（**高于成本**）
    正是该做的动作——护住已有的利润。判据要是成本均价，这个止损会被拒掉。
    """
    h = bought(acct(), price=50000)
    changed, err = S.apply_tpsl(h, 75000, [("sl", 60000)])
    assert err is None
    assert h["sl"] == 60000
    assert h["cost"] / h["qty"] == pytest.approx(50000), "成本仍在止损之下"


def test_zero_clears():
    h = bought(acct())
    S.apply_tpsl(h, 50000, [("sl", 45000), ("tp", 60000)])
    changed, err = S.apply_tpsl(h, 50000, [("sl", 0)])
    assert err is None and "sl" not in h
    assert h["tp"] == 60000, "只清了止损，止盈不该跟着没"


def test_a_rejected_pair_does_not_half_apply():
    """一边改一边校验的话，止损设上了、止盈被拒、回执只报错——
    他以为什么都没生效，其实账户已经变了。"""
    h = bought(acct())
    changed, err = S.apply_tpsl(h, 50000, [("sl", 45000), ("tp", 40000)])
    assert err is not None
    assert "sl" not in h and "tp" not in h
    assert changed == []


def test_parse_tpsl_ignores_junk():
    assert S.parse_tpsl(["tp=70000", "垃圾", "sl=abc", "lev=10"]) == [("tp", 70000.0)]


# ── 触发 ────────────────────────────────────────────────────
def _armed(sl=None, tp=None, price=50000.0):
    a = acct()
    h = bought(a, price=price)
    if sl:
        h["sl"] = sl
    if tp:
        h["tp"] = tp
    storage.data["vtrade"] = {"777": a}
    return a


def test_stop_loss_fires_and_sells_everything():
    a = _armed(sl=45000)
    ctx = FakeCtx()
    run(S.check_tpsl(ctx, {"BTC": {"price": 44000}}))
    assert "BTC" not in a["spot"], "止损要全卖，不是卖一半"
    assert a["history"][-1]["exit_kind"] == "现货止损"
    assert ctx.bot.sent and "止损" in ctx.bot.sent[0][1]


def test_take_profit_fires():
    a = _armed(tp=60000)
    run(S.check_tpsl(FakeCtx(), {"BTC": {"price": 61000}}))
    assert a["history"][-1]["exit_kind"] == "现货止盈"


def test_fills_at_the_order_price_not_the_market_price():
    """用当前价成交会让模拟盘系统性地优于实盘——和 vorders 一个口径。"""
    a = _armed(sl=45000)
    run(S.check_tpsl(FakeCtx(), {"BTC": {"price": 40000}}))
    assert a["history"][-1]["exit"] == 45000


def test_not_triggered_when_price_has_not_reached():
    a = _armed(sl=45000, tp=60000)
    run(S.check_tpsl(FakeCtx(), {"BTC": {"price": 50000}}))
    assert "BTC" in a["spot"] and not a["history"]


def test_holding_without_tpsl_is_untouched():
    a = acct()
    bought(a)
    storage.data["vtrade"] = {"777": a}
    run(S.check_tpsl(FakeCtx(), {"BTC": {"price": 1}}))
    assert "BTC" in a["spot"], "没设止盈损的持币不该被任何价格动到"


def test_missing_price_is_skipped_not_sold():
    """取价失败必须什么都不做。拿不到价就当触发是最坏的一种静默失败。"""
    a = _armed(sl=45000)
    run(S.check_tpsl(FakeCtx(), {}))
    assert "BTC" in a["spot"]


# ── 挂单冲突 ────────────────────────────────────────────────
def test_trigger_cancels_blocking_sell_orders():
    """被锁在限价卖单里的币卖不掉，不撤的话止损只能卖出可卖的那部分。"""
    a = _armed(sl=45000)
    qty = a["spot"]["BTC"]["qty"]
    VO.place(a, VO.SPOT, "BTC", "sell", 70000, qty=qty * 0.8, frozen=0.0)
    assert S.sellable(a, "BTC") == pytest.approx(qty * 0.2)

    ctx = FakeCtx()
    run(S.check_tpsl(ctx, {"BTC": {"price": 44000}}))
    assert "BTC" not in a["spot"], "撤掉卖单之后应该全部卖光"
    assert a["orders"] == []
    assert "限价卖单" in ctx.bot.sent[0][1], "撤了别人的单必须说出来"


def test_trigger_keeps_the_limit_buy_order():
    """限价买单是另一笔决定（想在更低的价接回来），不替他做主。"""
    a = _armed(sl=45000)
    VO.place(a, VO.SPOT, "BTC", "buy", 30000, quote=1000, frozen=1000)
    run(S.check_tpsl(FakeCtx(), {"BTC": {"price": 44000}}))
    assert [o["side"] for o in a["orders"]] == ["buy"]


def test_other_symbols_sell_orders_survive():
    a = _armed(sl=45000)
    VO.place(a, VO.SPOT, "ETH", "sell", 4000, qty=1, frozen=0.0)
    run(S.check_tpsl(FakeCtx(), {"BTC": {"price": 44000}}))
    assert [o["sym"] for o in a["orders"]] == ["ETH"]


# ── 后台任务：这是最容易静默失效的一环 ──────────────────────
def test_spot_only_account_is_still_checked(monkeypatch):
    """只玩现货、一个合约仓都没有的人，60 秒任务也必须跑到他头上。

    原来的收集只看 `positions`，空的就 return——现货止盈损会一次都不触发，
    而且不报错、日志干净，是这个项目最贵的那类 bug。
    """
    a = _armed(sl=45000)
    assert not a["positions"], "这条测试的前提就是没有合约仓"

    async def _noop(_ctx):
        return None

    async def _prices(syms):
        return {s: {"price": 44000} for s in syms}

    monkeypatch.setattr(VO, "check_orders", _noop)
    monkeypatch.setattr(V, "get_prices", _prices)
    run(V.check_liquidations(FakeCtx()))
    assert "BTC" not in a["spot"], "现货止损没被后台任务触发"


def test_perp_and_spot_share_one_price_fetch():
    """挂单/爆仓/永续止盈损/现货止盈损四件事共用一批价格，别多打一轮接口。"""
    import inspect
    src = inspect.getsource(V.check_liquidations)
    assert "check_orders" in src
    assert "check_tpsl" in src
    # 取价只有一次：多一次 get_prices 就是多一轮接口
    assert src.count("await get_prices(") == 1


# ── 入口 ────────────────────────────────────────────────────
def test_spot_row_has_a_tpsl_button():
    """功能必须有按钮入口；而且现货那行要和合约那行同一个形状。"""
    import inspect
    from handlers import vpanel
    src = inspect.getsource(vpanel.home)
    assert "vg:ssl:" in src


def test_button_is_wired_in_the_dispatcher():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert '"ssl"' in src and "ask_spot_sl" in src


def test_typed_reply_is_routed():
    """点了按钮之后发来的 sl=xxx 要有人接，否则按钮点了没下文。"""
    import inspect
    from handlers import quickprice
    src = inspect.getsource(quickprice.quick_price)
    assert "await_vssl" in src and "on_spot_sl" in src


def test_vtpsl_covers_spot():
    import inspect
    src = inspect.getsource(V.vtpsl)
    assert "现货" in src and "apply_tpsl" in src
