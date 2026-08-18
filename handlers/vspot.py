"""虚拟盘的**现货**部分 —— 买入、卖出、持币、限价委托、止盈止损。

和永续那半边最本质的区别，也是练手时最该建立的直觉：
  • **现货不会爆仓**。买了就是买了，跌 90% 还在手上；永续跌到爆仓价就没了。
  • **现货没有资金费**，可以一直拿；永续拿着要扣钱。
  • 现货按「花多少 U 买」，永续按「多少保证金 × 多少倍」——
    同样一句"我看多 BTC"，两边要填的东西完全不同。

账户是共用的：现货和永续花的是同一笔 USDT 余额。这一点和 Bybit 的统一账户一致，
也让"我到底还剩多少子弹"只有一个数，不用来回换算。
"""
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply
from handlers.vtrade import _acct, fmt, get_price, get_prices, is_group, _tradable
from handlers import vorders as VO

log = logging.getLogger(__name__)

TAKER = 0.001            # 现货吃单费率（Bybit 现货 0.1%，比合约贵得多）
MIN_QUOTE = 10.0         # 最小买入金额，太小的单没有练习价值也算不准


def _spot(a):
    return a.setdefault("spot", {})


def holding(a, sym):
    return _spot(a).get(sym.upper())


def locked(a, sym):
    """挂着的限价卖单占用了多少币。

    这里刻意**不把币从持仓里扣走**：扣走就得同时把成本按比例搬到订单上，
    撤单再搬回来——两次浮点搬运，成本均价迟早对不上，而且没人会发现。
    改成"记账式锁定"：持仓不动，可卖数量 = 持有 - 已锁定。
    """
    return sum(o.get("qty") or 0 for o in a.get("orders", [])
               if o.get("market") == VO.SPOT and o.get("side") == "sell"
               and o.get("sym") == sym.upper())


def sellable(a, sym):
    h = holding(a, sym)
    return max(0.0, (h["qty"] if h else 0.0) - locked(a, sym))


def settle(a, sym, side, price, rate, quote=None, qty=None):
    """现货成交结算。买：花 quote 得币；卖：出 qty 得 USDT。返回给用户看的文本。

    成本均价按**加权**算：分批买入后要知道自己的真实成本，
    否则"我这单是赚是亏"根本判断不了——而这是现货唯一要盯的数。
    """
    sym = sym.upper()
    book = _spot(a)
    if side == "buy":
        fee = quote * rate
        got = (quote - fee) / price
        h = book.setdefault(sym, {"qty": 0.0, "cost": 0.0})
        h["qty"] += got
        h["cost"] += quote          # 成本含手续费：真实付出的就是这么多
        h["ts"] = time.time()
        a["balance"] -= quote
        save_data()
        avg = h["cost"] / h["qty"] if h["qty"] else price
        return (f"🛒 *现货买入*\n{sym}　${fmt(price)}\n"
                f"买到 {got:.8g} 个（花 ${quote:,.2f}，手续费 ${fee:,.2f}）\n"
                f"持有 {h['qty']:.8g}　成本均价 ${fmt(avg)}\n"
                f"账户可用 ${a['balance']:,.2f}\n\n"
                f"卖出 `/vsell {sym} {h['qty']:.8g}`｜查持币 `/vspot`")

    h = book.get(sym)
    if not h or qty > h["qty"] + 1e-12:
        return f"❌ {sym} 持币不足（现有 {h['qty']:.8g} 个）" if h else f"❌ 没有 {sym} 的现货"
    gross = qty * price
    fee = gross * rate
    net = gross - fee
    ratio = qty / h["qty"]
    cost_out = h["cost"] * ratio            # 按比例结转成本
    pnl = net - cost_out
    h["qty"] -= qty
    h["cost"] -= cost_out
    a["balance"] += net
    if h["qty"] <= 1e-12:
        book.pop(sym, None)
    a.setdefault("history", []).append({
        "sym": sym, "side": "spot", "lev": 1, "entry": cost_out / qty if qty else price,
        "exit": price, "margin": cost_out, "pnl": pnl,
        "roe": (pnl / cost_out * 100) if cost_out else 0.0,
        "ts": time.time(), "dur": time.time() - (h.get("ts") or time.time()),
        "value": gross, "fee": fee, "funding": 0.0, "exit_kind": "现货卖出",
    })
    save_data()
    emoji = "🟢" if pnl >= 0 else "🔴"
    return (f"{emoji} *现货卖出*\n{sym}　${fmt(price)}\n"
            f"卖出 {qty:.8g} 个，到手 ${net:,.2f}（手续费 ${fee:,.2f}）\n"
            f"这笔盈亏 {pnl:+,.2f}（成本 ${cost_out:,.2f}）\n"
            f"账户可用 ${a['balance']:,.2f}")


