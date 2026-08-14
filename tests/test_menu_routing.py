"""按钮点下去必须真的有反应 —— 守「有按钮但没接线」这一类 bug。

v1.14.0 的「🌱 缓步增长」就是这么死的：菜单入口从 `do:steady` 换成了带天数的
`stdy:30:0`，`steady.on_button` 也写好了、测试也齐，唯独 `menu.button_handler`
里没加转发分支。callback 一路 elif 全部落空，而 button_handler 开头的
`query.answer()` 已经把转圈停掉了 —— 点下去既不报错也不动，最难查的那种「没反应」。

所以这里不盯某一个按钮，而是把所有静态键盘的 callback 全拉出来，逐个对照
button_handler 里**真实存在**的路由。以后谁再加按钮忘了接线，先炸在这。
"""
import asyncio
import inspect
import re

import pytest

from handlers import menu, steady


# ── 从 button_handler 源码里抠出它认识的 callback ────────────────────
def _routes():
    """返回 (精确匹配集合, 前缀集合)。

    button_handler 是一条大 if/elif 链，只有三种形态：
    `d == "x"` / `d.startswith("x")` / `d in CATS`。
    """
    src = inspect.getsource(menu._dispatch)
    # 左边必须卡死是变量 d 本身：不加边界的话 `uid in vals` 里的 "d in v" 也会中招
    b = r'(?<![\w.])d'
    exact = set(re.findall(b + r' == "([^"]+)"', src))
    prefixes = set(re.findall(b + r'\.startswith\("([^"]+)"\)', src))
    for name in re.findall(b + r' in (\w+)', src):     # 目前只有 `d in CATS`
        exact |= set(getattr(menu, name))
    return exact, prefixes


EXACT, PREFIXES = _routes()


def _routed(cb):
    return cb in EXACT or any(cb.startswith(p) for p in PREFIXES)


def _cbs(kb):
    rows = getattr(kb, "inline_keyboard", kb)
    return [b.callback_data for row in rows for b in row if b.callback_data]


def _all_keyboards():
    yield "首页", _cbs(menu.main_menu_kb())
    for name, (_text, rows) in menu.CATS.items():
        yield name, _cbs(rows)
    yield "缓步增长天数面板", _cbs(steady.days_kb())


def test_the_route_table_was_actually_parsed():
    """抠源码是脆的：哪天 button_handler 改了写法，这里要先自曝而不是全绿放行。"""
    assert "menu_main" in EXACT
    assert "cat_scan" in EXACT          # 来自 `d in CATS`
    assert "pump:" in PREFIXES
    assert len(EXACT) > 50 and len(PREFIXES) > 20


@pytest.mark.parametrize("name,cbs", list(_all_keyboards()))
def test_every_button_has_a_route(name, cbs):
    dead = [c for c in cbs if not _routed(c)]
    assert not dead, f"{name} 里这些按钮点了没人接：{dead}"


# ── 缓步增长入口：别再和默认窗口脱节 ─────────────────────────────────
def test_steady_entry_uses_the_current_default_window():
    """入口天数写死过 30，而默认窗口后来改成了 7，点进去和 /steady 不是同一个东西。"""
    cbs = _cbs(menu.CATS["cat_scan"][1])
    assert f"stdy:{steady.DEFAULT_DAYS}:0" in cbs


def test_steady_button_actually_reaches_the_handler():
    """端到端跑一遍分发：点 stdy: 必须真的调到 steady 的扫描，而不是静默落空。"""
    called = {}

    async def fake_run(days, crypto_only=True):
        called["days"] = days
        called["crypto_only"] = crypto_only
        return [], [], 0, 0

    class FakeQuery:
        data = f"stdy:{steady.DEFAULT_DAYS}:0"
        message = None

        def __init__(self):
            self.edits = []

        async def answer(self, text=None, **kw):
            pass

        async def edit_message_text(self, text, **kw):
            self.edits.append(text)

    class FakeUpdate:
        def __init__(self, q):
            self.callback_query = q

    q = FakeQuery()
    orig_run = steady.run
    steady.run = fake_run
    try:
        asyncio.run(menu.button_handler(FakeUpdate(q), None))
    finally:
        steady.run = orig_run

    assert called.get("days") == steady.DEFAULT_DAYS, "分发没走到 steady.run"
    assert q.edits, "结果没回写到消息里"
