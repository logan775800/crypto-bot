"""统一 K 线层：四家 × 现货/永续 → 同一种结构。

这块最危险的不是取不到数，而是**读串了字段却不报错**——指标照样算得出来，
只是全是垃圾。四家的差异（2026-08-14 实测）：
  Bybit/OKX 新→旧，币安/Gate 旧→新；Gate 时间戳是秒；
  Gate 现货是 [t, 成交额, 收, 高, 低, 开, 量]，根本不是 OHLC 顺序；
  OKX 永续第 5 列是张数、现货第 5 列才是基础量。
下面用各家真实响应的样本数据回放，逐家验归一。
"""
import asyncio

import pytest

from handlers import klines as K
from handlers import source as S


# ── 各家的真实响应样本（从 tools/probe_klines.py 实测抄回） ──────────
BYBIT_ROWS = [  # 新→旧
    ["1786777200000", "63081.4", "63086", "63035.2", "63046.8", "98.392", "6204418.75"],
    ["1786776300000", "63053.5", "63068.1", "63005.6", "63005.7", "66.954", "4221221.30"],
]
OKX_SWAP_ROWS = [  # 新→旧；[t,o,h,l,c,张数,基础量,成交额,confirm]
    ["1786777200000", "63084.1", "63084.1", "63034", "63042.9",
     "25875.7", "258.757", "16315196.99", "0"],
    ["1786776300000", "63055.6", "63068.1", "63008.9", "63009",
     "11379.47", "113.7947", "7174090.88", "1"],
]
OKX_SPOT_ROWS = [  # 现货第5列就是基础量，第6列成交额
    ["1786777200000", "63112.4", "63114.1", "63062.1", "63071.5",
     "7.16976557", "452371.16", "452371.16", "0"],
]
BINANCE_ROWS = [  # 旧→新；[开盘时间,o,h,l,c,量,收盘时间,成交额,...]
    [1786773600000, "63089.00", "63095.32", "63037.76", "63037.76", "78.21",
     1786774499999, "4933879.31", 3884, "13.28", "838147.14", "0"],
    [1786777200000, "63114.68", "63118.00", "63068.00", "63080.34", "124.73",
     1786778099999, "7870542.65", 7994, "79.41", "5010460.87", "0"],
]
GATE_SPOT_ROWS = [  # 旧→新；秒；[t, 成交额, 收, 高, 低, 开, 基础量, 已收]
    ["1786773600", "1005219.70", "63039.6", "63094.8", "63039.6", "63077.4",
     "15.93", "true"],
    ["1786777200", "883189.84", "63067.8", "63115.3", "63065.8", "63111.6",
     "13.99", "false"],
]
GATE_SWAP_ROWS = [  # 旧→新；秒；dict；v 是张数
    {"t": 1786773600, "o": "63047.4", "h": "63064.3", "l": "63005", "c": "63006",
     "v": 342454, "sum": "2158929.78"},
    {"t": 1786777200, "o": "63083.6", "h": "63088.4", "l": "63036", "c": "63046.4",
     "v": 490711, "sum": "3094861.10"},
]


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def json(self):
        return self._p


class FakeClient:
    """按 URL 关键字回不同样本，并记下实际请求参数。"""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, params or {}))
        for key, payload in self.mapping.items():
            if key in url:
                return FakeResp(payload)
        raise AssertionError(f"没预料到的请求: {url}")


@pytest.fixture
def client(monkeypatch):
    def apply(mapping):
        c = FakeClient(mapping)
        monkeypatch.setattr(S, "client", lambda: c)
        return c
    return apply


def fetch(*a, **kw):
    return asyncio.run(K.fetch(*a, **kw))


# ── 归一：四家出来必须是同一种结构 ───────────────────────────────
def test_bybit_is_reversed_to_oldest_first(client):
    client({"bybit.com": {"result": {"list": BYBIT_ROWS}, "time": 1786777300000}})
    rows, meta = fetch("BTC", "15m", 100, "bybit", S.SWAP)
    assert [r[0] for r in rows] == [1786776300000, 1786777200000]
    assert rows[-1][1:5] == [63081.4, 63086.0, 63035.2, 63046.8]
    assert meta["label"] == "Bybit永续"


def test_okx_swap_takes_base_volume_not_contracts(client):
    """永续第5列是张数(25875.7)，基础量在第6列(258.757)——拿错量能全歪。"""
    client({"okx.com": {"data": OKX_SWAP_ROWS}})
    rows, _ = fetch("BTC", "15m", 100, "okx", S.SWAP)
    assert rows[-1][5] == 258.757
    assert rows[-1][6] == 16315196.99


