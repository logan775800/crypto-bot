"""群里什么时候该唤醒 AI、什么时候必须闭嘴。

线上真事（2026-08-14）：有人在「🚨 合约异动告警」下面回复了一句「@jackhui66」，
只是想叫同事来看，机器人却把这条当成在跟自己说话，答了一段「我读不到该用户的资料…」。

原因是旧规则太宽：**回复机器人的任意消息**都算在跟它说话。可告警/播报也是机器人
发的，群里习惯在告警下面接龙讨论，于是它每条都插嘴。

现在收窄成两条：
  1. 只有回复 **AI 自己说过的话** 才算（告警/播报不登记，见 chat._remember_ai_msg）；
  2. 就算回复的是 AI 那条，只要正文 @ 了别人而没 @ 机器人，也闭嘴。
"""
import asyncio
import types

import pytest

from handlers import chat


BOT_ID, BOT_NAME = 777, "cryptocurrencyuu_bot"


def _ctx(chat_data=None, user_data=None):
    return types.SimpleNamespace(
        bot=types.SimpleNamespace(id=BOT_ID, username=BOT_NAME),
        chat_data={} if chat_data is None else chat_data,
        user_data={} if user_data is None else user_data,
    )


def _entities(text):
    """按 @xxx 造出 Telegram 那样的 mention entity（偏移量这里用不着精确到 UTF-16，
    因为 _msg 的 parse_entity 直接按名字返回）。"""
    ents = []
    for i, tok in enumerate(text.split()):
        if tok.startswith("@"):
            ents.append(types.SimpleNamespace(type="mention", _name=tok))
    return ents


def _msg(text, reply_to_id=None, reply_from_bot=True, caption=False):
    """够用的假 Message：只实现被测代码真正会碰的字段和方法。"""
    rt = None
    if reply_to_id is not None:
        rt = types.SimpleNamespace(
            message_id=reply_to_id,
            from_user=types.SimpleNamespace(id=BOT_ID if reply_from_bot else 999),
        )
    ents = _entities(text)
    m = types.SimpleNamespace(
        text=None if caption else text,
        caption=text if caption else None,
        entities=None if caption else ents,
        caption_entities=ents if caption else None,
        reply_to_message=rt,
        replies=[],
    )
    m.parse_entity = lambda e: e._name
    m.parse_caption_entity = lambda e: e._name
    return m


def _update(msg, chat_type="supergroup"):
    return types.SimpleNamespace(
        message=msg,
        effective_chat=types.SimpleNamespace(type=chat_type, id=-100123),
        effective_user=types.SimpleNamespace(id=42),
    )


def _run(coro):
    """mention_chat 命中时会抛 ApplicationHandlerStop，没命中则正常返回。
    返回 True = 唤醒了 AI。"""
    from telegram.ext import ApplicationHandlerStop
    try:
        asyncio.run(coro)
        return False
    except ApplicationHandlerStop:
        return True


@pytest.fixture
def no_ai(monkeypatch):
    """把真正发 AI 请求那步换掉，只记录有没有被调用。"""
    called = []

    async def fake_reply(update, context, text, images=None):
        called.append(text)

    monkeypatch.setattr(chat, "_reply", fake_reply)
    monkeypatch.setattr(chat, "_images_for", lambda *a, **k: _async_none())
    return called


def _async_none():
    async def _n():
        return None
    return _n()


# ---------------------------------------------------------------- 线上那条 bug
def test_reply_to_alert_and_at_someone_else_stays_silent(no_ai):
    """截图里那条：回复告警 + @别人 → 机器人必须一声不吭。"""
    ctx = _ctx()                       # chat_data 里没有 ai_msgs：告警不登记
    msg = _msg("@jackhui66", reply_to_id=5001)
    assert _run(chat.mention_chat(_update(msg), ctx)) is False
    assert no_ai == []


