"""`/howto` —— 在群里一发就把使用指南贴出来。

为什么单独做一条命令：指南发过一次就沉进聊天记录了，新进群的人看不到，
老人也懒得往上翻。给它一个命令，谁想看谁发一下。

和 `/help` 的分工：`/help` 是**全部**命令的清单（长，给已经会用的人查）；
这条是**怎么上手**（短，给不知道从哪点起的人）。所以这里刻意只列几个入口，
详细的甩链接——正文一长，底下的按钮就被挤出屏幕了。

`/vtrade` 那类涉及个人账户的**不放按钮**：按钮回调是就地编辑原消息，
在群里点一下等于把自己的持仓贴给全群看。文字里提一句就够了，
他们私聊发命令。
"""
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply

log = logging.getLogger(__name__)

# docs/guide.md 仍然跟着代码走（改功能时顺手改它），但**不再往群里甩这个链接**：
# 2026-08-25 他要求去掉。理由也站得住——完整清单现在就在置顶里（/pinhowto 维护，
# 68 个命令 + 按钮路径），再挂一个 GitHub 外链等于让人跳出去看一份重复的东西，
# 而且那是个公开仓库地址。
GUIDE_URL = "https://github.com/logan775800/crypto-bot/blob/main/docs/guide.md"

TEXT = (
    "📖 *机器人怎么用*\n\n"
    "最快上手：**直接发币名就查价**，比如 `BTC`\n"
    "记不住命令就发 /menu，或 /commands 看全部命令（点了直接执行）\n\n"
    "常用的几个：\n"
    "📅 `/rank 3` 　3日/7日涨跌榜\n"
    "⚖️ `/lsr` 　　多空比极值榜，最被看多/看空各 3 个\n"
    "💣 `/liqmap TRUMP` 　清算地图（模型估算，不是交易所数据）\n"
    "📊 `/pos BTR` 　这波是谁推的：大户加仓，还是空头平仓（轧空）推的\n"
    "🎮 `/vtrade` 　虚拟盘练手，1 万 U 起步（**私聊我**发）\n"
    "🔗 `/oc BANK` 　查链上代币，交易所没上的币也能查\n\n"
    "完整清单看**群置顶**（68 个命令 + 按钮在哪），管理员发 /pinhowto 可刷新。\n\n"
    "群里 @我 或回复我的消息就能直接对话问问题。\n"
    "⚠️ 数据仅供参考，不构成投资建议"
)


def kb(private):
    """群里只放**不暴露个人数据**的入口。

    按钮回调是就地编辑原消息，在群里点「虚拟交易台」等于把自己的持仓
    贴给全群看——所以那个入口只在私聊出现。
    """
    rows = [
        [InlineKeyboardButton("📅 3日涨跌榜", callback_data="dr:w:3:all:all:hot"),
         InlineKeyboardButton("⚖️ 多空比", callback_data="ls:v:binance")],
        [InlineKeyboardButton("💣 清算地图", callback_data="lq:pick:-:-"),
         InlineKeyboardButton("📋 功能菜单", callback_data="menu_main")],
    ]
    if private:
        rows.insert(1, [InlineKeyboardButton("🎮 虚拟交易台", callback_data="vg:home")])
    return InlineKeyboardMarkup(rows)


