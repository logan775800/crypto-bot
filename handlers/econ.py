"""净盈亏比 —— 把「价格距离算出来的盈亏比」换成「到手的钱」。

为什么必须单独做这层：用 |TP-入场| ÷ |入场-止损| 算出来的 1.8:1，在真实账户里
可能只有 1.1:1，低流动性小币上甚至会是负的。中间被三样东西吃掉：

  • **手续费**：开+平各一次。吃单 0.055% × 2 = 0.11% 的名义，止损距离只有 1%
    的短线单，光手续费就吃掉一成多的预期；
  • **滑点**：市价单要一档一档吃穿盘口。AKE/LAB 这种深度几万 U 的币，
    开两万 U 的仓自己就把价格打上去了，成交均价根本不是你看到的那个价；
  • **资金费**：按持仓时长累计。8 小时一期，隔夜单起码一期，费率 0.05% 的
    拥挤行情里持有一天 = 0.15% 的名义。

三样加起来通常是 0.2%~0.5% 的名义。止损距离 5% 时无所谓，止损距离 1% 时
它就是胜负手——而这正是短线永续最常见的场景。

本模块只做纯计算，数据由调用方喂进来（盘口来自 marketdata，费率来自账户接口），
这样能测、也不会因为某个接口挂了就整条算不出来。
"""
import logging
import math

log = logging.getLogger(__name__)

# Bybit 线性永续 VIP0 默认费率。拿得到账户真实费率就用真实的，拿不到用这个兜底
# （宁可高估成本：低估会让一单看起来能做，实际做完是亏的）。
TAKER = 0.00055
MAKER = 0.00020
FUNDING_PERIOD_H = 8        # 多数永续 8 小时一结算；1h 高频费率的币要按实际传


def walk_book(levels, notional):
    """吃穿盘口：按名义金额 notional 逐档吃，返回 (成交均价, 实际吃到的名义, 是否吃光整本)。

    levels 是 [(价, 量), ...]，量的单位是**标的币**。这就是滑点的真实来源——
    盘口薄的时候，你的单子自己会把价格推走。
    """
    if not levels or notional <= 0:
        return None, 0.0, False
    got_notional = 0.0
    got_qty = 0.0
    for price, qty in levels:
        if price <= 0 or qty <= 0:
            continue
        level_notional = price * qty
        if got_notional + level_notional >= notional:
            need_qty = (notional - got_notional) / price
            got_qty += need_qty
            got_notional = notional
            break
        got_notional += level_notional
        got_qty += qty
    else:
        # 整本都吃完了还不够 → 这单在当前盘口下根本吃不满
        return (got_notional / got_qty if got_qty else None), got_notional, True
    return (got_notional / got_qty if got_qty else None), got_notional, False


def slippage(levels, notional, ref_price):
    """相对参考价(买一/卖一/中价)的滑点。返回 dict，深度不够时 partial=True。"""
    avg, filled, exhausted = walk_book(levels, notional)
    depth = sum(p * q for p, q in (levels or []) if p > 0 and q > 0)
    if avg is None or not ref_price:
        return {"pct": None, "avg": None, "filled": filled, "depth": depth,
                "partial": True, "note": "盘口数据不可用，滑点无法估算"}
    pct = abs(avg - ref_price) / ref_price * 100
    note = ""
    if exhausted:
        note = (f"⚠️ 盘口{len(levels)}档全吃光也只成交 {filled:,.0f} USDT"
                f"（需要 {notional:,.0f}）—— 会部分成交，剩下的要么挂着要么追价")
    elif pct > 0.3:
        note = f"⚠️ 滑点 {pct:.2f}% 偏大，考虑改挂单或拆单"
    return {"pct": pct, "avg": avg, "filled": filled, "depth": depth,
            "partial": exhausted, "note": note}


