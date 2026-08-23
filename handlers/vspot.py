"""虚拟盘的**现货**部分 —— 买入、卖出、持币、限价委托、止盈止损。

和永续那半边最本质的区别，也是练手时最该建立的直觉：
  • **现货不会爆仓**。买了就是买了，跌 90% 还在手上；永续跌到爆仓价就没了。
    ——但"不会被强制带走"不等于"不用设止损"，所以这里一样有止盈止损。
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
from handlers.vtrade import (_acct, fmt, get_price, get_prices, is_group,
                             _tradable, _check_tpsl)
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


def settle(a, sym, side, price, rate, quote=None, qty=None, kind="现货卖出"):
    """现货成交结算。买：花 quote 得币；卖：出 qty 得 USDT。返回给用户看的文本。

    成本均价按**加权**算：分批买入后要知道自己的真实成本，
    否则"我这单是赚是亏"根本判断不了——而这是现货唯一要盯的数。

    kind 只影响写进历史的 exit_kind。复盘要分得清"我自己决定卖的"和
    "被止损带走的"——这两者混成一个标签，`/rstats` 就再也看不出
    止损到底救了他还是害了他。
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
        "value": gross, "fee": fee, "funding": 0.0, "exit_kind": kind,
    })
    save_data()
    emoji = "🟢" if pnl >= 0 else "🔴"
    return (f"{emoji} *现货卖出*\n{sym}　${fmt(price)}\n"
            f"卖出 {qty:.8g} 个，到手 ${net:,.2f}（手续费 ${fee:,.2f}）\n"
            f"这笔盈亏 {pnl:+,.2f}（成本 ${cost_out:,.2f}）\n"
            f"账户可用 ${a['balance']:,.2f}")


# ── 止盈止损 ────────────────────────────────────────────────
# 永续早就有（/vtpsl，后台 60 秒轮询触及自动平），现货一直没有。当时的取舍是
# "现货不会爆仓，止损的紧迫性低一档"——但那只说明**不会被强制带走**，
# 不代表不该有计划。现货拿着不动、跌 60% 还在安慰自己"又不会爆仓"，
# 恰恰是练手阶段最该被纠正的习惯。
#
# 触发后按**挂单价**成交，和 vtrade._check_tpsl / vorders 一个口径：
# 用当前价会让模拟盘的成交价系统性地优于实盘。

def apply_tpsl(h, mark, pairs):
    """把 {"tp"/"sl": 价格} 应用到持币上。返回 (改动说明, 错误文本或 None)。

    现货只有一个方向（拿着就是做多），所以约束很简单：
    止损在现价**之下**，止盈在**之上**。

    ⚠️ 这里刻意拿**现价**做判据，而不是成本均价——和永续那边
    （`vtrade.vtpsl` 用入场价校验方向）不一样，别当成不一致给"修"回去：
    现货拿了三个月涨了 50% 之后，把止损抬到成本价之上正是该做的动作，
    用成本价卡会把最该设的那个止损拒掉。真正会出事的只有"挂上去立刻就触发"，
    而那是个现价问题。
    """
    # 先全部校验、再全部落盘。一边改一边校验的话，
    # 「sl=45000 tp=40000」会变成止损设上了、止盈被拒、而回执只报错——
    # 他以为什么都没生效，其实账户已经变了。
    if mark:
        for k, px in pairs:
            if px is None or px <= 0:
                continue
            if k == "sl" and px >= mark:
                return [], (
                    f"❌ 止损 ${fmt(px)} 在现价 ${fmt(mark)} 之上——"
                    f"挂上去 60 秒内就会卖掉。现在就想卖用 `/vsell`，"
                    f"想护住利润请填**低于**现价的数。")
            if k == "tp" and px <= mark:
                return [], (
                    f"❌ 止盈 ${fmt(px)} 在现价 ${fmt(mark)} 之下——"
                    f"挂上去 60 秒内就会卖掉。止盈要填**高于**现价的数。")
    changed = []
    for k, px in pairs:
        if px is None or px <= 0:
            if h.pop(k, None) is not None:
                changed.append(f"清除{'止损' if k == 'sl' else '止盈'}")
            continue
        h[k] = px
        changed.append(f"{'止损' if k == 'sl' else '止盈'} ${fmt(px)}")
    return changed, None


