import logging
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes
from api import get_price, get_fear_greed, get_gas_price, get_market_data, get_top_movers
from config import COIN_IDS
from handlers.util import sanitize_link_text, safe_edit, escape_md
from handlers.steady import DEFAULT_DAYS as STEADY_DEFAULT_DAYS

log = logging.getLogger(__name__)

POPULAR = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX", "DOT"]

# 欢迎语（/start 与群欢迎共用）
WELCOME_TEXT = (
    "👋 *欢迎使用加密货币助手* 🤖\n\n"
    "我能帮你：\n"
    "📊 查币价、市场看板、涨跌榜\n"
    "📈 技术分析 + AI 解读\n"
    "🔔 到价自动提醒\n"
    "🛠 多所比价、市场情绪、Gas、巨鲸\n"
    "💼 记录持仓盈亏（私聊）\n\n"
    "💡 *最快上手*：直接发币名即可查价，例如 `BTC`、`pepe`\n"
    "或点下方按钮 👇"
)

# ============ 底部常驻键盘 ============
def persistent_kb():
    """常驻在输入框下方的快捷键，菜单滚走了也能一键唤起。"""
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📋 菜单"), KeyboardButton("📊 看板")],
            [KeyboardButton("💰 查价"), KeyboardButton("❓ 帮助")],
        ],
        resize_keyboard=True,
    )

# ============ 主菜单 ============
def main_menu_kb():
    """首页：按「我要干什么」分组，一屏看完。

    仍然**不做「更多」收纳**——v1.8.0 试过按频率精简 + 「更多」，实际用下来
    多一层点击比多几个按钮更烦，v1.10.1 已经恢复过一次全平铺。

    v1.32.0 从 23 个入口降到 10 个，靠的是**去重和合并同类，不是把功能藏起来**：
      • 四个交易所专区（OKX/币安/Bybit/Gate）本来就是同一批功能的四份拷贝，
        合成一个入口、进去选所。大多数时候人关心的是"某个币"，不是"某家所"。
      • 三套分类逻辑（按数据类型 / 按交易所 / 按场景）混在一层，是"又杂又乱"
        的真正来源。现在统一按**场景**分，每个入口回答一个"我要干什么"。
      • 名字打架的合并：市场看板+行情查询、实用工具+交易工具、
        订阅推送+价格预警、技术分析+策略回测。
    合并后的面板里，原来的入口一个不少，回调也全部沿用——只是不再占首页的位置。
    """
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 行情", callback_data="cat_market"),
         InlineKeyboardButton("🔍 机会扫描", callback_data="cat_scan")],
        [InlineKeyboardButton("📈 分析与图表", callback_data="cat_analysis"),
         InlineKeyboardButton("🔗 链上代币", callback_data="cat_onchain")],
        [InlineKeyboardButton("🎮 虚拟交易台", callback_data="vg:home"),
         InlineKeyboardButton("🧮 交易工具", callback_data="cat_calc")],
        [InlineKeyboardButton("🛡 风险中心", callback_data="cat_risk"),
         InlineKeyboardButton("📅 复盘中心", callback_data="cat_review")],
        [InlineKeyboardButton("🏦 交易所专区", callback_data="cat_venues"),
         InlineKeyboardButton("🔔 提醒与订阅", callback_data="cat_notify")],
        [InlineKeyboardButton("💬 AI 助手（问我任何问题）", callback_data="ask_start")],
        [InlineKeyboardButton("⌨️ 全部命令", callback_data="cmd:home"),
         InlineKeyboardButton("🩺 体检", callback_data="do:datacheck"),
         InlineKeyboardButton("❓ 帮助", callback_data="cat_help")],
    ])


def _back():
    return [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")]


def followup_kb(symbol=None, back=None):
    """分析结果底下的**闭环按钮**。

    以前分析完是死胡同：你得自己记住币名和价位，再去打 /net、/risk、/plan。
    中间这段手工搬运正是「看完就算了」的原因——闭环不是锦上添花，
    它决定了这个分析会不会真的被用上。
    带上 symbol 后续步骤就不用再问一遍币名。
    """
    s = (symbol or "").upper().replace("USDT", "") or "-"
    rows = [
        [InlineKeyboardButton("📋 生成计划", callback_data=f"fu:plan:{s}"),
         InlineKeyboardButton("💰 净盈亏比", callback_data=f"fu:net:{s}")],
        [InlineKeyboardButton("🧮 算仓位", callback_data=f"fu:risk:{s}"),
         InlineKeyboardButton("🔔 设预警", callback_data=f"fu:alert:{s}")],
        [InlineKeyboardButton("🎮 模拟开仓", callback_data=f"fu:vopen:{s}"),
         InlineKeyboardButton("🩺 数据体检", callback_data=f"fu:check:{s}")],
        # 清算地图接进闭环：分析完最该回答的下一个问题就是"止损放哪儿"，
        # 而那张图直接告诉你哪些价位是插针磁吸区。少这个按钮就还得自己去打 /liqmap
        # 「清算地图」回答止损放哪儿，「谁推的」回答这波还有没有人接力——
        # 同一个币的两个问题，并排放，不该有一个要去翻命令
        [InlineKeyboardButton("💣 清算地图", callback_data=f"lq:w:{s}:7日"),
         InlineKeyboardButton("📊 这波是谁推的", callback_data=f"pf:r:{s}")],
        # 闭环的最后一环：前面几步都在"算"，真要下单还得自己去翻 /trade。
        # 少这一个按钮，分析和实盘之间就还隔着一次手工搬运。
        [InlineKeyboardButton("🎛 进交易台", callback_data="tpanel")],
    ]
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data=back or "menu_main")])
    return InlineKeyboardMarkup(rows)


# 闭环按钮 → (提示文案模板, 引导式命令名)。模板里的 {s} 会替换成币名，
# 这样用户只需要补价位，不用重复输入币种。
FOLLOWUP = {
    "plan": ("📋 *生成 {s} 交易计划*　发方向：`{s} long` 或 `{s} short`", "plan"),
    "net": ("💰 *{s} 净盈亏比*　发：`{s} long 入场 止损 止盈 名义USDT`\n"
            "　例 `{s} long 0.081 0.0795 0.086 2000`", "net"),
    "risk": ("🧮 *{s} 反推仓位*　发：`{s} 入场 止损 [风险%]`\n"
             "　例 `{s} 0.081 0.0828 0.5%`", "risk"),
    "alert": ("🔔 *{s} 持续波动监控*　发：`{s} 幅度%`\n"
              "　例 `{s} 2` —— 涨跌超 ±2% 就提醒，报后自动续盯", "watchpct"),
    "vopen": ("🎮 *{s} 模拟开仓*　发：`{s} long 保证金 杠杆`\n"
              "　例 `{s} long 1000 10`（含真实滑点与费率）", "vopen"),
    "check": ("🩺 直接体检 {s}", "datacheck"),
}


# 新功能一律要有按钮入口 —— 只能靠打命令的功能等于没做。
# 需要参数的（净盈亏比/回测）走「引导式」：点按钮 → 提示怎么填 → 用户发一行参数。
CATS = {
    "cat_scan": (
        "🔍 *机会扫描*\n\n"
        "按**可交易性**排序，不是按涨幅——涨幅第一名往往是最不该碰的那个。\n"
        "四维打分：趋势 / 流动性 / 拥挤 / 执行，任一项不及格直接否决。",
        [[InlineKeyboardButton("🔍 全市场扫描（约20秒）", callback_data="do:scan")],
         [InlineKeyboardButton("🌱 缓步增长（可选天数）",
                               callback_data=f"stdy:{STEADY_DEFAULT_DAYS}:0")],
         [InlineKeyboardButton("📊 合约涨跌榜", callback_data="ctr:top"),
          InlineKeyboardButton("⚡ 15m急涨急跌", callback_data="pump:top")],
         [InlineKeyboardButton("🚀 5分钟破位（箱体+均线顺势）",
                               callback_data="bo:scan")],
         [InlineKeyboardButton("💎 微市值（<300万，能下单的）",
                               callback_data="mc:300")],
         [InlineKeyboardButton("🔗 链上代币专区", callback_data="cat_onchain")]]),
    "cat_calc": (
        "🧮 *交易工具*\n\n"
        "下单前的三件事：这是哪个合约、扣完成本还剩多少、这个止损能开多大。",
        [[InlineKeyboardButton("💰 净盈亏比", callback_data="ask:net"),
          InlineKeyboardButton("🔎 合约身份", callback_data="ask:sym")],
         [InlineKeyboardButton("🧪 规则回测", callback_data="ask:backtest"),
          InlineKeyboardButton("🩺 数据体检", callback_data="ask:datacheck")],
         [InlineKeyboardButton("📋 生成交易计划", callback_data="ask:plan"),
          InlineKeyboardButton("📐 标注图表", callback_data="ask:achart")],
         # 合并进来的：原首页「实用工具」（Gas/巨鲸/套利/解锁那些）
         [InlineKeyboardButton("🛠 实用工具（Gas/巨鲸/套利/解锁）",
                               callback_data="cat_tools")],
         [InlineKeyboardButton("🎮 虚拟盘怎么用", callback_data="cat_vtrade")]]),
    "cat_risk": (
        "🛡 *风险中心*\n\n"
        "参数会**真的挡住**仓位计算，不是印在文档里让你自己遵守。",
        [[InlineKeyboardButton("⚙️ 我的风控参数", callback_data="do:riskprofile")],
         [InlineKeyboardButton("🧮 反推仓位", callback_data="ask:risk"),
          InlineKeyboardButton("🛡 风险守护", callback_data="rgpanel")],
         [InlineKeyboardButton("🔔 事件预警", callback_data="ask:events"),
          InlineKeyboardButton("📊 合约异动", callback_data="ctr:panel")],
         [InlineKeyboardButton("✅ 开仓检查清单", callback_data="do:checklist")],
         # 合并进来的：原首页「我的持仓」——持仓本来就是风险的主体
         [InlineKeyboardButton("💼 我的持仓", callback_data="cat_holding")]]),
    "cat_review": (
        "📅 *复盘中心*\n\n"
        "周报看的是**行为漂移**而不是单周盈亏——行为你能控制，盈亏你不能。",
        [[InlineKeyboardButton("📅 本周周报", callback_data="do:weekly"),
          InlineKeyboardButton("📊 成绩单30天", callback_data="do:rstats")],
         [InlineKeyboardButton("🚗 持仓驾驶舱", callback_data="do:cockpit"),
          InlineKeyboardButton("📋 我的计划", callback_data="do:plans")]]),
}

# 引导式输入：callback → (提示文案, 对应命令名)
ASK = {
    "net": ("💰 *净盈亏比*　发一行参数：\n`BANK long 0.081 0.0795 0.086 2000`\n"
            "　币 方向 入场 止损 止盈 名义USDT `[持仓小时]`", "net"),
    "sym": ("🔎 *合约身份*　发币名，如 `LAB`", "sym"),
    "backtest": ("🧪 *规则回测*　发参数：`BTC 1h trend`\n"
                 "　周期 5m/15m/1h/4h　规则 trend/pullback/breakout", "backtest"),
    "datacheck": ("🩺 *数据体检*　发币名，如 `BANK`", "datacheck"),
    "plan": ("📋 *交易计划*　发「币 方向」，如 `BANK short`", "plan"),
    "achart": ("📐 *标注图表*　发「币 周期」，如 `BTC 1h`", "achart"),
    "risk": ("🧮 *反推仓位*　发「入场 止损 [风险%]」，如 `0.081 0.0828 0.5%`\n"
             "　带币名更好：`BANK 0.081 0.0828 0.5%`", "risk"),
    "events": ("🔔 *事件预警*　发要盯的币，如 `BTC ETH`\n"
               "　盯 OI跳升/价OI结构切换/费率跨拥挤区/盘口翻转", "events"),
}

class _FakeCtx:
    """把「按钮 + 用户发的一行参数」伪装成一次命令调用。

    这样按钮入口和命令入口走的是**同一份**处理逻辑，不会出现
    「命令能用、按钮版行为不一样」这种要分别维护两套的情况。
    """
    def __init__(self, args, real):
        self.args = args
        self.bot = real.bot
        self.user_data = real.user_data
        self.chat_data = real.chat_data
        self.application = getattr(real, "application", None)


def _fake_update(query):
    """用 query.message 冒充 update.message，让命令处理函数原样可用。"""
    return type("U", (), {
        "message": query.message,
        "effective_chat": query.message.chat,
        "effective_user": query.from_user,
    })()


