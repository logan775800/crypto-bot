"""
Bybit V5 私有交易客户端（永续合约 linear / USDT 本位）。

只用 httpx + 标准库 hmac，不引入 ccxt。
- 通过环境变量 BYBIT_TESTNET 切换 模拟盘 / 实盘（默认 True = 模拟盘，安全优先）。
- 所有私有接口用 HMAC-SHA256 按 Bybit V5 规范签名：
    sign = HMAC_SHA256(secret, timestamp + api_key + recv_window + (queryString 或 body))

⚠️ 这是会真实下单/撤单的模块。先在模拟盘（testnet）跑通再切实盘。
   直接运行本文件可做「连通+签名」冒烟自测：python bybit_trade.py
"""
import os
import time
import hmac
import hashlib
import json
import logging
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

import httpx

# ── 环境：默认模拟盘，务必先在模拟盘验证 ────────────────────────────────
# BYBIT_TESTNET 只要不是显式的 "false/0/no"，一律当作模拟盘（防手滑上实盘）
def _is_testnet():
    return os.environ.get("BYBIT_TESTNET", "true").strip().lower() not in ("false", "0", "no")

BYBIT_API_KEY = os.environ.get("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.environ.get("BYBIT_API_SECRET", "")

RECV_WINDOW = "5000"


def _base_url():
    return "https://api-testnet.bybit.com" if _is_testnet() else "https://api.bybit.com"


class BybitError(Exception):
    """Bybit 返回 retCode != 0 时抛出，带上 retCode / retMsg 方便定位。"""
    def __init__(self, ret_code, ret_msg, endpoint=""):
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        super().__init__(f"[Bybit {ret_code}] {ret_msg} ({endpoint})")


class BybitClient:
    """Bybit V5 客户端。category 固定 linear（USDT 永续）。"""

    def __init__(self, api_key=None, api_secret=None, category="linear"):
        self.api_key = api_key or BYBIT_API_KEY
        self.api_secret = api_secret or BYBIT_API_SECRET
        self.category = category
        if not self.api_key or not self.api_secret:
            raise RuntimeError("缺少 BYBIT_API_KEY / BYBIT_API_SECRET，请在 .env 配置")

    # ── 签名 & 请求 ──────────────────────────────────────────────
    def _headers(self, payload_str):
        ts = str(int(time.time() * 1000))
        origin = ts + self.api_key + RECV_WINDOW + payload_str
        sign = hmac.new(
            self.api_secret.encode(), origin.encode(), hashlib.sha256
        ).hexdigest()
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign,
            "Content-Type": "application/json",
        }

    async def _get(self, path, params=None, signed=True):
        params = params or {}
        # Bybit 要求签名用的 query 与实际发送完全一致，这里按 key 排序统一构造
        qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        url = f"{_base_url()}{path}" + (f"?{qs}" if qs else "")
        headers = self._headers(qs) if signed else {}
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return self._unwrap(resp.json(), path)

    async def _post(self, path, body):
        body_str = json.dumps(body, separators=(",", ":"))  # 紧凑串，签名与发送一致
        headers = self._headers(body_str)
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{_base_url()}{path}", headers=headers, content=body_str)
            resp.raise_for_status()
            return self._unwrap(resp.json(), path)

    @staticmethod
    def _unwrap(data, endpoint, tolerate=()):
        code = data.get("retCode")
        if code != 0 and code not in tolerate:
            raise BybitError(code, data.get("retMsg", ""), endpoint)
        return data.get("result", {})

    # ── 公开行情（无需签名）────────────────────────────────────────
    async def instrument_info(self, symbol):
        """返回该合约的下单精度：tickSize（价格步长）/ qtyStep（数量步长）/ 最小下单量。"""
        r = await self._get(
            "/v5/market/instruments-info",
            {"category": self.category, "symbol": symbol}, signed=False,
        )
        lst = r.get("list") or []
        if not lst:
            raise RuntimeError(f"未找到合约 {symbol}（注意 Bybit 永续格式如 BTCUSDT）")
        it = lst[0]
        return {
            "tickSize": it["priceFilter"]["tickSize"],
            "qtyStep": it["lotSizeFilter"]["qtyStep"],
            "minOrderQty": it["lotSizeFilter"]["minOrderQty"],
        }

    async def last_price(self, symbol):
        r = await self._get(
            "/v5/market/tickers",
            {"category": self.category, "symbol": symbol}, signed=False,
        )
        return float(r["list"][0]["lastPrice"])

    # ── 私有：账户 / 仓位 ─────────────────────────────────────────
    async def wallet_balance(self, coin="USDT"):
        r = await self._get(
            "/v5/account/wallet-balance",
            {"accountType": "UNIFIED", "coin": coin},
        )
        lst = r.get("list") or []
        if not lst:
            return {}
        return lst[0]

    async def position(self, symbol):
        r = await self._get(
            "/v5/position/list", {"category": self.category, "symbol": symbol}
        )
        lst = r.get("list") or []
        return lst[0] if lst else {}

    async def set_leverage(self, symbol, leverage):
        try:
            return await self._post(
                "/v5/position/set-leverage",
                {"category": self.category, "symbol": symbol,
                 "buyLeverage": str(leverage), "sellLeverage": str(leverage)},
            )
        except BybitError as e:
            # 110043 = 杠杆未变化，视为成功；其余抛出
            if e.ret_code == 110043:
                return {}
            raise

    # ── 私有：订单 ───────────────────────────────────────────────
    async def place_limit(self, symbol, side, qty, price, link_id=None):
        """挂限价单。side: 'Buy' / 'Sell'。qty/price 需已按步长取整。返回含 orderId。"""
        body = {
            "category": self.category, "symbol": symbol, "side": side,
            "orderType": "Limit", "qty": str(qty), "price": str(price),
            "timeInForce": "GTC", "positionIdx": 0, "reduceOnly": False,
        }
        if link_id:
            body["orderLinkId"] = link_id
        return await self._post("/v5/order/create", body)

    async def cancel(self, symbol, order_id=None, link_id=None):
        body = {"category": self.category, "symbol": symbol}
        if order_id:
            body["orderId"] = order_id
        if link_id:
            body["orderLinkId"] = link_id
        try:
            return await self._post("/v5/order/cancel", body)
        except BybitError as e:
            # 110001 = 订单不存在（可能已成交/已撤），容忍
            if e.ret_code == 110001:
                return {}
            raise

    async def cancel_all(self, symbol):
        return await self._post(
            "/v5/order/cancel-all", {"category": self.category, "symbol": symbol}
        )

    async def open_orders(self, symbol):
        """当前挂着的（未成交）订单列表。"""
        r = await self._get(
            "/v5/order/realtime",
            {"category": self.category, "symbol": symbol, "openOnly": 0},
        )
        return r.get("list") or []

    async def order_status(self, symbol, link_id):
        """按 orderLinkId 查最终状态（用于判断是否 Filled）。查历史，成交后仍可查到。"""
        r = await self._get(
            "/v5/order/history",
            {"category": self.category, "symbol": symbol, "orderLinkId": link_id},
        )
        lst = r.get("list") or []
        return lst[0] if lst else {}


