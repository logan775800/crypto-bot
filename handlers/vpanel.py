"""虚拟交易台 —— 全按钮操作，不用记任何命令。

为什么必须做：功能堆到 `/vopen /vclose /vtpsl /vbuy /vsell /vorders /vcancel`
七八个命令之后，等于没有入口——他自己都要翻聊天记录找，新用户更是无从下手。

刻意和**实盘交易台**（handlers/rtrade.py 的 t* 那套）走同一套操作路径：
选币 → 方向 → 杠杆 → 保证金 → 市价/限价。
模拟盘的意义是手感能迁移，如果模拟盘点三下、实盘点五下，那点手感也白练了。

只有两处需要打字，因为它们没有合理的档位可选：
  • 限价单的**价格**（每个币量级不同，给不出通用档位）
  • 「其他币」的**代号**
其余（方向、杠杆、保证金、买入金额、平仓比例）全是按钮。

回调命名空间 `vg:`（virtual guided）。和实盘的 `t*` 分开——
namespace 撞车会让"点了模拟盘的按钮结果动了实盘"这种事变得可能。
"""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from handlers.util import safe_edit
from handlers import guided
from handlers import vorders as VO
from handlers import vspot as S
from handlers.vtrade import (_acct, fmt, get_price, get_prices, _pnl, _liq,
                             START_BALANCE, MAX_LEV, open_position, FEE_RATE)

log = logging.getLogger(__name__)

COMMON = ["BTC", "ETH", "SOL", "BNB", "XRP", "DOGE"]
LEVS = [1, 3, 5, 10, 20, 50, 100]
MARGINS = [100, 500, 1000, 2000, 5000]
SPOT_AMOUNTS = [100, 500, 1000, 2000, 5000]


def _kb(rows):
    return InlineKeyboardMarkup(rows)


def _back(to="vg:home"):
    return InlineKeyboardButton("⬅️ 返回", callback_data=to)


