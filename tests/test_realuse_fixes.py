"""2026-08-25 真机反馈修的五条。每条都附现场，不然下次会被当成"多余的判断"删掉。"""
import asyncio
import types

import pytest

import storage
from handlers import access as A, detail as D, howto as H, monitor as M, watchpct as W


def _run(c):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(c)


# ── ① 合约地址被大写成 0X… 导致链上监控建不了 ────────────────
def test_contract_address_survives_the_command_parser():
    """现场：`/watchpct 0x1706…4444 5` 回「没查到 0X1706…4444 的价格」。

    真因是命令解析先 `norm_symbol()` 把地址 `.upper()` 成 `0X…`，
    下游 add_watch 里那道 `is_address()` 当场失效，请求掉进交易所分支。
    add_watch 的注释早就写明地址不能过 norm_symbol——**但大写发生在它之前**。
    """
    import inspect
    from handlers.onchain import is_address
    from handlers.watchpct import norm_symbol

    addr = "0x1706f1e06c69F3A8Cf33cce179d5d78a5c6f4444"
    assert is_address(addr)
    assert not is_address(norm_symbol(addr)), "前提：大写之后就不认了"

    src = inspect.getsource(W.watchpct)
    assert "is_address" in src, "命令解析没先判地址，链上监控建不了"


def test_unwatch_also_keeps_the_address_intact():
    """取消时也不能大写——存进去的是原样地址，大写了就对不上。"""
    import inspect
    assert "is_address" in inspect.getsource(W.unwatchpct)


# ── ② 部署成功但群里没有更新播报 ──────────────────────────────
def test_group_ids_in_admin_list_still_get_the_broadcast():
    """现场：部署成功了，群里却没有更新内容。

    `announce_update` 会把 ADMIN_IDS 里的会话排除（免得管理员收两遍），
    而 ADMIN_CHAT_ID 里填**群 id** 是常见配置——那会把整个群静默剔掉。
    Telegram 的 id 有符号约定：正数是用户，负数是群/频道。只该排除正数。
    """
    import inspect
    src = inspect.getsource(M.announce_update)
    assert "int(a) > 0" in src, "群 id 会被当成管理员排除，整个群收不到更新播报"


def test_zero_recipients_is_logged_as_a_warning():
    """发了 0 个是异常现场，只记 info 的话日志里看不出来。"""
    import inspect
    src = inspect.getsource(M.announce_update)
    assert "一个会话都没发出去" in src


# ── ③ 三张图的顺序 ────────────────────────────────────────────
def test_four_hour_chart_comes_first():
    """相册第一张会被 Telegram 放大，其余缩成小图。
    他盯的是入场时机，那就该把 4h 放第一；周线是背景板，缩着看方向就够。"""
    assert D._TF_LABELS[0][0] == "4h", "4小时图要放第一张"
    assert [tf for tf, _ in D._TF_LABELS] == ["4h", "1d", "1w"]


def test_verdict_is_still_computed_on_the_daily():
    """图的排序和"用哪个周期算研判"是两件事。
    MA3/13/23 的阈值是按日线定的语义，换周期同一套阈值说的不是一回事。"""
    import inspect
    assert "1d" in inspect.getsource(D.build_multi_charts)


# ── ④ 只让群成员私聊 ──────────────────────────────────────────
def test_member_gate_is_on_by_default():
    """白名单要一个个 /allow，人一多必然放弃、开关最后被关掉。
    "在群里就能用"才是让准入控制真正可用的前提。"""
    storage.data.pop("access", None)
    assert A.member_gate_on() is True


def test_member_gate_can_be_turned_off():
    storage.data["access"] = {"on": True, "member_gate": False}
    assert A.member_gate_on() is False
    storage.data.pop("access", None)


def test_unknown_membership_is_denied():
    """**查不出来就放行 = 没有控制。**接口报错时必须当成不在群里。"""
    class Bot:
        async def get_chat_member(self, *a, **k):
            raise RuntimeError("接口挂了")
    storage.data["access"] = {"on": True, "member_gate": True, "chats": ["-100123"]}
    A._member_cache.clear()
    assert _run(A.member_allowed(Bot(), 999)) is False
    storage.data.pop("access", None)


def test_member_of_the_group_is_allowed():
    class Bot:
        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="member")
    storage.data["access"] = {"on": True, "member_gate": True, "chats": ["-100123"]}
    A._member_cache.clear()
    assert _run(A.member_allowed(Bot(), 999)) is True
    storage.data.pop("access", None)


def test_left_members_are_not_allowed():
    """退群后要自动失效——这是它比白名单好用的地方。"""
    class Bot:
        async def get_chat_member(self, cid, uid):
            return types.SimpleNamespace(status="left")
    storage.data["access"] = {"on": True, "member_gate": True, "chats": ["-100123"]}
    A._member_cache.clear()
    assert _run(A.member_allowed(Bot(), 999)) is False
    storage.data.pop("access", None)