def parse_tpsl(args):
    """把 ["tp=70000", "sl=60000"] 解析成 [("tp", 70000.0), ...]。看不懂的忽略。"""
    out = []
    for kv in args:
        k, _, v = str(kv).partition("=")
        k = k.strip().lower()
        if k not in ("tp", "sl"):
            continue
        try:
            out.append((k, float(v.replace(",", "").replace("$", "").strip())))
        except ValueError:
            continue
    return out


def _release_sell_orders(a, sym):
    """撤掉该币挂着的限价**卖**单，把被锁的币放出来。返回撤掉几张。

    为什么要撤：止损的意思就是"让我出来"，而挂在限价卖单里的币卖不掉。
    不撤的话止损只能卖掉可卖的那部分，剩下的继续躺在一张远得多的卖单里——
    这正是止损最不该有的行为。交易所的 OCO 也是这么干：一边触发，另一边撤掉。

    只撤**卖单**。同币的限价买单是另一笔决定（想在更低的价接回来），
    和"我要出来"不冲突，不替他做主。
    """
    doomed = [o for o in a.get("orders", [])
              if o.get("market") == VO.SPOT and o.get("side") == "sell"
              and o.get("sym") == sym.upper()]
    for o in doomed:
        VO.cancel(a, o["id"])
    return len(doomed)


async def check_tpsl(context, prices):
    """后台：现货持币触及止盈止损就全卖。

    由 vtrade.check_liquidations 那个 60 秒任务调用，**共用它取的那批价格**——
    挂单、爆仓、永续止盈损、现货止盈损四件事一次取价全办了，别多打一轮接口。
    """
    accts = data.get("vtrade", {})
    hit_any = False
    for uid, a in accts.items():
        chat_id = a.get("chat_id")
        for sym, h in list(_spot(a).items()):
            if not (h.get("tp") or h.get("sl")):
                continue
            info = (prices or {}).get(sym)
            if not info:
                continue
            # 现货持币在方向上就是一张多单，判定逻辑和永续多单一模一样，
            # 复用同一个函数——两份实现迟早会在某一版里对不上
            fired = _check_tpsl({"side": "long", "tp": h.get("tp"), "sl": h.get("sl")},
                                info["price"])
            if not fired:
                continue
            kind, px = fired
            freed = _release_sell_orders(a, sym)
            qty = h["qty"]
            if qty <= 0:
                continue
            try:
                text = settle(a, sym, "sell", px, TAKER, qty=qty,
                              kind=f"现货{kind}")
            except Exception as e:
                log.error(f"现货{kind}平仓失败 {uid} {sym}: {e}")
                continue
            hit_any = True
            if not chat_id:
                continue
            note = (f"\n（同时撤掉 {freed} 张 {sym} 的限价卖单，"
                    f"否则被锁的币出不来）" if freed else "")
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"🎯 *现货{kind}自动卖出*　@${fmt(px)}\n\n{text}{note}",
                    parse_mode="Markdown")
            except Exception as e:
                log.error(f"现货{kind}通知失败 {chat_id}: {e}")
    if hit_any:
        save_data()


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
        # 设了却看不见等于没设——这类"状态藏起来"是最容易让人以为功能没生效的
        if h.get("tp") or h.get("sl"):
            lines.append(f"　🎯 止盈 {fmt(h['tp']) if h.get('tp') else '—'}"
                         f"　止损 {fmt(h['sl']) if h.get('sl') else '—'}")
    lines += ["━━━━━━",
              f"持币现值 ${total:,.2f}　可用 ${a['balance']:,.2f}",
              "", "买入 `/vbuy BTC 1000`｜卖出 `/vsell BTC all`",
              "止盈损 `/vtpsl BTC tp=70000 sl=60000`（触及自动全卖）"]
    return "\n".join(x for x in lines if x)
