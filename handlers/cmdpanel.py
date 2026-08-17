"""全部命令的按钮面板 —— 从**已注册的 handler 自动生成**，不手工维护清单。

为什么不写死一张表：这个 bot 有 150 个命令，手工维护必然漏，而且漏掉的
恰恰是新加的那个（用户最需要发现的就是新功能）。所以直接读 Application
里注册了什么，按模块分组，用 docstring 首行当标签。加了新命令自动出现。

一个关键前提让这件事变得简单：**这个 bot 里几乎每个带参数的命令，
在不带参数调用时都会打印自己的用法**（/net /sym /plan /backtest /restore …）。
所以按钮统一「不带参数执行」就够了——要么直接出结果，要么出用法。
"""
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

log = logging.getLogger(__name__)

PAGE = 20            # 每页命令数，太多 Telegram 会挤成一坨

# 模块 → 分类名。没映射到的落进「其他」，并由测试盯着（避免新模块悄悄沉底）。
MODULE_CN = {
    "handlers.price": "💰 行情查询",
    "handlers.quickprice": "💰 行情查询",
    "handlers.market": "💰 行情查询",
    "handlers.marketdata": "💰 行情查询",
    "handlers.movers": "💰 行情查询",
    "handlers.dashboard": "💰 行情查询",
    "handlers.detail": "💰 行情查询",
    "handlers.compare": "💰 行情查询",
    "handlers.stock": "💰 行情查询",
    "handlers.analysis": "📈 分析与回测",
    "handlers.indicator_alert": "📈 分析与回测",
    "handlers.regime": "📈 分析与回测",
    "handlers.chart": "📈 分析与回测",
    "handlers.annotchart": "📈 分析与回测",
    "handlers.strategy": "📈 分析与回测",
    "handlers.backtest": "📈 分析与回测",
    "handlers.weak_scan": "📈 分析与回测",
    "handlers.scan": "🔍 机会发现",
    "handlers.streak": "🔍 机会发现",
    "handlers.fundextreme": "🔍 机会发现",
    "handlers.arbitrage": "🔍 机会发现",
    "handlers.whale": "🔍 机会发现",
    "handlers.whale_track": "🔍 机会发现",
    "handlers.okx": "🏦 交易所专区",
    "handlers.binance": "🏦 交易所专区",
    "handlers.bybit": "🏦 交易所专区",
    "handlers.gate": "🏦 交易所专区",
    "handlers.source": "🏦 交易所专区",       # /source 选用哪家的价
    "handlers.onchain": "🔍 机会发现",        # 链上代币：交易所还没上的那一段
    "handlers.steady": "🔍 机会发现",
    "handlers.changelog": "🧭 菜单与帮助",
    "handlers.plan": "📋 交易计划",
    "handlers.econ": "🧮 成本与仓位",
    "handlers.sizing": "🧮 成本与仓位",
    "handlers.symbols": "🧮 成本与仓位",
    "handlers.riskguard": "🛡 风险管理",
    "handlers.riskprofile": "🛡 风险管理",
    "handlers.keyguard": "🛡 风险管理",
    "handlers.privacy": "🛡 风险管理",
    "handlers.alert": "🔔 预警与监控",
    "handlers.condalert": "🔔 预警与监控",
    "handlers.watchpct": "🔔 预警与监控",
    "handlers.contract_alert": "🔔 预警与监控",
    "handlers.pumpalert": "🔔 预警与监控",
    "handlers.events": "🔔 预警与监控",
    "handlers.market_alert": "🔔 预警与监控",
    "handlers.unlock": "🔔 预警与监控",
    "handlers.news": "📰 资讯与推送",
    "handlers.broadcast": "📰 资讯与推送",
    "handlers.summary": "📰 资讯与推送",
    "handlers.brief": "📰 资讯与推送",
    "handlers.vtrade": "🎮 虚拟合约",
    "handlers.rtrade": "🔴 实盘交易",
    "handlers.grid": "🔴 实盘交易",
    "handlers.rstats": "📅 复盘中心",
    "handlers.weekly": "📅 复盘中心",
    "handlers.portfolio": "💼 我的持仓",
    "handlers.cockpit": "💼 我的持仓",
    "handlers.datameta": "🩺 诊断与维护",
    "handlers.backup": "🩺 诊断与维护",
    "handlers.monitor": "🩺 诊断与维护",
    "handlers.docfile": "🩺 诊断与维护",
    "handlers.deploy": "🩺 诊断与维护",
    "handlers.menu": "🧭 菜单与帮助",
    "handlers.welcome": "🧭 菜单与帮助",
    "handlers.checklist": "🧭 菜单与帮助",
    "handlers.chat": "💬 AI 助手",
    "handlers.ai": "💬 AI 助手",
    "handlers.prefs": "🧭 菜单与帮助",
    "handlers.cmdpanel": "🧭 菜单与帮助",
    "__main__": "🧭 菜单与帮助",
    "bot": "🧭 菜单与帮助",
}
OTHER = "🗂 其他"

