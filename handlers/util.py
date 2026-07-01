"""共用工具：Markdown 转义 + 容错发送

旧版 Telegram Markdown 对 _ * ` [ 等字符很敏感，动态内容（AI 输出、新闻标题、
小币符号）里一旦含这些字符，整条消息会发送失败，用户看到的却是"获取失败"。
这里集中处理：静态菜单文本本来就安全，只需给动态内容转义 + 兜底降级。
"""
import logging
from telegram.error import BadRequest


def escape_md(text):
    """转义旧版 Markdown 特殊字符，用于把动态文本安全嵌进 Markdown 消息。"""
    if not text:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return text


def sanitize_link_text(text):
    """清理 [标题](链接) 里的标题：去掉会破坏链接结构的方括号，转义格式字符。"""
    text = (text or "").replace("[", "").replace("]", "")
    for ch in ("_", "*", "`"):
        text = text.replace(ch, "\\" + ch)
    return text


async def safe_reply(message, text, **kwargs):
    """reply_text 的容错版：Markdown 渲染失败时自动降级为纯文本，绝不因排版丢消息。"""
    try:
        return await message.reply_text(text, **kwargs)
    except BadRequest as e:
        logging.warning(f"Markdown 渲染失败，降级纯文本: {e}")
        kwargs.pop("parse_mode", None)
        try:
            return await message.reply_text(text, **kwargs)
        except Exception as e2:
            logging.error(f"降级后仍发送失败: {e2}")


async def safe_edit(query, text, **kwargs):
    """edit_message_text 的容错版：忽略'未改动'错误，Markdown 失败时降级纯文本。"""
    try:
        return await query.edit_message_text(text, **kwargs)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        logging.warning(f"Markdown 渲染失败，降级纯文本: {e}")
        kwargs.pop("parse_mode", None)
        try:
            return await query.edit_message_text(text, **kwargs)
        except Exception as e2:
            logging.error(f"降级后仍编辑失败: {e2}")
