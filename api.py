import httpx
import time
import asyncio
from config import COIN_IDS

BASE = "https://api.coingecko.com/api/v3"

# ---------- 缓存 + 限流 ----------
_cache = {}           # url+params -> (timestamp, data)
_cache_ttl = 60       # 缓存60秒
_last_call = [0]      # 上次调用时间
_min_interval = 2.0   # 两次调用最小间隔2秒
_lock = asyncio.Lock()

async def _get(path, params):
    # 缓存key
    key = path + str(sorted(params.items()))
    now = time.time()
    # 命中缓存
    if key in _cache:
        ts, data = _cache[key]
        if now - ts < _cache_ttl:
            return data
    # 限流：保证两次真实调用间隔
    async with _lock:
        wait = _min_interval - (time.time() - _last_call[0])
        if wait > 0:
            await asyncio.sleep(wait)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{BASE}{path}", params=params)
            resp.raise_for_status()
            data = resp.json()
        _last_call[0] = time.time()
        _cache[key] = (now, data)
        return data

async def get_price(symbol: str, vs: str = "usd"):
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    raw = await _get("/simple/price", {
        "ids": coin_id, "vs_currencies": vs, "include_24hr_change": "true"
    })
    info = raw.get(coin_id)
    if not info or vs not in info:
        return None
    return {"price": info[vs], "change": info.get(f"{vs}_24h_change", 0)}

async def get_prices(symbols, vs: str = "usd"):
    """批量查：返回 {symbol: {price, change}}"""
    ids = [COIN_IDS[s] for s in symbols if s in COIN_IDS]
    if not ids:
        return {}
    raw = await _get("/simple/price", {
        "ids": ",".join(ids), "vs_currencies": vs, "include_24hr_change": "true"
    })
    id_to_sym = {v: k for k, v in COIN_IDS.items()}
    result = {}
    for cid, info in raw.items():
        sym = id_to_sym.get(cid)
        # 缺 symbol 映射或缺该计价货币字段的跳过，避免整批 KeyError
        if sym is None or vs not in info:
            continue
        result[sym] = {"price": info[vs], "change": info.get(f"{vs}_24h_change", 0)}
    return result

# 兼容旧代码：保留返回 usd 字段的版本（alert/portfolio/broadcast 用）
async def get_prices_usd(symbols):
    data = await get_prices(symbols, "usd")
    return {s: {"usd": v["price"], "change": v["change"]} for s, v in data.items()}

async def get_market_data(symbols):
    """获取市值/成交量等详细数据：返回 {symbol: {...}}"""
    ids = [COIN_IDS[s] for s in symbols if s in COIN_IDS]
    if not ids:
        return {}
    raw = await _get("/coins/markets", {
        "vs_currency": "usd",
        "ids": ",".join(ids),
        "order": "market_cap_desc",
        "price_change_percentage": "24h,7d,30d",
    })
    result = {}
    for c in raw:
        # 反查 symbol
        sym = c["symbol"].upper()
        result[sym] = {
            "price": c["current_price"],
            "market_cap": c["market_cap"],
            "market_cap_rank": c["market_cap_rank"],
            "volume": c["total_volume"],
            "change_24h": c.get("price_change_percentage_24h_in_currency") or 0,
            "change_7d": c.get("price_change_percentage_7d_in_currency") or 0,
            "change_30d": c.get("price_change_percentage_30d_in_currency") or 0,
            "high_24h": c["high_24h"],
            "low_24h": c["low_24h"],
            "ath": c.get("ath"),
            "ath_change": c.get("ath_change_percentage"),
            "circ_supply": c.get("circulating_supply"),
            "total_supply": c.get("total_supply"),
            "fdv": c.get("fully_diluted_valuation"),
        }
    return result

async def get_top_movers(limit=10):
    """涨跌榜：取市值前100的币，按24h涨跌排序"""
    raw = await _get("/coins/markets", {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": 100,
        "page": 1,
        "price_change_percentage": "24h",
    })
    coins = [{
        "symbol": c["symbol"].upper(),
        "price": c["current_price"],
        "change": c.get("price_change_percentage_24h") or 0,
    } for c in raw]
    gainers = sorted(coins, key=lambda x: x["change"], reverse=True)[:limit]
    losers = sorted(coins, key=lambda x: x["change"])[:limit]
    return gainers, losers

async def get_market_leaders(limit=22):
    """按市值取前N的币（含价格+24h涨跌），用于市场看板。多取几个方便调用方过滤稳定币。"""
    raw = await _get("/coins/markets", {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": limit,
        "page": 1,
        "price_change_percentage": "24h",
    })
    return [{
        "symbol": c["symbol"].upper(),
        "price": c["current_price"],
        "change": c.get("price_change_percentage_24h") or 0,
    } for c in raw]

async def get_market_chart(symbol: str, days: int = 7):
    """获取历史价格：返回 [(timestamp_ms, price), ...]"""
    from config import COIN_IDS
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    raw = await _get(f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd",
        "days": days,
    })
    return raw.get("prices", [])  # [[ts, price], ...]

