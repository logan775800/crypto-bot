"""虚拟合约交易（模拟盘）—— 用真实行情练手，不碰真钱。

面向永续杠杆玩法：开多/开空、指定保证金+杠杆、实时浮动盈亏、理论爆仓价、
平仓结算回账户、胜率/历史统计，后台自动监控爆仓。
价格取 **Bybit USDT 永续**（和实盘同源），该币没有永续才退回 CoinGecko 现货。
开仓含真实滑点/账户费率/合约最小下单量校验，持仓扣资金费，止盈止损挂单——
刻意做成和实盘同构：模拟盘的意义是手感能迁移，零摩擦练出来的直觉到实盘全是错的。
"""
import time
import logging
import httpx
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from api import get_price as _cg_price, get_prices as _cg_prices
from config import COIN_IDS
from storage import data, save_data
from handlers.util import safe_reply, safe_edit

START_BALANCE = 10000.0   # 初始虚拟本金（USDT）
FEE_RATE = 0.0005         # 单边吃单手续费兜底值（取不到真实费率时用）


async def _real_frictions(symbol, side, notional, entry):
    """把实盘的三种摩擦搬进模拟盘：滑点、真实费率、部分成交。

    模拟盘的意义是让手感能迁移到实盘。零滑点、固定费率的模拟盘会养出两个
    错误直觉：「这个位置我挂得到」和「这个量我开得进去」——低流动性小币上
    这两条都不成立，而那正是最容易亏大钱的地方。

    取不到盘口就退回无摩擦，但要如实标出来，别假装算过。
    """
    out = {"entry": entry, "slip": 0.0, "fee_rate": FEE_RATE,
           "partial": False, "estimated": False, "spec_note": ""}
    # 合约规格：数量要按步长取整、不能低于最小下单量。实盘会直接拒单，
    # 模拟盘不校验的话，用户会练出一堆实盘根本下不进去的仓位
    try:
        from handlers import sizing
        spec = await sizing.spec_for(symbol)
        if spec:
            qty = notional / entry if entry else 0
            mul = spec.get("multiplier") or 1
            if mul > 1:
                qty /= mul
            step = spec.get("qty_step") or 0
            if step > 0:
                qty = int(qty / step) * step
            mn = spec.get("min_qty") or 0
            if mn and qty < mn:
                out["spec_note"] = (f"⚠️ 实盘下不进去：算出 {qty:g}，"
                                    f"低于 {spec['symbol']} 的最小下单量 {mn:g}")
            elif mul > 1:
                out["spec_note"] = f"合约面值 ×{mul}，实盘对应 {qty:g} 张"
    except Exception as e:
        logging.debug(f"虚拟盘取合约规格失败 {symbol}: {e}")
    try:
        from handlers import econ
        mi = await econ.market_inputs(symbol, side, notional)
        slip = mi["slip_in"] or 0.0
        # 滑点方向：开多成交价被推高，开空被压低
        out["entry"] = entry * (1 + slip / 100) if side == "long" else entry * (1 - slip / 100)
        out["slip"] = slip if side == "long" else -slip
        out["fee_rate"] = mi["taker"]
        out["partial"] = mi.get("partial", False)
        out["estimated"] = True
    except Exception as e:
        logging.warning(f"虚拟盘摩擦估算失败 {symbol}: {e}")
    return out


def accrue_funding(pos, rate, now=None):
    """按持仓时长累计资金费。返回本次新增的成本（正=付出）。

    实盘里隔夜单的资金费经常比手续费还贵，模拟盘不扣就会让人以为
    「拿着不动没成本」——那是永续最贵的错觉之一。
    """
    import time as _t
    now = now or _t.time()
    last = pos.get("funding_ts") or pos.get("open_ts") or now
    hours = (now - last) / 3600
    if hours <= 0:
        return 0.0
    from handlers import econ
    notional = pos.get("margin", 0) * pos.get("lev", 1)
    cost = econ.funding_cost(notional, rate, hours, pos.get("side", "long"))
    pos["funding_paid"] = pos.get("funding_paid", 0.0) + cost
    pos["funding_ts"] = now
    return cost
MAX_LEV = 125             # 杠杆上限


def is_group(update: Update):
    return update.effective_chat.type in ("group", "supergroup")


# ── 标记价：优先 Bybit USDT 永续（与真实交易一致），查不到才退回 CoinGecko 现货 ──
_BYBIT_TICKERS = "https://api.bybit.com/v5/market/tickers"


