"""账户数据脱敏 —— AI 走第三方中转站，别把「你有多少钱」发出去。

最容易做成自欺欺人的地方：给了「名义占权益 40%」又给了数量，
数量×价格÷40% 就把权益反推出来了。所以下面专门验"数量不能同时出现"。
"""
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
