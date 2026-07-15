"""Bybit 实盘手动交易（USDT 永续，linear）—— 真金白银，与虚拟盘 /vopen 严格分开。

命令（默认仅管理员 ADMIN_CHAT_ID，默认走模拟盘 BYBIT_TESTNET=true）：
  /ropen SYMBOL long|short 保证金 杠杆 价格 [tp=x] [sl=y]   限价开仓(带可选止盈止损)，弹二次确认
  /rclose SYMBOL [百分比] [价格]                            平仓：默认市价全平(reduceOnly)，带价格则挂限价平
  /rpos [SYMBOL]                                          实盘持仓(入场/爆仓价/浮盈直接读交易所)
  /rbal                                                   合约账户 USDT 权益
  /rorders SYMBOL                                         当前未成交挂单
  /rcancel SYMBOL                                         撤掉该合约全部挂单

安全：开仓一律二次确认；平仓强制 reduceOnly 只减不反开；杠杆封顶；密钥缺失/权限不足直接报错不静默。
⚠️ 先在模拟盘(testnet)全流程验证，再把 .env 的 BYBIT_TESTNET 改 false 上实盘。
"""
import time
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_CHAT_ID
from handlers.util import safe_reply, safe_edit
from storage import data, save_data
from bybit_trade import BybitClient, BybitError, round_step, _is_testnet
from decimal import ROUND_DOWN

log = logging.getLogger(__name__)

MAX_LEVERAGE = 75          # 杠杆护栏，超过拒绝（防手滑）
LIQ_COOLDOWN = 1800        # 爆仓预警同币冷却 30 分钟


def _is_admin(chat_id):
    return not ADMIN_CHAT_ID or str(chat_id) == str(ADMIN_CHAT_ID)


def _norm(sym):
    """BTC / btc → BTCUSDT；已是 USDT 结尾则原样。"""
    s = sym.upper()
    return s if s.endswith("USDT") else s + "USDT"


def _fmt(p):
    try:
        p = float(p)
    except (TypeError, ValueError):
        return str(p)
    ap = abs(p)
    if ap >= 100:
        return f"{p:,.2f}"
    if ap >= 1:
        return f"{p:,.4f}"
    return f"{p:.8f}".rstrip("0").rstrip(".")


def _client():
    """建客户端；缺 key 时抛 RuntimeError，上层转成友好提示。"""
    return BybitClient()


def _env_tag():
    return "🧪模拟盘" if _is_testnet() else "🔴实盘"


async def _guard(update):
    """统一入口校验：仅管理员 + 私聊。通过返回 True。"""
    if update.effective_chat.type in ("group", "supergroup"):
        await safe_reply(update.message, "🔒 实盘交易请私聊使用")
        return False
    if not _is_admin(update.effective_chat.id):
        await safe_reply(update.message, "⛔ 仅管理员可操作实盘交易")
        return False
    return True


def _parse_kv(args):
    """从剩余参数里挑出 tp=、sl=；返回 (tp, sl)。"""
    tp = sl = None
    for a in args:
        low = a.lower()
        if low.startswith("tp="):
            tp = a[3:]
        elif low.startswith("sl="):
            sl = a[3:]
    return tp, sl


