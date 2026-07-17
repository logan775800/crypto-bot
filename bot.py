import logging
import datetime
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, PicklePersistence
)
from config import TOKEN, BROADCAST_HOUR, BROADCAST_MINUTE, update_coins, COIN_IDS
import api
from handlers import price, alert, portfolio, menu, broadcast, chart, market, analysis, ai, arbitrage, whale, welcome, dashboard, okx, market_alert, backup, monitor, prefs, movers, news, unlock, summary, quickprice, stock, whale_track, indicator_alert, strategy, contract_alert, contract_ws, grid, watchpct, checklist, streak, vtrade, rtrade, chat, rstats, riskguard, brief, condalert, fundextreme, annotchart, datameta

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

HELP_TEXT = (
    "❓ *使用帮助*\n\n"
    "*最快上手*：直接发币名就能查价，例如 `BTC`、`eth`、`pepe`\n\n"
    "*常用命令*\n"
    "/menu 打开功能菜单（推荐，点按钮即可）\n"
    "/dashboard 市场看板\n"
    "/price BTC 查币价（/price BTC cny 看人民币）\n"
    "/top 涨跌榜　/analyze BTC 技术分析\n"
    "/ai BTC AI 解读　/news 最新新闻\n"
    "💬 *群里 @我 或回复我* 就能直接对话问问题；私聊用 `/ask 你的问题`（/resetchat 清空记忆）\n"
    "/alert 价格预警（也可在菜单里点着设）\n"
    "/cond BTC <60000 rsi15m<30 🎯 条件提醒：价格+指标**同时**满足才叫（/conds 管理）\n"
    "/fex 💵 资金费率极端榜（全市场跨所，按结算周期归一日化，标出1h高频费率）\n"
    "/achart BTC 1h 📐 标注图表：EMA/摆动高低点/前高前低/1.5×ATR止损带**画在图上**\n"
    "/datacheck BANK 🔎 数据体检：各维度取不取得到、数据截至几点、完整度多少\n"
    "/gasalert 15 Gas跌破提醒　/arbwatch 0.8 套利监控\n"
    "/track 0x地址 追踪巨鲸地址　/tracked 我的追踪\n"
    "/portfolio 我的持仓（请私聊使用）\n\n"
    "*🎮 虚拟合约交易*（模拟盘，练手不碰真钱，私聊）\n"
    "/vopen BTC long 1000 10 开多（1000U保证金10x，入场取现价）\n"
    "　└ /vclose BTC 平仓　/vpos 持仓+浮盈+爆仓价　/vhistory 胜率历史\n\n"
    "*🔴 实盘交易*（Bybit永续·管理员·默认模拟盘）\n"
    "/trade 🎛 交易台——点按钮开仓/平仓/改止损/预警，记不住命令就用它\n"
    "/ropen BTC long 1000 10 62000 sl=60000 tp=68000 限价开仓(带止盈损,弹确认)\n"
    "　└ /rclose BTC 平仓　/rpos 实盘持仓　/rbal 余额　/rorders /rcancel 挂单\n"
    "　└ /rtpsl BTC tp= sl= 改止盈止损　/rliqalert 5 爆仓预警\n"
    "/rstats 30 实盘复盘：胜率/盈亏比/期望值/最大回撤，按币·多空·持仓时长·时段拆解\n"
    "　└ `/rstats 30 ai` 让 AI 从数字里挑出你的行为漏洞（这是唯一能提升胜率的功能）\n"
    "/risk 🛡 风险守护：保证金率/同向集中度/当日亏损熔断/裸奔仓位/BTC破位联动\n"
    "/brief 🌅 AI 盘前简报：市场结构+资金费极值+**你每个仓的具体风险点**（可订阅每天8:30）\n\n"
    "*盯盘 / 合约*\n"
    "/watchpct BTC 2 持续波动监控，涨跌超±2%就提醒（报后自动续盯）\n"
    "　└ 加「合约」盯永续价，如 `/watchpct LAB 2 合约`（OKX/Bybit永续秒级实时）\n"
    "　└ /watchpcts 我的监控　/unwatchpct BTC 取消\n"
    "/upstreak 连续N天上涨的合约扫描，如 `/upstreak 3 bybit`\n"
    "/downstreak 连续N天下跌的合约扫描（抄底参考）\n"
    "/watchcontract 订阅全交易所合约异动告警（±20%起分级）\n"
    "/checklist 合约交易检查清单（开仓前必看，含费率周期/爆仓/止损）\n"
    "　└ 查币名看合约行情会标「资金费结算周期」，1h高频费率会⚠️提醒\n\n"
    "*底部快捷键*：📋菜单 / 📊看板 / 💰查价 / ❓帮助\n"
    "⚠️ 所有数据仅供参考，不构成投资建议"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 先发欢迎语并装上底部常驻键盘，再弹出分类菜单
    await update.message.reply_text(
        menu.WELCOME_TEXT, reply_markup=menu.persistent_kb(), parse_mode="Markdown")
    await update.message.reply_text(
        "👇 点分类直接出结果，无需记命令",
        reply_markup=menu.main_menu_kb(), parse_mode="Markdown")

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP_TEXT, reply_markup=menu.persistent_kb(), parse_mode="Markdown")

