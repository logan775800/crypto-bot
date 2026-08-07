"""合约身份解析 —— 解析错合约比不分析更危险，所以纯函数部分要锁死。

不联网：合约清单直接塞进模块缓存。
"""
import asyncio
import time

import pytest

from handlers import symbols as sy


def _inst(**kw):
    kw.setdefault("quote", "USDT")
    kw.setdefault("settle", "USDT")
    kw.setdefault("kind", "线性永续")
    kw.setdefault("multiplier", 1)
    kw.setdefault("min_qty", 1)
    kw.setdefault("qty_step", 1)
    kw.setdefault("tick", 0.0001)
    kw.setdefault("max_lev", 25)
    kw.setdefault("qty_unit", "币")
    kw.setdefault("status", "Trading")
    return sy.Inst(**kw)


@pytest.fixture(autouse=True)
def _cache():
    """直接注入清单，绕开网络。缓存是模块级的，用完要还原。"""
    old = dict(sy._cache)
    sy._cache.update({
        "ts": time.time(),
        "bybit": {
            "PEPE": [_inst(exchange="Bybit", symbol="1000PEPEUSDT", base="1000PEPE",
                           underlying="PEPE", multiplier=1000)],
            "LAB": [_inst(exchange="Bybit", symbol="LABUSDT", base="LAB", underlying="LAB")],
            "BTC": [_inst(exchange="Bybit", symbol="BTCUSDT", base="BTC",
                          underlying="BTC", min_qty=0.001, qty_step=0.001, max_lev=100)],
        },
        "okx": {
            "LAB": [_inst(exchange="OKX", symbol="LAB-USDT-SWAP", base="LAB",
                          underlying="LAB", qty_unit="张（1张=10 LAB）")],
        },
    })
    yield
    sy._cache.clear()
    sy._cache.update(old)


# ── 面值倍数 ─────────────────────────────────────────────────────
def test_multiplier_prefix_and_suffix():
    """交易所两种写法都有，写死一种另一种就会被当成不同的币。"""
    assert sy._split_multiplier("1000PEPE") == ("PEPE", 1000)
    assert sy._split_multiplier("SHIB1000") == ("SHIB", 1000)
    assert sy._split_multiplier("BTC") == ("BTC", 1)


def test_multiplier_does_not_eat_real_names():
    """别把本来就带数字的币名切坏。"""
    assert sy._split_multiplier("1INCH") == ("1INCH", 1)


def test_clean_strips_noise():
    assert sy._clean("btc/usdt") == "BTC"
    assert sy._clean("LAB-USDT-SWAP") == "LAB"
    assert sy._clean(" pepe 永续 ") == "PEPE"


# ── 解析 ─────────────────────────────────────────────────────────
def test_resolve_finds_1000x_by_plain_name():
    """用户说 PEPE，要能找到 1000PEPEUSDT，并带出面值倍数。"""
    insts, under = asyncio.run(sy.resolve("PEPE"))
    assert under == "PEPE" and len(insts) == 1
    assert insts[0].symbol == "1000PEPEUSDT" and insts[0].multiplier == 1000


def test_resolve_returns_all_candidates_across_exchanges():
    """同代号跨所必须全给出来，不许替用户挑一个。"""
    insts, _ = asyncio.run(sy.resolve("LAB"))
    assert {i.exchange for i in insts} == {"Bybit", "OKX"}


def test_resolve_unknown_returns_empty():
    insts, under = asyncio.run(sy.resolve("NOSUCHCOIN"))
    assert insts == [] and under == "NOSUCHCOIN"


# ── 给模型的约束 ─────────────────────────────────────────────────
def test_for_ai_single_warns_about_multiplier():
    insts, under = asyncio.run(sy.resolve("PEPE"))
    txt = sy.for_ai(insts, under)
    assert "1000PEPEUSDT" in txt
    assert "面值" in txt and "绝不能" in txt      # 拿现货价套止损是这里最贵的错


def test_for_ai_multiple_forbids_guessing():
    insts, under = asyncio.run(sy.resolve("LAB"))
    txt = sy.for_ai(insts, under)
    assert "自行假定" in txt              # 「你**不得**自行假定」，别把 markdown 星号算进来
    assert "不要给出任何价位" in txt


def test_for_ai_converges_when_prices_confirm_same_project():
    """跨所同名但价格核对一致 = 同一个项目，不该次次逼用户选；
    但必须点明面值/单位差异——「开100」在两个所不是一回事。"""
    insts, under = asyncio.run(sy.resolve("LAB"))
    prices = {"Bybit:LABUSDT": 1.0, "OKX:LAB-USDT-SWAP": 1.001}
    txt = sy.for_ai(insts, under, warn=None, prices=prices)
    assert "已确认" in txt and "LABUSDT" in txt
    assert "面值倍数与计量单位不同" in txt


