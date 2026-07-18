"""持仓驾驶舱 /cockpit —— 不只是盈亏，而是每个仓「现在处于什么状态、该怎么办」。

和 /rpos（读交易所原始持仓）、/risk 守护面板（后台告警）的分工：
这里是**主动打开看**的深度视图，每个仓结合行情给一句判断，账户层给红色风险提示。

每仓给：方向/均价/标记/浮盈、杠杆/爆仓价/距爆仓%、有无止损、
        1h 趋势状态（顺势/逆势/结构破坏）、资金费在吃你还是喂你、
        下一关键支撑阻力、**建议动作**（持有/减仓/移止损/别补仓/重评估）。
账户层复用 riskguard 的集中度/BTC联动判断，避免两套逻辑漂移。

逐仓要拉 1h K线 + 资金费，用连接池并发（v1.0.83 修的复用），N 个仓不至于串行等。
"""
import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from handlers.util import safe_reply, safe_edit, escape_md
from handlers import marketdata as md

log = logging.getLogger(__name__)
MAJORS = ("BTCUSDT", "ETHUSDT")


def _f(x):
    """自适应精度——小价币(PEPE 之类)用固定 2 位会显示成 0.00，得按量级给位数。
    直接复用 marketdata.f，和别处的价格显示保持一致。"""
    return md.f(x)


async def _analyze_one(sym, pos):
    """一个仓的行情侧分析。任何一块失败都降级，不让整张舱因为一个币挂掉。"""
    out = {"symbol": sym, "pos": pos}
    try:
        r = await md._get("/v5/market/kline", {
            "category": md.CAT, "symbol": sym, "interval": "60", "limit": 250})
        rows = (r.get("list") or [])[::-1]
        c = [float(x[4]) for x in rows]
        h = [float(x[2]) for x in rows]
        lo = [float(x[3]) for x in rows]
        if len(c) >= 30:
            out["last"] = c[-1]
            out["ema20"] = md.ema(c, 20)
            out["ema50"] = md.ema(c, 50)
            tag, h3, l3 = md.structure(h, lo)
            out["structure"] = tag
            out["swing_high"] = h3[0] if h3 else None
            out["swing_low"] = l3[0] if l3 else None
            n50 = min(50, len(c))
            out["prior_high"] = max(h[-n50:])
            out["prior_low"] = min(lo[-n50:])
    except Exception as e:
        log.warning(f"驾驶舱 {sym} K线失败: {e}")
    try:
        t = await md._get("/v5/market/tickers", {"category": md.CAT, "symbol": sym})
        tk = (t.get("list") or [{}])[0]
        out["funding"] = float(tk.get("fundingRate") or 0) * 100
    except Exception as e:
        log.warning(f"驾驶舱 {sym} 资金费失败: {e}")
    return out


def trend_state(side, a):
    """顺势 / 逆势 / 结构破坏。给建议动作用。返回 (状态, 是否危险)。"""
    e20 = a.get("ema20")
    last = a.get("last")
    struct = a.get("structure", "")
    if not (e20 and last):
        return "数据不足", False
    up_struct = "上升" in struct
    down_struct = "下降" in struct
    price_up = last > e20
    if side == "long":
        if price_up and up_struct:
            return "顺势 ✅", False
        if not price_up and down_struct:
            return "逆势 ⚠️（多单处于 1h 下降结构+价在EMA20下）", True
        if not price_up:
            return "结构走弱 ⚠️（价跌破 1h EMA20）", True
        return "过渡", False
    else:   # short
        if not price_up and down_struct:
            return "顺势 ✅", False
        if price_up and up_struct:
            return "逆势 ⚠️（空单处于 1h 上升结构+价在EMA20上）", True
        if price_up:
            return "结构走强 ⚠️（价站上 1h EMA20）", True
        return "过渡", False


