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
