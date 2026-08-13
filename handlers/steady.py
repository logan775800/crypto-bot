"""缓步增长扫描 —— 找「每天一点点往上磨」的币，不是找涨得最猛的。

和现有两个扫描的区别（别搞混）：
  • /scan     按可交易性排序，看的是**当下**这一刻能不能做；
  • /upstreak 连续 N 天收阳 —— 太严也太噪：连拉三天的爆涨币会中招，
    而 30 天稳涨 25%、中间红了两天的反而选不出来；
  • 本模块看的是**一段时间里的走势质量**：涨得稳不稳，而不是涨得多不多。

核心指标是「对数价格线性回归的 **年化斜率 × R²**」：
  • 斜率 = 涨得多快；
  • R²  = 走得多稳（拟合优度，1 = 完美一条直线）；
  • 相乘 = 「稳中有升」的量化定义。
这是动量策略里的经典做法（Clenow）。为什么不用「N 天涨幅」排序：涨幅只看
首尾两点，一个「跌 20% 再拉 50%」和一个「每天磨 1%」算出来一样，
但前者是你根本拿不住的过山车。R² 就是用来区分这两者的。

用对数价格而不是原始价：复利过程在对数坐标下才是直线，用原价拟合会让
高价段权重过大，同样的百分比走势在不同价位上算出不同的"稳定度"。

三条硬过滤（都是为了把「拉盘」和「缓涨」分开）：
  • 单日最大涨幅占比过高 → 那是某一天拉的，不是磨上来的；
  • 窗口内最大回撤过大   → 过程不平滑，拿着难受；
  • R² 过低              → 根本没有趋势，只是噪音里恰好首尾差了点。
"""
import asyncio
import logging
import math

from handlers import marketdata as md
from handlers import scan

log = logging.getLogger(__name__)

DEFAULT_DAYS = 30       # 回看窗口（自然日）
MIN_DAYS = 14
MAX_DAYS = 120
POOL = 150              # 细算多少个币（按成交额取前 N —— 缓涨要的是能拿住的币）
CONCURRENCY = 8
# 流动性门槛比 /scan 低一档。/scan 找的是"此刻能不能进出"，需要厚盘口；
# 缓涨是拿几周的，中小市值反而是主场——借用 20M 的门槛会把候选池砍到
# 二十几个，选不出东西来。
MIN_TURNOVER = 5_000_000

MIN_R2 = 0.60           # 低于此说明不是趋势，是噪音
MAX_DD = 25.0           # 窗口内最大回撤上限（%）
MAX_SINGLE_DAY = 25.0   # 单日涨幅上限（%）——超了就是拉盘不是磨
MAX_DAY_SHARE = 0.5     # 单日涨幅占总涨幅的比例上限
TOP_SHOW = 10


def linfit_log(closes):
    """对数收盘价的最小二乘拟合。返回 (每日斜率, R²)。

    数据不足或价格非正时返回 (None, None) —— 不猜。
    """
    n = len(closes)
    if n < MIN_DAYS or any(c <= 0 for c in closes):
        return None, None
    ys = [math.log(c) for c in closes]
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None, None
    sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    slope = sxy / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((ys[i] - (slope * xs[i] + intercept)) ** 2 for i in range(n))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, max(0.0, r2)


def max_drawdown(closes):
    """窗口内最大回撤（%）。缓涨的核心是过程平滑，这个数比总涨幅更能说明问题。"""
    peak = closes[0]
    dd = 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            dd = max(dd, (peak - c) / peak * 100)
    return dd


def profile(closes):
    """一段日线 → 走势质量画像。纯函数，方便测。"""
    if len(closes) < MIN_DAYS:
        return None
    slope, r2 = linfit_log(closes)
    if slope is None:
        return None
    # 日斜率 → 年化涨幅%。exp(slope*365)-1，超大值截断避免展示成天文数字
    ann = (math.exp(slope * 365) - 1) * 100
    ann = max(-99.0, min(ann, 100_000.0))
    rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
    ups = sum(1 for r in rets if r > 0)
    total = (closes[-1] / closes[0] - 1) * 100
    best = max(rets) if rets else 0.0
    return {
        "slope": slope, "r2": r2, "ann": ann,
        "score": ann * r2,                 # 稳中有升：涨得快 × 走得稳
        "total": total,
        "up_ratio": ups / len(rets) * 100 if rets else 0,
        "dd": max_drawdown(closes),
        "best_day": best,
        # 单日涨幅占总涨幅的比重：接近 1 说明整段涨幅几乎是某一天拉出来的
        "day_share": (best / total) if total > 0 else None,
        "days": len(closes),
    }