async def get_price(symbol):
    """单币标记价。返回 {price, change} 或 None。优先 Bybit 永续。"""
    s = symbol.upper()
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(_BYBIT_TICKERS, params={"category": "linear", "symbol": f"{s}USDT"})
            d = r.json()
        lst = (d.get("result") or {}).get("list") or []
        if d.get("retCode") == 0 and lst:
            t = lst[0]
            return {"price": float(t["lastPrice"]),
                    "change": float(t.get("price24hPcnt") or 0) * 100}
    except Exception as e:
        logging.warning(f"vtrade Bybit 取价失败 {s}: {e}")
    return await _cg_price(s)          # 该币没有 Bybit 永续 → 退回现货


async def get_prices(symbols):
    """批量标记价 {sym: {price, change}}。一次拉 Bybit 全部永续，缺的再用 CoinGecko 补。"""
    want = {s.upper() for s in symbols}
    out = {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(_BYBIT_TICKERS, params={"category": "linear"})
            d = r.json()
        if d.get("retCode") == 0:
            for t in (d.get("result") or {}).get("list") or []:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT"):
                    continue
                base = sym[:-4]
                if base in want:
                    out[base] = {"price": float(t["lastPrice"]),
                                 "change": float(t.get("price24hPcnt") or 0) * 100}
    except Exception as e:
        logging.warning(f"vtrade Bybit 批量取价失败: {e}")
    missing = [s for s in want if s not in out]
    if missing:
        try:
            out.update(await _cg_prices(missing))
        except Exception as e:
            logging.warning(f"vtrade CoinGecko 补价失败: {e}")
    return out


async def _tradable(symbol):
    """Bybit 上有没有这个币的 USDT 永续。查不到清单时放行——
    合约清单接口挂了不该连练手都不让练，真开不了 get_price 那步自然会拦。"""
    try:
        from handlers import symbols as sy
        insts, _under = await sy.resolve(symbol)
        if not insts:
            return False
        return sy.preferred(insts) is not None or bool(insts)
    except Exception as e:
        logging.warning(f"虚拟盘校验币种失败 {symbol}: {e}")
        return True


def fmt(p):
    """自适应价格精度：大币两位小数，小币多给几位有效数字。"""
    if p is None:
        return "?"
    ap = abs(p)
    if ap >= 100:
        return f"{p:,.2f}"
    if ap >= 1:
        return f"{p:,.4f}"
    if ap >= 0.01:
        return f"{p:.5f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def _acct(uid):
    """取/建某用户的虚拟账户。"""
    data.setdefault("vtrade", {})
    a = data["vtrade"].get(uid)
    if a is None:
        a = {"balance": START_BALANCE, "positions": {}, "history": [], "chat_id": None}
        data["vtrade"][uid] = a
    a.setdefault("balance", START_BALANCE)
    a.setdefault("positions", {})
    a.setdefault("history", [])
    a.setdefault("chat_id", None)
    return a


def _pnl(pos, mark):
    """未实现盈亏（USDT）。多：(现价-入场)*张数；空反之。"""
    qty = pos["qty"]
    if pos["side"] == "long":
        return (mark - pos["entry"]) * qty
    return (pos["entry"] - mark) * qty


def _liq(pos):
    """理论爆仓价（逐仓，忽略维持保证金/手续费，实际会更早）。"""
    entry, lev = pos["entry"], pos["lev"]
    if pos["side"] == "long":
        return entry * (1 - 1 / lev)
    return entry * (1 + 1 / lev)


def _pos_line(sym, pos, mark):
    pnl = _pnl(pos, mark)
    roe = pnl / pos["margin"] * 100 if pos["margin"] else 0
    liq = _liq(pos)
    # 距爆仓还有多少（按现价到爆仓价的百分比）
    dist = (mark - liq) / mark * 100 if pos["side"] == "long" else (liq - mark) / mark * 100
    emoji = "🟢" if pnl >= 0 else "🔴"
    dir_txt = "多 📈" if pos["side"] == "long" else "空 📉"
    return (
        f"{emoji} *{sym}* {dir_txt} {pos['lev']}x\n"
        f"   入场 ${fmt(pos['entry'])} → 现价 ${fmt(mark)}\n"
        f"   保证金 ${pos['margin']:,.2f}｜仓位 ${pos['margin']*pos['lev']:,.2f}\n"
        f"   浮盈 {pnl:+,.2f} ({roe:+.1f}%)\n"
        f"   爆仓价 ${fmt(liq)}（距爆仓 {dist:+.1f}%）"
    )


# ============ 开仓 ============
async def vopen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await safe_reply(update.message, "🔒 虚拟交易涉及你的账户，请私聊我使用")
        return
    args = context.args
    if len(args) < 4:
        await safe_reply(update.message, 
            "📝 *开仓用法*\n"
            "`/vopen 币 方向 保证金 杠杆 [入场价]`\n\n"
            "例：`/vopen BTC long 1000 10`\n"
            "　= 用 1000U 保证金 10 倍做多 BTC（入场取现价）\n"
            "`/vopen ETH short 500 20 3800`\n"
            "　= 500U 20 倍做空 ETH，指定入场价 3800\n\n"
            "方向：`long`/`多`　`short`/`空`",
            parse_mode="Markdown")
        return
    symbol = args[0].upper()
    # 判定标准是「Bybit 有没有这个永续」，不是「COIN_IDS 里有没有」。
    # COIN_IDS 是 CoinGecko 的主流币映射，卡在这儿等于把中小市值币全挡在门外——
    # 而那恰恰是最该练手、也最容易亏钱的地方。
    if not await _tradable(symbol):
        await safe_reply(update.message,
            f"❌ Bybit 没有 {symbol} 的 USDT 永续（代号写错？或该币无永续）\n"
            f"用 `/sym {symbol}` 查它在哪个所、叫什么名字",
            parse_mode="Markdown")
        return
    side_raw = args[1].lower()
    if side_raw in ("long", "多", "buy", "l"):
        side = "long"
    elif side_raw in ("short", "空", "sell", "s"):
        side = "short"
    else:
        await safe_reply(update.message, "方向要填 long/多 或 short/空")
        return
    try:
        margin = float(args[2])
        lev = float(args[3])
    except ValueError:
        await safe_reply(update.message, "保证金和杠杆要是数字")
        return
    if margin <= 0:
        await safe_reply(update.message, "保证金要大于 0")
        return
    if not (1 <= lev <= MAX_LEV):
        await safe_reply(update.message, f"杠杆范围 1~{MAX_LEV} 倍")
        return

    uid = str(update.effective_user.id)
    a = _acct(uid)
    a["chat_id"] = update.effective_chat.id
    if symbol in a["positions"]:
        await safe_reply(update.message, 
            f"你已有 {symbol} 的持仓，先 `/vclose {symbol}` 平掉再开", parse_mode="Markdown")
        return

    # 入场价：指定 = **挂限价委托**（和实盘一样，等价格到了才成交），
    # 不指定 = 市价立刻成交。
    # 老行为是"指定价就假装以那个价成交了"——那会养出「我总能在想要的价位进场」
    # 这个最贵的错觉，而挂多远、等多久、要不要追，恰恰是最该练的东西。
    limit = None
    if len(args) >= 5:
        try:
            limit = float(args[4])
        except ValueError:
            await safe_reply(update.message, "入场价要是数字")
            return
        if limit <= 0:
            await safe_reply(update.message, "入场价要大于 0")
            return
    # 无论市价还是挂单都要先拿现价：挂单要判断"这个价现在是不是已经能成交了"
    try:
        r = await get_price(symbol)
    except Exception as e:
        logging.error(f"vopen 查价出错: {e}")
        r = None
    if not r:
        await safe_reply(update.message, "取现价失败，稍后再试")
        return
    entry = r["price"]

    from handlers import vorders as VO
    if limit and not VO.will_fill_now(side, limit, entry):
        cost = margin + margin * lev * FEE_RATE      # 冻结按兜底费率估，成交时按实际结
        if cost > a["balance"] + 1e-9:
            await safe_reply(update.message,
                f"💸 余额不足\n可用 ${a['balance']:,.2f}，"
                f"这张挂单要冻结 ${cost:,.2f}")
            return
        if len(a.get("orders", [])) >= VO.MAX_ORDERS:
            await safe_reply(update.message, f"挂单太多（上限 {VO.MAX_ORDERS} 张），先撤几张")
            return
        o = VO.place(a, VO.PERP, symbol, side, limit,
                     margin=margin, lev=lev, frozen=cost)
        dir_txt = "开多 📈" if side == "long" else "开空 📉"
        await safe_reply(update.message,
            f"📋 *限价委托已挂*\n{symbol} {dir_txt} {lev:g}x\n"
            f"挂单价 ${fmt(limit)}（现价 ${fmt(entry)}，差 "
            f"{(limit-entry)/entry*100:+.2f}%）\n"
            f"保证金 ${margin:,.2f}　已冻结 ${cost:,.2f}\n"
            f"到价自动成交，走挂单费率 {VO.MAKER_RATE*100:g}%（比吃单便宜）\n\n"
            f"撤单 `/vcancel {o['id']}`｜看挂单 `/vorders`", parse_mode="Markdown")
        return
    if limit:
        entry = limit          # 挂价已经能立刻成交 = 吃单，按这个价走

    notional = margin * lev
    # 和实盘同构：入场价要算上真实滑点，数量要过合约规格。
    # 不这么做的话，用户在模拟盘上养成的手感（"这个位置我能挂到"、"这个量能开"）
    # 到实盘全部失效——那比不练还糟，因为他会带着错误的信心上真钱。
    sim = await _real_frictions(symbol, side, notional, entry)
    entry = sim["entry"]
    fee = notional * sim["fee_rate"]
    cost = margin + fee
    if cost > a["balance"] + 1e-9:
        await safe_reply(update.message, 
            f"💸 余额不足\n可用 ${a['balance']:,.2f}，本单需 ${cost:,.2f}"
            f"（保证金 ${margin:,.2f} + 手续费 ${fee:,.2f}）")
        return

    text = open_position(a, symbol, side, margin, lev, entry, sim["fee_rate"],
                         slip=sim["slip"], partial=sim["partial"],
                         spec_note=sim.get("spec_note"))
    await safe_reply(update.message, text, parse_mode="Markdown")


def open_position(a, symbol, side, margin, lev, entry, fee_rate,
                  tp=None, sl=None, slip=0.0, partial=False, spec_note=None):
    """真正建仓的那一段。抽出来是为了让**挂单成交**走同一条路——
    两套结算逻辑迟早会分叉，而分叉的那天没人会发现（模拟盘不会有人对账）。"""
    notional = margin * lev
    fee = notional * fee_rate
    a["balance"] -= (margin + fee)
    a["positions"][symbol] = {
        "side": side, "margin": margin, "lev": lev, "entry": entry,
        "qty": notional / entry, "open_ts": time.time(), "open_fee": fee,
        "funding_paid": 0.0, "funding_ts": time.time(),
        "fee_rate": fee_rate,
    }
    if tp:
        a["positions"][symbol]["tp"] = tp
    if sl:
        a["positions"][symbol]["sl"] = sl
    save_data()
    liq = _liq(a["positions"][symbol])
    dir_txt = "做多 📈" if side == "long" else "做空 📉"
    return (
        f"✅ *虚拟开仓*\n"
        f"{symbol} {dir_txt} {lev:g}x\n"
        f"入场价 ${fmt(entry)}" + (f"（含滑点 {slip:+.3f}%）" if slip else "") + "\n"
        + ("⚠️ 盘口深度不够这个名义，实盘会**部分成交**\n" if partial else "")
        + (f"{spec_note}\n" if spec_note else "")
        + f"保证金 ${margin:,.2f}｜仓位 ${notional:,.2f}\n"
        f"手续费 -${fee:,.2f}（费率 {fee_rate*100:.3f}%）\n"
        + (f"止盈 ${fmt(tp)}\n" if tp else "")
        + (f"止损 ${fmt(sl)}\n" if sl else "")
        + f"理论爆仓价 ${fmt(liq)}\n"
        f"剩余可用 ${a['balance']:,.2f}\n\n"
        f"平仓 `/vclose {symbol}`｜查仓 `/vpos`\n"
        f"模拟盘，不构成投资建议")


# ============ 平仓 ============
async def vclose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    if not context.args:
        await safe_reply(update.message, "用法：`/vclose BTC` 全平，`/vclose BTC 50` 平一半", parse_mode="Markdown")
        return
    symbol = context.args[0].upper()
    uid = str(update.effective_user.id)
    a = _acct(uid)
    pos = a["positions"].get(symbol)
    if not pos:
        await safe_reply(update.message, f"你没有 {symbol} 的虚拟持仓")
        return
    # 平仓比例
    pct = 100.0
    if len(context.args) >= 2:
        try:
            pct = float(context.args[1])
        except ValueError:
            await safe_reply(update.message, "平仓比例要是数字（1~100）")
            return
        if not (0 < pct <= 100):
            await safe_reply(update.message, "平仓比例要在 1~100 之间")
            return
    try:
        r = await get_price(symbol)
    except Exception as e:
        logging.error(f"vclose 查价出错: {e}")
        r = None
    if not r:
        await safe_reply(update.message, f"取现价失败，稍后再试：{str(e)[:80]}")
        return
    mark = r["price"]

    frac = pct / 100.0
    close_margin = pos["margin"] * frac
    close_qty = pos["qty"] * frac
    # 平掉这部分的盈亏
    if pos["side"] == "long":
        pnl = (mark - pos["entry"]) * close_qty
    else:
        pnl = (pos["entry"] - mark) * close_qty
    close_fee = close_margin * pos["lev"] * pos.get("fee_rate", FEE_RATE)
    # 持仓期间的资金费：拿着不动是有成本的，这是永续最贵的错觉之一
    fund = 0.0
    try:
        from handlers import marketdata as md
        t = await md._get("/v5/market/tickers",
                          {"category": md.CAT, "symbol": md.norm(symbol)})
        rate = float(((t.get("list") or [{}])[0]).get("fundingRate") or 0)
        fund = accrue_funding(pos, rate) * frac
    except Exception as e:
        logging.warning(f"虚拟平仓资金费计算失败 {symbol}: {e}")
    net = pnl - close_fee - fund
    # 逐仓：亏损不超过这部分保证金（超了就是爆仓，balance 只退到 0）
    ret = max(0.0, close_margin + net)
    a["balance"] += ret
    roe = net / close_margin * 100 if close_margin else 0

    if pct >= 100:
        del a["positions"][symbol]
        remain_txt = ""
    else:
        pos["margin"] -= close_margin
        pos["qty"] -= close_qty
        remain_txt = f"\n剩余仓位 {100-pct:g}%（保证金 ${pos['margin']:,.2f}）"

    a["history"].append({
        "sym": symbol, "side": pos["side"], "lev": pos["lev"],
        "entry": pos["entry"], "exit": mark, "margin": close_margin,
        "pnl": net, "roe": roe, "ts": time.time(),
        # 下面几个是给复盘层用的：没有 dur 就算不出「持仓超一天」这类行为标签
        "dur": time.time() - (pos.get("open_ts") or time.time()),
        "value": close_margin * pos["lev"], "fee": close_fee, "funding": fund,
        "exit_kind": "manual",
    })
    save_data()
    emoji = "🟢" if net >= 0 else "🔴"
    word = "止盈" if net >= 0 else "止损"
    await safe_reply(update.message, 
        f"{emoji} *虚拟平仓 {word}* {'(部分)' if pct<100 else ''}\n"
        f"{symbol} {'多' if pos['side']=='long' else '空'} {pos['lev']:g}x\n"
        f"入场 ${fmt(pos['entry'])} → 平仓 ${fmt(mark)}\n"
        f"实现盈亏 {net:+,.2f} ({roe:+.1f}%)\n"
        f"　毛盈亏 {pnl:+,.2f}｜手续费 -{close_fee:,.2f}"
        + (f"｜资金费 {-fund:+,.2f}" if fund else "")
        + f"{remain_txt}\n"
        f"账户可用 ${a['balance']:,.2f}",
        parse_mode="Markdown")


# ============ 查持仓 + 账户 ============
async def vpos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    a = _acct(uid)
    positions = a["positions"]
    if not positions:
        await safe_reply(update.message, 
            f"💼 *虚拟合约账户*\n"
            f"可用余额 ${a['balance']:,.2f}（初始 ${START_BALANCE:,.0f}）\n"
            f"当前无持仓。\n\n"
            f"开仓：`/vopen BTC long 1000 10`\n"
            f"历史：`/vhistory`",
            parse_mode="Markdown")
        return
    try:
        prices = await get_prices(list(positions.keys()))
    except Exception as e:
        logging.error(f"vpos 查价出错: {e}")
        await safe_reply(update.message, f"查价失败，稍后再试：{str(e)[:80]}")
        return

    lines = ["💼 *虚拟合约账户*\n"]
    total_pnl = 0.0
    locked_margin = 0.0
    for sym, pos in positions.items():
        info = prices.get(sym)
        if not info:
            lines.append(f"• {sym}: 取价失败")
            locked_margin += pos["margin"]
            continue
        mark = info["price"]
        total_pnl += _pnl(pos, mark)
        locked_margin += pos["margin"]
        lines.append(_pos_line(sym, pos, mark))
    equity = a["balance"] + locked_margin + total_pnl
    e = "🟢" if total_pnl >= 0 else "🔴"
    lines.append("─────────")
    lines.append(f"可用余额 ${a['balance']:,.2f}")
    lines.append(f"持仓保证金 ${locked_margin:,.2f}")
    lines.append(f"{e} 未实现盈亏 {total_pnl:+,.2f}")
    lines.append(f"💰 账户权益 ${equity:,.2f}（初始 ${START_BALANCE:,.0f}, {(equity/START_BALANCE-1)*100:+.1f}%）")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 刷新", callback_data="vpos_refresh"),
        InlineKeyboardButton("📜 历史", callback_data="vhist_show"),
    ]])
    await safe_reply(update.message, "\n".join(lines), reply_markup=kb, parse_mode="Markdown")