def test_reply_to_any_bot_push_stays_silent(no_ai):
    """就算不 @ 任何人，回复告警本身也不该唤醒 —— 告警下面接龙聊行情是常态。"""
    ctx = _ctx()
    msg = _msg("这波太猛了")
    msg.reply_to_message = types.SimpleNamespace(
        message_id=5001, from_user=types.SimpleNamespace(id=BOT_ID))
    assert _run(chat.mention_chat(_update(msg), ctx)) is False
    assert no_ai == []


# ---------------------------------------------------------------- 不能误伤的
def test_at_bot_still_works(no_ai):
    ctx = _ctx()
    msg = _msg(f"@{BOT_NAME} BTC 现在能追吗")
    assert _run(chat.mention_chat(_update(msg), ctx)) is True
    assert no_ai and "BTC" in no_ai[0]
    assert "@" not in no_ai[0], "@机器人 那段要从提问里剥掉"


def test_reply_to_ai_answer_keeps_multi_turn(no_ai):
    """多轮对话是回复触发的唯一正当用途，必须留着。"""
    ctx = _ctx(chat_data={"ai_msgs": [5001]})
    msg = _msg("那空单呢", reply_to_id=5001)
    assert _run(chat.mention_chat(_update(msg), ctx)) is True
    assert no_ai == ["那空单呢"]


def test_at_bot_wins_even_when_also_at_someone_else(no_ai):
    """同时 @ 了机器人和别人 —— 明确点了名，就该答。"""
    ctx = _ctx(chat_data={"ai_msgs": [5001]})
    msg = _msg(f"@jackhui66 @{BOT_NAME} 你看这个", reply_to_id=5001)
    assert _run(chat.mention_chat(_update(msg), ctx)) is True


def test_reply_to_ai_but_at_someone_else_stays_silent(no_ai):
    """回复 AI 那条、却 @ 别人：是拉人来看 AI 说了啥，不是在问机器人。"""
    ctx = _ctx(chat_data={"ai_msgs": [5001]})
    msg = _msg("@jackhui66 你看它怎么说", reply_to_id=5001)
    assert _run(chat.mention_chat(_update(msg), ctx)) is False
    assert no_ai == []


def test_reply_to_other_human_stays_silent(no_ai):
    ctx = _ctx(chat_data={"ai_msgs": [5001]})
    msg = _msg("我也觉得", reply_to_id=5001, reply_from_bot=False)
    assert _run(chat.mention_chat(_update(msg), ctx)) is False


def test_guided_flow_is_not_hijacked(no_ai):
    """引导式流程进行中（await_ 态）时，回复 AI 也要让路，否则流程收不到输入。"""
    ctx = _ctx(chat_data={"ai_msgs": [5001]}, user_data={"await_ropen": True})
    msg = _msg("100 65000", reply_to_id=5001)
    assert _run(chat.mention_chat(_update(msg), ctx)) is False


def test_private_chat_never_auto_triggers(no_ai):
    ctx = _ctx(chat_data={"ai_msgs": [5001]})
    msg = _msg("BTC 多少钱", reply_to_id=5001)
    assert _run(chat.mention_chat(_update(msg, chat_type="private"), ctx)) is False


# ---------------------------------------------------------------- 登记簿本身
def test_remember_ai_msg_is_bounded():
    """不设上限的话，长期跑的群会把 chat_data 撑大（还要进 persistence 落盘）。"""
    ctx = _ctx()
    for i in range(chat.AI_MSG_MEMO + 30):
        chat._remember_ai_msg(ctx, types.SimpleNamespace(message_id=i))
    ids = ctx.chat_data["ai_msgs"]
    assert len(ids) == chat.AI_MSG_MEMO
    assert ids[-1] == chat.AI_MSG_MEMO + 29, "留下的应该是最近的，不是最早的"
    assert chat._is_ai_msg(ctx, chat.AI_MSG_MEMO + 29)
    assert not chat._is_ai_msg(ctx, 0), "太早的已经淘汰，回复它不再唤醒"