def reject_reason(p):
    """不符合「缓步增长」的原因。返回 None 表示通过。

    这些不是打分项而是硬过滤：一个靠单日暴拉撑起来的涨幅，
    再高的分数也不是「缓步」，混进来会直接毁掉这个榜的意义。
    """
    if p["slope"] <= 0:
        return "趋势向下"
    if p["r2"] < MIN_R2:
        return f"不成趋势(R²{p['r2']:.2f})"
    if p["best_day"] > MAX_SINGLE_DAY:
        return f"单日暴涨{p['best_day']:.0f}%，是拉盘不是磨"
    if p["day_share"] is not None and p["day_share"] > MAX_DAY_SHARE:
        return f"涨幅{p['day_share']*100:.0f}%来自单日，不算缓涨"
    if p["dd"] > MAX_DD:
        return f"最大回撤{p['dd']:.0f}%，过程不平滑"
    return None


# ── 取数 ────────────────────────────────────────────────────────
async def _daily(sym, days):
    try:
        r = await md._get("/v5/market/kline", {
            "category": md.CAT, "symbol": sym, "interval": "D",
            "limit": min(days + 2, 200)})
        rows = (r.get("list") or [])[::-1]
        # 最后一根是**当天未走完**的日线，丢掉——半天的涨跌会污染整段拟合
        closes = [float(x[4]) for x in rows][:-1]
        return closes[-days:] if len(closes) > days else closes
    except Exception as e:
        log.debug(f"缓涨扫描取 {sym} 日线失败: {e}")
        return []


async def _one(sym, turnover, days, sem):
    async with sem:
        closes = await _daily(sym, days)
    if len(closes) < MIN_DAYS:
        return None
    p = profile(closes)
    if not p:
        return None
    p["symbol"] = sym
    p["turnover"] = turnover
    p["price"] = closes[-1]
    p["reject"] = reject_reason(p)
    return p


async def run(days=DEFAULT_DAYS, limit=POOL, crypto_only=True):
    """返回 (通过筛选的[按稳中有升排序], 被否掉的[带原因], 扫描总数, 排除的非加密数)。"""
    r, types = await asyncio.gather(
        md._get("/v5/market/tickers", {"category": md.CAT}),
        md.symbol_types(), return_exceptions=True)
    if isinstance(r, Exception):
        raise r
    types = {} if isinstance(types, Exception) else types
    rows, skipped = [], 0
    for t in (r.get("list") or []):
        s = t.get("symbol") or ""
        if not s.endswith("USDT"):
            continue
        try:
            tv = float(t.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if tv < MIN_TURNOVER:
            continue
        # 「稳」这个筛选天然会把代币化股票和贵金属顶到前面——它们本来就比
        # 加密币稳得多。对做加密永续的人那是噪音，默认排除。
        if crypto_only and types.get(s, "") != "":
            skipped += 1
            continue
        rows.append((s, tv))
    # 这里按成交额降序取样，和 /scan 刻意相反：/scan 找的是"今天动了的"，
    # 而缓涨要的是"能拿得住的"——流动性差的币磨上去也出不来。
    rows.sort(key=lambda x: -x[1])
    rows = rows[:limit]
    sem = asyncio.Semaphore(CONCURRENCY)
    res = await asyncio.gather(*[_one(s, tv, days, sem) for s, tv in rows],
                               return_exceptions=True)
    good, bad = [], []
    for x in res:
        if not x or isinstance(x, Exception):
            continue
        (bad if x["reject"] else good).append(x)
    good.sort(key=lambda p: -p["score"])
    return good, bad, len(rows), skipped


def render(good, bad, scanned, days, skipped=0):
    if not good:
        return (f"📉 近 {days} 天没有符合「缓步增长」的币（扫了 {scanned} 个）。\n"
                f"要求：趋势向上、R²≥{MIN_R2}、最大回撤<{MAX_DD:g}%、"
                f"无单日暴涨>{MAX_SINGLE_DAY:g}%。\n"
                f"整体震荡或普涨普跌的市场里选不出来是正常的——"
                f"这时候没有「稳」可言。")
    lines = [f"🌱 *缓步增长*　近 {days} 天走得最稳的（扫了 {scanned} 个）",
             f"排序 = 年化斜率 × R²（涨得快 × 走得稳），不是按涨幅",
             "━━━━━━━━━━━━━━"]
    from handlers.util import escape_md
    for p in good[:TOP_SHOW]:
        short = p["symbol"].replace("USDT", "")
        lines.append(
            f"*{escape_md(short)}*　{md.f(p['price'])}　"
            f"{days}天 {p['total']:+.1f}%　*{p['score']:.0f}分*")
        lines.append(
            f"　R² {p['r2']:.2f}（越接近1越像一条直线）｜"
            f"上涨天数 {p['up_ratio']:.0f}%｜最大回撤 {p['dd']:.1f}%")
        lines.append(
            f"　最大单日 {p['best_day']:+.1f}%｜年化斜率 {p['ann']:+.0f}%｜"
            f"成交额 {p['turnover']/1e6:.0f}M")
        lines.append("")
    if bad:
        near = sorted(bad, key=lambda p: -p.get("score", 0))[:3]
        lines.append("被否掉的（离得最近的几个）：")
        for p in near:
            lines.append(f"　{p['symbol'].replace('USDT','')}　{p['reject']}")
        lines.append("")
    lines.append(f"口径：日线**已收盘**的最近 {days} 天（当天那根不算，"
                 f"半天的涨跌会污染拟合）；对数价格做最小二乘。")
    lines.append("为什么不按涨幅排：涨幅只看首尾两点，"
                 "「跌20%再拉50%」和「每天磨1%」算出来一样，但前者你拿不住。")
    lines.append("⚠️ 走势稳 ≠ 会继续稳，只是筛选起点，不是买入建议")
    return "\n".join(lines)


# ── 按钮面板 ────────────────────────────────────────────────────
# 窗口长度是这个功能最需要调的参数：30 天看的是当下这波，90 天看的是
# 能不能一直磨。只给默认值等于只给了一半功能，所以结果卡上直接带切换按钮，
# 换窗口不用退回菜单重来。
DAY_CHOICES = (14, 30, 60, 90)


def days_kb(current=DEFAULT_DAYS, show_all=False):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    row = [InlineKeyboardButton(f"{'✅' if d == current else ''}{d}天",
                                callback_data=f"stdy:{d}:{1 if show_all else 0}")
           for d in DAY_CHOICES]
    toggle = ("🪙 仅加密（点此含股票）" if not show_all
              else "📈 含股票商品（点此仅加密）")
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton(toggle,
                              callback_data=f"stdy:{current}:{0 if show_all else 1}")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="cat_scan")],
    ])