async def _run_direct(query, context, name):
    """无参数功能：点按钮直接跑。用 query.message 冒充 update.message。"""
    handlers = {
        "scan": ("handlers.scan", "scan_cmd", "🔍 扫描中…"),
        "steady": ("handlers.steady", "steady_cmd", "🌱 扫描中…"),
        "riskprofile": ("handlers.riskprofile", "risk_profile_cmd", None),
        "weekly": ("handlers.weekly", "weekly_cmd", "📅 生成周报…"),
        "rstats": ("handlers.rstats", "rstats", "📊 拉取成绩单…"),
        "cockpit": ("handlers.cockpit", "cockpit", "🚗 读取持仓…"),
        "plans": ("handlers.plan", "plans_cmd", None),
        "checklist": ("handlers.checklist", "checklist", None),
        "datacheck": ("handlers.datameta", "datacheck", "🩺 体检中…"),
    }
    if name not in handlers:
        await query.answer("未知功能")
        return
    mod_name, fn_name, tip = handlers[name]
    await query.answer(tip or "")
    import importlib
    try:
        fn = getattr(importlib.import_module(mod_name), fn_name)
    except (ImportError, AttributeError) as e:
        logging.error(f"菜单直跑 {name} 找不到处理函数: {e}")
        await query.message.reply_text(f"这个功能暂时用不了：{str(e)[:60]}")
        return
    args = ["30"] if name == "rstats" else []
    try:
        await fn(_fake_update(query), _FakeCtx(args, context))
    except Exception as e:
        logging.error(f"菜单直跑 {name} 出错: {e}")
        await query.message.reply_text(f"执行失败：{str(e)[:80]}")


async def run_awaited_cmd(update, context, text):
    """用户在引导式输入后发来的那行参数 → 转成对应命令执行。

    返回 True 表示已处理（调用方要停止后续的「当成币名查价」）。
    """
    cmd = context.user_data.pop("await_cmd", None)
    if not cmd:
        return False
    table = {
        "net": ("handlers.econ", "net_cmd"),
        "sym": ("handlers.symbols", "sym_cmd"),
        "backtest": ("handlers.backtest", "backtest_cmd"),
        "datacheck": ("handlers.datameta", "datacheck"),
        "plan": ("handlers.plan", "plan_cmd"),
        "achart": ("handlers.annotchart", "achart"),
        "events": ("handlers.events", "events_cmd"),
        "risk": ("handlers.riskguard", "risk"),
    }
    if cmd not in table:
        return False
    import importlib
    mod_name, fn_name = table[cmd]
    try:
        fn = getattr(importlib.import_module(mod_name), fn_name)
    except (ImportError, AttributeError) as e:
        logging.error(f"引导式命令 {cmd} 找不到处理函数: {e}")
        return False
    try:
        await fn(update, _FakeCtx((text or "").split(), context))
    except Exception as e:
        logging.error(f"引导式命令 {cmd} 执行出错: {e}")
        await update.message.reply_text(f"执行失败：{str(e)[:80]}")
    return True


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # 打开菜单即视为放弃未完成的预警设置，避免残留状态误把后续输入当价格/币名
    context.user_data.pop("await_alert", None)
    context.user_data.pop("await_alert_coin", None)
    context.user_data.pop("await_watchpct", None)
    context.user_data.pop("await_track_addr", None)
    context.user_data.pop("await_cmd", None)    # 放弃未完成的引导式输入
    context.user_data.pop("ai_session", None)   # 打开菜单即退出 AI 问答会话
    await update.message.reply_text(
        "🤖 *加密货币助手*\n\n点击下方分类，按钮直接出结果，无需记命令👇",
        reply_markup=main_menu_kb(), parse_mode="Markdown"
    )

# 币种按钮（带功能前缀，点了直接执行该功能）
def coin_grid(action, back="menu_main"):
    rows = []
    for i in range(0, len(POPULAR), 5):
        rows.append([InlineKeyboardButton(c, callback_data=f"{action}:{c}") for c in POPULAR[i:i+5]])
    # 带上来源 action，"查其他币"才能知道点完币名后要接着做什么
    rows.append([InlineKeyboardButton("🔍 查其他币", callback_data=f"askcoin:{action}")])
    rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data=back)])
    return InlineKeyboardMarkup(rows)

# 预警方向选择键盘（选完币后用；quickprice 也复用）
def alert_direction_kb(symbol):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 涨破提醒(一次)", callback_data=f"alertset:{symbol}:above"),
         InlineKeyboardButton("📉 跌破提醒(一次)", callback_data=f"alertset:{symbol}:below")],
        [InlineKeyboardButton("⚡ 涨跌超±5% 就提醒(一次)", callback_data=f"alertpctset:{symbol}")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="cat_alert"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])

def back_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")]])

def back_to(cat):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ 返回", callback_data=cat),
                                  InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")]])

def _alert_desc(a):
    """把一条预警渲染成一行说明。"""
    t = a.get("type")
    if t == "pct":
        return f"{a['symbol']} 涨跌±{a['pct']:g}% (基准 ${a['base_price']:,.2f}) [一次]"
    arrow = "涨破" if a.get("direction") == "above" else "跌破"
    tag = "[持续]" if t == "watch" else "[一次]"
    return f"{a['symbol']} {arrow} ${a['target']:,.2f} {tag}"

async def render_my_alerts(query):
    """列出当前会话的所有预警，每条带删除按钮。查看和删除后都用它刷新。"""
    from storage import data as _ad
    chat_id = query.message.chat_id
    mine = [(gi, a) for gi, a in enumerate(_ad.get("alerts", [])) if a.get("chat_id") == chat_id]
    if not mine:
        await safe_edit(query, 
            "📋 *我的价格预警*\n\n你还没有设置任何预警。\n返回上一步选币即可添加👇",
            reply_markup=back_to("cat_alert"), parse_mode="Markdown")
        return
    lines = ["📋 *我的价格预警*\n每条都能单独换数据源：\n"]
    rows = []
    from handlers.source import change_btn
    for n, (gi, a) in enumerate(mine, 1):
        # 序号有两套：gi 是全局下标（删除用），n-1 是本会话内的序号（换源用）
        lines.append(f"{n}. {_alert_desc(a)}　[{a.get('src') or '自动'}]")
        rows.append([InlineKeyboardButton(f"❌ 删除 {n}. {a['symbol']}",
                                          callback_data=f"delalert:{gi}"),
                     change_btn(f"al|{n - 1}")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data="my_alerts"),
                 InlineKeyboardButton("⬅️ 返回", callback_data="cat_alert")])
    await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


async def render_my_watchpct(query):
    """列出当前会话的持续波动监控，每条带取消按钮。"""
    from storage import data as _ad
    from handlers.watchpct import fmt
    chat_id = query.message.chat_id
    mine = [w for w in _ad.get("watchpct", []) if w["chat_id"] == chat_id]
    if not mine:
        await safe_edit(query, 
            "👁 *我的波动监控*\n\n还没有。点【👁 持续波动监控】添加👇",
            reply_markup=back_to("cat_alert"), parse_mode="Markdown")
        return
    lines = ["👁 *我的波动监控*\n每条都能单独换数据源：\n"]
    rows = []
    from handlers.source import change_btn
    for n, w in enumerate(mine, 1):
        lines.append(f"{n}. {w['symbol']}  ±{w['pct']}%  基准 ${fmt(w['base'])}（{w.get('src','?')}）")
        rows.append([InlineKeyboardButton(f"❌ 取消 {w['symbol']}",
                                          callback_data=f"delwatchpct:{w['symbol']}"),
                     change_btn(f"wp|{w['symbol']}")])
    rows.append([InlineKeyboardButton("🔄 刷新", callback_data="my_watchpct"),
                 InlineKeyboardButton("⬅️ 返回", callback_data="cat_alert")])
    await safe_edit(query, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")


def _short_addr(a):
    return a[:6] + "..." + a[-4:] if a and len(a) > 12 else a

def gas_panel(chat_id):
    from storage import data as _d
    cur = _d.get("gas_subs", {}).get(str(chat_id))
    status = f"✅ 已开启：ETH gas 跌破 {cur['threshold']:g} gwei 提醒" if cur else "⬜ 未开启"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("≤10", callback_data="gasset:10"),
         InlineKeyboardButton("≤15", callback_data="gasset:15"),
         InlineKeyboardButton("≤20", callback_data="gasset:20"),
         InlineKeyboardButton("≤30", callback_data="gasset:30")],
        [InlineKeyboardButton("❌ 关闭提醒", callback_data="gasset:off")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="cat_tools"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])
    text = f"⛽ *Gas 提醒*\n{status}\n\n点阈值设置(ETH主网 gas 跌破即通知)；自定义用 `/gasalert 12`"
    return text, kb

def arb_panel(chat_id):
    from storage import data as _d
    cur = _d.get("arb_subs", {}).get(str(chat_id))
    status = f"✅ 已开启：净价差 ≥ {cur['threshold']:g}% 告警" if cur else "⬜ 未开启"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("≥0.5%", callback_data="arbset:0.5"),
         InlineKeyboardButton("≥0.8%", callback_data="arbset:0.8"),
         InlineKeyboardButton("≥1.5%", callback_data="arbset:1.5"),
         InlineKeyboardButton("≥3%", callback_data="arbset:3")],
        [InlineKeyboardButton("❌ 关闭监控", callback_data="arbset:off")],
        [InlineKeyboardButton("⬅️ 返回", callback_data="cat_tools"),
         InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")],
    ])
    text = (f"💱 *套利监控*\n{status}\n\n点阈值(跨所净价差达标即告警，每5分钟扫)；"
            f"自定义 `/arbwatch 1.2`\n⚠️ 净价差已扣约0.2%手续费，未含提币费/滑点")
    return text, kb

def _fmt_usd(u):
    if u <= 0:
        return "全部(不过滤)"
    if u >= 10000:
        return f"${u/10000:g}万"
    return f"${u:,.0f}"

def track_panel(chat_id):
    from storage import data as _d
    d = _d.get("whale_addr", {}).get(str(chat_id), {})
    min_usd = _d.get("whale_min", {}).get(str(chat_id), 10000)
    rows = []
    if d:
        lines = [f"🐋 *地址追踪*  (只推 ≥ {_fmt_usd(min_usd)})\n已关注(点❌取消)："]
        for addr, cfg in d.items():
            lbl = cfg.get("label") or _short_addr(addr)
            lines.append(f"• {lbl}")
            rows.append([InlineKeyboardButton(f"❌ {lbl}", callback_data=f"trackdel:{addr}")])
        text = "\n".join(lines)
    else:
        text = f"🐋 *地址追踪*  (只推 ≥ {_fmt_usd(min_usd)})\n还没关注任何地址。\n关注后该地址有大额 ETH/稳定币转账会通知你。"
    rows.append([InlineKeyboardButton("➕ 添加地址", callback_data="trackadd")])
    rows.append([InlineKeyboardButton("≥$1万", callback_data="trackmin:10000"),
                 InlineKeyboardButton("≥$5万", callback_data="trackmin:50000"),
                 InlineKeyboardButton("≥$10万", callback_data="trackmin:100000"),
                 InlineKeyboardButton("≥$50万", callback_data="trackmin:500000")],
        )
    rows.append([InlineKeyboardButton("≥$100万", callback_data="trackmin:1000000"),
                 InlineKeyboardButton("≥$150万", callback_data="trackmin:1500000"),
                 InlineKeyboardButton("≥$300万", callback_data="trackmin:3000000")],
        )
    rows.append([InlineKeyboardButton("全部(不过滤)", callback_data="trackmin:0")])
    rows.append([InlineKeyboardButton("⬅️ 返回", callback_data="cat_tools"),
                 InlineKeyboardButton("🏠 主菜单", callback_data="menu_main")])
    return text, InlineKeyboardMarkup(rows)

# ============ 按钮处理 ============
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """所有按钮回调的总入口。

    外面这层 try 只拦一件事：「Message is not modified」。
    面板普遍带「🔄 刷新」，数据没变时重渲染出的内容和原消息一字不差，Telegram
    就报 BadRequest。它冒到全局错误处理器，会变成一条带 traceback 的「机器人异常」
    推给管理员——用户那边什么事都没有，管理员却被一条正常操作刷屏。

    刷新出一样的内容本来就不是错误，是**无事发生**。这里咽掉它。

    「Query is too old」同理：子模块（onchain 等）会再应答一次同一个 query，
    机器人忙的时候这次应答也会过期。活已经干完了，没必要为一个消不掉的转圈
    推一条 traceback 给管理员。

    其余异常照旧往上抛，别把真问题一起藏了。
    """
    try:
        await _dispatch(update, context)
    except BadRequest as e:
        msg = str(e).lower()
        if "not modified" in msg:
            log.debug(f"按钮重渲染内容未变，忽略：{update.callback_query.data}")
        elif "query is too old" in msg or "query id is invalid" in msg:
            log.warning(f"回调应答过期，忽略：{update.callback_query.data}")
        else:
            raise


NOTIFY_TEXT = (
    "🔔 *提醒与订阅*\n\n"
    "**提醒**是你盯某个条件（到价、波动、指标）；\n"
    "**订阅**是我定期推给你（早报、新闻、异动）。\n"
    "下面几个是最常用的，✅已订阅 ⬜未订阅："
)


