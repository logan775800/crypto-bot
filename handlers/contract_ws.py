"""OKX / Bybit 永续合约行情 WebSocket 实时流：价格穿过阈值秒级触发合约告警。

为什么只有 OKX/Bybit：币安合约 WS(fstream.binance.com)在本部署网络不可达，
币安改由 handlers/contract_alert.py 的 REST 轮询兜底。

判档去重与推送复用 contract_alert 的 eval_tier_cross / push_to_subscribers，
所以 WS 与 REST 轮询写同一份台阶记录，不会重复告警。

命中先进缓冲区，由 flush 循环每几秒合并成一条群消息发出——既保证秒级时效，
又避免多个币同时穿档时刷屏。
"""
import time
import json
import asyncio
import logging
import httpx

from storage import data, save_data
from handlers.contract_alert import MIN_TURNOVER, eval_tier_cross, push_to_subscribers
from handlers import watchpct

# WS 里 OKX/Bybit 的 tick 都是永续，映射成波动监控里的来源名
_WS_SRC = {"OKX": "OKX永续", "Bybit": "Bybit永续"}

OKX_WS = "wss://ws.okx.com:8443/ws/v5/public"
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"
OKX_BASE = "https://www.okx.com"
BYBIT_BASE = "https://api.bybit.com"

FLUSH_INTERVAL = 4        # 秒：缓冲区多久合并发一次
RESUB_INTERVAL = 6 * 3600  # 秒：每隔多久重连一次以刷新合约列表(捕捉新上市)

_pending = []             # 待推送告警缓冲
_bot = None


async def _send_wp(chat_id, text):
    try:
        await _bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        save_data()   # 基准/冷却已在 on_tick 里更新，落盘
    except Exception as e:
        logging.error(f"波动监控实时推送失败 {chat_id}: {e}")


def _queue(ex, sym, change, price):
    """波动监控实时命中即时推送（不依赖合约订阅）；合约异动分级则需订阅。"""
    # 1) 持续波动监控：秒级 tick 命中就立刻发（与 /watchcontract 订阅无关）
    try:
        hits = watchpct.on_tick(_WS_SRC.get(ex, ex), sym, price)
        for chat_id, text in hits:
            asyncio.ensure_future(_send_wp(chat_id, text))
    except Exception as e:
        logging.error(f"波动监控实时检查出错: {e}")

    # 2) 合约异动分级告警：有订阅者才判档入队
    if not data.get("contract_watch"):
        return
    tier = eval_tier_cross(ex, sym, change)
    if tier:
        _pending.append({"ex": ex, "sym": sym, "change": change, "price": price,
                         "tier": tier, "direction": "up" if change > 0 else "down"})


# ---------- 合约列表（REST，(re)connect 时刷新）----------
async def _okx_instruments():
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get(f"{OKX_BASE}/api/v5/public/instruments", params={"instType": "SWAP"})
        d = r.json()
    return [x["instId"] for x in d.get("data", [])
            if x.get("instId", "").endswith("-USDT-SWAP") and x.get("state") == "live"]


async def _bybit_instruments():
    async with httpx.AsyncClient(timeout=12) as c:
        r = await c.get(f"{BYBIT_BASE}/v5/market/instruments-info", params={"category": "linear"})
        d = r.json()
    out = []
    for x in d.get("result", {}).get("list", []):
        if (x.get("quoteCoin") == "USDT" and x.get("contractType") == "LinearPerpetual"
                and x.get("status") == "Trading"):
            out.append(x["symbol"])
    return out


