"""版本更新要播报到群里，而不是只发管理员私聊。

线上现象（2026-08-14）：v1.16.4 上线后，「本次更新」只出现在和机器人的私聊里，
群里的人只看到机器人行为变了，不知道为什么变。根因是 startup_notify 走
notify_admin，目标集合从头到尾只有 ADMIN_IDS。

两条克制必须一起测，否则修完这个会立刻换来「机器人天天刷群」：
  • 只在版本号变化时播报（容器重启/改 .env 不算）；
  • 没写更新说明就不播报。
"""
import asyncio
import types

import pytest

import storage
from handlers import monitor, changelog


class FakeBot:
    def __init__(self, fail_on=()):
        self.sent = []
        self.fail_on = set(fail_on)

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_on:
            raise RuntimeError("chat not found")
        self.sent.append((chat_id, text))


def _ctx(fail_on=()):
    return types.SimpleNamespace(bot=FakeBot(fail_on))


@pytest.fixture(autouse=True)
def clean_data(monkeypatch):
    """每条用例都从干净的 data 开始，别让上一条的订阅漏过来。"""
    storage.data.clear()
    storage.apply_defaults()
    monkeypatch.setattr(storage, "save_data", lambda: None)
    yield
    storage.data.clear()
    storage.apply_defaults()


@pytest.fixture
def notes(monkeypatch):
    monkeypatch.setattr(changelog, "notes_for", lambda v: ["改了个东西"])


# ---------------------------------------------------------------- 主诉
def test_update_goes_to_subscribed_groups(notes, monkeypatch):
    monkeypatch.setattr(monitor, "ADMIN_IDS", {"111"})
    storage.data["contract_watch"] = [-1003950673952]
    ctx = _ctx()
    asyncio.run(monitor.announce_update(ctx, "v9.9.9"))
    assert [c for c, _t in ctx.bot.sent] == [-1003950673952]
    assert "v9.9.9" in ctx.bot.sent[0][1] and "改了个东西" in ctx.bot.sent[0][1]


def test_admin_is_not_notified_twice(notes, monkeypatch):
    """管理员刚在私聊收过启动播报，别再收一条几乎一样的。"""
    monkeypatch.setattr(monitor, "ADMIN_IDS", {"111"})
    storage.data["broadcast_chats"] = [111, -1003950673952]
    ctx = _ctx()
    asyncio.run(monitor.announce_update(ctx, "v9.9.9"))
    assert [c for c, _t in ctx.bot.sent] == [-1003950673952]


def test_group_text_is_not_about_restarts(notes):
    """群里的人不关心容器重启，只关心行为变了什么。"""
    txt = changelog.update_text("v9.9.9")
    assert "重启" not in txt and "已加载" not in txt
    assert "v9.9.9" in txt


# ---------------------------------------------------------------- 别刷群
def test_same_version_announced_only_once(notes, monkeypatch):
    monkeypatch.setattr(monitor, "ADMIN_IDS", set())
    storage.data["contract_watch"] = [-100]
    ctx = _ctx()
    asyncio.run(monitor.announce_update(ctx, "v9.9.9"))
    asyncio.run(monitor.announce_update(ctx, "v9.9.9"))   # 容器重启，版本没变
    assert len(ctx.bot.sent) == 1


def test_new_version_announces_again(notes, monkeypatch):
    monkeypatch.setattr(monitor, "ADMIN_IDS", set())
    storage.data["contract_watch"] = [-100]
    ctx = _ctx()
    asyncio.run(monitor.announce_update(ctx, "v9.9.9"))
    asyncio.run(monitor.announce_update(ctx, "v9.9.10"))
    assert len(ctx.bot.sent) == 2


def test_no_notes_means_no_announcement(monkeypatch):
    monkeypatch.setattr(changelog, "notes_for", lambda v: [])
    monkeypatch.setattr(monitor, "ADMIN_IDS", set())
    storage.data["contract_watch"] = [-100]
    ctx = _ctx()
    asyncio.run(monitor.announce_update(ctx, "v9.9.9"))
    assert ctx.bot.sent == []
    assert storage.data["announced_version"] == "", "没播报就不该占用版本标记"


def test_dead_chat_does_not_block_others_or_replay(notes, monkeypatch):
    """一个死会话不能挡住其余会话，也不能让每次重启都重播一遍。"""
    monkeypatch.setattr(monitor, "ADMIN_IDS", set())
    storage.data["contract_watch"] = [-100, -200, -300]
    ctx = _ctx(fail_on={-200})
    asyncio.run(monitor.announce_update(ctx, "v9.9.9"))
    assert [c for c, _t in ctx.bot.sent] == [-100, -300]
    assert storage.data["announced_version"] == "v9.9.9"


# ---------------------------------------------------------------- 目标集合
def test_subscribed_chats_covers_every_subscription_shape():
    """四类结构都要认：id列表 / 带chat_id的字典列表 / 以chat_id为键的字典 / 内嵌字段。"""
    storage.data["contract_watch"] = [-1]                      # id 列表
    storage.data["watchpct"] = [{"chat_id": -2, "symbol": "BTC"}]  # 字典列表
    storage.data["fex_subs"] = {"-3": {"threshold": 1}}        # 以 id 为键
    storage.data["brief"] = {"enabled": True, "chat_id": -4}   # 内嵌字段
    storage.data["vtrade"] = {"42": {"chat_id": -5}}
    storage.data["holding_watch"] = {"42": -6}
    got = set(storage.subscribed_chats())
    assert got == {-1, -2, -3, -4, -5, -6}


def test_subscribed_chats_dedups_and_normalizes_types():
    """同一个群可能既订了合约告警又订了播报，字符串和整数混着存。"""
    storage.data["contract_watch"] = [-100, "-100"]
    storage.data["broadcast_chats"] = ["-100"]
    assert storage.subscribed_chats() == [-100]


def test_subscribed_chats_ignores_junk():
    storage.data["contract_watch"] = [None, "", "abc", -100]
    assert storage.subscribed_chats() == [-100]


def test_migrate_and_announce_share_one_key_list():
    """两处对「chat_id 存在哪」的认知必须同源，否则后加的订阅类型会被漏掉。

    这条守的是那份常量清单本身：谁再加订阅类型，加进常量就两边都生效。
    """
    storage.data["pump_watch"] = {"-100": {"pct": 5}}
    storage.data["weekly_subs"] = [-100]
    assert -100 in storage.subscribed_chats()
    moved = storage.migrate_chat(-100, -200)
    assert moved == 2, "这两类订阅在群升级时也必须跟着搬家"
    assert storage.subscribed_chats() == [-200]