async def get_fear_greed():
    """恐惧贪婪指数（来自 alternative.me）"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get("https://api.alternative.me/fng/?limit=1")
        resp.raise_for_status()
        d = resp.json()
        item = d["data"][0]
        return {
            "value": int(item["value"]),
            "classification": item["value_classification"],
        }

async def get_gas_price():
    """以太坊 gas 价格（公共RPC，返回 gwei）"""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://ethereum-rpc.publicnode.com",
            json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1},
        )
        resp.raise_for_status()
        wei_hex = resp.json()["result"]
        wei = int(wei_hex, 16)
        gwei = wei / 1e9
        return gwei

# 多链 gas（各链公共RPC，返回 gwei）
GAS_CHAINS = [
    ("ETH", "https://ethereum-rpc.publicnode.com"),
    ("Arbitrum", "https://arbitrum-one-rpc.publicnode.com"),
    ("Optimism", "https://optimism-rpc.publicnode.com"),
    ("Base", "https://base-rpc.publicnode.com"),
    ("Polygon", "https://polygon-bor-rpc.publicnode.com"),
    ("BSC", "https://bsc-rpc.publicnode.com"),
]

async def _gas_of(client, rpc):
    try:
        resp = await client.post(rpc, json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1})
        resp.raise_for_status()
        return int(resp.json()["result"], 16) / 1e9
    except Exception:
        return None

async def get_gas_multi():
    """并发拿多链 gas，返回 [(链名, gwei或None), ...]"""
    async with httpx.AsyncClient(timeout=10) as client:
        results = await asyncio.gather(*[_gas_of(client, rpc) for _, rpc in GAS_CHAINS])
    return [(GAS_CHAINS[i][0], results[i]) for i in range(len(GAS_CHAINS))]

async def fetch_top_coins(limit=250):
    """拉取市值前N的币，构建 symbol -> id 映射"""
    raw = await _get("/coins/markets", {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": limit,
        "page": 1,
    })
    mapping = {}
    for c in raw:
        sym = c["symbol"].upper()
        # 同名符号保留市值高的（先到的，因为按市值降序）
        if sym not in mapping:
            mapping[sym] = c["id"]
    return mapping

async def get_daily_prices(symbol: str, days: int = 35):
    """获取日线价格序列（用于技术指标计算），返回价格列表"""
    from config import COIN_IDS
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    raw = await _get(f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd",
        "days": days,
        "interval": "daily",
    })
    prices = raw.get("prices", [])
    return [p[1] for p in prices]  # 只要价格

async def get_daily_ohlc_prices(symbol: str, days: int = 35):
    """日线价格带时间戳，用于画技术分析图"""
    from config import COIN_IDS
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    raw = await _get(f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd", "days": days, "interval": "daily",
    })
    return raw.get("prices", [])  # [[ts, price], ...]

async def get_prices_by_period(symbol: str, days: int):
    """按天数取价格序列（不同days对应不同粒度）"""
    from config import COIN_IDS
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    raw = await _get(f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd", "days": days,
    })
    return [p[1] for p in raw.get("prices", [])]

async def get_ohlc(symbol: str, days: int = 30):
    """获取OHLC蜡烛数据：返回 [[ts,open,high,low,close], ...]"""
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    raw = await _get(f"/coins/{coin_id}/ohlc", {"vs_currency": "usd", "days": days})
    return raw  # [[ts,o,h,l,c], ...]

async def get_volumes(symbol: str, days: int = 14):
    """获取成交量序列"""
    coin_id = COIN_IDS.get(symbol.upper())
    if not coin_id:
        return None
    raw = await _get(f"/coins/{coin_id}/market_chart", {"vs_currency": "usd", "days": days})
    return [v[1] for v in raw.get("total_volumes", [])]


async def get_price_okx(symbol):
    """从 OKX 查价格（CoinGecko查不到时的fallback）"""
    import httpx
    symbol = symbol.upper()
    inst = f"{symbol}-USDT"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": inst}
            )
            resp.raise_for_status()
            d = resp.json()
            if d.get("code") == "0" and d.get("data"):
                t = d["data"][0]
                last = float(t["last"])
                op = float(t["open24h"])
                change = (last - op) / op * 100 if op > 0 else 0
                return {"price": last, "change": change, "source": "OKX"}
    except Exception:
        pass
    return None


async def get_daily_prices_okx(symbol, days=35):
    """从OKX拿历史日线收盘价（CoinGecko没有该币时的fallback）"""
    import httpx
    inst = f"{symbol.upper()}-USDT"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://www.okx.com/api/v5/market/candles",
                params={"instId": inst, "bar": "1D", "limit": str(days)}
            )
            d = resp.json()
            if d.get("code") == "0" and d.get("data"):
                # OKX返回最新在前，每条 [ts,open,high,low,close,...]
                # 取收盘价，反转成时间正序
                closes = [float(c[4]) for c in d["data"]]
                closes.reverse()
                return closes
    except Exception:
        pass
    return None
