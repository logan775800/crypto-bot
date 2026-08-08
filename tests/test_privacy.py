"""账户数据脱敏 —— AI 走第三方中转站，别把「你有多少钱」发出去。

最容易做成自欺欺人的地方：给了「名义占权益 40%」又给了数量，
数量×价格÷40% 就把权益反推出来了。所以下面专门验"数量不能同时出现"。
"""
import asyncio

import pytest

from handlers import privacy
from storage import data


@pytest.fixture(autouse=True)
def _reset():
    data.pop("ai_redact_account", None)
    yield
    data.pop("ai_redact_account", None)


def test_default_is_on():
    """外发数据这件事，安全的那一侧才是合理默认。"""
    assert privacy.enabled()


def test_toggle():
    privacy.set_enabled(False)
    assert not privacy.enabled()
    privacy.set_enabled(True)
    assert privacy.enabled()


def test_pct_never_falls_back_to_absolute():
    """权益拿不到时返回 '?'，绝不能退化成把绝对值发出去。"""
    assert privacy.pct(500, 0) == "?"
    assert privacy.pct(500, None) == "?"
    assert "500" not in privacy.pct(500, 0)


def test_money_switches_with_the_flag():
    privacy.set_enabled(True)
    assert privacy.money(500, 10000) == "5.0%权益"
    privacy.set_enabled(False)
    assert "500" in privacy.money(500, 10000)


def test_note_only_when_redacting():
    privacy.set_enabled(True)
    assert "脱敏" in privacy.note()
    privacy.set_enabled(False)
    assert privacy.note() == ""


# ── 账户快照 ─────────────────────────────────────────────────────
class _FakeClient:
    async def wallet_balance(self, coin="USDT"):
        return {"totalEquity": "50000", "totalAvailableBalance": "30000",
                "totalPerpUPL": "1234.5"}

    async def positions_all(self):
        return [{"symbol": "BTCUSDT", "side": "Buy", "leverage": "10",
                 "size": "0.5", "avgPrice": "64000", "markPrice": "65000",
                 "positionValue": "32500", "unrealisedPnl": "500",
                 "liqPrice": "58000"}]


@pytest.fixture
def fake_client(monkeypatch):
    from handlers import rtrade
    monkeypatch.setattr(rtrade, "_client", lambda: _FakeClient())


def _snapshot():
    from handlers.chat import _account_snapshot
    return asyncio.run(_account_snapshot())


def test_redacted_snapshot_hides_equity(fake_client):
    privacy.set_enabled(True)
    txt = _snapshot()
    assert "50000" not in txt and "50,000" not in txt
    assert "30000" not in txt and "30,000" not in txt


def test_redacted_snapshot_hides_quantity(fake_client):
    """给了百分比就不能再给数量——否则 数量×价格÷百分比 直接反推出权益。"""
    privacy.set_enabled(True)
    txt = _snapshot()
    assert "数量" not in txt
    assert "0.5" not in txt


def test_redacted_snapshot_keeps_prices(fake_client):
    """价格是公开行情，藏了反而没法分析。"""
    privacy.set_enabled(True)
    txt = _snapshot()
    for public in ("64,000", "65,000", "58,000"):
        assert public in txt or public.replace(",", "") in txt


def test_redacted_snapshot_keeps_ratios_useful(fake_client):
    """脱敏不能把分析价值一起脱掉：名义占比、浮盈占比要还在。"""
    privacy.set_enabled(True)
    txt = _snapshot()
    assert "65.0%权益" in txt          # 32500 / 50000
    assert "10x" in txt                # 杠杆不敏感


def test_plain_snapshot_shows_absolute(fake_client):
    privacy.set_enabled(False)
    txt = _snapshot()
    assert "50,000" in txt and "数量" in txt


def test_snapshot_tells_model_not_to_reverse_engineer(fake_client):
    privacy.set_enabled(True)
    assert "不要试图反推" in _snapshot()


# ── 成绩单换算成 R ───────────────────────────────────────────────
def _trades():
    return [{"symbol": "BTCUSDT", "side": "long", "pnl": 300, "dur": 600,
             "lev": 10, "value": 1000, "ts": 1, "entry": 1, "exit": 1, "qty": 1},
            {"symbol": "BTCUSDT", "side": "long", "pnl": -100, "dur": 600,
             "lev": 10, "value": 1000, "ts": 2, "entry": 1, "exit": 1, "qty": 1}]


def test_digest_uses_r_units_when_redacting():
    from handlers.rstats import build_ai_digest
    privacy.set_enabled(True)
    txt = build_ai_digest(_trades(), 30)
    assert "R" in txt and "USDT" not in txt.split("\n")[1]
    assert "1R = 平均亏损" in txt


def test_digest_r_math_is_right():
    """1R = 平均亏损 100，总盈亏 +200 → +2.00R。"""
    from handlers.rstats import build_ai_digest
    privacy.set_enabled(True)
    assert "+2.00R" in build_ai_digest(_trades(), 30)


def test_digest_shows_usdt_when_off():
    from handlers.rstats import build_ai_digest
    privacy.set_enabled(False)
    assert "USDT" in build_ai_digest(_trades(), 30)
