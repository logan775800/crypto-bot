"""周报 + 交易行为画像 —— 「这周和上周比，我的行为变了没有」。

单周的胜率是噪音：20 笔交易的胜率在 35%~65% 之间随机跳，看它做决策等于看抛硬币。
有意义的是**行为指标的趋势**：平均持仓时长、平均杠杆、单笔风险离散度、
亏损归因的构成——这些东西比盈亏稳定得多，而且是你真正能控制的。

所以周报的主体不是"这周赚了多少"，而是"你的行为往哪个方向漂了"。
盈亏放在最后，因为它是结果不是原因。

快照落盘，用于跨周对比；没有上周数据时如实说"第一周，只有基线没有趋势"。
"""
import logging
import time

from storage import data, save_data

log = logging.getLogger(__name__)

WEEK = 7 * 86400


def behavior(trades):
    """从成交记录算出一组**行为**指标（不是盈亏指标）。

    选取标准：能被用户直接控制、且比盈亏稳定。「平均持仓时长」你说了算，
    「这周赚多少」你说了不算。
    """
    if not trades:
        return None
    n = len(trades)
    durs = [t["dur"] for t in trades if t.get("dur") is not None]
    levs = [t["lev"] for t in trades if (t.get("lev") or 0) > 0]
    values = [t["value"] for t in trades if (t.get("value") or 0) > 0]
    losses = [abs(t["pnl"]) for t in trades if t["pnl"] < 0]
    alt = sum(1 for t in trades if t["symbol"] not in ("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    longs = sum(1 for t in trades if t["side"] == "long")
    # 单笔亏损的离散度：止损纪律的直接体现。纪律好的人这个数很小，
    # 忽大忽小说明有时候在扛单——扛单是账户最常见的死法
    loss_cv = None
    if len(losses) >= 3:
        m = sum(losses) / len(losses)
        if m > 0:
            var = sum((x - m) ** 2 for x in losses) / len(losses)
            loss_cv = (var ** 0.5) / m
    return {
        "n": n,
        "avg_dur": (sum(durs) / len(durs)) if durs else None,
        "avg_lev": (sum(levs) / len(levs)) if levs else None,
        "avg_value": (sum(values) / len(values)) if values else None,
        "loss_cv": loss_cv,
        "alt_ratio": alt / n * 100,
        "long_ratio": longs / n * 100,
        "pnl": sum(t["pnl"] for t in trades),
    }


def _arrow(cur, prev, lower_better=True, pct=8):
    """变化箭头。变动小于阈值一律算持平——把噪音读成趋势比不看还糟。"""
    if cur is None or prev is None or prev == 0:
        return "—", ""
    d = (cur - prev) / abs(prev) * 100
    if abs(d) < pct:
        return "→", f"{d:+.0f}%"
    good = (d < 0) if lower_better else (d > 0)
    return ("✅" if good else "⚠️"), f"{d:+.0f}%"


def compare(cur, prev):
    """本周 vs 上周的行为漂移。返回渲染好的行。"""
    if not cur:
        return ["这周没有已平仓交易。"]
    rows = []

    def line(label, val, prev_val, fmt, lower_better=True, note=""):
        if val is None:
            return
        mark, d = _arrow(val, prev_val, lower_better)
        rows.append(f"　{label} {fmt(val)}　{mark} {d}" + (f"　{note}" if note else ""))

    line("交易笔数", cur["n"], (prev or {}).get("n"), lambda x: f"{x:.0f}笔",
         lower_better=True, note="笔数暴涨常伴随质量下降")
    line("平均持仓", cur["avg_dur"], (prev or {}).get("avg_dur"),
         lambda x: f"{x/60:.0f}分钟", lower_better=False,
         note="太短=追进去被打脸，太长=在扛")
    line("平均杠杆", cur["avg_lev"], (prev or {}).get("avg_lev"),
         lambda x: f"{x:.1f}x")
    line("平均名义", cur["avg_value"], (prev or {}).get("avg_value"),
         lambda x: f"{x:,.0f}U")
    line("亏损离散度", cur["loss_cv"], (prev or {}).get("loss_cv"),
         lambda x: f"{x:.2f}", note="越小说明止损越守纪律；忽大忽小=在扛单")
    line("山寨占比", cur["alt_ratio"], (prev or {}).get("alt_ratio"),
         lambda x: f"{x:.0f}%")
    line("做多占比", cur["long_ratio"], (prev or {}).get("long_ratio"),
         lambda x: f"{x:.0f}%", lower_better=False,
         note="长期严重偏向一边=在赌方向不是在跟结构")
    return rows


def _snap_key(chat_id):
    return str(chat_id)


def build(trades, prev_snap, days=7):
    from handlers.rstats import compute_stats, attribution
    cur = behavior(trades)
    s = compute_stats(trades)
    lines = [f"📅 *交易周报*　近 {days} 天", "━━━━━━━━━━━━━━"]
    if not cur:
        lines.append("这周没有已平仓交易。没有数据就没有结论——这是好事，不是坏事。")
        return "\n".join(lines), cur
    lines.append("*行为画像*（和上周比）")
    if not prev_snap:
        lines.append("　第一周，只有基线没有趋势。下周这里会显示变化。")
    lines += compare(cur, prev_snap)
    lines.append("")
    rows, tot = attribution(trades)
    if rows:
        lines.append(f"*亏损归因*　总亏损 {tot:,.2f} USDT")
        for tag, n, amt in rows[:4]:
            lines.append(f"　{tag}　{n}笔　{amt:,.2f}（{amt/tot*100:.0f}%）")
        lines.append("")
    if s:
        # 盈亏放最后：它是结果不是原因，放前面会让人只盯着它
        lines.append("*结果*（参考，单周盈亏噪音很大）")
        lines.append(f"　{s['n']}笔｜胜率 {s['win_rate']:.0f}%｜"
                     f"总盈亏 {s['total']:+,.2f} USDT")
        lines.append(f"　期望值 {s['expectancy']:+,.2f}/笔")
        if s["n"] < 20:
            lines.append("　样本 <20 笔，胜率和期望值都不稳，别据此改打法")
    lines.append("")
    lines.append("_周报看的是**行为漂移**而不是单周盈亏——"
                 "行为你能控制，盈亏你不能。_")
    return "\n".join(lines), cur


async def weekly_report(context):
    """后台 job：给订阅者推周报。"""
    subs = data.get("weekly_subs") or []
    if not subs:
        return
    from handlers.rstats import _load
    try:
        trades, _fund = await _load(7)
    except Exception as e:
        log.error(f"周报取数失败: {e}")
        return
    snaps = data.setdefault("weekly_snap", {})
    for chat_id in list(subs):
        prev = snaps.get(_snap_key(chat_id))
        text, cur = build(trades, prev)
        if cur:
            snaps[_snap_key(chat_id)] = cur
        try:
            await context.bot.send_message(chat_id=int(chat_id), text=text,
                                           parse_mode="Markdown")
        except Exception as e:
            log.error(f"周报推送失败 {chat_id}: {e}")
    save_data()


async def weekly_cmd(update, context):
    """/weekly —— 立即看一份周报；/weekly on|off 订阅每周自动推送。"""
    from handlers.util import safe_reply
    from config import is_admin
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    a = [x.lower() for x in (context.args or [])]
    subs = data.setdefault("weekly_subs", [])
    # 虚拟盘周报：练手阶段的行为漂移比实盘更值得看——习惯就是那时候养成的。
    # 它读的是自己的虚拟账户，不碰实盘接口，所以不需要管理员权限。
    if a and a[0] in ("v", "虚拟", "模拟"):
        from handlers.rstats import load_virtual
        trades = load_virtual(uid, 7)
        snaps = data.setdefault("weekly_snap", {})
        key = f"v{chat_id}"
        text, cur = build(trades, snaps.get(key), days=7)
        if cur:
            snaps[key] = cur
            save_data()
        await safe_reply(update.message, "🎮 虚拟盘周报\n" + text,
                         parse_mode="Markdown")
        return
    if not is_admin(uid):
        await safe_reply(update.message,
                         "实盘周报基于真实成交记录，仅管理员可用。\n"
                         "虚拟盘周报发 `/weekly v`（不需要权限）", parse_mode="Markdown")
        return
    if a and a[0] in ("on", "订阅"):
        if chat_id not in subs:
            subs.append(chat_id)
            save_data()
        await safe_reply(update.message, "✅ 已订阅每周自动周报（每周一推送）")
        return
    if a and a[0] in ("off", "取消"):
        data["weekly_subs"] = [c for c in subs if c != chat_id]
        save_data()
        await safe_reply(update.message, "已取消周报订阅")
        return
    await safe_reply(update.message, "📅 生成周报中…")
    from handlers.rstats import _load
    try:
        trades, _fund = await _load(7)
    except Exception as e:
        await safe_reply(update.message, f"取数失败：{str(e)[:80]}")
        return
    snaps = data.setdefault("weekly_snap", {})
    prev = snaps.get(_snap_key(chat_id))
    text, cur = build(trades, prev)
    if cur:
        snaps[_snap_key(chat_id)] = cur
        save_data()
    await safe_reply(update.message, text, parse_mode="Markdown")
