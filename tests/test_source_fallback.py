"""CoinGecko 兜底 + 健康检查降噪。

背景：CoinGecko 是这个项目最脆的一环（免费额度被十几个定时任务共用）。
它挂掉的后果不是报错，是**静默失效**——价格预警不响、持仓不更新，
用户会以为「价格没到」而不是「系统没查到」。这类错觉比报错危险得多。
"""
import asyncio

import pytest

import api
from handlers import monitor


@pytest.fixture(autouse=True)
def _reset():
    api._LAST_FALLBACK[0] = 0.0
    monitor._fails.clear()
    monitor._health.update({k: True for k in monitor._health})
    yield


def _boom(*_a, **_k):
    raise RuntimeError("CoinGecko 挂了")


# ── 取价兜底 ─────────────────────────────────────────────────────
def test_single_price_falls_back_to_bybit(monkeypatch):
    monkeypatch.setattr(api, "_get", _boom)

    async def fake(symbols):
        return {s: {"usd": 100.0, "change": 1.0} for s in symbols}
    from handlers import marketdata
    monkeypatch.setattr(marketdata, "simple_prices", fake)

    r = asyncio.run(api.get_price("BTC"))
    assert r and r["price"] == 100.0


def test_batch_fills_only_the_missing_ones(monkeypatch):
    """CoinGecko 只漏了一部分时，别把已拿到的也覆盖掉。"""
    async def half(_path, _params):
        return {"bitcoin": {"usd": 64000, "usd_24h_change": 1.0}}
    monkeypatch.setattr(api, "_get", half)
    called = {}

    async def fake(symbols):
        called["syms"] = list(symbols)
        return {s: {"usd": 7.0, "change": 0.0} for s in symbols}
    from handlers import marketdata
    monkeypatch.setattr(marketdata, "simple_prices", fake)

    r = asyncio.run(api.get_prices(["BTC", "ETH"]))
    assert r["BTC"]["price"] == 64000       # CoinGecko 的保留
    assert r["ETH"]["price"] == 7.0         # 缺的才补
    assert called["syms"] == ["ETH"]        # 只为缺的那个再请求


def test_coin_without_coingecko_mapping_still_resolves(monkeypatch):
    """小币没有 COIN_IDS 映射，以前直接返回 None——现在该走 Bybit。"""
    async def fake(symbols):
        return {s: {"usd": 0.004, "change": -3.0} for s in symbols}
    from handlers import marketdata
    monkeypatch.setattr(marketdata, "simple_prices", fake)
    r = asyncio.run(api.get_price("AKE"))
    assert r and r["price"] == 0.004


def test_non_usd_quote_is_not_faked(monkeypatch):
    """Bybit 只有 USDT 计价。人民币这类给不了就如实返回空，不许拿美元冒充。"""
    monkeypatch.setattr(api, "_get", _boom)
    assert asyncio.run(api.get_price("BTC", "cny")) is None


def test_fallback_failure_does_not_raise(monkeypatch):
    monkeypatch.setattr(api, "_get", _boom)

    async def also_boom(_s):
        raise RuntimeError("Bybit 也挂了")
    from handlers import marketdata
    monkeypatch.setattr(marketdata, "simple_prices", also_boom)
    assert asyncio.run(api.get_price("BTC")) is None


def test_fallback_usage_is_recorded(monkeypatch):
    """健康检查要靠它区分「源挂了但功能还在」。"""
    monkeypatch.setattr(api, "_get", _boom)

    async def fake(symbols):
        return {s: {"usd": 1.0, "change": 0.0} for s in symbols}
    from handlers import marketdata
    monkeypatch.setattr(marketdata, "simple_prices", fake)
    assert not api.fallback_recent()
    asyncio.run(api.get_price("BTC"))
    assert api.fallback_recent()


# ── 健康检查降噪 ─────────────────────────────────────────────────
class _Resp:
    def __init__(self, code):
        self.status_code = code


def _client_returning(code):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, _url):
            if code is None:
                raise RuntimeError("连不上")
            return _Resp(code)
    return _C


def _run_check(monkeypatch, code, times=1):
    sent = []

    async def fake_notify(_ctx, text):
        sent.append(text)
    monkeypatch.setattr(monitor, "notify_admin", fake_notify)
    monkeypatch.setattr(monitor.httpx, "AsyncClient", _client_returning(code))
    for _ in range(times):
        asyncio.run(monitor._check_source(None, "coingecko", "u", "CoinGecko行情源"))
    return sent


def test_one_timeout_does_not_alert(monkeypatch):
    """单次 10 秒超时就报警的话，误报会让人开始忽略告警——那比不报还糟。"""
    assert _run_check(monkeypatch, None, times=1) == []


def test_two_failures_still_quiet(monkeypatch):
    assert _run_check(monkeypatch, None, times=2) == []


def test_third_consecutive_failure_alerts(monkeypatch):
    sent = _run_check(monkeypatch, None, times=3)
    assert len(sent) == 1 and "连续 3 次" in sent[0]


def test_alert_names_the_affected_features(monkeypatch):
    """「部分功能可能受影响」等于没说——要点名。"""
    sent = _run_check(monkeypatch, None, times=3)
    assert "涨跌榜" in sent[0] and "价格预警" in sent[0]


def test_rate_limit_is_not_treated_as_outage(monkeypatch):
    """429 是我们自己调太快，不是源挂了。算成故障会天天误报且指错方向。"""
    assert _run_check(monkeypatch, 429, times=5) == []
    assert monitor._health["coingecko"] is True


def test_alert_mentions_fallback_is_working(monkeypatch):
    api._LAST_FALLBACK[0] = __import__("time").time()
    sent = _run_check(monkeypatch, None, times=3)
    assert "Bybit 兜底源" in sent[0]


def test_recovery_notice_after_alert(monkeypatch):
    _run_check(monkeypatch, None, times=3)
    sent = _run_check(monkeypatch, 200, times=1)
    assert sent and "恢复" in sent[0]


def test_no_duplicate_alerts_while_down(monkeypatch):
    sent = _run_check(monkeypatch, None, times=8)
    assert len(sent) == 1
