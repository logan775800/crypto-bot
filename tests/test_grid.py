"""永续网格 `/gridstart` —— 这条路会**真下单**，而它一直是零测试。

为什么补这个：pytest 是发版门槛（部署前跑，挂了容器都不换）。
`grid.py` 直连 `BybitClient` 下限价单，却没有任何测试挡着——
等于这条真钱路径上没有门槛，任何一次重构都可以不声不响地把它改坏。

这里锁的是**参数校验、初始方向判定、成交翻转记账、越界告警、闸门继承**这几件，
全部离线：交易所客户端整个打桩，一个网络请求都不发。
"""
import asyncio
import inspect
import os
import types

import pytest

import storage
from handlers import grid


# ── 装置 ──────────────────────────────────────────────────────────
# 注意：碰全局 storage.data 的测试必须**重置**用到的键（= {}，不是 setdefault）。
# 服务器上 pytest 是随机顺序跑的，靠定义顺序的假设在本地全绿、线上时挂时不挂。

@pytest.fixture(autouse=True)
def _clean():
    storage.data["grids"] = {}
    yield
    storage.data["grids"] = {}


class FakeClient:
    """只记账，不联网。字段名照抄 BybitClient 的真实返回结构。"""

    def __init__(self, px=120.0, tick="0.1", qty_step="0.001", min_qty="0.001"):
        self.px = px
        self.info = {"tickSize": tick, "qtyStep": qty_step, "minOrderQty": min_qty}
        self.orders = []        # 每次 place_limit 的入参
        self.levs = []
        self.cancelled = []
        self.open_links = []    # open_orders 要回什么
        self.statuses = {}      # link_id -> orderStatus

    async def instrument_info(self, symbol):
        return self.info

    async def last_price(self, symbol):
        return self.px

    async def set_leverage(self, symbol, leverage):
        self.levs.append((symbol, leverage))

    async def place_limit(self, symbol, side, qty, price, link_id=None, **kw):
        self.orders.append({"symbol": symbol, "side": side, "qty": qty,
                            "price": price, "link_id": link_id})
        return {"orderId": "OID%d" % len(self.orders)}

    async def cancel_all(self, symbol):
        self.cancelled.append(symbol)

    async def open_orders(self, symbol):
        return [{"orderLinkId": l} for l in self.open_links]

    async def order_status(self, symbol, link_id):
        return {"orderStatus": self.statuses.get(link_id, "New")}


class FakeMsg:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


def _update(chat_id=111):
    """conftest 把 ADMIN_CHAT_ID 设成 111,222，所以 111 是管理员、999 不是。"""
    msg = FakeMsg()
    return types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=chat_id), message=msg), msg


def _ctx(*args):
    return types.SimpleNamespace(args=list(args))


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _job_ctx():
    return types.SimpleNamespace(bot=FakeBot())


@pytest.fixture
def client(monkeypatch):
    c = FakeClient()
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: c)
    monkeypatch.setattr(grid, "_is_testnet", lambda: True)
    return c


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ── orderLinkId 的 36 字符硬上限 ──────────────────────────────────
# Bybit 对 orderLinkId 有长度限制，超了整单被拒。docstring 里写了「<=36字符」，
# 但从来没有人验证过——写在注释里的约束等于没有约束。

@pytest.mark.parametrize("gap", [0, 9, 99, 999])
@pytest.mark.parametrize("side", ["Buy", "Sell"])
def test_link_id_stays_within_bybit_36_char_limit(gap, side):
    lid = grid._link_id("99999", gap, side)
    assert len(lid) <= 36, f"orderLinkId 超长会被交易所整单拒掉：{lid}"


def test_link_id_separates_gaps_and_sides():
    """同一网格里每格每方向都得是不同的 id，撞了会挂错格。"""
    a = grid._link_id("123", 0, "Buy")
    b = grid._link_id("123", 1, "Buy")
    c = grid._link_id("123", 0, "Sell")
    assert a != b and a != c


# ── 参数校验：不合理的参数一张单都不能下 ─────────────────────────────
# 这里的重点不是"回了什么话"，而是**交易所有没有被碰到**。
# 校验写在下单之前，错了就是拿真钱试错。

