"""密钥安全与操作审计 —— 唯一一处直接关系到真钱的模块。

三件事：
  1. **权限体检**：这把 key 到底有哪些权限、能不能提现、有没有绑 IP、什么时候过期。
     "我配的是只读密钥"是最常见的误以为——Bybit 的权限在网页上勾选，
     勾错了本地看不出来，只有问接口才知道。
  2. **一键禁用**：出事时最需要的是"立刻停手"，而不是登服务器改 .env 重启。
     开关落盘，所有下单路径先查它。
  3. **操作审计**：谁、什么时候、下了什么单。事后复盘和事故定责都要它，
     而且它必须写在**下单函数内部**，不能靠调用方自觉。

刻意不做的：密钥加密存储。密钥要拿来签名就必须在内存里是明文，
本地加密只防"文件被看到"，防不住能读进程内存的人；而能读进程内存的人
已经拿到容器权限，加密只是心理安慰。真正有用的是**下面这三条 + 交易所侧
的权限最小化和 IP 白名单**，所以把力气花在这里。
"""
import logging
import time

from storage import data, save_data

log = logging.getLogger(__name__)

AUDIT_MAX = 500          # 审计日志留最近多少条


def trading_enabled():
    """实盘下单总开关。默认开，但一旦关掉就必须显式打开。"""
    return not (data.get("trading_disabled") or False)


def set_trading(on, who=None, why=""):
    data["trading_disabled"] = not on
    audit("trading_switch", {"on": on, "by": who, "why": why})
    save_data()


def audit(action, detail=None):
    """写一条审计。绝不能因为写日志失败而影响下单本身。"""
    try:
        log_list = data.setdefault("audit_log", [])
        log_list.append({"ts": time.time(), "action": action,
                         "detail": detail or {}})
        if len(log_list) > AUDIT_MAX:
            del log_list[: len(log_list) - AUDIT_MAX]
    except Exception as e:
        log.error(f"写审计日志失败: {e}")


async def key_info():
    """问交易所要这把 key 的真实权限。返回 dict 或抛异常。"""
    from handlers.rtrade import _client
    c = _client()
    r = await c._get("/v5/user/query-api", {})
    return r or {}


def _perm_lines(info):
    perms = info.get("permissions") or {}
    rows = []
    risky = []
    for group, items in perms.items():
        if not items:
            continue
        rows.append(f"　{group}: {'、'.join(items)}")
        # 提现权限是唯一一个「拿到 key 就能把钱转走」的权限
        if group.lower() == "wallet" and any(
                "withdraw" in str(x).lower() for x in items):
            risky.append("🚨 *这把 key 有提现权限* —— 交易机器人完全不需要它，"
                         "去 Bybit 后台立刻取消勾选")
    if info.get("ips"):
        rows.append(f"　IP 白名单: {'、'.join(info['ips'])}")
    else:
        risky.append("⚠️ 没有绑定 IP 白名单 —— key 泄露后任何机器都能用它下单")
    return rows, risky


async def keycheck_cmd(update, context):
    """/keycheck —— 这把密钥到底有哪些权限。"""
    from handlers.util import safe_reply
    from config import is_admin
    if not is_admin(update.effective_user.id):
        await safe_reply(update.message, "仅管理员")
        return
    try:
        from bybit_trade import _is_testnet
        env = "🧪 模拟盘" if _is_testnet() else "🔴 **实盘**"
    except Exception:
        env = "?"
    await safe_reply(update.message, "🔐 查询密钥权限…")
    try:
        info = await key_info()
    except Exception as e:
        await safe_reply(update.message, f"查询失败：{str(e)[:80]}\n"
                                         f"（未配置密钥、或该 key 无 query-api 权限）")
        return
    rows, risky = _perm_lines(info)
    exp = info.get("expiredAt") or "永不过期"
    lines = ["🔐 *密钥体检*", f"环境：{env}",
             f"只读标记：{'是' if str(info.get('readOnly')) == '1' else '否（可下单）'}",
             f"到期：{exp}",
             "权限："] + (rows or ["　（接口没返回权限明细）"])
    lines.append(f"下单总开关：{'✅ 开启' if trading_enabled() else '🔴 已禁用'}")
    if risky:
        lines += ["", *risky]
    else:
        lines += ["", "✅ 没有提现权限、且绑了 IP —— 这是交易机器人该有的配置"]
    lines += ["", "出事时立刻停手：`/killswitch on`（禁用所有实盘下单）",
              "查最近操作：`/audit`"]
    await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")


async def killswitch_cmd(update, context):
    """/killswitch on|off —— 一键禁用/恢复实盘下单。"""
    from handlers.util import safe_reply
    from config import is_admin
    uid = update.effective_user.id
    if not is_admin(uid):
        await safe_reply(update.message, "仅管理员")
        return
    a = [x.lower() for x in (context.args or [])]
    if not a:
        await safe_reply(update.message,
            f"🔴 *实盘下单开关*\n\n当前：{'✅ 开启' if trading_enabled() else '🔴 已禁用'}\n\n"
            "`/killswitch on` 禁用下单（查询/平仓不受影响）\n"
            "`/killswitch off` 恢复\n\n"
            "禁用后开仓类操作会被直接拒绝，不需要登服务器改配置重启",
            parse_mode="Markdown")
        return
    if a[0] in ("on", "禁用"):
        set_trading(False, uid, " ".join(a[1:]))
        await safe_reply(update.message, "🔴 已禁用实盘开仓。平仓和查询仍可用。\n"
                                         "恢复：`/killswitch off`", parse_mode="Markdown")
    elif a[0] in ("off", "恢复"):
        set_trading(True, uid)
        await safe_reply(update.message, "✅ 已恢复实盘下单")
    else:
        await safe_reply(update.message, "用法 `/killswitch on` / `/killswitch off`",
                         parse_mode="Markdown")


async def audit_cmd(update, context):
    """/audit [N] —— 最近的实盘操作记录。"""
    from handlers.util import safe_reply
    from config import is_admin
    if not is_admin(update.effective_user.id):
        await safe_reply(update.message, "仅管理员")
        return
    try:
        n = int((context.args or ["20"])[0])
    except ValueError:
        n = 20
    rows = (data.get("audit_log") or [])[-max(1, min(n, 50)):]
    if not rows:
        await safe_reply(update.message, "还没有操作记录")
        return
    lines = [f"📜 *操作审计*　最近 {len(rows)} 条"]
    for r in reversed(rows):
        t = time.strftime("%m-%d %H:%M:%S", time.localtime(r.get("ts", 0)))
        d = r.get("detail") or {}
        brief = "、".join(f"{k}={v}" for k, v in list(d.items())[:4])
        lines.append(f"`{t}` {r.get('action')}　{brief}")
    await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")