# ── 首页：一屏看完 + 所有动作都在手边 ────────────────────────
async def home(query):
    a = _acct(str(query.from_user.id))
    pos = a.get("positions", {})
    spot = a.get("spot", {})
    orders = a.get("orders", [])
    syms = set(pos) | set(spot)
    prices = {}
    if syms:
        try:
            prices = await get_prices(list(syms))
        except Exception as e:
            log.warning(f"虚拟台取价失败: {e}")

    lines = ["🎮 *虚拟交易台*　真实行情，不碰真钱"]
    locked = upnl = spot_val = 0.0
    if pos:
        lines.append("")
        lines.append("*合约持仓*")
        for sym, p in pos.items():
            locked += p["margin"]
            mark = (prices.get(sym) or {}).get("price")
            if not mark:
                lines.append(f"　{sym}　取价失败")
                continue
            pl = _pnl(p, mark)
            upnl += pl
            roe = pl / p["margin"] * 100 if p["margin"] else 0
            d = "多" if p["side"] == "long" else "空"
            lines.append(f"　{'🟢' if pl >= 0 else '🔴'} {sym} {d} {p['lev']:g}x"
                         f"　{pl:+,.2f} ({roe:+.1f}%)")
            lines.append(f"　　入场 ${fmt(p['entry'])}　爆仓 ${fmt(_liq(p))}")
    if spot:
        lines.append("")
        lines.append("*现货持币*")
        for sym, h in spot.items():
            mark = (prices.get(sym) or {}).get("price")
            avg = h["cost"] / h["qty"] if h["qty"] else 0
            if mark:
                val = h["qty"] * mark
                spot_val += val
                pl = val - h["cost"]
                lines.append(f"　{'🟢' if pl >= 0 else '🔴'} {sym}　{h['qty']:.6g} 个"
                             f"　{pl:+,.2f}")
                lines.append(f"　　均价 ${fmt(avg)}　现价 ${fmt(mark)}")
            else:
                lines.append(f"　{sym}　{h['qty']:.6g} 个　均价 ${fmt(avg)}")
            if h.get("tp") or h.get("sl"):
                lines.append(f"　　🎯 止盈 {fmt(h['tp']) if h.get('tp') else '—'}"
                             f"　止损 {fmt(h['sl']) if h.get('sl') else '—'}")
    if orders:
        lines.append("")
        lines.append(f"*委托单* {len(orders)} 张（点下面「委托单」管理）")

    equity = a["balance"] + locked + upnl + spot_val
    lines += ["━━━━━━━━━━━━━━",
              f"可用 ${a['balance']:,.2f}　合约保证金 ${locked:,.2f}",
              f"总权益 ${equity:,.2f}（{(equity/START_BALANCE-1)*100:+.1f}%）"]
    if not pos and not spot and not orders:
        lines.append("")
        lines.append("还没有任何持仓。点下面的按钮就能开始，不用记命令。")

    rows = []
    for sym in pos:
        rows.append([
            InlineKeyboardButton(f"平50% {sym}", callback_data=f"vg:cl:{sym}:50"),
            InlineKeyboardButton("全平", callback_data=f"vg:cl:{sym}:100"),
            InlineKeyboardButton("改止损", callback_data=f"vg:sl:{sym}"),
        ])
    for sym in spot:
        # 三个按钮，和上面合约那行**同一个形状**（平/全平/改止损）。
        # 现货少一个止盈损入口的话，"练手感"就在这里断了一截
        rows.append([
            InlineKeyboardButton(f"卖50% {sym}", callback_data=f"vg:sl50:{sym}"),
            InlineKeyboardButton("全卖", callback_data=f"vg:sall:{sym}"),
            InlineKeyboardButton("🎯止盈损", callback_data=f"vg:ssl:{sym}"),
        ])
    rows.append([InlineKeyboardButton("➕ 开合约", callback_data="vg:open"),
                 InlineKeyboardButton("🛒 买现货", callback_data="vg:buy")])
    rows.append([InlineKeyboardButton(f"📋 委托单({len(orders)})", callback_data="vg:ord"),
                 InlineKeyboardButton("📜 历史胜率", callback_data="vhist_show")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data="vg:home"),
                 InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")])
    await safe_edit(query, "\n".join(lines), reply_markup=_kb(rows),
                    parse_mode="Markdown")


# ── 引导开仓：币 → 方向 → 杠杆 → 保证金 → 市价/限价 ──────────
async def pick_symbol(query, what):
    """what = perp / spot，两条流程共用这一屏。"""
    rows = [[InlineKeyboardButton(c, callback_data=f"vg:{what}sym:{c}")
             for c in COMMON[i:i + 3]] for i in (0, 3)]
    rows.append([InlineKeyboardButton("✍️ 其他币", callback_data=f"vg:coin:{what}")])
    rows.append([_back()])
    title = "➕ *开合约*　第 1 步：选币" if what == "perp" else "🛒 *买现货*　第 1 步：选币"
    await safe_edit(query, title + "\n\n没有的币点「其他币」手动输入",
                    reply_markup=_kb(rows), parse_mode="Markdown")


async def pick_side(query, sym):
    rows = [[InlineKeyboardButton("📈 做多", callback_data=f"vg:side:{sym}:long"),
             InlineKeyboardButton("📉 做空", callback_data=f"vg:side:{sym}:short")],
            [InlineKeyboardButton("⬅️ 换币", callback_data="vg:open"), _back()]]
    r = await get_price(sym)
    px = f"现价 ${fmt(r['price'])}" if r else "取价失败"
    await safe_edit(query, f"➕ *{sym}*　{px}\n\n第 2 步：方向",
                    reply_markup=_kb(rows), parse_mode="Markdown")


async def pick_lev(query, sym, side):
    rows = [[InlineKeyboardButton(f"{l}x", callback_data=f"vg:lev:{sym}:{side}:{l}")
             for l in LEVS[i:i + 4]] for i in (0, 4)]
    rows.append([InlineKeyboardButton("⬅️ 换方向", callback_data=f"vg:persym:{sym}"),
                 _back()])
    d = "做多 📈" if side == "long" else "做空 📉"
    await safe_edit(query, f"➕ *{sym} {d}*\n\n第 3 步：杠杆\n"
                           f"杠杆越高爆仓价越近——{LEVS[3]}x 大约跌 10% 就爆",
                    reply_markup=_kb(rows), parse_mode="Markdown")


async def pick_margin(query, sym, side, lev):
    a = _acct(str(query.from_user.id))
    bal = a["balance"]
    rows = [[InlineKeyboardButton(f"{m}U", callback_data=f"vg:mgn:{sym}:{side}:{lev}:{m}")
             for m in MARGINS[i:i + 3] if m <= bal] for i in (0, 3)]
    rows = [r for r in rows if r]
    pct = [int(bal * p) for p in (0.1, 0.25, 0.5)]
    rows.append([InlineKeyboardButton(f"{int(p*100)}%仓 {v}U",
                                      callback_data=f"vg:mgn:{sym}:{side}:{lev}:{v}")
                 for p, v in zip((0.1, 0.25, 0.5), pct) if v >= 10])
    rows = [r for r in rows if r]
    rows.append([InlineKeyboardButton("⬅️ 换杠杆", callback_data=f"vg:side:{sym}:{side}"),
                 _back()])
    d = "做多 📈" if side == "long" else "做空 📉"
    await safe_edit(query, f"➕ *{sym} {d} {lev}x*\n\n第 4 步：保证金"
                           f"（可用 ${bal:,.2f}）",
                    reply_markup=_kb(rows), parse_mode="Markdown")


async def pick_type(query, sym, side, lev, margin):
    rows = [[InlineKeyboardButton("⚡ 市价成交", callback_data=f"vg:mkt:{sym}:{side}:{lev}:{margin}")],
            [InlineKeyboardButton("📋 挂限价单", callback_data=f"vg:lim:{sym}:{side}:{lev}:{margin}")],
            [InlineKeyboardButton("⬅️ 换保证金", callback_data=f"vg:lev:{sym}:{side}:{lev}"),
             _back()]]
    d = "做多 📈" if side == "long" else "做空 📉"
    r = await get_price(sym)
    px = f"现价 ${fmt(r['price'])}\n" if r else ""
    await safe_edit(query,
        f"➕ *{sym} {d} {lev}x*　保证金 {margin}U\n{px}\n"
        f"第 5 步：怎么进场\n"
        f"　⚡ 市价 = 立刻成交，按吃单费率\n"
        f"　📋 限价 = 挂在你要的价位等，成交费率更低（0.02%）",
        reply_markup=_kb(rows), parse_mode="Markdown")


async def do_market(query, context, sym, side, lev, margin):
    a = _acct(str(query.from_user.id))
    a["chat_id"] = query.message.chat_id if query.message else None
    r = await get_price(sym)
    if not r:
        await safe_edit(query, "取价失败，稍后再试", reply_markup=_kb([[_back()]]))
        return
    if margin + margin * lev * FEE_RATE > a["balance"] + 1e-9:
        await safe_edit(query, f"💸 余额不足（可用 ${a['balance']:,.2f}）",
                        reply_markup=_kb([[_back()]]))
        return
    from handlers.vtrade import _real_frictions
    sim = await _real_frictions(sym, side, margin * lev, r["price"])
    text = open_position(a, sym, side, margin, lev, sim["entry"], sim["fee_rate"],
                         slip=sim["slip"], partial=sim["partial"],
                         spec_note=sim.get("spec_note"))
    await safe_edit(query, text, reply_markup=_kb([
        [InlineKeyboardButton("🎯 设止盈止损", callback_data=f"vg:sl:{sym}")],
        [InlineKeyboardButton("⬅️ 回交易台", callback_data="vg:home")]]),
        parse_mode="Markdown")


# ── 现货 ────────────────────────────────────────────────────
async def pick_spot_amount(query, sym):
    a = _acct(str(query.from_user.id))
    bal = a["balance"]
    rows = [[InlineKeyboardButton(f"{m}U", callback_data=f"vg:sbuy:{sym}:{m}")
             for m in SPOT_AMOUNTS[i:i + 3] if m <= bal] for i in (0, 3)]
    rows = [r for r in rows if r]
    half = int(bal * 0.5)
    if half >= S.MIN_QUOTE:
        rows.append([InlineKeyboardButton(f"半仓 {half}U",
                                          callback_data=f"vg:sbuy:{sym}:{half}")])
    rows.append([InlineKeyboardButton("⬅️ 换币", callback_data="vg:buy"), _back()])
    r = await get_price(sym)
    px = f"现价 ${fmt(r['price'])}\n" if r else ""
    await safe_edit(query, f"🛒 *买入 {sym}*\n{px}\n花多少 U？（可用 ${bal:,.2f}）\n"
                           f"现货不会爆仓、不扣资金费",
                    reply_markup=_kb(rows), parse_mode="Markdown")


async def do_spot_buy(query, sym, quote):
    a = _acct(str(query.from_user.id))
    a["chat_id"] = query.message.chat_id if query.message else None
    if quote > a["balance"] + 1e-9:
        await safe_edit(query, f"💸 余额不足（可用 ${a['balance']:,.2f}）",
                        reply_markup=_kb([[_back()]]))
        return
    r = await get_price(sym)
    if not r:
        await safe_edit(query, "取价失败，稍后再试", reply_markup=_kb([[_back()]]))
        return
    rows = [[InlineKeyboardButton("⚡ 市价买入", callback_data=f"vg:sgo:{sym}:{quote}")],
            [InlineKeyboardButton("📋 挂限价买单", callback_data=f"vg:slim:{sym}:{quote}")],
            [InlineKeyboardButton("⬅️ 换金额", callback_data=f"vg:spotsym:{sym}"), _back()]]
    await safe_edit(query,
        f"🛒 *买入 {sym}*　${quote:,.0f}\n现价 ${fmt(r['price'])}\n\n"
        f"　⚡ 市价 = 立刻买到\n　📋 限价 = 挂低价等回落，成交费率更低",
        reply_markup=_kb(rows), parse_mode="Markdown")


async def do_spot_market(query, sym, quote):
    a = _acct(str(query.from_user.id))
    r = await get_price(sym)
    if not r:
        await safe_edit(query, "取价失败，稍后再试", reply_markup=_kb([[_back()]]))
        return
    text = S.settle(a, sym, "buy", r["price"], S.TAKER, quote=quote)
    await safe_edit(query, text, reply_markup=_kb([[_back()]]), parse_mode="Markdown")


async def do_spot_sell(query, sym, pct):
    a = _acct(str(query.from_user.id))
    free = S.sellable(a, sym)
    if free <= 0:
        await safe_edit(query, f"{sym} 没有可卖的（可能都挂在限价卖单里）",
                        reply_markup=_kb([[_back()]]))
        return
    qty = free * pct / 100
    r = await get_price(sym)
    if not r:
        await safe_edit(query, "取价失败，稍后再试", reply_markup=_kb([[_back()]]))
        return
    text = S.settle(a, sym, "sell", r["price"], S.TAKER, qty=qty)
    await safe_edit(query, text, reply_markup=_kb([[_back()]]), parse_mode="Markdown")


# ── 委托单：每张一个撤单按钮 ─────────────────────────────────
async def orders_panel(query):
    a = _acct(str(query.from_user.id))
    rows_o = a.get("orders", [])
    syms = {o["sym"] for o in rows_o}
    prices = {}
    if syms:
        try:
            prices = await get_prices(list(syms))
        except Exception as e:
            log.warning(f"挂单面板取价失败: {e}")
    rows = [[InlineKeyboardButton(f"❌ 撤 {o['id']} {o['sym']} {VO.side_cn(o)}",
                                  callback_data=f"vg:cx:{o['id']}")]
            for o in rows_o]
    if rows_o:
        rows.append([InlineKeyboardButton("❌ 全部撤销", callback_data="vg:cx:all")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data="vg:ord"), _back()])
    await safe_edit(query, VO.render(a, prices), reply_markup=_kb(rows),
                    parse_mode="Markdown")


