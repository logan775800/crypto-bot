"""群内 @机器人 自由对话（复用现有 AI 中转站，多轮上下文）。

触发：
  • 群里 @机器人 或 回复机器人的任意消息 → 自由对话（自动阻止后续当币名查价）
  • 任意场景 /ask 你的问题
每个会话保留最近若干轮上下文，做到连续对话。纯对话，不查实时数据（会引导用命令查）。
"""
import re
import logging
from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop

from config import AI_API_KEY, AI_BASE_URL
from handlers.util import safe_reply
from handlers.ai import ask_ai_messages, ask_ai_tools

log = logging.getLogger(__name__)

# AI 输出的是 GitHub 风 markdown(## 标题 / **粗体**)，转成 Telegram 旧版 Markdown（单星号粗体、无标题）
_HDR = re.compile(r'(?m)^[ \t]{0,3}#{1,6}[ \t]*(.+?)[ \t]*$')
_BOLD = re.compile(r'\*\*(.+?)\*\*')


def _md_to_tg(t):
    return _BOLD.sub(r'*\1*', _HDR.sub(r'*\1*', t))


def _strip_md(t):
    return _HDR.sub(r'\1', t).replace('**', '').replace('*', '')


async def _send(msg, text):
    """发 AI 回复：不引用原消息(直接发群里)，markdown 渲染失败则降级为去标记纯文本。"""
    try:
        await msg.reply_text(_md_to_tg(text), parse_mode="Markdown", do_quote=False)
    except Exception as e:
        log.warning(f"AI回复Markdown渲染失败，降级纯文本: {e}")
        try:
            await msg.reply_text(_strip_md(text), do_quote=False)
        except Exception as e2:
            log.error(f"AI回复发送失败: {e2}")

MAX_TURNS = 10   # 保留最近 10 轮（20 条）上下文

SYSTEM = (
    "你是嵌在一个 Telegram 加密行情机器人里的智能交易助手，用简体中文回答。"
    "用户是做加密杠杆永续合约的活跃交易者（主玩 Bybit/OKX）。\n\n"
    "你可以调用工具查实时数据——别猜、别让用户自己去查。需要当前币价/涨跌、"
    "合约价与资金费率、涨跌榜、市场情绪时，直接调对应工具拿到数据再作答：\n"
    "- get_price：某币现货价 + 24h涨跌\n"
    "- get_contract：某币永续合约价 + 资金费率 + 涨跌\n"
    "- get_top_movers：24h 涨幅/跌幅榜\n"
    "- get_fear_greed：恐惧贪婪指数\n\n"
    "⚠️ 关键：需要实时数据时必须主动调工具，绝不要停下来反问「你先给个币种」。"
    "如果问题需要具体币种但用户没点名是哪个币，就**默认按 BTC** 拉数据分析，"
    "开头说明「以 BTC 为例，换币再告诉我」即可，别只抛问题就结束。"
    "如果是全市场层面的问题（谁涨得猛、现在情绪如何），就调 get_top_movers / get_fear_greed。\n\n"
    "回答要有料、具体、带风控视角：该展开分析就展开（不用硬憋成一句话），但别啰嗦废话。"
    "聊具体买卖时用「风险温度 / 仓位管理」的口吻而非涨跌预测，可以给出你自己的倾向，"
    "末尾简短带一句「不构成投资建议」即可（不用每段都加）。"
    "也能解释这个 bot 的功能和命令怎么用：/menu 菜单，/price 查价，/dashboard 看板，"
    "/analyze 技术分析，/watchpct 波动监控，/alert 预警，/checklist 合约风控清单，"
    "/vopen 虚拟合约练手、/vpos 虚拟持仓，/trade 实盘交易台(Bybit,点按钮开平仓)。"
)

# ── 给 AI 的实时数据工具（只读，安全）───────────────────────────────
TOOLS = [
    {"type": "function", "function": {
        "name": "get_price", "description": "查某个币的现货价格和24小时涨跌幅",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "币种代号，如 BTC、ETH、SOL"}},
            "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_contract", "description": "查某个币的永续合约行情：合约价、资金费率、涨跌",
        "parameters": {"type": "object", "properties": {
            "symbol": {"type": "string", "description": "币种代号，如 BTC"}},
            "required": ["symbol"]}}},
    {"type": "function", "function": {
        "name": "get_top_movers", "description": "查全市场24小时涨幅榜和跌幅榜(前15)",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_fear_greed", "description": "查加密市场恐惧贪婪指数(0-100)",
        "parameters": {"type": "object", "properties": {}}}},
]


