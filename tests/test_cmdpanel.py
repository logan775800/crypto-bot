"""全部命令按钮面板。

用户反复卡在「记不住命令」上。150 个命令手工维护按钮清单必然漏，
而且漏掉的恰恰是新加的那个——所以面板从**已注册的 handler 自动生成**。
这些用例守的就是那个自动性：新命令必须自动出现、必须有分类、必须有说明。
"""
import importlib
import io
import re

import pytest
from telegram.ext import CommandHandler

from handlers import cmdpanel


class _App:
    def __init__(self):
        self.handlers = {0: []}

    def add_handler(self, h, group=0):
        self.handlers.setdefault(group, []).append(h)


@pytest.fixture(scope="module")
def index():
    """按 bot.py 里真实注册的命令建索引（不启动网络）。"""
    import bot as B
    src = io.open("bot.py", encoding="utf-8").read()
    app = _App()
    for name, path in re.findall(
            r'CommandHandler\("([a-z_]+)", ([a-zA-Z_.]+)\)', src):
        mod, _dot, attr = path.rpartition(".")
        try:
            owner = importlib.import_module("handlers." + mod) if mod else B
        except ImportError:
            continue
        fn = getattr(owner, attr, None)
        if fn:
            app.add_handler(CommandHandler(name, fn))
    return cmdpanel.build_index(app)


def _registered():
    src = io.open("bot.py", encoding="utf-8").read()
    return set(re.findall(r'CommandHandler\("([a-z_]+)"', src))


# ── 自动性 ───────────────────────────────────────────────────────
def test_every_registered_command_is_in_the_panel(index):
    """新加命令必须自动出现在面板里——这条是整个设计的意义所在。"""
    missing = _registered() - set(index)
    assert not missing, f"这些命令没进面板：{sorted(missing)}"


def test_panel_has_no_ghost_commands(index):
    """反过来也要成立：面板里不能有已经删掉的命令。"""
    assert not (set(index) - _registered())


def test_index_is_not_trivially_small(index):
    assert len(index) > 100


# ── 分类 ─────────────────────────────────────────────────────────
def test_every_command_has_a_category(index):
    assert all(i["cat"] for i in index.values())


def test_almost_nothing_falls_into_other(index):
    """落进「其他」= 有新模块没登记分类。允许少量，但不该成片。"""
    other = [n for n, i in index.items() if i["cat"] == cmdpanel.OTHER]
    assert len(other) <= 3, f"未分类命令过多：{sorted(other)}"


def test_other_category_sorts_last(index):
    cats = [c for c, _n in cmdpanel.categories()]
    if cmdpanel.OTHER in cats:
        assert cats[-1] == cmdpanel.OTHER


# ── 标签 ─────────────────────────────────────────────────────────
def test_most_commands_have_a_human_label(index):
    """一排光秃秃的 /unwatchhold 等于没做——绝大多数要有中文说明。"""
    bare = [n for n, i in index.items() if i["label"] == f"/{n}"]
    ratio = 1 - len(bare) / len(index)
    assert ratio >= 0.9, f"只有 {ratio:.0%} 的命令有说明，缺：{sorted(bare)[:20]}"


def test_label_keeps_the_command_name(index):
    """按钮上必须能看到命令本身，否则用户学不会怎么打字调用。"""
    for name, info in index.items():
        assert info["label"].startswith(f"/{name}")


def test_labels_fit_telegram_button(index):
    for info in index.values():
        assert len(info["label"]) <= 40


def test_no_invented_label_when_nothing_is_known():
    """没有任何说明时只给命令名，不许凭函数名编一个出来。"""
    def anon(update, context):
        pass
    assert cmdpanel._label("zzz_unknown_cmd", anon) == "/zzz_unknown_cmd"


# ── 键盘 ─────────────────────────────────────────────────────────
def test_home_lists_all_categories(index):
    cbs = [b.callback_data for r in cmdpanel.home_kb().inline_keyboard for b in r]
    assert len([c for c in cbs if c.startswith("cmd:cat:")]) == len(cmdpanel.categories())
    assert "menu_main" in cbs


def test_category_page_paginates(index):
    big = max(cmdpanel.categories(), key=lambda kv: len(kv[1]))
    kb, page, total = cmdpanel.cat_kb(big[0], 0)
    assert total >= 1 and page == 0
    cbs = [b.callback_data for r in kb.inline_keyboard for b in r]
    assert len([c for c in cbs if c.startswith("cmd:run:")]) <= cmdpanel.PAGE
    if total > 1:
        assert any(c.startswith("cmd:pg:") for c in cbs)


def test_page_index_is_clamped(index):
    big = max(cmdpanel.categories(), key=lambda kv: len(kv[1]))
    _kb, page, total = cmdpanel.cat_kb(big[0], 999)
    assert page == total - 1


def test_callback_data_within_telegram_limit(index):
    for name in index:
        assert len(f"cmd:run:{name}".encode()) <= 64
    for cat, _n in cmdpanel.categories():
        assert len(f"cmd:cat:{cat}".encode()) <= 64