async def do_cancel(query, key):
    a = _acct(str(query.from_user.id))
    if key == "all":
        gone = VO.cancel_all(a)
    else:
        one = VO.cancel(a, key)
        gone = [one] if one else []
    if not gone:
        await query.answer("这张单已经不在了")
    else:
        await query.answer(f"已撤 {len(gone)} 张")
    await orders_panel(query)


# ── 平仓 / 止损：复用已有逻辑，只是换成按钮触发 ───────────────
async def do_close(query, sym, pct):
    from handlers.vtrade import _auto_close
    a = _acct(str(query.from_user.id))
    pos = a.get("positions", {}).get(sym)
    if not pos:
        await query.answer("这个仓已经不在了")
        await home(query)
        return
    r = await get_price(sym)
    if not r:
        await query.answer("取价失败")
        return
    if pct >= 100:
        text = await _auto_close(a, sym, pos, r["price"], "手动平仓")
    else:
        # 部分平仓：按比例拆出一个"子仓"去结算，剩下的等比缩小。
        # ⚠️ _auto_close 会把整个仓从账户里 pop 掉（它本来只服务全平），
        # 所以结算完必须**把剩下的那部分放回去**，否则平 50% 等于全平。
        # 资金费也按比例分摊，不然剩余仓位会被重复扣一遍已付的费用。
        f = pct / 100
        part = dict(pos)
        part["margin"] = pos["margin"] * f
        part["qty"] = pos["qty"] * f
        part["funding_paid"] = pos.get("funding_paid", 0.0) * f
        text = await _auto_close(a, sym, part, r["price"], f"平{pct}%")
        rest = dict(pos)
        rest["margin"] = pos["margin"] * (1 - f)
        rest["qty"] = pos["qty"] * (1 - f)
        rest["funding_paid"] = pos.get("funding_paid", 0.0) * (1 - f)
        a["positions"][sym] = rest
        from storage import save_data
        save_data()
    await safe_edit(query, text, reply_markup=_kb([[_back()]]), parse_mode="Markdown")