@pytest.mark.parametrize("args,why", [
    (("BTCUSDT", "200", "100", "10", "1"), "下限 >= 上限"),
    (("BTCUSDT", "100", "200", "1", "1"), "格数 < 2"),
    (("BTCUSDT", "100", "200", "10", "0"), "每格量 = 0"),
    (("BTCUSDT", "100", "200", "10", "-1"), "每格量为负"),
    (("BTCUSDT", "100", "200", "10", "1", "0"), "杠杆 < 1"),
])
def test_unreasonable_params_never_reach_the_exchange(monkeypatch, args, why):
    touched = []
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: touched.append(1))
    up, msg = _update()
    _run(grid.grid_start(up, _ctx(*args)))
    assert not touched, f"{why} —— 却已经去连交易所了"
    assert any("参数不合理" in r for r in msg.replies)


def test_non_numeric_params_are_caught(monkeypatch):
    touched = []
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: touched.append(1))
    up, msg = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "abc", "200", "10", "1")))
    assert not touched
    assert any("格式错误" in r for r in msg.replies)


def test_missing_params_show_usage(monkeypatch):
    touched = []
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: touched.append(1))
    up, msg = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100")))
    assert not touched
    assert any("用法" in r for r in msg.replies)


# ── 管理员门禁 ────────────────────────────────────────────────────

def test_non_admin_cannot_start_a_grid(monkeypatch):
    touched = []
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: touched.append(1))
    up, msg = _update(chat_id=999)
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "10", "1")))
    assert not touched
    assert any("仅管理员" in r for r in msg.replies)


def test_non_admin_cannot_stop_a_grid(client):
    up, msg = _update(chat_id=999)
    _run(grid.grid_stop(up, _ctx("BTCUSDT")))
    assert not client.cancelled
    assert any("仅管理员" in r for r in msg.replies)


# ── 重复启动 ──────────────────────────────────────────────────────

def test_duplicate_grid_is_refused(client):
    storage.data["grids"]["111:BTCUSDT"] = {"status": "running", "chat_id": 111}
    up, msg = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "10", "1")))
    assert not client.orders, "同一个币开第二张网格 = 两套单互相打架"
    assert any("已有运行中" in r for r in msg.replies)


def test_a_stopped_grid_does_not_block_a_new_one(client):
    storage.data["grids"]["111:BTCUSDT"] = {"status": "stopped", "chat_id": 111}
    up, _ = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "2", "1")))
    assert client.orders


# ── 初始方向判定 ──────────────────────────────────────────────────
# 每格初始挂哪一边由「这一格相对现价在哪」决定。挂反了等于开局就站错方向。
# 区间 100~200 切 2 格 → 档位 [100, 150, 200]。

def test_gap_entirely_below_price_opens_as_buy_at_its_lower_edge(client):
    client.px = 250.0                      # 两格都在价下
    up, _ = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "2", "1")))
    sides = [(o["side"], float(o["price"])) for o in client.orders]
    assert sides == [("Buy", 100.0), ("Buy", 150.0)]


def test_gap_entirely_above_price_opens_as_sell_at_its_upper_edge(client):
    client.px = 50.0                       # 两格都在价上
    up, _ = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "2", "1")))
    sides = [(o["side"], float(o["price"])) for o in client.orders]
    assert sides == [("Sell", 150.0), ("Sell", 200.0)]


def test_the_gap_containing_the_price_opens_as_buy(client):
    client.px = 120.0                      # 落在第 0 格里
    up, _ = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "2", "1")))
    sides = [(o["side"], float(o["price"])) for o in client.orders]
    assert sides == [("Buy", 100.0), ("Sell", 200.0)]


def test_one_order_per_gap(client):
    """n 格恒定 n 张单，同一价位不会同时挂买和卖——这是这个模型的立身之本。"""
    up, _ = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "8", "1")))
    assert len(client.orders) == 8
    prices = [o["price"] for o in client.orders]
    assert len(set(prices)) == 8


# ── 精度护栏 ──────────────────────────────────────────────────────

def test_gap_finer_than_tick_is_rejected(client):
    """格间距小于最小价位时档位会撞在一起，铺出来是一堆同价单。"""
    client.info = {"tickSize": "10", "qtyStep": "0.001", "minOrderQty": "0.001"}
    up, msg = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "50", "1")))
    assert not client.orders
    assert any("格间距太小" in r for r in msg.replies)


def test_qty_below_exchange_minimum_is_rejected(client):
    client.info = {"tickSize": "0.1", "qtyStep": "0.001", "minOrderQty": "5"}
    up, msg = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "2", "1")))
    assert not client.orders
    assert any("最小下单量" in r for r in msg.replies)


