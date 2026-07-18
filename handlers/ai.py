import json
import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from config import COIN_IDS, AI_API_KEY, AI_BASE_URL, AI_MODEL
from api import get_daily_prices, get_price
from indicators import analyze as do_analyze, macd

async def ask_ai(prompt: str):
    """调用中转站AI"""
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    body = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": "你是加密货币行情分析助手。基于提供的技术指标数据，给出简洁客观的中文分析（200字内）。必须说明这不构成投资建议。不要编造数据。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

async def ask_ai_messages(messages, system=None, temperature=0.7):
    """多轮对话版：messages 是 [{role,content}...]，可选 system。用于群内@对话。"""
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    body = {"model": AI_MODEL, "messages": msgs, "temperature": temperature}
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]


async def _chat_completion(msgs, tools=None, temperature=0.7, timeout=70):
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    body = {"model": AI_MODEL, "messages": msgs, "temperature": temperature}
    if tools:
        body["tools"] = tools
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]


async def ask_ai_tools(messages, tools, tool_executor, system=None,
                       temperature=0.7, max_rounds=7):
    """带函数调用的多轮对话。tools=OpenAI function schema 列表；
    tool_executor(name, args)->str 执行工具并返回文本结果。
    循环：模型请求→若返回 tool_calls 则执行并回填→直到出最终文本或到 max_rounds。
    不修改传入的 messages。返回最终文本。"""
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)
    for _ in range(max_rounds):
        m = await _chat_completion(msgs, tools=tools, temperature=temperature)
        tcs = m.get("tool_calls")
        if not tcs:
            return m.get("content") or ""
        # 回填这一轮的 assistant(tool_calls) + 各工具结果
        msgs.append({"role": "assistant", "content": m.get("content"), "tool_calls": tcs})
        for tc in tcs:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except Exception:
                args = {}
            try:
                result = await tool_executor(name, args)
            except Exception as e:
                logging.error(f"工具 {name} 执行出错: {e}")
                result = f"（工具 {name} 出错：{str(e)[:80]}）"
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                         "content": str(result)[:4000]})
    # 到达轮次上限：最后不带 tools 强制出文本总结
    m = await _chat_completion(msgs, tools=None, temperature=temperature)
    return m.get("content") or "（没能得出结论，换个问法试试）"


async def ask_ai_struct(messages, tools, fn_name, system=None, temperature=0.3):
    """强制模型以**结构化参数**回答（用于交易计划这种必须能被程序读的输出）。

    优先走 OpenAI function-calling 的 tool_choice 强制；中转站若不支持 tool_choice，
    退回「让它自己调工具」，再退回从正文里抠 JSON。三层兜底是必要的——
    中转站的兼容性不完全可控，而这条链路挂了用户就拿不到计划。
    温度默认调低：计划要的是稳定可复现，不是创意。
    """
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)

    async def _call(body):
        async with httpx.AsyncClient(timeout=90) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]

    base = {"model": AI_MODEL, "messages": msgs, "tools": tools,
            "temperature": temperature}
    attempts = [
        dict(base, tool_choice={"type": "function", "function": {"name": fn_name}}),
        dict(base),                    # 中转站不认 tool_choice 就让它自己选
    ]
    last_err = None
    for body in attempts:
        try:
            m = await _call(body)
        except Exception as e:
            last_err = e
            logging.warning(f"结构化调用失败，换方式重试: {str(e)[:120]}")
            continue
        for tc in (m.get("tool_calls") or []):
            if tc.get("function", {}).get("name") == fn_name:
                try:
                    return json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError as e:
                    last_err = e
                    logging.warning(f"工具参数不是合法 JSON: {e}")
        # 没走工具：试着从正文里抠 JSON（有些中转站会把 JSON 写在 content 里）
        got = _json_from_text(m.get("content") or "")
        if got:
            return got
        last_err = last_err or RuntimeError("模型没有调用工具也没给出 JSON")
    raise RuntimeError(f"AI 未能给出结构化结果：{str(last_err)[:120]}")


def _json_from_text(text):
    """从正文里抠出第一个 JSON 对象（```json 围栏或裸对象）。抠不到返回 None。"""
    if not text:
        return None
    t = text.strip()
    if "```" in t:
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, _re.S)
        if m:
            t = m.group(1)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(t[i:j + 1])
    except json.JSONDecodeError:
        return None


async def ai_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not AI_API_KEY or not AI_BASE_URL:
        await update.message.reply_text("AI 功能未配置（缺少密钥或URL）")
        return
    if not context.args:
        await update.message.reply_text("用法：/ai BTC")
        return
    symbol = context.args[0].upper()
    if symbol not in COIN_IDS:
        await update.message.reply_text(f"不支持的币种：{symbol}")
        return

    await update.message.reply_text(f"🤖 AI 正在分析 {symbol}...")

    try:
        # 收集技术指标数据
        prices = await get_daily_prices(symbol, 35)
        cur = await get_price(symbol)
        if not prices or not cur:
            await update.message.reply_text("数据获取失败")
            return
        r = do_analyze(prices)
        macd_line, macd_sig = macd(prices)

        # 组织数据给AI
        data_text = (
            f"币种: {symbol}\n"
            f"当前价: ${cur['price']:,.2f}\n"
            f"24h涨跌: {cur['change']:+.2f}%\n"
            f"RSI(14): {r.get('rsi', 0):.1f}\n"
            f"MA7: ${r.get('ma7', 0):,.2f}\n"
            f"MA30: ${r.get('ma30', 0):,.2f}\n"
            f"MACD: {macd_sig}\n"
            f"近期价格: {[round(p) for p in prices[-7:]]}"
        )
        prompt = f"请分析以下加密货币技术指标数据：\n{data_text}\n\n给出简洁的趋势解读。"

        ai_reply = await ask_ai(prompt)

        await update.message.reply_text(
            f"🤖 {symbol} AI 分析\n\n{ai_reply}\n\n"
            f"━━━━━━━━\n⚠️ AI分析仅供参考，不构成投资建议"
        )
    except Exception as e:
        logging.error(f"AI分析出错: {e}")
        await update.message.reply_text(f"AI分析失败：{str(e)[:100]}")


async def build_ai_text(symbol):
    """返回AI分析文本（供按钮调用）"""
    if not AI_API_KEY or not AI_BASE_URL:
        return "AI未配置"
    prices = await get_daily_prices(symbol, 35)
    cur = await get_price(symbol)
    if not prices or not cur:
        return "数据获取失败"
    r = do_analyze(prices)
    ml, ms = macd(prices)
    data_text = (f"币种:{symbol} 价${cur['price']:,.2f} 24h{cur['change']:+.2f}% "
                 f"RSI:{r.get('rsi',0):.1f} MA7:${r.get('ma7',0):,.0f} MA30:${r.get('ma30',0):,.0f} MACD:{ms}")
    reply = await ask_ai(f"分析这些技术指标：{data_text}，给简洁趋势解读")
    # AI 输出是自由文本，可能含 _ * ` 等字符，转义后再嵌入 Markdown，避免整条消息渲染失败
    from handlers.util import escape_md
    return f"🤖 *{symbol} AI分析*\n\n{escape_md(reply)}\n\n⚠️ 不构成投资建议"