# ── 命令 ────────────────────────────────────────────────────
async def vbuy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vbuy BTC 1000 [限价] —— 现货买入。不带价 = 市价立刻成交。"""
    if is_group(update):
        await safe_reply(update.message, "🔒 虚拟交易涉及你的账户，请私聊我使用")
        return
    args = context.args or []
    if len(args) < 2:
        await safe_reply(update.message,
            "🛒 *现货买入*\n\n"
            "`/vbuy BTC 1000`　花 1000U 市价买入\n"
            "`/vbuy BTC 1000 60000`　挂 60000 的限价委托，等价格跌到才成交\n\n"
            "现货不会爆仓、不扣资金费，买了就一直拿着。\n"
            "卖出 `/vsell BTC 0.01`｜持币 `/vspot`｜挂单 `/vorders`",
            parse_mode="Markdown")
        return
    sym = args[0].upper()
    if not await _tradable(sym):
        await safe_reply(update.message, f"❌ 查不到 {sym} 的行情（代号写错？）")
        return
    try:
        quote = float(args[1])
    except ValueError:
        await safe_reply(update.message, "买入金额要是数字")
        return
    if quote < MIN_QUOTE:
        await safe_reply(update.message, f"最少买 ${MIN_QUOTE:g}")
        return
    uid = str(update.effective_user.id)
    a = _acct(uid)
    a["chat_id"] = update.effective_chat.id
    if quote > a["balance"] + 1e-9:
        await safe_reply(update.message,
                         f"💸 余额不足\n可用 ${a['balance']:,.2f}，本单需 ${quote:,.2f}")
        return

    limit = None
    if len(args) >= 3:
        try:
            limit = float(args[2])
        except ValueError:
            await safe_reply(update.message, "限价要是数字")
            return
    r = await get_price(sym)
    if not r:
        await safe_reply(update.message, "取现价失败，稍后再试")
        return
    mark = r["price"]

    if limit and not VO.will_fill_now("buy", limit, mark):
        if len(a.get("orders", [])) >= VO.MAX_ORDERS:
            await safe_reply(update.message, f"挂单太多（上限 {VO.MAX_ORDERS} 张），先撤几张")
            return
        o = VO.place(a, VO.SPOT, sym, "buy", limit, quote=quote, frozen=quote)
        await safe_reply(update.message,
            f"📋 *限价买单已挂*\n{sym}　@${fmt(limit)}（现价 ${fmt(mark)}）\n"
            f"金额 ${quote:,.2f}　已冻结\n"
            f"价格跌到就自动成交，走挂单费率 {VO.MAKER_RATE*100:g}%\n\n"
            f"撤单 `/vcancel {o['id']}`｜看挂单 `/vorders`", parse_mode="Markdown")
        return

    px = limit or mark
    await safe_reply(update.message, settle(a, sym, "buy", px, TAKER, quote=quote),
                     parse_mode="Markdown")


async def vsell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vsell BTC 0.01 [限价] —— 现货卖出。数量写 all 就是全卖。"""
    if is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    args = context.args or []
    if len(args) < 2:
        await safe_reply(update.message,
            "💱 *现货卖出*\n\n`/vsell BTC 0.01`　市价卖 0.01 个\n"
            "`/vsell BTC all`　全卖\n"
            "`/vsell BTC all 70000`　挂 70000 限价卖单\n\n"
            "持币 `/vspot`", parse_mode="Markdown")
        return
    sym = args[0].upper()
    uid = str(update.effective_user.id)
    a = _acct(uid)
    a["chat_id"] = update.effective_chat.id
    h = holding(a, sym)
    if not h:
        await safe_reply(update.message, f"没有 {sym} 的现货持仓")
        return
    if args[1].lower() in ("all", "全部"):
        qty = sellable(a, sym)
    else:
        try:
            qty = float(args[1])
        except ValueError:
            await safe_reply(update.message, "数量要是数字，或写 all")
            return
    free = sellable(a, sym)
    if qty <= 0 or qty > free + 1e-12:
        lk = locked(a, sym)
        await safe_reply(update.message,
                         f"数量超了：持有 {h['qty']:.8g} 个"
                         + (f"，其中 {lk:.8g} 个挂在限价卖单里（/vorders 看）" if lk else "")
                         + f"，可卖 {free:.8g} 个")
        return

    limit = None
    if len(args) >= 3:
        try:
            limit = float(args[2])
        except ValueError:
            await safe_reply(update.message, "限价要是数字")
            return
    r = await get_price(sym)
    if not r:
        await safe_reply(update.message, "取现价失败，稍后再试")
        return
    mark = r["price"]

    if limit and not VO.will_fill_now("sell", limit, mark):
        # 卖单锁的是币不是钱：持仓不动，靠 locked() 记账（见上面的注释）
        o = VO.place(a, VO.SPOT, sym, "sell", limit, qty=qty, frozen=0.0)
        await safe_reply(update.message,
            f"📋 *限价卖单已挂*\n{sym}　@${fmt(limit)}（现价 ${fmt(mark)}）\n"
            f"数量 {qty:.8g} 个　已锁定\n"
            f"价格涨到就自动成交\n\n撤单 `/vcancel {o['id']}`", parse_mode="Markdown")
        return

    px = limit or mark
    await safe_reply(update.message, settle(a, sym, "sell", px, TAKER, qty=qty),
                     parse_mode="Markdown")