async def on_button(query, context):
    """处理 stdy:<天数>:<是否含股票>。由 menu.button_handler 转发。"""
    from handlers.util import safe_edit
    try:
        _p, d, allf = query.data.split(":")
        days = max(MIN_DAYS, min(int(d), MAX_DAYS))
        show_all = allf == "1"
    except (ValueError, IndexError):
        await query.answer("参数看不懂")
        return
    await query.answer(f"扫描近 {days} 天…")
    await safe_edit(query, f"🌱 扫描近 {days} 天走势最稳的币…（约 20~40 秒）")
    try:
        good, bad, scanned, skipped = await run(days, crypto_only=not show_all)
    except Exception as e:
        log.error(f"缓涨面板扫描失败: {e}")
        await safe_edit(query, f"扫描失败：{str(e)[:80]}",
                        reply_markup=days_kb(days, show_all))
        return
    await safe_edit(query, render(good, bad, scanned, days, skipped),
                    reply_markup=days_kb(days, show_all), parse_mode="Markdown")


USAGE = (
    "🌱 *缓步增长扫描*\n\n"
    "`/steady`　默认近 30 天\n"
    "`/steady 60`　自定义窗口（14~120 天）\n\n"
    "找的是「每天一点点往上磨」的币，不是涨得最猛的。\n"
    "排序用对数价格回归的**年化斜率 × R²**：涨得快 × 走得稳。\n\n"
    "会排除：单日暴涨拉起来的、回撤太大的、R² 太低（根本不成趋势）的。\n"
    "和 `/upstreak`（连续N天收阳）的区别：那个太严也太噪，"
    "连拉三天的爆涨币会中招，而稳涨一个月、中间红两天的反而选不出来。"
)


async def steady_cmd(update, context):
    """/steady [天数] 缓步增长扫描（稳中有升，不是涨得猛）"""
    from handlers.util import safe_reply
    args = [a.lower() for a in (context.args or [])]
    show_all = "all" in args or "全部" in args
    args = [a for a in args if a not in ("all", "全部")]
    days = DEFAULT_DAYS
    if args:
        if args[0].lower() in ("help", "帮助", "?"):
            await safe_reply(update.message, USAGE, parse_mode="Markdown")
            return
        try:
            days = max(MIN_DAYS, min(int(args[0]), MAX_DAYS))
        except ValueError:
            await safe_reply(update.message, USAGE, parse_mode="Markdown")
            return
    await safe_reply(update.message,
                     f"🌱 扫描近 {days} 天走势最稳的币…（约 20~40 秒）")
    try:
        good, bad, scanned, skipped = await run(days, crypto_only=not show_all)
    except Exception as e:
        log.error(f"/steady 失败: {e}")
        await safe_reply(update.message, f"扫描失败：{str(e)[:80]}")
        return
    await safe_reply(update.message, render(good, bad, scanned, days, skipped),
                     reply_markup=days_kb(days, show_all), parse_mode="Markdown")
