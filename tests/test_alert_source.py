"""价格预警 / 波动监控的数据源行为。

最要命的一条：**同一条预警的基准和现价必须来自同一个源**。
实测各所同一币的价差在流动性好的盘上很小（BTC 0.02%、TUT 0.08%），
但现货↔永续的基差和冷门小币要大得多，而且换源那一刻是阶跃——
足够让 ±2% 这种小阈值凭空响一次，用户会当成真的动了，可能真去下单。
"""
import asyncio

import pytest

import storage
from handlers import alert as A
from handlers import source as S
from handlers import watchpct as W


@pytest.fixture(autouse=True)
def _clean():
    """碰全局 data 的键必须自己重置（服务器上 pytest 是随机顺序跑的）。"""
    for k in ("alerts", "watchpct", "user_prefs"):
        storage.data[k] = [] if k != "user_prefs" else {}
    yield
    for k in ("alerts", "watchpct", "user_prefs"):
        storage.data[k] = [] if k != "user_prefs" else {}


def fake_market(prices):
    def make(ex):
        async def fetch(_c, _sym, market):
            return prices.get((ex, market))
        return fetch
    return {ex: make(ex) for ex in ("okx", "binance", "bybit", "gate", "coingecko")}


@pytest.fixture
def market(monkeypatch):
    def apply(prices):
        monkeypatch.setattr(S, "_FETCH", fake_market(prices))
    return apply


class Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append((chat_id, text))


class Ctx:
    def __init__(self):
        self.bot = Bot()


# ── 建预警时盖上数据源 ───────────────────────────────────────────
def test_new_alert_inherits_the_chat_default():
    S.set_pref(7, "gate", S.SWAP)
    A.add_alert(7, {"type": "fixed", "symbol": "AKE", "target": 1, "direction": "above"})
    assert storage.data["alerts"][0]["src"] == "Gate永续"


def test_auto_default_leaves_the_source_open():
    """没设默认时不锁源，后台按自动链找——老用户的用法不变。"""
    A.add_alert(7, {"type": "fixed", "symbol": "BTC", "target": 1, "direction": "above"})
    assert storage.data["alerts"][0]["src"] == ""


def test_index_is_per_chat():
    """「换数据源」按钮带的是本会话内的序号，混进别人的预警就改错人了。"""
    A.add_alert(7, {"type": "fixed", "symbol": "A", "target": 1, "direction": "above"})
    A.add_alert(9, {"type": "fixed", "symbol": "B", "target": 1, "direction": "above"})
    idx = A.add_alert(7, {"type": "fixed", "symbol": "C", "target": 1, "direction": "above"})
    assert idx == 1


# ── 后台检查：绝不跨源比价 ───────────────────────────────────────
def test_each_alert_is_checked_against_its_own_source(market):
    """两条一样的 ±2% 预警，基准一样，但源不同——只有源上真动了的那条该响。"""
    market({("okx", S.SPOT): 100.0, ("gate", S.SPOT): 105.0})
    storage.data["alerts"] = [
        {"type": "pct", "chat_id": 1, "symbol": "X", "pct": 2, "base_price": 100.0,
         "src": "OKX"},
        {"type": "pct", "chat_id": 2, "symbol": "X", "pct": 2, "base_price": 100.0,
         "src": "Gate"},
    ]
    ctx = Ctx()
    asyncio.run(A.check_alerts(ctx))
    chats = [c for c, _ in ctx.bot.sent]
    assert chats == [2], "OKX 那条没动却响了 = 拿 Gate 的价比了 OKX 的基准"


def test_legacy_alert_without_src_still_works(market):
    """老数据没有 src 字段，不能因此静默失效——预警不响比报错危险得多。"""
    market({("okx", S.SPOT): 130.0})
    storage.data["alerts"] = [
        {"type": "fixed", "chat_id": 1, "symbol": "X", "target": 120.0,
         "direction": "above"},
    ]
    ctx = Ctx()
    asyncio.run(A.check_alerts(ctx))
    assert len(ctx.bot.sent) == 1