def test_okx_spot_uses_a_different_column(client):
    """同一家两种语义：现货第5列就是基础量。"""
    client({"okx.com": {"data": OKX_SPOT_ROWS}})
    rows, _ = fetch("BTC", "15m", 100, "okx", S.SPOT)
    assert rows[-1][5] == 7.16976557
    assert rows[-1][6] == 452371.16


def test_binance_is_already_oldest_first(client):
    client({"binance.com": BINANCE_ROWS})
    rows, _ = fetch("BTC", "15m", 100, "binance", S.SWAP)
    assert [r[0] for r in rows] == [1786773600000, 1786777200000]
    assert rows[-1][4] == 63080.34
    assert rows[-1][6] == 7870542.65      # 成交额取第8列，不是第6列


def test_gate_spot_field_order_is_not_ohlc(client):
    """Gate 现货是 [t, 额, 收, 高, 低, 开, 量]。照 OHLC 读会把收当开。"""
    client({"gateio.ws": GATE_SPOT_ROWS})
    rows, _ = fetch("BTC", "15m", 100, "gate", S.SPOT)
    t, o, h, lo, c, v, tn = rows[-1]
    assert (o, h, lo, c) == (63111.6, 63115.3, 63065.8, 63067.8)
    assert (v, tn) == (13.99, 883189.84)


def test_gate_timestamps_are_seconds(client):
    """Gate 给的是秒，不乘 1000 的话 K 线会被当成 1970 年的。"""
    client({"gateio.ws": GATE_SPOT_ROWS})
    rows, _ = fetch("BTC", "15m", 100, "gate", S.SPOT)
    assert rows[-1][0] == 1786777200 * 1000


def test_gate_swap_converts_contracts_to_base(monkeypatch, client):
    """Gate 永续的 v 是张数，要乘合约乘数才是币的数量。"""
    client({"gateio.ws": GATE_SWAP_ROWS})

    async def fake_contracts():
        return {"BTC_USDT": {"quanto_multiplier": "0.0001"}}
    from handlers import gate as gate_mod
    monkeypatch.setattr(gate_mod, "contracts", fake_contracts)
    rows, _ = fetch("BTC", "15m", 100, "gate", S.SWAP)
    assert rows[-1][5] == pytest.approx(490711 * 0.0001)
    assert rows[-1][6] == 3094861.10


def test_every_exchange_returns_the_same_shape(client, monkeypatch):
    async def fake_contracts():
        return {"BTC_USDT": {"quanto_multiplier": "1"}}
    from handlers import gate as gate_mod
    monkeypatch.setattr(gate_mod, "contracts", fake_contracts)
    client({"bybit.com": {"result": {"list": BYBIT_ROWS}},
            "okx.com": {"data": OKX_SWAP_ROWS},
            "binance.com": BINANCE_ROWS,
            "gateio.ws": GATE_SWAP_ROWS})
    for ex in ("bybit", "okx", "binance", "gate"):
        rows, meta = fetch("BTC", "15m", 100, ex, S.SWAP)
        assert rows, ex
        for r in rows:
            assert len(r) == 7, ex
            assert isinstance(r[0], int) and r[0] > 10 ** 12, f"{ex} 时间戳不是毫秒"
            assert r[2] >= max(r[1], r[4]) and r[3] <= min(r[1], r[4]), \
                f"{ex} 的高低开收对不上，多半读串了列"
        assert rows[0][0] < rows[-1][0], f"{ex} 不是旧→新"


# ── 周期与上限 ───────────────────────────────────────────────────
def test_interval_is_translated_per_exchange(client):
    c = client({"okx.com": {"data": OKX_SWAP_ROWS}})
    fetch("BTC", "4h", 100, "okx", S.SWAP)
    assert c.calls[0][1]["bar"] == "4H", "OKX 的 ≥1h 周期必须大写，小写会报参数错误"


def test_bybit_interval_aliases_still_work(client):
    """marketdata 里到处在用 '60' '240' 'D' 这种 Bybit 写法。"""
    assert K.norm_interval("60") == "1h"
    assert K.norm_interval("240") == "4h"
    assert K.norm_interval("D") == "1d"


def test_okx_limit_is_capped_and_says_so(client):
    c = client({"okx.com": {"data": OKX_SWAP_ROWS}})
    rows, meta = fetch("BTC", "15m", 1000, "okx", S.SWAP)
    assert c.calls[0][1]["limit"] == 300, "OKX 单次最多 300 根"
    assert meta["capped"] is True
    assert "300" in K.note(meta) or "上限" in K.note(meta)