# ============ 历史 + 胜率 ============
def _history_text(a):
    hist = a.get("history", [])
    if not hist:
        return "📜 *虚拟交易历史*\n还没有平仓记录。开一单试试：`/vopen BTC long 1000 10`"
    wins = [h for h in hist if h["pnl"] >= 0]
    total_pnl = sum(h["pnl"] for h in hist)
    win_rate = len(wins) / len(hist) * 100
    gross_win = sum(h["pnl"] for h in wins)
    gross_loss = -sum(h["pnl"] for h in hist if h["pnl"] < 0)
    pf = (gross_win / gross_loss) if gross_loss > 0 else 0
    lines = [
        "📜 *虚拟交易历史*\n",
        f"总交易 {len(hist)} 笔｜胜率 {win_rate:.0f}% ({len(wins)}胜{len(hist)-len(wins)}负)",
        f"累计盈亏 {total_pnl:+,.2f}"
        + (f"｜盈亏比 {pf:.2f}" if gross_loss > 0 else ""),
        "\n近 10 笔：",
    ]
    for h in reversed(hist[-10:]):
        emoji = "🟢" if h["pnl"] >= 0 else "🔴"
        dir_txt = "多" if h["side"] == "long" else "空"
        lines.append(
            f"{emoji} {h['sym']} {dir_txt}{h['lev']:g}x  "
            f"{h['pnl']:+,.2f} ({h['roe']:+.0f}%)")
    return "\n".join(lines)