# ── 开仓（限价 + 可选 TP/SL，二次确认）───────────────────────────────
async def ropen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if len(args) < 5:
        await safe_reply(update.message,
            "📝 *实盘限价开仓*\n"
            "`/ropen SYMBOL long|short 保证金 杠杆 价格 [tp=x] [sl=y]`\n\n"
            "例：`/ropen BTC long 1000 10 62000 sl=60000 tp=68000`\n"
            "　= 62000 限价挂多，1000U 保证金 10x，止损60000 止盈68000\n\n"
            f"当前环境：{_env_tag()}（改 .env 的 BYBIT_TESTNET 切换）",
            parse_mode="Markdown")
        return

    symbol = _norm(args[0])
    side_raw = args[1].lower()
    if side_raw in ("long", "多", "buy", "l"):
        side, order_side = "long", "Buy"
    elif side_raw in ("short", "空", "sell", "s"):
        side, order_side = "short", "Sell"
    else:
        await safe_reply(update.message, "方向要填 long/多 或 short/空")
        return
    try:
        margin = float(args[2])
        lev = float(args[3])
        price = float(args[4])
    except ValueError:
        await safe_reply(update.message, "保证金/杠杆/价格要是数字")
        return
    if margin <= 0 or price <= 0:
        await safe_reply(update.message, "保证金和价格要大于 0")
        return
    if not (1 <= lev <= MAX_LEVERAGE):
        await safe_reply(update.message, f"杠杆范围 1~{MAX_LEVERAGE} 倍")
        return
    tp, sl = _parse_kv(args[5:])

    # 精度：取合约步长，价格按 tickSize、数量按 qtyStep 向下取整
    try:
        client = _client()
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT_API_KEY/SECRET，请在服务器 .env 里填好再用")
        return
    try:
        info = await client.instrument_info(symbol)
    except Exception as e:
        log.error(f"ropen 取合约信息出错: {e}")
        await safe_reply(update.message, f"❌ 取 {symbol} 合约信息失败：{e}")
        return

    price_s = round_step(price, info["tickSize"])
    notional = margin * lev
    raw_qty = notional / float(price_s)
    qty_s = round_step(raw_qty, info["qtyStep"], mode=ROUND_DOWN)
    if float(qty_s) < float(info["minOrderQty"]):
        await safe_reply(update.message,
            f"❌ 数量 {qty_s} 低于最小下单量 {info['minOrderQty']}。加大保证金或杠杆。")
        return
    tp_s = round_step(tp, info["tickSize"]) if tp else None
    sl_s = round_step(sl, info["tickSize"]) if sl else None

    # 暂存待确认订单，弹按钮
    context.user_data["ro_pending"] = {
        "symbol": symbol, "side": side, "order_side": order_side,
        "qty": qty_s, "price": price_s, "lev": lev,
        "margin": margin, "tp": tp_s, "sl": sl_s,
    }
    dir_txt = "做多 📈" if side == "long" else "做空 📉"
    extra = ""
    if tp_s:
        extra += f"\n止盈 ${_fmt(tp_s)}"
    if sl_s:
        extra += f"\n止损 ${_fmt(sl_s)}"
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 确认下单", callback_data="roconf"),
        InlineKeyboardButton("❌ 取消", callback_data="rocancel"),
    ]])
    await safe_reply(update.message,
        f"⚠️ *确认实盘下单* {_env_tag()}\n"
        f"{symbol} {dir_txt} {lev:g}x\n"
        f"限价 ${_fmt(price_s)}｜数量 {qty_s}\n"
        f"保证金约 ${margin:,.2f}｜名义 ${notional:,.2f}{extra}\n\n"
        f"确认后挂 GTC 限价单，到价成交。",
        reply_markup=kb, parse_mode="Markdown")


async def confirm_open(query, context):
    """确认按钮回调：真正提交限价开仓单。"""
    p = context.user_data.pop("ro_pending", None)
    if not p:
        await safe_edit(query, "没有待确认的订单（可能已过期），重新 /ropen")
        return
    try:
        client = _client()
    except RuntimeError:
        await safe_edit(query, "❌ 未配置 BYBIT API 密钥")
        return
    await safe_edit(query, f"⏳ 提交中… {p['symbol']} {p['side']} {p['lev']:g}x")
    try:
        await client.set_leverage(p["symbol"], int(p["lev"]) if float(p["lev"]).is_integer() else p["lev"])
    except BybitError as e:
        log.warning(f"设杠杆失败(继续下单): {e}")
    try:
        r = await client.place_limit(
            p["symbol"], p["order_side"], p["qty"], p["price"],
            tp=p["tp"], sl=p["sl"],
        )
    except BybitError as e:
        await safe_edit(query, f"❌ 下单被拒：[{e.ret_code}] {e.ret_msg}")
        return
    except Exception as e:
        log.error(f"confirm_open 下单出错: {e}")
        await safe_edit(query, f"❌ 下单失败：{e}")
        return
    oid = r.get("orderId", "?")
    dir_txt = "多" if p["side"] == "long" else "空"
    extra = ""
    if p["tp"]:
        extra += f"｜止盈 ${_fmt(p['tp'])}"
    if p["sl"]:
        extra += f"｜止损 ${_fmt(p['sl'])}"
    await safe_edit(query,
        f"✅ *已挂单* {_env_tag()}\n"
        f"{p['symbol']} {dir_txt} {p['lev']:g}x 限价 ${_fmt(p['price'])} 数量 {p['qty']}{extra}\n"
        f"订单号 `{oid}`\n"
        f"到价成交后 /rpos 看持仓；未成交可 /rorders 查、/rcancel {p['symbol']} 撤。",
        parse_mode="Markdown")


