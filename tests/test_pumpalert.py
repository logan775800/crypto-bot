"""Bybit 15m 急涨急跌告警测试（纯本地，不联网）。

锁死三类最容易回归的行为：
  1. 滚动窗口涨跌幅算得准，历史不足 15 分钟绝不误报；
  2. 成交额过滤在 _fetch 里真的生效（微盘不进流程）；
  3. 去重/升级/重新武装：不刷屏，但升档要报、回落后能再报。
"""
import json
import pytest

from handlers import pumpalert as P


@pytest.fixture(autouse=True)
def _clean():
    P._price_hist.clear()
    yield
    P._price_hist.clear()


# ── 滚动窗口算法 ────────────────────────────────────────────────
def test_change_vs_15m_ago():
    now = 100000.0
    hist = [[now - 900, 1.00], [now - 300, 1.10], [now, 1.15]]
    assert round(P._rolling_change(hist, now), 2) == 15.00


def test_change_down():
    now = 100000.0
    assert round(P._rolling_change([[now - 900, 2.0], [now, 1.6]], now), 2) == -20.00


def test_reference_is_newest_snapshot_at_least_window_old():
    """基准必须取『≥15分钟前』里最新的那条，不能用更晚(窗口内)的点。"""
    now = 100000.0
    hist = [[now - 1000, 0.90], [now - 905, 0.95], [now - 899, 1.00], [now, 1.20]]
    # ref = now-905 的 0.95（now-899 已在窗口内，不算）
    assert round(P._rolling_change(hist, now), 1) == 26.3


def test_insufficient_history_returns_none():
    now = 100000.0
    assert P._rolling_change([[now - 300, 1.0], [now, 1.3]], now) is None


def test_empty_history():
    assert P._rolling_change([], 100000.0) is None


def test_zero_reference_price_safe():
    now = 100000.0
    assert P._rolling_change([[now - 900, 0.0], [now, 1.0]], now) is None


# ── 成交额过滤（在 _fetch 内）───────────────────────────────────
def _fake_client(tickers):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"retCode": 0, "result": {"list": tickers}}

    class _C:
        async def get(self, url, params=None):
            return _R()

    return _C()


# 仓库没装 pytest-asyncio，协程测试统一用 asyncio.run 驱动
def test_fetch_filters_low_turnover_and_non_usdt():
    import asyncio
    tickers = [
        {"symbol": "BTCUSDT", "lastPrice": "95000", "turnover24h": "9000000000"},
        {"symbol": "SMALLUSDT", "lastPrice": "1.0", "turnover24h": "100000"},   # 微盘，滤
        {"symbol": "ETHUSDC", "lastPrice": "3000", "turnover24h": "9999999999"},  # 非USDT，滤
        {"symbol": "OKUSDT", "lastPrice": "0", "turnover24h": "9999999999"},     # 价0，滤
    ]
    out = asyncio.run(P._fetch_bybit_perps(_fake_client(tickers)))
    assert {m["sym"] for m in out} == {"BTC"}


# ── ingest 裁剪 & 淘汰 ──────────────────────────────────────────
def test_ingest_prunes_old_samples():
    now = 100000.0
    P._price_hist["X"] = [[now - 5000, 1.0]]   # 远超窗口的老点
    P._ingest([{"sym": "X", "price": 1.1, "turnover": 5e6}], now)
    # 老点应被裁掉，只留窗口+余量内的
    assert all(ts >= now - P.WINDOW - P.HIST_MARGIN for ts, _ in P._price_hist["X"])


def test_ingest_evicts_delisted_symbols():
    now = 100000.0
    P._price_hist["GONE"] = [[now - P.WINDOW - P.HIST_MARGIN - 100, 1.0]]
    P._ingest([{"sym": "LIVE", "price": 1.0, "turnover": 5e6}], now)
    assert "GONE" not in P._price_hist and "LIVE" in P._price_hist


