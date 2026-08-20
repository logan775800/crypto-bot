"""币安（Binance）私有交易客户端 —— USDT 本位合约 + 现货。

**接口刻意和 bybit_trade.BybitClient 一模一样**（同名方法、同样的入参出参），
这样 handlers/rtrade.py 那 965 行交易逻辑一行都不用改，
换所只是换一个 client 对象。两套并行实现迟早会分叉，而分叉的那天
你会在真钱路径上发现它。

两家的差异全部收在这一层里翻译，主要有四处：
  1. **签名方式**：Bybit 是 timestamp+key+recv+payload 拼串签；
     币安是把 query string（含 timestamp）整体 HMAC，再当作 signature 参数附上。
  2. **止盈止损**：Bybit 是下单时带 tp/sl 字段；
     币安必须**另外下两张条件单**（STOP_MARKET / TAKE_PROFIT_MARKET，closePosition）。
  3. **持仓/余额接口**：/fapi/v2/positionRisk、/fapi/v2/account，字段名全不同。
  4. **精度**：都在 exchangeInfo 的 filters 里，还多一个 MIN_NOTIONAL
     （最小名义额，Bybit 没有这条，不看会莫名其妙被拒单）。

⚠️ 默认走**测试网**，和 Bybit 一样"认不出来的值一律当模拟盘"，防手滑上实盘。
   合约测试网 testnet.binancefuture.com，现货测试网 testnet.binance.vision，
   **两个测试网的 key 也是分开申请的**，别指望一把通用。
"""
import hashlib
import hmac
import logging
import os
import time
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY", "")
BINANCE_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")

RECV_WINDOW = 5000


def _mode():
    """live / testnet。默认 testnet——防手滑上实盘这条和 Bybit 保持一致。"""
    v = os.environ.get("BINANCE_TESTNET", "true").strip().lower()
    return "live" if v in ("false", "0", "no") else "testnet"


BASES = {
    ("live", "futures"): "https://fapi.binance.com",
    ("testnet", "futures"): "https://testnet.binancefuture.com",
    ("live", "spot"): "https://api.binance.com",
    ("testnet", "spot"): "https://testnet.binance.vision",
}
MODE_CN = {"live": "⚠️ 实盘 LIVE", "testnet": "🧪 测试网 TESTNET"}

AUTH_HINT = """币安拒绝了这把 key。按可能性排：

1. key 和端点对不上。币安的**合约测试网和现货测试网是两套独立的 key**：
   · 合约测试网 testnet.binancefuture.com 申请的 → BINANCE_TESTNET=true
   · 实盘 key（binance.com 后台）        → BINANCE_TESTNET=false
2. key 没开合约权限。后台建 key 时要勾「启用期货」，只勾读取是下不了单的。
3. 绑了 IP 白名单但里面不是这台服务器的公网 IP。
4. 值里混了引号或空格（.env 里写 KEY="xxx" 会把引号也当成 key）。

别猜，让它自己试（服务器上跑）：
    docker compose exec crypto-bot python binance_trade.py probe"""


class BinanceError(Exception):
    """币安返回 code<0 时抛。带上 code/msg 方便定位——它的错误码很具体，
    比如 -2019 是保证金不足、-1121 是交易对不存在，直接甩给用户比"下单失败"有用。"""

    def __init__(self, code, msg, endpoint=""):
        self.ret_code = code
        self.ret_msg = msg
        super().__init__(f"[Binance {code}] {msg} ({endpoint})")


def round_step(value, step):
    """按步长向下取整。币安对数量/价格的步长要求比 Bybit 还严，超精度直接拒单。"""
    from decimal import Decimal, ROUND_DOWN
    if not step:
        return value
    d = (Decimal(str(value)) / Decimal(str(step))).quantize(
        Decimal("1"), rounding=ROUND_DOWN) * Decimal(str(step))
    return float(d)