def funding_cost(notional, rate, hold_hours, side, period_h=FUNDING_PERIOD_H):
    """持仓期间的资金费。返回**成本**（正数=你付钱，负数=你收钱）。

    费率为正时多头付给空头，为负时反过来。按周期数向上取整——只要跨过一次结算
    就得付一整期，持有 9 小时付两期，这是很多人算漏的地方。
    """
    if not (notional > 0 and hold_hours > 0 and period_h > 0):
        return 0.0
    periods = math.ceil(hold_hours / period_h)
    cost = notional * rate * periods
    return cost if side == "long" else -cost


def analyze(entry, stop, tp, notional, side,
            fee_in=TAKER, fee_out=TAKER,
            slip_in_pct=0.0, slip_out_pct=0.0,
            funding_rate=0.0, hold_hours=0.0, period_h=FUNDING_PERIOD_H):
    """一单的净收益拆解。所有成本都按**名义**计，和杠杆无关。

    返回毛/净两套盈亏比 —— 用户关心的是净的那个，但两个并排放着才能看出
    成本吃掉了多少。
    """
    if not (entry > 0 and stop > 0 and tp > 0 and notional > 0):
        return None
    win_pct = abs(tp - entry) / entry
    loss_pct = abs(entry - stop) / entry
    if loss_pct == 0:
        return None
    gross_win = notional * win_pct
    gross_loss = notional * loss_pct

    fee_open = notional * fee_in
    # 平仓时的名义随价格变化，用出场价折算，别一律按开仓名义算
    fee_close_win = notional * (tp / entry) * fee_out
    fee_close_loss = notional * (stop / entry) * fee_out
    slip = notional * (slip_in_pct + slip_out_pct) / 100
    fund = funding_cost(notional, funding_rate, hold_hours, side, period_h)

    cost_win = fee_open + fee_close_win + slip + fund
    cost_loss = fee_open + fee_close_loss + slip + fund
    net_win = gross_win - cost_win
    net_loss = gross_loss + cost_loss        # 亏的时候成本是**加**上去的

    # 回本需要走多远：成本占名义的比例
    breakeven_pct = (fee_open + fee_close_win + slip + max(fund, 0)) / notional * 100
    return {
        "entry": entry, "stop": stop, "tp": tp, "side": side, "notional": notional,
        "win_pct": win_pct * 100, "loss_pct": loss_pct * 100,
        "gross_win": gross_win, "gross_loss": gross_loss,
        "gross_rr": gross_win / gross_loss,
        "fee_open": fee_open, "fee_close": fee_close_win,
        "slippage": slip, "funding": fund,
        "cost_total": cost_win,
        "net_win": net_win, "net_loss": net_loss,
        "net_rr": (net_win / net_loss) if net_loss > 0 else None,
        "breakeven_pct": breakeven_pct,
        "eaten_pct": (1 - (net_win / net_loss) / (gross_win / gross_loss)) * 100
                     if net_loss > 0 and gross_loss > 0 else None,
    }


def verdict(a):
    """一句话结论。净盈亏比才是判据，毛的那个只是用来对比。"""
    if not a:
        return ""
    rr = a.get("net_rr")
    if rr is None:
        return "净盈亏比算不出（止损距离为 0？）"
    if rr < 0:
        return "❌ 成本已经吃掉全部预期收益——这单**净期望为负**，不该做"
    if rr < 1:
        return "❌ 净盈亏比 <1：赢一次赚的还不够输一次亏的，位置或成本不对"
    if rr < 1.5:
        return "⚠️ 净盈亏比偏低，胜率要很高才划算——考虑等更好的入场位"
    return "✅ 净盈亏比可接受"


