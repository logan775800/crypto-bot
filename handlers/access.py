"""准入控制 —— 谁能用这个机器人。

在这之前是**完全敞开**的：任何人搜到 @cryptocurrencyuu_bot 都能私聊它查价、
问 AI、跑全市场扫描。AI 走的是自费中转站、行情走的是有配额的接口，
等于谁都能花他的钱；扫描这类重活还会把机器人拖住（PTB 默认串行处理更新，
见 handlers/menu.py 里那段回调过期的注释）。

设计取舍：
  • **默认不开**。这个开关一打开就可能把正在用的群全挡在外面，
    所以必须由他自己在想限的那一刻显式开启，而不是升级后突然生效。
  • 开启的**那一刻**自动把管理员和当前会话放进白名单——
    最蠢的失败是他在群里发 /access on 然后把自己锁在门外。
  • 群按 **chat** 授权（群里所有人都能用），私聊按 **user** 授权。
    他要挡的是陌生人私聊，不是让群友挨个申请。
  • 被拒的人只回一次提示，之后静默：陌生人不该靠刷命令换来一堆回复，
    而完全不回又会显得像坏了。
  • **拒绝时给管理员发一条带 id 的通知**，他直接 /allow <id> 就能放行，
    不用去问对方"你的 id 是多少"。
"""
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop

from config import ADMIN_IDS, is_admin
from storage import data, save_data
from handlers.util import safe_reply

log = logging.getLogger(__name__)

DENY_COOLDOWN = 600      # 同一个人多久内只提示一次
NOTIFY_COOLDOWN = 3600   # 同一个人多久内只惊动管理员一次
_last_deny = {}          # id -> ts（进程内即可，重启后重来一次不算问题）
_last_notify = {}

# 这些命令永远放行：不然他自己被锁在外面时连开关都够不着
ALWAYS_OK = ("/access", "/allow", "/deny", "/allowed", "/id", "/start")


def _cfg():
    return data.setdefault("access", {"on": False, "users": [], "chats": []})


def enabled():
    return bool(_cfg().get("on"))


def _ids(key):
    return {str(x) for x in _cfg().get(key, [])}


def allowed(chat_id, user_id):
    """这个会话/这个人能不能用。管理员永远能用。"""
    if not enabled():
        return True
    if is_admin(user_id) or is_admin(chat_id):
        return True
    return str(chat_id) in _ids("chats") or str(user_id) in _ids("users")


def add(kind, value):
    lst = _cfg().setdefault(kind, [])
    if str(value) not in {str(x) for x in lst}:
        lst.append(str(value))
        save_data()
        return True
    return False


def remove(kind, value):
    lst = _cfg().setdefault(kind, [])
    before = len(lst)
    _cfg()[kind] = [x for x in lst if str(x) != str(value)]
    if len(_cfg()[kind]) != before:
        save_data()
        return True
    return False


def _is_always_ok(update):
    """开关类命令永远放行——把自己锁在门外还够不着开关是最蠢的失败。

    群里发命令会带 @机器人名（/access@xxx_bot on），先把它剥掉再比。
    """
    msg = update.effective_message
    text = (msg.text or "").strip() if msg else ""
    if not text.startswith("/"):
        return False
    return text.split()[0].split("@")[0].lower() in ALWAYS_OK


async def _notify_admin_once(context, update, uid, cid):
    """告诉管理员有人被挡了，带上 id 方便直接放行。"""
    now = time.time()
    if now - _last_notify.get(uid, 0) < NOTIFY_COOLDOWN:
        return
    _last_notify[uid] = now
    u = update.effective_user
    who = (u.full_name if u else "?") or "?"
    kind = "私聊" if str(cid) == str(uid) else f"群 {cid}"
    try:
        from handlers.monitor import notify_admin
        await notify_admin(context,
                           f"🚪 有人被挡在门外\n{who}（id {uid}）在{kind}里想用机器人\n"
                           f"放行发 /allow {uid}｜看名单 /allowed")
    except Exception as e:
        log.warning(f"准入通知失败: {e}")