# ── 去重 / 升级 / 重新武装 ───────────────────────────────────────
def test_should_alert_first_then_dedup():
    recs, now = {}, 100000.0
    assert P._should_alert(recs, "A", "up", 16.0, now) is True    # 首次
    assert P._should_alert(recs, "A", "up", 17.0, now) is False   # 未到升级步长
    assert P._should_alert(recs, "A", "up", 26.0, now) is True    # +10 升级重报


def test_should_alert_opposite_direction_independent():
    recs, now = {}, 100000.0
    assert P._should_alert(recs, "A", "up", 16.0, now) is True
    assert P._should_alert(recs, "A", "down", 16.0, now) is True  # 反向独立计


def test_state_expiry_allows_realert():
    recs, now = {}, 100000.0
    assert P._should_alert(recs, "A", "up", 16.0, now) is True
    later = now + P.STATE_TTL + 1
    assert P._should_alert(recs, "A", "up", 16.0, later) is True  # 过期后重新算首次


# ── 端到端：一轮扫描 ────────────────────────────────────────────
def test_scan_end_to_end(monkeypatch):
    import asyncio
    from storage import data

    monkeypatch.setattr("storage.save_data", lambda: None)
    P._price_hist.clear()
    data["pump_watch"] = {"555": {"pct": 15.0}}
    data["pump_alerted"] = {}
    data.setdefault("user_prefs", {})

    now = [1_000_000.0]
    monkeypatch.setattr(P.time, "time", lambda: now[0])

    # 15 分钟从 1.00 拉到 1.20
    def plan(t):
        minute = int((t - 1_000_000.0) // 60)
        return 1.00 + 0.20 * min(minute, 15) / 15.0

    async def fake_fetch(client=None):
        return [{"sym": "BANK", "price": plan(now[0]), "turnover": 5e7},
                {"sym": "BTC", "price": 95000.0, "turnover": 9e9}]

    monkeypatch.setattr(P, "_fetch_bybit_perps", fake_fetch)

    sent = []

    class Bot:
        async def send_message(self, chat_id, text, **kw):
            sent.append(text)

    class Ctx:
        bot = Bot()

    fired = []
    for minute in range(17):
        now[0] = 1_000_000.0 + minute * 60
        sent.clear()
        asyncio.run(P.scan_pump(Ctx()))
        if sent:
            fired.append(minute)

    # 攒够 15 分钟历史后、BANK 达 +20% 才首报；此前不误报
    assert fired and fired[0] >= 15
    assert any("BANK" in t and "🚀" in t for t in sent) or fired
    assert "BANK" in P._price_hist


def test_scan_respects_quiet_hours(monkeypatch):
    import asyncio
    from storage import data
    monkeypatch.setattr("storage.save_data", lambda: None)
    P._price_hist.clear()
    data["pump_watch"] = {"555": {"pct": 15.0}}
    data["pump_alerted"] = {}
    # 全天静音
    data["user_prefs"] = {"555": {"quiet": ["00:00", "23:59"]}}

    now = [1_000_000.0]
    monkeypatch.setattr(P.time, "time", lambda: now[0])

    async def fake_fetch(client=None):
        minute = int((now[0] - 1_000_000.0) // 60)
        return [{"sym": "BANK", "price": 1.0 + 0.02 * min(minute, 15), "turnover": 5e7}]

    monkeypatch.setattr(P, "_fetch_bybit_perps", fake_fetch)
    sent = []

    class Ctx:
        class bot:
            @staticmethod
            async def send_message(chat_id, text, **kw):
                sent.append(text)

    for minute in range(17):
        now[0] = 1_000_000.0 + minute * 60
        asyncio.run(P.scan_pump(Ctx()))
    assert not sent, "静音时段不该推送"


def test_no_subscribers_skips_fetch(monkeypatch):
    import asyncio
    from storage import data
    data["pump_watch"] = {}
    called = {"n": 0}

    async def fake_fetch(client=None):
        called["n"] += 1
        return []

    monkeypatch.setattr(P, "_fetch_bybit_perps", fake_fetch)

    class Ctx:
        bot = None

    asyncio.run(P.scan_pump(Ctx()))
    assert called["n"] == 0, "没人订阅就不该拉行情（省 API）"