# ── 需要打字的两处：限价价格、其他币代号、改止损 ──────────────
async def ask_price(query, context, kind, payload):
    """kind = perp / spot。存下上下文，等他发一个价格。"""
    guided.arm_chat(context, "await_vprice",
                    query.message.chat_id if query.message else 0,
                    value={"kind": kind, **payload})
    what = "开仓" if kind == "perp" else "买入"
    await safe_edit(query,
        f"📋 *挂限价{what}单*\n\n直接发一个**价格**给我，例如 `58000`\n"
        f"价格到了才成交，成交按 0.02% 挂单费率\n\n取消发 /menu",
        parse_mode="Markdown")


async def ask_coin(query, context, what):
    guided.arm_chat(context, "await_vcoin",
                    query.message.chat_id if query.message else 0, value=what)
    await safe_edit(query,
        "✍️ 发一个**币名**给我，例如 `ARB`\n\n取消发 /menu",
        parse_mode="Markdown")


async def ask_sl(query, context, sym):
    guided.arm_chat(context, "await_vsl",
                    query.message.chat_id if query.message else 0, value=sym)
    a = _acct(str(query.from_user.id))
    pos = a.get("positions", {}).get(sym)
    cur = f"当前止损 ${fmt(pos.get('sl'))}\n" if pos and pos.get("sl") else ""
    liq = f"爆仓价 ${fmt(_liq(pos))}\n" if pos else ""
    await safe_edit(query,
        f"🎯 *{sym} 设止盈止损*\n{cur}{liq}\n"
        f"发「`sl=58000`」设止损、「`tp=70000`」设止盈，两个可以一起发\n"
        f"填 `0` 清除\n\n取消发 /menu", parse_mode="Markdown")