def test_no_cap_flag_when_within_limit(client):
    client({"bybit.com": {"result": {"list": BYBIT_ROWS}}})
    _rows, meta = fetch("BTC", "15m", 200, "bybit", S.SWAP)
    assert meta["capped"] is False


def test_unknown_interval_is_reported_not_guessed(client):
    _rows, meta = fetch("BTC", "7分钟", 100, "bybit", S.SWAP)
    assert not _rows and "周期" in meta["error"]


# ── 交易对拼装 ───────────────────────────────────────────────────
@pytest.mark.parametrize("ex,market,want", [
    ("bybit", S.SWAP, "BTCUSDT"), ("binance", S.SPOT, "BTCUSDT"),
    ("okx", S.SWAP, "BTC-USDT-SWAP"), ("okx", S.SPOT, "BTC-USDT"),
    ("gate", S.SWAP, "BTC_USDT"), ("gate", S.SPOT, "BTC_USDT"),
])
def test_symbol_format_per_exchange(ex, market, want):
    assert K._sym(ex, "BTCUSDT", market) == want


# ── 出错时要分清「这家没有」和「接口挂了」 ────────────────────────
def test_empty_result_says_which_exchange_lacks_it(client):
    client({"okx.com": {"data": []}})
    rows, meta = fetch("AKE", "15m", 100, "okx", S.SWAP)
    assert not rows
    assert "OKX" in meta["error"] and "AKE" in meta["error"]


def test_network_error_is_not_mistaken_for_missing_coin(client, monkeypatch):
    class Boom:
        async def get(self, *a, **k):
            raise RuntimeError("连接超时")
    monkeypatch.setattr(S, "client", lambda: Boom())
    rows, meta = fetch("BTC", "15m", 100, "gate", S.SWAP)
    assert not rows and "失败" in meta["error"]


def test_binance_error_dict_does_not_crash(client):
    """币安报错时回的是 dict 不是 list，照 list 解析会 TypeError。"""
    client({"binance.com": {"code": -1121, "msg": "Invalid symbol."}})
    rows, meta = fetch("NOPE", "15m", 100, "binance", S.SWAP)
    assert not rows and meta["error"]


# ── 会话默认 ─────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean():
    import storage
    storage.data["user_prefs"] = {}
    yield
    storage.data["user_prefs"] = {}


def test_kline_default_falls_back_to_bybit_not_auto(client):
    """K 线不能用「自动」：今天这家明天那家，画出来的结构对不上、回测不可比。"""
    c = client({"bybit.com": {"result": {"list": BYBIT_ROWS}}})
    asyncio.run(K.fetch_for(1, "BTC", "15m", 100))
    assert "bybit.com" in c.calls[0][0]


def test_kline_respects_the_session_default(client):
    c = client({"gateio.ws": GATE_SPOT_ROWS})
    S.set_pref(1, "gate", S.SPOT)
    asyncio.run(K.fetch_for(1, "BTC", "15m", 100))
    assert "gateio.ws" in c.calls[0][0]


def test_pref_label_helper():
    assert S.pref_label(1) is None
    S.set_pref(1, "gate", S.AUTO)
    assert S.pref_label(1) == "Gate永续"      # 市场没选时 K 线默认永续


# ── 品类判定：这是找出来的一个老 bug ──────────────────────────────
# Bybit 的 symbolType 实测有四个值（821 个合约）：
#   ''(504) 加密 ／ 'stock'(193) 美股 ／ 'commodity'(4) 贵金属 ／
#   'innovation'(120) **创新区，是真加密币**（AKE、AEON、ACU…）
# 原来 /scan、/steady 按「非空即非加密」过滤，把这 120 个新上的小币全当股票
# 剔掉了——而它们恰恰是扫描最该覆盖的一批（AKE 当时成交额 2.6 亿）。
def test_innovation_zone_coins_are_crypto():
    from handlers import marketdata as md
    assert md.is_crypto_type("innovation"), "创新区是真币，不能当股票剔掉"
    assert md.is_crypto_type("")


@pytest.mark.parametrize("t", ["stock", "commodity"])
def test_real_noncrypto_types_are_excluded(t):
    from handlers import marketdata as md
    assert not md.is_crypto_type(t)


def test_unknown_type_is_treated_as_crypto():
    """清单接口哪天多个新取值，宁可多列一个也别把正经币过滤掉。"""
    from handlers import marketdata as md
    assert md.is_crypto_type("brand_new_category")