class BinanceClient:
    """币安客户端。category 参数只为和 BybitClient 签名对齐，实际用 market 区分。"""

    def __init__(self, api_key=None, api_secret=None, category="linear",
                 market="futures"):
        self.api_key = api_key or BINANCE_API_KEY
        self.api_secret = api_secret or BINANCE_API_SECRET
        self.category = category
        self.market = market            # futures / spot
        if not self.api_key or not self.api_secret:
            raise RuntimeError("缺少 BINANCE_API_KEY / BINANCE_API_SECRET，请在 .env 配置")

    # ── 基础 ────────────────────────────────────────────────
    def base(self, market=None):
        return BASES[(_mode(), market or self.market)]

    def _sign(self, params):
        """币安：把 query string 整体签名，再把 signature 追加上去。
        顺序必须和实际发送的一致，所以这里统一用 urlencode 的结果去签。"""
        params = dict(params or {})
        params["timestamp"] = int(time.time() * 1000)
        params["recvWindow"] = RECV_WINDOW
        qs = urlencode(params)
        sig = hmac.new(self.api_secret.encode(), qs.encode(),
                       hashlib.sha256).hexdigest()
        return qs + "&signature=" + sig

    async def _req(self, method, path, params=None, signed=True, market=None):
        url = self.base(market) + path
        headers = {"X-MBX-APIKEY": self.api_key} if signed else {}
        async with httpx.AsyncClient(timeout=15) as c:
            if signed:
                qs = self._sign(params)
                if method == "GET":
                    r = await c.get(f"{url}?{qs}", headers=headers)
                elif method == "DELETE":
                    r = await c.delete(f"{url}?{qs}", headers=headers)
                else:
                    r = await c.post(f"{url}?{qs}", headers=headers)
            else:
                r = await c.get(url, params=params or {})
        return self._unwrap(r, path)

    @staticmethod
    def _unwrap(resp, endpoint):
        try:
            data = resp.json()
        except Exception:
            resp.raise_for_status()
            raise BinanceError(-1, resp.text[:120], endpoint)
        # 币安的错误是 {"code": -2019, "msg": "..."}；成功时没有 code 字段
        if isinstance(data, dict) and data.get("code") is not None \
                and int(data["code"]) < 0:
            raise BinanceError(data["code"], data.get("msg", ""), endpoint)
        if resp.status_code >= 400:
            raise BinanceError(resp.status_code, str(data)[:120], endpoint)
        return data

    # ── 公开行情 ────────────────────────────────────────────
    async def instrument_info(self, symbol):
        """下单精度。比 Bybit 多一个 minNotional——不看这条会莫名被拒单。"""
        path = "/fapi/v1/exchangeInfo" if self.market == "futures" \
            else "/api/v3/exchangeInfo"
        data = await self._req("GET", path, signed=False)
        for s in data.get("symbols", []):
            if s.get("symbol") != symbol:
                continue
            out = {"tickSize": "0", "qtyStep": "0", "minOrderQty": "0",
                   "minNotional": "0"}
            for f in s.get("filters", []):
                t = f.get("filterType")
                if t == "PRICE_FILTER":
                    out["tickSize"] = f.get("tickSize", "0")
                elif t in ("LOT_SIZE", "MARKET_LOT_SIZE"):
                    out["qtyStep"] = f.get("stepSize", out["qtyStep"])
                    out["minOrderQty"] = f.get("minQty", out["minOrderQty"])
                elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                    out["minNotional"] = f.get("minNotional") or f.get("notional", "0")
            return out
        raise RuntimeError(f"币安没有合约 {symbol}（注意格式如 BTCUSDT）")

    async def last_price(self, symbol):
        path = "/fapi/v1/ticker/price" if self.market == "futures" \
            else "/api/v3/ticker/price"
        d = await self._req("GET", path, {"symbol": symbol}, signed=False)
        return float(d["price"])

    # ── 账户 ────────────────────────────────────────────────
    async def wallet_balance(self, coin="USDT"):
        """返回和 Bybit 同名的字段，让上层不用分家。"""
        if self.market == "spot":
            d = await self._req("GET", "/api/v3/account")
            free = 0.0
            for b in d.get("balances", []):
                if b.get("asset") == coin:
                    free = float(b.get("free", 0)) + float(b.get("locked", 0))
            return {"totalEquity": f"{free}", "totalAvailableBalance": f"{free}",
                    "totalPerpUPL": "0"}
        d = await self._req("GET", "/fapi/v2/account")
        return {
            "totalEquity": d.get("totalMarginBalance", "0"),
            "totalAvailableBalance": d.get("availableBalance", "0"),
            "totalPerpUPL": d.get("totalUnrealizedProfit", "0"),
        }

    @staticmethod
    def _pos_out(p):
        """币安持仓 → Bybit 的字段名。上层（驾驶舱/复盘）只认 Bybit 那套。"""
        amt = float(p.get("positionAmt", 0) or 0)
        return {
            "symbol": p.get("symbol"),
            "side": "Buy" if amt > 0 else ("Sell" if amt < 0 else "None"),
            "size": abs(amt),
            "avgPrice": p.get("entryPrice", "0"),
            "markPrice": p.get("markPrice", "0"),
            "liqPrice": p.get("liquidationPrice", "0"),
            "unrealisedPnl": p.get("unRealizedProfit", "0"),
            "leverage": p.get("leverage", "0"),
            "positionValue": str(abs(amt) * float(p.get("markPrice", 0) or 0)),
        }

    async def position(self, symbol):
        rows = await self._req("GET", "/fapi/v2/positionRisk", {"symbol": symbol})
        for p in rows:
            if abs(float(p.get("positionAmt", 0) or 0)) > 0:
                return self._pos_out(p)
        return {}

    async def positions_all(self):
        rows = await self._req("GET", "/fapi/v2/positionRisk")
        return [self._pos_out(p) for p in rows
                if abs(float(p.get("positionAmt", 0) or 0)) > 0]

    async def set_leverage(self, symbol, leverage):
        return await self._req("POST", "/fapi/v1/leverage",
                               {"symbol": symbol, "leverage": int(leverage)})

    # ── 下单 ────────────────────────────────────────────────
    async def place_limit(self, symbol, side, qty, price, link_id=None,
                          reduce_only=False, tp=None, sl=None):
        from bybit_trade import _guard_order
        body = {"symbol": symbol, "side": side.upper(), "type": "LIMIT",
                "timeInForce": "GTC", "quantity": qty, "price": price,
                "reduceOnly": reduce_only}
        # 复用 Bybit 那边的闸门和审计：killswitch / LIVE_TRADING 是**账户无关**的，
        # 换个交易所就绕过去的话，那个开关等于没有
        _guard_order({"symbol": symbol, "side": side, "orderType": "Limit",
                      "qty": qty, "price": price, "reduceOnly": reduce_only})
        if not reduce_only:
            body.pop("reduceOnly")     # 币安开仓单带 reduceOnly=false 会被拒
        r = await self._req("POST", "/fapi/v1/order", body)
        # 币安的止盈止损是**另外两张条件单**，不是下单时的字段
        if tp or sl:
            await self.set_trading_stop(symbol, tp=tp, sl=sl,
                                        side=side.upper())
        return {"orderId": r.get("orderId"), "raw": r}

    async def place_market(self, symbol, side, qty, reduce_only=False,
                           tp=None, sl=None, link_id=None):
        # 参数顺序必须和 BybitClient.place_market 完全一致：rtrade 是按位置传的，
        # 少一个 tp/sl 就会在换所下单那一刻 TypeError——而那是真钱路径。
        from bybit_trade import _guard_order
        _guard_order({"symbol": symbol, "side": side, "orderType": "Market",
                      "qty": qty, "reduceOnly": reduce_only})
        body = {"symbol": symbol, "side": side.upper(), "type": "MARKET",
                "quantity": qty}
        if reduce_only:
            body["reduceOnly"] = True
        r = await self._req("POST", "/fapi/v1/order", body)
        if tp or sl:
            await self.set_trading_stop(symbol, tp=tp, sl=sl, side=side.upper())
        return {"orderId": r.get("orderId"), "raw": r}

    async def set_trading_stop(self, symbol, tp=None, sl=None, side=None):
        """止盈止损。币安要下两张 closePosition 的条件单，方向与持仓相反。"""
        if side is None:
            pos = await self.position(symbol)
            side = "SELL" if pos.get("side") == "Buy" else "BUY"
        else:
            side = "SELL" if side.upper() in ("BUY", "LONG") else "BUY"
        out = []
        for px, otype in ((sl, "STOP_MARKET"), (tp, "TAKE_PROFIT_MARKET")):
            if not px:
                continue
            r = await self._req("POST", "/fapi/v1/order", {
                "symbol": symbol, "side": side, "type": otype,
                "stopPrice": px, "closePosition": "true"})
            out.append(r.get("orderId"))
        return {"orderIds": out}

    async def cancel(self, symbol, order_id=None, link_id=None):
        return await self._req("DELETE", "/fapi/v1/order",
                               {"symbol": symbol, "orderId": order_id})

    async def cancel_all(self, symbol):
        return await self._req("DELETE", "/fapi/v1/allOpenOrders",
                               {"symbol": symbol})

    async def open_orders(self, symbol=None):
        p = {"symbol": symbol} if symbol else {}
        rows = await self._req("GET", "/fapi/v1/openOrders", p)
        return [{"orderId": o.get("orderId"), "symbol": o.get("symbol"),
                 "side": o.get("side", "").title(), "qty": o.get("origQty"),
                 "price": o.get("price"), "orderType": o.get("type", "").title(),
                 "reduceOnly": o.get("reduceOnly")} for o in rows]

    # ── 现货 ────────────────────────────────────────────────
    async def spot_order(self, symbol, side, qty=None, quote=None, price=None):
        """现货买卖。quote = 按 USDT 金额买（币安支持 quoteOrderQty，省得自己换算）。"""
        from bybit_trade import _guard_order
        _guard_order({"symbol": symbol, "side": side, "orderType": "Spot",
                      "qty": qty or quote, "reduceOnly": False})
        body = {"symbol": symbol, "side": side.upper()}
        if price:
            body.update({"type": "LIMIT", "timeInForce": "GTC",
                         "price": price, "quantity": qty})
        else:
            body["type"] = "MARKET"
            if quote:
                body["quoteOrderQty"] = quote
            else:
                body["quantity"] = qty
        return await self._req("POST", "/api/v3/order", body, market="spot")

    async def spot_balances(self):
        d = await self._req("GET", "/api/v3/account", market="spot")
        return [b for b in d.get("balances", [])
                if float(b.get("free", 0)) + float(b.get("locked", 0)) > 0]