def notify_kb(chat_id):
    """提醒与订阅面板的键盘。**只此一份**（同 subs_kb 的理由）。

    这些告警原来分别埋在「价格/条件提醒」和「定期订阅推送」里面，
    从 /menu 数下去要点三下——规矩是超过两层就当成 bug 去修入口。
    原来的位置一个都没删，这里只是把最常用的提上来。
    """
    from storage import data as _sd
    from handlers import breakout as _bo, newtoken as _nt, liqflip as _lf

    pp = "✅" if str(chat_id) in (_sd.get("pump_watch") or {}) else "⬜"
    _cw = _sd.get("contract_watch") or []
    cp = "✅" if (chat_id in _cw or str(chat_id) in [str(x) for x in _cw]) else "⬜"
    p3 = "✅" if str(chat_id) in (_sd.get("pump3") or {}) else "⬜"
    bk_on = _bo.is_on(chat_id)
    bk = "✅" if bk_on else "⬜"
    nc = "✅" if _nt.is_on(chat_id) else "⬜"
    bs = "✅" if _nt.burst_enabled(chat_id) else "⬜"
    lf = "✅" if _lf.is_on(chat_id) else "⬜"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{pp} ⚡急涨急跌(15m,可调阈值)", callback_data="pump:panel")],
        [InlineKeyboardButton(f"{cp} 📊合约异动(24h±20%起,带清算地图)",
                              callback_data="ctr:panel")],
        [InlineKeyboardButton(f"{p3} 🚨极端拉升(15m暴拉+多日已涨)", callback_data="p3:panel")],
        [InlineKeyboardButton(f"{bk} 🚀箱体破位(5m,均线顺势)",
                              callback_data=f"bo:{'off' if bk_on else 'on'}")],
        [InlineKeyboardButton(f"{nc} 🌱链上新币(筛过安全检查)", callback_data="nt:toggle")],
        # 梗爆发和上面那条判据完全不同：它看的是「同一个名字 30 分钟内被抄几次」，
        # 不是「哪个池子够大」。两个是正交的信号，所以是两个开关不是一个档位。
        [InlineKeyboardButton(f"{bs} 🀄梗爆发(同名被抄的速度)", callback_data="nt:burst")],
        # 判据是回测出来的（26币7天5万个窗口），不是"看着像"——
        # 所以它配得上一个一级开关，而不是塞进某个面板的第三层
        [InlineKeyboardButton(f"{lf} 🩸爆仓一边倒(摸顶抄底)", callback_data="lf:panel")],
        [InlineKeyboardButton("🔔 价格/条件提醒", callback_data="cat_alert")],
        [InlineKeyboardButton("📬 定期订阅推送", callback_data="cat_subs")],
        _back()])


def subs_kb(chat_id):
    """订阅面板的键盘。**只此一份。**

    以前 `cat_subs` 和 `tog_*` 各抄了一份一模一样的键盘：改了 A 忘了 B，
    点一下总开关，新加的子开关就会从屏幕上消失——而且看起来像功能没做。
    这类"同一段 UI 两处维护"的地方迟早分叉，抽出来是唯一的解。
    """
    from storage import data as _sd
    from handlers import market_alert as _ma

    def st(key):
        v = _sd.get(key, [])
        return "✅" if (chat_id in v or str(chat_id) in v) else "⬜"

    def mk(kind):
        return "✅" if _ma.kind_on(chat_id, kind) else "⬜"

    pp = "✅" if str(chat_id) in (_sd.get("pump_watch") or {}) else "⬜"
    _cw = _sd.get("contract_watch", [])
    cp = "✅" if (chat_id in _cw or str(chat_id) in [str(x) for x in _cw]) else "⬜"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{pp} ⚡急涨急跌(15m,可调阈值)", callback_data="pump:panel")],
        [InlineKeyboardButton(f"{cp} 📊合约异动(24h±20%起,可调档)", callback_data="ctr:panel")],
        [InlineKeyboardButton(f"{st('market_watch')} 市场异动告警（总开关）",
                              callback_data="tog_market")],
        # 「新币上线」和「放量异动」以前捆在一个订阅里，只想要放量的人
        # 被迫连新币一起收。拆成两个子开关，总开关关着时它们不生效。
        [InlineKeyboardButton(f"{mk('newcoin')} 　└ 🆕新币上线", callback_data="mk:newcoin"),
         InlineKeyboardButton(f"{mk('surge')} 　└ 📊放量异动", callback_data="mk:surge")],
        [InlineKeyboardButton(f"{st('news_subs')} 新闻推送", callback_data="tog_news")],
        [InlineKeyboardButton(f"{st('unlock_subs')} 解锁提醒", callback_data="tog_unlock")],
        [InlineKeyboardButton(f"{st('summary_subs')} 每日总结", callback_data="tog_summary")],
        [InlineKeyboardButton(f"{st('broadcast_chats')} 每日行情播报",
                              callback_data="tog_broadcast")],
        [InlineKeyboardButton(f"{st('analysis_subs')} 每日技术分析",
                              callback_data="tog_analysis")],
        [InlineKeyboardButton("⚙️ 我的个性化设置", callback_data="my_settings")],
        [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
    ])


