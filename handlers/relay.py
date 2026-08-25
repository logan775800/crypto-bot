"""频道搬运 `/relay` —— 把你订阅的频道的新帖自动转到你的群里。

## 为什么不能用机器人做

**Bot API 读不到机器人自己不是管理员的频道。** 别人的频道不可能给你加管理员，
所以这条路是死的——不是配置问题，是接口就没这个能力。

所以这里走 **MTProto（Telethon）**：用**一个真人账号**登录，
它订阅了哪个频道就能读哪个，私密频道也行。

**这个账号不必是他本人的**——`TG_SESSION` 只是"某个账号的凭证"，代码不关心是谁。
2026-08-25 他问「可以用另外一个账号的 TG_SESSION 吗」，答案是可以，
而且这正是该做的：主号不暴露在自动化风险里。用小号的前提是
**那个号自己订阅了源频道、而且在目标群里**。

⚠️ 但别现注册一个新号：**全新账号被限制的概率反而更高**——
刚注册、没有任何正常聊天记录、上来就只做自动转发，是标准的 spam 特征。

**由此带来一个新的失效模式**：配置的人（他，通过机器人）和干活的账号（小号）
**不是同一个**。他在面板上设好了目标群，不代表那个小号进得去；
转发失败只写日志，他那边看到的就是"没反应"。所以有 `selfcheck()`——
逐条实测号是谁、群进没进、频道订没订、能不能转。

⚠️ **这是拿个人账号做自动化，Telegram 对此是灰色态度，账号可能被限制。**
2026-08-24 我把风险讲清楚之后他明确选了这条路。所以：
  · `TG_SESSION` 没配就**完全不启动**，机器人和现在一模一样（不报错、不降级）；
  · 默认**关闭**，要他自己 `/relay on`；
  · 有每小时上限，防某个频道刷屏把群淹了。

## 转发用「原生转发」不是「复制内容」

`client.forward_messages` 转过去会自带「转发自 XXX」的头，图文格式一个不丢，
来源归属天然正确。复制内容的话要自己拼图文、自己标来源，还容易把出处弄丢——
搬别人的东西，出处不能弄丢。

代价是消息显示成**他本人转发的**（因为是他的账号在转），不是机器人发的。
这反而更自然。

## 登录

一次性跑 `tools_tg_login.py` 换出 session（**必须在连得上 Telegram 的机器上跑**），
把 TG_API_ID / TG_API_HASH / TG_SESSION 填进 .env。
注意 docker-compose 的 environment 是**白名单**，三个变量都要加进去，
不然容器根本读不到（这个坑 BYBIT_* 上踩过）。
"""
import logging
import re
import time

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import is_admin
from storage import data, save_data
from handlers.util import safe_reply, safe_edit, escape_md

log = logging.getLogger(__name__)

MAX_PER_HOUR = 20        # 单个频道每小时最多转几条，防刷屏
_client = None           # Telethon 客户端；没配就一直是 None
_sent = []               # [(ts, source)]，用来算每小时上限


def cfg():
    return data.setdefault("relay", {
        "on": False, "target": None, "sources": [],
        "include": [], "exclude": [],
    })


def configured():
    """三个环境变量齐了才算配了。缺任何一个 = 这个功能整个不存在。"""
    import os
    return bool(os.environ.get("TG_API_ID") and os.environ.get("TG_API_HASH")
                and os.environ.get("TG_SESSION"))


def _norm(s):
    """@名字 / t.me/名字 / 纯名字 / -100 开头的 id 都收，统一成可用的形式。"""
    s = (s or "").strip()
    s = re.sub(r"^https?://t\.me/(s/)?", "", s)
    s = s.lstrip("@").strip("/")
    if re.fullmatch(r"-?\d+", s):
        return int(s)
    return s or None