def next_levels(side, a, ref=None):
    """下一关键支撑/阻力。多单更关心下方支撑（止损参考），空单关心上方阻力。

    ref = 参照现价。传持仓的 markPrice 进来时用它，否则退回 K线 last——
    两者本该一致，但分别来自持仓接口和行情接口，统一口径免得支撑/阻力挑错边。"""
    cur = ref or a.get("last")
    if not cur:
        return None, None
    highs = [x for x in (a.get("swing_high"), a.get("prior_high")) if x and x > cur]
    lows = [x for x in (a.get("swing_low"), a.get("prior_low")) if x and x < cur]
    res = min(highs) if highs else None       # 最近的上方阻力
    sup = max(lows) if lows else None         # 最近的下方支撑
    return sup, res


def suggest(side, a, pos, dist_liq, has_sl, state_danger):
    """建议动作。规则明确、可解释——不是黑箱评分。"""
    upnl = float(pos.get("unrealisedPnl") or 0)
    acts = []
    # 没止损永远第一优先
    if not has_sl:
        acts.append("先设止损")
    # 逼近爆仓
    if dist_liq is not None and dist_liq < 15:
        acts.append("距爆仓近，减仓或加保证金")
    # 逆势/结构破坏
    if state_danger:
        if upnl < 0:
            acts.append("逆势且浮亏，别补仓摊低成本，考虑认错")
        else:
            acts.append("结构转不利，落袋或收紧止损")
    # 盈利且顺势
    if upnl > 0 and not state_danger and has_sl:
        acts.append("顺势持有，止损上移保本")
    if not acts:
        acts.append("持有观察")
    return acts


def _pos_block(a, account_equity=None):
    """一个仓的驾驶舱块。"""
    pos = a["pos"]
    sym = a["symbol"]
    short = sym.replace("USDT", "")
    side = "long" if pos.get("side") == "Buy" else "short"
    side_txt = "多 📈" if side == "long" else "空 📉"
    try:
        mark = float(pos.get("markPrice") or 0)
        liq = float(pos.get("liqPrice") or 0)
        entry = float(pos.get("avgPrice") or 0)
    except (TypeError, ValueError):
        mark = liq = entry = 0
    upnl = float(pos.get("unrealisedPnl") or 0)
    dist_liq = abs(mark - liq) / mark * 100 if mark > 0 and liq > 0 else None
    has_sl = str(pos.get("stopLoss") or "0") not in ("0", "0.0", "", "None")
    state, danger = trend_state(side, a)
    sup, res = next_levels(side, a, ref=mark or None)

    emoji = "🟢" if upnl >= 0 else "🔴"
    lines = [
        f"{emoji} *{escape_md(short)}* {side_txt} {pos.get('leverage','?')}x"
        f"｜浮盈 {upnl:+,.2f}",
        f"　均价 {_f(entry)} → 标记 {_f(mark)}",
    ]
    liq_txt = f"爆仓 {_f(liq)}（距 {dist_liq:.1f}%）" if dist_liq is not None else "爆仓价未返回"
    if dist_liq is not None and dist_liq < 15:
        liq_txt += " ⚠️"
    lines.append(f"　{liq_txt}｜止损 " + ("已设 ✅" if has_sl else "*未设 ❗*"))
    lines.append(f"　1h 趋势：{state}")
    if a.get("funding") is not None:
        fr = a["funding"]
        who = ("你在付费" if (side == "long" and fr > 0) or (side == "short" and fr < 0)
               else "你在收费")
        lines.append(f"　资金费 {fr:+.4f}%/期（{who}）")
    if sup or res:
        lines.append(f"　下一支撑 {_f(sup) if sup else '—'}｜上方阻力 {_f(res) if res else '—'}")
    for act in suggest(side, a, pos, dist_liq, has_sl, danger):
        lines.append(f"　→ {act}")
    return "\n".join(lines)