def test_thin_spacing_warns_about_fees(client):
    """格间距接近双边手续费时利润是负的，但这只该警告不该拦——是他的钱他决定。"""
    client.px = 1000000.0
    up, msg = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "2", "1")))
    assert client.orders, "只警告，不拦"
    assert any("接近手续费" in r for r in msg.replies)


# ── 闸门继承 ──────────────────────────────────────────────────────

def test_grid_orders_go_through_the_guarded_entry_point():
    """网格必须用 `client.place_limit` 下单，不能自己拼 HTTP 绕过闸门。

    `/killswitch` 和 `LIVE_TRADING=off` 的实现都在 `bybit_trade._guard_order`，
    而它写在 `place_limit` **内部**。哪天有人图省事在 grid 里直接发请求，
    这两个开关对网格就等于不存在了——而网格恰恰是唯一会自己反复下单的模块。
    """
    src = inspect.getsource(grid)
    assert "place_limit" in src
    for raw in ("_post(", "_get(", "httpx.", "requests."):
        assert raw not in src, f"网格绕过了下单函数直接发请求（{raw}），闸门会失效"


def test_killswitch_stops_a_grid_from_placing_orders(monkeypatch):
    """闸门真的抛的时候，网格不能把它吞掉当没事发生。"""
    import bybit_trade

    class GuardedClient(FakeClient):
        async def place_limit(self, symbol, side, qty, price, link_id=None, **kw):
            bybit_trade._guard_order({"symbol": symbol, "side": side,
                                      "orderType": "Limit", "qty": qty,
                                      "price": price, "reduceOnly": False})
            return await super().place_limit(symbol, side, qty, price, link_id, **kw)

    c = GuardedClient()
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: c)
    monkeypatch.setattr(grid, "_is_testnet", lambda: False)
    monkeypatch.setenv("BYBIT_TESTNET", "false")
    monkeypatch.setenv("LIVE_TRADING", "off")

    up, msg = _update()
    _run(grid.grid_start(up, _ctx("BTCUSDT", "100", "200", "2", "1")))
    assert not c.orders, "闸门关着还是把单铺出去了"
    assert any("失败" in r for r in msg.replies), "被闸门拦下要说出来，不能静默"


# ── 成交翻转与记账 ────────────────────────────────────────────────

def _running_grid(chat_id=111, **kw):
    # qty 刻意不取 1：一格利润是 (U-L)×qty，qty=1 时乘不乘都一样，
    # 漏掉 qty 这个 bug 就测不出来了。
    g = {"chat_id": chat_id, "symbol": "BTCUSDT", "gid_seq": "1",
         "lower": 100.0, "upper": 200.0, "n": 1,
         "levels": ["100", "200"], "qty": "3", "leverage": 1,
         "tick": "0.1", "qty_step": "0.001",
         "orders": {"0": {"holding": False, "L": "100", "U": "200",
                          "side": "Buy", "price": "100",
                          "link_id": "LID0", "order_id": "O0"}},
         "status": "running", "realized": 0.0, "fills": 0,
         "breakout_notified": False, "started_ts": 0}
    g.update(kw)
    storage.data["grids"]["%d:BTCUSDT" % chat_id] = g
    return g


def test_buy_fill_flips_to_holding_and_places_the_sell(client, monkeypatch):
    g = _running_grid()
    client.open_links = []                 # 不在挂单里 = 已经离场
    client.statuses = {"LID0": "Filled"}
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    _run(grid.poll_grids(_job_ctx()))

    o = g["orders"]["0"]
    assert o["holding"] is True
    assert g["fills"] == 1
    assert g["realized"] == 0.0, "买入还没赚到钱，这一步不该记利润"
    assert client.orders[-1]["side"] == "Sell"
    assert float(client.orders[-1]["price"]) == 200.0


def test_sell_fill_books_exactly_one_gap_of_profit(client, monkeypatch):
    g = _running_grid()
    g["orders"]["0"].update(holding=True, side="Sell", price="200")
    client.open_links = []
    client.statuses = {"LID0": "Filled"}
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    _run(grid.poll_grids(_job_ctx()))

    o = g["orders"]["0"]
    assert o["holding"] is False
    assert g["fills"] == 1
    assert g["realized"] == pytest.approx((200.0 - 100.0) * 3.0)
    assert client.orders[-1]["side"] == "Buy"
    assert float(client.orders[-1]["price"]) == 100.0