def test_universe_tags_instead_of_dropping(monkeypatch):
    """非加密只打标记不剔除——「一共扫了多少个」要算进去，筛不筛是调用方的事。"""
    async def fake_types():
        return {"BTCUSDT": "", "AKEUSDT": "innovation", "AAPLUSDT": "stock"}

    async def fake_bad():
        return {"AAPL"}
    from handlers import marketdata as md
    monkeypatch.setattr(md, "symbol_types", fake_types)
    monkeypatch.setattr(K, "noncrypto_bases", fake_bad)

    class C:
        async def get(self, url, params=None):
            return FakeResp({"result": {"list": [
                {"symbol": "BTCUSDT", "turnover24h": "3", "price24hPcnt": "0.01"},
                {"symbol": "AKEUSDT", "turnover24h": "2", "price24hPcnt": "0.02"},
                {"symbol": "AAPLUSDT", "turnover24h": "1", "price24hPcnt": "0.03"},
            ]}})
    monkeypatch.setattr(S, "client", lambda: C())
    u = asyncio.run(K.universe("bybit", S.SWAP))
    got = {x["symbol"]: x["crypto"] for x in u}
    assert got == {"BTC": True, "AKE": True, "AAPL": False}
    assert [x["symbol"] for x in u] == ["BTC", "AKE", "AAPL"]   # 按成交额降序


def test_bybit_change_is_a_fraction_not_percent(monkeypatch):
    """Bybit 的 price24hPcnt 是小数(-0.000155)，别当百分数用，会差 100 倍。"""
    async def fake_types():
        return {}

    async def fake_bad():
        return set()
    from handlers import marketdata as md
    monkeypatch.setattr(md, "symbol_types", fake_types)
    monkeypatch.setattr(K, "noncrypto_bases", fake_bad)

    class C:
        async def get(self, url, params=None):
            return FakeResp({"result": {"list": [
                {"symbol": "BTCUSDT", "turnover24h": "1", "price24hPcnt": "-0.0155"}]}})
    monkeypatch.setattr(S, "client", lambda: C())
    u = asyncio.run(K.universe("bybit", S.SWAP))
    assert u[0]["change"] == pytest.approx(-1.55)


def test_okx_turnover_is_derived_from_base_volume(monkeypatch):
    """OKX 没有成交额字段，要 volCcy24h × last 自己算。"""
    async def fake_bad():
        return set()
    monkeypatch.setattr(K, "noncrypto_bases", fake_bad)

    class C:
        async def get(self, url, params=None):
            return FakeResp({"data": [{"instId": "BTC-USDT-SWAP", "last": "100",
                                       "open24h": "80", "volCcy24h": "5"}]})
    monkeypatch.setattr(S, "client", lambda: C())
    u = asyncio.run(K.universe("okx", S.SWAP))
    assert u[0]["turnover"] == 500
    assert u[0]["change"] == pytest.approx(25.0)


# ── 均线口径统一 + 中文字体 + OKX 持仓量代理 ─────────────────────
def test_okx_oi_period_is_mapped():
    """rubik 只认 5m / 1H / 1D——直接把 "15m" 传过去会静默拿到空列表，
    表现成"这家没有持仓量"，扫描就永远给不出拥挤度。"""
    per, ratio = K._OKX_OI_PERIOD["15m"]
    assert per == "5m" and ratio == 3          # 13 根 15m ≈ 39 根 5m，跨度一样
    assert K._OKX_OI_PERIOD["4h"][0] == "1H"
    assert K._OKX_OI_PERIOD["1d"][0] == "1D"


def test_okx_oi_uses_the_mapped_period(monkeypatch):
    calls = []

    class C:
        async def get(self, url, params=None):
            calls.append(params or {})
            return FakeResp({"code": "0",
                             "data": [[str(i), str(100 + i), "1"] for i in range(60)]})
    monkeypatch.setattr(S, "client", lambda: C())
    rows = asyncio.run(K.oi_series("BTC", "okx", "15m", 13))
    assert calls[0]["period"] == "5m"
    assert len(rows) == 39                     # 13 × 3


def test_okx_oi_empty_on_error(monkeypatch):
    class C:
        async def get(self, url, params=None):
            return FakeResp({"code": "51000", "msg": "period error"})
    monkeypatch.setattr(S, "client", lambda: C())
    assert asyncio.run(K.oi_series("BTC", "okx", "15m", 13)) == []
