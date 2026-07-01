import logging
import httpx
import re
from telegram import Update
from telegram.ext import ContextTypes
from xml.etree import ElementTree
from config import AI_API_KEY, AI_BASE_URL, AI_MODEL

NEWS_SOURCES = [
    ("Cointelegraph", "https://cointelegraph.com/rss"),
]

def clean_html(text):
    text = re.sub(r'<[^>]+>', '', text or '')
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&#039;', "'").replace('&quot;', '"').replace('&nbsp;', ' ')
    return text.strip()

def clean_url(url):
    """去掉 utm 等垃圾参数"""
    return url.split('?')[0]

async def fetch_news(limit=8):
    items = []
    for source_name, url in NEWS_SOURCES:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                root = ElementTree.fromstring(resp.content)
                for item in root.findall(".//item")[:limit]:
                    title = clean_html(item.findtext("title", ""))
                    link = clean_url(item.findtext("link", ""))
                    desc = clean_html(item.findtext("description", ""))[:200]
                    items.append({"title": title, "link": link, "desc": desc})
        except Exception as e:
            logging.error(f"抓取新闻出错 {source_name}: {e}")
    return items

async def translate_news(items):
    """用AI把新闻翻译成中文+提炼摘要"""
    if not AI_API_KEY or not AI_BASE_URL:
        return None  # 没配AI，返回None走原文
    # 组装新闻列表给AI
    news_text = ""
    for i, it in enumerate(items, 1):
        news_text += f"{i}. {it['title']}\n"
    prompt = (
        "下面是加密货币英文新闻标题，请翻译成简洁的中文标题，每条一行，"
        "格式：序号. 中文标题。只输出翻译结果，不要额外说明。\n\n" + news_text
    )
    try:
        url = AI_BASE_URL.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
        body = {
            "model": AI_MODEL,
            "messages": [
                {"role": "system", "content": "你是专业的加密货币新闻翻译，把英文标题翻译成简洁准确的中文。"},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            reply = resp.json()["choices"][0]["message"]["content"]
        # 解析AI返回的中文标题（按行）
        cn_titles = {}
        for line in reply.strip().split("\n"):
            m = re.match(r'\s*(\d+)[.、]\s*(.+)', line)
            if m:
                cn_titles[int(m.group(1))] = m.group(2).strip()
        return cn_titles
    except Exception as e:
        logging.error(f"新闻翻译出错: {e}")
        return None

async def news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📰 获取最新加密货币新闻...")
    try:
        items = await fetch_news(8)
        if not items:
            await update.message.reply_text("暂时获取不到新闻")
            return

        # 尝试AI翻译
        cn_titles = await translate_news(items)

        lines = ["📰 *最新加密货币新闻*\n"]
        for i, it in enumerate(items, 1):
            title = cn_titles.get(i, it["title"]) if cn_titles else it["title"]
            # 中文标题 + 链接（用Markdown链接格式，标题可点）
            lines.append(f"{i}. [{title}]({it['link']})")
        lines.append("\n📎 来源: Cointelegraph | 点标题看原文")
        await update.message.reply_text(
            "\n".join(lines),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"新闻命令出错: {e}")
        await update.message.reply_text("获取失败，请稍后再试")


# ===== 新闻定时推送 =====
from storage import data as _ndata, save_data as _nsave

async def sub_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _ndata.setdefault("news_subs", [])
    if chat_id in _ndata["news_subs"]:
        await update.message.reply_text("已订阅新闻推送 ✅")
        return
    _ndata["news_subs"].append(chat_id)
    _nsave()
    await update.message.reply_text(
        "✅ 已订阅加密新闻推送！\n\n"
        "• 每小时推送最新新闻到这里\n"
        "• 中文标题，点开看原文\n\n"
        "取消用 /unsubnews"
    )

async def unsub_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _ndata.setdefault("news_subs", [])
    if chat_id in _ndata["news_subs"]:
        _ndata["news_subs"].remove(chat_id)
        _nsave()
        await update.message.reply_text("已取消新闻推送")
    else:
        await update.message.reply_text("你还没订阅")

# 定时推送（job调用）
async def push_news(context: ContextTypes.DEFAULT_TYPE):
    _ndata.setdefault("news_subs", [])
    if not _ndata["news_subs"]:
        return
    _ndata.setdefault("pushed_news", [])

    try:
        items = await fetch_news(8)
        if not items:
            return
        # 去重：只推没推过的（用链接判断）
        pushed = set(_ndata["pushed_news"])
        new_items = [it for it in items if it["link"] not in pushed]
        if not new_items:
            return  # 没有新新闻
        new_items = new_items[:5]  # 一次最多推5条

        # AI翻译
        cn_titles = await translate_news(new_items)

        lines = ["📰 *最新加密新闻*\n"]
        for i, it in enumerate(new_items, 1):
            title = cn_titles.get(i, it["title"]) if cn_titles else it["title"]
            lines.append(f"{i}. [{title}]({it['link']})")
        lines.append("\n📎 来源: Cointelegraph")
        text = "\n".join(lines)

        for chat_id in _ndata["news_subs"]:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text,
                    parse_mode="Markdown", disable_web_page_preview=True)
            except Exception as e:
                logging.error(f"新闻推送失败 {chat_id}: {e}")

        # 记录已推送（保留最近100条链接，避免无限增长）
        _ndata["pushed_news"].extend(it["link"] for it in new_items)
        _ndata["pushed_news"] = _ndata["pushed_news"][-100:]
        _nsave()
    except Exception as e:
        logging.error(f"新闻定时推送出错: {e}")


# ===== 新闻定时推送 =====
from storage import data as _ndata, save_data as _nsave

async def sub_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _ndata.setdefault("news_subs", [])
    if chat_id in _ndata["news_subs"]:
        await update.message.reply_text("已订阅新闻推送 ✅")
        return
    _ndata["news_subs"].append(chat_id)
    _nsave()
    await update.message.reply_text(
        "✅ 已订阅加密新闻推送！\n\n"
        "• 每小时推送最新新闻到这里\n"
        "• 中文标题，点开看原文\n\n"
        "取消用 /unsubnews"
    )

async def unsub_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _ndata.setdefault("news_subs", [])
    if chat_id in _ndata["news_subs"]:
        _ndata["news_subs"].remove(chat_id)
        _nsave()
        await update.message.reply_text("已取消新闻推送")
    else:
        await update.message.reply_text("你还没订阅")

# 定时推送（job调用）
async def push_news(context: ContextTypes.DEFAULT_TYPE):
    _ndata.setdefault("news_subs", [])
    if not _ndata["news_subs"]:
        return
    _ndata.setdefault("pushed_news", [])

    try:
        items = await fetch_news(8)
        if not items:
            return
        # 去重：只推没推过的（用链接判断）
        pushed = set(_ndata["pushed_news"])
        new_items = [it for it in items if it["link"] not in pushed]
        if not new_items:
            return  # 没有新新闻
        new_items = new_items[:5]  # 一次最多推5条

        # AI翻译
        cn_titles = await translate_news(new_items)

        lines = ["📰 *最新加密新闻*\n"]
        for i, it in enumerate(new_items, 1):
            title = cn_titles.get(i, it["title"]) if cn_titles else it["title"]
            lines.append(f"{i}. [{title}]({it['link']})")
        lines.append("\n📎 来源: Cointelegraph")
        text = "\n".join(lines)

        for chat_id in _ndata["news_subs"]:
            try:
                await context.bot.send_message(chat_id=chat_id, text=text,
                    parse_mode="Markdown", disable_web_page_preview=True)
            except Exception as e:
                logging.error(f"新闻推送失败 {chat_id}: {e}")

        # 记录已推送（保留最近100条链接，避免无限增长）
        _ndata["pushed_news"].extend(it["link"] for it in new_items)
        _ndata["pushed_news"] = _ndata["pushed_news"][-100:]
        _nsave()
    except Exception as e:
        logging.error(f"新闻定时推送出错: {e}")