async def vspot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/vspot —— 现货持币一览。"""
    if is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    a = _acct(str(update.effective_user.id))
    await safe_reply(update.message, await render(a), parse_mode="Markdown")


async def render(a):
    book = _spot(a)
    if not book:
        return ("🪙 *现货账户*　（空）\n\n"
                f"可用 ${a['balance']:,.2f}\n\n"
                "`/vbuy BTC 1000` 花 1000U 买入\n"
                "现货不会爆仓、不扣资金费——和永续最大的区别就在这。")
    try:
        prices = await get_prices(list(book.keys()))
    except Exception as e:
        log.error(f"现货查价失败: {e}")
        prices = {}
    lines = ["🪙 *现货账户*"]
    total = 0.0
    for sym, h in book.items():
        mark = (prices.get(sym) or {}).get("price")
        avg = h["cost"] / h["qty"] if h["qty"] else 0
        if mark:
            val = h["qty"] * mark
            pnl = val - h["cost"]
            total += val
            lines.append(f"*{sym}*　{h['qty']:.8g} 个　现值 ${val:,.2f}")
            lines.append(f"　成本均价 ${fmt(avg)}　现价 ${fmt(mark)}")
            lines.append(f"　浮动盈亏 {pnl:+,.2f}（{pnl/h['cost']*100:+.1f}%）"
                         if h["cost"] else "")
        else:
            lines.append(f"*{sym}*　{h['qty']:.8g} 个　成本均价 ${fmt(avg)}（取价失败）")
    lines += ["━━━━━━",
              f"持币现值 ${total:,.2f}　可用 ${a['balance']:,.2f}",
              "", "买入 `/vbuy BTC 1000`｜卖出 `/vsell BTC all`"]
    return "\n".join(x for x in lines if x)