_INDEX = {}          # {命令名: {"fn":..., "cat":..., "label":...}}


# 兜底说明：HELP_TEXT 和 /setmycommands 都没提到的命令。
# 多是「取消/删除」这类和主命令成对出现、文档里被略过的。宁可写在这儿，
# 也不要在面板上给用户一排光秃秃的 /unwatchhold。
FALLBACK_DESC = {
    "start": "开始使用 / 重新装上底部键盘",
    "add": "添加持仓记录", "sell": "卖出持仓记录", "holdings": "我的持仓清单",
    "delhold": "删除某条持仓", "watchhold": "订阅持仓异动提醒",
    "unwatchhold": "取消持仓异动提醒",
    "alerts": "我的价格预警", "delalert": "删除某条价格预警",
    "alertpct": "设置涨跌幅预警", "watch": "添加价格监控",
    "btcregime": "BTC市场环境(变化时提醒)", "watchcontract": "订阅合约异动告警", "unwatchcontract": "取消合约异动告警",
    "unwatchmarket": "取消市场异动告警",
    "delcond": "删除某条条件提醒", "rsialerts": "我的 RSI 提醒",
    "subscribe": "订阅每日播报", "unsubscribe": "取消每日播报",
    "subanalysis": "订阅每日分析", "unsubanalysis": "取消每日分析",
    "unsubnews": "取消新闻推送", "unsubsummary": "取消每日总结",
    "unsubunlock": "取消解锁提醒", "unfollow": "取消关注某币",
    "myfollows": "我关注的币", "untrack": "取消追踪某地址",
    "quiet": "设置免打扰时段",
    "calc": "换算器（币 ↔ 法币）", "compare": "两个币对比",
    "swap": "兑换比价", "index": "市场总指数", "info": "币种基本信息",
    "stock": "美股/指数行情", "depth": "盘口深度", "oi": "持仓量 OI",
    "fundingrank": "资金费率排行", "okxk": "OKX K线图",
    "chart": "价格走势图", "piechart": "持仓占比饼图",
    "chartanalyze": "K线图 + 技术分析", "indicators": "技术指标明细",
    "multi": "多周期分析",
    "delplan": "删除交易计划（/delplan all 全删）",
    "vreset": "重置虚拟账户", "vtrade": "虚拟账户总览",
    "rcancel": "撤销实盘挂单",
    "backup": "立即备份数据",
    "broadcast": "立即播报一次（测试用）",
}

_HELP_DESC = None


def _help_descriptions():
    """从 bot.HELP_TEXT 里抽每个命令的说明。

    150 个命令里三分之二没写 docstring，但 HELP_TEXT 和 /setmycommands 里
    早就有中文说明了——与其给 109 个函数补 docstring，不如复用已经写好的。
    """
    global _HELP_DESC
    if _HELP_DESC is not None:
        return _HELP_DESC
    _HELP_DESC = {}
    try:
        import bot as _bot
        # 优先用 /setmycommands 那批说明：它们本来就是写给按钮看的，短且准
        for bc in getattr(_bot, "BOT_COMMANDS", []) or []:
            d = (getattr(bc, "description", "") or "").strip()
            if d:
                _HELP_DESC[bc.command] = d
        for line in (_bot.HELP_TEXT or "").split("\n"):
            # 一行里常挤着好几个命令：「　└ /vclose BTC 平仓　/vpos 持仓　/vhistory 胜率」
            # 所以先按全角空格/竖线切段，再逐段认命令，不能只匹配行首
            for seg in re.split(r"[　｜]", line.strip().lstrip("└ ")):
                m = re.match(r"^/(\w+)\s*(.*)$", seg.strip())
                if not m:
                    continue
                name, rest = m.group(1), m.group(2)
                # 去掉示例参数（"BTC long 0.081 ..."）：从第一个中文字起才是说明
                cn = re.search(r"[一-鿿].*", rest)
                desc = (cn.group(0) if cn else rest).strip()
                desc = re.sub(r"[*`]", "", desc).strip(" ，,。.（(")
                if desc and name not in _HELP_DESC:
                    _HELP_DESC[name] = desc
    except Exception as e:
        log.warning(f"解析 HELP_TEXT 失败: {e}")
    return _HELP_DESC


def _label(name, fn):
    """按「HELP_TEXT 说明 > docstring 首行 > 只显示命令名」取标签。

    宁可朴素也别编——没有说明就只给命令名，不要凭函数名猜一个出来。
    """
    desc = _help_descriptions().get(name, "") or FALLBACK_DESC.get(name, "")
    if not desc:
        doc = (fn.__doc__ or "").strip()
        if doc:
            first = doc.split("\n")[0].strip()
            first = re.sub(r"^/\S+\s*", "", first)
            first = re.split(r"\s*(?:——|—|--)\s*", first, maxsplit=1)[-1]
            desc = first.strip(" 。.：:")
    if not desc:
        return f"/{name}"
    return f"/{name} {desc[:24]}"