async def ask_spot_sl(query, context, sym):
    """现货的止盈止损。和永续那屏分开是因为**判据不一样**：
    现货没有爆仓价可参考，方向要对着现价校验（见 vspot.apply_tpsl 的注释）。"""
    guided.arm_chat(context, "await_vssl",
                    query.message.chat_id if query.message else 0, value=sym)
    a = _acct(str(query.from_user.id))
    h = S.holding(a, sym)
    cur = ""
    if h and (h.get("tp") or h.get("sl")):
        cur = (f"当前 止盈 {fmt(h['tp']) if h.get('tp') else '—'}"
               f"　止损 {fmt(h['sl']) if h.get('sl') else '—'}\n")
    avg = f"成本均价 ${fmt(h['cost'] / h['qty'])}\n" if h and h.get("qty") else ""
    r = await get_price(sym)
    px = f"现价 ${fmt(r['price'])}\n" if r else ""
    await safe_edit(query,
        f"🎯 *{sym} 现货止盈止损*\n{avg}{px}{cur}\n"
        f"发「`sl=58000`」设止损、「`tp=70000`」设止盈，两个可以一起发\n"
        f"填 `0` 清除\n\n"
        f"触及会**全卖**，并撤掉这个币挂着的限价卖单\n\n取消发 /menu",
        parse_mode="Markdown")