async def cancel_open(query, context):
    context.user_data.pop("ro_pending", None)
    await safe_edit(query, "已取消，未下单。")


# ── 平仓（默认市价 reduceOnly 全平；带价格则限价平）─────────────────────
async def rclose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await safe_reply(update.message,
            "用法：`/rclose BTC` 市价全平，`/rclose BTC 50` 平一半，`/rclose BTC 100 63000` 限价平",
            parse_mode="Markdown")
        return
    symbol = _norm(context.args[0])
    pct = 100.0
    limit_price = None
    if len(context.args) >= 2:
        try:
            pct = float(context.args[1])
        except ValueError:
            await safe_reply(update.message, "平仓比例要是数字（1~100）")
            return
        if not (0 < pct <= 100):
            await safe_reply(update.message, "平仓比例要在 1~100 之间")
            return
    if len(context.args) >= 3:
        try:
            limit_price = float(context.args[2])
        except ValueError:
            await safe_reply(update.message, "限价要是数字")
            return

    try:
        client = _client()
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    try:
        pos = await client.position(symbol)
    except Exception as e:
        log.error(f"rclose 查仓出错: {e}")
        await safe_reply(update.message, f"❌ 查持仓失败：{e}")
        return
    size = float(pos.get("size", 0) or 0)
    if size <= 0:
        await safe_reply(update.message, f"{symbol} 当前无持仓")
        return
    pos_side = pos.get("side")  # 'Buy'=多 / 'Sell'=空
    close_side = "Sell" if pos_side == "Buy" else "Buy"

    try:
        info = await client.instrument_info(symbol)
    except Exception as e:
        await safe_reply(update.message, f"❌ 取合约精度失败：{e}")
        return
    if pct >= 100:
        qty_s = round_step(size, info["qtyStep"], mode=ROUND_DOWN)
    else:
        qty_s = round_step(size * pct / 100.0, info["qtyStep"], mode=ROUND_DOWN)
    if float(qty_s) <= 0:
        await safe_reply(update.message, "平仓数量取整后为 0，比例太小")
        return

    try:
        if limit_price is not None:
            price_s = round_step(limit_price, info["tickSize"])
            await client.place_limit(symbol, close_side, qty_s, price_s, reduce_only=True)
            how = f"限价 ${_fmt(price_s)} 挂平仓单"
        else:
            await client.place_market(symbol, close_side, qty_s, reduce_only=True)
            how = "市价平仓"
    except BybitError as e:
        await safe_reply(update.message, f"❌ 平仓被拒：[{e.ret_code}] {e.ret_msg}")
        return
    except Exception as e:
        log.error(f"rclose 下单出错: {e}")
        await safe_reply(update.message, f"❌ 平仓失败：{e}")
        return
    await safe_reply(update.message,
        f"✅ {_env_tag()} {symbol} {how} {qty_s}（{'全平' if pct>=100 else f'{pct:g}%'}，reduceOnly）\n"
        f"结果看 /rpos。",
        parse_mode="Markdown")


# ── 查持仓 ──────────────────────────────────────────────────────────
def _pos_line(p):
    side = "多 📈" if p.get("side") == "Buy" else "空 📉"
    sym = p.get("symbol", "?")
    size = p.get("size", "?")
    entry = p.get("avgPrice", "?")
    mark = p.get("markPrice", "?")
    liq = p.get("liqPrice") or "—"
    upnl = float(p.get("unrealisedPnl", 0) or 0)
    lev = p.get("leverage", "?")
    emoji = "🟢" if upnl >= 0 else "🔴"
    return (
        f"{emoji} *{sym}* {side} {lev}x\n"
        f"   数量 {size}｜入场 ${_fmt(entry)} → 标记 ${_fmt(mark)}\n"
        f"   浮盈 {upnl:+,.2f} USDT｜爆仓价 ${_fmt(liq) if liq!='—' else '—'}"
    )


