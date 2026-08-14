"""BTC 市场环境变化提醒 /btcregime

已有的告警都在盯**单个币的价格动作**（异动、急涨跌、指标、条件）。缺的是一层
更慢的东西：**大盘的状态**。山寨的多头计划在 BTC 多头排列下和空头排列下，
是两件完全不同的事——可没人会盯着 BTC 4h 图等它换状态。

设计上只有一个真问题：**防抖**。
均线一纠缠，状态就会在两个值之间来回跳，一天推十条，几次之后这个功能就被静音了。
所以这里做三层克制：

  1. 只看 4h（慢），不看 15m/1h —— 快周期本来就该由异动告警覆盖；
  2. 新状态要**连续 CONFIRM 次**读到才认，中途跳回去就清零；
  3. 认了之后进入 COOLDOWN 冷静期，期间不再播报任何变化。

宁可晚半天告诉你环境变了，也不要一天喊三次狼来了。
"""
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from handlers import marketdata as md
from handlers.util import safe_reply
from storage import data, save_data

log = logging.getLogger(__name__)

SYMBOL = "BTCUSDT"
INTERVAL = "4h"
CONFIRM = 2                 # 连续读到几次才认（每次间隔 CHECK_EVERY）
CHECK_EVERY = 1800          # 秒，任务轮询间隔
COOLDOWN = 6 * 3600         # 认定后的冷静期，期间不再播报

# 状态定义：只用 EMA 排列，不掺主观判断。三态足够指导「顺势/别逆势/别做趋势单」。
BULL, BEAR, CHOP = "bull", "bear", "chop"
LABEL = {BULL: "🟢 多头排列", BEAR: "🔴 空头排列", CHOP: "🟡 均线纠缠"}
MEANING = {
    BULL: "顺势做多风险相对低；山寨空单容易被抬轿。",
    BEAR: "顺势做空风险相对低；山寨多单容易被砸穿止损。",
    CHOP: "没有趋势，趋势单两头挨打；适合减小仓位或只做区间。",
}


def classify(e20, e50, e200):
    """EMA20/50/200 排列 → 三态。任何一个取不到就返回 None（不猜）。"""
    if not all(isinstance(x, (int, float)) for x in (e20, e50, e200)):
        return None
    if e20 > e50 > e200:
        return BULL
    if e20 < e50 < e200:
        return BEAR
    return CHOP


async def read_regime():
    """拉 BTC 4h K 线算当前状态。取不到数据返回 None —— 宁可这轮不判，也不猜。"""
    r = await md._get("/v5/market/kline", {
        "category": md.CAT, "symbol": SYMBOL,
        "interval": md.INTERVALS[INTERVAL], "limit": 250})
    rows = (r.get("list") or [])[::-1]        # Bybit 返回最新在前，反过来
    closes = [float(x[4]) for x in rows]
    if len(closes) < 200:
        return None                            # 样本不够算不了 EMA200
    return classify(md.ema(closes, 20), md.ema(closes, 50), md.ema(closes, 200))


def _state():
    st = data.get("btc_regime")
    if not isinstance(st, dict):
        st = {"state": "", "pending": "", "pending_n": 0, "changed_ts": 0}
        data["btc_regime"] = st
    return st


def step(now_state, st, now):
    """把一次观测喂进状态机，返回要播报的文案（不需要播报则返回 ""）。

    纯函数（除了改传进来的 st），好测——防抖逻辑正是最该被测的部分。
    """
    if not now_state:
        return ""
    prev = st.get("state") or ""
    if not prev:                       # 第一次跑：记下基线，不播报
        st["state"] = now_state
        st["pending"], st["pending_n"] = "", 0
        return ""
    if now_state == prev:              # 回到原状态：把在途的确认清零
        st["pending"], st["pending_n"] = "", 0
        return ""
    if st.get("pending") == now_state:
        st["pending_n"] = st.get("pending_n", 0) + 1
    else:
        st["pending"], st["pending_n"] = now_state, 1
    if st["pending_n"] < CONFIRM:
        return ""                      # 还没连续够次数，再看看
    # changed_ts 为 0 是「从没播报过」，不是「刚刚播报过」——不加这个判断，
    # 第一次环境变化会被冷静期直接吞掉。生产上 now 是真实 epoch，减 0 后是个巨大的
    # 数，碰巧躲过了；只有测试里用小时间戳才暴露出来。别把它简化掉。
    last = st.get("changed_ts") or 0
    if last and now - last < COOLDOWN:
        # 冷静期内：状态照记，但不播报，免得纠缠期一天喊三次
        st["state"] = now_state
        st["pending"], st["pending_n"] = "", 0
        return ""
    st["state"] = now_state
    st["pending"], st["pending_n"] = "", 0
    st["changed_ts"] = now
    return (f"🧭 *BTC 市场环境变了*\n"
            f"{LABEL.get(prev, prev)} → *{LABEL.get(now_state, now_state)}*\n"
            f"（4h EMA20/50/200 排列，连续 {CONFIRM} 次确认）\n\n"
            f"{MEANING.get(now_state, '')}\n\n"
            f"⚠️ 不是买卖信号，是提醒你回头看一眼手上的计划和仓位。")


async def check_regime(context: ContextTypes.DEFAULT_TYPE):
    """定时任务：读状态 → 过状态机 → 有变化就推给订阅者。"""
    subs = [s for s in (data.get("regime_subs") or [])]
    st = _state()
    try:
        now_state = await read_regime()
    except Exception as e:
        log.warning(f"BTC 环境读取失败: {e}")
        return
    text = step(now_state, st, time.time())
    save_data()
    if not text or not subs:
        return
    for cid in subs:
        try:
            await context.bot.send_message(chat_id=int(cid), text=text,
                                           parse_mode="Markdown")
        except Exception as e:
            log.warning(f"BTC 环境播报失败 {cid}: {e}")


async def btcregime(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/btcregime [on|off] —— 订阅 BTC 市场环境变化提醒；不带参数看当前状态。"""
    cid = update.effective_chat.id
    subs = data.setdefault("regime_subs", [])
    arg = (context.args[0].lower() if context.args else "")
    if arg in ("on", "开", "订阅"):
        if cid not in subs:
            subs.append(cid)
            save_data()
        await safe_reply(update.message,
                         f"✅ 已订阅 BTC 市场环境提醒。\n"
                         f"只在 4h 均线排列真的换了状态时才叫你"
                         f"（连续 {CONFIRM} 次确认 + {COOLDOWN // 3600} 小时冷静期），不会刷屏。\n"
                         f"取消：`/btcregime off`", parse_mode="Markdown")
        return
    if arg in ("off", "关", "取消"):
        if cid in subs:
            subs.remove(cid)
            save_data()
        await safe_reply(update.message, "已取消 BTC 市场环境提醒")
        return
    st = _state()
    cur = st.get("state")
    if not cur:
        try:
            cur = await read_regime()
        except Exception:
            cur = None
    body = (f"当前：*{LABEL.get(cur, '未知')}*\n{MEANING.get(cur, '')}"
            if cur else "当前状态还没读到（数据源没取到，稍后再试）")
    await safe_reply(update.message,
                     f"🧭 *BTC 市场环境*（4h EMA20/50/200）\n\n{body}\n\n"
                     f"{'已订阅' if cid in subs else '未订阅'}变化提醒　"
                     f"`/btcregime on` 开　`/btcregime off` 关",
                     parse_mode="Markdown")
