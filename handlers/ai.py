import json
import logging
import httpx
from telegram import Update
from telegram.ext import ContextTypes
from config import (COIN_IDS, AI_API_KEY, AI_BASE_URL, AI_MODEL,
                    AI_FALLBACK_MODELS, is_admin)
from api import get_daily_prices, get_price
from indicators import analyze as do_analyze, macd

# ── 模型自动降级 ────────────────────────────────────────────────────
# 中转站的渠道会不定期下线：模型还挂在 /v1/models 列表里，但你的 token 分组下
# 已经没有账号支持它，请求直接 404 model_not_found，整个 AI 就哑了
# （2026-07-23 就是这么挂的：gpt-5.6-terra-openai-compact 掉了渠道）。
# 这里在**运行时**沿候选链往下试，撞到「模型级」错误就换下一个。
_ACTIVE_MODEL = None      # 进程内粘住当前可用模型，避免每次都先去撞一次死模型

# 这些状态码属于「这个模型现在用不了」，换一个还有救；
# 401/400 之类是请求本身有问题，换模型也没用，直接抛。
_SWITCH_STATUS = {404, 429, 500, 502, 503}
_UNSUPPORTED_HINTS = ("image", "vision", "multimodal", "not support",
                      "unsupported", "不支持")


def _model_override():
    """/aimodel 设置的手动指定模型（落盘，重启仍在）。"""
    try:
        from storage import data
        return (data.get("ai_model_override") or "").strip()
    except Exception:
        return ""


def _model_candidates():
    """候选顺序：手动指定 > 上次跑通的 > .env 配置 > 内置备用链。"""
    out = []
    for m in [_model_override(), _ACTIVE_MODEL, AI_MODEL] + list(AI_FALLBACK_MODELS):
        if m and m not in out:
            out.append(m)
    return out


def current_model():
    """当前实际在用的模型（给 /version、/aimodel 显示）。"""
    return _model_override() or _ACTIVE_MODEL or AI_MODEL


async def _post_chat(body, timeout=70):
    """发一次 chat/completions，模型不可用时自动换下一个候选。返回 message 字典。"""
    global _ACTIVE_MODEL
    if not AI_API_KEY or not AI_BASE_URL:
        raise RuntimeError("AI 未配置（缺 AI_API_KEY / AI_BASE_URL）")
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    tried, last = [], None
    for model in _model_candidates():
        tried.append(model)
        payload = dict(body, model=model)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                msg = resp.json()["choices"][0]["message"]
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            detail = (e.response.text or "")[:200]
            switchable = code in _SWITCH_STATUS or (
                code == 400 and any(h in detail.lower() for h in _UNSUPPORTED_HINTS))
            if not switchable:
                raise
            last = f"{model}: HTTP{code} {detail[:100]}"
            logging.warning(f"AI 模型 {model} 不可用({code})，换下一个: {detail[:100]}")
            continue
        if model != _ACTIVE_MODEL:
            if _ACTIVE_MODEL or model != AI_MODEL:
                logging.warning(f"AI 模型切换: {_ACTIVE_MODEL or AI_MODEL} → {model}")
            _ACTIVE_MODEL = model
        return msg
    raise RuntimeError(f"所有 AI 模型都不可用（已试 {len(tried)} 个：{'、'.join(tried)}）"
                       f"｜最后错误 {last}")


