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
    """这个会话/这个人能不能用（**不含群成员自动放行**，那条要查 Telegram，
    是异步的，见 `member_allowed`）。管理员永远能用。"""
    if not enabled():
        return True
    if is_admin(user_id) or is_admin(chat_id):
        return True
    return str(chat_id) in _ids("chats") or str(user_id) in _ids("users")


# ── 群成员自动放行 ────────────────────────────────────────────
# 他的原话：「谁知道机器人的用户名就可以使用，这样太危险了，
# 我只想要加入了群组的用户可以私聊」。
#
# 白名单要一个个 /allow，群里十个人就得加十次，人一多必然放弃 → 开关最后被关掉。
# 所以加一条：**在指定群里的人，私聊自动放行**，退群后自动失效。
#
# 判定要问 Telegram（`get_chat_member`），所以带缓存——每条消息都问一次
# 既慢又会被限流。缓存只存"通过"，不存"不通过"：刚被拉进群的人
# 不该等一小时才能用。
_MEMBER_TTL = 3600
_member_cache = {}          # user_id -> 通过时刻
_MEMBER_OK = ("member", "administrator", "creator", "restricted")


def member_gate_on():
    """群成员自动放行开着吗。默认**开**——它是让准入控制真正可用的前提。"""
    return bool(_cfg().get("member_gate", True))


def gate_chats():
    """拿来判成员资格的群。没单独配就用已放行的群 + 管理员里的负数 id
    （负数 = 群/频道，见 Telegram 的 id 符号约定）。"""
    out = [str(c) for c in _cfg().get("chats", []) if str(c).startswith("-")]
    for a in ADMIN_IDS:
        if str(a).startswith("-") and str(a) not in out:
            out.append(str(a))
    return out


async def member_allowed(bot, user_id):
    """这个人在我们的群里吗。查不到（接口报错）时**返回 False**——
    准入控制上"查不出来就放行"等于没有控制。"""
    if not member_gate_on():
        return False
    hit = _member_cache.get(str(user_id))
    if hit and time.time() - hit < _MEMBER_TTL:
        return True
    for cid in gate_chats():
        try:
            m = await bot.get_chat_member(int(cid), int(user_id))
            if getattr(m, "status", None) in _MEMBER_OK:
                _member_cache[str(user_id)] = time.time()
                return True
        except Exception as e:      # 不在群里会直接抛，属正常路径
            log.debug(f"[access] 查 {user_id} 是否在 {cid}: {e}")
    return False


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
    # 白名单没命中，再问一次「他在不在我们群里」。
    # 放在最后是因为这条要打 Telegram 接口，前面能判掉的就别浪费一次请求。
    if await member_allowed(context.bot, user.id):
        return

    now = time.time()
    if now - _last_deny.get(user.id, 0) > DENY_COOLDOWN:
        _last_deny[user.id] = now
        try:
            if update.callback_query:
                await update.callback_query.answer("这个机器人未对你开放", show_alert=True)
            elif update.effective_message:
                await safe_reply(update.effective_message,
                                 "🚪 这个机器人只对群成员开放。\n"
                                 "先加入群，之后直接私聊就能用（可能要等一下生效）。\n"
                                 f"已经在群里还是被挡，把这个 id 发给管理员：{user.id}")
        except Exception as e:
            log.debug(f"拒绝提示发送失败: {e}")
        await _notify_admin_once(context, update, user.id, chat.id)
    raise ApplicationHandlerStop


# ── 管理命令 ──────────────────────────────────────────────
def _status_text():
    c = _cfg()
    mg = "✅ 开" if member_gate_on() else "⬜ 关"
    return ("🚪 *准入控制*\n"
            f"状态：{'✅ 已开启（名单外的人用不了）' if c.get('on') else '🔓 关闭（任何人都能用）'}\n"
            f"群成员自动放行：{mg}　判定用的群：{len(gate_chats())} 个\n"
            f"管理员：{len(ADMIN_IDS)} 人（永远放行）\n"
            f"放行的会话：{len(c.get('chats', []))} 个\n"
            f"放行的用户：{len(c.get('users', []))} 人\n\n"
            "`/access on` 开启　`/access off` 关闭\n"
            "`/access member on|off` 群成员自动放行（默认开）\n"
            "`/allow <id>` 放行　`/deny <id>` 撤销　`/allowed` 看名单\n\n"
            "开着「群成员自动放行」时：**在群里的人私聊直接能用，退群后自动失效**，\n"
            "不用一个个 /allow。缓存 1 小时，刚拉进群的人可能要等一下。\n"
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
        extra = ""
        if member_gate_on():
            n = len(gate_chats())
            extra = (f"\n\n👥 **群成员自动放行是开着的**：在这 {n} 个群里的人，"
                     f"私聊直接能用，退群后自动失效——不用一个个 `/allow`。\n"
                     f"不想要就发 `/access member off`。")
        await safe_reply(update.message,
                         "✅ 已开启准入控制。\n当前会话和你本人已在名单里。\n"
                         "**名单外的人现在用不了了**——要单独放行发 `/allow <id>`。\n"
                         "有人被挡时我会把他的 id 发给你，直接照着放行即可。" + extra,
                         parse_mode="Markdown")
    elif a[0] in ("off", "关"):
        c["on"] = False
        save_data()
        await safe_reply(update.message, "🔓 已关闭准入控制——任何人都能用了。")
    elif a[0] in ("member", "群成员", "成员"):
        # /access member on|off —— 单独控制"在群里就放行"这条，不带参数只报状态
        if len(a) > 1 and a[1] in ("off", "关"):
            c["member_gate"] = False
            save_data()
            await safe_reply(update.message,
                             "⬜ 已关闭群成员自动放行——现在只认 `/allow` 加过的名单。",
                             parse_mode="Markdown")
        elif len(a) > 1 and a[1] in ("on", "开"):
            c["member_gate"] = True
            save_data()
            await safe_reply(update.message,
                             f"✅ 已开启群成员自动放行。判定用的群有 {len(gate_chats())} 个：\n"
                             + ("\n".join(gate_chats()) or "（一个都没有——先把群 `/allow` 进来）"),
                             parse_mode="Markdown")
        else:
            await safe_reply(update.message, _status_text(), parse_mode="Markdown")
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
