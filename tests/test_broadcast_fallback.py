"""每日播报的行情源降级 —— 一个源限流不该让整条播报变成五个字。

同时锁死：两个源都挂时必须把**各自的原因**写出来，别再只给一句"获取失败"。
"""
import asyncio

import pytest

from handlers import broadcast as bc


def _run():
    return asyncio.run(bc.build_broadcast_text())


@pytest.fixture
def cg(monkeypatch):
    """替换 CoinGecko 源。传 dict 就返回它，传 Exception 就抛。"""
    def setup(val):
        async def fake(_syms):
            if isinstance(val, Exception):
                raise val
            return val
        monkeypatch.setattr(bc, "get_prices", fake)
    return setup


@pytest.fixture
def bybit(monkeypatch):
    """替换 Bybit 备用源（broadcast 里是函数内 import，所以打桩打在源模块上）。"""
    def setup(val):
        async def fake(_syms):
            if isinstance(val, Exception):
                raise val
            return val
        from handlers import marketdata
        monkeypatch.setattr(marketdata, "simple_prices", fake)
    return setup


_OK = {"BTC": {"usd": 64895.0, "change": 0.42},
       "ETH": {"usd": 1914.58, "change": -1.2}}


def test_normal_path_uses_coingecko(cg):
    cg(_OK)
    txt = _run()
    assert "BTC: $64,895" in txt and "ETH" in txt
    assert "行情源" not in txt          # 没降级就不该多这句噪音


def test_falls_back_to_bybit_when_coingecko_raises(cg, bybit):
    """限流时收到的应该是行情，不是故障。"""
    cg(RuntimeError("429 Too Many Requests"))
    bybit(_OK)
    txt = _run()
    assert "BTC: $64,895" in txt
    assert "行情源：Bybit" in txt and "CoinGecko 暂不可用" in txt


def test_falls_back_when_coingecko_returns_empty(cg, bybit):
    """不抛异常但返回空，同样等于没数据。"""
    cg({})
    bybit(_OK)
    assert "行情源：Bybit" in _run()


def test_partial_coingecko_result_does_not_trigger_fallback(cg, bybit):
    """部分币缺失是正常的（CoinGecko 没收录某些币），不该为此换源。"""
    cg({"BTC": {"usd": 1.0, "change": 0.0}})
    bybit(RuntimeError("不该被调到"))
    txt = _run()
    assert "BTC" in txt and "行情源" not in txt


def test_both_sources_down_reports_both_reasons(cg, bybit):
    """这是修的核心：以前只有"行情获取失败"五个字，排查只能靠猜。"""
    cg(RuntimeError("429 Too Many Requests"))
    bybit(RuntimeError("bybit timeout"))
    txt = _run()
    assert "429" in txt and "bybit timeout" in txt
    assert "CoinGecko" in txt and "Bybit" in txt


def test_both_sources_empty_is_also_explained(cg, bybit):
    cg({})
    bybit({})
    txt = _run()
    assert "两个源都返回空" in txt
