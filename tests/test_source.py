"""统一取价层 + 预警/监控的数据源选择。

这块的风险不在"能不能取到价"，而在**取错源却不自知**：
  • 拿 OKX 的基准去比 Gate 的现价 —— 小币差个百分之几很常见，
    足够凭空触发一次 ±2% 预警，用户还以为真动了；
  • 用户指定了 Gate，取不到就偷偷换 Bybit —— 他以为在盯 Gate 的价；
  • 标签字符串被改 —— data.json 里的存量监控和 WebSocket 实时通道全部对不上号。
下面每一条都是冲着这三件事去的。
"""
import asyncio

import pytest

from handlers import source as S


# ── 标签：存量数据的一部分，只能加不能改 ──────────────────────────
def test_labels_match_the_websocket_channel():
    """contract_ws._WS_SRC 把 WS 的 tick 映射成这些标签，改一个字实时监控就哑了。"""
    from handlers.contract_ws import _WS_SRC
    for label in _WS_SRC.values():
        assert label in S.FROM_LABEL, f"{label} 不在标签表里，WS 实时通道会对不上"


def test_labels_are_stable():
    """这些字符串已经写进了用户 data.json 的 src 字段。"""
    assert S.label_of("bybit", S.SWAP) == "Bybit永续"
    assert S.label_of("okx", S.SWAP) == "OKX永续"
    assert S.label_of("binance", S.SPOT) == "Binance"
    assert S.label_of("gate", S.SWAP) == "Gate永续"


def test_label_round_trip():
    for (ex, market), label in S.LABEL.items():
        assert S.split_label(label) == (ex, market)


def test_unknown_label_degrades_to_auto():
    """老数据里可能有任何东西，不能因为一个不认识的标签就崩在后台任务里。"""
    assert S.split_label("火币现货") == (S.AUTO, S.AUTO)
    assert S.split_label(None) == (S.AUTO, S.AUTO)
    assert S.split_label("") == (S.AUTO, S.AUTO)


# ── 币名归一 ─────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    ("btc", "BTC"), ("BTCUSDT", "BTC"), ("BTC-USDT", "BTC"),
    ("BTC_USDT", "BTC"), (" ake ", "AKE"), ("USDT", "USDT"),
])
def test_norm(raw, want):
    assert S.norm(raw) == want


# ── 候选顺序 ─────────────────────────────────────────────────────
def test_explicit_exchange_never_tries_another():
    """指定了 Gate 就只能查 Gate。取不到要如实说没有，不能偷偷换一家——
    否则用户以为在盯 Gate 的价，实际盯的是 Bybit 的。"""
    cands = S.candidates("gate", S.SWAP)
    assert cands == [("gate", S.SWAP)]
    assert all(ex == "gate" for ex, _ in S.candidates("gate", S.AUTO))


def test_auto_keeps_the_original_priority():
    """顺序改了，存量监控会换源、基准跳变，等于凭空报一次警。"""
    order = [ex for ex, _ in S.candidates(S.AUTO, S.SPOT)]
    assert order[:4] == ["okx", "binance", "bybit", "gate"]


def test_coingecko_is_last_resort_and_spot_only():
    """留着兜底是为了少数只有 CoinGecko 有映射的币；但它没有永续。"""
    assert S.candidates(S.AUTO, S.SPOT)[-1] == ("coingecko", S.SPOT)
    assert all(ex != "coingecko" for ex, _ in S.candidates(S.AUTO, S.SWAP))
    assert all(ex != "coingecko" for ex, _ in S.candidates("bybit", S.AUTO))


# ── 取价 ─────────────────────────────────────────────────────────
def fake_market(prices):
    """prices: {(ex, market): 价格}。没列的当作这家没有这个币。"""
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


def test_auto_falls_through_to_whoever_has_it(market):
    """AKE 这类小币 OKX 没有、Bybit 有——截图里那条监控就是这么落到 Bybit 的。"""
    market({("bybit", S.SWAP): 0.0107})
    p, label = asyncio.run(S.price("AKE"))
    assert (p, label) == (0.0107, "Bybit永续")


def test_explicit_exchange_returns_nothing_rather_than_wrong_price(market):
    """Gate 没有就返回空，绝不拿 Bybit 的价冒充。"""
    market({("bybit", S.SWAP): 100.0})
    assert asyncio.run(S.price("AKE", "gate")) == (None, None)


