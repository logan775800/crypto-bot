"""
永续合约网格策略（Bybit V5, USDT 本位）。

模型：按「格子间隙(gap)」记账，n 个格子恒定 n 张挂单，每个格子任一时刻只有一张单：
  - 未持有该格 → 在下边界 L 挂 BUY；成交后 → 在上边界 U 挂 SELL
  - 已持有该格 → 在上边界 U 挂 SELL；成交后 → 在下边界 L 挂 BUY，锁定一格利润 (U-L)*qty
这样同一价位不会同时挂买卖，翻转只在本格内发生，杜绝重复挂单。

命令（默认仅管理员 ADMIN_CHAT_ID 可用，动真钱）：
  /gridstart SYMBOL 下限 上限 格数 每格量 [杠杆=1]
      例: /gridstart BTCUSDT 60000 70000 20 0.001 1
  /gridstatus            查看所有网格状态与估算利润
  /gridstop SYMBOL       撤掉该网格全部挂单并停止

后台 poll_grids 每 20s 轮询成交并补反向单 + 越界告警。
⚠️ BYBIT_TESTNET 未显式设为 false 时走模拟盘。先模拟盘验证再上实盘。
"""
import time
import logging
from telegram import Update
from telegram.ext import ContextTypes

from storage import data, save_data
from config import is_admin
from bybit_trade import BybitClient, BybitError, round_step, _is_testnet

log = logging.getLogger(__name__)


def _grids():
    return data.setdefault("grids", {})


def _is_admin(chat_id):
    # 未配置 ADMIN_CHAT_ID 时不限制（方便测试）；配置了则只允许管理员（支持多个id）
    return is_admin(chat_id)


def _link_id(gid_seq, gap, side):
    """生成唯一 orderLinkId（<=36字符）：网格短号+格号+方向+毫秒尾数。"""
    return f"g{gid_seq}-{gap}-{side[0]}-{int(time.time() * 1000) % 1_000_000}"


