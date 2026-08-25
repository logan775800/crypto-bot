"""更新日志：启动播报要说清这一版改了什么。

这里最有价值的一条是 test_current_version_has_notes —— 它把「写更新说明」
变成**部署门槛**：pytest 跑在部署流水线里，忘了写就发不出去。
靠自觉记着写，迟早会有一版是空的，而那一版恰恰是用户最想知道改了什么的。
"""
import pytest

from config import VERSION
from handlers import changelog as C


def test_current_version_has_notes():
    """发版必须写更新说明——忘了写，这条会先炸，部署直接中止。"""
    items = C.notes_for(VERSION)
    assert items, (
        f"CHANGELOG.md 里没有 {VERSION} 这一段。\n"
        f"在文件最上面加：\n\n## {VERSION}　(YYYY-MM-DD)\n- 这版改了什么\n")
    assert all(it.strip() for it in items), "有空条目"


def test_notes_are_in_human_language():
    """写给使用者看的，不是提交标题的复制粘贴。"""
    for it in C.notes_for(VERSION):
        assert not it.startswith(("feat:", "fix:", "chore:", "refactor:")), \
            f"别把提交前缀原样搬过来：{it}"
        assert len(it) <= 200, f"太长了，说不清就分两条：{it[:50]}…"


def _key(v):
    return tuple(int(x) for x in v.lstrip("v").split("."))


def test_file_is_ordered_newest_first():
    vers = [v for v, _d, _i in C.load()]
    assert vers == sorted(vers, key=_key, reverse=True), "版本顺序乱了"


def test_no_duplicate_versions():
    vers = [v for v, _d, _i in C.load()]
    assert len(vers) == len(set(vers)), "有重复版本段"


def test_history_is_complete_enough():
    """历史是从 git tag 生成的，别哪次清理把它删空了。"""
    assert len(C.load()) > 100


# ── 解析 ─────────────────────────────────────────────────────────
SAMPLE = """# 更新日志

开头这段说明不该被当成条目。

## v9.9.9　(2026-01-02)
- 第一条
- 第二条

## v9.9.8
* 星号也算条目
不是条目的普通行
"""


def test_parse_picks_up_versions_items_and_dates():
    got = C._parse(SAMPLE)
    assert [v for v, _d, _i in got] == ["v9.9.9", "v9.9.8"]
    assert got[0][1] == "2026-01-02"
    assert got[0][2] == ["第一条", "第二条"]
    assert got[1][2] == ["星号也算条目"]


def test_parse_ignores_the_preamble():
    assert all("开头" not in it for _v, _d, items in C._parse(SAMPLE) for it in items)


def test_missing_date_is_fine():
    assert C._parse(SAMPLE)[1][1] == ""


# ── 启动播报 ─────────────────────────────────────────────────────
def test_startup_text_carries_the_notes():
    text = C.startup_text(VERSION)
    assert VERSION in text
    assert "本次更新" in text
    assert C.notes_for(VERSION)[0][:20] in text


def test_startup_text_survives_a_version_with_no_notes():
    """日志缺失也必须照常播报——不能因为没写说明就静默不报启动。"""
    text = C.startup_text("v0.0.1")
    assert "已启动" in text and "所有功能已加载" in text


def test_startup_text_caps_the_length(monkeypatch):
    """一版改了三十条也不能刷屏。"""
    monkeypatch.setattr(C, "notes_for", lambda _v: [f"第{i}条" for i in range(30)])
    text = C.startup_text("vX")
    assert text.count("•") <= 8
    assert "/changelog" in text


def test_render_says_so_when_a_version_has_nothing():
    assert "没写更新说明" in C.render("v0.0.1")


# ── 入口 ─────────────────────────────────────────────────────────
def test_button_is_routed():
    import inspect
    from handlers import menu
    assert 'startswith("cl:")' in inspect.getsource(menu._dispatch)


def test_help_panel_links_to_it():
    """新功能要有按钮入口，光有命令不算做完。"""
    import inspect
    from handlers import menu
    src = inspect.getsource(menu._dispatch)
    assert "cl:cur" in src


def test_command_is_registered():
    import inspect
    import bot
    assert 'CommandHandler("changelog"' in inspect.getsource(bot.main)


def test_startup_notify_uses_it():
    import inspect
    from handlers import monitor
    assert "startup_text" in inspect.getsource(monitor.startup_notify)


# ── 多行条目（这个 bug 静默了很多版）──────────────────────────
# CHANGELOG.md 里为了不超行宽，长条目都是折行写的。解析器原来**只收
# `- ` 开头的行**，缩进的续行被静默丢掉——于是播报出去的每条都断在
# 第一行末尾的半句话上：
#     「后台任务心跳巡检：告警任务挂掉的表现是「什么都没发生」，」
# 他截图问「你做的这些怎么用都不知道」，一半原因就是说明根本没发完整。

def test_multi_line_entries_are_joined():
    text = ("## v9.9.9　(2026-01-01)\n"
            "- 第一条前半句，\n"
            "  第一条后半句\n"
            "- 第二条\n")
    items = C._parse(text)[0][2]
    assert items == ["第一条前半句，第一条后半句", "第二条"], \
        "缩进续行没被拼回来，播报会断在半句话上"


def test_continuation_does_not_leak_into_the_next_version():
    text = ("## v9.9.9　(2026-01-01)\n"
            "- 甲\n"
            "## v9.9.8　(2026-01-01)\n"
            "- 乙\n")
    parsed = {v: items for v, _d, items in C._parse(text)}
    assert parsed["v9.9.9"] == ["甲"]
    assert parsed["v9.9.8"] == ["乙"]


def test_prose_before_the_first_version_is_ignored():
    """文件开头那几行说明文字不能被当成条目。"""
    text = ("# 更新日志\n\n每次发版在最上面加一段。\n\n"
            "## v9.9.9　(2026-01-01)\n- 甲\n")
    assert C._parse(text)[0][2] == ["甲"]


# ── 播报要短、而且要说去哪看怎么用 ────────────────────────────
def test_broadcast_gives_one_line_per_item():
    """CHANGELOG 条目是写给仓库看的（判据、取舍）。群里的人只要一句
    「变了什么」——把整段原文播出去，他看完的原话是「怎么用都不知道」。"""
    for it in C.notes_for(VERSION):
        assert len(C.brief(it)) <= 62, f"播报这条还是太长：{C.brief(it)}"


def test_brief_never_cuts_mid_word_markdown():
    """截断后残留半个 ** 会把整段格式带歪。"""
    assert "**" not in C.brief("**加粗的开头**：后面还有很长很长很长的解释文字")


def test_broadcast_tells_you_where_the_how_to_is():
    """**这是他反复卡住的那一点**：知道变了什么，不知道怎么用。
    播报必须指向 /howto，否则更新说明本身就是个死胡同。"""
    out = C.update_text(VERSION)
    assert "/howto" in out, "播报没告诉人去哪看怎么用"
    assert "/changelog" in out
