"""引导式输入的状态管理（点按钮 → 提示 → 下一条消息当参数）。

这套东西以前是直接往 `context.user_data["await_xxx"] = True` 里塞的，
在群里踩出一个很难看的坑：

  某人点过【👁 持续波动监控】没填完 → 之后他在群里说的**每一句话**都被
  当成"在回答提示"，机器人逐条回「请发「币 百分比」，例如 DOGE 5」。
  别人正常聊天被刷屏，而且没人知道怎么关掉。

三个原因叠在一起，缺一条都不至于这么糟：
  1. **user_data 是按用户存的，跨会话共享**——在私聊点的按钮，群里发消息照样被拦；
  2. **永不过期**，而且状态还被 PicklePersistence 持久化，重启都还在；
  3. **群里把闲聊当输入去纠正**——「又不能做网格」被回一句"百分比要是数字"。

所以这里定三条规矩：
  • 绑会话：只在**点按钮的那个会话**里生效；
  • 有时效：超过 TTL 自动失效，不用用户记得发 /menu；
  • 群里只认"像输入的消息"：带中文、长句一律当闲聊**静默放行**，
    不回复也不报错——宁可漏接一次输入，也不要在群里刷屏。
"""
import re
import time

TTL = 300               # 引导态有效期（秒）。点完按钮 5 分钟不填就当放弃了
MAX_INPUT_LEN = 40      # 超过这个长度的消息不可能是「币 百分比」这类参数行

CJK = re.compile(r"[一-鿿]")


def arm(context, key, update, value=True):
    """开启一个引导态，绑定到当前会话并打上时间戳。"""
    context.user_data[key] = {
        "v": value,
        "chat": update.effective_chat.id if update and update.effective_chat else None,
        "ts": time.time(),
    }


def arm_chat(context, key, chat_id, value=True):
    """同 arm，但直接给 chat_id（按钮回调里手头只有 query）。"""
    context.user_data[key] = {"v": value, "chat": chat_id, "ts": time.time()}


def _unwrap(rec):
    """兼容老数据：以前存的是裸值（True 或 dict），没有会话和时间戳。"""
    if isinstance(rec, dict) and "v" in rec and "ts" in rec:
        return rec["v"], rec.get("chat"), rec.get("ts", 0)
    return rec, None, None


def pending(context, key, update):
    """取引导态的值；不该生效时返回 None（过期的顺手清掉）。

    不该生效 = 已过期 / 不是设置它的那个会话。
    """
    rec = context.user_data.get(key)
    if not rec:
        return None
    val, chat, ts = _unwrap(rec)
    if ts is not None and time.time() - ts > TTL:
        context.user_data.pop(key, None)
        return None
    if chat is not None and update and update.effective_chat \
            and update.effective_chat.id != chat:
        return None                 # 别的会话，不动它的状态，只是这里不生效
    return val


def clear(context, *keys):
    for k in keys:
        context.user_data.pop(k, None)


# 中文名的币是真实存在的（链上代币「牛来」就是），所以"带中文=闲聊"这条
# 不能一刀切。这几个引导态收的是**币名/地址**，必须放中文过去；
# 其余收的是「DOGE 5」这类参数行，继续按纯 ASCII 卡死。
CJK_OK_KEYS = {"await_onchain", "await_ropen_coin", "await_alert_coin",
               "await_vcoin"}
CJK_MAX_LEN = 12        # 中文币名再长也就几个字；长句是聊天
# 中文闲聊几乎一定带这些；币名不会
CHAT_PUNCT = re.compile(r"[，。！？、；：…?!~（）()\"'《》]")
# 光靠"短 + 没标点"挡不住闲聊：当初刷屏的原话「又不能做网格」正好 6 个字没标点。
# 这些是中文口语里绕不开的虚词/动词，币名基本不会用到。
# 宁可漏接一次（他还能发 /oc 牛来），也不要在群里把别人的话当成查询。
CHAT_CHARS = set("的了吗呢吧啊呀嘛么怎哪谁为什不是这那还也就都"
                 "我你他她能有没在要会想说看问买卖好行对错很太真假做")


def looks_like_input(text, allow_cjk=False):
    """这条消息看起来像"在回答提示"吗？

    引导式要的都是「DOGE 5」「0x地址」「65000」这类短参数行。
    带标点、长句 —— 那是在聊天，不是在回答我。

    allow_cjk：这个引导态收的是币名，中文名合法（「牛来」）。这时改判：
    短、不带空格、不带中文标点才算输入。误判的代价也就是多查一次没有的币，
    而且引导态只活 5 分钟、只在点按钮的那个会话里生效——不会像当初那样刷屏。
    """
    s = (text or "").strip()
    if not s or len(s) > MAX_INPUT_LEN:
        return False
    if CJK.search(s):
        if not allow_cjk:
            return False
        return (len(s) <= CJK_MAX_LEN and " " not in s
                and not CHAT_PUNCT.search(s)
                and not (CHAT_CHARS & set(s)))
    # 参数行基本都是「字母数字 + 空格 + 少量符号」
    return bool(re.fullmatch(r"[A-Za-z0-9 ._:=+\-/]+", s))


def should_handle(context, key, update, text):
    """群里要不要把这条消息当引导输入。→ (值, 要不要静默跳过)。

    私聊里放宽：那是一对一，回一句"格式不对"是帮忙不是打扰。
    群里必须严格：宁可漏接一次输入，也不要把别人的闲聊回成"百分比要是数字"。
    """
    val = pending(context, key, update)
    if val is None:
        return None, False
    is_group = bool(update and update.effective_chat
                    and update.effective_chat.type in ("group", "supergroup"))
    if is_group and not looks_like_input(text, allow_cjk=key in CJK_OK_KEYS):
        return None, True           # 群里的闲聊：静默放行，状态留着等真正的输入
    return val, False
