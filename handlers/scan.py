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
# ATR 可接受区间（占价格%）。太低=没波动，赚不回成本；太高=止损必须放很远，
# 同样风险下仓位小到没意义，而且插针能扫掉任何合理止损。
ATR_MIN, ATR_MAX = 0.6, 8.0
# 净盈亏比过滤：按 1.5×ATR 止损、**3R 结构目标**试算，要求净值 ≥2.0。
# 目标必须高于门槛——第一版用 2R 目标配 2.0 门槛，扣完成本必然 <2，
# 结果全市场每个币都被这一条否掉，等于没有区分度。
# 3R 目标是"一个像样的结构位"的合理代理；能不能剩下 2R 才是要考的。
PROBE_RR = 3.0
MIN_NET_RR = 2.0
BTC_ALIGN_PCT = 1.5            # BTC 15m 动这么多以上时，反向的山寨机会要打折

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
    # 方向要**过半**才敢叫。1 票多头 + 2 票缠绕就标「多头」是过度自信——
    # 实测 ACE 24h 跌 22.6% 却被标成多头，正是这么来的。
    if abs(agree) * 2 <= n:
        direction = "分歧"
    else:
        direction = "多头" if agree > 0 else "空头"
        if abs(agree) < n:
            direction += "(周期不一致)"
    return _clamp(base), direction


import math

DEPTH_BAND = 0.5        # 深度统计的价格带：中价 ±0.5%


def score_liquidity(turnover, depth):
    """成交额 + 盘口深度，**对数刻度**。

    第一版用线性（÷2亿、÷50万），结果是除了 BTC 那几个，全市场都趴在 20 分
    以下，于是 8 个候选里 6 个被「流动性不足」否决——而它们全都通过了 2000 万
    的粗筛门槛。两套标准自相矛盾，扫描器实际输出的是"什么都别做"。

    流动性天然跨数量级（2000 万到 25 亿差两个量级），只能用对数。
    刻度按真实数据标定：门槛 2000 万给基础分，每涨一个量级加一档。
    """
    t = 0.0
    if turnover and turnover > 0:
        # 2000万→19分，2亿→37，20亿→55
        rel = math.log10(max(turnover, MIN_TURNOVER) / MIN_TURNOVER) / 2
        t = _clamp(55 * min(1.0, 0.35 + 0.65 * rel), 0, 55)
    d = 0.0
    if depth and depth > 0:
        # 带内深度 1万→0分，10万→18，100万→36，250万以上→满 45
        d = _clamp(45 * (math.log10(depth / 10_000) / 2.5), 0, 45)
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


# 单期资金费到这个量级 = 极端，单项就该否决，不必等 OI 也暴增。
# 0.2%/期 ≈ 年化 219%，持有拥挤那一边一天就要付掉 0.6% 的名义。
FUNDING_EXTREME = 0.002


def crowded_side(funding, threshold=FUNDING_HOT):
    """资金费告诉你**哪一边挤**：为正是多头在付钱=多头拥挤，为负则反之。

    必须带阈值：+0.005%/期 是常态噪音，不是拥挤。不设阈值的话，
    几乎每个币都会被判成「顺势方向恰是拥挤方」，这条否决就废了。
    """
    if not funding or abs(funding) < threshold:
        return None
    return "多头" if funding > 0 else "空头"


