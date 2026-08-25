"""置顶功能的**真执行**测试 + 一条全库护栏。

## 为什么单独开一个文件

`tests/test_realuse_fixes.py` 里那几条置顶相关的测试**全是 `inspect.getsource`
字符串检查**——它们验的是"代码里写没写这句话"，而**从来没执行过那个函数**。
于是 `handlers/howto.py` 里两处 `log.info(...)` 而模块根本没定义 `log`，
一路绿灯发到线上，真机上每 10 分钟抛一次：

    NameError: name 'log' is not defined
      File "/app/handlers/howto.py", line 244, in auto_refresh_pins

**教训：源码字符串检查验的是约定，不是正确性。** 一个从没被调用过的函数，
里面有 NameError / TypeError，所有"护栏"照样全绿。凡是新加的后台任务，
必须有一条**真的把它跑一遍**的测试。
"""
import asyncio
import pathlib
import types

import pytest

import storage
from handlers import howto as H


@pytest.fixture(autouse=True)
def _clean():
    storage.data["pinned_howto"] = {}
    storage.data.pop("pinned_version", None)
    yield
    storage.data["pinned_howto"] = {}
    storage.data.pop("pinned_version", None)


class FakeBot:
    def __init__(self, pin_ok=True, edit_ok=True):
        self.pin_ok, self.edit_ok = pin_ok, edit_ok
        self.sent, self.pinned, self.edited = [], [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        return types.SimpleNamespace(message_id=42)

    async def pin_chat_message(self, chat_id, mid, **kw):
        if not self.pin_ok:
            raise RuntimeError("Not enough rights to manage pinned messages in the chat")
        self.pinned.append(mid)

    async def edit_message_text(self, **kw):
        if not self.edit_ok:
            raise RuntimeError("boom")
        self.edited.append(kw.get("message_id"))


def _ctx(bot):
    return types.SimpleNamespace(bot=bot)


def _run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


# ── 真的把每条路径跑一遍 ──────────────────────────────────────
def test_first_pin_sends_and_pins():
    bot = FakeBot()
    ok, msg = _run(H.refresh_pin(_ctx(bot), -100))
    assert ok and bot.sent and bot.pinned == [42]
    assert storage.data["pinned_howto"]["-100"] == 42


def test_pin_failure_still_leaves_the_content_in_the_group():
    """钉不上也要把内容留下——他只看到「❌ 置顶失败」时，
    以为消息压根没发，其实（修好后）就在上面。"""
    bot = FakeBot(pin_ok=False)
    ok, msg = _run(H.refresh_pin(_ctx(bot), -100))
    assert ok is False
    assert bot.sent, "钉失败就把内容一起丢了"
    assert "管理员" in msg and "用户权限" in msg


def test_second_call_edits_instead_of_sending_again():
    bot = FakeBot()
    _run(H.refresh_pin(_ctx(bot), -100))
    bot2 = FakeBot()
    _run(H.refresh_pin(_ctx(bot2), -100))
    assert bot2.edited == [42], "第二次应该编辑同一条，不是再发一条"
    assert not bot2.sent


def test_edit_failure_falls_back_to_sending_a_new_one():
    storage.data["pinned_howto"] = {"-100": 42}
    bot = FakeBot(edit_ok=False)
    ok, _msg = _run(H.refresh_pin(_ctx(bot), -100))
    assert ok and bot.sent, "旧消息被删了的话要能重新发一条"


def test_auto_refresh_actually_runs():
    """**这条就是线上炸掉的那个函数。**只做字符串检查的话它永远绿。"""
    from config import VERSION
    storage.data["pinned_howto"] = {"-100": 42}
    bot = FakeBot()
    _run(H.auto_refresh_pins(_ctx(bot)))
    assert storage.data.get("pinned_version") == VERSION
    assert bot.edited == [42]


def test_auto_refresh_is_a_noop_on_the_same_version():
    """版本没变就别动——不然每 10 分钟编辑一次置顶。"""
    from config import VERSION
    storage.data["pinned_howto"] = {"-100": 42}
    storage.data["pinned_version"] = VERSION
    bot = FakeBot()
    _run(H.auto_refresh_pins(_ctx(bot)))
    assert not bot.edited and not bot.sent


def test_auto_refresh_without_any_pinned_group_does_nothing():
    """没人 /pinhowto 过就不该主动去任何群发东西。"""
    bot = FakeBot()
    _run(H.auto_refresh_pins(_ctx(bot)))
    assert not bot.sent


# ── 全库护栏：用了 log 就必须定义 log ──────────────────────────
def test_every_module_that_logs_defines_its_logger():
    """这一条防的是**整类** NameError，不只是这一次。

    `handlers/howto.py` 里写了 `log.info(...)` 而模块没定义 `log`，
    因为那两行从来没被测试执行过，一路发到线上才炸。
    静态扫一遍最便宜：用了 `log.` 的模块必须自己定义 `log`。
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    bad = []
    for f in sorted((root / "handlers").glob("*.py")) + [root / "api.py",
                                                         root / "storage.py"]:
        src = f.read_text(encoding="utf-8")
        uses = "log." in src or "log = " in src
        if not uses:
            continue
        # 定义方式：模块级 log = ... ，或从别处 import log
        defines = ("\nlog = " in src or src.startswith("log = ")
                   or "import log" in src)
        if "log." in src and not defines:
            bad.append(f.name)
    assert not bad, f"这些模块用了 log 却没定义：{bad}"
