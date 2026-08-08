"""账户数据脱敏 —— 别把「你有多少钱」发给中转站。

AI 走的是第三方中转站（OpenAI 兼容中继）。密钥和 token 从不进 prompt，
所以中转站动不了钱；但账户权益、持仓名义、成交盈亏是作为**工具结果**
喂给模型的，中转站全都看得见。真实风险不是丢钱，是财务状况被看到。

关键在于：这些绝对金额对分析**几乎没有增益**。仓位反推用的是百分比，
复盘看的是期望值和盈亏比——把钱换成相对量之后分析质量一点不降，
有些地方反而更好（R 倍数比 USDT 更适合跨时期比较）。

⚠️ 脱敏必须彻底，否则是自欺欺人：给了「名义占权益 40%」就**不能再给数量**，
否则 数量×价格÷40% 就把权益反推出来了。价格本身是公开行情，不用脱敏。
"""
import logging

from storage import data, save_data

log = logging.getLogger(__name__)

DEFAULT_ON = True          # 默认开：外发数据这件事，安全的那一侧才是合理默认


def enabled():
    v = data.get("ai_redact_account")
    return DEFAULT_ON if v is None else bool(v)


def set_enabled(on):
    data["ai_redact_account"] = bool(on)
    save_data()


def pct(value, equity, digits=1):
    """金额 → 占权益百分比。equity 不可用时返回 '?'，绝不回退成绝对值。"""
    try:
        v, e = float(value), float(equity)
    except (TypeError, ValueError):
        return "?"
    if e <= 0:
        return "?"
    return f"{v / e * 100:.{digits}f}%"


def money(value, equity, digits=2):
    """按开关决定给绝对值还是相对值。所有外发金额都该经过这里。"""
    if not enabled():
        try:
            return f"{float(value):,.{digits}f} USDT"
        except (TypeError, ValueError):
            return str(value)
    return pct(value, equity) + "权益"


def note():
    """附在外发数据末尾，让模型知道单位是相对的、别追问绝对金额。"""
    if not enabled():
        return ""
    return ("\n（账户金额已脱敏为占权益百分比，绝对数值不外发。"
            "按百分比推理即可，不要追问具体有多少钱，也不要试图反推。）")


USAGE = (
    "🔒 *AI 数据脱敏*\n"
    "━━━━━━━━━━━━━━\n"
    "AI 走第三方中转站。密钥和 token 从不进 prompt，中转站动不了你的钱；\n"
    "但账户权益、持仓名义、成交盈亏是作为工具结果喂给模型的，它看得见。\n\n"
    "开启后外发的账户金额一律换成**占权益百分比**，绝对数值不出门。\n"
    "分析质量不受影响——仓位反推本来就按百分比，复盘看的是期望值和盈亏比。\n\n"
    "想看真实数字用 `/rbal` `/cockpit` `/rstats`，这些**不经过中转站**。\n"
    "━━━━━━━━━━━━━━\n"
    "`/privacy on` 开启（默认）　`/privacy off` 关闭"
)


async def privacy_cmd(update, context):
    """/privacy [on|off] —— AI 外发账户数据的脱敏开关。"""
    from handlers.util import safe_reply
    from config import is_admin
    if not is_admin(update.effective_user.id):
        await safe_reply(update.message, "仅管理员")
        return
    a = [x.lower() for x in (context.args or [])]
    if a and a[0] in ("on", "开"):
        set_enabled(True)
        await safe_reply(update.message, "🔒 已开启：外发给 AI 的账户金额只给百分比")
        return
    if a and a[0] in ("off", "关"):
        set_enabled(False)
        await safe_reply(update.message,
                         "⚠️ 已关闭：账户绝对金额会随分析请求发给中转站")
        return
    state = "🔒 已开启" if enabled() else "⚠️ 已关闭"
    await safe_reply(update.message, USAGE + f"\n\n当前：{state}",
                     parse_mode="Markdown")