async def howto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/howto 使用指南（群里发一下，大家都看得到）"""
    private = update.effective_chat.type == "private"
    await safe_reply(update.message, TEXT, reply_markup=kb(private),
                     parse_mode="Markdown", disable_web_page_preview=True)


# ── 置顶消息：让机器人自己维护 ──────────────────────────────────
# 他的原话：「tg群置顶信息一点都不全，不能概括全部命令功能按钮，
# 以及更新后的新命令和功能按钮在哪也不清楚，以后每次更新版本你都要更新这条置顶消息」。
#
# 我发不了 Telegram，靠我每次贴一段文本让他手动改置顶，**必然会有忘掉的那次**——
# 而那次之后置顶就永久过时了。所以改成：**机器人自己发、自己置顶、自己更新**。
# 内容从当前版本的 CHANGELOG 现取，永远不会落后于代码。

PIN_HEAD = "📌 机器人怎么用"


def pinned_text():
    """置顶正文：怎么上手 + 按你想干什么找 + 这一版新增了什么。

    **最后那段是关键**：他抱怨的是"更新后新命令在哪不清楚"。
    版本号和更新条目从 CHANGELOG 现取，不写死。
    """
    from config import VERSION
    from handlers import changelog as C

    new = ""
    items = C.notes_for(VERSION)
    if items:
        lines = "\n".join(f"· {C.brief(it, 70)}" for it in items[:6])
        new = f"\n\n━━━ 这一版（{VERSION}）新增 ━━━\n{lines}\n发 /changelog 看细节"

    return (
        f"{PIN_HEAD}\n\n"
        "行情 + 分析 + 模拟交易 + 告警。不用记命令，绝大多数功能点按钮就行。\n"
        "找不到就发 /commands（全部命令按分类列出，点一下直接执行）。\n\n"

        "━━━ 不知道从哪开始就试这三个 ━━━\n"
        "1️⃣ 直接发币名 —— 发 BTC。回行情卡 + 4小时/日线/周线三张图 + 研判\n"
        "2️⃣ 发 /menu —— 功能菜单，10 个入口全平铺，点按钮出结果\n"
        "3️⃣ 发 /howto —— 随时调出这份指南\n\n"

        "━━━ 💰 查行情 ━━━\n"
        "直接发币名｜/oc BANK 链上币（交易所没上的也能查，中文名也认）\n"
        "/info 币详情　/compare 对比　/fear 恐惧贪婪　/index 大盘　/gas 手续费\n"
        "菜单：📊 行情\n\n"

        "━━━ 📅 看榜单 ━━━\n"
        "/rank 3 三日涨跌榜　/top 24小时榜　/movers 异动快照\n"
        "/upstreak 连涨　/downstreak 连跌（和涨跌榜不是一回事）\n"
        "/lsr 多空比极值　/fex 资金费率极端榜　/pumptop 15m滚动榜\n\n"

        "━━━ 🔍 找机会 ━━━\n"
        "/scan 按能不能下单打分（不是涨幅榜）　/microcap 微市值\n"
        "/breakout 箱体破位　/steady 稳健　/arb 跨所价差　/whale 巨鲸\n"
        "菜单：🔍 机会扫描\n\n"

        "━━━ 📈 看图和分析 ━━━\n"
        "/achart BTC 1h 标注图（结构位、止损带画在图上）\n"
        "/chart K线　/analyze 技术分析　/backtest 回测　/btcregime 大盘环境\n"
        "/liqmap BTC 清算地图（模型估算，不是交易所数据）\n"
        "/pos BTC 持仓结构 —— 大户持仓比/人数多空比/持仓量各挪了多少\n"
        "菜单：📈 分析与图表；或发个币名 → 卡片上「📊 这波是谁推的」\n\n"

        "━━━ 🔔 提醒（你盯某个条件）━━━\n"
        "/alert BTC 70000 到价提醒　/myalert 我的提醒\n"
        "/watchpct BTC 2 涨跌超2%就叫你（报完自动续盯）\n"
        "/cond 条件提醒（价格+指标组合）　/rsialert RSI 提醒\n"
        "菜单：🔔 提醒与订阅 → 价格/条件提醒\n\n"

        "━━━ 🚨 告警订阅（我主动推给你）━━━\n"
        "菜单：🔔 提醒与订阅（两下就到，✅=已订阅）\n"
        "⚡ /watchpump 急涨急跌 —— 15分钟内涨跌到阈值，币安+Bybit 约600个永续\n"
        "📊 /watchcontract 合约异动 —— 24h破台阶(20%起)，自带清算地图+持仓结构\n"
        "🚨 /pump3 极端拉升 —— 15m暴拉且多日已涨，一个月响几次\n"
        "📈 /watchmarket 市场异动 —— 新币上线 + 放量\n"
        "🀄 /newtoken burst 梗爆发 —— 同一个名字 30 分钟内被抄几次（看热度不看池子）\n"
        "💧 链上防跑路 —— /watchpct 合约地址 5，建完自动带 LP 撤出告警\n"
        "📰 /subnews 新闻　/subscribe 早报　/unlock 解锁提醒\n"
        "/follow BTC ETH 只看关注的币　/quiet 23:00 8:00 免打扰\n\n"

        "━━━ 🎮 模拟交易（练手，不是真钱）━━━\n"
        "/vtrade 交易台，1万U起步，开仓五步全按钮\n"
        "合约 /vopen /vclose /vpos /vtpsl 止盈损　/vhistory 胜率\n"
        "现货 /vbuy /vsell /vspot　委托 /vorders /vcancel　重来 /vreset\n\n"

        "━━━ 📅 复盘和风险 ━━━\n"
        "/rstats 30 v 近30天模拟盘成绩单（加 ai 让它挑你的行为漏洞）\n"
        "/weekly 周报　/plan 交易计划　/checklist 开仓自查\n"
        "/risk 风险中心　/cockpit 持仓驾驶舱　/net 成本计算\n\n"

        "━━━ 💬 问它 ━━━\n"
        "群里 @机器人 或回复它的消息就能直接对话｜私聊 /ask 问题\n\n"

        "━━━ ⚙️ 设置 ━━━\n"
        "/source 换默认数据源（一条命令切15个模块）　/venue 换交易所\n"
        "/datacheck 系统体检　/version 当前版本　/changelog 更新记录\n\n"

        "━━━ 新人最容易卡的三件事 ━━━\n"
        "① 账户类功能（模拟盘、持仓、复盘）必须私聊，群里会拒绝\n"
        "　 那是不让你的持仓被全群看见\n"
        "② 找不到某个功能就发 /commands，全部命令按分类列出来\n"
        "③ 不知道命令怎么用，只发命令本身不带参数，它会告诉你用法\n\n"

        "━━━ 三条先记住 ━━━\n"
        "· /vtrade 是模拟盘，不是真钱，随便试\n"
        "· /liqmap 那张清算图是模型估算，没有交易所公布这个\n"
        "· 所有数据仅供参考，不构成投资建议"
        + new)


async def refresh_pin(context, chat_id):
    """把置顶消息刷成最新（没有就发一条并置顶）。返回 (成功, 说明)。

    记住 message_id，以后**编辑同一条**而不是每次发新的——
    每版发一条新置顶会把群刷得没法看，而且旧的还留在那儿继续误导人。
    """
    from storage import data, save_data
    store = data.setdefault("pinned_howto", {})
    key = str(chat_id)
    text = pinned_text()

    mid = store.get(key)
    if mid:
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id, message_id=int(mid), text=text,
                disable_web_page_preview=True)
            return True, f"已更新置顶（消息 {mid}）"
        except Exception as e:
            # 「not modified」说明内容没变，也算成功，别当失败重发一条
            if "not modified" in str(e).lower():
                return True, "置顶内容已经是最新的"
            log.info(f"[pin] 编辑旧置顶失败，改发新的: {e}")

    # 先把消息发出去、记住 id，再单独去钉。
    # 原来发和钉在同一个 try 里：钉失败就整件事算失败，连内容都没留下，
    # 而其实内容已经发出去了——他看到的是"❌ 置顶失败"，却不知道消息就在上面。
    try:
        m = await context.bot.send_message(chat_id, text,
                                           disable_web_page_preview=True)
    except Exception as e:
        return False, f"消息都没发出去：{e}"

    store[key] = m.message_id
    save_data()
    try:
        await context.bot.pin_chat_message(chat_id, m.message_id,
                                           disable_notification=True)
        return True, "已发新置顶并钉住"
    except Exception as e:
        # **这条提示以前指错了地方。** 他照着去「用户权限」里勾了「置顶消息」，
        # 结果还是失败——那是**群成员的默认权限**，对机器人无效：
        # Telegram 的 Bot API 要求置顶必须是**管理员**。
        # 提示指错方向比不提示更浪费时间，所以这里把两条路径都写出来。
        return False, (
            f"内容已经发出来了（就在上面这条），但钉不上去：{e}\n\n"
            "机器人必须是**管理员**才能置顶，注意别走错菜单：\n"
            "❌ 群资料 →「用户权限」里勾「置顶消息」—— 那是普通成员的权限，对机器人无效\n"
            "✅ 群资料 →「管理员」→ 添加管理员 → 选这个机器人 → 打开「置顶消息」\n\n"
            "不想给管理员也行：**长按上面那条消息手动置顶**，\n"
            "以后每次更新我照样会把那条编辑成最新的（编辑不需要管理员）。")


async def pin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pinhowto —— 管理员在群里发一下，就把置顶刷成当前版本。"""
    from config import is_admin
    if not is_admin(update.effective_user.id):
        return
    if update.effective_chat.type == "private":
        await safe_reply(update.message, "这条要在**群里**发——置顶是群的功能。",
                         parse_mode="Markdown")
        return
    ok, msg = await refresh_pin(context, update.effective_chat.id)
    await safe_reply(update.message, ("✅ " if ok else "❌ ") + msg)


async def auto_refresh_pins(context):
    """版本变了就自动刷新所有已置顶过的群——这正是他要的"每次更新都更新置顶"。

    只刷**已经有过置顶**的群（他手动 /pinhowto 建立过的），
    不会主动去别的群刷屏。
    """
    from storage import data, save_data
    from config import VERSION
    store = data.get("pinned_howto") or {}
    if not store or data.get("pinned_version") == VERSION:
        return
    for cid in list(store):
        ok, msg = await refresh_pin(context, int(cid))
        log.info(f"[pin] {cid}: {msg}")
    data["pinned_version"] = VERSION
    save_data()