async def chat_id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/id 显示当前会话的 chat_id + 你是否被当前运行的机器人识别为管理员（排查部署权限用）"""
    from config import is_admin, ADMIN_IDS
    c = update.effective_chat
    u = update.effective_user
    admin = "✅ 是" if is_admin(u.id) else "❌ 否"
    await update.message.reply_text(
        f"本会话 chat_id: `{c.id}`\n类型: {c.type}\n你的 user_id: `{u.id}`\n"
        f"是否管理员(能否点部署): {admin}\n"
        f"当前生效的管理员 id: `{', '.join(sorted(ADMIN_IDS)) or '(未配置)'}`",
        parse_mode="Markdown")

async def prune_job(context: ContextTypes.DEFAULT_TYPE):
    """每日清理 data.json 里只增不减的冷却/去重记录，避免文件越写越大越慢。"""
    from storage import prune_data
    try:
        removed = prune_data()
        if removed:
            logging.info(f"data.json 清理: {removed}")
    except Exception as e:
        logging.error(f"data.json 清理失败: {e}")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """全局错误处理：任何 handler 未捕获的异常都记日志 + 私信管理员，
    避免像以前那样「功能静默失效、只能靠用户截图才发现」。"""
    import traceback
    err = context.error
    logging.error(f"未捕获异常: {err}", exc_info=err)
    try:
        where = ""
        if isinstance(update, Update):
            u = update.effective_user
            m = update.effective_message
            where = (f"\n会话 {update.effective_chat.id if update.effective_chat else '?'}"
                     f"｜用户 {u.id if u else '?'}"
                     f"\n消息: {(m.text or '')[:80] if m else ''}")
        tb = "".join(traceback.format_exception(type(err), err, err.__traceback__))[-600:]
        from handlers.monitor import notify_admin
        await notify_admin(context, f"🐞 机器人异常\n{type(err).__name__}: {str(err)[:150]}{where}\n\n{tb}")
    except Exception as e:
        logging.error(f"异常上报失败: {e}")


async def version_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/version 报告线上真正在跑的版本 + 关键配置状态（排查"部署到底生效没"用）。"""
    from config import VERSION, AI_MODEL, AI_API_KEY, ADMIN_IDS, is_admin
    ai_ok = "已配置" if AI_API_KEY else "❌未配置"
    try:
        from bybit_trade import BYBIT_API_KEY, _is_testnet
        by = ("🧪模拟盘" if _is_testnet() else "🔴实盘") if BYBIT_API_KEY else "❌未配置"
    except Exception:
        by = "❌未配置"
    me = "✅ 是" if is_admin(update.effective_user.id) else "❌ 否"
    await update.message.reply_text(
        f"🤖 *运行状态*\n"
        f"━━━━━━━━━━━━━━\n"
        f"📦 版本　　`{VERSION}`\n"
        f"🧠 AI模型　`{AI_MODEL}`（{ai_ok}）\n"
        f"💹 Bybit　 {by}\n"
        f"👤 管理员　{len(ADMIN_IDS) or '未限制'} 人｜你是管理员：{me}\n"
        f"━━━━━━━━━━━━━━\n"
        f"版本对不上就是部署没生效",
        parse_mode="Markdown")


