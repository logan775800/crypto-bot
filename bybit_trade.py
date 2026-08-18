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


def _guard_order(body):
    """所有开仓单的最后一道闸 + 审计。**写在下单函数内部**，不靠调用方自觉。

    只拦开仓（reduceOnly=False）：出事时你需要的是「不能再开新仓」，
    而平仓和撤单恰恰是这时候最该畅通的。
    审计无论是否放行都要写——被拦下的尝试同样是需要复盘的信息。
    """
    try:
        from handlers.keyguard import trading_enabled, audit
    except Exception:
        return                      # keyguard 不可用时不阻断交易（它是护栏不是依赖）
    opening = not body.get("reduceOnly")
    allowed = trading_enabled() or not opening
    audit("order" if allowed else "order_blocked", {
        "symbol": body.get("symbol"), "side": body.get("side"),
        "type": body.get("orderType"), "qty": body.get("qty"),
        "price": body.get("price", "market"), "reduceOnly": body.get("reduceOnly"),
        "testnet": _is_testnet(),
    })
    if not allowed:
        raise RuntimeError("实盘开仓已被 /killswitch 禁用。平仓和查询不受影响，"
                           "恢复请发 /killswitch off")


def _mode():
    """live / demo / testnet。

    Bybit 有**两套模拟盘**，key 互不通用，这是配置时最容易翻车的一处：
      • testnet.bybit.com —— 独立注册的测试站，端点 api-testnet.bybit.com
      • 主站里的「模拟交易 Demo Trading」—— 用你的实盘账号，端点 api-demo.bybit.com
    在主站模拟交易里建的 key 拿去打 api-testnet，交易所只回一句
    `401 API key is invalid`，完全看不出是端点选错了。
    """
    v = os.environ.get("BYBIT_TESTNET", "true").strip().lower()
    if v in ("false", "0", "no"):
        return "live"
    if v in ("demo", "demo-trading", "sim"):
        return "demo"
    return "testnet"


BASE_URLS = {"live": "https://api.bybit.com",
             "demo": "https://api-demo.bybit.com",
             "testnet": "https://api-testnet.bybit.com"}
MODE_CN = {"live": "⚠️ 实盘 LIVE", "demo": "主站模拟交易 DEMO",
           "testnet": "测试站 TESTNET"}


def _base_url():
    return BASE_URLS[_mode()]


AUTH_HINT = """401 API key is invalid —— 交易所说这把 key 在**当前端点**上不认识。
按可能性排：

1. **key 和端点对不上**（最常见）。Bybit 有两套模拟盘，key 不通用：
   · 在 testnet.bybit.com（要单独注册）建的 → BYBIT_TESTNET=true
   · 在主站右上角切「模拟交易 Demo」里建的 → BYBIT_TESTNET=demo
   · 实盘 key → BYBIT_TESTNET=false（先确认没提现权限、绑了服务器 IP）
2. **值里混了引号或空格**。.env 里写 KEY="xxx" 会把引号一起当成 key。
   验：docker compose exec crypto-bot python -c \\
       "import os;k=os.environ['BYBIT_API_KEY'];print(repr(k),len(k))"
3. key 建好后没启用、已过期（绑 IP 的 key 90 天不用会失效）、或被删了。

别猜是哪一种，让它自己试：
    docker compose exec crypto-bot python bybit_trade.py probe
三个端点各查一次余额（只读不下单），直接告诉你这把 key 属于哪套。"""


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

    async def positions_all(self):
        """当前所有有持仓的合约（size>0），用于 /rpos 不带币时列全部。"""
        r = await self._get(
            "/v5/position/list", {"category": self.category, "settleCoin": "USDT"}
        )
        return [p for p in (r.get("list") or []) if float(p.get("size", 0) or 0) > 0]

    async def closed_pnl(self, start_ms=None, end_ms=None, symbol=None,
                         limit=100, cursor=None):
        """已平仓盈亏明细（/v5/position/closed-pnl）——交易所自己的账，别自己算。
        ⚠️ Bybit 硬限制：startTime~endTime 间隔必须 ≤7 天，更长要调用方切片。
        翻页用返回的 nextPageCursor。"""
        p = {"category": self.category, "limit": limit}
        if symbol:
            p["symbol"] = symbol
        if start_ms:
            p["startTime"] = int(start_ms)
        if end_ms:
            p["endTime"] = int(end_ms)
        if cursor:
            p["cursor"] = cursor
        return await self._get("/v5/position/closed-pnl", p)

    async def executions(self, start_ms=None, end_ms=None, symbol=None,
                         limit=100, cursor=None):
        """成交明细（/v5/execution/list）。同样 ≤7 天窗口。
        用途：closed-pnl 不含开仓时间，靠这里的逐笔成交还原「仓是何时开的」；
        execType=Funding 的记录则是资金费实际扣款。"""
        p = {"category": self.category, "limit": limit}
        if symbol:
            p["symbol"] = symbol
        if start_ms:
            p["startTime"] = int(start_ms)
        if end_ms:
            p["endTime"] = int(end_ms)
        if cursor:
            p["cursor"] = cursor
        return await self._get("/v5/execution/list", p)

    async def set_trading_stop(self, symbol, tp=None, sl=None):
        """给已有仓位设/改止盈止损（全仓量）。tp/sl 传 None 表示不动。"""
        body = {"category": self.category, "symbol": symbol, "positionIdx": 0}
        if tp is not None:
            body["takeProfit"] = str(tp)
        if sl is not None:
            body["stopLoss"] = str(sl)
        return await self._post("/v5/position/trading-stop", body)

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
    async def place_limit(self, symbol, side, qty, price, link_id=None,
                          reduce_only=False, tp=None, sl=None):
        """挂限价单。side: 'Buy' / 'Sell'。qty/price 需已按步长取整。返回含 orderId。
        reduce_only=True 用于只减仓（平仓单）；tp/sl 为开仓单附带的止盈止损触发价。"""
        body = {
            "category": self.category, "symbol": symbol, "side": side,
            "orderType": "Limit", "qty": str(qty), "price": str(price),
            "timeInForce": "GTC", "positionIdx": 0, "reduceOnly": reduce_only,
        }
        if tp:
            body["takeProfit"] = str(tp)
        if sl:
            body["stopLoss"] = str(sl)
        if link_id:
            body["orderLinkId"] = link_id
        _guard_order(body)
        return await self._post("/v5/order/create", body)

    async def place_market(self, symbol, side, qty, reduce_only=False,
                           tp=None, sl=None, link_id=None):
        """市价单。平仓请 reduce_only=True（只减仓，杜绝反向开成新仓）。"""
        body = {
            "category": self.category, "symbol": symbol, "side": side,
            "orderType": "Market", "qty": str(qty),
            "positionIdx": 0, "reduceOnly": reduce_only,
        }
        if tp:
            body["takeProfit"] = str(tp)
        if sl:
            body["stopLoss"] = str(sl)
        if link_id:
            body["orderLinkId"] = link_id
        _guard_order(body)
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
    print(f"环境: {MODE_CN[_mode()]}  base={_base_url()}")
    k = BYBIT_API_KEY
    print(f"key: {k[:4]}…{k[-3:] if len(k) > 7 else ''}（{len(k)} 位）"
          if k else "key: ❌ 空 —— 容器没读到，改完 .env 要 up -d --force-recreate")
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