async def vhistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    a = _acct(uid)
    await safe_reply(update.message, _history_text(a), parse_mode="Markdown")


# ============ 重置账户 ============
async def vreset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    # 二次确认
    if not context.args or context.args[0] != "confirm":
        await safe_reply(update.message, 
            "⚠️ 重置会清空当前所有虚拟持仓和历史，本金回到 "
            f"${START_BALANCE:,.0f}。\n确认请发 `/vreset confirm`", parse_mode="Markdown")
        return
    data.setdefault("vtrade", {})
    data["vtrade"][uid] = {
        "balance": START_BALANCE, "positions": {}, "history": [],
        "chat_id": update.effective_chat.id,
    }
    save_data()
    await safe_reply(update.message, 
        f"🔄 已重置虚拟账户，本金 ${START_BALANCE:,.0f}。开仓：`/vopen BTC long 1000 10`",
        parse_mode="Markdown")


# ============ 菜单按钮渲染（供 menu.button_handler 调用）============
async def render_vpos(query):
    uid = str(query.from_user.id)
    a = _acct(uid)
    positions = a["positions"]
    from handlers.menu import back_to
    if not positions:
        await safe_edit(query, 
            f"💼 *虚拟合约账户*\n可用余额 ${a['balance']:,.2f}（初始 ${START_BALANCE:,.0f}）\n"
            f"当前无持仓。\n\n用命令开仓：`/vopen BTC long 1000 10`",
            reply_markup=back_to("cat_vtrade"), parse_mode="Markdown")
        return
    try:
        prices = await get_prices(list(positions.keys()))
    except Exception:
        prices = {}
    lines = ["💼 *虚拟合约账户*\n"]
    total_pnl = 0.0
    locked = 0.0
    for sym, pos in positions.items():
        info = prices.get(sym)
        locked += pos["margin"]
        if not info:
            lines.append(f"• {sym}: 取价失败")
            continue
        total_pnl += _pnl(pos, info["price"])
        lines.append(_pos_line(sym, pos, info["price"]))
    equity = a["balance"] + locked + total_pnl
    e = "🟢" if total_pnl >= 0 else "🔴"
    lines += ["─────────", f"可用 ${a['balance']:,.2f}｜保证金 ${locked:,.2f}",
              f"{e} 浮盈 {total_pnl:+,.2f}",
              f"💰 权益 ${equity:,.2f}（{(equity/START_BALANCE-1)*100:+.1f}%）"]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 刷新", callback_data="vpos_refresh"),
        InlineKeyboardButton("📜 历史", callback_data="vhist_show"),
    ], [InlineKeyboardButton("⬅️ 返回", callback_data="cat_vtrade")]])
    await safe_edit(query, "\n".join(lines), reply_markup=kb, parse_mode="Markdown")