async def on_chat_migrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """群升级为超级群时 chat_id 会变，旧 id 从此推送 400（订阅静默失效）。
    这里自动把所有订阅从旧 id 迁到新 id，用户无需重新订阅。"""
    m = update.message
    if not m:
        return
    if m.migrate_to_chat_id is not None:        # 收到于旧群：chat.id=旧, migrate_to=新
        old, new = m.chat.id, m.migrate_to_chat_id
    elif m.migrate_from_chat_id is not None:    # 收到于新超级群：chat.id=新, migrate_from=旧
        old, new = m.migrate_from_chat_id, m.chat.id
    else:
        return
    from storage import migrate_chat
    moved = migrate_chat(old, new)
    logging.info(f"群升级：{old} → {new}，已迁移 {moved} 条订阅")
    if moved:
        try:
            await context.bot.send_message(
                chat_id=new,
                text=f"🔄 检测到本群已升级为超级群，已自动迁移 {moved} 项订阅/监控，无需重新设置。")
        except Exception as e:
            logging.error(f"群升级迁移通知失败: {e}")


async def migrate_chat_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/migratechat <旧chat_id> 手动把旧会话的订阅迁到当前会话。
    用于补救「群升级发生在机器人加自动迁移之前」导致的旧订阅失效。仅管理员。"""
    from config import is_admin
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ 仅管理员可操作")
        return
    if not context.args:
        await update.message.reply_text(
            "用法：/migratechat <旧chat_id>\n"
            "在**目标会话里**发送，把旧 id 名下的订阅/监控全部迁到本会话。\n"
            "群 id 一般是负数，如 -123456789")
        return
    try:
        old = int(context.args[0])
    except ValueError:
        await update.message.reply_text("旧 chat_id 要是数字（如 -123456789）")
        return
    new = update.effective_chat.id
    if old == new:
        await update.message.reply_text("旧 id 就是当前会话，无需迁移")
        return
    from storage import migrate_chat
    moved = migrate_chat(old, new)
    await update.message.reply_text(
        f"🔄 已把 `{old}` 名下 {moved} 项订阅/监控迁到本会话 `{new}`" if moved
        else f"没找到 `{old}` 名下的任何订阅（id 是否写对？）", parse_mode="Markdown")


async def whois_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whois <user_id> 把一个 Telegram user_id 解析成名字/用户名（该用户需跟机器人打过交道）。"""
    if update.effective_chat.type in ("group", "supergroup"):
        await update.message.reply_text("🔒 请私聊我使用 /whois")
        return
    if not context.args:
        await update.message.reply_text("用法：/whois 7774574457")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("user_id 要是纯数字")
        return
    try:
        c = await context.bot.get_chat(uid)
        name = " ".join(x for x in (c.first_name, c.last_name) if x) or "（无名字）"
        uname = f"@{c.username}" if c.username else "（无用户名）"
        me = "（就是你自己）" if uid == update.effective_user.id else ""
        await update.message.reply_text(
            f"👤 user_id {uid} {me}\n名字：{name}\n用户名：{uname}")
    except Exception as e:
        await update.message.reply_text(
            f"查不到这个 id 的资料（多半是该用户从没跟本机器人打过交道，或号已注销）。\n{str(e)[:100]}")