ENV_VALUE = {"testnet": "true", "demo": "demo", "live": "false"}


async def _probe():
    """三个端点各试一次，看哪个认这把 key —— 别再靠猜。

    401 只说"key 无效"，不说"你打错门了"。而 Bybit 的 testnet / 主站模拟交易 /
    实盘是三套独立的 key 体系，光看 key 本身（都是 18 位字母数字）分不出是哪套的。
    挨个试一遍最省事：只查余额，只读，不下任何单。
    """
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        print("❌ key 或 secret 是空的 —— 容器没读到。"
              "改完 .env 要 docker compose up -d --force-recreate")
        return
    print(f"key {BYBIT_API_KEY[:4]}…{BYBIT_API_KEY[-3:]}（{len(BYBIT_API_KEY)} 位）"
          f"，三个端点挨个试（只查余额，不下单）\n")
    orig = os.environ.get("BYBIT_TESTNET")
    hit = None
    try:
        for mode in ("testnet", "demo", "live"):
            os.environ["BYBIT_TESTNET"] = ENV_VALUE[mode]
            try:
                bal = await BybitClient().wallet_balance("USDT")
                print(f"  ✅ {mode:<8} {_base_url():<34} 认这把 key"
                      f"（总权益 {bal.get('totalEquity', '?')}）")
                hit = hit or mode
            except Exception as e:
                why = "key 不属于这个环境" if is_auth_error(e) else str(e)[:70]
                print(f"  ❌ {mode:<8} {_base_url():<34} {why}")
    finally:
        if orig is None:
            os.environ.pop("BYBIT_TESTNET", None)
        else:
            os.environ["BYBIT_TESTNET"] = orig

    print()
    if hit:
        print(f"→ 这把 key 属于 **{hit}**。在 .env 里设 "
              f"BYBIT_TESTNET={ENV_VALUE[hit]} 然后 up -d --force-recreate")
        if hit == "live":
            print("  ⚠️ 这是**实盘** key。切之前先确认：没勾提现权限、绑了服务器 IP。"
                  "建议先去建一把模拟盘 key 验完流程再上实盘。")
    else:
        print("→ 三个端点都不认。那就不是端点的问题了：\n"
              "  · key 建好后没启用 / 已被删 / 已过期（绑 IP 的 key 90 天不用会失效）\n"
              "  · secret 抄错了（key 对 secret 错，签名不过也是 401）\n"
              "  · key 绑了 IP 白名单，但里面不是这台服务器的公网 IP\n"
              "  去交易所后台重建一把，权限只勾合约，别勾提现。")


def is_auth_error(e):
    """这个异常是不是「交易所不认这把 key」。401 是 Bybit 对 key 无效的回法。"""
    code = getattr(getattr(e, "response", None), "status_code", None)
    return code in (401, 403) or "API key is invalid" in str(e)


if __name__ == "__main__":
    import asyncio
    import sys
    logging.basicConfig(level=logging.WARNING)   # INFO 会把每条 httpx 请求刷出来
    if len(sys.argv) > 1 and sys.argv[1] == "probe":
        asyncio.run(_probe())
        raise SystemExit(0)
    try:
        asyncio.run(_smoke())
    except Exception as e:
        # 401 光甩一句 "API key is invalid" 没法定位——最常见的原因是端点选错了
        # （主站模拟交易的 key 打去了 testnet），而报错里一个字都没提这回事。
        print(f"❌ 自测失败: {e}")
        if is_auth_error(e):
            print("\n" + AUTH_HINT)