async def render_vhist(query):
    uid = str(query.from_user.id)
    a = _acct(uid)
    from handlers.menu import back_to
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 刷新", callback_data="vhist_show"),
        InlineKeyboardButton("💼 持仓", callback_data="vpos_refresh"),
    ], [InlineKeyboardButton("⬅️ 返回", callback_data="cat_vtrade")]])
    from handlers.util import safe_edit
    await safe_edit(query, _history_text(a), reply_markup=kb, parse_mode="Markdown")


# ============ 后台自动爆仓监控（job，每 60s）============
def _check_tpsl(pos, mark):
    """价格是否触及这个仓的止盈/止损。返回 (类型, 成交价) 或 None。

    触及价用**挂单价**而不是当前价：实盘条件单在触发价成交（滑点另算），
    用当前价会让模拟盘的成交价系统性地优于实盘。
    """
    side = pos.get("side")
    sl, tp = pos.get("sl"), pos.get("tp")
    if sl:
        if (side == "long" and mark <= sl) or (side == "short" and mark >= sl):
            return "止损", sl
    if tp:
        if (side == "long" and mark >= tp) or (side == "short" and mark <= tp):
            return "止盈", tp
    return None


async def _auto_close(a, sym, pos, price, kind):
    """止盈/止损自动平仓。复用和手动平仓一致的成本口径。"""
    qty = pos["qty"]
    if pos["side"] == "long":
        pnl = (price - pos["entry"]) * qty
    else:
        pnl = (pos["entry"] - price) * qty
    fee = pos["margin"] * pos["lev"] * pos.get("fee_rate", FEE_RATE)
    fund = pos.get("funding_paid", 0.0)
    net = pnl - fee - fund
    a["balance"] += max(0.0, pos["margin"] + net)
    roe = net / pos["margin"] * 100 if pos["margin"] else 0
    a["history"].append({
        "sym": sym, "side": pos["side"], "lev": pos["lev"],
        "entry": pos["entry"], "exit": price, "margin": pos["margin"],
        "pnl": net, "roe": roe, "ts": time.time(), "auto": kind,
        "dur": time.time() - (pos.get("open_ts") or time.time()),
        "value": pos["margin"] * pos["lev"], "fee": fee, "funding": fund,
        "exit_kind": "止盈" if kind == "止盈" else "止损",
    })
    a["positions"].pop(sym, None)
    save_data()
    emoji = "🟢" if net >= 0 else "🔴"
    return (f"{emoji} *虚拟{kind}自动平仓*\n"
            f"{sym} {'多' if pos['side']=='long' else '空'} {pos['lev']:g}x\n"
            f"入场 ${fmt(pos['entry'])} → {kind} ${fmt(price)}\n"
            f"实现盈亏 {net:+,.2f} ({roe:+.1f}%)\n"
            f"　毛 {pnl:+,.2f}｜手续费 -{fee:,.2f}"
            + (f"｜资金费 {-fund:+,.2f}" if fund else "") + "\n"
            f"账户可用 ${a['balance']:,.2f}")