# ── 过滤 ────────────────────────────────────────────────────
def wanted(text, conf):
    """这条要不要转。

    include 为空 = 全都要；非空 = 命中任一关键词才要。
    exclude 命中任一就丢，**exclude 优先于 include**——
    「我要解锁消息，但广告一律不要」这个意图，只有 exclude 优先才表达得出来。
    """
    t = (text or "").lower()
    for kw in conf.get("exclude") or []:
        if kw.lower() in t:
            return False, f"命中排除词「{kw}」"
    inc = conf.get("include") or []
    if not inc:
        return True, ""
    for kw in inc:
        if kw.lower() in t:
            return True, ""
    return False, "没命中任何关键词"


def rate_ok(source, now=None):
    """每小时上限。某个频道抽风连发 50 条时，不能把他的群淹了。"""
    now = now or time.time()
    global _sent
    _sent = [(t, s) for t, s in _sent if now - t < 3600]
    return sum(1 for _t, s in _sent if s == source) < MAX_PER_HOUR


def mark(source, now=None):
    _sent.append((now or time.time(), source))


# ── Telethon ────────────────────────────────────────────────
async def start(app):
    """启动搬运客户端。由 bot.post_init 调用。

    **没配就安静地什么都不做**——和 Bybit 密钥缺失时一样：
    功能不存在，但不能因此让机器人起不来。
    """
    global _client
    if not configured():
        log.info("频道搬运：未配置 TG_SESSION，跳过（功能不启用）")
        return
    import os
    try:
        from telethon import TelegramClient, events
        from telethon.sessions import StringSession
    except ImportError:
        log.warning("频道搬运：缺 telethon，跳过。装它：pip install telethon")
        return
    try:
        _client = TelegramClient(StringSession(os.environ["TG_SESSION"]),
                                 int(os.environ["TG_API_ID"]),
                                 os.environ["TG_API_HASH"])
        await _client.connect()
        if not await _client.is_user_authorized():
            log.error("频道搬运：session 无效或已被踢下线，重新跑 tools_tg_login.py")
            _client = None
            return
        me = await _client.get_me()
        log.info(f"频道搬运：已连接（{me.first_name} @{me.username or '-'}）")

        @_client.on(events.NewMessage())
        async def _on_new(event):
            try:
                await handle(event)
            except Exception as e:
                log.error(f"频道搬运处理出错: {e}")
    except Exception as e:
        log.error(f"频道搬运启动失败: {e}")
        _client = None


async def handle(event):
    """收到任意新消息 → 判断是不是我们盯的频道 → 过滤 → 转发。"""
    conf = cfg()
    if not conf.get("on") or not conf.get("target") or not conf.get("sources"):
        return
    chat = await event.get_chat()
    uname = (getattr(chat, "username", "") or "").lower()
    cid = getattr(chat, "id", None)
    src = None
    for s in conf["sources"]:
        if (isinstance(s, int) and s == cid) or \
           (isinstance(s, str) and s.lower() == uname):
            src = s
            break
    if src is None:
        return

    ok, _why = wanted(event.message.message or "", conf)
    if not ok:
        return
    if not rate_ok(str(src)):
        log.warning(f"频道搬运：{src} 已达每小时 {MAX_PER_HOUR} 条上限，本条跳过")
        return
    try:
        # 原生转发：自带「转发自 XXX」的头，图文和出处一个不丢
        await _client.forward_messages(int(conf["target"]), event.message)
        mark(str(src))
    except Exception as e:
        log.error(f"频道搬运转发失败 {src} → {conf['target']}: {e}")


