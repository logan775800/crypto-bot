"""群内 @机器人 自由对话（复用现有 AI 中转站，多轮上下文）。

触发：
  • 群里 @机器人 或 回复机器人的任意消息 → 自由对话（自动阻止后续当币名查价）
  • 任意场景 /ask 你的问题
每个会话保留最近若干轮上下文，做到连续对话。纯对话，不查实时数据（会引导用命令查）。
"""
import logging
from telegram import Update
from telegram.ext import ContextTypes, ApplicationHandlerStop

from config import AI_API_KEY, AI_BASE_URL
from handlers.util import safe_reply, escape_md
from handlers.ai import ask_ai_messages

log = logging.getLogger(__name__)

MAX_TURNS = 10   # 保留最近 10 轮（20 条）上下文

SYSTEM = (
    "你是嵌在一个 Telegram 加密行情机器人里的助手，用简体中文、口语化、简洁地回答，别长篇大论。"
    "用户是做加密杠杆永续合约的交易者（主玩 Bybit/OKX）。"
    "你能做两类事：(1) 解释这个 bot 的功能和命令怎么用；(2) 聊行情/交易问题，"
    "给有风控框架、具体但非指令性的看法——聊具体买卖时用「风险温度」而非涨跌预测的口吻，"
    "并简短带一句「不构成投资建议」。"
    "你没有实时行情和用户持仓数据，凡涉及当前币价/资金费率/用户持仓这类实时信息，"
    "就引导用户用对应命令查（如 /price、/fprice、/vpos、/rpos），绝不编造具体数字。\n\n"
    "这个 bot 的主要命令：/menu 功能菜单，/price 查币价，/dashboard 市场看板，/top 涨跌榜，"
    "/analyze 技术分析，/ai AI解读，/news 新闻，/watchpct 持续波动监控，/alert 价格预警，"
    "/checklist 合约风控清单，/vopen 虚拟合约开仓、/vpos 虚拟持仓、/vhistory 胜率，"
    "/trade 实盘交易台(Bybit，点按钮开平仓)。"
)


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
        reply = await ask_ai_messages(hist, system=SYSTEM)
    except Exception as e:
        log.error(f"群聊AI出错: {e}")
        # 出错不留脏上下文
        if hist and hist[-1]["role"] == "user":
            hist.pop()
        await safe_reply(update.message, f"AI 出错了，稍后再试：{str(e)[:80]}")
        return
    hist.append({"role": "assistant", "content": reply})
    await safe_reply(update.message, escape_md(reply), parse_mode="Markdown")


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