# ── 精度工具：把价格/数量按交易所步长取整 ──────────────────────────────
def round_step(value, step, mode=ROUND_DOWN):
    """把 value 按 step 步长取整，返回字符串（保留 step 的小数位，避免科学计数/多余0）。"""
    v, s = Decimal(str(value)), Decimal(str(step))
    q = (v / s).to_integral_value(rounding=mode) * s
    return format(q.quantize(s, rounding=ROUND_HALF_UP), "f")


# ── 冒烟自测：验证签名 & 连通，只读不下单 ──────────────────────────────
async def _smoke():
    env = "模拟盘 TESTNET" if _is_testnet() else "⚠️ 实盘 LIVE"
    print(f"环境: {env}  base={_base_url()}")
    c = BybitClient()
    print("→ 校验签名：查 USDT 余额 ...")
    bal = await c.wallet_balance("USDT")
    equity = bal.get("totalEquity", "?")
    print(f"   ✅ 签名有效。账户总权益 totalEquity = {equity}")
    print("→ 查 BTCUSDT 下单精度 ...")
    info = await c.instrument_info("BTCUSDT")
    print(f"   tickSize={info['tickSize']} qtyStep={info['qtyStep']} minQty={info['minOrderQty']}")
    px = await c.last_price("BTCUSDT")
    print(f"   BTCUSDT 最新价 = {px}")
    print("冒烟自测通过 ✅（未下任何单）")


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(_smoke())
    except Exception as e:
        print(f"❌ 自测失败: {e}")