async def rpos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        client = _client()
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    try:
        if context.args:
            symbol = _norm(context.args[0])
            p = await client.position(symbol)
            positions = [p] if float(p.get("size", 0) or 0) > 0 else []
        else:
            positions = await client.positions_all()
    except BybitError as e:
        await safe_reply(update.message, f"❌ 查持仓被拒：[{e.ret_code}] {e.ret_msg}")
        return
    except Exception as e:
        log.error(f"rpos 出错: {e}")
        await safe_reply(update.message, f"❌ 查持仓失败：{e}")
        return
    if not positions:
        await safe_reply(update.message, f"{_env_tag()} 当前无持仓。开仓 `/ropen BTC long 1000 10 62000`", parse_mode="Markdown")
        return
    lines = [f"💼 *实盘持仓* {_env_tag()}\n"]
    total = 0.0
    for p in positions:
        total += float(p.get("unrealisedPnl", 0) or 0)
        lines.append(_pos_line(p))
    e = "🟢" if total >= 0 else "🔴"
    lines.append("─────────")
    lines.append(f"{e} 合计浮盈 {total:+,.2f} USDT")
    await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")


# ── 账户余额 ────────────────────────────────────────────────────────
async def rbal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    try:
        client = _client()
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    try:
        bal = await client.wallet_balance("USDT")
    except Exception as e:
        log.error(f"rbal 出错: {e}")
        await safe_reply(update.message, f"❌ 查余额失败：{e}")
        return
    eq = bal.get("totalEquity", "?")
    avail = bal.get("totalAvailableBalance", "?")
    upnl = bal.get("totalPerpUPL", "?")
    await safe_reply(update.message,
        f"💰 *合约账户* {_env_tag()}\n"
        f"总权益 {eq} USDT\n"
        f"可用 {avail} USDT\n"
        f"未实现盈亏 {upnl} USDT",
        parse_mode="Markdown")


# ── 挂单查询 / 撤单 ─────────────────────────────────────────────────
async def rorders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await safe_reply(update.message, "用法：`/rorders BTC`", parse_mode="Markdown")
        return
    symbol = _norm(context.args[0])
    try:
        client = _client()
        orders = await client.open_orders(symbol)
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    except Exception as e:
        await safe_reply(update.message, f"❌ 查挂单失败：{e}")
        return
    if not orders:
        await safe_reply(update.message, f"{symbol} 无未成交挂单")
        return
    lines = [f"📋 *{symbol} 挂单* {_env_tag()}\n"]
    for o in orders:
        ro = "减仓" if o.get("reduceOnly") else "开仓"
        lines.append(f"{o.get('side')} {o.get('qty')} @ ${_fmt(o.get('price'))} [{ro}]  `{o.get('orderId','')[:8]}`")
    lines.append(f"\n撤全部：`/rcancel {context.args[0]}`")
    await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")


async def rcancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    if not context.args:
        await safe_reply(update.message, "用法：`/rcancel BTC`", parse_mode="Markdown")
        return
    symbol = _norm(context.args[0])
    try:
        client = _client()
        await client.cancel_all(symbol)
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    except BybitError as e:
        await safe_reply(update.message, f"❌ 撤单被拒：[{e.ret_code}] {e.ret_msg}")
        return
    except Exception as e:
        await safe_reply(update.message, f"❌ 撤单失败：{e}")
        return
    await safe_reply(update.message, f"✅ 已撤 {symbol} 全部挂单")


# ── 改已有仓位的止盈止损 ────────────────────────────────────────────
async def rtpsl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    args = context.args
    if not args:
        await safe_reply(update.message,
            "用法：`/rtpsl BTC tp=68000 sl=60000`\n"
            "只改一个也行：`/rtpsl BTC sl=61000`；清除填 0：`/rtpsl BTC tp=0`",
            parse_mode="Markdown")
        return
    symbol = _norm(args[0])
    tp, sl = _parse_kv(args[1:])
    if tp is None and sl is None:
        await safe_reply(update.message, "至少要给一个 tp= 或 sl=（清除填 0）")
        return
    try:
        client = _client()
    except RuntimeError:
        await safe_reply(update.message, "❌ 未配置 BYBIT API 密钥")
        return
    # 有持仓才好设；顺便取精度
    try:
        pos = await client.position(symbol)
        if float(pos.get("size", 0) or 0) <= 0:
            await safe_reply(update.message, f"{symbol} 当前无持仓，先开仓再设止盈止损")
            return
        info = await client.instrument_info(symbol)
    except Exception as e:
        log.error(f"rtpsl 预检出错: {e}")
        await safe_reply(update.message, f"❌ 读取持仓/精度失败：{e}")
        return

    def _tick(v):
        if v is None:
            return None
        return "0" if str(v) in ("0", "0.0") else round_step(v, info["tickSize"])

    tp_s, sl_s = _tick(tp), _tick(sl)
    try:
        await client.set_trading_stop(symbol, tp=tp_s, sl=sl_s)
    except BybitError as e:
        await safe_reply(update.message, f"❌ 设置被拒：[{e.ret_code}] {e.ret_msg}")
        return
    except Exception as e:
        log.error(f"rtpsl 设置出错: {e}")
        await safe_reply(update.message, f"❌ 设置失败：{e}")
        return
    parts = []
    if tp_s is not None:
        parts.append("清除止盈" if tp_s == "0" else f"止盈 ${_fmt(tp_s)}")
    if sl_s is not None:
        parts.append("清除止损" if sl_s == "0" else f"止损 ${_fmt(sl_s)}")
    await safe_reply(update.message,
        f"✅ {_env_tag()} {symbol} 已更新：{'，'.join(parts)}", parse_mode="Markdown")