# ── 自检 ────────────────────────────────────────────────────
# **配置的人和干活的账号不是同一个**（session 可以是小号的），
# 所以"我在面板上设好了"完全不代表"那个号进得了群、订阅了那些频道"。
# 转发失败只会进日志，他那边看到的是"没反应"——这个项目最贵的那类 bug。
# 所以逐条实测：号是谁、群进没进、频道订没订、能不能转。
async def selfcheck():
    conf = cfg()
    lines = ["🩺 *搬运自检*", ""]
    if not configured():
        return "🩺 还没配 TG 账号（面板上有步骤）"
    if _client is None:
        return ("🩺 *搬运自检*\n\n❌ 客户端没连上。\n"
                "多半是 session 失效或被踢下线了——重新跑一次 "
                "`tools_tg_login.py` 换一串新的。")
    try:
        me = await _client.get_me()
        lines.append(f"账号：*{escape_md(me.first_name or '')}* "
                     f"@{me.username or '(无用户名)'}　id `{me.id}`")
        lines.append("　（转发会显示成这个号转的，群友看到的是它）")
    except Exception as e:
        return f"🩺 取账号信息失败：{str(e)[:80]}"

    # 目标群：这个号在不在里面
    tgt = conf.get("target")
    lines.append("")
    if not tgt:
        lines.append("❌ 还没设转发目标（在目标群里发 `/relay here`）")
    else:
        try:
            ent = await _client.get_entity(int(tgt))
            name = getattr(ent, "title", None) or str(tgt)
            lines.append(f"✅ 目标群：*{escape_md(name)}*，这个号在里面")
        except Exception as e:
            lines.append(f"❌ 目标 `{tgt}` 进不去：{str(e)[:60]}")
            lines.append("　 → 用你的主号把这个搬运号拉进那个群")

    # 每个来源频道：订没订、能不能转
    lines.append("")
    srcs = conf.get("sources") or []
    if not srcs:
        lines.append("❌ 还没加频道（`/relay add @频道名`）")
    for s in srcs:
        try:
            ent = await _client.get_entity(s if isinstance(s, str) else int(s))
            name = getattr(ent, "title", None) or str(s)
            # 频道可以开「限制保存内容」，那样任何人都转不出去——
            # 不检这条的话，表现是"订阅了、也没报错、就是不转"
            if getattr(ent, "noforwards", False):
                lines.append(f"⚠️ *{escape_md(name)}*：订阅了，但该频道"
                             f"**禁止转发内容**，搬不出来")
            else:
                lines.append(f"✅ *{escape_md(name)}*　可读可转")
        except Exception as e:
            lines.append(f"❌ `{escape_md(str(s))}`：{str(e)[:50]}")
            lines.append("　 → 让这个搬运号先去订阅这个频道")

    lines.append("")
    lines.append(f"开关：{'🟢 开启中' if conf.get('on') else '🔴 已关闭（不会转）'}")
    used = len([1 for t, _s in _sent if time.time() - t < 3600])
    lines.append(f"最近一小时已转 {used} 条（每频道上限 {MAX_PER_HOUR}）")
    return "\n".join(lines)