def is_auth_error(e):
    code = getattr(e, "ret_code", None)
    # -2015 无效key/IP/权限，-2014 key格式错，-1022 签名错，401 = 端点不认
    return code in (-2015, -2014, -1022, 401, 403) or "API-key" in str(e)


async def _probe():
    """合约/现货 × 测试网/实盘 各试一次，直接告诉他这把 key 属于哪套。"""
    if not BINANCE_API_KEY or not BINANCE_API_SECRET:
        print("❌ key 或 secret 是空的 —— 容器没读到。"
              "改完 .env 要 docker compose up -d --force-recreate")
        return
    print(f"key {BINANCE_API_KEY[:6]}…{BINANCE_API_KEY[-4:]}"
          f"（{len(BINANCE_API_KEY)} 位），挨个试（只查余额，不下单）\n")
    orig = os.environ.get("BINANCE_TESTNET")
    hit = []
    try:
        for mode in ("testnet", "live"):
            os.environ["BINANCE_TESTNET"] = "true" if mode == "testnet" else "false"
            for market in ("futures", "spot"):
                c = BinanceClient(market=market)
                try:
                    bal = await c.wallet_balance("USDT")
                    print(f"  ✅ {mode:<8} {market:<8} {c.base():<40}"
                          f" 认（USDT {bal.get('totalEquity')}）")
                    hit.append((mode, market))
                except Exception as e:
                    why = "key 不属于这里" if is_auth_error(e) else str(e)[:60]
                    print(f"  ❌ {mode:<8} {market:<8} {c.base():<40} {why}")
    finally:
        if orig is None:
            os.environ.pop("BINANCE_TESTNET", None)
        else:
            os.environ["BINANCE_TESTNET"] = orig
    print()
    if hit:
        m = hit[0][0]
        print(f"→ 这把 key 属于 **{m}**。在 .env 里设 "
              f"BINANCE_TESTNET={'true' if m == 'testnet' else 'false'}")
        markets = {x[1] for x in hit}
        if "futures" not in markets:
            print("  ⚠️ 合约那边不认：建 key 时要勾「启用期货」，只勾读取下不了单")
        if "spot" not in markets:
            print("  ℹ️ 现货那边不认——只做合约的话不影响")
    else:
        print("→ 四种都不认。\n" + AUTH_HINT)


if __name__ == "__main__":
    import asyncio
    import sys
    logging.basicConfig(level=logging.WARNING)
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        asyncio.run(_probe())
        raise SystemExit(0)
    asyncio.run(_probe())