# ── 启动网格 ────────────────────────────────────────────────────────
async def grid_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ 仅管理员可操作实盘网格")
        return
    args = context.args
    if len(args) < 5:
        await update.message.reply_text(
            "用法：/gridstart SYMBOL 下限 上限 格数 每格量 [杠杆=1]\n"
            "例：/gridstart BTCUSDT 60000 70000 20 0.001 1")
        return
    try:
        symbol = args[0].upper()
        lower, upper = float(args[1]), float(args[2])
        n = int(args[3])
        qty_in = float(args[4])
        leverage = int(args[5]) if len(args) > 5 else 1
    except ValueError:
        await update.message.reply_text("参数格式错误，价格/数量要是数字，格数/杠杆要是整数")
        return

    if lower >= upper or n < 2 or qty_in <= 0 or leverage < 1:
        await update.message.reply_text("参数不合理：需 下限<上限、格数≥2、每格量>0、杠杆≥1")
        return

    key = f"{chat_id}:{symbol}"
    if _grids().get(key, {}).get("status") == "running":
        await update.message.reply_text(f"⚠️ {symbol} 已有运行中的网格，先 /gridstop {symbol}")
        return

    env = "模拟盘" if _is_testnet() else "⚠️实盘"
    await update.message.reply_text(f"⏳ 正在 {env} 铺设 {symbol} 网格 ...")

    try:
        client = BybitClient()
        info = await client.instrument_info(symbol)
        tick, qty_step, min_qty = info["tickSize"], info["qtyStep"], float(info["minOrderQty"])

        qty = round_step(qty_in, qty_step)
        if float(qty) < min_qty:
            await update.message.reply_text(
                f"每格量 {qty} 小于最小下单量 {min_qty}，请调大")
            return

        # 档位价（等差），按 tick 取整并校验严格递增
        raw_step = (upper - lower) / n
        levels = [round_step(lower + i * raw_step, tick) for i in range(n + 1)]
        if any(float(levels[i]) >= float(levels[i + 1]) for i in range(n)):
            await update.message.reply_text("格间距太小（小于最小价位 tick），请减少格数或拉大区间")
            return

        px = await client.last_price(symbol)

        # 网格利润率健康检查：格间距% 要明显大于双边手续费（挂单maker约0.02%*2）
        spacing_pct = raw_step / px * 100
        fee_note = ""
        if spacing_pct < 0.1:
            fee_note = f"\n⚠️ 格间距仅 {spacing_pct:.3f}%，接近手续费，利润很薄，建议拉大间距"

        await client.set_leverage(symbol, leverage)

        # 逐格铺单
        gid_seq = str(int(time.time()) % 100000)
        orders = {}
        placed, notional = 0, 0.0
        for g in range(n):
            L, U = levels[g], levels[g + 1]
            # 判定该格初始方向：整格在价下→挂买(未持有)；整格在价上→挂卖(持有/开空)；含价→挂买
            if float(U) <= px:
                holding = False
            elif float(L) >= px:
                holding = True
            else:
                holding = False
            side = "Sell" if holding else "Buy"
            price = U if holding else L
            lid = _link_id(gid_seq, g, side)
            try:
                res = await client.place_limit(symbol, side, qty, price, lid)
                orders[str(g)] = {
                    "holding": holding, "L": L, "U": U,
                    "side": side, "price": price,
                    "link_id": lid, "order_id": res.get("orderId", ""),
                }
                placed += 1
                notional += float(price) * float(qty)
            except BybitError as e:
                log.error(f"铺单失败 gap{g} {side}@{price}: {e}")

        _grids()[key] = {
            "chat_id": chat_id, "symbol": symbol, "gid_seq": gid_seq,
            "lower": lower, "upper": upper, "n": n,
            "levels": levels, "qty": qty, "leverage": leverage,
            "tick": tick, "qty_step": qty_step,
            "orders": orders, "status": "running",
            "realized": 0.0, "fills": 0, "breakout_notified": False,
            "started_ts": int(time.time()),
        }
        save_data()

        margin = notional / leverage
        await update.message.reply_text(
            f"✅ {symbol} 网格已启动（{env}）\n"
            f"区间 {lower}~{upper}，{n} 格，每格 {qty}，杠杆 {leverage}x\n"
            f"当前价 {px}，已挂 {placed}/{n} 单\n"
            f"名义敞口约 {notional:.2f} USDT，约需保证金 {margin:.2f} USDT{fee_note}")
    except Exception as e:
        log.exception("网格启动失败")
        await update.message.reply_text(f"❌ 启动失败：{e}")


# ── 停止网格 ────────────────────────────────────────────────────────
async def grid_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("⛔ 仅管理员可操作实盘网格")
        return
    if not context.args:
        await update.message.reply_text("用法：/gridstop SYMBOL")
        return
    symbol = context.args[0].upper()
    key = f"{chat_id}:{symbol}"
    grid = _grids().get(key)
    if not grid or grid.get("status") != "running":
        await update.message.reply_text(f"没有运行中的 {symbol} 网格")
        return
    try:
        client = BybitClient()
        await client.cancel_all(symbol)
        grid["status"] = "stopped"
        grid["stopped_ts"] = int(time.time())
        save_data()
        await update.message.reply_text(
            f"🛑 {symbol} 网格已停止，已撤销全部挂单。\n"
            f"累计成交 {grid['fills']} 次，估算网格利润 {grid['realized']:.4f} USDT（未计手续费/持仓浮盈亏）\n"
            f"⚠️ 若仍有持仓，请自行到交易所平仓。")
    except Exception as e:
        log.exception("网格停止失败")
        await update.message.reply_text(f"❌ 停止失败：{e}")


# ── 网格状态 ────────────────────────────────────────────────────────
async def grid_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    mine = {k: g for k, g in _grids().items() if g.get("chat_id") == chat_id}
    if not mine:
        await update.message.reply_text("你还没有网格。/gridstart 启动一个")
        return
    lines = []
    for g in mine.values():
        active = sum(1 for o in g["orders"].values() if o.get("link_id"))
        lines.append(
            f"{'🟢' if g['status']=='running' else '⚪'} {g['symbol']} "
            f"[{g['lower']}~{g['upper']}] {g['n']}格 每格{g['qty']} {g['leverage']}x\n"
            f"   挂单 {active} 成交 {g['fills']} 次 估利润 {g['realized']:.4f} USDT")
    env = "模拟盘" if _is_testnet() else "⚠️实盘"
    await update.message.reply_text(f"📊 网格（{env}）\n" + "\n".join(lines))


