"""AI 输出渲染、限流、多管理员——都出过线上问题。"""
import types
import config
from handlers import chat, rtrade


def test_md_to_tg_converts_github_markdown():
    """AI 输出 GitHub 风 markdown，Telegram 不认 ## 和 **，必须转，否则原样露出来（踩过）。"""
    src = "## 标题\n正文 **加粗** 结束\n### 小标题"
    out = chat._md_to_tg(src)
    assert "##" not in out and "**" not in out
    assert "*标题*" in out and "*加粗*" in out


def test_strip_md_is_plain_fallback():
    out = chat._strip_md("## 标题\n**粗**")
    assert "#" not in out and "*" not in out and "标题" in out


def test_ai_quota_limits_normal_user_and_resets_daily(monkeypatch):
    monkeypatch.setattr(chat, "AI_DAILY_LIMIT", 2)
    monkeypatch.setattr(config, "ADMIN_IDS", {"111"})
    ctx = types.SimpleNamespace(user_data={})
    assert chat._ai_quota_ok(ctx, 42)[0] is True
    assert chat._ai_quota_ok(ctx, 42)[0] is True
    assert chat._ai_quota_ok(ctx, 42)[0] is False        # 超额拦截
    ctx.user_data["ai_quota"] = {"date": "2000-01-01", "count": 99}
    assert chat._ai_quota_ok(ctx, 42)[0] is True         # 跨天重置


def test_ai_quota_exempts_admin(monkeypatch):
    monkeypatch.setattr(chat, "AI_DAILY_LIMIT", 1)
    monkeypatch.setattr(config, "ADMIN_IDS", {"111"})
    ctx = types.SimpleNamespace(user_data={})
    for _ in range(5):
        assert chat._ai_quota_ok(ctx, 111)[0] is True    # 管理员不限


def test_is_admin_supports_multiple_ids(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", {"111", "222"})
    assert config.is_admin(111) and config.is_admin("222")
    assert not config.is_admin(999)


def test_is_admin_unrestricted_when_unset(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", set())
    assert config.is_admin(12345)                        # 未配置时不限制（方便测试）


def test_rtrade_symbol_and_kv_parsing():
    assert rtrade._norm("btc") == "BTCUSDT"
    assert rtrade._norm("ETHUSDT") == "ETHUSDT"
    assert rtrade._parse_kv(["tp=68000", "sl=60000"]) == ("68000", "60000")
    assert rtrade._parse_kv([]) == (None, None)