# ---------- OKX 实时循环 ----------
async def _okx_loop():
    import websockets
    while True:
        try:
            insts = await _okx_instruments()
            if not insts:
                await asyncio.sleep(30)
                continue
            async with websockets.connect(OKX_WS, open_timeout=15, ping_interval=None,
                                          max_size=None, close_timeout=5) as ws:
                for i in range(0, len(insts), 50):
                    args = [{"channel": "tickers", "instId": x} for x in insts[i:i + 50]]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                logging.info(f"OKX WS 已订阅 {len(insts)} 个永续合约")
                deadline = time.time() + RESUB_INTERVAL
                while time.time() < deadline:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=25)
                    except asyncio.TimeoutError:
                        await ws.send("ping")     # OKX：静默>30s会断，主动心跳
                        continue
                    if not msg or msg[0] != "{":  # "pong" 等非 JSON
                        continue
                    d = json.loads(msg)
                    if d.get("arg", {}).get("channel") != "tickers" or not d.get("data"):
                        continue
                    for t in d["data"]:
                        try:
                            last = float(t["last"]); op = float(t["open24h"])
                            if op <= 0:
                                continue
                            change = (last - op) / op * 100
                            turnover = float(t.get("volCcy24h", 0) or 0) * last
                            if turnover < MIN_TURNOVER:
                                continue
                            _queue("OKX", t["instId"][:-len("-USDT-SWAP")], change, last)
                        except (ValueError, KeyError):
                            continue
        except Exception as e:
            logging.warning(f"OKX WS 断开重连: {type(e).__name__}: {str(e)[:90]}")
            await asyncio.sleep(5)


# ---------- Bybit 实时循环 ----------
async def _bybit_loop():
    import websockets
    while True:
        try:
            syms = await _bybit_instruments()
            if not syms:
                await asyncio.sleep(30)
                continue
            async with websockets.connect(BYBIT_WS, open_timeout=15, ping_interval=None,
                                          max_size=None, close_timeout=5) as ws:
                for i in range(0, len(syms), 10):
                    await ws.send(json.dumps({"op": "subscribe",
                                              "args": [f"tickers.{s}" for s in syms[i:i + 10]]}))
                logging.info(f"Bybit WS 已订阅 {len(syms)} 个永续合约")

                async def _keepalive():
                    while True:
                        await asyncio.sleep(20)   # Bybit：需每20s发ping
                        await ws.send(json.dumps({"op": "ping"}))
                ka = asyncio.ensure_future(_keepalive())

                state = {}   # sym -> 合并后的最新字段（Bybit delta 只带变化字段）
                deadline = time.time() + RESUB_INTERVAL
                try:
                    while time.time() < deadline:
                        msg = await asyncio.wait_for(ws.recv(), timeout=40)
                        d = json.loads(msg)
                        topic = d.get("topic", "")
                        if not topic.startswith("tickers.") or not d.get("data"):
                            continue
                        t = d["data"]
                        sym = t.get("symbol") or topic.split(".", 1)[1]
                        st = state.setdefault(sym, {})
                        st.update(t)              # 合并 snapshot/delta
                        if "lastPrice" not in st or "price24hPcnt" not in st:
                            continue
                        try:
                            last = float(st["lastPrice"]); change = float(st["price24hPcnt"]) * 100
                            turnover = float(st.get("turnover24h", 0) or 0)
                            if turnover and turnover < MIN_TURNOVER:
                                continue
                            base = sym[:-4] if sym.endswith("USDT") else sym
                            _queue("Bybit", base, change, last)
                        except (ValueError, KeyError):
                            continue
                finally:
                    ka.cancel()
        except Exception as e:
            logging.warning(f"Bybit WS 断开重连: {type(e).__name__}: {str(e)[:90]}")
            await asyncio.sleep(5)


# ---------- 缓冲区定时合并推送 ----------
async def _flush_loop():
    global _pending
    while True:
        await asyncio.sleep(FLUSH_INTERVAL)
        if not _pending:
            continue
        batch = _pending
        _pending = []
        save_data()   # 台阶记录已由 eval_tier_cross 更新，落盘
        try:
            await push_to_subscribers(_bot, batch)
        except Exception as e:
            logging.error(f"合约WS告警推送失败: {e}")


def start(application):
    """在 bot 启动(post_init)时调用：拉起 OKX/Bybit 实时流与推送循环。

    websockets 缺失或环境不支持时安全跳过，REST 轮询仍兜底。
    """
    global _bot
    try:
        import websockets  # noqa: F401
    except ImportError:
        logging.warning("未安装 websockets，合约实时告警降级为纯 REST 轮询")
        return
    _bot = application.bot
    application.create_task(_okx_loop())
    application.create_task(_bybit_loop())
    application.create_task(_flush_loop())
    logging.info("合约实时告警(WebSocket) 已启动：OKX + Bybit")
