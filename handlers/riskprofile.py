"""个性化风控参数 —— 把「应该控制风险」变成账户里一组会拦人的数字。

写死的默认值对所有人一样，但每个人能承受的和实际会犯的错不一样。这里让每个
参数可调，并且**参数会真的挡住计算**（仓位算出来超限就压回上限并说明），
而不是印在帮助文档里让人自己遵守。

一条特殊规则：**连亏后自动降险**。这是唯一一个不需要用户判断力就能生效的
风控——人在连亏之后最想做的事是加倍捞回来，而那正是账户归零的标准路径。
连亏数直接从实盘成交记录读，不靠自己申报。
"""
import logging

from storage import data, save_data

log = logging.getLogger(__name__)

# 默认值：保守但不至于做不了事
DEFAULTS = {
    "risk_pct": 0.5,          # 单笔风险占权益
    "max_risk_pct": 1.0,      # 单笔风险上限（手动指定也不许超过）
    "max_same_side_pct": 200, # 同向名义占权益上限
    "max_lev": 20,            # 自己给自己设的杠杆上限
    "max_open": 5,            # 最多同时持仓数
    "streak_cut": 3,          # 连亏几笔开始降险
    "streak_factor": 0.5,     # 降到原来的多少
}

LABELS = {
    "risk_pct": ("单笔风险", "%", "每单最多亏权益的百分之几"),
    "max_risk_pct": ("单笔风险上限", "%", "手动指定也不许超过它"),
    "max_same_side_pct": ("同向名义上限", "%", "所有同方向仓位名义合计占权益"),
    "max_lev": ("杠杆上限", "x", "自己给自己设的，比交易所限制更严"),
    "max_open": ("最多持仓数", "个", "同时开着的仓不超过这么多"),
    "streak_cut": ("连亏降险触发", "笔", "连亏几笔后自动砍风险"),
    "streak_factor": ("降险系数", "×", "触发后风险降到原来的多少"),
}


def profile(uid):
    """某人的风控参数，缺项用默认值补齐。"""
    p = dict(DEFAULTS)
    p.update((data.get("risk_profile") or {}).get(str(uid)) or {})
    return p


def set_param(uid, key, value):
    if key not in DEFAULTS:
        return False, f"没有这个参数。可设：{'、'.join(DEFAULTS)}"
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, "值要是数字"
    if v <= 0:
        return False, "值要大于 0"
    if key in ("max_open", "streak_cut"):
        v = int(v)
    if key == "streak_factor" and v > 1:
        return False, "降险系数要 ≤1（它是「降到原来的多少」）"
    data.setdefault("risk_profile", {}).setdefault(str(uid), {})[key] = v
    save_data()
    return True, f"已设 {LABELS[key][0]} = {v:g}{LABELS[key][1]}"


def effective_risk(uid, streak=0):
    """考虑连亏之后**实际生效**的单笔风险%，以及为什么。

    连亏是从真实成交记录读出来的，不是用户自评——人对自己刚亏了几笔
    的记忆非常有弹性。
    """
    p = profile(uid)
    base = min(p["risk_pct"], p["max_risk_pct"])
    if streak >= p["streak_cut"]:
        cut = base * p["streak_factor"]
        return cut, (f"已连亏 {streak} 笔（阈值 {p['streak_cut']}），"
                     f"风险自动从 {base:g}% 降到 {cut:g}%。"
                     f"连亏后加倍捞回来是账户归零的标准路径。")
    return base, ""


def check_limits(uid, plan, positions, equity):
    """新单是否违反自己设的限制。返回 [(是否硬拦, 说明)]。

    硬拦的只有"会把账户置于结构性危险"的那几条；其余给警告让人自己决定——
    风控参数是护栏不是牢笼，全部硬拦会让人干脆关掉它。
    """
    p = profile(uid)
    out = []
    n_open = len(positions or [])
    if n_open >= p["max_open"]:
        out.append((True, f"已有 {n_open} 个仓，达到你设的上限 {p['max_open']} 个"))
    if plan and equity > 0:
        side = plan.get("side")
        same = sum(float(x.get("positionValue") or 0) for x in (positions or [])
                   if ("long" if x.get("side") == "Buy" else "short") == side)
        after = (same + plan.get("notional", 0)) / equity * 100
        if after > p["max_same_side_pct"]:
            out.append((True, f"开完同向名义将达权益 {after:.0f}%，"
                              f"超过你设的 {p['max_same_side_pct']:g}%"))
        if plan.get("risk_pct", 0) > p["max_risk_pct"]:
            out.append((True, f"本单风险 {plan['risk_pct']:g}% 超过你设的上限 "
                              f"{p['max_risk_pct']:g}%"))
        if plan.get("lev", 0) > p["max_lev"]:
            out.append((False, f"杠杆 {plan['lev']:g}x 超过你设的 {p['max_lev']:g}x"))
    return out


def render(uid, streak=0):
    p = profile(uid)
    eff, why = effective_risk(uid, streak)
    lines = ["⚙️ *我的风控参数*", "━━━━━━━━━━━━━━"]
    for k in DEFAULTS:
        name, unit, desc = LABELS[k]
        mark = "" if k in ((data.get("risk_profile") or {}).get(str(uid)) or {}) else "（默认）"
        lines.append(f"`{k}`　{name} *{p[k]:g}{unit}*{mark}")
        lines.append(f"　{desc}")
    lines.append("━━━━━━━━━━━━━━")
    lines.append(f"当前生效单笔风险：*{eff:g}%*")
    if why:
        lines.append(f"⚠️ {why}")
    lines.append("")
    lines.append("改：`/riskset risk_pct 0.3`　重置：`/riskset reset`")
    lines.append("参数会真的挡住仓位计算，不是印在文档里让你自己遵守")
    return "\n".join(lines)


async def _streak(uid):
    """从真实成交记录读当前连亏笔数。读不到就当 0（不因此降险）。"""
    try:
        from config import is_admin
        if not is_admin(uid):
            return 0
        from handlers.rstats import _load, compute_stats
        trades, _fund = await _load(14)
        s = compute_stats(trades)
        return (s or {}).get("cur_loss_streak", 0)
    except Exception as e:
        log.debug(f"读连亏笔数失败: {e}")
        return 0


async def risk_profile_cmd(update, context):
    """/riskprofile —— 看我的风控参数与当前生效值。"""
    from handlers.util import safe_reply
    uid = update.effective_user.id
    streak = await _streak(uid)
    await safe_reply(update.message, render(uid, streak), parse_mode="Markdown")


async def risk_set_cmd(update, context):
    """/riskset <参数> <值> —— 改风控参数。"""
    from handlers.util import safe_reply
    uid = update.effective_user.id
    a = context.args or []
    if not a:
        await safe_reply(update.message, render(uid, await _streak(uid)),
                         parse_mode="Markdown")
        return
    if a[0].lower() == "reset":
        (data.get("risk_profile") or {}).pop(str(uid), None)
        save_data()
        await safe_reply(update.message, "已重置为默认风控参数")
        return
    if len(a) < 2:
        await safe_reply(update.message, "用法 `/riskset risk_pct 0.3`",
                         parse_mode="Markdown")
        return
    ok, msg = set_param(uid, a[0].lower(), a[1])
    await safe_reply(update.message, ("✅ " if ok else "❌ ") + msg)
