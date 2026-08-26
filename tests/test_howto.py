"""`/howto` —— 群里发一下就把使用指南贴出来。

他要的：「把这个发送到群里 让他们都知道怎么操作」。
指南发过一次就沉进聊天记录了，新进群的人看不到，所以给它一条命令。

两条护栏：
  1. **群里不能出现暴露个人账户的按钮**——按钮回调是就地编辑原消息，
     在群里点「虚拟交易台」等于把自己的持仓贴给全群看；
  2. **正文要短**——正文一长，底下的按钮就被挤出屏幕（v1.36 那次的教训）。
"""
import pytest

from handlers import howto as H


def test_guide_link_is_no_longer_pushed_to_the_group():
    """**判据反过来了，别当成护栏被人偷偷拆了。**

    2026-08-24 这条原本锁的是「正文里必须带完整链接，群友要能直接点」。
    2026-08-25 他明确要求去掉，理由也站得住：完整清单现在就在**群置顶**里
    （`/pinhowto` 维护，68 个命令 + 按钮路径），再挂一个 GitHub 外链等于
    让人跳出去看一份重复的东西，而且那是个公开仓库地址。

    `docs/guide.md` 本身还留着（跟代码一起版本管理），只是不往群里甩了。
    """
    assert H.GUIDE_URL.startswith("https://github.com/logan775800/crypto-bot")
    assert H.GUIDE_URL not in H.TEXT, "他要求群里不再出现这个外链"
    assert "置顶" in H.TEXT, "去掉链接之后，得告诉人完整清单去哪看"


def test_text_is_short_enough_for_the_buttons_to_be_reachable():
    assert len(H.TEXT.splitlines()) <= 18, "正文太长，按钮会被挤出屏幕"


def test_text_lists_the_entry_points_people_actually_need():
    for cmd in ("/menu", "/commands", "/rank", "/lsr", "/liqmap", "/vtrade", "/oc"):
        assert cmd in H.TEXT, f"指南里少了 {cmd}"
    assert "直接发币名" in H.TEXT, "最快上手那条不能少"


def test_liqmap_is_still_labelled_an_estimate():
    """这条消息会被转发出去，"估算"两个字不能在这里被弄丢。"""
    assert "估算" in H.TEXT and "不是交易所数据" in H.TEXT


def test_no_investment_advice_line():
    assert "不构成投资建议" in H.TEXT


# ── 群里不能暴露个人账户 ─────────────────────────────────────
def test_group_keyboard_has_no_personal_account_entry():
    """按钮是就地编辑原消息：群里点「虚拟交易台」= 把自己的持仓贴给全群。"""
    cbs = [b.callback_data for r in H.kb(private=False).inline_keyboard for b in r]
    assert "vg:home" not in cbs
    for c in cbs:
        assert not c.startswith(("t", "vg:", "rs")), f"{c} 涉及个人账户，不该出现在群里"


def test_private_keyboard_does_offer_the_trading_desk():
    cbs = [b.callback_data for r in H.kb(private=True).inline_keyboard for b in r]
    assert "vg:home" in cbs


def test_market_entries_are_in_both():
    for priv in (True, False):
        cbs = [b.callback_data for r in H.kb(priv).inline_keyboard for b in r]
        assert "dr:w:3:all:all:hot" in cbs
        assert "ls:v:binance" in cbs
        assert "lq:pick:-:-" in cbs
        assert "menu_main" in cbs


def test_every_button_target_is_handled():
    """按钮指向的回调必须真有人接——点了没反应比没按钮更糟。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    for r in H.kb(private=True).inline_keyboard:
        for b in r:
            c = b.callback_data
            prefix = c.split(":")[0]
            assert (f'd.startswith("{prefix}:")' in src
                    or f'd == "{c}"' in src
                    or f'"{c}"' in src), f"{c} 没人接"


# ── 入口 ────────────────────────────────────────────────────
def test_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("howto"' in src and 'BotCommand("howto"' in src


def test_reachable_from_the_help_panel():
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    seg = src.split('elif d == "cat_help":')[1].split("elif d ==")[0]
    assert '"howto"' in seg


def test_command_is_categorised_in_the_panel():
    from handlers import cmdpanel
    assert cmdpanel.MODULE_CN.get("handlers.howto")


def test_link_preview_is_disabled():
    """一张 GitHub 大卡片会把消息撑得老长，按钮又被挤下去。"""
    import inspect
    assert "disable_web_page_preview" in inspect.getsource(H.howto)


# ── docs/guide.md 本身 ──────────────────────────────────────
# `/howto` 把群友引到这份文件上，所以它写错 = 一群人照着抄错的命令。
# 2026-08-25 复盘发现它建好之后一次没更新过：`/pump3`、`/relay` 和整个
# v1.43~v1.46 都是在它之后上的，而**没有任何测试看着它**。
# 下面两条不要求"每个命令都写进指南"（178 个命令，指南只讲怎么上手，
# 全清单归 /help 和 /commands），只要求**指南里出现的命令必须真的存在**。

def _guide():
    import pathlib
    return (pathlib.Path(__file__).resolve().parent.parent
            / "docs" / "guide.md").read_text(encoding="utf-8")


def _registered():
    import pathlib
    import re
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    return set(re.findall(r'CommandHandler\("([a-z0-9_]+)"', src))


def test_every_command_in_the_guide_actually_exists():
    """指南里给的命令必须能打得通。

    照着文档抄一条不存在的命令，机器人一声不吭——这比文档缺一节严重得多，
    因为人会以为是自己打错了。
    """
    import re
    cmds = set(re.findall(r'(?<![\w/])/([a-z0-9_]{2,})', _guide()))
    known = _registered()
    unknown = sorted(c for c in cmds if c not in known)
    assert not unknown, f"指南里这些命令并不存在：{unknown}"


def test_the_alert_chapter_is_present():
    """告警是这几版更新的主线，也是唯一"我主动推给你"的一类功能。
    指南长期没有这一章，等于订阅了什么、怎么取消都没地方查。"""
    g = _guide()
    for cmd in ("/watchpump", "/watchcontract", "/pump3", "/watchmarket",
                "/pumptop", "/alertnow"):
        assert cmd in g, f"指南里少了 {cmd}"


def test_coverage_claims_match_the_code():
    """覆盖范围写在脸上是这个项目的规矩，写错的口径比不写更糟。"""
    g = _guide()
    assert "并集" in g, "急涨急跌是币安+Bybit 取并集，指南要说清扫的是哪些盘子"
    assert "~500" not in g and "约 500 个" not in g