def test_market_choice_is_respected(market):
    market({("okx", S.SPOT): 10.0, ("okx", S.SWAP): 10.5})
    assert asyncio.run(S.price("X", "okx", S.SPOT)) == (10.0, "OKX")
    assert asyncio.run(S.price("X", "okx", S.SWAP)) == (10.5, "OKX永续")


def test_zero_or_negative_price_is_not_a_price(market):
    """停牌/异常时有的所会回 0，用它当基准后面全是假信号。"""
    market({("okx", S.SPOT): 0.0, ("bybit", S.SPOT): 3.0})
    assert asyncio.run(S.price("X", market=S.SPOT)) == (3.0, "Bybit")


def test_one_exchange_blowing_up_does_not_kill_the_chain(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("okx 超时")

    async def ok(*_a, **_k):
        return 5.0
    monkeypatch.setattr(S, "_FETCH", {"okx": boom, "binance": ok, "bybit": ok,
                                      "gate": ok, "coingecko": ok})
    p, label = asyncio.run(S.price("X", market=S.SPOT))
    assert (p, label) == (5.0, "Binance")


def test_price_at_pins_the_source(market):
    """轮询必须回到基准那个源，否则涨跌是跨所算的。"""
    market({("okx", S.SPOT): 10.0, ("gate", S.SPOT): 11.0})
    assert asyncio.run(S.price_at("X", "Gate")) == 11.0
    assert asyncio.run(S.price_at("X", "OKX")) == 10.0


def test_prices_at_batches_on_one_source(market):
    market({("gate", S.SWAP): 2.5})
    got = asyncio.run(S.prices_at(["AAA", "BBB"], "Gate永续"))
    assert got == {"AAA": 2.5, "BBB": 2.5}


def test_prices_at_skips_what_it_cannot_get(market):
    """取不到的键不出现——调用方靠"键在不在"判断，塞个 0 进去会被当成价格。"""
    market({})
    assert asyncio.run(S.prices_at(["AAA"], "Gate永续")) == {}


# ── 默认偏好 + 单条覆盖 ──────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_prefs():
    """碰全局 storage.data 的测试必须自己清干净（服务器上是随机顺序跑的）。"""
    import storage
    storage.data["user_prefs"] = {}
    storage.data["alerts"] = []
    storage.data["watchpct"] = []
    yield
    storage.data["user_prefs"] = {}
    storage.data["alerts"] = []
    storage.data["watchpct"] = []


def test_pref_defaults_to_auto():
    assert S.get_pref(123) == (S.AUTO, S.AUTO)


def test_pref_round_trip():
    S.set_pref(123, "gate", S.SWAP)
    assert S.get_pref(123) == ("gate", S.SWAP)


def test_override_beats_the_default(market):
    """「单条覆盖 > 会话默认」——这就是这套选择方式的全部含义。"""
    market({("okx", S.SPOT): 10.0, ("gate", S.SWAP): 11.0})
    S.set_pref(123, "okx", S.SPOT)
    assert asyncio.run(S.price_for(123, "X"))[0] == 10.0
    assert asyncio.run(S.price_for(123, "X", override="Gate永续"))[0] == 11.0


def test_describe_is_readable():
    assert "自动" in S.describe(S.AUTO, S.AUTO)
    assert S.describe("bybit", S.SWAP) == "Bybit 永续"


# ── 面板按钮 ─────────────────────────────────────────────────────
def _cbs(kb):
    return [b.callback_data for row in kb.inline_keyboard for b in row]


def test_panel_offers_every_exchange():
    cbs = _cbs(S.source_kb("def"))
    for ex in ("auto", "okx", "binance", "bybit", "gate"):
        assert f"src:ex:def:{ex}" in cbs
    for m in ("auto", "spot", "swap"):
        assert f"src:mk:def:{m}" in cbs
    assert "src:ok:def" in cbs


def test_panel_callbacks_fit_telegram_limit():
    for cb in _cbs(S.source_kb("al|12")) + _cbs(S.source_kb("wp|1000PEPE")):
        assert len(cb.encode()) <= 64


def test_panel_is_routed_by_the_menu():
    """有按钮没接线 = 点了没反应，v1.14.0 刚栽过一次。"""
    import inspect
    from handlers import menu
    assert 'startswith("src:")' in inspect.getsource(menu.button_handler)