# ── 爆仓临近预警：开关 + 后台推送 ────────────────────────────────────
async def rliqalert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await _guard(update):
        return
    data.setdefault("rtrade_alert", {})
    arg = context.args[0].lower() if context.args else ""
    if arg in ("off", "关", "0"):
        data["rtrade_alert"]["enabled"] = False
        save_data()
        await safe_reply(update.message, "🔕 已关闭实盘爆仓预警")
        return
    # 默认阈值 8%，可自定义 /rliqalert 5
    threshold = 8.0
    if arg:
        try:
            threshold = float(arg)
        except ValueError:
            await safe_reply(update.message, "用法：`/rliqalert 5`（距爆仓≤5%提醒）或 `/rliqalert off`", parse_mode="Markdown")
            return
        if not (0.5 <= threshold <= 50):
            await safe_reply(update.message, "阈值范围 0.5~50%")
            return
    data["rtrade_alert"].update({
        "enabled": True, "threshold": threshold,
        "chat_id": update.effective_chat.id,
    })
    data["rtrade_alert"].setdefault("cooldown", {})
    save_data()
    await safe_reply(update.message,
        f"🔔 已开启实盘爆仓预警：任一持仓现价距爆仓价 ≤ {threshold:g}% 就通知你\n"
        f"（每币 30 分钟冷却）。关闭发 /rliqalert off", parse_mode="Markdown")


async def check_liq_alerts(context: ContextTypes.DEFAULT_TYPE):
    """后台 job：实盘持仓逼近爆仓价时推送。未开启/无密钥/无仓位则静默跳过。"""
    cfg = data.get("rtrade_alert", {})
    if not cfg.get("enabled") or not cfg.get("chat_id"):
        return
    try:
        client = _client()
    except RuntimeError:
        return  # 没配密钥，静默
    try:
        positions = await client.positions_all()
    except Exception as e:
        log.warning(f"爆仓预警查仓失败: {e}")
        return
    if not positions:
        return
    threshold = cfg.get("threshold", 8.0)
    cooldown = cfg.setdefault("cooldown", {})
    chat_id = cfg["chat_id"]
    now = time.time()
    changed = False
    for p in positions:
        try:
            mark = float(p.get("markPrice") or 0)
            liq = float(p.get("liqPrice") or 0)
        except (TypeError, ValueError):
            continue
        if mark <= 0 or liq <= 0:
            continue  # 全仓大保证金时 liqPrice 可能为空
        dist = abs(mark - liq) / mark * 100
        if dist > threshold:
            continue
        sym = p.get("symbol", "?")
        if now - cooldown.get(sym, 0) < LIQ_COOLDOWN:
            continue
        cooldown[sym] = now
        changed = True
        side = "多" if p.get("side") == "Buy" else "空"
        upnl = float(p.get("unrealisedPnl", 0) or 0)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(f"⚠️ *爆仓预警* {_env_tag()}\n"
                      f"{sym} {side} {p.get('leverage','?')}x 距爆仓仅 {dist:.1f}%\n"
                      f"现价 ${_fmt(mark)}｜爆仓价 ${_fmt(liq)}｜浮盈 {upnl:+,.2f}\n"
                      f"考虑加保证金 / 减仓 / 挪止损。`/rclose {sym.replace('USDT','')}` 可平"),
                parse_mode="Markdown")
        except Exception as e:
            log.error(f"爆仓预警推送失败: {e}")
    if changed:
        save_data()
