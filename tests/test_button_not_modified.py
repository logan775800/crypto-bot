"""刷新按钮渲染出一样的内容时，不该变成一条「机器人异常」推给管理员。

线上现象（2026-08-14）：点「👁 我的波动监控」里的 🔄 刷新，数据没变，
重渲染的文本和按钮跟原消息一字不差，Telegram 报
「Message is not modified」，冒到全局错误处理器 → 管理员私聊收到带 traceback
的异常告警。用户那边什么都没发生，管理员却被正常操作刷屏。

根因是系统性的：handlers/ 里 149 处 query.edit_message_text 全是裸调用，
绕过了本来就会咽掉这个错的 safe_edit。这里守两道：
  1. 面板代码必须走 safe_edit（用例扫源码，防止有人再写回裸调用）；
  2. button_handler 外面兜一层，就算漏了也不推异常告警。
"""
import asyncio
import pathlib
import re
import types

import pytest
from telegram.error import BadRequest

from handlers import menu
from handlers.util import safe_edit


HANDLERS = pathlib.Path(__file__).resolve().parent.parent / "handlers"

NOT_MODIFIED = ("Message is not modified: specified new message content and "
                "reply markup are exactly the same as a current content and "
                "reply markup of the message")


# ---------------------------------------------------------------- 第一道
def test_no_raw_edit_message_text_in_handlers():
    """除了 util.safe_edit 自己，谁都不许直接调 query.edit_message_text。

    裸调用有两个坑：内容没变时报 not modified；动态内容(币名带下划线)
    会让 Markdown 渲染失败。safe_edit 两个都兜住了。
    """
    bad = []
    for path in sorted(HANDLERS.glob("*.py")):
        if path.name == "util.py":
            continue                      # safe_edit 的实现本体
        for i, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
            if line.lstrip().startswith("#"):
                continue
            if re.search(r"\bquery\.edit_message_text\s*\(", line):
                bad.append(f"{path.name}:{i}")
    assert not bad, "这些地方要改成 safe_edit(query, ...)：\n" + "\n".join(bad)


def test_safe_edit_swallows_not_modified():
    calls = []

    class Q:
        async def edit_message_text(self, text, **kw):
            calls.append(text)
            raise BadRequest(NOT_MODIFIED)

    asyncio.run(safe_edit(Q(), "一样的内容"))
    assert len(calls) == 1, "不该为 not modified 重试一次纯文本"


def test_safe_edit_still_falls_back_to_plain_text():
    """Markdown 渲染失败要降级重发——别把这个能力和上面的吞错搞混。"""
    calls = []

    class Q:
        async def edit_message_text(self, text, **kw):
            calls.append(kw.get("parse_mode"))
            if kw.get("parse_mode"):
                raise BadRequest("Can't parse entities: bad offset")
            return "ok"

    assert asyncio.run(safe_edit(Q(), "AKE_USDT", parse_mode="Markdown")) == "ok"
    assert calls == ["Markdown", None]


# ---------------------------------------------------------------- 第二道
def _update(data="my_watchpct"):
    q = types.SimpleNamespace(data=data)

    async def answer(*a, **k):
        return None

    q.answer = answer
    return types.SimpleNamespace(callback_query=q)


def test_button_handler_swallows_not_modified(monkeypatch):
    async def boom(update, context):
        raise BadRequest(NOT_MODIFIED)

    monkeypatch.setattr(menu, "_dispatch", boom)
    asyncio.run(menu.button_handler(_update(), None))      # 不抛就是通过


def test_button_handler_still_raises_real_badrequests(monkeypatch):
    """别为了清净把真问题也藏了——只咽 not modified 这一种。"""
    async def boom(update, context):
        raise BadRequest("Can't parse entities: bad offset 12")

    monkeypatch.setattr(menu, "_dispatch", boom)
    with pytest.raises(BadRequest):
        asyncio.run(menu.button_handler(_update(), None))


def test_button_handler_still_raises_other_errors(monkeypatch):
    async def boom(update, context):
        raise RuntimeError("真的炸了")

    monkeypatch.setattr(menu, "_dispatch", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(menu.button_handler(_update(), None))


def test_dispatch_is_still_wired():
    """button_handler 必须真的把活转给 _dispatch，别拆着拆着拆断了。"""
    seen = []

    async def spy(update, context):
        seen.append(update.callback_query.data)

    orig, menu._dispatch = menu._dispatch, spy
    try:
        asyncio.run(menu.button_handler(_update("cat_alert"), None))
    finally:
        menu._dispatch = orig
    assert seen == ["cat_alert"]
