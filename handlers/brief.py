"""每日 AI 盘前简报 /brief —— 和模板化的 /summary 不同，这份是给持仓的人看的。

/summary 是「市场发生了什么」，谁看都一样。
这份是「结合你手上的仓，今天该注意什么」：市场结构 + 资金费极值 + 你每个仓的具体风险点，
一起丢给 AI 出一份可执行的简报。

因为含真实账户数据 → 管理员 + 私聊。定时推送到 /brief 里订阅的那个会话。
"""
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from storage import data, save_data
from handlers.util import safe_reply, safe_edit

log = logging.getLogger(__name__)


def _cfg():
    return data.setdefault("brief", {})


SYSTEM = (
    "你是一个加密永续合约交易者的盘前简报官。用户是做杠杆永续的活跃交易者（主玩 Bybit）。"
    "下面给你的是今天的市场数据快照和他**真实账户的持仓**。\n\n"
    "写一份盘前简报，结构：\n"
    "1) **今天的市场基调**：一句话。BTC/ETH 多周期结构说明现在是趋势日还是震荡日，"
    "适合追还是适合等回踩。要引用具体数字。\n"
    "2) **值得注意的**：资金费极值意味着什么（谁拥挤了、有没有挤压风险）、情绪位置。"
    "没什么可说就直接说「今天没有特别的」，别硬凑。\n"
    "3) **你的仓**：逐个点名，每个仓给一句**具体的**风险点或操作提示——"
    "距爆仓多少、有没有止损、和 BTC 方向是否一致、资金费在吃你还是喂你。"
    "没持仓就说「今天空仓，可以从容挑机会」并给 1~2 个观察对象。\n"
    "4) **今天的一条纪律**：结合上面的情况，给一条最该守的（具体、可执行）。\n\n"
    "要求：简体中文，直接、具体、带数字。不要预测涨跌，用「风险温度/仓位管理」的口吻。"
    "总长控制在 500 字内。末尾一句「不构成投资建议」。"
)


async def _funding_extremes(limit=6):
    """全市场资金费极值（Bybit 一次 tickers 就能拿全量费率，不用逐个查）。
    同时带上结算周期——1h 结算的币费率要 ×8 才能和常规 8h 的比，
    这是 ST/退市币最容易吃亏的地方。"""
    from handlers import marketdata as md
    try:
        t = await md._get("/v5/market/tickers", {"category": "linear"})
        inst = await md._get("/v5/market/instruments-info", {"category": "linear", "limit": 1000})
    except Exception as e:
        log.warning(f"简报-资金费极值取数失败: {e}")
        return None
    # {币: 结算周期分钟}
    iv = {}
    for x in (inst.get("list") or []):
        try:
            iv[x["symbol"]] = int(x.get("fundingInterval") or 480)
        except (TypeError, ValueError, KeyError):
            continue
    rows = []
    for x in (t.get("list") or []):
        sym = x.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        try:
            rate = float(x.get("fundingRate") or 0) * 100
            turnover = float(x.get("turnover24h") or 0)
        except (TypeError, ValueError):
            continue
        if turnover < 5_000_000:      # 太小的池子费率再夸张也没意义
            continue
        mins = iv.get(sym, 480)
        rows.append({
            "sym": sym.replace("USDT", ""), "rate": rate, "mins": mins,
            # 归一到 8h 口径才能横向比：1h 结算的实际抽血速度是标称的 8 倍
            "daily": rate * (1440 / mins),
        })
    if not rows:
        return None
    rows.sort(key=lambda x: x["daily"])
    out = []

    def _fmt(r):
        tag = f"（{r['mins']//60}h结算 ⚠️高频）" if r["mins"] < 480 else ""
        return f"{r['sym']} {r['rate']:+.4f}%/期 → 折日 {r['daily']:+.3f}%{tag}"

    out.append("空头付费最多(轧空风险): " + "、".join(_fmt(r) for r in rows[:limit]))
    out.append("多头付费最多(挤多风险): " + "、".join(_fmt(r) for r in rows[-limit:][::-1]))
    return "\n".join(out)


async def _position_risk():
    """逐仓算具体风险点：距爆仓、有没有止损、名义、资金费方向。"""
    from handlers.rtrade import _client, _env_tag
    client = _client()
    out = [f"【真实账户 {_env_tag()}】"]
    try:
        bal = await client.wallet_balance("USDT")
        equity = float(bal.get("totalEquity") or 0)
        out.append(f"总权益 {equity:,.2f} USDT｜可用 {bal.get('totalAvailableBalance','?')}"
                   f"｜未实现盈亏 {bal.get('totalPerpUPL','?')}")
        mmr = bal.get("accountMMRate")
        if mmr not in (None, ""):
            out.append(f"账户维持保证金率 {float(mmr)*100:.2f}%（100%=强平）")
    except Exception as e:
        out.append(f"查余额失败：{str(e)[:60]}")
        equity = 0.0
    try:
        ps = await client.positions_all()
    except Exception as e:
        out.append(f"查持仓失败：{str(e)[:60]}")
        return "\n".join(out)
    if not ps:
        out.append("当前无持仓")
        return "\n".join(out)
    for p in ps:
        sym = p.get("symbol", "?")
        side = "多" if p.get("side") == "Buy" else "空"
        try:
            mark = float(p.get("markPrice") or 0)
            liq = float(p.get("liqPrice") or 0)
        except (TypeError, ValueError):
            mark = liq = 0
        dist = f"距爆仓 {abs(mark-liq)/mark*100:.1f}%" if mark > 0 and liq > 0 else "爆仓价未返回(仓位小/全仓)"
        sl = p.get("stopLoss")
        sl_txt = f"止损 {sl}" if str(sl or "0") not in ("0", "0.0", "", "None") else "❗未设止损"
        val = float(p.get("positionValue") or 0)
        share = f"，名义占权益 {val/equity*100:.0f}%" if equity > 0 else ""
        out.append(f"{sym} {side} {p.get('leverage','?')}x｜均价 {p.get('avgPrice')}"
                   f"｜标记 {mark}｜浮盈 {float(p.get('unrealisedPnl') or 0):+.2f}"
                   f"｜{dist}｜{sl_txt}｜名义 ${val:,.0f}{share}")
    return "\n".join(out)