def build_index(app):
    """从 Application 里已注册的 CommandHandler 建索引。启动时调一次。"""
    _INDEX.clear()
    try:
        from telegram.ext import CommandHandler
        groups = getattr(app, "handlers", {}) or {}
        for hs in groups.values():
            for h in hs:
                if not isinstance(h, CommandHandler):
                    continue
                fn = h.callback
                cat = MODULE_CN.get(getattr(fn, "__module__", ""), OTHER)
                for name in sorted(h.commands):
                    _INDEX[name] = {"fn": fn, "cat": cat,
                                    "label": _label(name, fn)}
    except Exception as e:
        log.error(f"命令索引构建失败: {e}")
    log.info(f"命令面板已索引 {len(_INDEX)} 个命令")
    return _INDEX


def index():
    return _INDEX


def categories():
    """[(分类, 命令名列表)]，按分类名排序，「其他」永远垫底。"""
    g = {}
    for name, info in _INDEX.items():
        g.setdefault(info["cat"], []).append(name)
    for v in g.values():
        v.sort()
    return sorted(g.items(), key=lambda kv: (kv[0] == OTHER, kv[0]))


# ── 面板 ────────────────────────────────────────────────────────
def home_kb():
    rows = []
    for cat, names in categories():
        rows.append([InlineKeyboardButton(f"{cat}（{len(names)}）",
                                          callback_data=f"cmd:cat:{cat}")])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows)


def cat_kb(cat, page=0):
    names = dict(categories()).get(cat, [])
    total = max(1, (len(names) + PAGE - 1) // PAGE)
    page = max(0, min(page, total - 1))
    chunk = names[page * PAGE:(page + 1) * PAGE]
    rows = [[InlineKeyboardButton(_INDEX[n]["label"], callback_data=f"cmd:run:{n}")]
            for n in chunk]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ 上一页", callback_data=f"cmd:pg:{cat}:{page-1}"))
    if page < total - 1:
        nav.append(InlineKeyboardButton("下一页 ▶️", callback_data=f"cmd:pg:{cat}:{page+1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("⬅️ 全部分类", callback_data="cmd:home"),
                 InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")])
    return InlineKeyboardMarkup(rows), page, total


HOME_TEXT = (
    "⌨️ *全部命令*\n"
    "━━━━━━━━━━━━━━\n"
    "按分类点开，点命令直接执行。\n"
    "需要参数的命令会先告诉你怎么填，照着发一行就行。\n"
    "（这份清单是从已注册的命令自动生成的，不会漏掉新功能）"
)


async def commands_cmd(update, context):
    """/commands 全部命令（按钮面板）"""
    from handlers.util import safe_reply
    if not _INDEX:
        await safe_reply(update.message, "命令索引还没建好，稍后再试")
        return
    await safe_reply(update.message, HOME_TEXT + f"\n\n共 {len(_INDEX)} 个命令",
                     reply_markup=home_kb(), parse_mode="Markdown")


async def on_button(query, context):
    """处理 cmd: 开头的回调。由 menu.button_handler 转发。"""
    from handlers.util import safe_edit
    d = query.data
    if d == "cmd:home":
        await safe_edit(query, HOME_TEXT + f"\n\n共 {len(_INDEX)} 个命令",
                        reply_markup=home_kb(), parse_mode="Markdown")
        return
    if d.startswith("cmd:cat:"):
        cat = d.split(":", 2)[2]
        kb, page, total = cat_kb(cat)
        await safe_edit(query, f"*{cat}*　第 {page+1}/{total} 页\n点命令直接执行",
                        reply_markup=kb, parse_mode="Markdown")
        return
    if d.startswith("cmd:pg:"):
        _p, _g, cat, page = d.split(":", 3)
        kb, page, total = cat_kb(cat, int(page))
        await safe_edit(query, f"*{cat}*　第 {page+1}/{total} 页\n点命令直接执行",
                        reply_markup=kb, parse_mode="Markdown")
        return
    if d.startswith("cmd:run:"):
        name = d.split(":", 2)[2]
        info = _INDEX.get(name)
        if not info:
            await query.answer("这个命令不在了")
            return
        await query.answer(f"/{name}")
        from handlers.menu import _FakeCtx, _fake_update
        try:
            # 不带参数执行：这个 bot 里带参数的命令在无参时都会打印自己的用法，
            # 所以用户要么直接拿到结果，要么拿到「该怎么填」
            await info["fn"](_fake_update(query), _FakeCtx([], context))
        except Exception as e:
            log.error(f"命令面板执行 /{name} 出错: {e}")
            await query.message.reply_text(f"/{name} 执行失败：{str(e)[:80]}")