async def on_spot_sl(message, context, sym, text):
    """收到现货的 sl=/tp= 设置。"""
    from storage import save_data
    guided.clear(context, "await_vssl")
    a = _acct(str(message.from_user.id))
    h = S.holding(a, sym)
    if not h:
        await message.reply_text(f"没有 {sym} 的现货持币了")
        return
    pairs = S.parse_tpsl(text.replace("，", " ").split())
    if not pairs:
        await message.reply_text("格式：`sl=58000` 或 `tp=70000`（填 0 清除）",
                                 parse_mode="Markdown")
        return
    r = await get_price(sym)
    if not r:
        await message.reply_text("取现价失败，稍后再试（现价是校验方向用的）")
        return
    changed, err = S.apply_tpsl(h, r["price"], pairs)
    if err:
        await message.reply_text(err, parse_mode="Markdown")
        return
    save_data()
    await message.reply_text(
        f"✅ {sym} 现货已设：" + "、".join(changed) +
        f"（现价 ${fmt(r['price'])}）\n后台每 60 秒检查，触及自动全卖")


async def on_price(message, context, pending, text):
    """收到限价单的价格。"""
    try:
        price = float(text.replace(",", "").replace("$", "").strip())
    except ValueError:
        await message.reply_text("价格要是数字，例如 58000（取消发 /menu）")
        return
    if price <= 0:
        await message.reply_text("价格要大于 0")
        return
    guided.clear(context, "await_vprice")
    a = _acct(str(message.from_user.id))
    a["chat_id"] = message.chat_id
    sym = pending["sym"]
    r = await get_price(sym)
    mark = r["price"] if r else None

    if pending["kind"] == "perp":
        side, lev, margin = pending["side"], pending["lev"], pending["margin"]
        if mark and VO.will_fill_now(side, price, mark):
            await message.reply_text(
                f"这个价现在就能成交（现价 ${fmt(mark)}），等于市价单。\n"
                f"要挂单等回落的话，做多请挂**低于**现价、做空挂**高于**现价。",
                parse_mode="Markdown")
            return
        cost = margin + margin * lev * FEE_RATE
        if cost > a["balance"] + 1e-9:
            await message.reply_text(f"💸 余额不足，这张单要冻结 ${cost:,.2f}")
            return
        o = VO.place(a, VO.PERP, sym, side, price, margin=margin, lev=lev, frozen=cost)
        d = "开多" if side == "long" else "开空"
        await message.reply_text(
            f"📋 *限价委托已挂*\n{sym} {d} {lev:g}x @${fmt(price)}\n"
            f"保证金 {margin}U　已冻结 ${cost:,.2f}\n"
            f"到价自动成交并通知你\n\n看挂单 /vorders　撤单 `/vcancel {o['id']}`",
            parse_mode="Markdown")
        return

    quote = pending["quote"]
    if mark and VO.will_fill_now("buy", price, mark):
        await message.reply_text(f"这个价现在就能买到（现价 ${fmt(mark)}）。"
                                 f"挂单请填**低于**现价的价格。", parse_mode="Markdown")
        return
    if quote > a["balance"] + 1e-9:
        await message.reply_text(f"💸 余额不足（可用 ${a['balance']:,.2f}）")
        return
    o = VO.place(a, VO.SPOT, sym, "buy", price, quote=quote, frozen=quote)
    await message.reply_text(
        f"📋 *限价买单已挂*\n{sym} @${fmt(price)}　${quote:,.0f}\n"
        f"已冻结，跌到就自动买入\n\n撤单 `/vcancel {o['id']}`",
        parse_mode="Markdown")