async def vtpsl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vtpsl BTC tp=70000 sl=60000 —— 给虚拟仓挂止盈止损（0 = 清除）。

    合约和现货共用这一个命令：同一件事不该有两个名字，他记不住第二个。
    默认给**合约仓**（那边有爆仓风险，紧迫性更高）；没有合约仓就落到现货持币上。
    两边都有时加一个 `现货` 指名道姓。
    """
    if is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    uid = str(update.effective_user.id)
    a = _acct(uid)
    args = context.args or []
    if not args:
        await safe_reply(update.message,
            "🎯 *虚拟止盈止损*\n\n`/vtpsl BTC tp=70000 sl=60000`\n"
            "　只设一个也行；填 `0` 清除\n"
            "　合约仓和现货持币都能设；两边都有时加「现货」指定：\n"
            "　`/vtpsl BTC 现货 sl=60000`\n\n"
            "后台每 60 秒检查一次，触及就自动平仓并通知——"
            "和实盘的条件单同构，练出来的体感能迁移。\n"
            "现货触发时会**全卖**，并撤掉这个币挂着的限价卖单"
            "（否则被锁住的币出不来，止损就成了半个止损）。",
            parse_mode="Markdown")
        return
    from handlers import vspot as S
    sym = args[0].upper()
    rest, force_spot = [], False
    for x in args[1:]:
        if str(x).lower() in ("spot", "现货"):
            force_spot = True
        else:
            rest.append(x)
    pos = a.get("positions", {}).get(sym)
    hold = S.holding(a, sym)

    if force_spot or (not pos and hold):
        if not hold:
            await safe_reply(update.message, f"没有 {sym} 的现货持币")
            return
        pairs = S.parse_tpsl(rest)
        if not pairs:
            await safe_reply(update.message,
                "没看懂参数，用法 `/vtpsl BTC 现货 tp=70000 sl=60000`",
                parse_mode="Markdown")
            return
        r = await get_price(sym)
        if not r:
            await safe_reply(update.message, "取现价失败，稍后再试（现价是校验方向用的）")
            return
        changed, err = S.apply_tpsl(hold, r["price"], pairs)
        if err:
            await safe_reply(update.message, err, parse_mode="Markdown")
            return
        save_data()
        await safe_reply(update.message,
            f"✅ {sym} 现货已设：{'、'.join(changed)}（现价 ${fmt(r['price'])}）\n"
            f"后台每 60 秒检查，触及自动全卖")
        return

    if not pos:
        await safe_reply(update.message, f"没有 {sym} 的虚拟持仓")
        return
    changed = []
    for kv in rest:
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        k = k.strip().lower()
        if k not in ("tp", "sl"):
            continue
        try:
            px = float(v)
        except ValueError:
            await safe_reply(update.message, f"{k} 的值要是数字")
            return
        if px <= 0:
            pos.pop(k, None)
            changed.append(f"清除{k.upper()}")
            continue
        # 方向自洽：做多的止损必须在入场之下，止盈在之上。写反了等于把
        # 止损变成止盈，实盘里这是会真亏钱的错误，模拟盘也不该放过去
        e = pos["entry"]
        if pos["side"] == "long":
            bad = (k == "sl" and px >= e) or (k == "tp" and px <= e)
        else:
            bad = (k == "sl" and px <= e) or (k == "tp" and px >= e)
        if bad:
            await safe_reply(update.message,
                f"❌ {k.upper()} {fmt(px)} 方向不对："
                f"{'做多' if pos['side']=='long' else '做空'}的止损要在入场"
                f"{'下方' if pos['side']=='long' else '上方'}、止盈在另一侧。已拒绝。")
            return
        pos[k] = px
        changed.append(f"{k.upper()}={fmt(px)}")
    if not changed:
        await safe_reply(update.message, "没看懂参数，用法 `/vtpsl BTC tp=70000 sl=60000`",
                         parse_mode="Markdown")
        return
    save_data()
    # 两边都有仓时说清这次动的是哪个，否则他会以为现货也一起设了
    also = (f"\n（这是**合约仓**；你的 {sym} 现货持币要单独设："
            f"`/vtpsl {sym} 现货 sl=…`）" if hold else "")
    await safe_reply(update.message,
                     f"✅ {sym} 已设：{'、'.join(changed)}\n"
                     f"后台每 60 秒检查，触及自动平仓{also}",
                     parse_mode="Markdown")


async def check_liquidations(context: ContextTypes.DEFAULT_TYPE):
    # 挂单和爆仓/止盈损查的是同一批价格，放同一个任务里跑，别多打一轮接口
    try:
        from handlers import vorders as VO
        await VO.check_orders(context)
    except Exception as e:
        logging.error(f"挂单检查出错: {e}")
    accts = data.get("vtrade", {})
    if not accts:
        return
    # 汇总所有用户持仓的币，一次批量查价。
    # ⚠️ 现货持币也要算进来：只按合约持仓收集的话，**只玩现货的人这个任务
    # 一次都不会跑**（下面 `if not syms: return` 直接退出），
    # 现货止盈止损就永远不触发——而且它不报错，是纯静默失效。
    syms = set()
    for a in accts.values():
        syms.update(a.get("positions", {}).keys())
        syms.update(s for s, h in (a.get("spot") or {}).items()
                    if h.get("tp") or h.get("sl"))
    if not syms:
        return
    try:
        prices = await get_prices(list(syms))
    except Exception as e:
        logging.error(f"爆仓监控查价出错: {e}")
        return

    # 现货止盈损：和爆仓/挂单共用上面这批价格
    try:
        from handlers import vspot as S
        await S.check_tpsl(context, prices)
    except Exception as e:
        logging.error(f"现货止盈损检查出错: {e}")

    changed = False
    for uid, a in accts.items():
        chat_id = a.get("chat_id")
        for sym, pos in list(a.get("positions", {}).items()):
            info = prices.get(sym)
            if not info:
                continue
            mark = info["price"]
            # 止盈止损先于爆仓判定：实盘里 TP/SL 是挂在交易所的条件单，
            # 价格一到就成交，根本走不到爆仓。不模拟这一层的话，模拟盘会
            # 把「本来该被止损带走的单」一路拿到爆仓，练出完全错误的体感。
            tp_sl = _check_tpsl(pos, mark)
            if tp_sl:
                kind, px = tp_sl
                msg = await _auto_close(a, sym, pos, px, kind)
                changed = True
                if chat_id and msg:
                    try:
                        await context.bot.send_message(chat_id=chat_id, text=msg,
                                                       parse_mode="Markdown")
                    except Exception as e:
                        logging.error(f"虚拟{kind}通知失败 {chat_id}: {e}")
                continue
            liq = _liq(pos)
            hit = (pos["side"] == "long" and mark <= liq) or \
                  (pos["side"] == "short" and mark >= liq)
            if not hit:
                continue
            # 爆仓：保证金归零，记历史，通知
            a["history"].append({
                "sym": sym, "side": pos["side"], "lev": pos["lev"],
                "entry": pos["entry"], "exit": mark, "margin": pos["margin"],
                "pnl": -pos["margin"], "roe": -100.0, "ts": time.time(),
                "dur": time.time() - (pos.get("open_ts") or time.time()),
                "value": pos["margin"] * pos["lev"], "fee": 0.0,
                "funding": pos.get("funding_paid", 0.0), "exit_kind": "爆仓",
            })
            del a["positions"][sym]
            changed = True
            if chat_id:
                dir_txt = "多" if pos["side"] == "long" else "空"
                try:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(f"💥 *虚拟爆仓*\n"
                              f"{sym} {dir_txt} {pos['lev']:g}x 触及爆仓价 ${fmt(liq)}\n"
                              f"现价 ${fmt(mark)}，保证金 ${pos['margin']:,.2f} 全损 (-100%)\n"
                              f"可用余额 ${a['balance']:,.2f}\n"
                              f"模拟盘，高杠杆爆仓就是这么快"),
                        parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"爆仓通知失败: {e}")
    if changed:
        save_data()
