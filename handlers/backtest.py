"""规则回测 —— 「这套打法在这个币上，扣掉成本之后，历史上到底是正还是负」。

和仓库里那个 momentum_backtest.py 不是一回事：那个是 CoinGecko 上的横截面
动量选币脚本；这里回测的是**你实际在用的进场规则**（趋势跟随/回踩/突破），
跑在 Bybit 永续的真实 K 线上，并且扣掉手续费与滑点。

三条让结果不至于骗人的规矩，比策略本身重要：

1. **不许前视**。信号在某根 K 线**收盘**才成立，进场价用**下一根的开盘价**。
   用当根收盘价进场是回测最常见的作弊——实盘你不可能在收盘那一刻成交。
2. **同一根里止损止盈都碰到时，算止损**。日内 K 线不知道先后顺序，
   假设有利的那个先到会把每个策略都美化成圣杯。
3. **成本照扣**。不扣成本的回测里，高频小止损的策略永远最漂亮——
   而那正是实盘上被手续费吃干净的那类。

输出里必须带样本量。20 笔以下的结论不稳，这一点会写在结果里而不是靠用户自觉。
"""
import logging

from handlers import marketdata as md

log = logging.getLogger(__name__)

DEFAULT_BARS = 1000        # 回测取多少根K线
MAX_HOLD = 96              # 最多持有多少根（防止一单挂到天荒地老）
MIN_SAMPLE = 20            # 少于这么多笔，结论不稳
RULES = ("trend", "pullback", "breakout")


def _ema(vals, n):
    return md.ema(vals, n)


def signals(bars, rule):
    """返回该规则下的进场索引列表 [(i, side)]，i 是**信号成立的那根**。

    进场发生在 i+1 的开盘 —— 这个偏移由 simulate 负责，signals 只负责判信号。
    """
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    lo = [b[3] for b in bars]
    c = [b[4] for b in bars]
    out = []
    if len(c) < 210:
        return out
    for i in range(200, len(c) - 1):
        win = c[:i + 1]
        e20, e50 = _ema(win, 20), _ema(win, 50)
        e20p, e50p = _ema(win[:-1], 20), _ema(win[:-1], 50)
        if not all((e20, e50, e20p, e50p)):
            continue
        if rule == "trend":
            # EMA20 上穿 EMA50 且价在两者之上 = 趋势启动
            if e20p <= e50p and e20 > e50 and c[i] > e20:
                out.append((i, "long"))
            elif e20p >= e50p and e20 < e50 and c[i] < e20:
                out.append((i, "short"))
        elif rule == "pullback":
            # 上升结构里回踩 EMA20 并收回 = 顺势回踩
            if e20 > e50 and lo[i] <= e20 and c[i] > e20 and c[i - 1] > e20:
                out.append((i, "long"))
            elif e20 < e50 and h[i] >= e20 and c[i] < e20 and c[i - 1] < e20:
                out.append((i, "short"))
        elif rule == "breakout":
            # 突破前 20 根高点收盘确认
            prior_h = max(h[i - 20:i])
            prior_l = min(lo[i - 20:i])
            if c[i] > prior_h and e20 > e50:
                out.append((i, "long"))
            elif c[i] < prior_l and e20 < e50:
                out.append((i, "short"))
    return out


def simulate(bars, rule, atr_mult=1.5, rr=2.0, cost_pct=0.11, max_hold=MAX_HOLD):
    """把信号跑成一串交易。返回 [trade]，每笔以 **R 倍数** 计价。

    用 R 而不是 USDT：R 是"一个止损"的意思，跟仓位大小无关，
    这样不同币、不同时期的结果可以直接比较。
    cost_pct 是**单边**成本占名义的百分比（手续费+滑点），开平各扣一次。
    """
    o = [b[1] for b in bars]
    h = [b[2] for b in bars]
    lo = [b[3] for b in bars]
    c = [b[4] for b in bars]
    a14 = None
    trades = []
    busy_until = -1
    for i, side in signals(bars, rule):
        if i <= busy_until:
            continue                      # 上一单还没结束，不重复进场
        j = i + 1
        if j >= len(c):
            break
        a14 = md.atr(h[:i + 1], lo[:i + 1], c[:i + 1], 14)
        if not a14 or a14 <= 0:
            continue
        entry = o[j]                      # 下一根开盘进场，不用当根收盘
        risk = a14 * atr_mult
        if risk <= 0 or entry <= 0:
            continue
        if side == "long":
            stop, tp = entry - risk, entry + risk * rr
        else:
            stop, tp = entry + risk, entry - risk * rr
        outcome, bars_held = None, 0
        for k in range(j, min(j + max_hold, len(c))):
            bars_held = k - j + 1
            hit_stop = (lo[k] <= stop) if side == "long" else (h[k] >= stop)
            hit_tp = (h[k] >= tp) if side == "long" else (lo[k] <= tp)
            if hit_stop and hit_tp:
                outcome = -1.0            # 同根都碰到 → 按最坏算
                break
            if hit_stop:
                outcome = -1.0
                break
            if hit_tp:
                outcome = rr
                break
        if outcome is None:               # 到期未触及，按收盘平
            last = c[min(j + max_hold - 1, len(c) - 1)]
            move = (last - entry) if side == "long" else (entry - last)
            outcome = move / risk
        # 成本换算成 R：成本占名义的比例 ÷ 止损距离占名义的比例
        stop_pct = risk / entry * 100
        cost_r = (cost_pct * 2) / stop_pct if stop_pct > 0 else 0
        trades.append({"i": i, "side": side, "entry": entry, "stop": stop,
                       "tp": tp, "gross_r": outcome, "net_r": outcome - cost_r,
                       "cost_r": cost_r, "bars": bars_held,
                       "stop_pct": stop_pct})
        busy_until = j + bars_held
    return trades


