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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from handlers.util import safe_reply

# 指南跟着代码一起版本管理，改功能时顺手改它，链接不变
GUIDE_URL = "https://github.com/logan775800/crypto-bot/blob/main/docs/guide.md"

TEXT = (
    "📖 *机器人怎么用*\n\n"
    "最快上手：**直接发币名就查价**，比如 `BTC`\n"
    "记不住命令就发 /menu，或 /commands 看全部命令（点了直接执行）\n\n"
    "常用的几个：\n"
    "📅 `/rank 3` 　3日/7日涨跌榜\n"
    "⚖️ `/lsr` 　　多空比极值榜，最被看多/看空各 3 个\n"
    "💣 `/liqmap TRUMP` 　清算地图（模型估算，不是交易所数据）\n"
    "🎮 `/vtrade` 　虚拟盘练手，1 万 U 起步（**私聊我**发）\n"
    "🔗 `/oc BANK` 　查链上代币，交易所没上的币也能查\n\n"
    f"完整指南（命令 + 按钮在哪 + 怎么用）：\n{GUIDE_URL}\n\n"
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