def test_registry_survives_missing_chat_data():
    """chat_data 不可用时登记与查询都不能抛，否则会把发消息带崩。"""
    broken = types.SimpleNamespace()
    chat._remember_ai_msg(broken, types.SimpleNamespace(message_id=1))
    assert chat._is_ai_msg(broken, 1) is False


# ---------------------------------------------------------------- @ 判定
def test_mentions_other_user_ignores_the_bot_itself():
    assert not chat._mentions_other_user(_msg(f"@{BOT_NAME} 在吗"), BOT_NAME)
    assert chat._mentions_other_user(_msg("@jackhui66 在吗"), BOT_NAME)
    assert not chat._mentions_other_user(_msg("没有艾特任何人"), BOT_NAME)


def test_mentions_other_user_is_case_insensitive():
    """Telegram 用户名不区分大小写，@CryptoCurrencyUU_Bot 也是在叫它自己。"""
    assert not chat._mentions_other_user(_msg(f"@{BOT_NAME.upper()} 在吗"), BOT_NAME)


def test_text_mention_counts_as_other_user():
    """没设用户名的人被 @ 时是 text_mention，一样算 @ 了别人。"""
    msg = _msg("看看这个")
    msg.entities = [types.SimpleNamespace(type="text_mention", _name="张三")]
    assert chat._mentions_other_user(msg, BOT_NAME)


def test_caption_path_uses_caption_entities():
    """图片走 caption，别去读 entities（那边是 None，会漏判）。"""
    msg = _msg("@jackhui66 看图", caption=True)
    assert chat._mentions_other_user(msg, BOT_NAME, caption=True)


# ── 工具链挂了要说出来，而且不能再用"你有工具"的提示词 ────────
# 2026-08-25 现场：他 @机器人「分析下 ake 给个建议」，回了一段
# 「目前本轮无法调用 resolve_symbol 和实时行情工具，因此不能确认 AKE 对应的
# 具体合约」。resolve_symbol 是**真实存在**的工具——不是模型瞎编，是真调不到。
#
# 两个错叠一起：
#   ① 工具对话失败后**静默**降级成纯对话，用户不知道这是故障；
#   ② 降级后还传那个写着"你有 12 个工具怎么调"的 SYSTEM，
#      模型明知有工具却一个都没有，只能回这种话——看起来像"这机器人啥也干不了"。

def test_degraded_path_uses_a_prompt_without_tools():
    """降级时必须换提示词。用带工具说明的那个，模型只会去解释自己调不到工具。"""
    from handlers import chat as C
    assert hasattr(C, "SYSTEM_NOTOOLS")
    assert "没有任何实时行情工具" in C.SYSTEM_NOTOOLS
    assert "不要提任何工具名" in C.SYSTEM_NOTOOLS


def test_degraded_prompt_forbids_making_up_numbers():
    """没有数据源的时候最危险的不是答不上来，是**编一个价格出来**。"""
    from handlers import chat as C
    assert "绝不编造" in C.SYSTEM_NOTOOLS
    assert "不构成投资建议" in C.SYSTEM_NOTOOLS


def test_degraded_prompt_points_at_the_non_ai_paths():
    """行情命令不走 AI，工具链挂了它们照常能用——要告诉用户这条退路。"""
    from handlers import chat as C
    assert "/scan" in C.SYSTEM_NOTOOLS or "发币名" in C.SYSTEM_NOTOOLS


def test_degradation_is_visible_to_the_user():
    """**静默降级是这个项目最贵的一类 bug。**
    用户必须知道这一轮的答案没有实时数据支撑。"""
    import inspect
    from handlers import chat as C
    src = inspect.getsource(C._reply)
    assert "取不到实时数据" in src, "降级了要在回复里说一句"
    assert "SYSTEM_NOTOOLS" in src


def test_degradation_is_recorded_in_the_heartbeat():
    """连续降级说明工具链真的坏了，该让管理员知道，而不是每次只糊弄过去。"""
    import inspect
    from handlers import chat as C
    src = inspect.getsource(C._reply)
    assert "monitor" in src and "beat" in src