def test_only_negative_ids_are_used_as_gate_groups():
    """判成员资格只能拿群/频道去问，拿用户 id 去问必然报错。"""
    storage.data["access"] = {"chats": ["-100123", "456"]}
    assert "-100123" in A.gate_chats()
    assert "456" not in A.gate_chats()
    storage.data.pop("access", None)


# ── ⑤ 置顶消息要自己跟着版本走 ────────────────────────────────
def test_pinned_text_covers_the_main_entry_points():
    """他的抱怨：置顶"不能概括全部命令功能按钮"。"""
    t = H.pinned_text()
    for cmd in ("/menu", "/howto", "/commands", "/vtrade", "/rank", "/lsr",
                "/scan", "/liqmap", "/watchpct", "/oc", "/changelog"):
        assert cmd in t, f"置顶里少了 {cmd}"


def test_pinned_text_says_what_is_new_in_this_version():
    """**这是他真正卡住的那点**：更新后新命令在哪不清楚。
    版本号和条目从 CHANGELOG 现取，不写死，永远不会落后于代码。"""
    from config import VERSION
    t = H.pinned_text()
    assert VERSION in t
    assert "新增" in t


def test_pin_edits_instead_of_spamming_new_messages():
    """每版发一条新置顶会把群刷得没法看，旧的还留着继续误导人。"""
    import inspect
    assert "edit_message_text" in inspect.getsource(H.refresh_pin)


def test_pin_refresh_only_touches_groups_that_opted_in():
    """只刷已经 /pinhowto 建立过置顶的群，不会主动去别的群刷屏。"""
    import inspect
    assert "pinned_howto" in inspect.getsource(H.auto_refresh_pins)


def test_pin_command_is_registered():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / "bot.py").read_text(
        encoding="utf-8")
    assert 'CommandHandler("pinhowto"' in src
    assert "auto_refresh_pins" in src, "没注册自动刷新，还是得手动改置顶"


# ── 置顶要写全，而且提示不能指错地方 ──────────────────────────
def test_pinned_text_fits_in_one_telegram_message():
    """Telegram 单条上限 4096 字。超了整条发不出去，
    而"发不出去"的表现是置顶功能整个不工作。"""
    t = H.pinned_text()
    assert len(t) < 3800, f"置顶已经 {len(t)} 字，逼近 4096 上限"


def test_pinned_text_covers_every_major_area():
    """他的原话：置顶「不能概括全部命令功能按钮」。
    178 个命令塞不进一条消息，但**每个大类都得有代表**，
    而且要写清对应的菜单入口在哪。"""
    t = H.pinned_text()
    areas = {
        "查行情": ("/oc", "/info", "/fear"),
        "榜单": ("/rank", "/top", "/lsr", "/fex"),
        "找机会": ("/scan", "/microcap", "/breakout"),
        "看图": ("/achart", "/chart", "/liqmap"),
        "提醒": ("/alert", "/watchpct", "/cond"),
        "告警订阅": ("/watchpump", "/watchcontract", "/pump3", "/watchmarket"),
        "模拟交易": ("/vtrade", "/vopen", "/vbuy", "/vreset"),
        "复盘风险": ("/rstats", "/weekly", "/plan", "/risk"),
        "设置": ("/source", "/venue", "/datacheck", "/changelog"),
    }
    for area, cmds in areas.items():
        for c in cmds:
            assert c in t, f"「{area}」这块少了 {c}"


def test_pinned_text_points_at_the_menu_paths():
    """光给命令不够——他反复说的是"按钮在哪不清楚"。"""
    t = H.pinned_text()
    assert t.count("菜单：") >= 4, "至少几个大类要写明菜单入口在哪"
    assert "提醒与订阅" in t


def test_pin_failure_points_at_the_admin_menu_not_member_permissions():
    """**这条提示指错过一次。**

    他照着"需要置顶消息权限"去群资料 →「用户权限」里勾了「置顶消息」，
    结果还是失败——那是**群成员的默认权限**，对机器人无效：
    Telegram 的 Bot API 要求置顶必须是管理员。
    指错方向的提示比不提示更浪费时间。
    """
    import inspect
    src = inspect.getsource(H.refresh_pin)
    assert "用户权限" in src and "管理员" in src, "没写清该点哪个菜单"
    assert "长按" in src, "要给一条不用管理员也能走的退路"


def test_content_is_sent_even_when_pinning_fails():
    """钉不上也要把内容留在群里。

    原来发和钉在同一个 try 里，钉失败就整件事算失败——
    他看到「❌ 置顶失败」，却不知道消息其实已经在上面了。
    """
    import inspect
    src = inspect.getsource(H.refresh_pin)
    send_at = src.index("send_message")
    pin_at = src.index("pin_chat_message")
    between = src[send_at:pin_at]
    assert "return False" in between or "except" in between, \
        "发送和置顶要分开处理，钉失败不能连内容一起丢"
