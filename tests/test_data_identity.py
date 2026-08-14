"""数据身份与状态分类：机器人自检报告里提出的几条，逐条钉住。

最要命的一条是**交易所身份混用**：K线/OI/盘口/逐笔/资金费全走 Bybit，
而 get_contract 以前按 okx→binance→bybit 取第一个成功的。于是同一轮分析里
合约价来自 OKX、指标来自 Bybit——山寨币两个所价格能差出一截，资金费率各算各的，
模型却当成同一个市场往下推。结论看起来一样可信，这才是危险的地方。
"""
import asyncio
import types

import pytest

from handlers import chat
from handlers.manifest import Manifest


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 交易所身份
def test_contract_prefers_bybit(monkeypatch):
    """Bybit 能取到就必须用 Bybit，别的所连试都不该试。"""
    tried = []

    async def by(sym):
        tried.append("bybit")
        return "BTC 永续 $60000 费率+0.01%"

    async def okx(sym):
        tried.append("okx")
        return "不该被调用"

    monkeypatch.setattr("handlers.bybit.build_fprice_text_by", by)
    monkeypatch.setattr("handlers.okx.build_fprice_text", okx)
    out = _run(chat._contract_overview("BTC"))
    assert tried == ["bybit"]
    assert out.startswith("[Bybit 永续]")
    assert "60000" in out


def test_fallback_to_other_exchange_is_loudly_labeled(monkeypatch):
    """回退到别的所必须明说——悄悄回退比取不到更危险。"""
    async def by(sym):
        return "Bybit未找到 XYZ 永续合约"

    async def okx(sym):
        return "XYZ 永续 $1.23"

    monkeypatch.setattr("handlers.bybit.build_fprice_text_by", by)
    monkeypatch.setattr("handlers.okx.build_fprice_text", okx)
    out = _run(chat._contract_overview("XYZ"))
    assert "不是 Bybit" in out
    assert "以 Bybit" in out, "要写明具体价位以哪边为准"
    assert "1.23" in out


def test_bybit_exception_still_falls_back(monkeypatch):
    async def by(sym):
        raise RuntimeError("网络炸了")

    async def okx(sym):
        return "XYZ 永续 $9"

    monkeypatch.setattr("handlers.bybit.build_fprice_text_by", by)
    monkeypatch.setattr("handlers.okx.build_fprice_text", okx)
    assert "不是 Bybit" in _run(chat._contract_overview("XYZ"))


def test_all_exchanges_missing(monkeypatch):
    async def missing(sym):
        return f"未找到 {sym} 永续合约"

    monkeypatch.setattr("handlers.bybit.build_fprice_text_by", missing)
    monkeypatch.setattr("handlers.okx.build_fprice_text", missing)
    monkeypatch.setattr("handlers.binance.build_fprice_text_bn", missing)
    assert "三个所都查不到" in _run(chat._contract_overview("NOPE"))


# ---------------------------------------------------------------- 状态分类
def _mf(*calls):
    mf = Manifest("BTCUSDT")
    for name, ok in calls:
        mf.record(name, {}, "结果" if ok else "⚠️ 取不到")
    return mf


def test_header_separates_market_from_account():
    """「19/21 项可用」把两件性质不同的事混成一个数：
    市场数据缺了是结论没地基，账户数据缺了是仓位不能按真实权益算。"""
    mf = _mf(("get_orderbook", True), ("get_my_account", True))
    h = mf.header()
    assert "`市场数据`" in h and "订单簿" in h
    assert "`账户数据`" in h and "真实账户" in h


def test_header_says_what_missing_account_costs_you():
    mf = _mf(("get_orderbook", True))
    h = mf.header()
    assert "账户数据" in h and "只能给公式" in h


def test_header_lists_missing_dimensions():
    mf = _mf(("get_liquidations", False))
    assert "缺失维度" in mf.header() and "清算数据" in mf.header()


def test_header_empty_when_nothing_called():
    assert Manifest().header() == ""


# ---------------------------------------------------------------- 取数跨度
def test_spread_warns_when_data_is_not_one_snapshot(monkeypatch):
    """盘口几秒就变样。跨度大的时候拿它和几分钟前的 K 线互相印证会出错。"""
    mf = _mf(("get_klines", True), ("get_orderbook", True))
    mf.times = [1000.0, 1000.0 + mf.SPREAD_WARN + 5]
    assert "取数跨度" in mf.header()
    assert "非同一时点" in mf.header()
    assert "不是同一时点的快照" in mf.ledger()


def test_no_spread_warning_when_fast():
    mf = _mf(("get_klines", True), ("get_orderbook", True))
    mf.times = [1000.0, 1002.0]
    assert "取数跨度" not in mf.header()


def test_spread_needs_two_calls():
    mf = _mf(("get_klines", True))
    assert mf.spread() == 0.0


# ---------------------------------------------------------------- 信号冲突
def test_book_and_trades_together_force_conflict_rule():
    """挂单是「打算成交」，逐笔是「已经成交」，两者常打架。
    挑一边讲成单边结论是这套数据最容易出的错。"""
    mf = _mf(("get_orderbook", True), ("get_recent_trades", True))
    assert "信号冲突" in mf.ledger()


def test_conflict_rule_absent_when_only_one_side():
    mf = _mf(("get_orderbook", True))
    assert "信号冲突" not in mf.ledger()