async def ask_ai(prompt: str):
    """调用中转站AI"""
    m = await _post_chat({
        "messages": [
            {"role": "system", "content": "你是加密货币行情分析助手。基于提供的技术指标数据，给出简洁客观的中文分析（200字内）。必须说明这不构成投资建议。不要编造数据。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
    }, timeout=60)
    return m.get("content") or ""

async def ask_ai_messages(messages, system=None, temperature=0.7):
    """多轮对话版：messages 是 [{role,content}...]，可选 system。用于群内@对话。"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(messages)
    m = await _post_chat({"messages": msgs, "temperature": temperature}, timeout=60)
    return m.get("content") or ""


async def _chat_completion(msgs, tools=None, temperature=0.7, timeout=70):
    body = {"messages": msgs, "temperature": temperature}
    if tools:
        body["tools"] = tools
    return await _post_chat(body, timeout=timeout)


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
    msgs = ([{"role": "system", "content": system}] if system else []) + list(messages)

    async def _call(body):
        # 走 _post_chat：模型挂了会自动降级到备用模型
        return await _post_chat(body, timeout=90)

    base = {"messages": msgs, "tools": tools, "temperature": temperature}
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


async def _probe_model(model, timeout=30):
    """用最小请求探一个模型活不活。返回 (ok, 说明)。"""
    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {AI_API_KEY}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 5}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.status_code == 200:
                return True, "可用"
            try:
                m = (r.json().get("error") or {}).get("message", "")[:60]
            except Exception:
                m = r.text[:60]
            return False, f"HTTP{r.status_code} {m}"
    except Exception as e:
        return False, f"{type(e).__name__} {str(e)[:50]}"


async def aimodel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/aimodel —— 查看/切换 AI 模型（仅管理员）。

    中转站的渠道时有时无，主模型掉线时整个 AI 会 404 罢工。有了这个命令就能
    在 Telegram 里直接换模型救急，不用登服务器改 .env 再重新部署。
    用法：/aimodel（看状态）｜/aimodel test（逐个探活）｜/aimodel list（中转站模型列表）
         ｜/aimodel <模型名>（切过去，切之前先探活）｜/aimodel auto（取消手动指定）
    """
    global _ACTIVE_MODEL
    from handlers.util import safe_reply
    from storage import data, save_data
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await safe_reply(update.message, "只有管理员能切换 AI 模型")
        return
    if not AI_API_KEY or not AI_BASE_URL:
        await safe_reply(update.message, "AI 未配置（缺 AI_API_KEY / AI_BASE_URL）")
        return
    arg = (context.args[0].strip() if context.args else "")

    if not arg:
        ov = _model_override()
        lines = [
            "🤖 *AI 模型状态*",
            f"当前在用：`{current_model()}`",
            f".env 配置：`{AI_MODEL}`",
            f"手动指定：{'`' + ov + '`' if ov else '（无，自动）'}",
            f"运行时已切到：{'`' + _ACTIVE_MODEL + '`' if _ACTIVE_MODEL else '（还没切过）'}",
            "",
            "备用链（主模型挂了按序自动顶上）：",
            "  " + " → ".join(f"`{m}`" for m in AI_FALLBACK_MODELS),
            "",
            "`/aimodel test` 逐个探活｜`/aimodel <模型名>` 切换｜`/aimodel auto` 恢复自动",
        ]
        await safe_reply(update.message, "\n".join(lines), parse_mode="Markdown")
        return

    if arg == "auto":
        data.pop("ai_model_override", None)
        save_data()
        await safe_reply(update.message,
                         f"✅ 已取消手动指定，恢复自动降级。当前：`{current_model()}`",
                         parse_mode="Markdown")
        return

    if arg == "list":
        url = AI_BASE_URL.rstrip("/") + "/models"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.get(url, headers={"Authorization": f"Bearer {AI_API_KEY}"})
                r.raise_for_status()
                ids = [x.get("id") for x in r.json().get("data", []) if x.get("id")]
        except Exception as e:
            await safe_reply(update.message, f"拉取模型列表失败：{str(e)[:80]}")
            return
        await safe_reply(update.message,
            f"中转站共 {len(ids)} 个模型：\n" + "\n".join(f"· `{i}`" for i in ids[:60]) +
            "\n\n⚠️ 列表里有 ≠ 你的分组能用，`/aimodel test` 或直接切过去才知道",
            parse_mode="Markdown")
        return

    if arg == "test":
        await safe_reply(update.message, "🔍 正在逐个探活…")
        cands = _model_candidates()
        out = []
        for m in cands[:10]:
            ok, why = await _probe_model(m)
            out.append(f"{'✅' if ok else '❌'} `{m}` {'' if ok else '— ' + why}")
        await safe_reply(update.message,
                         "🤖 *模型探活*\n" + "\n".join(out) +
                         f"\n\n当前在用：`{current_model()}`",
                         parse_mode="Markdown")
        return

    # 切到指定模型：先探活再落盘，避免把 AI 切进一个死模型
    ok, why = await _probe_model(arg)
    if not ok:
        await safe_reply(update.message,
                         f"❌ `{arg}` 探活失败：{why}\n没有切换，仍在用 `{current_model()}`",
                         parse_mode="Markdown")
        return
    data["ai_model_override"] = arg
    _ACTIVE_MODEL = arg
    save_data()
    await safe_reply(update.message, f"✅ 已切换到 `{arg}`（探活通过，已落盘）",
                     parse_mode="Markdown")


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