def stats(trades):
    """把交易列表压成能下判断的几个数。毛/净并排——差距本身就是结论。"""
    if not trades:
        return None
    n = len(trades)
    gross = [t["gross_r"] for t in trades]
    net = [t["net_r"] for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    eq, peak, dd = 0.0, 0.0, 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": n,
        "win_rate": len(wins) / n * 100,
        "gross_exp": sum(gross) / n,
        "net_exp": sum(net) / n,
        "cost_r": sum(t["cost_r"] for t in trades) / n,
        "total_r": sum(net),
        "max_dd_r": dd,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
        "avg_bars": sum(t["bars"] for t in trades) / n,
        "avg_stop_pct": sum(t["stop_pct"] for t in trades) / n,
    }


async def run(symbol, interval="1h", rule="trend", rr=2.0, cost_pct=0.11,
              limit=DEFAULT_BARS):
    """取K线 → 跑规则 → 出统计。返回 (stats, bars数, 错误信息|None)。"""
    sym = md.norm(symbol)
    if rule not in RULES:
        return None, 0, f"规则只能是 {'/'.join(RULES)}"
    try:
        r = await md._get("/v5/market/kline", {
            "category": md.CAT, "symbol": sym,
            "interval": md.INTERVALS.get(interval, "60"), "limit": min(limit, 1000)})
        rows = (r.get("list") or [])[::-1]
    except Exception as e:
        return None, 0, f"取K线失败：{str(e)[:60]}"
    if len(rows) < 250:
        return None, len(rows), (f"K线只有 {len(rows)} 根，不够跑回测"
                                 f"（至少 250 根，规则要用到 EMA50 和 200 根预热）")
    bars = [(int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]))
            for x in rows]
    trades = simulate(bars, rule, rr=rr, cost_pct=cost_pct)
    return stats(trades), len(bars), None


RULE_DESC = {
    "trend": "趋势跟随：EMA20 上穿/下穿 EMA50 且价格站对边",
    "pullback": "顺势回踩：上升结构里回踩 EMA20 并收回",
    "breakout": "突破：收盘突破前 20 根高/低点，且大趋势同向",
}


def render(s, symbol, interval, rule, bars, rr, cost_pct):
    if not s:
        return "这段历史里这条规则一次信号都没触发。换周期或换规则试试。"
    warn = ""
    if s["n"] < MIN_SAMPLE:
        warn = (f"\n⚠️ *只有 {s['n']} 笔样本*，这个结论不稳定，"
                f"别据此下结论——换更长周期或更多K线再看。")
    exp_emoji = "✅" if s["net_exp"] > 0 else "❌"
    pf = "∞" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
    return "\n".join([
        f"🧪 *规则回测*　{symbol} {interval}",
        f"{RULE_DESC.get(rule, rule)}",
        f"止损 1.5×ATR｜止盈 {rr:g}R｜单边成本 {cost_pct:g}%",
        f"样本 {bars} 根K线，共 *{s['n']}* 笔",
        "━━━━━━━━━━━━━━",
        f"胜率 {s['win_rate']:.1f}%｜盈利因子 {pf}",
        f"毛期望 {s['gross_exp']:+.3f} R/笔",
        f"{exp_emoji} *净期望 {s['net_exp']:+.3f} R/笔*　← 这个为正才有得做",
        f"　成本吃掉 {s['cost_r']:.3f} R/笔"
        + f"（平均止损距离 {s['avg_stop_pct']:.2f}%）",
        f"累计 {s['total_r']:+.1f} R｜最大回撤 {s['max_dd_r']:.1f} R",
        f"平均持有 {s['avg_bars']:.0f} 根",
        warn,
        "",
        "_口径：信号收盘成立、**下一根开盘**进场（不许前视）；"
        "同一根内止损止盈都触及时**按止损算**（不知先后，取最坏）。_",
        "⚠️ 历史不代表未来，回测正期望也不等于实盘能做出来",
    ])


USAGE = (
    "🧪 *规则回测* —— 扣掉成本之后，这套打法历史上是正还是负\n\n"
    "`/backtest BTC 1h trend`\n"
    "　币 周期 规则`[ 止盈R][ 单边成本%]`\n\n"
    "*规则*\n"
    + "\n".join(f"　`{k}`　{v}" for k, v in RULE_DESC.items()) + "\n\n"
    "默认止损 1.5×ATR、止盈 2R、单边成本 0.11%（吃单手续费+典型滑点）。\n"
    "结果以 **R** 计（1R = 一个止损），跟仓位大小无关，不同币能直接比。\n\n"
    "口径：不许前视（下一根开盘进场）、同根止损止盈都碰按止损算、成本照扣。"
)


async def backtest_cmd(update, context):
    from handlers.util import safe_reply
    a = context.args or []
    if not a:
        await safe_reply(update.message, USAGE, parse_mode="Markdown")
        return
    sym = a[0].upper()
    interval = a[1] if len(a) > 1 else "1h"
    rule = (a[2] if len(a) > 2 else "trend").lower()
    try:
        rr = float(a[3]) if len(a) > 3 else 2.0
        cost = float(a[4]) if len(a) > 4 else 0.11
    except ValueError:
        await safe_reply(update.message, "止盈R和成本%要是数字。\n\n" + USAGE,
                         parse_mode="Markdown")
        return
    await safe_reply(update.message, f"🧪 回测 {sym} {interval} {rule} …")
    s, bars, err = await run(sym, interval, rule, rr, cost)
    if err:
        await safe_reply(update.message, f"回测失败：{err}")
        return
    await safe_reply(update.message, render(s, sym, interval, rule, bars, rr, cost),
                     parse_mode="Markdown")