# ── 面板 ────────────────────────────────────────────────────
def panel():
    conf = cfg()
    ready = configured()
    live = _client is not None
    srcs = conf.get("sources") or []
    lines = ["📡 *频道搬运*　把订阅的频道新帖转到你的群", ""]
    if not ready:
        lines += [
            "⚠️ *还没配账号*，功能未启用。",
            "",
            "机器人**读不到别人的频道**（Bot API 的硬限制），所以要用你自己的",
            "账号登录一次。步骤：",
            "1. 去 my.telegram.org 申请 api id / api hash",
            "2. 在服务器上跑一次：",
            "　 docker compose exec crypto-bot python tools\\_tg\\_login.py",
            "3. 把打印出的三行填进 .env，重建容器",
            "",
            "⚠️ 这是拿你的个人账号做自动化，Telegram 可能限制账号。",
        ]
        return "\n".join(lines), InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ 返回", callback_data="menu_main")]])

    lines += [
        f"账号连接：{'✅ 已连上' if live else '❌ 没连上（看日志）'}",
        f"开关：{'🟢 开启中' if conf.get('on') else '🔴 已关闭'}",
        f"转发到：`{conf.get('target') or '（未设置）'}`",
        f"盯着的频道（{len(srcs)}）："
        + ("、".join(f"`{escape_md(str(s))}`" for s in srcs) if srcs else "（空）"),
    ]
    if conf.get("include"):
        lines.append(f"只转含：{'、'.join(conf['include'])}")
    if conf.get("exclude"):
        lines.append(f"不转含：{'、'.join(conf['exclude'])}")
    lines += [
        "",
        f"每个频道每小时最多转 {MAX_PER_HOUR} 条。转发带原生「转发自」的头，出处不丢。",
        "",
        "⚠️ **配置的是你，干活的是上面那个号**。它必须自己订阅了那些频道、"
        "而且在目标群里，否则不报错、就是不转。点【🩺 自检】逐条验。",
        "",
        "`/relay add @频道名`　加一个（私密频道用 id）",
        "`/relay del @频道名`　去掉",
        "`/relay here`　　　　把当前会话设为转发目标",
        "`/relay only 解锁 上币`　只转含这些词的",
        "`/relay skip 广告 推广`　不转含这些词的",
        "`/relay check`　　　 自检：这个号进群了吗、订阅了吗、能不能转",
    ]
    rows = [[InlineKeyboardButton("🔴 关闭" if conf.get("on") else "🟢 开启",
                                  callback_data="rl:toggle")],
            [InlineKeyboardButton("🩺 自检（这个号进群了吗/订阅了吗）",
                                  callback_data="rl:check")],
            [InlineKeyboardButton("📍 转发到当前会话", callback_data="rl:here")],
            [InlineKeyboardButton("🧹 清空关键词", callback_data="rl:clearkw"),
             InlineKeyboardButton("🔄 刷新", callback_data="rl:panel")],
            [InlineKeyboardButton("⬅️ 返回", callback_data="menu_main")]]
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def relay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/relay —— 频道搬运：把订阅的频道新帖转到群里（管理员）"""
    if not is_admin(update.effective_user.id):
        await safe_reply(update.message, "⛔ 仅管理员可配置频道搬运")
        return
    conf = cfg()
    args = context.args or []
    act = (args[0].lower() if args else "")
    rest = args[1:]

    if act == "add" and rest:
        for a in rest:
            n = _norm(a)
            if n is not None and n not in conf["sources"]:
                conf["sources"].append(n)
        save_data()
    elif act in ("del", "rm", "remove") and rest:
        for a in rest:
            n = _norm(a)
            if n in conf["sources"]:
                conf["sources"].remove(n)
        save_data()
    elif act == "here":
        conf["target"] = update.effective_chat.id
        save_data()
    elif act == "only":
        conf["include"] = list(rest)
        save_data()
    elif act == "skip":
        conf["exclude"] = list(rest)
        save_data()
    elif act in ("check", "自检", "test"):
        await safe_reply(update.message, await selfcheck(), parse_mode="Markdown")
        return
    elif act in ("on", "开"):
        conf["on"] = True
        save_data()
    elif act in ("off", "关"):
        conf["on"] = False
        save_data()

    txt, kb = panel()
    await safe_reply(update.message, txt, reply_markup=kb, parse_mode="Markdown")


async def on_button(query, context):
    if not is_admin(query.from_user.id):
        await query.answer("仅管理员", show_alert=True)
        return
    conf = cfg()
    act = (query.data or "").split(":")[-1]
    if act == "toggle":
        conf["on"] = not conf.get("on")
        save_data()
        await query.answer("已开启" if conf["on"] else "已关闭")
    elif act == "here":
        conf["target"] = query.message.chat_id if query.message else None
        save_data()
        await query.answer("转发目标已设为当前会话")
    elif act == "check":
        await query.answer("逐条验中…")
        try:
            txt = await selfcheck()
        except Exception as e:
            log.error(f"搬运自检出错: {e}")
            txt = f"自检失败：{str(e)[:100]}"
        await safe_reply(query.message, txt, parse_mode="Markdown")
        return
    elif act == "clearkw":
        conf["include"], conf["exclude"] = [], []
        save_data()
        await query.answer("关键词已清空")
    txt, kb = panel()
    await safe_edit(query, txt, reply_markup=kb, parse_mode="Markdown")
