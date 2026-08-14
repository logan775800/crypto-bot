"""实盘确认按钮的端到端行为。这里每一条错了都是真金白银。

自检报告说「尚未验证按钮回调、重复点击防护、下单失败后的状态同步」。
查下来防护是有的（confirm_open 第一件事就是 pop 掉 pending，网络请求在之后），
但从来没被测试锁住过——没锁住的安全属性，下一次重构就会不声不响地消失。
"""
import asyncio
import types

import pytest

from handlers import rtrade


class FakeQuery:
    def __init__(self):
        self.edits = []
        self.message = types.SimpleNamespace()

    async def edit_message_text(self, text, **kw):
        self.edits.append(text)


class FakeClient:
    def __init__(self, fail=None):
        self.orders = []
        self.levs = []
        self.fail = fail

    async def set_leverage(self, sym, lev):
        self.levs.append((sym, lev))

    async def place_limit(self, sym, side, qty, price, tp=None, sl=None):
        if self.fail:
            raise self.fail
        self.orders.append({"symbol": sym, "side": side, "qty": qty,
                            "price": price, "tp": tp, "sl": sl})
        return {"orderId": "OID%d" % len(self.orders)}


def _pending(**kw):
    p = {"symbol": "BTCUSDT", "side": "long", "order_side": "Buy", "qty": 0.01,
         "price": 60000.0, "lev": 10, "tp": None, "sl": 59000.0}
    p.update(kw)
    return p


def _ctx(pending=True):
    ud = {"ro_pending": _pending()} if pending else {}
    return types.SimpleNamespace(user_data=ud)


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(rtrade, "_client", lambda: c)
    return c


# ---------------------------------------------------------------- 重复点击
def test_double_click_places_only_one_order(client):
    """连点两次「确认下单」只能下一单。这条是这个文件存在的理由。"""
    ctx, q = _ctx(), FakeQuery()
    asyncio.run(rtrade.confirm_open(q, ctx))
    asyncio.run(rtrade.confirm_open(q, ctx))
    assert len(client.orders) == 1
    assert "没有待确认的订单" in q.edits[-1]


def test_pending_is_consumed_before_any_network_call(monkeypatch):
    """pending 必须在下单**之前**就被摘掉。

    如果先下单后清理，下单那几百毫秒里的第二次点击就会再下一单——
    这正是「点了没反应就再点一下」的用户习惯会踩到的。
    """
    seen = {}

    class Spy(FakeClient):
        async def place_limit(self, *a, **kw):
            seen["pending_still_there"] = "ro_pending" in ctx.user_data
            return await super().place_limit(*a, **kw)

    spy = Spy()
    monkeypatch.setattr(rtrade, "_client", lambda: spy)
    ctx = _ctx()
    asyncio.run(rtrade.confirm_open(FakeQuery(), ctx))
    assert seen["pending_still_there"] is False


def test_no_pending_never_touches_the_exchange(monkeypatch):
    """没有待确认订单时，连交易所客户端都不该构造。"""
    called = []
    monkeypatch.setattr(rtrade, "_client", lambda: called.append(1))
    q = FakeQuery()
    asyncio.run(rtrade.confirm_open(q, _ctx(pending=False)))
    assert called == []
    assert "没有待确认的订单" in q.edits[-1]


# ---------------------------------------------------------------- 参数传递
def test_order_params_match_the_confirmed_card(client):
    """确认卡上写的和真正发出去的必须是同一组数——这里错了用户是被骗着点的确认。"""
    ctx = _ctx()
    p = dict(ctx.user_data["ro_pending"])
    asyncio.run(rtrade.confirm_open(FakeQuery(), ctx))
    o = client.orders[0]
    assert (o["symbol"], o["side"], o["qty"], o["price"], o["sl"]) == \
           (p["symbol"], p["order_side"], p["qty"], p["price"], p["sl"])


def test_leverage_is_set_before_ordering(client):
    asyncio.run(rtrade.confirm_open(FakeQuery(), _ctx()))
    assert client.levs == [("BTCUSDT", 10)]


def test_success_message_carries_order_id(client):
    q = FakeQuery()
    asyncio.run(rtrade.confirm_open(q, _ctx()))
    assert "已挂单" in q.edits[-1] and "OID1" in q.edits[-1]


# ---------------------------------------------------------------- 失败同步
def test_rejected_order_reports_exchange_reason(monkeypatch):
    """被交易所拒绝要把原因原样带出来，不能只说「失败了」。"""
    err = rtrade.BybitError(110007, "余额不足")
    monkeypatch.setattr(rtrade, "_client", lambda: FakeClient(fail=err))
    q, ctx = FakeQuery(), _ctx()
    asyncio.run(rtrade.confirm_open(q, ctx))
    assert "下单被拒" in q.edits[-1] and "余额不足" in q.edits[-1]
    assert "ro_pending" not in ctx.user_data, "失败后也不能留着旧 pending"


def test_unexpected_error_does_not_leak_pending(monkeypatch):
    monkeypatch.setattr(rtrade, "_client", lambda: FakeClient(fail=RuntimeError("超时")))
    q, ctx = FakeQuery(), _ctx()
    asyncio.run(rtrade.confirm_open(q, ctx))
    assert "下单失败" in q.edits[-1]
    assert "ro_pending" not in ctx.user_data


def test_cancel_clears_pending_and_places_nothing(client):
    ctx = _ctx()
    q = FakeQuery()
    asyncio.run(rtrade.cancel_open(q, ctx))
    assert client.orders == []
    assert "ro_pending" not in ctx.user_data
    assert "未下单" in q.edits[-1]
