"""虚拟交易台的按钮流程。

他的原话：「这些可以做成功能按钮吗 不然这样很麻烦 特别是新用户哪里知道
使用这么多命令呢？」——功能堆到七八个命令之后，等于没有入口。

这里守两件事：
  1. 每一步都点得到（选币/方向/杠杆/保证金/市价限价/平仓/撤单全是按钮）；
  2. 部分平仓不能把整个仓吞掉——`_auto_close` 本来只服务全平，它会 pop 掉持仓。
"""
import asyncio
import types

import pytest

import storage
from handlers import vpanel as P
from handlers import vorders as VO


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for m in (storage, VO):
        monkeypatch.setattr(m, "save_data", lambda *a, **k: None)
    from handlers import vtrade as V
    monkeypatch.setattr(V, "save_data", lambda *a, **k: None)
    storage.data["vtrade"] = {}
    yield
    storage.data.pop("vtrade", None)


class Q:
    """够用的假 query：记录最后一次渲染的文本和按钮。"""
    def __init__(self, uid=7):
        self.from_user = types.SimpleNamespace(id=uid)
        self.message = types.SimpleNamespace(chat_id=uid)
        self.text = None
        self.kb = None
        self.answers = []

    async def answer(self, *a, **k):
        self.answers.append(a[0] if a else "")


def _patch_edit(monkeypatch, q):
    async def fake_edit(query, text, reply_markup=None, **kw):
        q.text, q.kb = text, reply_markup
    monkeypatch.setattr(P, "safe_edit", fake_edit)


def _patch_price(monkeypatch, px=60000.0):
    async def one(sym):
        return {"price": px, "change": 0.0}

    async def many(syms):
        return {s: {"price": px, "change": 0.0} for s in syms}
    monkeypatch.setattr(P, "get_price", one)
    monkeypatch.setattr(P, "get_prices", many)


def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


# ── 每一步都点得到 ──────────────────────────────────────────
def test_home_offers_open_and_buy(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch)
    asyncio.run(P.home(q))
    cbs = _cbs(q.kb)
    assert "vg:open" in cbs and "vg:buy" in cbs and "vg:ord" in cbs
    assert "不用记命令" in q.text


def test_the_whole_open_flow_is_clickable(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch)
    asyncio.run(P.pick_symbol(q, "perp"))
    assert "vg:perpsym:BTC" in _cbs(q.kb)

    asyncio.run(P.pick_side(q, "BTC"))
    assert "vg:side:BTC:long" in _cbs(q.kb)

    asyncio.run(P.pick_lev(q, "BTC", "long"))
    assert any(c.startswith("vg:lev:BTC:long:") for c in _cbs(q.kb))

    from handlers.vtrade import _acct
    _acct("7")["balance"] = 10000
    asyncio.run(P.pick_margin(q, "BTC", "long", 10))
    assert any(c.startswith("vg:mgn:BTC:long:10:") for c in _cbs(q.kb))

    asyncio.run(P.pick_type(q, "BTC", "long", 10, 1000))
    cbs = _cbs(q.kb)
    assert "vg:mkt:BTC:long:10:1000" in cbs, "要有市价"
    assert "vg:lim:BTC:long:10:1000" in cbs, "要有限价挂单"


def test_spot_flow_is_clickable(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch)
    asyncio.run(P.pick_symbol(q, "spot"))
    assert "vg:spotsym:BTC" in _cbs(q.kb)
    from handlers.vtrade import _acct
    _acct("7")["balance"] = 10000
    asyncio.run(P.pick_spot_amount(q, "BTC"))
    assert any(c.startswith("vg:sbuy:BTC:") for c in _cbs(q.kb))


def test_positions_get_action_buttons(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch)
    from handlers.vtrade import _acct
    a = _acct("7")
    a["positions"]["BTC"] = {"side": "long", "margin": 1000, "lev": 10,
                             "entry": 60000, "qty": 0.1666, "open_ts": 0}
    asyncio.run(P.home(q))
    cbs = _cbs(q.kb)
    assert "vg:cl:BTC:50" in cbs and "vg:cl:BTC:100" in cbs and "vg:sl:BTC" in cbs


