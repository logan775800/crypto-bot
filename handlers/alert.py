"""价格预警：一次性 / 持续 / 百分比。

取价走 handlers.source 统一层，带来两处行为变化（都是有意的）：

1. **不再卡 COIN_IDS 主流币白名单。** 以前 AKE、TUT 这类小币根本设不了价格预警
   （提示"仅支持市值较前的币"），而这些恰恰是最需要盯的。现在只要哪家交易所有
   这个币就能设。
2. **每条预警记住自己的数据源。** 以前全部走 CoinGecko，现在按「单条覆盖 >
   会话默认 > 自动」决定，后台按源分组取价——绝不拿 A 所的基准去比 B 所的现价。
   实测（2026-08-14）主流盘各所极差很小（BTC 0.02%、TUT 0.08%），但**现货↔永续
   的基差**和冷门小币的偏离要大得多，而且换源的那一刻是阶跃，够让 ±2% 这类
   小阈值凭空响一次。

老数据没有 src 字段，按会话默认处理，不用迁移。
"""
import logging
import time

from telegram import InlineKeyboardMarkup
from telegram.ext import ContextTypes
from telegram import Update

from handlers import source as src_mod
from storage import data, save_data

COOLDOWN = 300  # 持续预警冷却秒数（5分钟），避免刷屏


# ---------- 共用：建预警 / 描述数据源 ----------
def default_label(chat_id):
    """会话默认数据源的标签；「自动」返回空串（= 不锁源）。"""
    ex, market = src_mod.get_pref(chat_id)
    return src_mod.label_of(ex, market)


def add_alert(chat_id, entry):
    """写入一条预警并盖上数据源，返回它在本会话里的序号（从 0 起）。

    三个入口（命令 / 菜单一键 / 引导式）都走这里，否则加字段时总有一处漏掉。
    """
    entry.setdefault("src", default_label(chat_id))
    entry["chat_id"] = chat_id
    data["alerts"].append(entry)
    save_data()
    return sum(1 for a in data["alerts"] if a["chat_id"] == chat_id) - 1


def src_desc(a):
    """一条预警的数据源说明。"""
    label = a.get("src")
    return label if label else "自动"


def src_kb(chat_id, idx):
    """预警确认卡下面的「换数据源」按钮（只改这一条）。"""
    return InlineKeyboardMarkup([[src_mod.change_btn(f"al|{idx}")]])


async def repoint(chat_id, idx, label):
    """把第 idx 条预警改到指定数据源。百分比预警要顺带重设基准——
    换了源还沿用旧基准，等于拿 A 所的价去比 B 所的价。"""
    mine = [a for a in data["alerts"] if a["chat_id"] == chat_id]
    try:
        a = mine[int(idx)]
    except (ValueError, IndexError):
        return False, "没找到这条预警（可能已经触发或被删了）"

    a["src"] = label
    note = ""
    if a.get("type") == "pct":
        price, used = await src_mod.price_for(chat_id, a["symbol"], override=label)
        if price:
            a["base_price"] = price
            note = f"\n基准已按新源重设为 ${price:,.6g}（{used}）"
        else:
            note = "\n⚠️ 这个源查不到该币的价格，预警不会触发——建议换一个"
    save_data()
    return True, (f"✅ *{a['symbol']}* 这条预警的数据源已改为 *{label or '自动'}*{note}")


# ---------- 命令 ----------
async def alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text(
            "用法：\n"
            "/alert BTC 60000 above - 涨破(一次性)\n"
            "/alert BTC 50000 below - 跌破(一次性)\n"
            "/alertpct BTC 5 - 涨跌超5%(一次性)\n"
            "/watch BTC 60000 above - 持续监控(反复提醒)\n"
            "数据源用 /source 设，或设完在卡片上点「换数据源」"
        )
        return
    symbol = src_mod.norm(context.args[0])
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("目标价格要是数字")
        return
    direction = context.args[2].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("方向只能是 above 或 below")
        return
    chat_id = update.effective_chat.id
    price, used = await src_mod.price_for(chat_id, symbol)
    if price is None:
        await update.message.reply_text(
            f"查不到 {symbol} 的价格，所以这条预警设了也不会触发。\n"
            f"换个币名试试（用交易所里的基名，如 AKE、TUT），"
            f"或用 /source 换个数据源。")
        return

    idx = add_alert(chat_id, {
        "type": "fixed", "symbol": symbol, "target": target,
        "direction": direction, "set_by": update.effective_user.first_name,
    })
    arrow = "涨破" if direction == "above" else "跌破"
    await update.message.reply_text(
        f"✅ 预警已设置：{symbol} {arrow} ${target:,.6g}\n"
        f"当前 ${price:,.6g}　数据源 {used}",
        reply_markup=src_kb(chat_id, idx))