def account_flags(analyses, equity):
    """账户层红色提示。复用 riskguard 的判断逻辑，避免两套规则漂移。"""
    from handlers import riskguard
    positions = [a["pos"] for a in analyses]
    flags = []
    # 集中度
    m = riskguard.check_concentration(positions)
    if m:
        flags.append(m.split("\n")[0].replace("⚠️ *同向集中度告警*", "⚠️ *同向集中*"))
    # 裸奔仓
    naked = [a["symbol"].replace("USDT", "") for a in analyses
             if str(a["pos"].get("stopLoss") or "0") in ("0", "0.0", "", "None")]
    if naked:
        flags.append(f"⚠️ *{'、'.join(naked)}* 没设止损")
    # 山寨多单 + BTC 走弱
    alt_longs = [a for a in analyses
                 if a["pos"].get("side") == "Buy" and a["symbol"] not in MAJORS]
    if len(alt_longs) >= 2:
        notional = sum(float(a["pos"].get("positionValue") or 0) for a in alt_longs)
        share = f"（权益 {notional/equity*100:.0f}%）" if equity else ""
        flags.append(f"⚠️ 你有 {len(alt_longs)} 个山寨多单{share}，高度同向 —— "
                     f"BTC 一破位会一起走")
    return flags


async def build(equity, positions):
    """整张驾驶舱文本。逐仓并发分析。"""
    if not positions:
        return "🚗 *持仓驾驶舱*\n\n当前无持仓。空仓时可以从容挑机会——`/plan BTC long` 生成计划。"
    analyses = await asyncio.gather(*[
        _analyze_one(p.get("symbol"), p) for p in positions])
    total = sum(float(p.get("unrealisedPnl") or 0) for p in positions)
    e = "🟢" if total >= 0 else "🔴"
    lines = [f"🚗 *持仓驾驶舱*　权益 {_f(equity)} USDT",
             f"{e} 合计浮盈 {total:+,.2f} USDT｜{len(positions)} 个仓", ""]
    flags = account_flags(analyses, equity)
    if flags:
        lines.append("*账户风险*")
        lines.extend(flags)
        lines.append("")
    for a in analyses:
        lines.append(_pos_block(a, equity))
        lines.append("")
    lines.append("⚠️ 建议基于当前结构，不构成投资建议")
    return "\n".join(lines)


def _kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 刷新", callback_data="ckpt"),
         InlineKeyboardButton("🎛 交易台", callback_data="tpanel")],
        [InlineKeyboardButton("🛡 风险守护", callback_data="rgpanel"),
         InlineKeyboardButton("📊 复盘", callback_data="rsd:30")],
    ])


async def _load():
    from handlers.rtrade import _client
    c = _client()
    bal = await c.wallet_balance("USDT")
    pos = await c.positions_all()
    return float(bal.get("totalEquity") or 0), pos


async def cockpit(update, context):
    """/cockpit —— 持仓驾驶舱。"""
    from handlers.rtrade import _guard
    if not await _guard(update):
        return
    await safe_reply(update.message, "🚗 分析持仓中…（逐仓拉结构+资金费）")
    try:
        equity, pos = await _load()
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    except Exception as e:
        log.error(f"驾驶舱取账户失败: {e}")
        await safe_reply(update.message, f"❌ 取账户失败：{str(e)[:100]}")
        return
    await safe_reply(update.message, await build(equity, pos),
                     reply_markup=_kb(), parse_mode="Markdown")


async def from_btn(query, context):
    from handlers.rtrade import _btn_admin_ok
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    await query.answer("刷新中…")
    try:
        equity, pos = await _load()
    except RuntimeError:
        await safe_edit(query, "❌ 未配置 BYBIT API 密钥")
        return
    except Exception as e:
        log.error(f"驾驶舱刷新失败: {e}")
        await safe_edit(query, f"❌ 取账户失败：{str(e)[:100]}")
        return
    await safe_edit(query, await build(equity, pos), reply_markup=_kb(),
                    parse_mode="Markdown")