async def gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """总闸。注册在最高优先组，不放行就 ApplicationHandlerStop 掐断后面所有 handler。"""
    if not enabled():
        return
    chat = update.effective_chat
    user = update.effective_user
    if not chat or not user:
        return
    if _is_always_ok(update):
        return
    if allowed(chat.id, user.id):
        return

    now = time.time()
    if now - _last_deny.get(user.id, 0) > DENY_COOLDOWN:
        _last_deny[user.id] = now
        try:
            if update.callback_query:
                await update.callback_query.answer("这个机器人未对你开放", show_alert=True)
            elif update.effective_message:
                await safe_reply(update.effective_message,
                                 "🚪 这个机器人是私人使用的，未对你开放。\n"
                                 f"要用的话把这个 id 发给管理员：{user.id}")
        except Exception as e:
            log.debug(f"拒绝提示发送失败: {e}")
        await _notify_admin_once(context, update, user.id, chat.id)
    raise ApplicationHandlerStop


# ── 管理命令 ──────────────────────────────────────────────
def _status_text():
    c = _cfg()
    return ("🚪 *准入控制*\n"
            f"状态：{'✅ 已开启（白名单外的人用不了）' if c.get('on') else '🔓 关闭（任何人都能用）'}\n"
            f"管理员：{len(ADMIN_IDS)} 人（永远放行）\n"
            f"放行的会话：{len(c.get('chats', []))} 个\n"
            f"放行的用户：{len(c.get('users', []))} 人\n\n"
            "`/access on` 开启　`/access off` 关闭\n"
            "`/allow <id>` 放行　`/deny <id>` 撤销　`/allowed` 看名单\n"
            "开启时会自动把管理员和当前会话放进名单，不会把你自己锁在外面")


async def access_cmd(update, context):
    if not is_admin(update.effective_user.id):
        return
    a = [x.lower() for x in (context.args or [])]
    if not a:
        await safe_reply(update.message, _status_text(), parse_mode="Markdown")
        return
    c = _cfg()
    if a[0] in ("on", "开"):
        c["on"] = True
        # 先把自己和当前会话放进去，再开——顺序反了就可能把自己锁在门外
        add("chats", update.effective_chat.id)
        add("users", update.effective_user.id)
        save_data()
        await safe_reply(update.message,
                         "✅ 已开启准入控制。\n当前会话和你本人已在名单里。\n"
                         "**其他群/其他人现在用不了了**——要放行发 `/allow <id>`。\n"
                         "有人被挡时我会把他的 id 发给你，直接照着放行即可。",
                         parse_mode="Markdown")
    elif a[0] in ("off", "关"):
        c["on"] = False
        save_data()
        await safe_reply(update.message, "🔓 已关闭准入控制——任何人都能用了。")
    else:
        await safe_reply(update.message, _status_text(), parse_mode="Markdown")


async def allow_cmd(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await safe_reply(update.message,
                         "用法 `/allow <id>`。id 可以是用户 id 或群 id（群 id 是负数）。\n"
                         "不知道 id 就让对方发 /id 给你，或等他被挡时我通知你。",
                         parse_mode="Markdown")
        return
    v = context.args[0].strip()
    kind = "chats" if v.startswith("-") else "users"
    added = add(kind, v)
    await safe_reply(update.message,
                     f"{'✅ 已放行' if added else 'ℹ️ 本来就在名单里'} {v}"
                     f"（{'会话' if kind == 'chats' else '用户'}）")


async def deny_cmd(update, context):
    if not is_admin(update.effective_user.id):
        return
    if not context.args:
        await safe_reply(update.message, "用法 `/deny <id>`", parse_mode="Markdown")
        return
    v = context.args[0].strip()
    gone = remove("chats", v) or remove("users", v)
    await safe_reply(update.message,
                     f"{'✅ 已撤销' if gone else 'ℹ️ 名单里没有'} {v}")


async def allowed_cmd(update, context):
    if not is_admin(update.effective_user.id):
        return
    c = _cfg()
    lines = [_status_text(), "", "*会话*"]
    lines += [f"　`{x}`" for x in c.get("chats", [])] or ["　（空）"]
    lines += ["*用户*"]
    lines += [f"　`{x}`" for x in c.get("users", [])] or ["　（空）"]
    await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")