def render(a, extra=None):
    """渲染成一屏能看完的成本卡。"""
    if not a:
        return "❌ 算不了：入场/止损/止盈都要 >0，且入场≠止损"
    f = _fmt
    lines = [
        "💰 *净盈亏比*（扣掉手续费/滑点/资金费之后）",
        f"名义 {a['notional']:,.0f} USDT｜{'做多' if a['side']=='long' else '做空'}",
        f"入场 {f(a['entry'])} → 止损 {f(a['stop'])}（{a['loss_pct']:.2f}%）",
        f"止盈 {f(a['tp'])}（{a['win_pct']:.2f}%）",
        "━━━━━━━━━━━━━━",
        f"毛盈亏比　*{a['gross_rr']:.2f}* : 1",
        f"　赢 +{a['gross_win']:,.2f}　输 -{a['gross_loss']:,.2f} USDT",
        "",
        "*成本拆解*",
        f"　开仓手续费　-{a['fee_open']:,.2f}",
        f"　平仓手续费　-{a['fee_close']:,.2f}",
        f"　滑点　　　　-{a['slippage']:,.2f}",
        f"　资金费　　　{-a['funding']:+,.2f}" + ("（你付）" if a['funding'] > 0 else "（你收）"),
        f"　合计　　　　-{a['cost_total']:,.2f} USDT",
        "━━━━━━━━━━━━━━",
        f"*净盈亏比　{a['net_rr']:.2f} : 1*" if a['net_rr'] is not None else "净盈亏比 —",
        f"　净赢 +{a['net_win']:,.2f}　净输 -{a['net_loss']:,.2f} USDT",
        f"　回本需走 {a['breakeven_pct']:.3f}%（成本占名义）",
    ]
    if a.get("eaten_pct") is not None and a["eaten_pct"] > 0:
        lines.append(f"　成本吃掉了盈亏比的 *{a['eaten_pct']:.0f}%*")
    lines.append("")
    lines.append(verdict(a))
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _fmt(x):
    from handlers import marketdata as md
    return md.f(x)


# ── 真实数据接线（失败一律降级，不让整条算不出来）──────────────────
async def account_fee_rate(symbol):
    """账户真实费率。拿不到就返回 None，由调用方回退到 VIP0 默认值。

    真实费率可能比默认低不少（VIP 等级/返佣），用默认值会高估成本——
    高估的方向是安全的，所以拿不到不算故障。
    """
    try:
        from handlers.rtrade import _client
        c = _client()
        r = await c._get("/v5/account/fee-rate",
                         {"category": "linear", "symbol": symbol})
        row = ((r or {}).get("list") or [{}])[0]
        taker, maker = row.get("takerFeeRate"), row.get("makerFeeRate")
        if taker is None:
            return None
        return {"taker": float(taker), "maker": float(maker or taker)}
    except Exception as e:
        log.debug(f"取账户费率失败 {symbol}: {e}")
        return None