async def _dispatch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    # 回调 query 的有效期很短。PTB 默认**串行**处理更新，前一个慢操作（链上搜索、
    # AI 问答、全市场扫描）没跑完，后面的点击就在队列里排着；轮到应答时 query 已过期，
    # Telegram 回 BadRequest: Query is too old。
    # 以前这一行会把整个 dispatch 打断：用户那一下点击**什么都没发生**（按钮像坏了），
    # 管理员还收到一条带 traceback 的「机器人异常」。
    # 应答只是消掉按钮上的转圈，失败不影响后面真正干活——咽掉它，只记日志。
    try:
        await query.answer()
    except BadRequest as e:
        log.warning(f"回调应答过期（点击时机器人正忙，操作照常执行）：{query.data}｜{e}")
    d = query.data

    # ---- 主菜单 ----
    if d == "menu_main":
        await safe_edit(query, 
            "🤖 *加密货币助手*\n\n点击下方分类，按钮直接出结果，无需记命令👇",
            reply_markup=main_menu_kb(), parse_mode="Markdown")

    # ---- 急涨急跌面板（订阅/调阈值/看榜，全按钮）----
    elif d.startswith("pump:"):
        from handlers import pumpalert
        await pumpalert.on_button(query, context)

    # ---- 合约异动告警面板（订阅/最低档/看榜，全按钮）----
    elif d.startswith("ctr:"):
        from handlers import contract_alert
        await contract_alert.on_button(query, context)

    # ---- 事件驱动预警面板（OI跳升/结构切换/费率跨阈/盘口翻转）----
    elif d.startswith("ev:"):
        from handlers import events as _events
        await _events.on_button(query, context)

    # ---- 5分钟破位（箱体 + 均线顺势）----
    elif d.startswith("bo:"):
        from handlers import breakout as _bo
        await _bo.on_button(query, context)

    # ---- 链上新币上线告警 ----
    elif d == "nt:toggle":
        from handlers import newtoken as _nt
        cid = query.message.chat_id
        now_on = _nt.toggle(cid, not _nt.is_on(cid))
        ml, mt = _nt.thresholds()
        if now_on:
            tip = (f"🌱 已订阅链上新币告警\n"
                   f"门槛：流动性≥${ml:,}、1h成交≥{mt}笔\n"
                   f"⚠️ 光 BSC 一小时就有 60 个新池，门槛低了会刷屏")
        else:
            tip = "🔕 已关闭链上新币告警"
        try:
            await query.answer(tip, show_alert=True)
        except Exception:
            pass
        await safe_edit(query, NOTIFY_TEXT, reply_markup=notify_kb(cid),
                        parse_mode="Markdown")

    elif d.startswith("lf:"):
        # 爆仓一边倒：lf:panel 设置面板 / lf:toggle 开关 / lf:lv:档 / lf:test 自检
        from handlers import liqflip as _lf
        if d == "lf:panel":
            await safe_edit(query, _lf.panel_text(query.message.chat_id),
                            parse_mode="Markdown",
                            reply_markup=_lf.panel_kb(query.message.chat_id))
        else:
            await _lf.on_button(query, context)

    elif d == "nt:burst":
        from handlers import newtoken as _nt
        cid = query.message.chat_id
        on = _nt.toggle_burst(cid, not _nt.burst_enabled(cid))
        lv, need = _nt.burst_level()
        if on:
            tip = (f"🀄 已开启梗爆发（{lv}档）\n"
                   f"判据：30 分钟内同名新池 ≥{need} 个"
                   f"（英文名 ≥{need * _nt.BURST_EN_FACTOR}）\n"
                   f"看的是「抄的速度」不是「池子大小」\n"
                   f"每小时最多 {_nt.BURST_PER_HOUR} 条\n"
                   f"现在窗口里有什么：/newtoken burst now")
        else:
            tip = "🔕 已关闭梗爆发告警"
        try:
            await query.answer(tip, show_alert=True)
        except Exception:
            pass
        await safe_edit(query, NOTIFY_TEXT, reply_markup=notify_kb(cid),
                        parse_mode="Markdown")

    # ---- 市场异动的两个子类分别开关（新币 / 放量）----
    elif d.startswith("mk:"):
        from handlers import market_alert as _ma
        kind = d.split(":", 1)[1]
        if kind in _ma.KINDS:
            cid = query.message.chat_id
            now_on = _ma.set_kind(cid, kind, not _ma.kind_on(cid, kind))
            # 总开关关着时子开关不生效，这一点必须说——否则他打开了却收不到，
            # 又会以为坏了
            from storage import data as _sd2
            master = cid in (_sd2.get("market_watch") or []) or \
                str(cid) in [str(x) for x in (_sd2.get("market_watch") or [])]
            tip = f"{_ma.KINDS[kind]} {'已开启' if now_on else '已关闭'}"
            if now_on and not master:
                tip += "\n⚠️ 但「市场异动告警」总开关是关的，先把它打开才会收到"
            try:
                await query.answer(tip, show_alert=True)
            except Exception:
                pass
            await safe_edit(query,
                            "🔔 *订阅推送*\n✅已订阅 ⬜未订阅，点击切换：",
                            reply_markup=subs_kb(cid), parse_mode="Markdown")

    elif d.startswith("mc:"):
        from handlers import microcap as _mc
        await _mc.on_button(query, context)

    # ---- 链上代币专区（DEX，交易所还没上的币）----
    elif d == "cat_onchain":
        from handlers import onchain as _oc
        await safe_edit(query, _oc.HOME_TEXT, reply_markup=_oc.home_kb(),
                        parse_mode="Markdown")

    elif d.startswith("oc:"):
        from handlers import onchain as _oc
        await _oc.on_button(query, context)

    # ---- 更新日志（这一版改了什么）----
    elif d.startswith("cl:"):
        from handlers import changelog as _cl
        await _cl.on_button(query, context)

    # ---- 数据源选择（默认 / 单条预警 / 单个监控 共用同一个面板）----
    elif d.startswith("src:"):
        from handlers import source as _source
        await _source.on_button(query, context)

    # ---- 缓步增长面板（切窗口天数 / 仅加密↔含股票商品）----
    elif d.startswith("stdy:"):
        from handlers import steady
        await steady.on_button(query, context)

    # ---- 全部命令面板 ----
    elif d.startswith("cmd:"):
        from handlers import cmdpanel
        await cmdpanel.on_button(query, context)

    # ---- 新功能分类页（扫描/工具/风险/复盘）----
    elif d in CATS:
        text, rows = CATS[d]
        await safe_edit(query, text, parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup(rows + [_back()]))

    # ---- 引导式输入：点按钮 → 提示格式 → 下一条消息当参数 ----
    elif d.startswith("ask:"):
        key = d.split(":", 1)[1]
        if key not in ASK:
            await query.answer("未知功能")
            return
        tip, cmd = ASK[key]
        from handlers import guided
        guided.arm_chat(context, "await_cmd",
                        query.message.chat_id if query.message else 0, cmd)
        await safe_edit(query, tip + "\n\n_直接发消息即可，发 /menu 取消_",
                        parse_mode="Markdown",
                        reply_markup=InlineKeyboardMarkup([_back()]))

    # ---- 分析结果的闭环按钮：带着币名进入下一步 ----
    elif d.startswith("fu:"):
        _p, key, sym = (d.split(":") + ["", ""])[:3]
        if key not in FOLLOWUP:
            await query.answer("未知操作")
            return
        tip, cmd = FOLLOWUP[key]
        s = sym if sym and sym != "-" else "BTC"
        if key == "check":          # 体检不需要补参数，直接跑
            await query.answer("体检中…")
            from handlers import datameta
            await datameta.datacheck(_fake_update(query), _FakeCtx([s], context))
            return
        from handlers import guided
        guided.arm_chat(context, "await_cmd",
                        query.message.chat_id if query.message else 0, cmd)
        await query.answer()
        await query.message.reply_text(
            tip.format(s=s) + "\n\n_直接发消息即可，发 /menu 取消_",
            parse_mode="Markdown")

    elif d.startswith("ctx:"):
        from handlers import chat as _chat
        await _chat.on_ctx_button(query, context)

    # ---- 无参数功能：点了直接跑 ----
    elif d.startswith("do:"):
        await _run_direct(query, context, d.split(":", 1)[1])

    # ---- 部署审批：确认/取消（仅管理员）----
    elif d.startswith("jdok:") or d.startswith("jdno:"):
        from config import is_admin
        tag = d.split(":", 1)[1]
        uid = query.from_user.id
        if not is_admin(uid):
            await query.answer("只有管理员能操作部署", show_alert=True)
            return
        if d.startswith("jdno:"):
            await query.answer("已取消")
            await safe_edit(query, f"❌ 已取消部署 {tag}")
            return
        # 确认部署
        await query.answer("已确认，正在触发部署…")
        from handlers.deploy import trigger_deploy
        ok, msg = await trigger_deploy(tag)
        if ok:
            who = query.from_user.first_name or "管理员"
            await safe_edit(
                query,
                f"🚀 *部署已启动*\n"
                f"━━━━━━━━━━━━━━\n"
                f"📦 版本　　`{tag}`\n"
                f"👤 确认人　{escape_md(who)}\n"
                f"⚙️ 状态　　部署系统执行中…\n"
                f"⏱ 预计　　约 1~2 分钟\n"
                f"━━━━━━━━━━━━━━\n"
                f"完成后会自动播报「✅ 部署成功」",
                parse_mode="Markdown")
        else:
            # 失败保留按钮，修好后可直接重试，不用重新发通知
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"🔁 重试部署 {tag}", callback_data=f"jdok:{tag}"),
                InlineKeyboardButton("❌ 取消", callback_data=f"jdno:{tag}"),
            ]])
            await safe_edit(
                query,
                f"❌ *部署触发失败*\n"
                f"━━━━━━━━━━━━━━\n"
                f"📦 版本　`{tag}`\n"
                f"⚠️ 原因　{escape_md(str(msg))}\n"
                f"━━━━━━━━━━━━━━\n"
                # 说清楚"没开始"和"跑挂了"的区别：这一步是按钮没按下去，
                # 服务器上的代码和容器都没动，线上还是老版本，重试是安全的
                f"这一步只是**没触发成功**——服务器代码没动，线上仍是旧版本。\n"
                f"重试是安全的，点下方按钮即可",
                reply_markup=kb, parse_mode="Markdown")

    # ---- 查其他币（按来源决定后续动作）----
    elif d.startswith("askcoin:"):
        action = d.split(":", 1)[1]
        if action == "alertcoin":
            # 预警场景：记下"等用户发币名来设预警"，quickprice 会接住
            from handlers import guided
            guided.arm_chat(context, "await_alert_coin",
                            query.message.chat_id if query.message else 0)
            await safe_edit(query,
                "🔍 *给其他币设预警*\n\n发送币名即可，例如 `pepe`、`arb`\n"
                "（发完会让你选涨破/跌破；取消发 /menu）",
                parse_mode="Markdown")
        else:
            # 查价/详情/分析等：直接发币名即可，纯文字查价会接住
            await safe_edit(query, 
                "🔍 *查其他币*\n\n直接发送币名即可，例如：`pepe`、`wif`、`arb`\n"
                "（几百种币都支持，大小写都行）",
                reply_markup=back_kb(), parse_mode="Markdown")

    # ---- 刷新看板 ----
    elif d == "dash_refresh":
        from handlers.dashboard import build_dashboard
        await safe_edit(query, "🔄 刷新中...")
        try:
            text = await build_dashboard()
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 刷新", callback_data="dash_refresh"),
                 InlineKeyboardButton("📋 菜单", callback_data="menu_main")],
            ])
            await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"看板刷新出错: {e}")
            await safe_edit(query, f"刷新失败：{str(e)[:80]}", reply_markup=back_kb())

    # ============ 行情查询 ============
    # ── 合并入口（v1.32.0）──────────────────────────────────
    # 这三个面板本身不实现任何功能，只是把原来散在首页的入口收进来。
    # 原入口的 callback_data 一个没改：进得来、回得去，深链和历史消息里的按钮照常能用。
    elif d == "scan:src":
        from handlers import scan as _sc
        await safe_edit(query,
            "🏦 *换个盘子再扫*\n\n"
            "扫描的**盘口和深度是那一家的**，所以结果不跨所比较——"
            "同一个币在 Bybit 能进出，在小所可能吃两千 U 就滑 1%。\n"
            "现货和永续也不是一回事：永续能做空、有资金费，现货只能买。",
            reply_markup=_sc.source_kb(), parse_mode="Markdown")

    elif d.startswith("scan:on:"):
        from handlers import scan as _sc
        from handlers import busy
        label = d.split(":", 2)[2]
        uid = query.from_user.id
        async with busy.guard(uid, "scan") as ok:
            if not ok:
                await query.answer(busy.busy_text(uid, "scan", "扫描"),
                                   show_alert=True)
            else:
                await safe_edit(query, f"🔍 在 {label} 上扫描…（约 10~20 秒）")
                try:
                    rows = await _sc.run(source=label)
                except Exception as e:
                    logging.error(f"换源扫描失败 {label}: {e}")
                    await safe_edit(query, f"扫描失败：{str(e)[:80]}",
                                    reply_markup=_sc.result_kb())
                else:
                    context.chat_data["scan_rows"] = rows
                    context.chat_data["scan_src"] = label
                    await safe_edit(query, _sc.render_signals(rows, source=label),
                                    reply_markup=_sc.result_kb(),
                                    parse_mode="Markdown")

    elif d == "scan:detail":
        # 明细是上一次扫描的结果，不重新打接口——重扫要 15~30 秒，
        # 而他点这个是想看刚才那批的细节
        rows = context.chat_data.get("scan_rows")
        if not rows:
            await query.answer("这批结果已经过期了，重扫一次", show_alert=True)
        else:
            from handlers import scan as _sc
            await safe_edit(query,
                _sc.render(rows, source=context.chat_data.get("scan_src", "Bybit永续")),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⬅️ 回信号版", callback_data="scan:brief"),
                    InlineKeyboardButton("🏦 换所", callback_data="scan:src"),
                    InlineKeyboardButton("🔄 重扫", callback_data="do:scan")]]),
                parse_mode="Markdown")

    elif d == "scan:brief":
        rows = context.chat_data.get("scan_rows")
        if not rows:
            await query.answer("这批结果已经过期了，重扫一次", show_alert=True)
        else:
            from handlers import scan as _sc
            await safe_edit(query,
                _sc.render_signals(rows,
                                   source=context.chat_data.get("scan_src", "Bybit永续")),
                reply_markup=_sc.result_kb(),
                parse_mode="Markdown")

    elif d == "cat_market":
        await safe_edit(query,
            "📊 *行情*\n\n看盘子、查币价、看榜单——先知道现在是什么局面。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📊 市场看板（一屏总览）",
                                      callback_data="dash_refresh")],
                # 多日涨跌榜提到这一层：原来埋在「查币价/涨跌榜」里要点三下才够得着，
                # 他连按钮做没做都没看见。榜单是进「行情」最常想看的东西，
                # 不该比查单个币还深
                [InlineKeyboardButton("📅 3日涨跌榜", callback_data="dr:w:3:all:all:hot"),
                 InlineKeyboardButton("📅 7日涨跌榜", callback_data="dr:w:7:all:all:hot")],
                [InlineKeyboardButton("⚖️ 多空比极值榜", callback_data="ls:v:binance")],
                [InlineKeyboardButton("💰 查币价/涨跌榜", callback_data="cat_price"),
                 InlineKeyboardButton("📰 资讯快讯", callback_data="cat_news")],
                [InlineKeyboardButton("📡 换数据源（用哪家的价）",
                                      callback_data="src:home")],
                _back()]), parse_mode="Markdown")

    elif d == "cat_venues":
        await safe_edit(query,
            "🏦 *交易所专区*\n\n四家的功能是一套的（资金费/爆仓/多空比/新币榜…），"
            "先选一家。\n\n"
            "平时查某个币不用进这里——直接发币名，或用「📊 行情」，"
            "那边跟着你 `/source` 设的默认源走。",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔥 OKX", callback_data="cat_okx"),
                 InlineKeyboardButton("🅱️ 币安", callback_data="cat_binance")],
                [InlineKeyboardButton("🟡 Bybit", callback_data="cat_bybit"),
                 InlineKeyboardButton("🟢 Gate", callback_data="cat_gate")],
                [InlineKeyboardButton("📡 设默认数据源", callback_data="src:home")],
                _back()]), parse_mode="Markdown")

    elif d == "cat_notify":
        await safe_edit(query, NOTIFY_TEXT,
                        reply_markup=notify_kb(query.message.chat_id),
                        parse_mode="Markdown")

    elif d == "cat_price":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 查币价", callback_data="sub_price"),
             InlineKeyboardButton("📋 币详情", callback_data="sub_info")],
            [InlineKeyboardButton("🚀 涨跌榜(24h)", callback_data="do_top")],
            [InlineKeyboardButton("📅 3日涨跌榜", callback_data="dr:w:3:all:all:hot"),
             InlineKeyboardButton("📅 7日涨跌榜", callback_data="dr:w:7:all:all:hot")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, "📊 *行情查询*\n选择功能：", reply_markup=kb, parse_mode="Markdown")

    elif d == "sub_price":
        await safe_edit(query, "💰 *查币价* - 点币种：\n(更多币用 `/price 币名`)",
            reply_markup=coin_grid("getprice", "cat_price"), parse_mode="Markdown")

    elif d == "sub_info":
        await safe_edit(query, "📋 *币详情* - 点币种：",
            reply_markup=coin_grid("getinfo", "cat_price"), parse_mode="Markdown")

    elif d == "do_top":
        await safe_edit(query, "🚀 正在获取涨跌榜...")
        try:
            gainers, losers = await get_top_movers(15)
            lines = ["🚀 *24h涨幅榜 TOP15*"]
            for i, c in enumerate(gainers, 1):
                lines.append(f"{i}. {escape_md(c['symbol'])}: +{c['change']:.2f}%")
            lines.append("\n📉 *24h跌幅榜 TOP15*")
            for i, c in enumerate(losers, 1):
                lines.append(f"{i}. {escape_md(c['symbol'])}: {c['change']:.2f}%")
            await safe_edit(query, "\n".join(lines), reply_markup=back_to("cat_price"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"涨跌榜出错: {e}")
            await safe_edit(query, f"获取失败：{str(e)[:80]}", reply_markup=back_to("cat_price"))

    elif d.startswith("getprice:"):
        symbol = d.split(":")[1]
        try:
            r = await get_price(symbol)
            emoji = "📈" if r["change"] >= 0 else "📉"
            await safe_edit(query,
                f"{emoji} *{escape_md(symbol)}*\n价格: ${r['price']:,.2f}\n24h: {r['change']:+.2f}%",
                reply_markup=back_to("sub_price"), parse_mode="Markdown")
        except Exception:
            await safe_edit(query, "查询失败", reply_markup=back_to("sub_price"))

    elif d.startswith("getinfo:"):
        symbol = d.split(":")[1]
        try:
            md = await get_market_data([symbol])
            x = md.get(symbol)
            if x:
                await safe_edit(query,
                    f"📋 *{escape_md(symbol)}*\n价格: ${x['price']:,.2f}\n市值排名: #{x['market_cap_rank']}\n"
                    f"市值: ${x['market_cap']:,.0f}\n24h量: ${x['volume']:,.0f}\n"
                    f"24h: {x['change_24h']:+.2f}% | 7d: {x['change_7d']:+.2f}% | 30d: {x['change_30d']:+.2f}%",
                    reply_markup=back_to("sub_info"), parse_mode="Markdown")
            else:
                await safe_edit(query, "无数据", reply_markup=back_to("sub_info"))
        except Exception:
            await safe_edit(query, "查询失败", reply_markup=back_to("sub_info"))

    # ============ 策略回测 ============
    elif d == "cat_strategy":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("😴 弱势/横盘扫描", callback_data="do_weak")],
            [InlineKeyboardButton("📈 动量轮动回测", callback_data="do_momentum")],
            [InlineKeyboardButton("📈 连涨·Bybit", callback_data="streak:up:bybit"),
             InlineKeyboardButton("📉 连跌·Bybit", callback_data="streak:down:bybit")],
            [InlineKeyboardButton("📈 连涨·全部所", callback_data="streak:up:all"),
             InlineKeyboardButton("📉 连跌·全部所", callback_data="streak:down:all")],
            [InlineKeyboardButton("📋 合约交易检查清单", callback_data="show_checklist")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, 
            "📊 *策略回测 / 合约扫描*\n"
            "• 弱势/横盘扫描：找最横盘/最弱/相对抗跌的主流币\n"
            "• 动量轮动回测：只追最强K个币，对比死拿BTC\n"
            "• 连涨/连跌：找连续3天日线同向的永续合约（命令可自定义天数：`/upstreak 5 bybit`）\n"
            "• 检查清单：开仓前必看的合约风控自查\n\n"
            "⚠️ 回测/扫描≠未来，不构成投资建议",
            reply_markup=kb, parse_mode="Markdown")

    # 连涨/连跌合约扫描（streak:<up|down>:<ex>）
    elif d.startswith("streak:"):
        _, direction, exch = d.split(":")
        word = "连涨" if direction == "up" else "连跌"
        await safe_edit(query, 
            f"⏳ 扫描 {exch.upper()} 永续{word}中（连续3天），约需十几秒…")
        from handlers.streak import build_streak_text
        try:
            txt = await build_streak_text(direction, exch, 3, 5)
            await safe_edit(query, txt, reply_markup=back_to("cat_strategy"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单连涨/连跌扫描出错: {e}")
            await safe_edit(query, f"扫描失败，稍后再试：{str(e)[:80]}", reply_markup=back_to("cat_strategy"))

    # 合约交易检查清单
    elif d == "show_checklist":
        from handlers.checklist import CHECKLIST
        await safe_edit(query, CHECKLIST, reply_markup=back_to("cat_strategy"), parse_mode="Markdown")

    elif d == "do_weak":
        await safe_edit(query, "🔎 扫描市值前 50 主流币...")
        from handlers.strategy import build_weak_text
        try:
            await safe_edit(query, await build_weak_text(50),
                            reply_markup=back_to("cat_strategy"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单弱势扫描出错: {e}")
            await safe_edit(query, f"扫描失败：{str(e)[:80]}", reply_markup=back_to("cat_strategy"))

    elif d == "do_momentum":
        from handlers import busy
        from handlers.strategy import build_momentum_text
        uid = query.from_user.id
        async with busy.guard(uid, "momentum") as ok:
            if not ok:
                await query.answer(busy.busy_text(uid, "momentum", "动量回测"),
                                   show_alert=True)
            else:
                await safe_edit(query, "⏳ 动量轮动回测中，需逐个拉 24 个币的日线，"
                                       "数据源有限流，可能要 1~3 分钟。\n"
                                       "**这期间机器人的其他功能照常能用**，"
                                       "跑完直接出结果。",
                                parse_mode="Markdown")
                try:
                    await safe_edit(query, await build_momentum_text(),
                                    reply_markup=back_to("cat_strategy"),
                                    parse_mode="Markdown")
                except Exception as e:
                    logging.error(f"菜单动量回测出错: {e}")
                    await safe_edit(query, f"回测失败：{str(e)[:80]}",
                                    reply_markup=back_to("cat_strategy"))

    # ============ 技术分析 ============
    elif d == "cat_analysis":
        kb = coin_grid("doanalyze", "menu_main")
        # 在币种网格上方插一行「标注图表」入口（网格本身按 action 前缀走 doanalyze）
        rows = list(kb.inline_keyboard)
        # 合并进来的：策略回测原来是首页独立入口，和技术分析本就是一件事的两端
        rows.insert(0, [InlineKeyboardButton("📊 策略回测/动量轮动",
                                             callback_data="cat_strategy")])
        rows.insert(0, [InlineKeyboardButton("📐 标注图表(结构位+止损带画在图上)",
                                            callback_data="ac_help")])
        # 清算地图放最上面：它是"这单该在哪儿设止损"最直接的那张图
        rows.insert(0, [InlineKeyboardButton("💣 清算地图(各价位待强平·估算)",
                                            callback_data="lq:pick:-:-")])
        await safe_edit(query, 
            "📈 *技术分析* - 点币种做综合分析：\n(RSI+均线+MACD+布林带)",
            reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")

    elif d == "ac_help":
        await safe_edit(query,
            "📐 *标注图表*——把结构位画在图上，不用对着数字脑补\n\n"
            "`/achart BTC`　默认 1h\n"
            "`/achart SOL 15m`　周期 5m/15m/30m/1h/4h/1d\n\n"
            "图上标：🟡MA3 🔵MA13 🟣MA23、⬛摆动高低点(结构失效位=止损该放的地方)、"
            "🔴前高🟢前低(流动性区=止盈参考)、⚪VWAP、🟠1.5×ATR止损带。\n"
            "出图后可点【🤖 AI 解读这张图】。",
            reply_markup=back_to("cat_analysis"), parse_mode="Markdown")

    elif d.startswith("doanalyze:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"🔍 正在分析 {symbol}...")
        from handlers.analysis import build_analysis_text
        try:
            text = await build_analysis_text(symbol)
            # 分析完不该是死胡同：直接给出下一步，且带着币名，不用重新输
            kb = followup_kb(symbol, back="cat_analysis")
            rows = [[InlineKeyboardButton("🤖 AI解读", callback_data=f"doai:{symbol}")]]
            rows += [list(r) for r in kb.inline_keyboard]
            await safe_edit(query, text, reply_markup=InlineKeyboardMarkup(rows),
                            parse_mode="Markdown")
        except Exception as e:
            logging.error(f"分析出错: {e}")
            await safe_edit(query, f"分析失败：{str(e)[:80]}", reply_markup=back_to("cat_analysis"))

    elif d.startswith("doai:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"🤖 AI分析 {symbol} 中...")
        from handlers.ai import build_ai_text
        try:
            text = await build_ai_text(symbol)
            await safe_edit(query, text, reply_markup=followup_kb(symbol, "cat_analysis"),
                            parse_mode="Markdown")
        except Exception as e:
            logging.error(f"AI出错: {e}")
            # 带上原因：只写日志的话，用户只能看到"AI分析失败"四个字，
            # 到底是行情源限流还是模型挂了根本无从判断
            await safe_edit(query, f"AI分析失败：{str(e)[:100]}",
                                          reply_markup=back_to("cat_analysis"))

    # ============ 预警（引导式：选币→选方向→发价格）============
    elif d == "cat_alert":
        rows = []
        for i in range(0, len(POPULAR), 5):
            rows.append([InlineKeyboardButton(c, callback_data=f"alertcoin:{c}") for c in POPULAR[i:i+5]])
        rows.append([InlineKeyboardButton("🔍 查其他币", callback_data="askcoin:alertcoin")])
        rows.append([InlineKeyboardButton("👁 持续波动监控(±% 反复提醒)", callback_data="watchpct_start")])
        rows.append([InlineKeyboardButton("🎯 条件提醒(价格+指标组合)", callback_data="cond_help")])
        rows.append([InlineKeyboardButton("🚨 极端拉升(15m暴拉+多日已涨)",
                                          callback_data="p3:panel")])
        rows.append([InlineKeyboardButton("📋 我的价格预警", callback_data="my_alerts"),
                     InlineKeyboardButton("👁 我的波动监控", callback_data="my_watchpct")])
        rows.append([InlineKeyboardButton("📡 默认数据源（用哪家交易所的价）",
                                          callback_data="src:panel:def")])
        rows.append([InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")])
        from handlers import source as _source
        _ex, _mk = _source.get_pref(query.message.chat_id if query.message else 0)
        await safe_edit(query, 
            "🔔 *价格预警 / 波动监控*\n\n"
            "• 选币设**涨破/跌破**或**±5%**提醒(一次性)👇\n"
            "• 或点【👁 持续波动监控】盯指定币，涨跌超阈值**反复**提醒(支持小盘/合约币)\n"
            f"• 当前数据源：*{_source.describe(_ex, _mk)}*（每条设完还能单独改）",
            reply_markup=InlineKeyboardMarkup(rows), parse_mode="Markdown")

    # 查看我的预警列表（每条带删除按钮）
    elif d == "my_alerts":
        await render_my_alerts(query)

    # 删除某条预警（按全局下标，校验归属）
    elif d.startswith("delalert:"):
        from storage import data as _ad, save_data as _as
        chat_id = query.message.chat_id
        try:
            gi = int(d.split(":")[1])
        except ValueError:
            gi = -1
        alerts = _ad.get("alerts", [])
        if 0 <= gi < len(alerts) and alerts[gi].get("chat_id") == chat_id:
            alerts.pop(gi)
            _as()
        await render_my_alerts(query)

    # 选好币 → 选方向
    elif d.startswith("alertcoin:"):
        symbol = d.split(":")[1]
        await safe_edit(query, 
            f"🔔 *{symbol} 价格预警*\n选择提醒方式：",
            reply_markup=alert_direction_kb(symbol), parse_mode="Markdown")

    # 选好方向 → 等用户发价格（存到 user_data，quickprice 会接住）
    elif d.startswith("alertset:"):
        _, symbol, direction = d.split(":")
        from handlers import guided
        guided.arm_chat(context, "await_alert",
                        query.message.chat_id if query.message else 0,
                        {"symbol": symbol, "direction": direction})
        arrow = "涨破" if direction == "above" else "跌破"
        await safe_edit(query, 
            f"🔔 *{symbol} {arrow}提醒*\n\n请直接发送触发价格，例如 `65000`\n"
            f"（发送后自动设置，到价会提醒你；取消发 /menu）",
            parse_mode="Markdown")

    # 一键 ±5% 预警（用当前价做基准）
    elif d.startswith("alertpctset:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"⚡ 设置 {symbol} ±5% 提醒中...")
        from handlers import alert as _alert, source as _src
        try:
            chat_id = query.message.chat_id
            base, used = await _src.price_for(chat_id, symbol)
            if base is None:
                await safe_edit(query, "获取当前价失败，稍后再试", reply_markup=back_to("cat_alert"))
            else:
                idx = _alert.add_alert(chat_id, {
                    "type": "pct", "symbol": symbol, "pct": 5, "base_price": base,
                    "set_by": query.from_user.first_name,
                })
                await safe_edit(query, 
                    f"✅ 已设置 *{symbol}* 涨跌超 ±5% 提醒\n"
                    f"基准价 ${base:,.6g}　数据源 {used}",
                    reply_markup=_alert.src_kb(chat_id, idx), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"一键百分比预警出错: {e}")
            await safe_edit(query, f"设置失败，稍后再试：{str(e)[:80]}", reply_markup=back_to("cat_alert"))

    # 持续波动监控：引导用户发「币 百分比」，quickprice 接住
    elif d == "watchpct_start":
        from handlers import guided
        guided.arm_chat(context, "await_watchpct",
                        query.message.chat_id if query.message else 0)
        from handlers import source as _source
        _ex, _mk = _source.get_pref(query.message.chat_id if query.message else 0)
        await safe_edit(query, 
            "👁 *持续波动监控*\n\n请发送「币 百分比 [合约] [交易所]」，例如：\n"
            "`DOGE 5`　`KORU 10`　`BTC 3`\n"
            "`BTC 3 合约`　← 加「合约」二字强制盯**永续合约价**\n"
            "`AKE 2 合约 gate`　← 再加所名只盯那一家（okx/币安/bybit/gate）\n\n"
            f"不写交易所就用你的默认：*{_source.describe(_ex, _mk)}*\n"
            "（下面的按钮可以改默认；设完之后每个币还能单独换）\n\n"
            "该币每从基准涨跌超此百分比就提醒，报后自动以新价继续盯。\n"
            "支持小盘/合约币。取消发 /menu",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(f"📡 默认数据源：{_source.describe(_ex, _mk)}",
                                       callback_data="src:panel:def")],
                 [InlineKeyboardButton("⬅️ 返回", callback_data="cat_alert")]]),
            parse_mode="Markdown")

    # 我的波动监控列表（带取消按钮）
    elif d == "my_watchpct":
        await render_my_watchpct(query)

    # 取消某个波动监控
    elif d.startswith("delwatchpct:"):
        from storage import data as _ad, save_data as _as
        sym = d.split(":", 1)[1]
        chat_id = query.message.chat_id
        wl = _ad.get("watchpct", [])
        wl[:] = [w for w in wl if not (w["chat_id"] == chat_id and w["symbol"] == sym)]
        _as()
        await render_my_watchpct(query)

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
        await safe_edit(query, "🔥 *OKX 专区* (交易所实时数据)\n点按钮直接看：", reply_markup=kb, parse_mode="Markdown")

    elif d == "okx_new":
        await safe_edit(query, "🆕 查询中...")
        from handlers.okx import build_new_text
        try:
            await safe_edit(query, await build_new_text(), reply_markup=back_to("cat_okx"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"新币榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_okx"))

    elif d == "okx_gainers":
        await safe_edit(query, "🚀 查询中...")
        from handlers.okx import build_gainers_text
        try:
            await safe_edit(query, await build_gainers_text("SPOT"), reply_markup=back_to("cat_okx"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"涨幅榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_okx"))

    elif d == "okx_swap":
        await safe_edit(query, "📊 查询中...")
        from handlers.okx import build_gainers_text
        try:
            await safe_edit(query, await build_gainers_text("SWAP"), reply_markup=back_to("cat_okx"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"合约榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_okx"))

    elif d == "okx_funding_sel":
        await safe_edit(query, "💵 *资金费率* - 点币种：", reply_markup=coin_grid("okxfunding", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxfunding:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"💵 查询 {symbol}...")
        from handlers.okx import build_funding_text
        try:
            await safe_edit(query, await build_funding_text(symbol), reply_markup=back_to("okx_funding_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"资金费率出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("okx_funding_sel"))

    elif d == "okx_ratio_sel":
        await safe_edit(query, "⚖️ *多空比* - 点币种：", reply_markup=coin_grid("okxratio", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxratio:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"⚖️ 查询 {symbol}...")
        from handlers.okx import build_ratio_text
        try:
            await safe_edit(query, await build_ratio_text(symbol), reply_markup=back_to("okx_ratio_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"多空比出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("okx_ratio_sel"))

    elif d == "okx_liq_sel":
        await safe_edit(query, "💥 *爆仓* - 点币种：", reply_markup=coin_grid("okxliq", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxliq:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"💥 查询 {symbol}...")
        from handlers.okx import build_liq_text
        try:
            await safe_edit(query, await build_liq_text(symbol), reply_markup=back_to("okx_liq_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"爆仓出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("okx_liq_sel"))

    # ============ 资讯快讯 ============
    elif d == "cat_news":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📰 最新新闻", callback_data="do_news")],
            [InlineKeyboardButton("📸 异动快照", callback_data="do_movers")],
            [InlineKeyboardButton("📊 市场总结", callback_data="do_summary")],
            [InlineKeyboardButton("🔓 解锁排行", callback_data="do_unlocks")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, "📰 *资讯快讯*\n点按钮直接看：", reply_markup=kb, parse_mode="Markdown")

    elif d == "do_news":
        await safe_edit(query, "📰 获取新闻...")
        from handlers.news import fetch_news, translate_news
        try:
            items = await fetch_news(8)
            cn = await translate_news(items)
            lines = ["📰 *最新加密新闻*\n"]
            for i, it in enumerate(items, 1):
                title = sanitize_link_text(cn.get(i, it["title"]) if cn else it["title"])
                lines.append(f"{i}. [{title}]({it['link']})")
            await safe_edit(query, "\n".join(lines), reply_markup=back_to("cat_news"),
                parse_mode="Markdown", disable_web_page_preview=True)
        except Exception as e:
            logging.error(f"菜单新闻出错: {e}")
            await safe_edit(query, f"获取失败：{str(e)[:80]}", reply_markup=back_to("cat_news"))

    elif d == "do_movers":
        await safe_edit(query, "📸 获取异动快照...")
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
            for c in g: lines.append(f"  {escape_md(c['sym'])}: {c['change']:+.1f}%")
            lines.append("💥跌幅:")
            for c in l: lines.append(f"  {escape_md(c['sym'])}: {c['change']:+.1f}%")
            await safe_edit(query, "\n".join(lines), reply_markup=back_to("cat_news"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单异动出错: {e}")
            await safe_edit(query, f"获取失败：{str(e)[:80]}", reply_markup=back_to("cat_news"))

    elif d == "do_summary":
        await safe_edit(query, "📊 生成市场总结...")
        from handlers.summary import build_summary
        try:
            await safe_edit(query, await build_summary(), reply_markup=back_to("cat_news"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单总结出错: {e}")
            await safe_edit(query, f"生成失败：{str(e)[:80]}", reply_markup=back_to("cat_news"))

    elif d == "do_unlocks":
        await safe_edit(query, "🔓 查询解锁排行...")
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
                await safe_edit(query, "近30天主流币无大额解锁", reply_markup=back_to("cat_news"))
            else:
                lines=["🔓 *未来30天大额解锁*\n"]
                for x in r[:10]:
                    dt=datetime.datetime.fromtimestamp(x["ts"]).strftime("%m-%d")
                    lines.append(f"{dt} {escape_md(x['sym'])} 解锁{x['pct']:.1f}%")
                await safe_edit(query, "\n".join(lines), reply_markup=back_to("cat_news"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"菜单解锁出错: {e}")
            await safe_edit(query, f"获取失败：{str(e)[:80]}", reply_markup=back_to("cat_news"))

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
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")

    # ============ 订阅推送（按钮+状态）============
    elif d == "cat_subs":
        kb = subs_kb(query.message.chat_id)
        await safe_edit(query, 
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
            # 兼容历史 int/str 混存：已订阅则两种形式都清掉；未订阅则以 int 存
            if chat_id in _sd[key] or str(chat_id) in _sd[key]:
                _sd[key] = [x for x in _sd[key] if x != chat_id and x != str(chat_id)]
            else:
                _sd[key].append(chat_id)
            _ss()
        # 重新渲染订阅菜单（刷新状态）——**用同一个构造函数**。
        # 以前这里抄了一份一模一样的键盘，改了 cat_subs 忘了这儿，
        # 点一下总开关新加的子开关就会从屏幕上消失。
        kb = subs_kb(chat_id)
        await safe_edit(query, 
            "🔔 *订阅推送*\n✅已订阅 ⬜未订阅，点击切换：",
            reply_markup=kb, parse_mode="Markdown")

    elif d == "okx_fprice_sel":
        await safe_edit(query, "📊 *合约行情* - 点币种：", reply_markup=coin_grid("okxfprice", "cat_okx"), parse_mode="Markdown")

    elif d.startswith("okxfprice:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"📊 查询 {symbol} 合约...")
        from handlers.okx import build_fprice_text
        try:
            await safe_edit(query, await build_fprice_text(symbol), reply_markup=back_to("okx_fprice_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"合约行情出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("okx_fprice_sel"))

    # ============ 币安专区（镜像 OKX，数据来自 Binance）============
    elif d == "cat_binance":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 新币榜", callback_data="bn_new"),
             InlineKeyboardButton("🚀 涨幅榜", callback_data="bn_gainers")],
            [InlineKeyboardButton("📊 合约涨幅", callback_data="bn_swap"),
             InlineKeyboardButton("💵 资金费率", callback_data="bn_funding_sel")],
            [InlineKeyboardButton("⚖️ 多空比", callback_data="bn_ratio_sel"),
             InlineKeyboardButton("💥 爆仓", callback_data="bn_liq_sel")],
            [InlineKeyboardButton("📊 合约行情", callback_data="bn_fprice_sel")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, "🅱️ *币安专区* (Binance 数据)\n点按钮直接看：", reply_markup=kb, parse_mode="Markdown")

    elif d == "bn_new":
        await safe_edit(query, "🆕 查询中...")
        from handlers.binance import build_new_text_bn
        try:
            await safe_edit(query, await build_new_text_bn(), reply_markup=back_to("cat_binance"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"币安新币榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_binance"))

    elif d == "bn_gainers":
        await safe_edit(query, "🚀 查询中...")
        from handlers.binance import build_gainers_text_bn
        try:
            await safe_edit(query, await build_gainers_text_bn("SPOT"), reply_markup=back_to("cat_binance"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"币安涨幅榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_binance"))

    elif d == "bn_swap":
        await safe_edit(query, "📊 查询中...")
        from handlers.binance import build_gainers_text_bn
        try:
            await safe_edit(query, await build_gainers_text_bn("SWAP"), reply_markup=back_to("cat_binance"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"币安合约榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_binance"))

    elif d == "bn_funding_sel":
        await safe_edit(query, "💵 *资金费率* - 点币种：", reply_markup=coin_grid("bnfunding", "cat_binance"), parse_mode="Markdown")

    elif d.startswith("bnfunding:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"💵 查询 {symbol}...")
        from handlers.binance import build_funding_text_bn
        try:
            await safe_edit(query, await build_funding_text_bn(symbol), reply_markup=back_to("bn_funding_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"币安资金费率出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("bn_funding_sel"))

    elif d == "bn_ratio_sel":
        await safe_edit(query, "⚖️ *多空比* - 点币种：", reply_markup=coin_grid("bnratio", "cat_binance"), parse_mode="Markdown")

    elif d.startswith("bnratio:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"⚖️ 查询 {symbol}...")
        from handlers.binance import build_ratio_text_bn
        try:
            await safe_edit(query, await build_ratio_text_bn(symbol), reply_markup=back_to("bn_ratio_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"币安多空比出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("bn_ratio_sel"))

    elif d == "bn_liq_sel":
        await safe_edit(query, "💥 *爆仓* - 点币种：", reply_markup=coin_grid("bnliq", "cat_binance"), parse_mode="Markdown")

    elif d.startswith("bnliq:"):
        symbol = d.split(":")[1]
        from handlers.binance import build_liq_text_bn
        await safe_edit(query, await build_liq_text_bn(symbol), reply_markup=back_to("bn_liq_sel"), parse_mode="Markdown")

    elif d == "bn_fprice_sel":
        await safe_edit(query, "📊 *合约行情* - 点币种：", reply_markup=coin_grid("bnfprice", "cat_binance"), parse_mode="Markdown")

    elif d.startswith("bnfprice:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"📊 查询 {symbol} 合约...")
        from handlers.binance import build_fprice_text_bn
        try:
            await safe_edit(query, await build_fprice_text_bn(symbol), reply_markup=back_to("bn_fprice_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"币安合约行情出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("bn_fprice_sel"))

    # ============ Gate 专区（镜像币安专区，数据来自 Gate.io）============
    # 小币/新币 Gate 上得最早最全，币安查不到不等于没这个合约。
    elif d == "cat_gate":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 新币榜", callback_data="gt_new"),
             InlineKeyboardButton("🚀 涨幅榜", callback_data="gt_gainers")],
            [InlineKeyboardButton("📊 合约涨幅", callback_data="gt_swap"),
             InlineKeyboardButton("💵 资金费率", callback_data="gt_funding_sel")],
            [InlineKeyboardButton("⚖️ 多空比", callback_data="gt_ratio_sel"),
             InlineKeyboardButton("💥 爆仓", callback_data="gt_liq_sel")],
            [InlineKeyboardButton("📊 合约行情", callback_data="gt_fprice_sel")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, 
            "🟢 *Gate 专区* (Gate.io 数据)\n"
            "小币和新币上得最早最全；爆仓这里是真有数的（币安已关公开接口）。\n"
            "榜单已剔除代币化股票/指数等非加密合约。",
            reply_markup=kb, parse_mode="Markdown")

    elif d == "gt_new":
        await safe_edit(query, "🆕 查询中...")
        from handlers.gate import build_new_text_gt
        try:
            await safe_edit(query, await build_new_text_gt(), reply_markup=back_to("cat_gate"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Gate 新币榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_gate"))

    elif d == "gt_gainers":
        await safe_edit(query, "🚀 查询中...")
        from handlers.gate import build_gainers_text_gt
        try:
            await safe_edit(query, await build_gainers_text_gt("SPOT"), reply_markup=back_to("cat_gate"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Gate 涨幅榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_gate"))

    elif d == "gt_swap":
        await safe_edit(query, "📊 查询中...")
        from handlers.gate import build_gainers_text_gt
        try:
            await safe_edit(query, await build_gainers_text_gt("SWAP"), reply_markup=back_to("cat_gate"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Gate 合约榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_gate"))

    elif d == "gt_funding_sel":
        await safe_edit(query, "💵 *资金费率* - 点币种：", reply_markup=coin_grid("gtfunding", "cat_gate"), parse_mode="Markdown")

    elif d.startswith("gtfunding:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"💵 查询 {symbol}...")
        from handlers.gate import build_funding_text_gt
        try:
            await safe_edit(query, await build_funding_text_gt(symbol), reply_markup=back_to("gt_funding_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Gate 资金费率出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("gt_funding_sel"))

    elif d == "gt_ratio_sel":
        await safe_edit(query, "⚖️ *多空比* - 点币种：", reply_markup=coin_grid("gtratio", "cat_gate"), parse_mode="Markdown")

    elif d.startswith("gtratio:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"⚖️ 查询 {symbol}...")
        from handlers.gate import build_ratio_text_gt
        try:
            await safe_edit(query, await build_ratio_text_gt(symbol), reply_markup=back_to("gt_ratio_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Gate 多空比出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("gt_ratio_sel"))

    elif d == "gt_liq_sel":
        await safe_edit(query, "💥 *爆仓* - 点币种：", reply_markup=coin_grid("gtliq", "cat_gate"), parse_mode="Markdown")

    elif d.startswith("gtliq:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"💥 查询 {symbol}...")
        from handlers.gate import build_liq_text_gt
        try:
            await safe_edit(query, await build_liq_text_gt(symbol), reply_markup=back_to("gt_liq_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Gate 爆仓出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("gt_liq_sel"))

    elif d == "gt_fprice_sel":
        await safe_edit(query, "📊 *合约行情* - 点币种：", reply_markup=coin_grid("gtfprice", "cat_gate"), parse_mode="Markdown")

    elif d.startswith("gtfprice:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"📊 查询 {symbol} 合约...")
        from handlers.gate import build_fprice_text_gt
        try:
            await safe_edit(query, await build_fprice_text_gt(symbol), reply_markup=back_to("gt_fprice_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Gate 合约行情出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("gt_fprice_sel"))

    # ============ Bybit 专区（镜像 OKX/币安，数据来自 Bybit）============
    elif d == "cat_bybit":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 新币榜", callback_data="by_new"),
             InlineKeyboardButton("🚀 涨幅榜", callback_data="by_gainers")],
            [InlineKeyboardButton("📊 合约涨幅", callback_data="by_swap"),
             InlineKeyboardButton("💵 资金费率", callback_data="by_funding_sel")],
            [InlineKeyboardButton("⚖️ 多空比", callback_data="by_ratio_sel"),
             InlineKeyboardButton("💥 爆仓", callback_data="by_liq_sel")],
            [InlineKeyboardButton("📊 合约行情", callback_data="by_fprice_sel")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, "🟡 *Bybit 专区* (Bybit 数据)\n点按钮直接看：", reply_markup=kb, parse_mode="Markdown")

    elif d == "by_new":
        await safe_edit(query, "🆕 查询中...")
        from handlers.bybit import build_new_text_by
        try:
            await safe_edit(query, await build_new_text_by(), reply_markup=back_to("cat_bybit"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Bybit新币榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_bybit"))

    elif d == "by_gainers":
        await safe_edit(query, "🚀 查询中...")
        from handlers.bybit import build_gainers_text_by
        try:
            await safe_edit(query, await build_gainers_text_by("SPOT"), reply_markup=back_to("cat_bybit"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Bybit涨幅榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_bybit"))

    elif d == "by_swap":
        await safe_edit(query, "📊 查询中...")
        from handlers.bybit import build_gainers_text_by
        try:
            await safe_edit(query, await build_gainers_text_by("SWAP"), reply_markup=back_to("cat_bybit"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Bybit合约榜出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_bybit"))

    elif d == "by_funding_sel":
        await safe_edit(query, "💵 *资金费率* - 点币种：", reply_markup=coin_grid("byfunding", "cat_bybit"), parse_mode="Markdown")

    elif d.startswith("byfunding:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"💵 查询 {symbol}...")
        from handlers.bybit import build_funding_text_by
        try:
            await safe_edit(query, await build_funding_text_by(symbol), reply_markup=back_to("by_funding_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Bybit资金费率出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("by_funding_sel"))

    elif d == "by_ratio_sel":
        await safe_edit(query, "⚖️ *多空比* - 点币种：", reply_markup=coin_grid("byratio", "cat_bybit"), parse_mode="Markdown")

    elif d.startswith("byratio:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"⚖️ 查询 {symbol}...")
        from handlers.bybit import build_ratio_text_by
        try:
            await safe_edit(query, await build_ratio_text_by(symbol), reply_markup=back_to("by_ratio_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Bybit多空比出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("by_ratio_sel"))

    elif d == "by_liq_sel":
        await safe_edit(query, "💥 *爆仓* - 点币种：", reply_markup=coin_grid("byliq", "cat_bybit"), parse_mode="Markdown")

    elif d.startswith("byliq:"):
        symbol = d.split(":")[1]
        from handlers.bybit import build_liq_text_by
        await safe_edit(query, await build_liq_text_by(symbol), reply_markup=back_to("by_liq_sel"), parse_mode="Markdown")

    elif d == "by_fprice_sel":
        await safe_edit(query, "📊 *合约行情* - 点币种：", reply_markup=coin_grid("byfprice", "cat_bybit"), parse_mode="Markdown")

    elif d.startswith("byfprice:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"📊 查询 {symbol} 合约...")
        from handlers.bybit import build_fprice_text_by
        try:
            await safe_edit(query, await build_fprice_text_by(symbol), reply_markup=back_to("by_fprice_sel"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Bybit合约行情出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("by_fprice_sel"))

    # ============ 工具（按钮直达）============
    elif d == "cat_tools":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("😱 恐惧贪婪", callback_data="do_fear"),
             InlineKeyboardButton("⛽ Gas查询", callback_data="do_gas")],
            [InlineKeyboardButton("⛽ Gas提醒", callback_data="cat_gasalert"),
             InlineKeyboardButton("💱 多所比价", callback_data="sub_arb")],
            [InlineKeyboardButton("💱 套利监控", callback_data="cat_arbwatch"),
             InlineKeyboardButton("🐋 巨鲸扫描", callback_data="do_whale")],
            [InlineKeyboardButton("💵 资金费率极端榜(全市场)", callback_data="fex")],
            [InlineKeyboardButton("🐋 地址追踪", callback_data="cat_track")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, "🛠 *实用工具*\n点按钮直接出结果：", reply_markup=kb, parse_mode="Markdown")

    # ---- Gas 提醒（按钮设阈值）----
    elif d == "cat_gasalert":
        text, kb = gas_panel(query.message.chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
    elif d.startswith("gasset:"):
        from storage import data as _d, save_data as _s
        cid = str(query.message.chat_id); val = d.split(":")[1]
        _d.setdefault("gas_subs", {})
        if val == "off":
            _d["gas_subs"].pop(cid, None); _s(); await query.answer("已关闭")
        else:
            _d["gas_subs"][cid] = {"threshold": float(val), "armed": True}; _s()
            await query.answer(f"已设：跌破 {val} gwei")
        text, kb = gas_panel(cid)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")

    # ---- 套利监控（按钮设阈值）----
    elif d == "cat_arbwatch":
        text, kb = arb_panel(query.message.chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
    elif d.startswith("arbset:"):
        from storage import data as _d, save_data as _s
        cid = str(query.message.chat_id); val = d.split(":")[1]
        _d.setdefault("arb_subs", {})
        if val == "off":
            _d["arb_subs"].pop(cid, None); _s(); await query.answer("已关闭")
        else:
            _d["arb_subs"][cid] = {"threshold": float(val)}; _s()
            await query.answer(f"已设：净价差≥{val}%")
        text, kb = arb_panel(cid)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")

    # ---- 地址追踪（按钮增删）----
    elif d == "cat_track":
        text, kb = track_panel(query.message.chat_id)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
    elif d == "trackadd":
        from handlers import guided
        guided.arm_chat(context, "await_track_addr",
                        query.message.chat_id if query.message else 0)
        await safe_edit(query, "🐋 发送要追踪的以太坊地址(0x 开头 42 位)，我就开始盯它。\n(取消发 /menu)")
    elif d.startswith("trackdel:"):
        from storage import data as _d, save_data as _s
        cid = str(query.message.chat_id); addr = d.split(":", 1)[1]
        _d.get("whale_addr", {}).get(cid, {}).pop(addr, None); _s()
        await query.answer("已取消关注")
        text, kb = track_panel(cid)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
    elif d.startswith("trackmin:"):
        from storage import data as _d, save_data as _s
        cid = str(query.message.chat_id); val = int(d.split(":")[1])
        _d.setdefault("whale_min", {})[cid] = val; _s()
        await query.answer("已设最小金额")
        text, kb = track_panel(cid)
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")

    elif d == "do_fear":
        await safe_edit(query, "😱 获取中...")
        try:
            fg = await get_fear_greed()
            await safe_edit(query, 
                f"😱 *恐惧贪婪指数*\n{fg['value']}/100 - {fg['classification']}\n(不构成投资建议)",
                reply_markup=back_to("cat_tools"), parse_mode="Markdown")
        except Exception:
            await safe_edit(query, "获取失败", reply_markup=back_to("cat_tools"))

    elif d == "do_gas":
        await safe_edit(query, "⛽ 获取中...")
        try:
            gwei = await get_gas_price()
            await safe_edit(query, f"⛽ *以太坊Gas*: {gwei:.2f} gwei",
                reply_markup=back_to("cat_tools"), parse_mode="Markdown")
        except Exception:
            await safe_edit(query, "获取失败", reply_markup=back_to("cat_tools"))

    elif d == "do_whale":
        await safe_edit(query, "🐋 扫描最新区块...")
        from handlers.whale import build_whale_text
        try:
            text = await build_whale_text(100)
            await safe_edit(query, text, reply_markup=back_to("cat_tools"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"巨鲸出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("cat_tools"))

    elif d == "sub_arb":
        await safe_edit(query, "💱 *多交易所比价* - 点币种：",
            reply_markup=coin_grid("doarb", "cat_tools"), parse_mode="Markdown")

    elif d.startswith("doarb:"):
        symbol = d.split(":")[1]
        await safe_edit(query, f"💱 查询 {symbol} 各所价格...")
        from handlers.arbitrage import build_arb_text
        try:
            text = await build_arb_text(symbol)
            await safe_edit(query, text, reply_markup=back_to("sub_arb"), parse_mode="Markdown")
        except Exception as e:
            logging.error(f"比价出错: {e}")
            await safe_edit(query, f"查询失败：{str(e)[:80]}", reply_markup=back_to("sub_arb"))

    # ============ 持仓 ============
    elif d == "cat_holding":
        await safe_edit(query, 
            "💼 *我的持仓* (🔒私聊使用)\n\n"
            "`/buy BTC 0.5 60000` 买入\n"
            "`/sell BTC 0.3` 卖出\n"
            "`/portfolio` 组合盈亏\n"
            "`/ranking` 盈亏排行\n"
            "`/piechart` 持仓饼图",
            reply_markup=back_kb(), parse_mode="Markdown")

    # ============ 虚拟合约交易（模拟盘）============
    elif d == "cat_vtrade":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎮 打开虚拟交易台（全按钮）",
                                  callback_data="vg:home")],
            [InlineKeyboardButton("💼 合约持仓/账户", callback_data="vpos_refresh"),
             InlineKeyboardButton("🪙 现货持币", callback_data="vspot_show")],
            [InlineKeyboardButton("📋 我的委托单", callback_data="vord_show"),
             InlineKeyboardButton("📜 历史/胜率", callback_data="vhist_show")],
            [InlineKeyboardButton("🔴 实盘交易(Bybit)", callback_data="cat_rtrade")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query,
            "🎮 *虚拟交易*（模拟盘，真实行情，不碰真钱 🔒私聊）\n\n"
            "*永续合约*（有杠杆、会爆仓、扣资金费）\n"
            "`/vopen BTC long 1000 10` 市价开多（1000U 保证金 10x）\n"
            "`/vopen BTC long 1000 10 60000` **挂限价委托**，到价才成交\n"
            "`/vclose BTC` 平仓（`/vclose BTC 50` 平一半）\n"
            "`/vtpsl BTC tp=70000 sl=58000` 挂止盈止损\n\n"
            "*现货*（无杠杆、不会爆仓、不扣资金费）\n"
            "`/vbuy BTC 1000` 花 1000U 市价买入\n"
            "`/vbuy BTC 1000 58000` 挂限价买单\n"
            "`/vsell BTC all` 卖出（`/vsell BTC all 70000` 限价卖）\n\n"
            "*委托单*　`/vorders` 看挂单　`/vcancel 3` 撤单\n\n"
            "初始本金 $10,000。挂单走 0.02% 挂单费率、吃单按你账户的真实费率，"
            "开仓算真实滑点，后台每 60 秒盯爆仓/止盈损/挂单成交。\n"
            "⚠️ 模拟盘，不构成投资建议",
            reply_markup=kb, parse_mode="Markdown")

    # ---- 虚拟交易台（全按钮，不用记命令）----
    elif d.startswith("rl:"):
        from handlers import relay as _rl
        await _rl.on_button(query, context)

    elif d.startswith("p3:"):
        from handlers import pump3 as _p3
        await _p3.on_button(query, context)

    elif d.startswith("lq:"):
        # 清算地图：lq:<w|r|i|pick>:<币>:<窗口>
        from handlers import liqmap as _lq
        bits = d.split(":")
        await _lq.from_btn(query, context, bits[1], bits[2], bits[3])

    elif d.startswith("lqcoin:"):
        from handlers import liqmap as _lq
        await query.answer()
        await _lq.on_coin(query.message, context, d.split(":", 1)[1])

    elif d.startswith("ls:"):
        # 多空比极值榜：ls:<v|r|i>:<交易所>　v=换所(读缓存) r=重扫 i=看口径
        from handlers import lsratio as _ls
        bits = d.split(":")
        await _ls.from_btn(query, context, bits[2],
                           force=(bits[1] == "r"), detail=(bits[1] == "i"))

    elif d.startswith("pf:"):
        # 持仓结构：pf:<r|i|h小时数>:<币>　r=拉一次 i=看口径 h13=换成 13 小时窗口
        from handlers import posflow as _pf
        bits = d.split(":")
        act = bits[1]
        hrs = _pf.DEFAULT_HOURS
        if act.startswith("h"):
            try:
                hrs = int(act[1:])
            except ValueError:
                pass
        await _pf.from_btn(query, context, bits[2], detail=(act == "i"), hours=hrs)

    elif d.startswith("dr:"):
        # 多日涨跌榜：dr:<w|r|i>:<天数>:<交易所>:<市场>:<hot|full>
        # w=换窗口/换所/换市场/换范围（读缓存）　r=强制重扫　i=看口径
        from handlers import dayrank as _dr
        bits = d.split(":")
        await _dr.from_btn(query, context, int(bits[2]), bits[3], bits[4],
                           hot=(len(bits) < 6 or bits[5] == "hot"),
                           force=(bits[1] == "r"), detail=(bits[1] == "i"))

    elif d.startswith("vg:"):
        from handlers import vpanel as _vp
        bits = d.split(":")
        act = bits[1] if len(bits) > 1 else "home"
        try:
            if act == "home":
                await _vp.home(query)
            elif act == "open":
                await _vp.pick_symbol(query, "perp")
            elif act == "buy":
                await _vp.pick_symbol(query, "spot")
            elif act == "perpsym":
                await _vp.pick_side(query, bits[2])
            elif act == "spotsym":
                await _vp.pick_spot_amount(query, bits[2])
            elif act == "side":
                await _vp.pick_lev(query, bits[2], bits[3])
            elif act == "lev":
                await _vp.pick_margin(query, bits[2], bits[3], float(bits[4]))
            elif act == "mgn":
                await _vp.pick_type(query, bits[2], bits[3], float(bits[4]),
                                    float(bits[5]))
            elif act == "mkt":
                await _vp.do_market(query, context, bits[2], bits[3],
                                    float(bits[4]), float(bits[5]))
            elif act == "lim":
                await _vp.ask_price(query, context, "perp",
                                    {"sym": bits[2], "side": bits[3],
                                     "lev": float(bits[4]), "margin": float(bits[5])})
            elif act == "coin":
                await _vp.ask_coin(query, context, bits[2])
            elif act == "sbuy":
                await _vp.do_spot_buy(query, bits[2], float(bits[3]))
            elif act == "sgo":
                await _vp.do_spot_market(query, bits[2], float(bits[3]))
            elif act == "slim":
                await _vp.ask_price(query, context, "spot",
                                    {"sym": bits[2], "quote": float(bits[3])})
            elif act == "sl50":
                await _vp.do_spot_sell(query, bits[2], 50)
            elif act == "sall":
                await _vp.do_spot_sell(query, bits[2], 100)
            elif act == "cl":
                await _vp.do_close(query, bits[2], float(bits[3]))
            elif act == "sl":
                await _vp.ask_sl(query, context, bits[2])
            elif act == "ssl":
                await _vp.ask_spot_sl(query, context, bits[2])
            elif act == "ord":
                await _vp.orders_panel(query)
            elif act == "cx":
                await _vp.do_cancel(query, bits[2])
            else:
                await query.answer("不认识的操作")
        except Exception as e:
            logging.error(f"虚拟交易台出错 {d}: {e}")
            await safe_edit(query, f"操作失败：{str(e)[:80]}",
                            reply_markup=back_to("vg:home"))

    elif d == "vspot_show":
        from handlers import vspot as _vs
        from handlers.vtrade import _acct
        a = _acct(str(query.from_user.id))
        await safe_edit(query, await _vs.render(a),
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 刷新", callback_data="vspot_show"),
                            InlineKeyboardButton("⬅️ 返回", callback_data="cat_vtrade")]]),
                        parse_mode="Markdown")

    elif d == "vord_show":
        from handlers import vorders as _vo
        from handlers.vtrade import _acct, get_prices
        a = _acct(str(query.from_user.id))
        syms = {o["sym"] for o in a.get("orders", [])}
        prices = {}
        if syms:
            try:
                prices = await get_prices(list(syms))
            except Exception as e:
                logging.warning(f"挂单面板取价失败: {e}")
        await safe_edit(query, _vo.render(a, prices),
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("🔄 刷新", callback_data="vord_show"),
                            InlineKeyboardButton("⬅️ 返回", callback_data="cat_vtrade")]]),
                        parse_mode="Markdown")

    elif d == "vpos_refresh":
        from handlers.vtrade import render_vpos
        try:
            await render_vpos(query)
        except Exception as e:
            logging.error(f"虚拟持仓刷新出错: {e}")
            await safe_edit(query, f"刷新失败，稍后再试：{str(e)[:80]}", reply_markup=back_to("cat_vtrade"))

    elif d == "vhist_show":
        from handlers.vtrade import render_vhist
        try:
            await render_vhist(query)
        except Exception as e:
            logging.error(f"虚拟历史出错: {e}")
            await safe_edit(query, f"查询失败，稍后再试：{str(e)[:80]}", reply_markup=back_to("cat_vtrade"))

    # ---- 实盘交易说明卡（仅文字，下单须手打命令+确认，防误触）----
    elif d == "cat_rtrade":
        from bybit_trade import _is_testnet
        env = "🧪 当前模拟盘(testnet)" if _is_testnet() else "🔴 当前实盘(动真钱)"
        rtkb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🎛 打开交易台（推荐，点按钮操作）", callback_data="tpanel")],
            [InlineKeyboardButton("📊 实盘复盘", callback_data="rsd:30"),
             InlineKeyboardButton("🛡 风险守护", callback_data="rgpanel")],
            [InlineKeyboardButton("🌅 AI 盘前简报", callback_data="brnow")],
            [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
        ])
        await safe_edit(query, 
            "🔴 *Bybit 实盘交易*（管理员·私聊·真金白银）\n"
            f"{env}\n\n"
            "💡 记不住命令就点【🎛 交易台】，开仓/平仓/改止损/预警全是按钮。\n\n"
            "*或手打命令：*\n"
            "`/ropen BTC long 1000 10 62000 sl=60000 tp=68000`\n"
            "　限价开仓（保证金1000U·10x·价62000·带止盈止损），弹确认再下\n"
            "`/rclose BTC` 市价全平　`/rclose BTC 50` 平一半　`/rclose BTC 100 63000` 限价平\n"
            "`/rpos` 实盘持仓（入场/爆仓价/浮盈直读交易所）\n"
            "`/rtpsl BTC tp=68000 sl=61000` 改已有仓位止盈止损（清除填0）\n"
            "`/rliqalert 5` 爆仓预警：距爆仓≤5%推送（`off`关）\n"
            "`/rbal` 合约余额　`/rorders BTC` 挂单　`/rcancel BTC` 撤单\n\n"
            "*📊 复盘 / 风控*\n"
            "`/rstats 30` 成绩单：胜率·盈亏比·期望值·最大回撤，按币/多空/持仓时长/时段拆\n"
            "`/rstats 30 ai` AI 从你的数字里挑行为漏洞\n"
            "`/risk` 风险守护　`/brief` AI 盘前简报\n\n"
            "⚠️ 平仓强制 reduceOnly 只减不反开；先在模拟盘验证再上实盘\n"
            "（切换：服务器 .env 的 `BYBIT_TESTNET` true/false）",
            reply_markup=rtkb, parse_mode="Markdown")

    # ---- AI 助手：点按钮 → 进入 AI 问答会话（连续聊，直到退出）----
    elif d == "ask_start":
        if query.message.chat.type in ("group", "supergroup"):
            # 群里直接 @我 / 回复我就能连续对话，不需要会话开关
            await safe_edit(query, 
                "💬 *AI 助手*\n\n群里直接 **@我** 或 **回复我的消息** 就能连续对话，"
                "能查实时币价/资金费/涨跌榜/情绪来答。\n例：`@我 BTC 做空挂单区间给我拆一下`",
                reply_markup=back_kb(), parse_mode="Markdown")
        else:
            context.user_data["ai_session"] = True
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🚪 退出 AI 问答", callback_data="ask_stop")]])
            await safe_edit(query, 
                "💬 *已进入 AI 问答*（连续对话）\n\n直接发问题，可以一直追问，我记得上下文。"
                "需要实时数据我会自己查（币价/合约资金费/涨跌榜/情绪）。\n\n"
                "例：`做空 BTC 挂单区间给我拆一下`　`那如果改15分钟短线呢`\n\n"
                "退出：点下方按钮或发 /menu。",
                reply_markup=kb, parse_mode="Markdown")

    elif d == "ask_stop":
        context.user_data.pop("ai_session", None)
        await safe_edit(query, "已退出 AI 问答。发 /menu 打开菜单。", reply_markup=back_kb())

    # ---- 交易台 / 引导式开仓 / 一键持仓操作 ----
    elif d == "tpanel":
        from handlers import rtrade
        await rtrade.panel_edit(query, context)
    elif d == "topen":
        from handlers import rtrade
        await rtrade.guided_open_coins(query)
    elif d == "topother":
        from handlers import rtrade
        await rtrade.guided_other(query, context)
    elif d.startswith("tops:"):
        from handlers import rtrade
        await rtrade.guided_dir(query, d.split(":", 1)[1])
    elif d.startswith("topd:"):
        from handlers import rtrade
        _, sym, side = d.split(":")
        await rtrade.guided_lev(query, sym, side)
    elif d.startswith("topm:"):
        _, sym, side, lev, mgn = d.split(":")
        await rtrade.guided_price(query, context, sym, side, lev, mgn)

    elif d.startswith("topx:"):
        _, sym, side, lev = d.split(":")
        await rtrade.guided_amount(query, context, sym, side, lev)

    elif d.startswith("topl:"):
        from handlers import rtrade
        _, sym, side, lev = d.split(":")
        await rtrade.guided_margin(query, context, sym, side, lev)
    elif d.startswith("tcls:"):
        from handlers import rtrade
        _, sym, pct = d.split(":")
        await rtrade.close_from_btn(query, context, sym, float(pct))
    elif d.startswith("tsl:"):
        from handlers import rtrade
        await rtrade.ask_sl(query, context, d.split(":", 1)[1])
    elif d == "tliq":
        from handlers import rtrade
        await rtrade.liq_menu(query)
    elif d.startswith("tliqset:"):
        from handlers import rtrade
        await rtrade.liq_set(query, context, d.split(":", 1)[1])

    # ---- 实盘复盘 / 风险守护 / 盘前简报 ----
    elif d.startswith("rsd:"):          # 复盘：切换统计天数
        from handlers import rstats
        await rstats.days_from_btn(query, context, int(d.split(":", 1)[1]))
    elif d.startswith("rsai:"):         # 复盘：AI 诊断
        from handlers import rstats
        await rstats.ai_from_btn(query, context, int(d.split(":", 1)[1]))
    elif d == "rgpanel":
        from handlers import riskguard
        text, kb = riskguard.panel_content()
        await safe_edit(query, text, reply_markup=kb, parse_mode="Markdown")
    elif d == "rgtog":                  # 风险守护总开关
        from handlers import riskguard
        await riskguard.toggle(query, context)
    elif d.startswith("rgc:"):          # 单项检查开关
        from handlers import riskguard
        await riskguard.toggle_check(query, context, d.split(":", 1)[1])
    elif d.startswith("rgset:"):        # 阈值
        from handlers import riskguard
        _, key, val = d.split(":")
        await riskguard.set_threshold(query, context, key, val)
    elif d == "brtog":                  # 盘前简报每日推送开关
        from handlers import brief
        await brief.toggle(query, context)
    elif d == "brnow":
        from handlers import brief
        await brief.now_from_btn(query, context)

    # ---- 资金费率极端榜 ----
    elif d == "fex":
        from handlers import fundextreme
        await fundextreme.fex_from_btn(query, context)
    elif d.startswith("fexsub:"):
        # 只弹 toast 不重渲染——重渲染要重扫全市场，为了一个订阅开关不值得
        from handlers import fundextreme
        await fundextreme.sub_from_btn(query, context, d.split(":", 1)[1])

    # ---- 持仓驾驶舱 ----
    elif d == "ckpt":
        from handlers import cockpit
        await cockpit.from_btn(query, context)

    # ---- 交易计划 ----
    elif d.startswith("pl:"):
        from handlers import plan as _plan
        bits = d.split(":")
        # action 之后可能不止一段（如 pl:filll:p3:10），整体传给 button 自己拆
        await _plan.button(query, context, bits[1],
                           ":".join(bits[2:]) if len(bits) > 2 else None)

    # ---- 仓位计算：换风险档位重算 ----
    elif d.startswith("sz:"):
        from handlers import sizing
        _, sym, entry, stop, risk = d.split(":")
        await sizing.from_btn(query, context, sym, float(entry), float(stop), float(risk))

    # ---- 标注图表：切周期 / AI 解读 ----
    elif d.startswith("ac:"):
        from handlers import annotchart
        _, sym, iv = d.split(":")
        await annotchart.from_btn(query, context, sym, iv)
    elif d.startswith("acai:"):
        from handlers import annotchart
        _, sym, iv = d.split(":")
        await annotchart.ai_from_btn(query, context, sym, iv)

    # ---- 条件提醒用法卡（条件语法太自由，设置仍走命令）----
    elif d == "cond_help":
        from handlers.condalert import USAGE
        await safe_edit(query, USAGE, reply_markup=back_to("cat_alert"),
                        parse_mode="Markdown")

    # ---- 实盘开仓二次确认 ----
    elif d == "roconf":
        from handlers.rtrade import confirm_open
        try:
            await confirm_open(query, context)
        except Exception as e:
            logging.error(f"实盘确认下单出错: {e}")
            await safe_edit(query, f"❌ 下单异常：{e}")
    elif d == "rocancel":
        from handlers.rtrade import cancel_open
        await cancel_open(query, context)

    # ============ 帮助 ============
    elif d == "cat_help":
        from config import VERSION as _V
        await safe_edit(query, 
            "❓ *使用帮助*\n\n"
            "📊 行情 - 几百种币实时价格\n"
            "📈 分析 - 技术指标+AI解读\n"
            "🔔 预警 - 到价自动提醒\n"
            "🛠 工具 - 比价/情绪/Gas/巨鲸\n"
            "💼 持仓 - 记录盈亏(私聊)\n\n"
            "💡 随时发 /menu 打开\n"
            f"📦 当前版本 `{_V}`\n"
            "⚠️ 数据仅供参考，不构成投资建议",
            reply_markup=InlineKeyboardMarkup([
                # 完整指南（命令 + 按钮在哪 + 怎么用）——群里发 /howto 也能调出来
                [InlineKeyboardButton("📖 怎么用（完整指南）", callback_data="howto")],
                [InlineKeyboardButton(f"📋 {_V} 更新了什么", callback_data="cl:cur")],
                [InlineKeyboardButton("⬅️ 返回主菜单", callback_data="menu_main")],
            ]), parse_mode="Markdown")

    elif d == "howto":
        from handlers import howto as _ht
        priv = query.message.chat.type == "private" if query.message else True
        await safe_edit(query, _ht.TEXT, reply_markup=_ht.kb(priv),
                        parse_mode="Markdown", disable_web_page_preview=True)