def test_still_open_orders_are_left_alone(client, monkeypatch):
    g = _running_grid()
    client.open_links = ["LID0"]           # 还挂着
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    _run(grid.poll_grids(_job_ctx()))

    assert not client.orders, "单子还挂着就不该重挂，会变成两张"
    assert g["fills"] == 0


def test_externally_cancelled_order_is_reposted_on_the_same_side(client, monkeypatch):
    """被人在交易所手动撤了 → 补回**同方向**，不能当成成交去翻转。"""
    g = _running_grid()
    client.open_links = []
    client.statuses = {"LID0": "Cancelled"}
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    _run(grid.poll_grids(_job_ctx()))

    assert g["fills"] == 0
    assert g["realized"] == 0.0
    assert g["orders"]["0"]["holding"] is False
    assert client.orders[-1]["side"] == "Buy"


def test_transient_status_is_not_treated_as_a_fill(client, monkeypatch):
    """PartiallyFilled 这类瞬态要等下一轮，急着翻转会把半成交当成一格套利。"""
    g = _running_grid()
    client.open_links = []
    client.statuses = {"LID0": "PartiallyFilled"}
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    _run(grid.poll_grids(_job_ctx()))

    assert g["fills"] == 0
    assert not client.orders


# ── 越界告警 ──────────────────────────────────────────────────────

def test_breakout_alerts_once_not_every_poll(client, monkeypatch):
    """20 秒一轮的任务，不去重就是每 20 秒一条——告警刷屏比不告警更糟。"""
    g = _running_grid()
    client.open_links = ["LID0"]
    client.px = 250.0                      # 涨破上限
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    ctx = _job_ctx()
    _run(grid.poll_grids(ctx))
    _run(grid.poll_grids(ctx))

    assert len(ctx.bot.sent) == 1
    assert "涨破上限" in ctx.bot.sent[0][1]
    assert g["breakout_notified"] is True


def test_breakout_flag_resets_after_price_returns(client, monkeypatch):
    """回到区间内要重置，否则第二次真越界时反而不报了。"""
    g = _running_grid(breakout_notified=True)
    client.open_links = ["LID0"]
    client.px = 150.0                      # 回到区间内
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    _run(grid.poll_grids(_job_ctx()))
    assert g["breakout_notified"] is False


def test_breaking_below_says_so(client, monkeypatch):
    _running_grid()
    client.open_links = ["LID0"]
    client.px = 50.0
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)

    ctx = _job_ctx()
    _run(grid.poll_grids(ctx))
    assert "跌破下限" in ctx.bot.sent[0][1]


def test_stopped_grids_are_not_polled(client, monkeypatch):
    _running_grid(status="stopped")
    monkeypatch.setattr(grid, "BybitClient", lambda *a, **k: client)
    _run(grid.poll_grids(_job_ctx()))
    assert not client.orders


# ── 停止 ──────────────────────────────────────────────────────────

def test_stop_cancels_orders_and_marks_it_stopped(client):
    g = _running_grid()
    up, msg = _update()
    _run(grid.grid_stop(up, _ctx("BTCUSDT")))
    assert client.cancelled == ["BTCUSDT"]
    assert g["status"] == "stopped"
    assert any("已停止" in r for r in msg.replies)


def test_stop_warns_that_positions_are_not_closed(client):
    """撤单 ≠ 平仓。不说这句，他会以为网格停了就没风险敞口了。"""
    _running_grid()
    up, msg = _update()
    _run(grid.grid_stop(up, _ctx("BTCUSDT")))
    assert any("平仓" in r for r in msg.replies)


def test_stop_without_a_running_grid_does_nothing(client):
    up, msg = _update()
    _run(grid.grid_stop(up, _ctx("BTCUSDT")))
    assert not client.cancelled
    assert any("没有运行中" in r for r in msg.replies)


# ── 状态 ──────────────────────────────────────────────────────────

def test_status_only_shows_your_own_grids():
    _running_grid(chat_id=222)             # 另一个管理员的
    up, msg = _update(chat_id=111)
    _run(grid.grid_status(up, _ctx()))
    assert any("还没有网格" in r for r in msg.replies)


def test_status_reports_fills_and_profit():
    _running_grid(fills=7, realized=12.5)
    up, msg = _update()
    _run(grid.grid_status(up, _ctx()))
    out = "\n".join(msg.replies)
    assert "7" in out and "12.5" in out
