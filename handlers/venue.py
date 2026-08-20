"""交易所选择 —— 实盘/模拟交易走哪一家。

为什么不复制一套 handlers/binance_trade 命令：
`handlers/rtrade.py` 有 965 行交易逻辑（二次确认、reduceOnly 强制、杠杆护栏、
爆仓预警、审计），全部走一个 `_client()` 取客户端。复制一份等于**同一套风控
维护两遍**，而分叉的那天你会在真钱路径上发现它。
所以这里只做一件事：`client()` 返回**当前选中那家**的客户端，
两个客户端的方法签名完全一致（见 binance_trade 顶部的注释）。

选择存在 data.json，`/venue` 切换。默认 bybit（他原本就在用）。
"""
import logging

from storage import data, save_data

log = logging.getLogger(__name__)

VENUES = {
    "bybit": {"cn": "Bybit", "mod": "bybit_trade", "cls": "BybitClient",
              "env": "BYBIT_TESTNET", "keys": ("BYBIT_API_KEY", "BYBIT_API_SECRET")},
    "binance": {"cn": "币安", "mod": "binance_trade", "cls": "BinanceClient",
                "env": "BINANCE_TESTNET",
                "keys": ("BINANCE_API_KEY", "BINANCE_API_SECRET")},
}
DEFAULT = "bybit"


def current():
    v = data.get("trade_venue") or DEFAULT
    return v if v in VENUES else DEFAULT


def set_venue(name):
    name = (name or "").lower()
    if name not in VENUES:
        return False
    data["trade_venue"] = name
    save_data()
    return True


def _mod(name=None):
    import importlib
    return importlib.import_module(VENUES[name or current()]["mod"])


def client(name=None, **kw):
    """当前交易所的客户端。缺 key 时抛 RuntimeError，上层转成友好提示。"""
    name = name or current()
    m = _mod(name)
    return getattr(m, VENUES[name]["cls"])(**kw)


def mode(name=None):
    """live / demo / testnet —— 各家自己的说法，统一由各自模块给。"""
    return _mod(name)._mode()


def configured(name):
    """这家配了密钥没有。不看这个的话，切过去才发现没 key，白切一次。"""
    import os
    return all(os.environ.get(k) for k in VENUES[name]["keys"])


def tag(name=None):
    """屏幕上的环境标：交易所 + 是不是真钱。两个都要，缺一个都可能拿错账户。"""
    name = name or current()
    cn = VENUES[name]["cn"]
    try:
        m = mode(name)
    except Exception:
        return cn
    label = {"live": "🔴实盘", "demo": "🧪模拟交易",
             "testnet": "🧪测试网"}.get(m, "🧪模拟盘")
    return f"{cn}{label}"


def is_auth_error(e, name=None):
    m = _mod(name)
    fn = getattr(m, "is_auth_error", None)
    return bool(fn and fn(e))


def auth_hint(name=None):
    return getattr(_mod(name), "AUTH_HINT", "")


# ── /venue ──────────────────────────────────────────────────
def _status():
    lines = ["🏦 *交易所*　实盘/模拟交易走哪一家", ""]
    for k, v in VENUES.items():
        mark = "✅ 当前" if k == current() else "　"
        if not configured(k):
            state = "❌ 没配密钥"
        else:
            try:
                state = tag(k)
            except Exception:
                state = "?"
        lines.append(f"{mark} *{v['cn']}*　{state}")
    lines += ["", "`/venue bybit`　`/venue binance` 切换",
              "切换只影响**实盘交易那套**（/trade /rpos /rbal…），",
              "行情数据源是另一个开关：`/source`"]
    return "\n".join(lines)


async def venue_cmd(update, context):
    from handlers.util import safe_reply
    from config import is_admin
    if not is_admin(update.effective_user.id):
        await safe_reply(update.message, "仅管理员")
        return
    args = context.args or []
    if not args:
        await safe_reply(update.message, _status(), parse_mode="Markdown")
        return
    name = args[0].lower()
    if name not in VENUES:
        await safe_reply(update.message, "只支持 `bybit` / `binance`",
                         parse_mode="Markdown")
        return
    if not configured(name):
        k1, k2 = VENUES[name]["keys"]
        await safe_reply(update.message,
            f"❌ {VENUES[name]['cn']} 还没配密钥。\n"
            f"在服务器 .env 里加 {k1} / {k2}，"
            f"再 `docker compose up -d --force-recreate`。\n"
            f"配完先跑一次端点探测确认 key 属于哪套环境：\n"
            f"`docker compose exec crypto-bot python "
            f"{VENUES[name]['mod']}.py probe`", parse_mode="Markdown")
        return
    set_venue(name)
    await safe_reply(update.message,
        f"✅ 已切到 *{VENUES[name]['cn']}*　{tag(name)}\n\n"
        f"/trade 交易台、/rpos /rbal /rorders 现在都走这一家。\n"
        f"先发 `/keycheck` 核一遍权限再下单。", parse_mode="Markdown")
