"""多币种机会扫描 —— 按「可交易性」排序，不是按涨幅。

为什么不按涨幅排：涨幅榜第一名往往是最不该碰的那个——已经拉完、资金费拉爆、
盘口薄得吃两万U就滑 1%。那是**别人的利润**，不是你的机会。

所以这里给四个独立维度打分，并且**分开显示**而不是揉成一个数：
  • 趋势 —— 多周期 EMA 排列是否一致、斜率是否还在走
  • 流动性 —— 24h 成交额 + 盘口实际深度（能不能进得去出得来）
  • 拥挤 —— 资金费极端度 + OI 暴增（分数越高越危险，是**减分项**）
  • 执行 —— 价差 + 按参考名义实算的滑点（进场就先亏多少）

综合分不是四项平均，而是**带否决**：流动性或执行质量不及格时，趋势再漂亮也
直接压到"不建议"。这条规则的存在是因为小币最容易在这两项上把人埋了。

扫描是分层的：先一次拉全市场 ticker 做粗筛（便宜），再对候选做多周期取数
（贵）。不这么分层的话，一次 /scan 要打上千个请求。
"""
import asyncio
import logging

from handlers import marketdata as md

log = logging.getLogger(__name__)

MIN_TURNOVER = 20_000_000      # 24h 成交额下限(USDT)。低于这个的深度撑不住实盘
POOL = 40                      # 粗筛保留多少个进入细算
DEEP = 12                      # 细算多少个（每个要打 4 个接口，这是耗时大头）
CONCURRENCY = 6                # 并发上限，别把交易所打出限流
REF_NOTIONAL = 5000            # 算执行质量用的参考名义(USDT)

# 拥挤阈值：单期资金费到这个量级就说明一边已经很挤了
FUNDING_HOT = 0.0005           # 0.05%/期 ≈ 年化 55%