def test_unreachable_source_does_not_fire_anything(market):
    """取不到价时必须保持沉默，不能当成 0 触发跌破。"""
    market({})
    storage.data["alerts"] = [
        {"type": "fixed", "chat_id": 1, "symbol": "X", "target": 10.0,
         "direction": "below", "src": "Gate永续"},
    ]
    ctx = Ctx()
    asyncio.run(A.check_alerts(ctx))
    assert ctx.bot.sent == []
    assert storage.data["alerts"], "取不到价不该把预警删掉"


def test_message_says_which_source(market):
    market({("bybit", S.SWAP): 130.0})
    storage.data["alerts"] = [
        {"type": "fixed", "chat_id": 1, "symbol": "X", "target": 120.0,
         "direction": "above", "src": "Bybit永续"},
    ]
    ctx = Ctx()
    asyncio.run(A.check_alerts(ctx))
    assert "Bybit永续" in ctx.bot.sent[0][1]


def test_one_broken_source_does_not_stop_the_others(monkeypatch):
    """一个源挂了，别的源的预警还得照常检查。"""
    calls = []

    async def flaky(symbols, label):
        calls.append(label)
        if label == "Gate永续":
            raise RuntimeError("gate 超时")
        return {s: 130.0 for s in symbols}
    monkeypatch.setattr(S, "prices_at", flaky)
    storage.data["alerts"] = [
        {"type": "fixed", "chat_id": 1, "symbol": "X", "target": 120.0,
         "direction": "above", "src": "Gate永续"},
        {"type": "fixed", "chat_id": 2, "symbol": "Y", "target": 120.0,
         "direction": "above", "src": "OKX"},
    ]
    ctx = Ctx()
    asyncio.run(A.check_alerts(ctx))
    assert [c for c, _ in ctx.bot.sent] == [2]


# ── 换数据源：基准要跟着换 ───────────────────────────────────────
def test_repoint_rebases_a_pct_alert(market):
    """换了源还沿用旧基准，等于拿 A 所的价比 B 所的价，换完立刻误报一次。"""
    market({("okx", S.SPOT): 100.0, ("gate", S.SPOT): 105.0})
    A.add_alert(1, {"type": "pct", "symbol": "X", "pct": 2, "base_price": 100.0})
    ok, msg = asyncio.run(A.repoint(1, "0", "Gate"))
    assert ok
    assert storage.data["alerts"][0]["base_price"] == 105.0
    assert storage.data["alerts"][0]["src"] == "Gate"


def test_repoint_warns_when_the_new_source_has_no_such_coin(market):
    market({("bybit", S.SWAP): 1.0})
    A.add_alert(1, {"type": "pct", "symbol": "AKE", "pct": 2, "base_price": 1.0})
    ok, msg = asyncio.run(A.repoint(1, "0", "Gate"))
    assert ok and "查不到" in msg


def test_repoint_rejects_a_bad_index():
    ok, msg = asyncio.run(A.repoint(1, "5", "Gate"))
    assert not ok and "没找到" in msg


# ── 波动监控 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("tok,want", [
    ("gate", "gate"), ("Gate", "gate"), ("芝麻", "gate"),
    ("okx", "okx"), ("欧易", "okx"), ("币安", "binance"), ("bn", "binance"),
    ("bybit", "bybit"), ("合约", None), ("5", None),
])
def test_watchpct_parses_the_exchange_token(tok, want):
    assert W.parse_exchange(tok) == want


def test_watch_uses_the_chat_default(market):
    market({("okx", S.SPOT): 10.0, ("gate", S.SWAP): 11.0})
    S.set_pref(1, "gate", S.SWAP)
    ok, msg = asyncio.run(W.add_watch(1, "X", 5, "me"))
    assert ok and "Gate永续" in msg
    assert storage.data["watchpct"][0]["src"] == "Gate永续"


