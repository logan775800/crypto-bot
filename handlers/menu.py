import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from api import get_price, get_fear_greed, get_gas_price, get_market_data, get_top_movers
from config import COIN_IDS

POPULAR = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "DOT"]

# ============ 主菜单 ============
def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 市场看板", callback_data="dash_refresh")],
        [InlineKeyboardButton("💰 行情查询", callback_data="cat_price"),
         InlineKeyboardButton("📈 技术分析", callback_data="cat_analysis")],
        [InlineKeyboardButton("🔥 OKX专区", callback_data="cat_okx")],
        [InlineKeyboardButton("📰 资讯快讯", callback_data="cat_news"),
         InlineKeyboardButton("🔔 订阅推送", callback_data="cat_subs")],
        [InlineKeyboardButton("🔔 价格预警", callback_data="cat_alert"),
         InlineKeyboardButton("🛠 实用工具", callback_data="cat_tools")],
        [InlineKeyboardButton("💼 我的持仓", callback_data="cat_holding"),
         InlineKeyboardButton("❓ 使用帮助", callback_data="cat_help")],
    ])

async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *加密货币助手*\n\n点击下方分类，按钮直接出结果，无需记命令👇",
        reply_markup=main_menu_kb(), parse_mode="Markdown"
    )

# 币种按钮（带功能前缀，点了直接执行该功能）
def coin_grid(action, back="menu_main"):
    rows = []
    for i in range(0, len(POPULAR), 5):
        rows.append([InlineKeyboardButton(c, callback_data=f"{action}:{c}") for c in POPULAR[i:i+5]])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data=back)])
    return InlineKeyboardMarkup(rows)

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")]])

def back_to(cat):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data=cat),
                                  InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")]])