def test_for_ai_stays_ambiguous_when_prices_diverge():
    """价格差得离谱 = 真·同名不同币，这时必须问清楚，不许收敛。"""
    insts, under = asyncio.run(sy.resolve("LAB"))
    txt = sy.for_ai(insts, under, warn="🚨 同名不同币警告：相差 300%",
                    prices={"Bybit:LABUSDT": 1.0, "OKX:LAB-USDT-SWAP": 4.0})
    assert "自行假定" in txt


def test_for_ai_collapses_same_exchange_versions():
    """同一个所的 USDT永续/USDC永续/交割不是歧义，是同一币的不同合约。"""
    a = _inst(exchange="Bybit", symbol="BTCUSDT", base="BTC", underlying="BTC")
    b = _inst(exchange="Bybit", symbol="BTCPERP", base="BTC", underlying="BTC",
              settle="USDC")
    txt = sy.for_ai([a, b], "BTC")
    assert "已确认" in txt and "BTCUSDT" in txt
    assert "自行假定" not in txt


def test_preferred_picks_usdt_perp_over_usdc_and_futures():
    """排第一的会被 sizing 直接采用——拿错结算币，保证金和爆仓价算的是别的合约。"""
    perp = _inst(exchange="Bybit", symbol="BTCUSDT", base="BTC", underlying="BTC")
    usdc = _inst(exchange="Bybit", symbol="BTCPERP", base="BTC", underlying="BTC",
                 settle="USDC")
    dated = _inst(exchange="Bybit", symbol="BTCUSDT-14AUG26", base="BTC",
                  underlying="BTC", kind="线性交割")
    assert sy.preferred([usdc, dated, perp]) is perp
    assert sorted([usdc, dated, perp], key=lambda i: sy._rank(i, "BTC"))[0] is perp


def test_preferred_none_when_no_usdt_perp():
    usdc = _inst(exchange="Bybit", symbol="BTCPERP", base="BTC", underlying="BTC",
                 settle="USDC")
    assert sy.preferred([usdc]) is None


def test_for_ai_missing_forbids_analysis():
    txt = sy.for_ai([], "NOSUCHCOIN")
    assert "不要给出该币的任何价位分析" in txt


def test_identity_block_has_all_fields():
    insts, _ = asyncio.run(sy.resolve("BTC"))
    ident = insts[0].identity()
    for must in ("交易所", "交易对", "标的", "结算币", "计量单位", "最小下单"):
        assert must in ident


# ── 同名不同币检测 ───────────────────────────────────────────────
def test_divergence_flags_wildly_different_prices(monkeypatch):
    """真·同一个币跨所价差是千分之几；差 5% 以上只有一种解释：不是同一个项目。"""
    insts, _ = asyncio.run(sy.resolve("LAB"))
    prices = {"Bybit:LABUSDT": 1.0, "OKX:LAB-USDT-SWAP": 4.0}

    async def fake_px(inst):
        return prices[inst.key]
    monkeypatch.setattr(sy, "_last_price", fake_px)
    warn, _ = asyncio.run(sy.divergence_check(insts))
    assert warn and "同名不同币" in warn


def test_divergence_silent_for_normal_spread(monkeypatch):
    insts, _ = asyncio.run(sy.resolve("LAB"))
    prices = {"Bybit:LABUSDT": 1.000, "OKX:LAB-USDT-SWAP": 1.002}

    async def fake_px(inst):
        return prices[inst.key]
    monkeypatch.setattr(sy, "_last_price", fake_px)
    warn, _ = asyncio.run(sy.divergence_check(insts))
    assert warn is None


def test_divergence_normalizes_multiplier(monkeypatch):
    """1000PEPE 报价天然是 PEPE 的 1000 倍，折算后不该误报。"""
    a = _inst(exchange="Bybit", symbol="1000PEPEUSDT", base="1000PEPE",
              underlying="PEPE", multiplier=1000)
    b = _inst(exchange="OKX", symbol="PEPE-USDT-SWAP", base="PEPE",
              underlying="PEPE", multiplier=1)

    async def fake_px(inst):
        return 10.0 if inst.multiplier == 1000 else 0.01
    monkeypatch.setattr(sy, "_last_price", fake_px)
    warn, _ = asyncio.run(sy.divergence_check([a, b]))
    assert warn is None


def test_divergence_needs_two_prices(monkeypatch):
    """只拿到一个价格时不能瞎报警。"""
    insts, _ = asyncio.run(sy.resolve("LAB"))

    async def fake_px(inst):
        return 1.0 if inst.exchange == "Bybit" else None
    monkeypatch.setattr(sy, "_last_price", fake_px)
    warn, _ = asyncio.run(sy.divergence_check(insts))
    assert warn is None