def test_explicit_exchange_beats_the_default(market):
    market({("okx", S.SPOT): 10.0, ("gate", S.SWAP): 11.0})
    S.set_pref(1, "gate", S.SWAP)
    ok, msg = asyncio.run(W.add_watch(1, "X", 5, "me", market="spot", exchange="okx"))
    assert ok and "OKX" in msg
    assert storage.data["watchpct"][0]["base"] == 10.0


def test_explicit_exchange_without_the_coin_says_so(market):
    """指定 Gate 但 Gate 没有——要如实说，并提示换一家，不能偷偷用 Bybit。"""
    market({("bybit", S.SWAP): 1.0})
    ok, msg = asyncio.run(W.add_watch(1, "AKE", 5, "me", exchange="gate"))
    assert not ok
    assert "Gate" in msg and "换一家" in msg
    assert storage.data["watchpct"] == []


def test_watch_repoint_rebases(market):
    market({("okx", S.SPOT): 10.0, ("gate", S.SPOT): 12.0})
    asyncio.run(W.add_watch(1, "X", 5, "me", market="spot", exchange="okx"))
    ok, msg = asyncio.run(W.repoint(1, "X", "Gate"))
    assert ok
    w = storage.data["watchpct"][0]
    assert (w["src"], w["base"]) == ("Gate", 12.0)


def test_watch_repoint_refuses_a_source_without_the_coin(market):
    market({("okx", S.SPOT): 10.0})
    asyncio.run(W.add_watch(1, "X", 5, "me", market="spot", exchange="okx"))
    ok, msg = asyncio.run(W.repoint(1, "X", "Gate永续"))
    assert not ok
    assert storage.data["watchpct"][0]["src"] == "OKX", "失败了就不该动原来的监控"


# ── 命令和引导流程必须走同一套解析 ───────────────────────────────
# v1.16.0 只给 /watchpct 命令加了选交易所，菜单点进去的引导流程漏了，
# 而 Logan 平时就是点按钮进来的——他看到的还是老样子。
@pytest.mark.parametrize("tokens,want", [
    ([], ("auto", None)),
    (["合约"], ("swap", None)),
    (["gate"], ("auto", "gate")),
    (["合约", "gate"], ("swap", "gate")),
    (["gate", "合约"], ("swap", "gate")),      # 不分先后
    (["现货", "币安"], ("spot", "binance")),
    (["垃圾话"], ("auto", None)),
])
def test_tokens_parse_the_same_both_ways(tokens, want):
    assert W.parse_tokens(tokens) == want


def test_guided_flow_uses_the_shared_parser():
    """引导流程和命令都必须调 parse_tokens，否则又会只改一处。"""
    import inspect
    from handlers import quickprice
    assert "parse_tokens" in inspect.getsource(quickprice.quick_price)
    assert "parse_tokens" in inspect.getsource(W.watchpct)


def test_guided_prompt_mentions_exchange():
    """点按钮进来的那条提示里要写明能选交易所，否则等于没做。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    start = src.index('elif d == "watchpct_start"')
    block = src[start:start + 1400]
    assert "交易所" in block
    assert "src:panel:def" in block, "面板上要有改数据源的按钮"


def test_existing_watches_can_be_repointed_from_the_list():
    """已经在盯的币也要能换源，不能只有新建时能选。"""
    import inspect
    from handlers import menu
    assert "change_btn" in inspect.getsource(menu.render_my_watchpct)
    assert "change_btn" in inspect.getsource(menu.render_my_alerts)


# ── 小币不再被主流币白名单挡住 ───────────────────────────────────
def test_small_caps_can_have_alerts_now(market):
    """AKE、TUT 这类以前直接被 COIN_IDS 白名单拒了——而它们才是最需要盯的。"""
    from config import COIN_IDS
    assert "AKE" not in COIN_IDS
    market({("gate", S.SWAP): 0.0107})
    p, label = asyncio.run(S.price_for(1, "AKE"))
    assert p == 0.0107 and label == "Gate永续"