def overall(trend, liq, crowd, exec_q, atr_pct=None, net_rr=None, btc_conflict=False,
            direction=None, funding=None):
    """综合分 + 一句话结论。**带否决**：流动性/执行不及格时趋势不算数。

    后三个参数是「结果导向」的否决，跟前四维不同——它们不打分，只否决：
    波动区间不对、净盈亏比不够、跟 BTC 顶着干，这些不是"分低一点"，
    是"这单本身就不该做"。
    """
    raw = trend * 0.35 + liq * 0.25 + exec_q * 0.25 + (100 - crowd) * 0.15
    vetoes = []
    if liq < 30:
        vetoes.append("流动性不足")
    if exec_q < 30:
        vetoes.append("执行成本过高")
    if crowd > 80:
        vetoes.append("拥挤度极高")
    if atr_pct is not None:
        if atr_pct < ATR_MIN:
            vetoes.append(f"波动太小({atr_pct:.2f}%)，赚不回成本")
        elif atr_pct > ATR_MAX:
            vetoes.append(f"波动过大({atr_pct:.1f}%)，止损放不合理")
    if net_rr is not None and net_rr < MIN_NET_RR:
        vetoes.append(f"净盈亏比仅{net_rr:.2f}")
    if btc_conflict:
        vetoes.append("与BTC方向冲突")
    # 单项极端也要能否决。原来费率项封顶 60、OI 项封顶 40，
    # 单靠费率永远够不到 80 的拥挤否决线——最该拦的情况反而拦不住。
    if funding is not None and abs(funding) >= FUNDING_EXTREME:
        vetoes.append(f"费率极端({funding*100:+.3f}%/期)")
    # 你要做的方向，正好是已经挤满人的那一边
    crowd_side = crowded_side(funding)
    if direction and crowd_side and crowd_side in direction:
        vetoes.append(f"顺势方向恰是拥挤方({crowd_side}在付费率)")
    if vetoes:
        raw = min(raw, 35)
    # 方向不明时不给 ✅。综合分把「能不能做」（流动性/执行）和「该不该做」
    # （趋势/拥挤）加权平均了，于是一个盘口厚但周期打架的币能排到前面——
    # 可"没方向"意味着那里根本没有机会，只有流动性。
    no_direction = direction == "分歧"
    if no_direction and not vetoes:
        raw = min(raw, 55)
    if raw >= 70 and not vetoes and not no_direction:
        verdict = "✅ 可交易性好"
    elif vetoes:
        verdict = "❌ 不建议（" + "、".join(vetoes) + "）"
    elif no_direction:
        verdict = "🟡 只有流动性，没有方向——多周期打架，等它选边"
    elif raw >= 50:
        verdict = "🟡 可以看，但要控仓"
    else:
        verdict = "❌ 不建议"
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
    r, types = await asyncio.gather(
        md._get("/v5/market/tickers", {"category": md.CAT}),
        md.symbol_types(), return_exceptions=True)
    if isinstance(r, Exception):
        raise r
    types = {} if isinstance(types, Exception) else types
    rows = []
    for t in (r.get("list") or []):
        s = t.get("symbol") or ""
        if not s.endswith("USDT"):
            continue
        # 代币化美股/大宗商品（AAPL、XAU 这类）对做加密永续的人是噪音，
        # 而且风险特征不同（跟着美股开收盘跳空、周末流动性枯竭）。
        # 靠 instruments-info 的 symbolType 识别：加密是空串。
        if types.get(s, "") != "":
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
    """某周期的 EMA 排列、斜率、ATR%。取不到就返回 None —— 缺周期不猜。"""
    try:
        r = await md._get("/v5/market/kline", {
            "category": md.CAT, "symbol": sym,
            "interval": md.INTERVALS[iv], "limit": limit})
        rows = (r.get("list") or [])[::-1]
        c = [float(x[4]) for x in rows]
        if len(c) < 55:
            return None
        e20, e50 = md.ema(c, 20), md.ema(c, 50)
        if not (e20 and e50):
            return None
        last = c[-1]
        align = 1 if last > e20 > e50 else (-1 if last < e20 < e50 else 0)
        prev = md.ema(c[:-10], 20)
        slope = ((e20 - prev) / prev * 100) if prev else 0
        h = [float(x[2]) for x in rows]
        lo = [float(x[3]) for x in rows]
        a14 = md.atr(h, lo, c, 14)
        return {"align": align, "slope": slope, "close": last,
                "atr_pct": (a14 / last * 100) if (a14 and last) else None}
    except Exception as e:
        log.debug(f"扫描取 {sym} {iv} 失败: {e}")
        return None


async def _btc_bias():
    """BTC 15m 的方向与幅度。整轮扫描只取一次。取不到返回 0（不据此否决）。"""
    try:
        r = await md._get("/v5/market/kline", {
            "category": md.CAT, "symbol": "BTCUSDT",
            "interval": md.INTERVALS["15m"], "limit": 2})
        rows = (r.get("list") or [])[::-1]
        if len(rows) < 1:
            return 0.0
        o, c = float(rows[-1][1]), float(rows[-1][4])
        return (c - o) / o * 100 if o else 0.0
    except Exception as e:
        log.debug(f"扫描取 BTC 联动失败: {e}")
        return 0.0


