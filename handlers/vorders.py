"""虚拟盘的**委托单引擎** —— 限价挂单、冻结资金、到价成交、撤单。

为什么要有这层：原来 `/vopen BTC long 1000 10 60000` 是**假装以 60000 成交了**，
可实盘里指定价格意味着「挂在那儿等」，可能等不到、可能只成交一部分。
练手时最该练的恰恰是这个——挂多远、等多久、要不要追。
假装成交会养出"我总能在想要的价位进场"这个最贵的错觉。

设计要点：
  • **挂单必须冻结资金**。不冻结就能无限挂单，账户余额变成假的。
    冻结在下单时扣、撤单/成交时结算——和交易所一样。
  • **限价成交走 maker 费率**（Bybit 0.02% vs 吃单 0.055%），
    这是挂单相对吃单的真实优势，不体现出来就少了一个该练的决策维度。
  • **下单时已经满足触发条件的，立刻成交**（实盘也是这样，会直接吃对手盘），
    但那就是吃单，费率按 taker 算。
  • 触发判定用**挂单价**成交，不用现价——见 vtrade._check_tpsl 里同样的理由。

永续和现货共用这一套：区别只在成交时怎么结算（开仓位 vs 加币）。
"""
import logging
import time

from storage import data, save_data

log = logging.getLogger(__name__)

MAKER_RATE = 0.0002      # 挂单成交（Bybit 现货/合约 maker 约 0.02%）
MAX_ORDERS = 20          # 单账户最多挂多少张，防手滑刷爆

PERP, SPOT = "perp", "spot"


def _orders(a):
    return a.setdefault("orders", [])


def _next_id(a):
    """短 id，方便他直接打 /vcancel 3 而不是抄一串哈希。"""
    used = {int(o["id"]) for o in _orders(a) if str(o.get("id", "")).isdigit()}
    n = 1
    while n in used:
        n += 1
    return str(n)


def will_fill_now(side, order_px, mark):
    """下单当下就该成交吗？

    买/做多：挂价 >= 现价 → 立刻能买到（等于吃单）
    卖/做空：挂价 <= 现价 → 同理
    """
    if side in ("long", "buy"):
        return order_px >= mark
    return order_px <= mark


def hits(order, mark):
    """行情走到了吗。买单等价格跌到挂价，卖单等价格涨上来。"""
    px = order["price"]
    if order["side"] in ("long", "buy"):
        return mark <= px
    return mark >= px


def place(a, market, symbol, side, price, *, margin=None, lev=None,
          quote=None, qty=None, tp=None, sl=None, frozen=0.0):
    """挂一张限价单。资金**在这里就冻结**，返回订单 dict。"""
    o = {
        "id": _next_id(a), "market": market, "sym": symbol, "side": side,
        "price": float(price), "ts": time.time(), "frozen": float(frozen),
        "margin": margin, "lev": lev, "quote": quote, "qty": qty,
        "tp": tp, "sl": sl,
    }
    a["balance"] -= frozen
    _orders(a).append(o)
    save_data()
    return o


def cancel(a, oid):
    """撤单，退回冻结的钱。返回被撤的单或 None。"""
    for i, o in enumerate(_orders(a)):
        if str(o["id"]) == str(oid):
            a["balance"] += o.get("frozen", 0.0)
            _orders(a).pop(i)
            save_data()
            return o
    return None


def cancel_all(a, symbol=None):
    gone = []
    for o in list(_orders(a)):
        if symbol and o["sym"] != symbol.upper():
            continue
        gone.append(cancel(a, o["id"]))
    return [g for g in gone if g]


def side_cn(o):
    return {"long": "开多", "short": "开空",
            "buy": "现货买入", "sell": "现货卖出"}.get(o["side"], o["side"])


def render(a, prices=None):
    from handlers.vtrade import fmt
    rows = _orders(a)
    if not rows:
        return ("📋 *当前没有挂单*\n\n"
                "挂单方式：\n"
                "`/vopen BTC long 1000 10 60000` 永续限价开仓\n"
                "`/vbuy BTC 1000 60000` 现货限价买入\n"
                "不带价格就是市价，立刻成交。")
    lines = ["📋 *虚拟委托单*"]
    for o in sorted(rows, key=lambda x: x["ts"]):
        mark = (prices or {}).get(o["sym"], {}).get("price")
        gap = ""
        if mark:
            d = (o["price"] - mark) / mark * 100
            gap = f"　距现价 {d:+.2f}%"
        detail = (f"{o['margin']:g}U×{o['lev']:g}倍" if o["market"] == PERP
                  else (f"买 {o['quote']:g}U" if o["side"] == "buy"
                        else f"卖 {o['qty']:g} 个"))
        lines.append(f"`{o['id']}` {o['sym']} {side_cn(o)}　@${fmt(o['price'])}{gap}")
        lines.append(f"　　{detail}　冻结 ${o.get('frozen', 0):,.2f}")
        if o.get("tp") or o.get("sl"):
            lines.append(f"　　成交后挂 止盈 {o.get('tp') or '-'}｜止损 {o.get('sl') or '-'}")
    lines += ["", "撤单 `/vcancel <编号>`　全撤 `/vcancel all`"]
    return "\n".join(lines)