async def _tool_exec(name, args):
    """执行工具，返回给模型的文本结果。任何失败都返回说明字符串，不抛出。"""
    if name == "get_price":
        from api import get_price
        sym = str(args.get("symbol", "")).upper()
        r = await get_price(sym)
        if not r:
            return f"{sym}: 查不到该币现货价"
        return f"{sym} 现货 ${r['price']:,.6g}，24h {r['change']:+.2f}%"
    if name == "get_contract":
        sym = str(args.get("symbol", "")).upper()
        for src in ("okx", "binance", "bybit"):
            try:
                if src == "okx":
                    from handlers.okx import build_fprice_text
                    return await build_fprice_text(sym)
                if src == "binance":
                    from handlers.binance import build_fprice_text_bn
                    return await build_fprice_text_bn(sym)
                from handlers.bybit import build_fprice_text_by
                return await build_fprice_text_by(sym)
            except Exception:
                continue
        return f"{sym}: 三个所都查不到永续合约"
    if name == "get_top_movers":
        from api import get_top_movers
        gainers, losers = await get_top_movers(15)
        g = "、".join(f"{c['symbol']} {c['change']:+.1f}%" for c in gainers[:15])
        l = "、".join(f"{c['symbol']} {c['change']:+.1f}%" for c in losers[:15])
        return f"24h涨幅榜: {g}\n24h跌幅榜: {l}"
    if name == "get_fear_greed":
        from api import get_fear_greed
        fg = await get_fear_greed()
        return f"恐惧贪婪指数 {fg['value']}/100（{fg['classification']}）"
    return f"未知工具 {name}"


def _history(context):
    return context.chat_data.setdefault("chat_hist", [])


async def _reply(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str):
    if not AI_API_KEY or not AI_BASE_URL:
        await safe_reply(update.message, "AI 未配置（缺 AI_API_KEY / AI_BASE_URL）")
        return
    hist = _history(context)
    hist.append({"role": "user", "content": user_text})
    # 只保留最近 MAX_TURNS 轮，防止上下文无限增长
    if len(hist) > MAX_TURNS * 2:
        del hist[: len(hist) - MAX_TURNS * 2]
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    except Exception:
        pass
    try:
        # 优先带工具调用（能查实时数据）；工具链路异常则降级为纯对话
        try:
            reply = await ask_ai_tools(hist, TOOLS, _tool_exec, system=SYSTEM)
        except Exception as te:
            log.warning(f"工具对话失败，降级纯对话: {te}")
            reply = await ask_ai_messages(hist, system=SYSTEM)
    except Exception as e:
        log.error(f"群聊AI出错: {e}")
        # 出错不留脏上下文
        if hist and hist[-1]["role"] == "user":
            hist.pop()
        await safe_reply(update.message, f"AI 出错了，稍后再试：{str(e)[:80]}")
        return
    hist.append({"role": "assistant", "content": reply})
    await _send(update.message, reply)


async def handle_ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """菜单「💬 AI 助手」按钮点完后，用户发来的问题走这里（quickprice 拦截 await_ask 调用）。"""
    await _reply(update, context, text)


async def ask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/ask 你的问题 —— 任意场景可用（私聊也行）。"""
    q = " ".join(context.args).strip() if context.args else ""
    if not q:
        await safe_reply(update.message,
            "用法：`/ask 你的问题`\n例：`/ask BTC 现在追多风险大不大` 或 `/ask 怎么用虚拟合约练手`",
            parse_mode="Markdown")
        return
    await _reply(update, context, q)


async def reset_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resetchat 清空当前会话的对话记忆。"""
    context.chat_data.pop("chat_hist", None)
    await safe_reply(update.message, "🧹 已清空对话记忆，重新开始。")


async def mention_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群里 @机器人 或 回复机器人的消息时触发自由对话。私聊不自动触发（用 /ask）。"""
    msg = update.message
    if not msg or not msg.text:
        return
    if update.effective_chat.type not in ("group", "supergroup"):
        return  # 私聊纯文字留给查价；私聊用 /ask
    text = msg.text
    triggered = False
    # 1) 回复机器人的消息
    rt = msg.reply_to_message
    if rt and rt.from_user and rt.from_user.id == context.bot.id:
        triggered = True
    # 2) @机器人
    uname = context.bot.username
    if uname and ("@" + uname) in text:
        triggered = True
        text = text.replace("@" + uname, "").strip()
    if not triggered:
        return
    if not text:
        text = "你好"
    await _reply(update, context, text)
    # 已处理，阻止后续 quick_price 再把它当币名查价
    raise ApplicationHandlerStop