async def market_inputs(symbol, side, notional):
    """把「这一单」需要的真实市场参数一次取齐：盘口滑点 + 资金费率 + 费率。

    返回 dict，任何一项取不到都标 None 并在 notes 里说明——
    缺了就用保守默认值，但**必须让用户知道哪一项是估的**。
    """
    from handlers import marketdata as md
    out = {"slip_in": 0.0, "slip_out": 0.0, "funding_rate": 0.0,
           "taker": TAKER, "maker": MAKER, "notes": [], "partial": False}
    sym = md.norm(symbol)
    # 盘口：开仓吃的方向和平仓吃的方向相反
    try:
        r = await md._get("/v5/market/orderbook",
                          {"category": md.CAT, "symbol": sym, "limit": 200})
        bids = [(float(p), float(s)) for p, s in (r.get("b") or [])]
        asks = [(float(p), float(s)) for p, s in (r.get("a") or [])]
        if bids and asks:
            open_side = asks if side == "long" else bids       # 开多吃卖盘
            close_side = bids if side == "long" else asks      # 平多砸买盘
            ref_open = open_side[0][0]
            ref_close = close_side[0][0]
            si = slippage(open_side, notional, ref_open)
            so = slippage(close_side, notional, ref_close)
            out["slip_in"] = si["pct"] or 0.0
            out["slip_out"] = so["pct"] or 0.0
            out["partial"] = bool(si["partial"] or so["partial"])
            out["depth"] = si["depth"]
            for n in (si["note"], so["note"]):
                if n:
                    out["notes"].append(n)
        else:
            out["notes"].append("⚠️ 盘口为空，滑点按 0 估算（实际会更差）")
    except Exception as e:
        out["notes"].append(f"⚠️ 盘口取数失败（{str(e)[:40]}），滑点按 0 估算，实际会更差")
    # 资金费率（当前=下一期预测值）
    try:
        t = await md._get("/v5/market/tickers", {"category": md.CAT, "symbol": sym})
        tk = (t.get("list") or [{}])[0]
        out["funding_rate"] = float(tk.get("fundingRate") or 0)
    except Exception as e:
        out["notes"].append(f"⚠️ 资金费率取不到（{str(e)[:40]}），按 0 估算")
    # 账户真实费率（管理员配了密钥才有）
    fr = await account_fee_rate(sym)
    if fr:
        out.update(taker=fr["taker"], maker=fr["maker"])
        out["notes"].append(f"费率用的是你账户真实值（taker {fr['taker']*100:.4f}%）")
    else:
        out["notes"].append(f"费率用 VIP0 默认（taker {TAKER*100:.3f}%），真实费率可能更低")
    return out


USAGE = (
    "💰 *净盈亏比* —— 扣掉手续费/滑点/资金费之后，这单到底赚不赚\n\n"
    "`/net BANK long 0.081 0.0795 0.086 2000`\n"
    "　币 方向 入场 止损 止盈 名义USDT `[持仓小时]`\n\n"
    "会给出：毛盈亏比 vs *净盈亏比*、成本逐项拆解、回本需要走多少、"
    "以及**盘口深度够不够**（低流动性币会告诉你会不会部分成交）。\n\n"
    "为什么要看净的：止损 1% 的短线单，光两次吃单手续费就能把毛 1.2:1 "
    "压到净 0.98:1 —— 那不是策略，是慢性亏损。"
)


async def net_cmd(update, context):
    """/net —— 直接算一单的净盈亏比。"""
    from handlers.util import safe_reply
    a = context.args or []
    if len(a) < 6:
        await safe_reply(update.message, USAGE, parse_mode="Markdown")
        return
    try:
        sym, side = a[0].upper(), a[1].lower()
        entry, stop, tp, notional = (float(x.replace(",", "")) for x in a[2:6])
        hold = float(a[6]) if len(a) > 6 else 8.0
    except (ValueError, IndexError):
        await safe_reply(update.message, "参数看不懂。\n\n" + USAGE, parse_mode="Markdown")
        return
    if side not in ("long", "short"):
        await safe_reply(update.message, "方向只能是 long 或 short")
        return
    await safe_reply(update.message, f"💰 按当前盘口试算 {sym} …")
    try:
        _a, txt = await estimate(sym, side, entry, stop, tp, notional, hold)
    except Exception as e:
        log.error(f"/net 计算失败: {e}")
        await safe_reply(update.message, f"算不了：{str(e)[:80]}")
        return
    await safe_reply(update.message, txt, parse_mode="Markdown")


async def estimate(symbol, side, entry, stop, tp, notional, hold_hours=8):
    """一站式：取真实市场参数 → 算净盈亏比 → 返回 (分析dict, 渲染文本)。"""
    mi = await market_inputs(symbol, side, notional)
    a = analyze(entry, stop, tp, notional, side,
                fee_in=mi["taker"], fee_out=mi["taker"],
                slip_in_pct=mi["slip_in"], slip_out_pct=mi["slip_out"],
                funding_rate=mi["funding_rate"], hold_hours=hold_hours)
    extra = ""
    if mi["notes"]:
        extra = "\n_" + "；".join(mi["notes"]) + "_"
    return a, render(a, extra)