def test_spot_holdings_get_sell_buttons(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch)
    from handlers.vtrade import _acct
    a = _acct("7")
    a["spot"] = {"BTC": {"qty": 0.1, "cost": 5000}}
    asyncio.run(P.home(q))
    cbs = _cbs(q.kb)
    assert "vg:sl50:BTC" in cbs and "vg:sall:BTC" in cbs


def test_每张挂单都有撤单按钮(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch)
    from handlers.vtrade import _acct
    a = _acct("7")
    VO.place(a, VO.PERP, "BTC", "long", 55000, margin=100, lev=10, frozen=101)
    VO.place(a, VO.SPOT, "ETH", "buy", 3000, quote=500, frozen=500)
    asyncio.run(P.orders_panel(q))
    cbs = _cbs(q.kb)
    assert "vg:cx:1" in cbs and "vg:cx:2" in cbs and "vg:cx:all" in cbs


# ── 部分平仓不能吞掉整个仓 ───────────────────────────────────
def test_partial_close_keeps_the_rest(monkeypatch):
    """_auto_close 会 pop 掉持仓（它本来只服务全平）。
    平 50% 之后剩下的必须还在，而且保证金/数量减半。"""
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch, px=66000)
    from handlers.vtrade import _acct
    a = _acct("7")
    a["positions"]["BTC"] = {"side": "long", "margin": 1000, "lev": 10,
                             "entry": 60000, "qty": 0.16666, "open_ts": 0,
                             "fee_rate": 0.0005, "funding_paid": 20.0}
    asyncio.run(P.do_close(q, "BTC", 50))
    rest = a["positions"].get("BTC")
    assert rest is not None, "平一半不该把整个仓平掉"
    assert rest["margin"] == pytest.approx(500)
    assert rest["qty"] == pytest.approx(0.08333)
    assert rest["funding_paid"] == pytest.approx(10.0), "已付资金费也要分摊，别重复扣"
    assert len(a["history"]) == 1


def test_full_close_removes_the_position(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q); _patch_price(monkeypatch, px=66000)
    from handlers.vtrade import _acct
    a = _acct("7")
    a["positions"]["BTC"] = {"side": "long", "margin": 1000, "lev": 10,
                             "entry": 60000, "qty": 0.16666, "open_ts": 0,
                             "fee_rate": 0.0005}
    asyncio.run(P.do_close(q, "BTC", 100))
    assert "BTC" not in a["positions"]


# ── 打字只剩两处 ────────────────────────────────────────────
def test_limit_price_asks_for_a_number(monkeypatch):
    q = Q(); _patch_edit(monkeypatch, q)
    ctx = types.SimpleNamespace(user_data={})
    asyncio.run(P.ask_price(q, ctx, "perp",
                            {"sym": "BTC", "side": "long", "lev": 10, "margin": 1000}))
    assert "await_vprice" in ctx.user_data
    assert "价格" in q.text


def test_limit_price_rejects_a_price_that_would_fill_now(monkeypatch):
    """做多挂在现价之上等于市价单——直接说清楚，别默默当挂单收下。"""
    _patch_price(monkeypatch, px=60000)
    ctx = types.SimpleNamespace(user_data={})
    said = []

    class Msg:
        chat_id = 7
        from_user = types.SimpleNamespace(id=7)

        async def reply_text(self, text, **kw):
            said.append(text)

    asyncio.run(P.on_price(Msg(), ctx, {"kind": "perp", "sym": "BTC",
                                        "side": "long", "lev": 10, "margin": 100},
                           "61000"))
    assert said and "现在就能成交" in said[0]


def test_routing_is_wired():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert 'd.startswith("vg:")' in src
    for act in ("perpsym", "mgn", "mkt", "lim", "cl", "cx", "sbuy"):
        assert f'"{act}"' in src


def test_guided_inputs_are_intercepted():
    import inspect
    from handlers import quickprice
    src = inspect.getsource(quickprice)
    for key in ("await_vprice", "await_vcoin", "await_vsl"):
        assert key in src