async def post_init(application):
    """启动时加载币种 + 设置命令菜单"""
    import logging
    from telegram import BotCommand
    # 加载币种
    try:
        mapping = await api.fetch_top_coins(250)
        update_coins(mapping)
        logging.info(f"已加载 {len(COIN_IDS)} 种币")
    except Exception as e:
        logging.error(f"加载币种列表失败，使用默认: {e}")
    # 设置 Telegram 原生命令菜单
    commands = [
        BotCommand("menu", "📋 功能菜单"),
        BotCommand("help", "❓ 使用帮助"),
        BotCommand("ask", "💬 问我任何问题(AI对话)"),
        BotCommand("dashboard", "📊 市场看板"),
        BotCommand("summary", "📰 每日市场总结"),
        BotCommand("price", "💰 查币价"),
        BotCommand("top", "🚀 涨跌榜"),
        BotCommand("movers", "📸 异动快照"),
        BotCommand("weak", "😴 弱势/横盘扫描"),
        BotCommand("momentum", "📈 动量轮动回测"),
        BotCommand("gridstart", "🔳 启动永续网格(管理员)"),
        BotCommand("gridstatus", "🔳 网格状态/利润"),
        BotCommand("gridstop", "🛑 停止网格"),
        BotCommand("analyze", "📈 技术分析"),
        BotCommand("ai", "🤖 AI分析"),
        BotCommand("news", "📰 最新新闻"),
        BotCommand("unlock", "🔓 代币解锁查询"),
        BotCommand("unlocks", "🔓 近期解锁排行"),
        BotCommand("new", "🆕 OKX新币榜"),
        BotCommand("gainers", "🚀 OKX涨跌榜"),
        BotCommand("funding", "💵 资金费率"),
        BotCommand("fprice", "📊 合约行情"),
        BotCommand("liq", "💥 爆仓数据"),
        BotCommand("ratio", "⚖️ 多空比"),
        BotCommand("fear", "😱 恐惧贪婪"),
        BotCommand("gas", "⛽ Gas费"),
        BotCommand("gasalert", "⛽ Gas提醒(跌破阈值)"),
        BotCommand("whale", "🐋 巨鲸监控"),
        BotCommand("track", "🐋 追踪地址"),
        BotCommand("tracked", "🐋 我的追踪列表"),
        BotCommand("arb", "💱 多所比价"),
        BotCommand("arbwatch", "💱 套利监控告警"),
        BotCommand("alert", "🔔 价格预警"),
        BotCommand("rsialert", "📈 技术指标告警(RSI/均线)"),
        BotCommand("watchpct", "👁 持续波动监控(指定币±%)"),
        BotCommand("watchpcts", "👁 我的波动监控列表"),
        BotCommand("checklist", "📋 合约交易检查清单"),
        BotCommand("upstreak", "📈 连续N天上涨的合约扫描"),
        BotCommand("downstreak", "📉 连续N天下跌的合约扫描"),
        BotCommand("watchmarket", "🚨 订阅市场异动告警"),
        BotCommand("watchcontract", "📊 订阅全交易所合约异动告警"),
        BotCommand("subnews", "📰 订阅新闻推送"),
        BotCommand("subunlock", "🔓 订阅解锁提醒"),
        BotCommand("subsummary", "📊 订阅每日总结"),
        BotCommand("setalert", "⚙️ 设置告警阈值"),
        BotCommand("follow", "⭐ 关注币种"),
        BotCommand("myalert", "⚙️ 我的设置"),
        BotCommand("buy", "💼 买入(私聊)"),
        BotCommand("portfolio", "💼 我的持仓(私聊)"),
        BotCommand("ranking", "🏆 盈亏排行(私聊)"),
        BotCommand("vopen", "🎮 虚拟开仓(模拟合约)"),
        BotCommand("vpos", "🎮 虚拟持仓/账户"),
        BotCommand("vclose", "🎮 虚拟平仓"),
        BotCommand("vhistory", "🎮 虚拟交易胜率/历史"),
        BotCommand("trade", "🎛 实盘交易台(点按钮操作)"),
        BotCommand("ropen", "🔴 实盘限价开仓(Bybit)"),
        BotCommand("rclose", "🔴 实盘平仓(Bybit)"),
        BotCommand("rpos", "🔴 实盘持仓(Bybit)"),
        BotCommand("rbal", "🔴 实盘合约余额(Bybit)"),
        BotCommand("rstats", "📊 实盘复盘成绩单(胜率/期望值)"),
        BotCommand("risk", "🛡 风险守护(熔断/集中度/裸奔仓)"),
        BotCommand("brief", "🌅 AI盘前简报(结合你的持仓)"),
        BotCommand("cond", "🎯 条件触发提醒(价格+指标组合)"),
        BotCommand("conds", "🎯 我的条件提醒"),
        BotCommand("fex", "💵 资金费率极端榜(全市场跨所)"),
        BotCommand("achart", "📐 标注图表(结构位+止损带画在图上)"),
        BotCommand("datacheck", "🔎 数据体检(时间+完整度)"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logging.info("命令菜单已设置")
    except Exception as e:
        logging.error(f"设置命令菜单失败: {e}")
    # 启动合约实时告警 WebSocket（OKX + Bybit 秒级）
    try:
        contract_ws.start(application)
    except Exception as e:
        logging.error(f"启动合约实时告警失败: {e}")

def main():
    # 持久化：把 user_data/chat_data(等待输入态、AI会话、AI对话上下文)存盘，
    # 重启/部署也不丢——按钮引导流程(设监控/预警/开仓)不再因重启失效。
    # 文件放 /app（与 data.json 同为宿主机绑定挂载，force-recreate 不丢）。
    persistence = PicklePersistence(filepath="/app/ptb_persistence.pickle")
    app = Application.builder().token(TOKEN).persistence(persistence).post_init(post_init).build()

    # 基础
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("id", chat_id_cmd))
    app.add_handler(CommandHandler("version", version_cmd))
    app.add_handler(CommandHandler("whois", whois_cmd))
    app.add_handler(CommandHandler("migratechat", migrate_chat_cmd))
    app.add_handler(CommandHandler("menu", menu.menu))
    app.add_handler(CommandHandler("dashboard", dashboard.dashboard))
    # 行情
    app.add_handler(CommandHandler("price", price.price))
    app.add_handler(CommandHandler("top", price.top))
    app.add_handler(CommandHandler("compare", price.compare))
    app.add_handler(CommandHandler("info", price.info))
    app.add_handler(CommandHandler("analyze", analysis.analyze))
    app.add_handler(CommandHandler("ai", ai.ai_analyze))
    app.add_handler(CommandHandler("arb", arbitrage.arb))
    app.add_handler(CommandHandler("whale", whale.whale))
    app.add_handler(CommandHandler("track", whale_track.track))
    app.add_handler(CommandHandler("untrack", whale_track.untrack))
    app.add_handler(CommandHandler("tracked", whale_track.tracked))
    app.add_handler(CommandHandler("funding", okx.funding))
    app.add_handler(CommandHandler("fprice", okx.fprice))
    app.add_handler(CommandHandler("oi", okx.open_interest))
    app.add_handler(CommandHandler("okxk", okx.okx_kline))
    app.add_handler(CommandHandler("new", okx.new_coins))
    app.add_handler(CommandHandler("gainers", okx.gainers))
    app.add_handler(CommandHandler("swap", okx.swap_gainers))
    app.add_handler(CommandHandler("depth", okx.depth))
    app.add_handler(CommandHandler("ratio", okx.long_short))
    app.add_handler(CommandHandler("liq", okx.liquidation))
    app.add_handler(CommandHandler("fundingrank", okx.funding_rank))
    app.add_handler(CommandHandler("multi", analysis.multi_period))
    app.add_handler(CommandHandler("indicators", analysis.indicators_cmd))
    app.add_handler(CommandHandler("calc", price.calc))
    app.add_handler(CommandHandler("chart", chart.chart))
    app.add_handler(CommandHandler("chartanalyze", chart.analyze_chart))
    # 标注图表：结构位/止损带画在图上（任意周期，Bybit永续）
    app.add_handler(CommandHandler("achart", annotchart.achart))
    # 数据体检：哪些维度取得到、数据几点的（排查「AI说没数据其实是接口挂了」）
    app.add_handler(CommandHandler("datacheck", datameta.datacheck))
    app.add_handler(CommandHandler("fear", market.fear))
    app.add_handler(CommandHandler("gas", market.gas))
    app.add_handler(CommandHandler("gasalert", market.set_gas_alert))
    app.add_handler(CommandHandler("arbwatch", arbitrage.set_arb_watch))
    app.add_handler(CommandHandler("stock", stock.stock))
    app.add_handler(CommandHandler("index", stock.index))
    app.add_handler(CommandHandler("piechart", chart.portfolio_chart))
    # 预警
    app.add_handler(CommandHandler("alert", alert.alert))
    app.add_handler(CommandHandler("alertpct", alert.alert_pct))
    app.add_handler(CommandHandler("watch", alert.watch))
    app.add_handler(CommandHandler("alerts", alert.list_alerts))
    app.add_handler(CommandHandler("delalert", alert.del_alert))
    app.add_handler(CommandHandler("rsialert", indicator_alert.rsi_alert))
    app.add_handler(CommandHandler("rsialerts", indicator_alert.rsi_alerts))
    # 条件触发提醒（价格+指标组合，全满足才叫）
    app.add_handler(CommandHandler("cond", condalert.cond))
    app.add_handler(CommandHandler("conds", condalert.conds))
    app.add_handler(CommandHandler("delcond", condalert.delcond))
    # 资金费率极端榜（跨所，按结算周期归一到日化）
    app.add_handler(CommandHandler("fex", fundextreme.fex))
    app.add_handler(CommandHandler("fexsub", fundextreme.fexsub))
    # 持续波动监控（指定币，涨跌超阈值反复提醒，支持小盘/合约币）
    app.add_handler(CommandHandler("watchpct", watchpct.watchpct))
    app.add_handler(CommandHandler("watchpcts", watchpct.watchpcts))
    app.add_handler(CommandHandler("unwatchpct", watchpct.unwatchpct))
    app.add_handler(CommandHandler("checklist", checklist.checklist))
    app.add_handler(CommandHandler("upstreak", streak.upstreak))
    app.add_handler(CommandHandler("downstreak", streak.downstreak))
    # 持仓
    app.add_handler(CommandHandler("add", portfolio.add_holding))
    app.add_handler(CommandHandler("buy", portfolio.buy))
    app.add_handler(CommandHandler("sell", portfolio.sell))
    app.add_handler(CommandHandler("portfolio", portfolio.portfolio))
    app.add_handler(CommandHandler("ranking", portfolio.ranking))
    app.add_handler(CommandHandler("holdings", portfolio.holdings_list))
    app.add_handler(CommandHandler("delhold", portfolio.del_holding))
    # 虚拟合约交易（模拟盘，私聊）
    app.add_handler(CommandHandler("vopen", vtrade.vopen))
    app.add_handler(CommandHandler("vclose", vtrade.vclose))
    app.add_handler(CommandHandler("vpos", vtrade.vpos))
    app.add_handler(CommandHandler("vtrade", vtrade.vpos))
    app.add_handler(CommandHandler("vhistory", vtrade.vhistory))
    app.add_handler(CommandHandler("vreset", vtrade.vreset))
    # Bybit 实盘手动交易（管理员，默认模拟盘）
    app.add_handler(CommandHandler("ropen", rtrade.ropen))
    app.add_handler(CommandHandler("rclose", rtrade.rclose))
    app.add_handler(CommandHandler("rpos", rtrade.rpos))
    app.add_handler(CommandHandler("rbal", rtrade.rbal))
    app.add_handler(CommandHandler("rorders", rtrade.rorders))
    app.add_handler(CommandHandler("rcancel", rtrade.rcancel))
    app.add_handler(CommandHandler("rtpsl", rtrade.rtpsl))
    app.add_handler(CommandHandler("rliqalert", rtrade.rliqalert))
    app.add_handler(CommandHandler("trade", rtrade.trade))
    # 实盘复盘 / 风险守护 / 盘前简报（都读真实账户，管理员+私聊）
    app.add_handler(CommandHandler("rstats", rstats.rstats))
    app.add_handler(CommandHandler("risk", riskguard.risk))
    app.add_handler(CommandHandler("brief", brief.brief))
    # 播报（功能1）
    app.add_handler(CommandHandler("subscribe", broadcast.subscribe))
    app.add_handler(CommandHandler("unsubscribe", broadcast.unsubscribe))
    app.add_handler(CommandHandler("broadcast", broadcast.broadcast_now))
    # 按钮
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, welcome.welcome))
    # 群升级为超级群 → chat_id 变了，自动迁移订阅（否则旧 id 推送 400、订阅静默失效）
    app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, on_chat_migrate))
    app.add_handler(CommandHandler("follow", prefs.follow))
    app.add_handler(CommandHandler("unfollow", prefs.unfollow))
    app.add_handler(CommandHandler("myfollows", prefs.my_follows))
    app.add_handler(CommandHandler("setalert", prefs.set_alert))
    app.add_handler(CommandHandler("myalert", prefs.my_alert))
    app.add_handler(CommandHandler("quiet", prefs.set_quiet))
    app.add_handler(CommandHandler("watchmarket", market_alert.watch_market))
    app.add_handler(CommandHandler("watchcontract", contract_alert.watch_contract))
    app.add_handler(CommandHandler("unwatchcontract", contract_alert.unwatch_contract))
    app.add_handler(CommandHandler("movers", movers.movers))
    app.add_handler(CommandHandler("weak", strategy.weak))
    app.add_handler(CommandHandler("momentum", strategy.momentum))
    # 实盘网格（Bybit 永续，默认模拟盘；仅管理员）
    app.add_handler(CommandHandler("gridstart", grid.grid_start))
    app.add_handler(CommandHandler("gridstop", grid.grid_stop))
    app.add_handler(CommandHandler("gridstatus", grid.grid_status))
    app.add_handler(CommandHandler("news", news.news))
    app.add_handler(CommandHandler("unlock", unlock.unlock))
    app.add_handler(CommandHandler("unlocks", unlock.unlocks))
    app.add_handler(CommandHandler("summary", summary.summary))
    app.add_handler(CommandHandler("subsummary", summary.sub_summary))
    app.add_handler(CommandHandler("unsubsummary", summary.unsub_summary))
    app.add_handler(CommandHandler("subunlock", unlock.sub_unlock))
    app.add_handler(CommandHandler("unsubunlock", unlock.unsub_unlock))
    app.add_handler(CommandHandler("subnews", news.sub_news))
    app.add_handler(CommandHandler("unsubnews", news.unsub_news))
    app.add_handler(CommandHandler("unwatchmarket", market_alert.unwatch_market))
    app.add_handler(CommandHandler("backup", backup.backup_now))
    app.add_handler(CommandHandler("watchhold", portfolio.watch_holdings))
    app.add_handler(CommandHandler("unwatchhold", portfolio.unwatch_holdings))
    app.add_handler(CommandHandler("subanalysis", broadcast.sub_analysis))
    app.add_handler(CommandHandler("unsubanalysis", broadcast.unsub_analysis))
    # 群内 @机器人 / 回复机器人 自由对话
    app.add_handler(CommandHandler("ask", chat.ask))
    app.add_handler(CommandHandler("resetchat", chat.reset_chat))
    # 放到独立高优先组(-1)：没@到就空跑放行给下面的查价；@到了才 ApplicationHandlerStop 拦截。
    # 不能和查价同组，否则同组只跑第一个匹配 handler，会把所有文字"吃掉"导致查价失效。
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, chat.mention_chat), group=-1)
    # 底部常驻键盘按钮（必须在纯文字查价之前注册，先匹配先生效）
    app.add_handler(MessageHandler(filters.Regex(r"^📋 菜单$"), menu.menu))
    app.add_handler(MessageHandler(filters.Regex(r"^📊 看板$"), dashboard.dashboard))
    app.add_handler(MessageHandler(filters.Regex(r"^💰 查价$"), quickprice.price_hint))
    app.add_handler(MessageHandler(filters.Regex(r"^❓ 帮助$"), help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, quickprice.quick_price))
    app.add_handler(CallbackQueryHandler(menu.button_handler))
    app.add_error_handler(on_error)   # 未捕获异常 → 日志 + 私信管理员

    # 定时任务
    jq = app.job_queue
    jq.run_repeating(alert.check_alerts, interval=60, first=10)
    jq.run_repeating(indicator_alert.check_ti_alerts, interval=900, first=45)  # 技术指标告警，每15分钟
    jq.run_repeating(market_alert.scan_market, interval=300, first=30)  # 市场异动扫描
    jq.run_repeating(contract_alert.scan_contracts, interval=300, first=40)  # 合约异动兜底轮询(币安主路+WS安全网)，每5分钟
    jq.run_repeating(news.push_news, interval=3600, first=120)  # 新闻推送
    jq.run_repeating(unlock.check_unlocks, interval=86400, first=180)  # 解锁检查，每天
    jq.run_repeating(backup.auto_backup, interval=86400, first=60)  # 每天自动备份
    jq.run_repeating(prune_job, interval=86400, first=300)  # 每天清理 data.json 冗余(冷却/去重/历史封顶)
    jq.run_repeating(monitor.health_check, interval=300, first=120)  # 数据源健康检查，每5分钟
    jq.run_repeating(portfolio.check_holding_moves, interval=900, first=90)  # 持仓异动检查，每15分钟
    jq.run_repeating(market.check_gas_alerts, interval=300, first=100)  # Gas阈值提醒，每5分钟
    jq.run_repeating(arbitrage.scan_arb, interval=300, first=150)  # 套利监控扫描，每5分钟
    jq.run_repeating(whale_track.check_tracked, interval=600, first=200)  # 巨鲸地址追踪，每10分钟
    jq.run_repeating(grid.poll_grids, interval=20, first=25)  # 网格成交轮询+反向补单，每20秒
    jq.run_repeating(watchpct.check_watchpct, interval=60, first=35)  # 持续波动监控，每60秒
    jq.run_repeating(vtrade.check_liquidations, interval=60, first=50)  # 虚拟合约爆仓监控，每60秒
    jq.run_repeating(rtrade.check_liq_alerts, interval=60, first=55)  # 实盘爆仓临近预警，每60秒
    jq.run_repeating(riskguard.check_risk, interval=60, first=70)  # 风险守护(保证金率/集中度/当日熔断/裸奔仓/BTC联动)，每60秒
    jq.run_repeating(condalert.check_conds, interval=120, first=80)  # 条件触发提醒(价格+指标组合)，每2分钟
    jq.run_repeating(fundextreme.scan_fex, interval=3600, first=240)  # 资金费极值订阅扫描，每小时
    jq.run_once(monitor.startup_notify, when=15)  # 启动告警
    # 每日播报：每天固定时间（用 UTC，注意时区换算）
    jq.run_daily(broadcast.daily_analysis, time=datetime.time(hour=1, minute=0))
    jq.run_daily(summary.daily_summary, time=datetime.time(hour=0, minute=0))  # 每日总结，北京8点
    jq.run_daily(brief.daily_brief, time=datetime.time(hour=0, minute=30))  # AI盘前简报，北京8:30（job时间是UTC）
    jq.run_daily(
        broadcast.daily_broadcast,
        time=datetime.time(hour=BROADCAST_HOUR, minute=BROADCAST_MINUTE)
    )

    logging.info("Bot 启动中...")
    app.run_polling()

if __name__ == "__main__":
    main()