def _clamp(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def score_trend(tf):
    """多周期趋势一致性。tf = {周期: {"align": 1/0/-1, "slope": %}}。

    一致性比强度重要：4h 多、1h 空、15m 多 —— 这种"各说各话"的结构，
    无论哪个方向进去都是在猜，不是在跟。
    """
    if not tf:
        return 0.0, "无数据"
    aligns = [v.get("align", 0) for v in tf.values()]
    if not aligns:
        return 0.0, "无数据"
    agree = sum(aligns)
    n = len(aligns)
    base = abs(agree) / n * 70                     # 全一致给满 70
    slope = sum(abs(v.get("slope", 0) or 0) for v in tf.values()) / n
    base += _clamp(slope * 8, 0, 30)               # 斜率越陡越像真趋势
    direction = "多头" if agree > 0 else ("空头" if agree < 0 else "分歧")
    if abs(agree) < n:
        direction += "(周期不一致)"
    return _clamp(base), direction


def score_liquidity(turnover, depth):
    """成交额 + 盘口深度。两者都要——成交额大但盘口薄的币照样进不去。"""
    t = _clamp((turnover / 200_000_000) * 60, 0, 60) if turnover else 0
    d = _clamp((depth / 500_000) * 40, 0, 40) if depth else 0
    return _clamp(t + d)


def score_crowding(funding, oi_change):
    """拥挤度：**分数越高越危险**。资金费极端 + OI 暴增 = 一边人太多。"""
    f = _clamp(abs(funding or 0) / FUNDING_HOT * 50, 0, 60)
    o = _clamp(abs(oi_change or 0) / 20 * 40, 0, 40)   # 近12期 OI 变化 20% 记满
    return _clamp(f + o)


def score_execution(spread_pct, slip_pct, partial):
    """执行质量：进场就先亏掉多少。部分成交直接腰斩——那意味着计划做不完整。"""
    s = _clamp(100 - (spread_pct or 0) * 400, 0, 50)      # 0.05% 价差扣 20 分
    sl = _clamp(50 - (slip_pct or 0) * 100, 0, 50)        # 0.3% 滑点扣 30 分
    total = s + sl
    if partial:
        total *= 0.5
    return _clamp(total)


def overall(trend, liq, crowd, exec_q):
    """综合分 + 一句话结论。**带否决**：流动性/执行不及格时趋势不算数。"""
    raw = trend * 0.35 + liq * 0.25 + exec_q * 0.25 + (100 - crowd) * 0.15
    vetoes = []
    if liq < 30:
        vetoes.append("流动性不足")
    if exec_q < 30:
        vetoes.append("执行成本过高")
    if crowd > 80:
        vetoes.append("拥挤度极高")
    if vetoes:
        raw = min(raw, 35)
    if raw >= 70 and not vetoes:
        verdict = "✅ 可交易性好"
    elif raw >= 50 and not vetoes:
        verdict = "🟡 可以看，但要控仓"
    else:
        verdict = "❌ 不建议" + ("（" + "、".join(vetoes) + "）" if vetoes else "")
    return _clamp(raw), verdict


# ── 取数 ─────────────────────────────────────────────────────────
async def _pool():
    """粗筛：一次拉全市场 ticker → 过流动性门槛 → 按**动过**排序。

    这里的排序方式很关键。按成交额降序取的话，进入细算的永远是 BTC/ETH 那批，
    四个维度全部满分，扫描结果每天长得一模一样，等于没有扫描。
    真正的机会在「过了流动性门槛、但最近动过」的中小市值里。

    所以：**流动性是门槛（硬性），波动幅度是排序（选谁进细算）**。
    这不会让它退化成涨幅榜——涨幅只决定谁被看一眼，最终排序由四维打分决定，
    拥挤/执行不及格照样被否决。
    """
    r = await md._get("/v5/market/tickers", {"category": md.CAT})
    rows = []
    for t in (r.get("list") or []):
        s = t.get("symbol") or ""
        if not s.endswith("USDT"):
            continue
        try:
            turnover = float(t.get("turnover24h") or 0)
            move = abs(float(t.get("price24hPcnt") or 0)) * 100
        except (TypeError, ValueError):
            continue
        if turnover < MIN_TURNOVER:
            continue
        rows.append((s, t, turnover, move))
    rows.sort(key=lambda x: -x[3])          # 按波动幅度，不是成交额
    return [(s, t, tv) for s, t, tv, _m in rows[:POOL]]


async def _tf_snapshot(sym, iv, limit=120):
    """某周期的 EMA 排列与斜率。取不到就返回 None —— 缺周期不猜。"""
    try:
        r = await md._get("/v5/market/kline", {
            "category": md.CAT, "symbol": sym,
            "interval": md.INTERVALS[iv], "limit": limit})
        c = [float(x[4]) for x in (r.get("list") or [])[::-1]]
        if len(c) < 55:
            return None
        e20, e50 = md.ema(c, 20), md.ema(c, 50)
        if not (e20 and e50):
            return None
        last = c[-1]
        align = 1 if last > e20 > e50 else (-1 if last < e20 < e50 else 0)
        prev = md.ema(c[:-10], 20)
        slope = ((e20 - prev) / prev * 100) if prev else 0
        return {"align": align, "slope": slope, "close": last}
    except Exception as e:
        log.debug(f"扫描取 {sym} {iv} 失败: {e}")
        return None


async def _oi_change(sym):
    try:
        r = await md._get("/v5/market/open-interest", {
            "category": md.CAT, "symbol": sym, "intervalTime": "15min", "limit": 13})
        rows = (r.get("list") or [])[::-1]
        if len(rows) < 5:
            return None
        a, b = float(rows[0]["openInterest"]), float(rows[-1]["openInterest"])
        return (b - a) / a * 100 if a else None
    except Exception:
        return None


async def _book_quality(sym):
    """价差 + 参考名义下的滑点 + 深度。执行质量的全部依据。"""
    try:
        from handlers import econ
        r = await md._get("/v5/market/orderbook",
                          {"category": md.CAT, "symbol": sym, "limit": 200})
        bids = [(float(p), float(s)) for p, s in (r.get("b") or [])]
        asks = [(float(p), float(s)) for p, s in (r.get("a") or [])]
        if not bids or not asks:
            return None
        mid = (bids[0][0] + asks[0][0]) / 2
        spread = (asks[0][0] - bids[0][0]) / mid * 100
        s = econ.slippage(asks, REF_NOTIONAL, asks[0][0])
        return {"spread": spread, "slip": s["pct"] or 0,
                "partial": s["partial"], "depth": s["depth"]}
    except Exception as e:
        log.debug(f"扫描取 {sym} 盘口失败: {e}")
        return None


async def _deep(sym, ticker, turnover, sem):
    """对单个候选做细算。任何一项缺失都如实标 None，不用默认值糊过去。"""
    async with sem:
        tf_res, oi, book = await asyncio.gather(
            asyncio.gather(*[_tf_snapshot(sym, iv) for iv in ("4h", "1h", "15m")]),
            _oi_change(sym), _book_quality(sym), return_exceptions=True)
    tf = {}
    if not isinstance(tf_res, Exception):
        for iv, v in zip(("4h", "1h", "15m"), tf_res):
            if v:
                tf[iv] = v
    oi = None if isinstance(oi, Exception) else oi
    book = None if isinstance(book, Exception) else book
    try:
        funding = float(ticker.get("fundingRate") or 0)
        chg = float(ticker.get("price24hPcnt") or 0) * 100
        price = float(ticker.get("lastPrice") or 0)
    except (TypeError, ValueError):
        return None

    trend, direction = score_trend(tf)
    liq = score_liquidity(turnover, (book or {}).get("depth"))
    crowd = score_crowding(funding, oi)
    ex = score_execution((book or {}).get("spread"), (book or {}).get("slip"),
                         (book or {}).get("partial", False))
    total, verdict = overall(trend, liq, crowd, ex)
    missing = []
    if not tf:
        missing.append("K线")
    if oi is None:
        missing.append("OI")
    if not book:
        missing.append("盘口")
    return {
        "symbol": sym, "price": price, "chg": chg, "turnover": turnover,
        "trend": trend, "direction": direction, "liq": liq, "crowd": crowd,
        "exec": ex, "total": total, "verdict": verdict,
        "funding": funding, "oi_change": oi,
        "slip": (book or {}).get("slip"), "partial": (book or {}).get("partial"),
        "missing": missing,
    }


async def run(limit=DEEP):
    """完整扫描。返回按可交易性排序的结果。"""
    pool = await _pool()
    if not pool:
        return []
    sem = asyncio.Semaphore(CONCURRENCY)
    res = await asyncio.gather(
        *[_deep(s, t, tv, sem) for s, t, tv in pool[:limit]], return_exceptions=True)
    out = [r for r in res if r and not isinstance(r, Exception)]
    out.sort(key=lambda r: -r["total"])
    return out


def render(rows, limit=8):
    if not rows:
        return "扫描没拿到结果（行情源异常或全市场成交额都低于门槛）。"
    lines = [f"🔍 *机会扫描*　按**可交易性**排序，不是按涨幅",
             f"_粗筛成交额≥{MIN_TURNOVER/1e6:.0f}M，细算前 {len(rows)} 个_",
             "━━━━━━━━━━━━━━"]
    for r in rows[:limit]:
        short = r["symbol"].replace("USDT", "")
        from handlers.util import escape_md
        lines.append(f"*{escape_md(short)}*　{md.f(r['price'])}　"
                     f"24h {r['chg']:+.1f}%　*{r['total']:.0f}分*")
        lines.append(
            f"　趋势 {r['trend']:.0f}({r['direction']})｜流动性 {r['liq']:.0f}｜"
            f"拥挤 {r['crowd']:.0f}｜执行 {r['exec']:.0f}")
        extra = []
        if r["slip"] is not None:
            extra.append(f"滑点{r['slip']:.2f}%")
        if r["funding"]:
            extra.append(f"费率{r['funding']*100:+.3f}%")
        if r["oi_change"] is not None:
            extra.append(f"OI{r['oi_change']:+.0f}%")
        if extra:
            lines.append("　" + "｜".join(extra) + f"　按名义 {REF_NOTIONAL:,}U 试算")
        lines.append(f"　{r['verdict']}")
        if r["missing"]:
            lines.append(f"　⚠️ 缺 {'、'.join(r['missing'])}，该维度未计入")
        lines.append("")
    lines.append("_拥挤分越高越危险。流动性或执行不及格会直接否决，趋势再好也不算_")
    lines.append("⚠️ 只是可交易性排序，不是买入建议")
    return "\n".join(lines)


async def scan_cmd(update, context):
    """/scan —— 全市场按可交易性排序。"""
    from handlers.util import safe_reply
    await safe_reply(update.message,
                     f"🔍 扫描中…（粗筛全市场 → 细算前 {DEEP} 个，约 15~30 秒）")
    try:
        rows = await run()
    except Exception as e:
        log.error(f"/scan 失败: {e}")
        await safe_reply(update.message, f"扫描失败：{str(e)[:80]}")
        return
    await safe_reply(update.message, render(rows), parse_mode="Markdown")