# ── 后台轮询：检测成交 → 反向补单 + 越界告警 ───────────────────────────
async def poll_grids(context: ContextTypes.DEFAULT_TYPE):
    running = [(k, g) for k, g in _grids().items() if g.get("status") == "running"]
    if not running:
        return
    try:
        client = BybitClient()
    except Exception as e:
        log.error(f"网格轮询无法创建客户端: {e}")
        return

    changed = False
    for key, grid in running:
        symbol, qty = grid["symbol"], grid["qty"]
        try:
            open_orders = await client.open_orders(symbol)
            open_links = {o.get("orderLinkId") for o in open_orders}

            for gap, o in grid["orders"].items():
                lid = o.get("link_id")
                # 1) 仍在挂着 → 跳过
                if lid and lid in open_links:
                    continue
                # 2) 曾挂但已不在挂单列表 → 查最终状态
                if lid and lid not in open_links:
                    st = await client.order_status(symbol, lid)
                    status = st.get("orderStatus", "")
                    if status == "Filled":
                        # 成交 → 翻转本格方向
                        if o["holding"]:  # 原是卖单成交 → 完成一格套利
                            grid["realized"] += (float(o["U"]) - float(o["L"])) * float(qty)
                            o["holding"] = False
                        else:             # 原是买单成交 → 变持有
                            o["holding"] = True
                        grid["fills"] += 1
                        changed = True
                        await _notify(context, grid, o, status)
                        o["link_id"] = ""  # 待下方重新挂反向单
                    elif status in ("Cancelled", "Rejected", "Deactivated"):
                        o["link_id"] = ""  # 被外部撤/拒 → 重挂同方向
                        changed = True
                    else:
                        continue  # New/PartiallyFilled 等瞬态，下轮再看
                # 3) 需要（重新）挂单
                if not o.get("link_id"):
                    side = "Sell" if o["holding"] else "Buy"
                    price = o["U"] if o["holding"] else o["L"]
                    new_lid = _link_id(grid["gid_seq"], gap, side)
                    try:
                        res = await client.place_limit(symbol, side, qty, price, new_lid)
                        o.update(side=side, price=price, link_id=new_lid,
                                 order_id=res.get("orderId", ""))
                        changed = True
                    except BybitError as e:
                        log.error(f"{symbol} gap{gap} 补单失败 {side}@{price}: {e}")

            # 越界告警（只提醒一次）
            px = await client.last_price(symbol)
            if (px < grid["lower"] or px > grid["upper"]) and not grid.get("breakout_notified"):
                grid["breakout_notified"] = True
                changed = True
                where = "跌破下限" if px < grid["lower"] else "涨破上限"
                await context.bot.send_message(
                    grid["chat_id"],
                    f"🚨 {symbol} 现价 {px} 已{where} [{grid['lower']}~{grid['upper']}]！\n"
                    f"网格单边可能已打满，建议检查持仓，考虑 /gridstop {symbol} 或移动区间。")
            elif grid["lower"] <= px <= grid["upper"] and grid.get("breakout_notified"):
                grid["breakout_notified"] = False  # 回到区间内，重置以便下次再报
                changed = True
        except Exception as e:
            log.error(f"网格轮询 {symbol} 出错: {e}")

    if changed:
        save_data()


async def _notify(context, grid, o, status):
    try:
        # holding 刚被翻转：现在 holding=True 表示刚买入；holding=False 表示刚卖出完成套利
        if o["holding"]:
            msg = f"🟩 {grid['symbol']} 买单成交 @{o['L']}，已在 {o['U']} 挂反向卖单"
        else:
            profit = (float(o["U"]) - float(o["L"])) * float(grid["qty"])
            msg = (f"🟥 {grid['symbol']} 卖单成交 @{o['U']}，锁定一格利润 ≈{profit:.4f} USDT，"
                   f"已在 {o['L']} 挂反向买单\n累计 {grid['fills']} 次 / 估利润 {grid['realized']:.4f} USDT")
        await context.bot.send_message(grid["chat_id"], msg)
    except Exception as e:
        log.error(f"网格通知失败: {e}")