# ============ 按钮处理 ============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    d = query.data

    # ---- 主菜单 ----
    if d == "menu_main":
        await query.edit_message_text(
            "🤖 *加密货币助手*\n\n点击下方分类，按钮直接出结果，无需记命令👇",
            reply_markup=main_menu_kb(), parse_mode="Markdown")

    # ---- 刷新看板 ----
    elif d == "dash_refresh":
        from handlers.dashboard import build_dashboard
        await query.edit_message_text("🔄 刷新中...")
        try:
            text = await build_dashboard()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新", callback_data="dash_refresh"),
                 InlineKeyboardButton("📋 菜单", callback_data="menu_main")],
            ])
            await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await query.edit_message_text("刷新失败", reply_markup=back_kb())

    # ============ 行情查询 ============
    elif d == "cat_price":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 查币价", callback_data="sub_price"),
             InlineKeyboardButton("📋 币详情", callback_data="sub_info")],
            [InlineKeyboardButton("🚀 涨跌榜", callback_data="do_top")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await query.edit_message_text("📊 *行情查询*\n选择功能：", reply_markup=kb, parse_mode="Markdown")

    elif d == "sub_price":
        await query.edit_message_text("💰 *查币价* - 点币种：\n(更多币用 `/price 币名`)",
            reply_markup=coin_grid("getprice", "cat_price"), parse_mode="Markdown")

    elif d == "sub_info":
        await query.edit_message_text("📋 *币详情* - 点币种：",
            reply_markup=coin_grid("getinfo", "cat_price"), parse_mode="Markdown")

    elif d == "do_top":
        await query.edit_message_text("🚀 正在获取涨跌榜...")
        try:
            gainers, losers = await get_top_movers(5)
            lines = ["🚀 *24h涨幅榜*"]
            for i, c in enumerate(gainers, 1):
                lines.append(f"{i}. {c['symbol']}: +{c['change']:.2f}%")
            lines.append("\n📉 *24h跌幅榜*")
            for i, c in enumerate(losers, 1):
                lines.append(f"{i}. {c['symbol']}: {c['change']:.2f}%")
            await query.edit_message_text("\n".join(lines), reply_markup=back_to("cat_price"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"涨跌榜出错: {e}")
            await query.edit_message_text("获取失败", reply_markup=back_to("cat_price"))

    elif d.startswith("getprice:"):
        symbol = d.split(":")[1]
        try:
            r = await get_price(symbol)
            emoji = "📈" if r["change"] >= 0 else "📉"
            await query.edit_message_text(
                f"{emoji} *{symbol}*\n价格: ${r['price']:,.2f}\n24h: {r['change']:+.2f}%",
                reply_markup=back_to("sub_price"), parse_mode="Markdown")
        except Exception:
            await query.edit_message_text("查询失败", reply_markup=back_to("sub_price"))

    elif d.startswith("getinfo:"):
        symbol = d.split(":")[1]
        try:
            md = await get_market_data([symbol])
            x = md.get(symbol)
            if x:
                await query.edit_message_text(
                    f"📋 *{symbol}*\n价格: ${x['price']:,.2f}\n市值排名: #{x['market_cap_rank']}\n"
                    f"市值: ${x['market_cap']:,.0f}\n24h量: ${x['volume']:,.0f}\n"
                    f"24h: {x['change_24h']:+.2f}% | 7d: {x['change_7d']:+.2f}% | 30d: {x['change_30d']:+.2f}%",
                    reply_markup=back_to("sub_info"), parse_mode="Markdown")
            else:
                await query.edit_message_text("无数据", reply_markup=back_to("sub_info"))
        except Exception:
            await query.edit_message_text("查询失败", reply_markup=back_to("sub_info"))

    # ============ 技术分析 ============
    elif d == "cat_analysis":
        await query.edit_message_text("📈 *技术分析* - 点币种做综合分析：\n(RSI+均线+MACD+布林带)",
            reply_markup=coin_grid("doanalyze", "menu_main"), parse_mode="Markdown")

    elif d.startswith("doanalyze:"):
        symbol = d.split(":")[1]
        await query.edit_message_text(f"🔍 正在分析 {symbol}...")
        from handlers.analysis import build_analysis_text
        try:
            text = await build_analysis_text(symbol)
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🤖 AI解读", callback_data=f"doai:{symbol}")],
                [InlineKeyboardButton("⬅️ 返回", callback_data="cat_analysis"),
                 InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")]
            ]), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"分析出错: {e}")
            await query.edit_message_text("分析失败", reply_markup=back_to("cat_analysis"))

    elif d.startswith("doai:"):
        symbol = d.split(":")[1]
        await query.edit_message_text(f"🤖 AI分析 {symbol} 中...")
        from handlers.ai import build_ai_text
        try:
            text = await build_ai_text(symbol)
            await query.edit_message_text(text, reply_markup=back_to("cat_analysis"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"AI出错: {e}")
            await query.edit_message_text("AI分析失败", reply_markup=back_to("cat_analysis"))

    # ============ 预警 ============
    elif d == "cat_alert":
        await query.edit_message_text(
            "🔔 *价格预警*\n\n"
            "`/alert BTC 60000 above` 涨破提醒\n"
            "`/alert BTC 50000 below` 跌破提醒\n"
            "`/alertpct BTC 5` 涨跌超5%\n"
            "`/watch BTC 60000 above` 持续监控\n"
            "`/alerts` 查看 | `/delalert 1` 删除\n"
            "`/subscribe` 订阅每日播报\n\n"
            "(预警需输入价格，用命令设置)",
            reply_markup=back_kb(), parse_mode="Markdown")

    # ============ OKX 专区 ============
    elif d == "cat_okx":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 新币榜", callback_data="okx_new"),
             InlineKeyboardButton("🚀 涨幅榜", callback_data="okx_gainers")],
            [InlineKeyboardButton("📊 合约涨幅", callback_data="okx_swap"),
             InlineKeyboardButton("💵 资金费率", callback_data="okx_funding_sel")],
            [InlineKeyboardButton("⚖️ 多空比", callback_data="okx_ratio_sel"),
             InlineKeyboardButton("💥 爆仓", callback_data="okx_liq_sel")],
            [InlineKeyboardButton("📊 合约行情", callback_data="okx_fprice_sel")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await query.edit_message_text("🔥 *OKX 专区* (交易所实时数据)\n点按钮直接看：", reply_markup=kb, parse_mode="Markdown")

    elif d == "okx_new":
        await query.edit_message_text("🆕 查询中...")
        from handlers.okx import build_new_text
        try:
            await query.edit_message_text(await build_new_text(), reply_markup=back_to("cat_okx"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"新币榜出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("cat_okx"))

    elif d == "okx_gainers":
        await query.edit_message_text("🚀 查询中...")
        from handlers.okx import build_gainers_text
        try:
            await query.edit_message_text(await build_gainers_text("SPOT"), reply_markup=back_to("cat_okx"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"涨幅榜出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("cat_okx"))

    elif d == "okx_swap":
        await query.edit_message_text("📊 查询中...")
        from handlers.okx import build_gainers_text
        try:
            await query.edit_message_text(await build_gainers_text("SWAP"), reply_markup=back_to("cat_okx"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"合约榜出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("cat_okx"))

    elif d == "okx_funding_sel":
        await query.edit_message_text("💵 *资金费率* - 点币种：", reply_markup=coin_grid("okxfunding", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxfunding:"):
        symbol = d.split(":")[1]
        await query.edit_message_text(f"💵 查询 {symbol}...")
        from handlers.okx import build_funding_text
        try:
            await query.edit_message_text(await build_funding_text(symbol), reply_markup=back_to("okx_funding_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"资金费率出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("okx_funding_sel"))

    elif d == "okx_ratio_sel":
        await query.edit_message_text("⚖️ *多空比* - 点币种：", reply_markup=coin_grid("okxratio", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxratio:"):
        symbol = d.split(":")[1]
        await query.edit_message_text(f"⚖️ 查询 {symbol}...")
        from handlers.okx import build_ratio_text
        try:
            await query.edit_message_text(await build_ratio_text(symbol), reply_markup=back_to("okx_ratio_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"多空比出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("okx_ratio_sel"))

    elif d == "okx_liq_sel":
        await query.edit_message_text("💥 *爆仓* - 点币种：", reply_markup=coin_grid("okxliq", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxliq:"):
        symbol = d.split(":")[1]
        await query.edit_message_text(f"💥 查询 {symbol}...")
        from handlers.okx import build_liq_text
        try:
            await query.edit_message_text(await build_liq_text(symbol), reply_markup=back_to("okx_liq_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"爆仓出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("okx_liq_sel"))

    # ============ 资讯快讯 ============
    elif d == "cat_news":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📰 最新新闻", callback_data="do_news")],
            [InlineKeyboardButton("📸 异动快照", callback_data="do_movers")],
            [InlineKeyboardButton("📊 市场总结", callback_data="do_summary")],
            [InlineKeyboardButton("🔓 解锁排行", callback_data="do_unlocks")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await query.edit_message_text("📰 *资讯快讯*\n点按钮直接看：", reply_markup=kb, parse_mode="Markdown")

    elif d == "do_news":
        await query.edit_message_text("📰 获取新闻...")
        from handlers.news import fetch_news, translate_news
        try:
            items = await fetch_news(8)
            cn = await translate_news(items)
            lines = ["📰 *最新加密新闻*\n"]
            for i, it in enumerate(items, 1):
                title = cn.get(i, it["title"]) if cn else it["title"]
                lines.append(f"{i}. [{title}]({it['link']})")
            await query.edit_message_text("\n".join(lines), reply_markup=back_to("cat_news"),
                parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"菜单新闻出错: {e}")
            await query.edit_message_text("获取失败", reply_markup=back_to("cat_news"))

    elif d == "do_movers":
        await query.edit_message_text("📸 获取异动快照...")
        from handlers.movers import _okx_get
        try:
            from handlers import movers as _m
            # 复用 movers 逻辑：直接调OKX
            import handlers.movers
            d2 = await _okx_get("/api/v5/market/tickers", {"instType": "SPOT"})
            coins = []
            for t in d2["data"]:
                if not t["instId"].endswith("-USDT"): continue
                try:
                    last=float(t["last"]); op=float(t["open24h"]); vol=float(t["volCcy24h"])
                    if op<=0 or vol<1000000: continue
                    coins.append({"sym":t["instId"].replace("-USDT",""),"change":(last-op)/op*100})
                except: continue
            g=sorted(coins,key=lambda x:x["change"],reverse=True)[:5]
            l=sorted(coins,key=lambda x:x["change"])[:5]
            lines=["📸 *异动快照*\n🚀涨幅:"]
            for c in g: lines.append(f"  {c['sym']}: {c['change']:+.1f}%")
            lines.append("💥跌幅:")
            for c in l: lines.append(f"  {c['sym']}: {c['change']:+.1f}%")
            await query.edit_message_text("\n".join(lines), reply_markup=back_to("cat_news"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单异动出错: {e}")
            await query.edit_message_text("获取失败", reply_markup=back_to("cat_news"))

    elif d == "do_summary":
        await query.edit_message_text("📊 生成市场总结...")
        from handlers.summary import build_summary
        try:
            await query.edit_message_text(await build_summary(), reply_markup=back_to("cat_news"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单总结出错: {e}")
            await query.edit_message_text("生成失败", reply_markup=back_to("cat_news"))

    elif d == "do_unlocks":
        await query.edit_message_text("🔓 查询解锁排行...")
        try:
            import handlers.unlock as _u
            import time, datetime, asyncio
            now=time.time(); window=now+30*86400
            async def chk(sym,proj):
                try:
                    name,future,total=await _u.get_unlock_events(proj)
                    if not future or not total: return None
                    for e in future:
                        if e["timestamp"]<=window:
                            toks=e.get("noOfTokens",[]); pct=(sum(toks)/total*100) if toks and total else 0
                            if pct>=0.5: return {"sym":sym,"ts":e["timestamp"],"pct":pct}
                    return None
                except: return None
            res=await asyncio.gather(*[chk(s,p) for s,p in list(_u.SYMBOL_MAP.items())[:20]])
            r=[x for x in res if x]; r.sort(key=lambda x:x["ts"])
            if not r:
                await query.edit_message_text("近30天主流币无大额解锁", reply_markup=back_to("cat_news"))
            else:
                lines=["🔓 *未来30天大额解锁*\n"]
                for x in r[:10]:
                    dt=datetime.datetime.fromtimestamp(x["ts"]).strftime("%m-%d")
                    lines.append(f"{dt} {x['sym']} 解锁{x['pct']:.1f}%")
                await query.edit_message_text("\n".join(lines), reply_markup=back_to("cat_news"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单解锁出错: {e}")
            await query.edit_message_text("获取失败", reply_markup=back_to("cat_news"))

    # ============ 个性化设置 ============
    elif d == "my_settings":
        from handlers.prefs import get_pref
        chat_id = query.message.chat_id
        pref = get_pref(chat_id)
        follows = ', '.join(pref["follows"]) if pref["follows"] else "全市场（未设关注）"
        quiet = f"{pref['quiet'][0]}-{pref['quiet'][1]}" if pref.get("quiet") else "无"
        text = (
            "⚙️ *我的个性化设置*\n\n"
            f"📊 异动告警阈值: *{pref['threshold']:g}%*\n"
            f"⭐ 关注的币: *{follows}*\n"
            f"🔕 静音时段: *{quiet}*\n\n"
            "━━━━━━\n"
            "*如何修改*（发送命令）:\n\n"
            "`/setalert 15` 设阈值\n"
            "`/follow BTC ETH` 关注币\n"
            "`/unfollow BTC` 取消关注\n"
            "`/quiet 23:00 8:00` 免打扰\n"
            "`/quiet off` 取消免打扰\n\n"
            "💡 设关注后告警只推关注的币"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 刷新", callback_data="my_settings")],
            [InlineKeyboardButton("⬅️ 返回订阅", callback_data="cat_subs"),
             InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
        ])
        await query.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")

    # ============ 订阅推送（按钮+状态）============
    elif d == "cat_subs":
        from storage import data as _sd
        chat_id = query.message.chat_id
        # 各订阅状态检查
        def status(key, is_dict=False):
            v = _sd.get(key, {} if is_dict else [])
            if is_dict:
                return "✅" if str(chat_id) in v or chat_id in v else "⬜"
            return "✅" if chat_id in v else "⬜"
        m = status("market_watch")
        nw = status("news_subs")
        ul = status("unlock_subs")
        sm = status("summary_subs")
        bc = status("broadcast_chats")
        an = status("analysis_subs")
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{m} 市场异动告警", callback_data="tog_market")],
            [InlineKeyboardButton(f"{nw} 新闻推送", callback_data="tog_news")],
            [InlineKeyboardButton(f"{ul} 解锁提醒", callback_data="tog_unlock")],
            [InlineKeyboardButton(f"{sm} 每日总结", callback_data="tog_summary")],
            [InlineKeyboardButton(f"{bc} 每日行情播报", callback_data="tog_broadcast")],
            [InlineKeyboardButton(f"{an} 每日技术分析", callback_data="tog_analysis")],
            [InlineKeyboardButton("⚙️ 我的个性化设置", callback_data="my_settings")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await query.edit_message_text(
            "🔔 *订阅推送*\n✅已订阅 ⬜未订阅，点击切换：",
            reply_markup=kb, parse_mode="Markdown")

    # 切换订阅状态
    elif d.startswith("tog_"):
        from storage import data as _sd, save_data as _ss
        chat_id = query.message.chat_id
        what = d.replace("tog_", "")
        # 映射：订阅类型 -> (data键, 是否dict)
        sub_map = {
            "market": ("market_watch", False),
            "news": ("news_subs", False),
            "unlock": ("unlock_subs", False),
            "summary": ("summary_subs", False),
            "broadcast": ("broadcast_chats", False),
            "analysis": ("analysis_subs", False),
        }
        if what in sub_map:
            key, is_dict = sub_map[what]
            _sd.setdefault(key, [])
            if chat_id in _sd[key]:
                _sd[key].remove(chat_id)
            else:
                _sd[key].append(chat_id)
            _ss()
        # 重新渲染订阅菜单（刷新状态）
        def status(key):
            return "✅" if chat_id in _sd.get(key, []) else "⬜"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"{status('market_watch')} 市场异动告警", callback_data="tog_market")],
            [InlineKeyboardButton(f"{status('news_subs')} 新闻推送", callback_data="tog_news")],
            [InlineKeyboardButton(f"{status('unlock_subs')} 解锁提醒", callback_data="tog_unlock")],
            [InlineKeyboardButton(f"{status('summary_subs')} 每日总结", callback_data="tog_summary")],
            [InlineKeyboardButton(f"{status('broadcast_chats')} 每日行情播报", callback_data="tog_broadcast")],
            [InlineKeyboardButton(f"{status('analysis_subs')} 每日技术分析", callback_data="tog_analysis")],
            [InlineKeyboardButton("⚙️ 我的个性化设置", callback_data="my_settings")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await query.edit_message_text(
            "🔔 *订阅推送*\n✅已订阅 ⬜未订阅，点击切换：",
            reply_markup=kb, parse_mode="Markdown")

    elif d == "okx_fprice_sel":
        await query.edit_message_text("📊 *合约行情* - 点币种：", reply_markup=coin_grid("okxfprice", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxfprice:"):
        symbol = d.split(":")[1]
        await query.edit_message_text(f"📊 查询 {symbol} 合约...")
        from handlers.okx import build_fprice_text
        try:
            await query.edit_message_text(await build_fprice_text(symbol), reply_markup=back_to("okx_fprice_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"合约行情出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("okx_fprice_sel"))

    # ============ 工具（按钮直达）============
    elif d == "cat_tools":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("😱 恐惧贪婪", callback_data="do_fear"),
             InlineKeyboardButton("⛽ Gas费", callback_data="do_gas")],
            [InlineKeyboardButton("🐋 巨鲸监控", callback_data="do_whale"),
             InlineKeyboardButton("💱 多所比价", callback_data="sub_arb")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await query.edit_message_text("🛠 *实用工具*\n点按钮直接出结果：", reply_markup=kb, parse_mode="Markdown")

    elif d == "do_fear":
        await query.edit_message_text("😱 获取中...")
        try:
            fg = await get_fear_greed()
            await query.edit_message_text(
                f"😱 *恐惧贪婪指数*\n{fg['value']}/100 - {fg['classification']}\n(不构成投资建议)",
                reply_markup=back_to("cat_tools"), parse_mode="Markdown")
        except Exception:
            await query.edit_message_text("获取失败", reply_markup=back_to("cat_tools"))

    elif d == "do_gas":
        await query.edit_message_text("⛽ 获取中...")
        try:
            gwei = await get_gas_price()
            await query.edit_message_text(f"⛽ *以太坊Gas*: {gwei:.2f} gwei",
                reply_markup=back_to("cat_tools"), parse_mode="Markdown")
        except Exception:
            await query.edit_message_text("获取失败", reply_markup=back_to("cat_tools"))

    elif d == "do_whale":
        await query.edit_message_text("🐋 扫描最新区块...")
        from handlers.whale import build_whale_text
        try:
            text = await build_whale_text(100)
            await query.edit_message_text(text, reply_markup=back_to("cat_tools"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"巨鲸出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("cat_tools"))

    elif d == "sub_arb":
        await query.edit_message_text("💱 *多交易所比价* - 点币种：",
            reply_markup=coin_grid("doarb", "cat_tools"), parse_mode="Markdown")

    elif d.startswith("doarb:"):
        symbol = d.split(":")[1]
        await query.edit_message_text(f"💱 查询 {symbol} 各所价格...")
        from handlers.arbitrage import build_arb_text
        try:
            text = await build_arb_text(symbol)
            await query.edit_message_text(text, reply_markup=back_to("sub_arb"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"比价出错: {e}")
            await query.edit_message_text("查询失败", reply_markup=back_to("sub_arb"))

    # ============ 持仓 ============
    elif d == "cat_holding":
        await query.edit_message_text(
            "💼 *我的持仓* (🔒私聊使用)\n\n"
            "`/buy BTC 0.5 60000` 买入\n"
            "`/sell BTC 0.3` 卖出\n"
            "`/portfolio` 组合盈亏\n"
            "`/ranking` 盈亏排行\n"
            "`/piechart` 持仓饼图",
            reply_markup=back_kb(), parse_mode="Markdown")

    # ============ 帮助 ============
    elif d == "cat_help":
        await query.edit_message_text(
            "❓ *使用帮助*\n\n"
            "📊 行情 - 几百种币实时价格\n"
            "📈 分析 - 技术指标+AI解读\n"
            "🔔 预警 - 到价自动提醒\n"
            "🛠 工具 - 比价/情绪/Gas/巨鲸\n"
            "💼 持仓 - 记录盈亏(私聊)\n\n"
            "💡 随时发 /menu 打开\n"
            "⚠️ 数据仅供参考，不构成投资建议",
            reply_markup=back_kb(), parse_mode="Markdown")
