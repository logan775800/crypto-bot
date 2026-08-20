"""多币种机会扫描 —— 按「可交易性」排序，不是按涨幅。

为什么不按涨幅排：涨幅榜第一名往往是最不该碰的那个——已经拉完、资金费拉爆、
盘口薄得吃两万U就滑 1%。那是**别人的利润**，不是你的机会。

所以这里给四个独立维度打分，并且**分开显示**而不是揉成一个数：
  • 趋势 —— 多周期均线(MA3/13/23)排列是否一致、斜率是否还在走
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
DEEP = 16                      # 细算多少个（每个要打 4 个接口，这是耗时大头）
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
CROSS_BARS = 5            # 均线排列在最近几根内翻过来才算金叉/死叉
SUPPORT_LOOKBACK = 40     # 找近端支撑/压力看多少根
SUPPORT_ATR = 0.8         # 距支撑不到 0.8×ATR 才算"贴着"（按波动而不是固定%）
# 逆方向那个（多头的上方压力/空头的下方支撑）要**严得多**：
# 上涨趋势里价格本来就贴着近期高点，用同一个阈值的话 10 个信号里 6 个都挂这条，
# 不区分就是噪音，而噪音会让人连真正该看的警示一起忽略。
RISK_ATR = 0.25
VOL_HOT = 1.8             # 放量倍数门槛
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
            direction=None, funding=None, crowd_known=True):
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
    # 拥挤度算不出来时**不准给绿灯**。拥挤是减分项，拿不到数据 = 这一项按 0 分
    # 计入，等于凭空加了 15 分——实测同一个币 GPS，Bybit 判「拥挤度极高·不建议」，
    # 换到不给费率/持仓量的 OKX 就变成 86 分 ✅。缺数据不能让东西看起来更安全。
    if not crowd_known and not vetoes:
        raw = min(raw, 55)
    if raw >= 70 and not vetoes and not no_direction and crowd_known:
        verdict = "✅ 可交易性好"
    elif vetoes:
        verdict = "❌ 不建议（" + "、".join(vetoes) + "）"
    elif not crowd_known:
        verdict = ("🟡 拥挤度算不出来（这家不给资金费率/持仓量），"
                   "不敢给绿灯——换个数据源再看一眼")
    elif no_direction:
        verdict = "🟡 只有流动性，没有方向——多周期打架，等它选边"
    elif raw >= 50:
        verdict = "🟡 可以看，但要控仓"
    else:
        verdict = "❌ 不建议"
    return _clamp(raw), verdict


# ── 取数 ─────────────────────────────────────────────────────────
async def _pool(ex="bybit", market="swap"):
    """粗筛：一次拉全市场 ticker → 过流动性门槛 → 按**动过**排序。

    这里的排序方式很关键。按成交额降序取的话，进入细算的永远是 BTC/ETH 那批，
    四个维度全部满分，扫描结果每天长得一模一样，等于没有扫描。
    真正的机会在「过了流动性门槛、但最近动过」的中小市值里。

    所以：**流动性是门槛（硬性），波动幅度是排序（选谁进细算）**。
    这不会让它退化成涨幅榜——涨幅只决定谁被看一眼，最终排序由四维打分决定，
    拥挤/执行不及格照样被否决。
    """
    from handlers import klines as kl
    pool = await kl.universe(ex, market)
    rows = []
    for x in pool:
        # 代币化美股/大宗商品（AAPL、XAU 这类）对做加密永续的人是噪音，
        # 而且风险特征不同（跟着美股开收盘跳空、周末流动性枯竭）。
        if not x["crypto"]:
            continue
        if x["turnover"] < MIN_TURNOVER:
            continue
        rows.append((x["symbol"], x, x["turnover"], abs(x["change"])))
    rows.sort(key=lambda x: -x[3])          # 按波动幅度，不是成交额
    return [(s_, t, tv) for s_, t, tv, _m in rows[:POOL]]


async def _tf_snapshot(sym, iv, limit=120, ex="bybit", market="swap"):
    """某周期的均线排列(MA3/13/23)、斜率、ATR%。取不到就返回 None —— 缺周期不猜。"""
    try:
        from handlers import klines as kl
        rows, _meta = await kl.fetch(sym, iv, limit, ex, market)
        c = [x[4] for x in rows]
        if len(c) < 55:
            return None
        # 趋势排列直接复用 ma_align——和破位扫描**同一个函数**，
        # 不是"同一套周期各写一遍判定"。
        # 这里踩过一次：我给它多加了"价格要在最上面"这一条，看着更严谨，
        # 实测把"有方向"的比例从 87%(旧EMA口径) 压到 39%，
        # 大半个市场被判成"分歧"、扫描几乎给不出绿灯。ma_align 是 74%。
        # 顺势的定义就是**均线排列**，价格在不在最上面是另一回事（那是回踩）。
        from handlers.annotchart import _ma_series, ma_align, MA_PERIODS
        p1, p2, p3 = MA_PERIODS
        s1 = _ma_series(c, p1)[-1] if len(c) >= p1 else None
        s2 = _ma_series(c, p2)[-1] if len(c) >= p2 else None
        s3 = _ma_series(c, p3)[-1] if len(c) >= p3 else None
        if not (s1 and s2 and s3):
            return None
        align = ma_align(c)
        last = c[-1]
        prev_ser = _ma_series(c[:-10], p1) if len(c) > p1 + 10 else None
        prev = prev_ser[-1] if prev_ser and prev_ser[-1] is not None else None
        e20 = s1
        slope = ((e20 - prev) / prev * 100) if prev else 0
        h = [float(x[2]) for x in rows]
        lo = [float(x[3]) for x in rows]
        a14 = md.atr(h, lo, c, 14)
        # 下面三个是给「信号标签」用的。它们和四维打分回答的不是同一个问题：
        # 打分说"这个币能不能下单"，标签说"现在有没有信号"。两个都要。
        # 金叉/死叉：排列**刚刚**翻过来才算，一直多头排列不叫金叉
        prev_align = ma_align(c[:-CROSS_BARS]) if len(c) > p3 + CROSS_BARS else align
        cross = 0
        if align != 0 and prev_align != align:
            cross = align
        # 放量：**已收盘的那根** vs 它之前 20 根的均量。
        # 最后一根还在走，量只累积了一部分——拿它去比必然偏低。
        # 实测：4h 的比值 1.1~2.4，1h 0.28~1.37，15m 只有 0.13~0.67，
        # 越短的周期被压得越狠，等于「放量」在短周期上永远不会触发。
        vol = [float(x[5]) for x in rows]
        vol_ratio = None
        if len(vol) >= 22:
            base = sum(vol[-22:-2]) / 20
            vol_ratio = (vol[-2] / base) if base else None
        # 贴支撑/压力：离近端摆动低/高多近（按 ATR 衡量，不用固定百分比——
        # 不同币的波动差一个量级，固定 % 在小币上永远"贴着"）
        near = near_tight = None
        if a14:
            lo_n = min(lo[-SUPPORT_LOOKBACK:])
            hi_n = max(h[-SUPPORT_LOOKBACK:])
            if (last - lo_n) <= a14 * SUPPORT_ATR:
                near = "support"
            elif (hi_n - last) <= a14 * SUPPORT_ATR:
                near = "resist"
            # 逆方向的风险提示用更严的尺子（RISK_ATR），否则趋势币个个都挂
            if (last - lo_n) <= a14 * RISK_ATR:
                near_tight = "support"
            elif (hi_n - last) <= a14 * RISK_ATR:
                near_tight = "resist"
        return {"align": align, "slope": slope, "close": last,
                "atr_pct": (a14 / last * 100) if (a14 and last) else None,
                "cross": cross, "vol_ratio": vol_ratio, "near": near,
                "near_tight": near_tight}
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


async def _oi_change(sym, ex="bybit"):
    """近 12 期持仓量变化%。这家给不了就返回 None——扫描器对缺失是标"未知"，
    不是当成 0（OKX 没有按合约的持仓量历史接口）。"""
    try:
        from handlers import klines as kl
        rows = await kl.oi_series(sym, ex, "15m", 13)
        if len(rows) < 5:
            return None
        a, b = rows[0], rows[-1]
        return (b - a) / a * 100 if a else None
    except Exception:
        return None


async def _book_quality(sym, ex="bybit", market="swap"):
    """价差 + 参考名义下的滑点 + 深度。执行质量的全部依据。

    ⚠️ 盘口单位各家不同（OKX/Gate 是**张**），归一在 klines.orderbook 里做——
    不换算的话 OKX 的深度会被算成几百倍，"执行"这一维直接反了。
    """
    try:
        from handlers import econ
        from handlers import klines as kl
        bids, asks = await kl.orderbook(sym, ex, market, 200)
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


async def _deep(sym, ticker, turnover, sem, btc_move=0.0, ex="bybit",
                market="swap", pre_tf=None):
    """对单个候选做细算。任何一项缺失都如实标 None，不用默认值糊过去。

    pre_tf：便宜段已经取过的周期，直接复用，别再打一遍同样的接口。
    """
    need = [iv for iv in ("4h", "1h", "15m") if iv not in (pre_tf or {})]
    async with sem:
        tf_res, oi, book = await asyncio.gather(
            asyncio.gather(*[_tf_snapshot(sym, iv, ex=ex, market=market)
                             for iv in need]),
            _oi_change(sym, ex), _book_quality(sym, ex, market),
            return_exceptions=True)
    tf = dict(pre_tf or {})
    if not isinstance(tf_res, Exception):
        for iv, v in zip(need, tf_res):
            if v:
                tf[iv] = v
    oi = None if isinstance(oi, Exception) else oi
    book = None if isinstance(book, Exception) else book
    try:
        # funding 可能是 None（OKX 的行情接口不带、现货压根没有）——
        # 保持 None 传给拥挤度打分，别用 0 顶替：0 的意思是"中性"，不是"不知道"
        funding = ticker.get("funding")
        chg = float(ticker.get("change") or 0)
        price = float(ticker.get("price") or 0)
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
                             direction=direction, funding=funding,
                             crowd_known=not (funding is None and oi is None))
    missing = []
    if not tf:
        missing.append("K线")
    if oi is None:
        missing.append("OI")
    if not book:
        missing.append("盘口")
    if funding is None:
        # 费率拿不到时拥挤度只算了 OI 那一半，分数会偏低（看起来更安全）。
        # 必须说出来——把"不知道"当成"中性"正是这类分数骗人的方式。
        missing.append("费率")
    return {
        "symbol": sym, "price": price, "chg": chg, "turnover": turnover,
        "trend": trend, "direction": direction, "liq": liq, "crowd": crowd,
        "exec": ex, "total": total, "verdict": verdict,
        "funding": funding, "oi_change": oi,
        "slip": (book or {}).get("slip"), "partial": (book or {}).get("partial"),
        "atr_pct": atr_pct, "net_rr": net_rr, "btc_conflict": btc_conflict,
        "missing": missing, "tf": tf,
    }


async def run(limit=DEEP, source=None):
    """完整扫描。返回按可交易性排序的结果。

    source 是数据源标签，不传就是 Bybit 永续。⚠️ 换所扫的是**那家的盘口和深度**，
    结果不可跨所比较——"能不能进出"本来就取决于你在哪家下单。
    """
    from handlers import klines as kl
    ex, market = ("bybit", "swap") if not source else kl.src_mod.split_label(source)
    if ex == kl.src_mod.AUTO:
        ex, market = "bybit", "swap"
    pool, btc = await asyncio.gather(_pool(ex, market), _btc_bias(),
                                     return_exceptions=True)
    if isinstance(pool, Exception) or not pool:
        return []
    btc = 0.0 if isinstance(btc, Exception) else btc
    sem = asyncio.Semaphore(CONCURRENCY)

    # 两段式：先用**只取 K 线**的便宜通道给全池打信号，再只对有信号的那些
    # 跑盘口/OI 的否决。
    # 原来是按成交额取前 16 个做全套细算——成交额高不等于有信号，
    # 排在第 20 位但三周期共振的币根本轮不到被看一眼。
    # 换成这样之后覆盖从 16 涨到 40，请求数反而更少：
    # 便宜段每个币 2 个接口，贵的那两个（盘口/OI）只花在真有信号的十几个上。
    lite = await asyncio.gather(
        *[_lite(s, t, tv, sem, ex, market) for s, t, tv in pool[:POOL]],
        return_exceptions=True)
    cands = [r for r in lite if r and not isinstance(r, Exception)]
    hits = [r for r in cands if signal_of(r)[0] != 0]
    # 没有任何信号时也别空手而归：退回按成交额取前几个做细算，
    # 至少让「四维明细」有东西看
    todo = hits[:limit] or cands[:min(6, limit)]

    res = await asyncio.gather(
        *[_deep(r["symbol"], r["_ticker"], r["turnover"], sem, btc, ex, market,
                pre_tf=r["tf"]) for r in todo],
        return_exceptions=True)
    out = [r for r in res if r and not isinstance(r, Exception)]
    out.sort(key=lambda r: -r["total"])
    for r in out:
        r["scanned"] = len(cands)          # 实际打过标签的币数，要如实报出来
    return out


async def _lite(sym, ticker, turnover, sem, ex="bybit", market="swap"):
    """便宜段：只取 K 线算信号，不碰盘口和 OI。

    只用 4h + 1h 两个周期——15m 留给贵的那段。共振本来就该由大周期定调，
    15m 的作用是择时，而择时对"这个币值不值得细看"没有发言权。
    """
    async with sem:
        tf_res = await asyncio.gather(
            *[_tf_snapshot(sym, iv, ex=ex, market=market) for iv in ("4h", "1h")],
            return_exceptions=True)
    tf = {}
    for iv, v in zip(("4h", "1h"), tf_res):
        if v and not isinstance(v, Exception):
            tf[iv] = v
    if not tf:
        return None
    return {"symbol": sym, "turnover": turnover, "tf": tf, "_ticker": ticker,
            "price": float(ticker.get("lastPrice") or 0) if ticker else 0,
            "verdict": ""}


def render(rows, limit=8, source="Bybit永续"):
    if not rows:
        return "扫描没拿到结果（行情源异常或全市场成交额都低于门槛）。"
    lines = [f"🔍 *机会扫描*　按**可交易性**排序，不是按涨幅",
             f"数据源 {source}——盘口和深度是**这一家**的，"
             f"换所结果不可比（能不能进出取决于你在哪下单）",
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
    from handlers.source import pref_label
    src = pref_label(update.effective_chat.id)
    await safe_reply(update.message,
                     f"🔍 在 {src or 'Bybit永续'} 上扫描…"
                     f"（全池打标签 → 只对有信号的细算，约 10~20 秒）")
    try:
        rows = await run(source=src)
    except Exception as e:
        log.error(f"/scan 失败: {e}")
        await safe_reply(update.message, f"扫描失败：{str(e)[:80]}")
        return
    # 默认给**紧凑信号版**：一屏能扫完 20 个名字。
    # 四维明细没删，收进按钮——它回答的是"能不能下单"，
    # 而人打开扫描时第一个问题是"有哪些"。
    kb = result_kb()
    context.chat_data["scan_rows"] = rows
    context.chat_data["scan_src"] = src or "Bybit永续"
    await safe_reply(update.message,
                     render_signals(rows, source=src or "Bybit永续"),
                     reply_markup=kb, parse_mode="Markdown")


# ── 信号标签：一行说清「为什么是它」──────────────────────────
# 四维打分回答的是"能不能下单"，标签回答的是"现在有没有信号"。
# 原来的输出把每个币铺成 6 行（四个分数 + 一段 verdict + 缺失项），
# 一屏只看得到一个半币；而人要的是**一眼扫过 20 个名字**，
# 想细看再点进去。所以结论层压成一行，细节收进按钮。
STRONG_MIN = 3        # 命中几条算「强」


def signal_of(r):
    """→ (方向, 强度, [标签])。方向 1=买 -1=卖 0=没信号。

    共振的定义是**多周期同向**：单周期看多没意义，1h 多 4h 空的币，
    哪个方向进去都是在猜。这条和 /scan 的「分歧」判定同源。
    """
    tf = r.get("tf") or {}
    if not tf:
        return 0, 0, []
    aligns = [v.get("align", 0) for v in tf.values()]
    agree = sum(aligns)
    side = 1 if agree > 0 else (-1 if agree < 0 else 0)
    if side == 0:
        return 0, 0, []

    tags, hits = [], 0
    if abs(agree) == len(aligns) and len(aligns) >= 2:
        tags.append("多头共振" if side > 0 else "空头共振")
        hits += 2                      # 多周期全同向，这条最重
    # 金叉/死叉、放量都**看所有周期**，不是只看最短的那个。
    # 只看最短周期时踩过：XLM 的 4h 和 1h 双双金叉，因为 15m 没交叉就整个丢了——
    # 而大周期的金叉本来比小周期更有分量。
    vals = list(tf.values())
    if any(v.get("cross") == side for v in vals):
        tags.append("金叉" if side > 0 else "死叉")
        hits += 1
    if any((v.get("vol_ratio") or 0) >= VOL_HOT for v in vals):
        tags.append("放量")
        hits += 1

    # 位置：顺着方向的算加分，逆着方向的是**风险，要说出来**。
    # 原来只挑加分的那一半，逆的直接丢掉——多头正贴着压力位却什么都不提，
    # 恰恰是最容易让人追在高点的那种沉默。
    nears = {v.get("near") for v in vals}
    risks = {v.get("near_tight") for v in vals}
    if side > 0:
        if "support" in nears:
            tags.append("贴支撑")
            hits += 1
        elif "resist" in risks:          # 真顶在那儿才提，见 RISK_ATR
            tags.append("⚠️上方有压力")
    else:
        if "resist" in nears:
            tags.append("贴压力")
            hits += 1
        elif "support" in risks:
            tags.append("⚠️下方有支撑")
    if not [t for t in tags if not t.startswith("⚠️")]:
        return 0, 0, []                # 只剩风险标签，不算信号
    return side, (2 if hits >= STRONG_MIN else 1), tags


def render_signals(rows, source="Bybit永续", limit=12):
    """紧凑输出：一行一个币，买卖分开。

    保留**否决**这件事——那是这个扫描和"指标共振榜"的根本区别：
    盘口薄、费率极端、顺势方向恰是拥挤方的币，指标再漂亮也下不进去。
    被否的不混进名单，但要报出数量和理由，否则就成了"悄悄少给你几个"。
    """
    from handlers.util import escape_md
    buys, sells, vetoed = [], [], []
    for r in rows:
        side, strength, tags = signal_of(r)
        if side == 0 or not tags:
            continue
        if "不建议" in (r.get("verdict") or ""):
            vetoed.append(r)
            continue
        item = (r, strength, tags)
        (buys if side > 0 else sells).append(item)
    for lst in (buys, sells):
        lst.sort(key=lambda x: (-x[1], -x[0]["total"]))

    def block(items):
        out = []
        for r, strength, tags in items[:limit]:
            short = r["symbol"].replace("USDT", "")
            out.append(f"{short:<9}{md.f(r['price']):>12}   "
                       f"{'强' if strength == 2 else '中'}·{'+'.join(tags)}")
        return "```\n" + "\n".join(out) + "\n```"

    scanned = rows[0].get("scanned") if rows else None
    is_perp = "永续" in (source or "") or "swap" in (source or "").lower()
    # 永续能双向开仓，"买入/卖出"是现货的说法——用错词会让人以为只能做多
    long_cn, short_cn = ("做多", "做空") if is_perp else ("买入", "卖出")
    lines = [f"🔍 *信号扫描*　{escape_md(source)}",
             f"打过标签 {scanned or len(rows)} 个"
             f"（成交额≥{MIN_TURNOVER/1e6:.0f}M 的前 {POOL} 个），"
             f"其中 {len(rows)} 个做了盘口/拥挤度细算"]
    lines += [f"🟢 *{long_cn} ({len(buys)})*",
              block(buys) if buys else "　（这一轮没有）"]
    # 空组为 0 时也要**把这一行印出来**：整段消失读起来像"没扫做空"，
    # 而"当前没有空头共振"本身就是有用的信息（市场一边倒）
    lines += [f"🔴 *{short_cn} ({len(sells)})*",
              block(sells) if sells else "　（这一轮没有）"]
    if not buys and not sells:
        lines.append("多周期打架的时候，哪个方向进去都是在猜。"
                     "**没有信号本身就是信号。**")
    if vetoed:
        # verdict 形如「❌ 不建议（盘口太薄）」——要的是括号里那半句，
        # 只显示"❌ 不建议"等于没说理由
        reasons = set()
        for v in vetoed:
            txt = v.get("verdict") or ""
            reasons.add(txt.split("（")[1].rstrip("）") if "（" in txt else txt)
        why = "、".join(sorted(reasons)[:3])
        lines.append(f"⚠️ 另有 {len(vetoed)} 个指标漂亮但**下不进去**（{why}），已剔除")
    lines.append("共振=多周期均线同向｜强=命中3条以上")
    lines.append(f"⚠️ 口径：只扫 **{escape_md(source)}**——不含现货、"
                 f"不含其它交易所。换所发 `/source`")
    lines.append("⚠️ 仅供参考，不构成投资建议")
    return "\n".join(lines)


# ── 换所/换现货：就在结果下面，不用去改全局设置 ────────────────
# 他的原话：「这个只是扫描 bybit 合约的一个片面 不是现货也不是其它交易所」。
# 能力其实一直有（run() 认 source 标签），但入口埋在 /source 全局设置里，
# 等于没有——他要的是"在这张结果上换一下再看"。
SCAN_SOURCES = [("bybit", "swap"), ("bybit", "spot"),
                ("okx", "swap"), ("okx", "spot"),
                ("binance", "swap"), ("binance", "spot"),
                ("gate", "swap"), ("gate", "spot")]


def result_kb():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 四维明细（趋势/流动性/拥挤/执行）",
                              callback_data="scan:detail")],
        [InlineKeyboardButton("🏦 换所 / 换现货", callback_data="scan:src"),
         InlineKeyboardButton("🔄 重扫", callback_data="do:scan")]])


def source_kb():
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from handlers import source as src
    rows, cur = [], []
    for ex, mkt in SCAN_SOURCES:
        lab = src.label_of(ex, mkt)
        if not lab:
            continue
        cur.append(InlineKeyboardButton(lab, callback_data=f"scan:on:{lab}"))
        if len(cur) == 2:
            rows.append(cur)
            cur = []
    if cur:
        rows.append(cur)
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="scan:brief")])
    return InlineKeyboardMarkup(rows)