async def check_orders(context):
    """后台：所有账户的挂单，到价就成交。由 vtrade 的定时任务调用。

    单独抽出来是因为它和爆仓/止盈止损查的是同一批价格——
    合在一个任务里跑，一次取价三件事都办了，别为了好看多打一轮接口。
    """
    from handlers import vtrade as V
    accts = data.get("vtrade", {})
    syms = set()
    for a in accts.values():
        syms.update(o["sym"] for o in a.get("orders", []))
    if not syms:
        return
    try:
        prices = await V.get_prices(list(syms))
    except Exception as e:
        log.error(f"挂单监控查价出错: {e}")
        return

    for uid, a in accts.items():
        chat_id = a.get("chat_id")
        for o in list(a.get("orders", [])):
            info = prices.get(o["sym"])
            if not info or not hits(o, info["price"]):
                continue
            try:
                msg = await fill(a, o, maker=True)
            except Exception as e:
                log.error(f"挂单成交失败 {o}: {e}")
                continue
            if chat_id and msg:
                try:
                    await context.bot.send_message(chat_id=chat_id, text=msg,
                                                   parse_mode="Markdown")
                except Exception as e:
                    log.error(f"挂单成交通知失败 {chat_id}: {e}")


async def fill(a, o, maker=True):
    """成交一张单。maker=True 走挂单费率。返回给用户看的文本。"""
    from handlers import vtrade as V
    from handlers import vspot as S
    px = o["price"]
    rate = MAKER_RATE if maker else V.FEE_RATE
    # 先把冻结的钱还回账户，再按正常流程扣——这样成交路径和市价单完全一致，
    # 不用在结算逻辑里到处判断"这笔是不是挂单来的"
    a["balance"] += o.get("frozen", 0.0)
    _orders(a).remove(o)

    if o["market"] == PERP:
        text = V.open_position(a, o["sym"], o["side"], o["margin"], o["lev"],
                               px, rate, tp=o.get("tp"), sl=o.get("sl"))
    else:
        text = S.settle(a, o["sym"], o["side"], px, rate,
                        quote=o.get("quote"), qty=o.get("qty"))
    save_data()
    return f"✅ *挂单成交*（{'挂单' if maker else '吃单'}费率）\n" + text


# ── 命令 ────────────────────────────────────────────────────
async def vorders_cmd(update, context):
    """/vorders —— 我的虚拟挂单。"""
    from handlers.util import safe_reply
    from handlers import vtrade as V
    if V.is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    a = V._acct(str(update.effective_user.id))
    syms = {o["sym"] for o in _orders(a)}
    prices = {}
    if syms:
        try:
            prices = await V.get_prices(list(syms))
        except Exception as e:
            log.warning(f"挂单面板取价失败: {e}")
    await safe_reply(update.message, render(a, prices), parse_mode="Markdown")


async def vcancel_cmd(update, context):
    """/vcancel <编号|all|币种> —— 撤单。"""
    from handlers.util import safe_reply
    from handlers import vtrade as V
    if V.is_group(update):
        await safe_reply(update.message, "🔒 请私聊使用")
        return
    a = V._acct(str(update.effective_user.id))
    args = context.args or []
    if not args:
        await safe_reply(update.message,
                         "撤单用法：`/vcancel 3`　`/vcancel all`　`/vcancel BTC`",
                         parse_mode="Markdown")
        return
    key = args[0]
    if key.lower() in ("all", "全部"):
        gone = cancel_all(a)
    elif key.isdigit():
        o = cancel(a, key)
        gone = [o] if o else []
    else:
        gone = cancel_all(a, key)
    if not gone:
        await safe_reply(update.message, "没找到这张单（`/vorders` 看编号）",
                         parse_mode="Markdown")
        return
    back = sum(o.get("frozen", 0.0) for o in gone)
    await safe_reply(update.message,
                     f"✅ 撤掉 {len(gone)} 张单"
                     + (f"，退回冻结 ${back:,.2f}" if back else "")
                     + f"\n账户可用 ${a['balance']:,.2f}")
