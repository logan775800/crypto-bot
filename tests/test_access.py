"""准入控制：谁能用这个机器人。

在这之前是完全敞开的——任何人搜到就能私聊它问 AI、跑全市场扫描，
花的是他自己的中转站额度和行情配额。

这里守的几件事，每一件都是"开关本身不能把主人锁在门外"这类基础可靠性：
"""
import asyncio
import types

import pytest
from telegram.ext import ApplicationHandlerStop

import storage
from handlers import access


class Ctx:
    def __init__(self):
        self.bot = None
        self.bot_data = {}


def _upd(chat_id, user_id, text="/price BTC", chat_type="private", cq=False):
    msg = types.SimpleNamespace(text=text, reply_text=None)
    q = None
    if cq:
        async def answer(*a, **k):
            return None
        q = types.SimpleNamespace(answer=answer, data="menu_main")
    return types.SimpleNamespace(
        effective_chat=types.SimpleNamespace(id=chat_id, type=chat_type),
        effective_user=types.SimpleNamespace(id=user_id, full_name="路人"),
        effective_message=msg, callback_query=q, message=msg)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    storage.data.pop("access", None)
    access._last_deny.clear()
    access._last_notify.clear()
    monkeypatch.setattr(storage, "save_data", lambda *a, **k: None)
    monkeypatch.setattr(access, "save_data", lambda *a, **k: None)
    # 默认没有管理员配置时 is_admin 对谁都返回 True，会盖住本用例要测的逻辑
    monkeypatch.setattr(access, "is_admin", lambda x: str(x) == "777")
    yield
    storage.data.pop("access", None)


def _run(upd):
    return asyncio.run(access.gate(upd, Ctx()))


def test_off_by_default_lets_everyone_through():
    """升级后不该突然把正在用的群全挡住——必须他自己显式开。"""
    assert access.enabled() is False
    _run(_upd(123, 456))          # 不抛就是放行


def test_stranger_is_blocked_when_on():
    access._cfg()["on"] = True
    with pytest.raises(ApplicationHandlerStop):
        _run(_upd(123, 456))


def test_admin_always_gets_through():
    access._cfg()["on"] = True
    _run(_upd(777, 777))


def test_allowed_user_gets_through():
    access._cfg()["on"] = True
    access.add("users", 456)
    _run(_upd(456, 456))


def test_allowed_group_lets_its_members_through():
    """群按 chat 授权：群里所有人都能用，不用挨个申请。"""
    access._cfg()["on"] = True
    access.add("chats", -1001)
    _run(_upd(-1001, 999, chat_type="supergroup"))


def test_switch_commands_are_never_blocked():
    """把自己锁在门外还够不着开关，是最蠢的失败。"""
    access._cfg()["on"] = True
    for text in ("/access", "/access off", "/allowed", "/id",
                 "/access@cryptocurrencyuu_bot on"):
        _run(_upd(123, 456, text=text))


def test_ordinary_commands_are_still_blocked():
    access._cfg()["on"] = True
    with pytest.raises(ApplicationHandlerStop):
        _run(_upd(123, 456, text="/scan"))


def test_button_clicks_are_blocked_too():
    """只挡消息不挡按钮等于没挡——面板里的按钮照样能跑功能。"""
    access._cfg()["on"] = True
    with pytest.raises(ApplicationHandlerStop):
        _run(_upd(123, 456, cq=True))


def test_stranger_is_only_told_once():
    """陌生人不该靠刷命令换来一堆回复。"""
    access._cfg()["on"] = True
    sent = []

    async def fake_reply(msg, text, **kw):
        sent.append(text)

    orig, access.safe_reply = access.safe_reply, fake_reply
    try:
        for _ in range(4):
            with pytest.raises(ApplicationHandlerStop):
                _run(_upd(123, 456))
    finally:
        access.safe_reply = orig
    assert len(sent) == 1
    assert "456" in sent[0], "提示里要带他的 id，否则管理员没法放行"


def test_turning_on_adds_current_chat_and_admin():
    """在群里发 /access on 不能把这个群一起挡了。"""
    upd = _upd(-1002, 777, text="/access on", chat_type="supergroup")
    ctx = types.SimpleNamespace(args=["on"])
    sent = []

    async def fake_reply(msg, text, **kw):
        sent.append(text)

    orig, access.safe_reply = access.safe_reply, fake_reply
    try:
        asyncio.run(access.access_cmd(upd, ctx))
    finally:
        access.safe_reply = orig
    assert access.enabled()
    assert "-1002" in access._ids("chats")
    assert "777" in access._ids("users")


def test_allow_routes_negative_ids_to_chats():
    """群 id 是负数，要进 chats 而不是 users。"""
    access.add("chats", -1003950673952)
    assert "-1003950673952" in access._ids("chats")


def test_deny_removes():
    access.add("users", 456)
    access.remove("users", 456)
    assert "456" not in access._ids("users")