async def alert_pct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await update.message.reply_text("用法：/alertpct BTC 5")
        return
    symbol = src_mod.norm(context.args[0])
    try:
        pct = float(context.args[1])
    except ValueError:
        await update.message.reply_text("百分比要是数字")
        return
    chat_id = update.effective_chat.id
    try:
        base, used = await src_mod.price_for(chat_id, symbol)
    except Exception as e:
        logging.error(f"获取基准价出错: {e}")
        await update.message.reply_text(f"获取当前价格失败：{str(e)[:80]}")
        return
    if base is None:
        await update.message.reply_text(f"查不到 {symbol} 的价格，换个币名或用 /source 换数据源")
        return

    idx = add_alert(chat_id, {
        "type": "pct", "symbol": symbol, "pct": pct, "base_price": base,
        "set_by": update.effective_user.first_name,
    })
    await update.message.reply_text(
        f"✅ 百分比预警：{symbol} 从 ${base:,.6g} 涨跌超 ±{pct}% 提醒\n数据源 {used}",
        reply_markup=src_kb(chat_id, idx))


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("用法：/watch BTC 60000 above\n(持续监控，满足条件反复提醒，5分钟冷却)")
        return
    symbol = src_mod.norm(context.args[0])
    try:
        target = float(context.args[1])
    except ValueError:
        await update.message.reply_text("目标价格要是数字")
        return
    direction = context.args[2].lower()
    if direction not in ("above", "below"):
        await update.message.reply_text("方向只能是 above 或 below")
        return
    chat_id = update.effective_chat.id
    price, used = await src_mod.price_for(chat_id, symbol)
    if price is None:
        await update.message.reply_text(f"查不到 {symbol} 的价格，换个币名或用 /source 换数据源")
        return

    idx = add_alert(chat_id, {
        "type": "watch", "symbol": symbol, "target": target,
        "direction": direction, "set_by": update.effective_user.first_name,
        "last_notified": 0,
    })
    arrow = "涨破" if direction == "above" else "跌破"
    await update.message.reply_text(
        f"👁 持续监控：{symbol} {arrow} ${target:,.6g} 时反复提醒(5分钟冷却)\n数据源 {used}",
        reply_markup=src_kb(chat_id, idx))


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    my = [(i, a) for i, a in enumerate(data["alerts"]) if a["chat_id"] == chat_id]
    if not my:
        await update.message.reply_text("还没有预警")
        return
    lines = ["预警列表："]
    for idx, (i, a) in enumerate(my, 1):
        t = a.get("type")
        tail = f"　[{src_desc(a)}]"
        if t == "pct":
            lines.append(f"{idx}. [一次] {a['symbol']} 涨跌±{a['pct']}% (基准${a['base_price']:,.6g}){tail}")
        elif t == "watch":
            arrow = "涨破" if a["direction"] == "above" else "跌破"
            lines.append(f"{idx}. [持续] {a['symbol']} {arrow} ${a['target']:,.6g}{tail}")
        else:
            arrow = "涨破" if a["direction"] == "above" else "跌破"
            lines.append(f"{idx}. [一次] {a['symbol']} {arrow} ${a['target']:,.6g}{tail}")
    lines.append("\n用 /delalert 序号 删除　/source 改默认数据源")
    await update.message.reply_text("\n".join(lines))


async def del_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("用法：/delalert 1")
        return
    try:
        num = int(context.args[0])
    except ValueError:
        await update.message.reply_text("请输入序号数字")
        return
    chat_id = update.effective_chat.id
    my = [(i, a) for i, a in enumerate(data["alerts"]) if a["chat_id"] == chat_id]
    if num < 1 or num > len(my):
        await update.message.reply_text("序号不存在")
        return
    removed = data["alerts"].pop(my[num - 1][0])
    save_data()
    await update.message.reply_text(f"已删除：{removed['symbol']} 预警")


# ---------- 后台检查（三种类型） ----------
async def check_alerts(context: ContextTypes.DEFAULT_TYPE):
    if not data["alerts"]:
        return

    # 按数据源分组取价：同一条预警的基准和现价必须来自同一个源
    groups = {}
    for a in data["alerts"]:
        groups.setdefault(a.get("src") or "", set()).add(a["symbol"])
    prices = {}
    for label, syms in groups.items():
        try:
            got = await src_mod.prices_at(sorted(syms), label)
        except Exception as e:
            logging.error(f"预警查价出错（源 {label or '自动'}）: {e}")
            continue
        for sym, p in got.items():
            prices[(label, sym)] = p

    now = time.time()
    to_remove = []
    for a in data["alerts"]:
        cur = prices.get((a.get("src") or "", a["symbol"]))
        if not cur:
            continue
        t = a.get("type")

        if t == "pct":
            change = (cur - a["base_price"]) / a["base_price"] * 100
            if abs(change) >= a["pct"]:
                arrow = "涨" if change >= 0 else "跌"
                await _send(context, a,
                            f"🔔 百分比预警！\n{a['symbol']} 已{arrow} {abs(change):.2f}%\n"
                            f"基准 ${a['base_price']:,.6g} → 当前 ${cur:,.6g}"
                            f"（{src_desc(a)}）")
                to_remove.append(a)

        elif t == "watch":
            hit = (a["direction"] == "above" and cur >= a["target"]) or \
                  (a["direction"] == "below" and cur <= a["target"])
            if hit and (now - a.get("last_notified", 0)) >= COOLDOWN:
                arrow = "涨破" if a["direction"] == "above" else "跌破"
                await _send(context, a,
                            f"👁 持续监控！\n{a['symbol']} 当前 ${cur:,.6g}，"
                            f"已{arrow} ${a['target']:,.6g}（{src_desc(a)}）")
                a["last_notified"] = now  # 更新冷却，不删除

        else:  # fixed
            hit = (a["direction"] == "above" and cur >= a["target"]) or \
                  (a["direction"] == "below" and cur <= a["target"])
            if hit:
                arrow = "涨破" if a["direction"] == "above" else "跌破"
                await _send(context, a,
                            f"🔔 预警触发！\n{a['symbol']} 已{arrow} ${a['target']:,.6g}\n"
                            f"当前价格 ${cur:,.6g}（{src_desc(a)}）")
                to_remove.append(a)

    for a in to_remove:
        if a in data["alerts"]:
            data["alerts"].remove(a)
    save_data()  # watch更新了last_notified，也要存


async def _send(context, a, text):
    try:
        await context.bot.send_message(chat_id=a["chat_id"], text=text)
    except Exception as e:
        logging.error(f"推送失败: {e}")