def _net_rr(price, atr_pct, slip_pct, taker=0.00055, rr=PROBE_RR):
    """按 1.5×ATR 止损 / 3R 结构目标试算净盈亏比。

    这是全流程里唯一一个**结果导向**的指标：前面几维都在说"环境好不好"，
    这个直接回答"扣完成本还剩多少"。低流动性小币经常前四维看着还行，
    到这一步才现原形——因为滑点和手续费是按名义扣的，跟止损距离不成比例。
    """
    if not (price and atr_pct and atr_pct > 0):
        return None
    from handlers import econ
    stop_pct = atr_pct * 1.5
    entry = price
    stop = entry * (1 - stop_pct / 100)
    tp = entry * (1 + stop_pct * rr / 100)
    a = econ.analyze(entry, stop, tp, REF_NOTIONAL, "long",
                     fee_in=taker, fee_out=taker,
                     slip_in_pct=slip_pct or 0, slip_out_pct=slip_pct or 0)
    return (a or {}).get("net_rr")


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
        # 深度按**固定价格带**统计，不能按「200 档」——200 档覆盖多宽取决于
        # 该币的 tickSize：实测 BTC 的 200 档只有 0.1% 宽，SNXX 却有 44%。
        # 按档数汇总会把薄币高估、厚币低估，方向正好反了。
        lo, hi = mid * (1 - DEPTH_BAND / 100), mid * (1 + DEPTH_BAND / 100)
        band = (sum(p * q for p, q in bids if p >= lo)
                + sum(p * q for p, q in asks if p <= hi))
        return {"spread": spread, "slip": s["pct"] or 0,
                "partial": s["partial"], "depth": band}
    except Exception as e:
        log.debug(f"扫描取 {sym} 盘口失败: {e}")
        return None


async def _deep(sym, ticker, turnover, sem, btc_move=0.0):
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
    # 波动区间：用 1h 的 ATR%（有就用，没有退到任一可用周期）
    atr_pct = None
    for iv in ("1h", "4h", "15m"):
        if tf.get(iv, {}).get("atr_pct"):
            atr_pct = tf[iv]["atr_pct"]
            break
    net_rr = _net_rr(price, atr_pct, (book or {}).get("slip"))
    # BTC 联动：大盘在动，而这个币的趋势方向跟它顶着干 → 否决
    agree = sum(v.get("align", 0) for v in tf.values())
    btc_conflict = (abs(btc_move) >= BTC_ALIGN_PCT and agree != 0
                    and (btc_move > 0) != (agree > 0))
    total, verdict = overall(trend, liq, crowd, ex, atr_pct, net_rr, btc_conflict,
                             direction=direction, funding=funding)
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
        "atr_pct": atr_pct, "net_rr": net_rr, "btc_conflict": btc_conflict,
        "missing": missing,
    }


async def run(limit=DEEP):
    """完整扫描。返回按可交易性排序的结果。"""
    pool, btc = await asyncio.gather(_pool(), _btc_bias(), return_exceptions=True)
    if isinstance(pool, Exception) or not pool:
        return []
    btc = 0.0 if isinstance(btc, Exception) else btc
    sem = asyncio.Semaphore(CONCURRENCY)
    res = await asyncio.gather(
        *[_deep(s, t, tv, sem, btc) for s, t, tv in pool[:limit]],
        return_exceptions=True)
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
        if r.get("net_rr") is not None:
            extra.append(f"净RR {r['net_rr']:.2f}")
        if r.get("atr_pct"):
            extra.append(f"ATR{r['atr_pct']:.2f}%")
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
    lines.append("拥挤分越高越危险。流动性/执行不及格、费率极端、"
                 "或顺势方向恰是拥挤方，都会直接否决")
    lines.append("「分歧」= 多周期打架，没方向就没机会，只有流动性")
    lines.append(f"净RR 是按 1.5×ATR 止损、{PROBE_RR:g}R 目标试算的**门槛检查**"
                 f"（<{MIN_NET_RR:g} 直接否），不是排序依据——它在宽止损的币上普遍偏高")
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