async def build_brief():
    """拼数据 → 交给 AI。每一块单独 try，任一块挂了简报照出。"""
    from config import AI_API_KEY, AI_BASE_URL
    if not AI_API_KEY or not AI_BASE_URL:
        return "AI 未配置（缺 AI_API_KEY / AI_BASE_URL），盘前简报需要 AI"
    from handlers import marketdata as md
    parts = []

    try:
        parts.append(await md.market_context())
    except Exception as e:
        log.warning(f"简报-市场联动失败: {e}")
    for sym, ivs in (("BTC", ("4h", "1h")), ("ETH", ("4h",))):
        for iv in ivs:
            try:
                parts.append(await md.klines_analysis(sym, iv))
            except Exception as e:
                log.warning(f"简报-{sym} {iv} K线失败: {e}")
    try:
        fe = await _funding_extremes()
        if fe:
            parts.append("【全市场资金费极值】\n" + fe)
    except Exception as e:
        log.warning(f"简报-资金费失败: {e}")
    try:
        parts.append(await _position_risk())
    except RuntimeError:
        parts.append("【真实账户】未配置 Bybit 密钥，拿不到持仓")
    except Exception as e:
        log.warning(f"简报-持仓失败: {e}")

    if not parts:
        return "所有数据源都取不到，简报生成失败"
    from handlers.ai import ask_ai_messages
    return await ask_ai_messages(
        [{"role": "user", "content": "这是今天的数据，给我出盘前简报：\n\n" + "\n\n".join(parts)}],
        system=SYSTEM)


def _kb():
    on = _cfg().get("enabled")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔕 关闭每日推送" if on else "🔔 开启每日推送(北京8:30)",
                              callback_data="brtog")],
        [InlineKeyboardButton("🔄 重新生成", callback_data="brnow")],
        [InlineKeyboardButton("🛡 风险守护", callback_data="rgpanel"),
         InlineKeyboardButton("📊 复盘", callback_data="rsd:30")],
    ])


async def brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/brief 立即出一份盘前简报。"""
    from handlers.rtrade import _guard
    if not await _guard(update):
        return
    await safe_reply(update.message, "🌅 正在生成盘前简报…（拉市场结构+资金费+你的持仓，约 20~40 秒）")
    try:
        text = await build_brief()
    except Exception as e:
        log.error(f"盘前简报出错: {e}")
        await safe_reply(update.message, f"简报生成失败：{str(e)[:120]}")
        return
    from handlers.chat import _send
    await _send(update.message, f"🌅 *今日盘前简报*\n\n{text}")
    await safe_reply(update.message, "订阅每日自动推送 👇", reply_markup=_kb())


async def daily_brief(context: ContextTypes.DEFAULT_TYPE):
    """定时 job：北京时间 8:30 推送。未订阅则静默。"""
    c = _cfg()
    if not c.get("enabled") or not c.get("chat_id"):
        return
    try:
        text = await build_brief()
    except Exception as e:
        log.error(f"每日简报生成失败: {e}")
        return
    from handlers.chat import _md_to_tg, _strip_md
    body = f"🌅 *今日盘前简报*\n\n{text}"
    try:
        await context.bot.send_message(chat_id=c["chat_id"], text=_md_to_tg(body),
                                       parse_mode="Markdown")
    except Exception as e:
        log.warning(f"每日简报 Markdown 发送失败，降级纯文本: {e}")
        try:
            await context.bot.send_message(chat_id=c["chat_id"], text=_strip_md(body))
        except Exception as e2:
            log.error(f"每日简报推送失败: {e2}")


# ── 按钮回调（由 menu.button_handler 分发）──────────────────────────
async def toggle(query, context):
    from handlers.rtrade import _btn_admin_ok
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    c = _cfg()
    c["enabled"] = not c.get("enabled")
    c["chat_id"] = query.message.chat_id
    save_data()
    await query.answer("已开启：每天北京 8:30 推送" if c["enabled"] else "已关闭每日推送")
    await safe_edit(query, "🌅 *盘前简报*\n"
                    + ("✅ 已开启，每天北京时间 8:30 自动推送到本会话。"
                       if c["enabled"] else "⬜ 每日推送已关闭，随时 /brief 手动出一份。"),
                    reply_markup=_kb(), parse_mode="Markdown")


async def now_from_btn(query, context):
    from handlers.rtrade import _btn_admin_ok
    if not _btn_admin_ok(query):
        await query.answer("仅管理员", show_alert=True)
        return
    await query.answer("生成中，约 20~40 秒…")
    try:
        text = await build_brief()
    except Exception as e:
        log.error(f"简报按钮出错: {e}")
        await query.answer("生成失败", show_alert=True)
        return
    from handlers.chat import _send
    await _send(query.message, f"🌅 *今日盘前简报*\n\n{text}")