async def on_coin(message, context, what, text):
    from handlers.vtrade import _tradable
    sym = text.strip().upper()
    if " " in sym or len(sym) > 12:
        await message.reply_text("发单个币名，例如 ARB（取消发 /menu）")
        return
    if not await _tradable(sym):
        await message.reply_text(f"❌ 查不到 {sym}（代号写错？）")
        return
    guided.clear(context, "await_vcoin")
    if what == "perp":
        rows = [[InlineKeyboardButton("📈 做多", callback_data=f"vg:side:{sym}:long"),
                 InlineKeyboardButton("📉 做空", callback_data=f"vg:side:{sym}:short")]]
        await message.reply_text(f"➕ *{sym}*　选方向", reply_markup=_kb(rows),
                                 parse_mode="Markdown")
    else:
        rows = [[InlineKeyboardButton(f"{m}U", callback_data=f"vg:sbuy:{sym}:{m}")
                 for m in SPOT_AMOUNTS[:3]]]
        await message.reply_text(f"🛒 *买入 {sym}*　选金额", reply_markup=_kb(rows),
                                 parse_mode="Markdown")


async def on_sl(message, context, sym, text):
    """收到 sl=/tp= 设置。复用 vtrade 的存储格式。"""
    from storage import save_data
    guided.clear(context, "await_vsl")
    a = _acct(str(message.from_user.id))
    pos = a.get("positions", {}).get(sym)
    if not pos:
        await message.reply_text(f"没有 {sym} 的虚拟持仓了")
        return
    changed = []
    for kv in text.replace("，", " ").split():
        k, _, v = kv.partition("=")
        k = k.strip().lower()
        if k not in ("sl", "tp"):
            continue
        try:
            val = float(v)
        except ValueError:
            continue
        if val <= 0:
            pos.pop(k, None)
            changed.append(f"清除{'止损' if k == 'sl' else '止盈'}")
        else:
            pos[k] = val
            changed.append(f"{'止损' if k == 'sl' else '止盈'} ${fmt(val)}")
    if not changed:
        await message.reply_text("格式：`sl=58000` 或 `tp=70000`（填 0 清除）",
                                 parse_mode="Markdown")
        return
    save_data()
    await message.reply_text(f"✅ {sym} 已设：" + "、".join(changed) +
                             "\n后台每 60 秒检查，触及自动平仓")


async def desk(update, context):
    """/vtrade —— 直接打开交易台。命令只是入口，进去全是按钮。"""
    from handlers.util import safe_reply
    from handlers.vtrade import is_group
    if is_group(update):
        await safe_reply(update.message, "🔒 虚拟交易涉及你的账户，请私聊我使用")
        return
    # 首屏用发送而不是编辑：命令没有可编辑的原消息
    a = _acct(str(update.effective_user.id))
    a["chat_id"] = update.effective_chat.id
    msg = await update.message.reply_text("🎮 正在打开虚拟交易台…")

    class _Q:            # 复用 home() 的渲染逻辑，省一套并行实现
        from_user = update.effective_user
        message = msg

        async def answer(self, *a, **k):
            return None
    await home(_Q())
