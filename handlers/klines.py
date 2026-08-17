"""统一 K 线层：四家交易所 × 现货/永续 → 同一种 OHLCV 结构。

这是「取价能选交易所，但看图/分析/回测只能看 Bybit」剩下的那一半。
取一个价只要一个数，四家接口大同小异；K 线不一样，四家的**字段顺序、排序方向、
时间戳单位**全都不同，而且错了不会报错——只会让指标算出一堆看起来合理的垃圾。
所以下面每一条都是 2026-08-14 实测的，不是照记忆写的：

  Bybit   新→旧  毫秒  [t, o, h, l, c, 基础量, 成交额]
  OKX     新→旧  毫秒  [t, o, h, l, c, ?, ?, ?, confirm]
                       ⚠️ 永续的第 5 列是**张数**，基础量在第 6 列、成交额在第 7 列；
                       现货则是第 5 列基础量、第 6 列成交额——同一家两种语义
  币安    旧→新  毫秒  [开盘时间, o, h, l, c, 基础量, 收盘时间, 成交额, ...]
  Gate现货 旧→新  **秒**  [t, 成交额, 收, 高, 低, 开, 基础量, 是否已收]
                       ⚠️ 根本不是 OHLC 顺序，照 Bybit 那套读会把收盘价当开盘价
  Gate永续 旧→新  **秒**  {t, o, h, l, c, v(张数), sum(成交额)}

另外两个坑：
  • OKX 单次最多 300 根（要 1000 也只给 300），EMA200 够用但「400 根做结构」会缩水，
    所以返回里带 capped 标记，让调用方如实告诉用户，而不是假装取到了；
  • OKX 的周期 ≥1 小时必须大写（4H/1D/1W），小写直接报 51000 参数错误。
"""
import logging
import time

from handlers import source as src_mod

log = logging.getLogger(__name__)

SPOT, SWAP = src_mod.SPOT, src_mod.SWAP

# 统一周期写法 → 各家的写法。None = 这家不支持这个周期（如实报错，不偷偷换周期）
INTERVALS = {
    #          bybit   okx    binance  gate
    "1m":     ("1",   "1m",   "1m",   "1m"),
    "3m":     ("3",   "3m",   "3m",   "3m"),
    "5m":     ("5",   "5m",   "5m",   "5m"),
    "15m":    ("15",  "15m",  "15m",  "15m"),
    "30m":    ("30",  "30m",  "30m",  "30m"),
    "1h":     ("60",  "1H",   "1h",   "1h"),
    "2h":     ("120", "2H",   "2h",   "2h"),
    "4h":     ("240", "4H",   "4h",   "4h"),
    "6h":     ("360", "6H",   "6h",   "6h"),
    "12h":    ("720", "12H",  "12h",  "12h"),
    "1d":     ("D",   "1D",   "1d",   "1d"),
    "1w":     ("W",   "1W",   "1w",   "7d"),
}
_EX_COL = {"bybit": 0, "okx": 1, "binance": 2, "gate": 3}

# Bybit 那套写法也认（marketdata 里到处在用）
_ALIAS = {"60": "1h", "120": "2h", "240": "4h", "360": "6h", "720": "12h",
          "d": "1d", "D": "1d", "w": "1w", "W": "1w", "1D": "1d", "1W": "1w"}

MAX_LIMIT = {"bybit": 1000, "okx": 300, "binance": 1000, "gate": 1000}


def norm_interval(interval):
    """把各种写法归一成 '15m' 这种。认不出就原样返回，让下游报错说清楚。"""
    s = str(interval or "").strip()
    s = _ALIAS.get(s, s.lower())
    return _ALIAS.get(s, s)


def supports(ex, interval):
    row = INTERVALS.get(norm_interval(interval))
    return bool(row and row[_EX_COL.get(ex, 0)])


def _sym(ex, symbol, market):
    base = src_mod.norm(symbol)
    if ex == "okx":
        return f"{base}-USDT-SWAP" if market == SWAP else f"{base}-USDT"
    if ex == "gate":
        return f"{base}_USDT"
    return f"{base}USDT"          # bybit / binance


# ---------- 各家取数 + 归一 ----------
async def _bybit(c, sym, iv, limit, market):
    r = await c.get("https://api.bybit.com/v5/market/kline",
                    params={"category": "linear" if market == SWAP else "spot",
                            "symbol": sym, "interval": iv, "limit": limit})
    d = r.json()
    rows = ((d.get("result") or {}).get("list")) or []
    # Bybit 会回服务器时间，用它判断「最后一根K线多旧」比用本机时钟准
    srv = int(d.get("time") or 0) or None
    return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
             float(x[5]), float(x[6])] for x in reversed(rows)], srv


async def _okx(c, sym, iv, limit, market):
    r = await c.get("https://www.okx.com/api/v5/market/candles",
                    params={"instId": sym, "bar": iv, "limit": min(limit, 300)})
    rows = r.json().get("data") or []
    out = []
    for x in reversed(rows):
        # 永续第5列是张数，基础量在第6列；现货第5列就是基础量
        vol = float(x[6]) if market == SWAP else float(x[5])
        turn = float(x[7]) if market == SWAP else float(x[6])
        out.append([int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
                    vol, turn])
    return out, None


async def _binance(c, sym, iv, limit, market):
    base = "https://fapi.binance.com/fapi/v1/klines" if market == SWAP \
        else "https://api.binance.com/api/v3/klines"
    r = await c.get(base, params={"symbol": sym, "interval": iv, "limit": limit})
    rows = r.json()
    if not isinstance(rows, list):        # 币安报错时回的是 dict
        return [], None
    return [[int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4]),
             float(x[5]), float(x[7])] for x in rows], None


async def _gate(c, sym, iv, limit, market):
    if market == SWAP:
        r = await c.get("https://api.gateio.ws/api/v4/futures/usdt/candlesticks",
                        params={"contract": sym, "interval": iv, "limit": limit})
        rows = r.json()
        if not isinstance(rows, list):
            return [], None
        # v 是张数，换成基础量要乘合约乘数——gate.contracts() 已有 1 小时缓存，不额外花钱
        mult = 1.0
        try:
            from handlers import gate as gate_mod
            meta = (await gate_mod.contracts()).get(sym) or {}
            mult = float(meta.get("quanto_multiplier") or 1) or 1.0
        except Exception as e:
            log.debug(f"取 Gate 合约乘数失败，成交量按张数计: {e}")
        return [[int(x["t"]) * 1000, float(x["o"]), float(x["h"]), float(x["l"]),
                 float(x["c"]), float(x["v"]) * mult, float(x.get("sum") or 0)]
                for x in rows], None
    r = await c.get("https://api.gateio.ws/api/v4/spot/candlesticks",
                    params={"currency_pair": sym, "interval": iv, "limit": limit})
    rows = r.json()
    if not isinstance(rows, list):
        return [], None
    # [t, 成交额, 收, 高, 低, 开, 基础量, 是否已收] —— 顺序是反的，别照 OHLC 读
    return [[int(x[0]) * 1000, float(x[5]), float(x[3]), float(x[4]), float(x[2]),
             float(x[6]), float(x[1])] for x in rows], None


_FETCH = {"bybit": _bybit, "okx": _okx, "binance": _binance, "gate": _gate}


async def fetch(symbol, interval="15m", limit=300, ex="bybit", market=SWAP):
    """取归一后的 K 线。

    返回 (rows, meta)：
      rows = [[时间戳毫秒, 开, 高, 低, 收, 基础量, 成交额USDT], ...]，**旧→新**
      meta = {label, ex, market, interval, asked, got, capped, error}
    取不到时 rows 为空、meta['error'] 说明原因——「这个币这家没有」和「接口挂了」
    是两回事，调用方要能分开告诉用户。
    """
    ex = ex if ex in _FETCH else "bybit"
    iv_key = norm_interval(interval)
    row = INTERVALS.get(iv_key)
    label = src_mod.label_of(ex, market)
    meta = {"label": label, "ex": ex, "market": market, "interval": iv_key,
            "asked": limit, "got": 0, "capped": False, "error": ""}
    if not row:
        meta["error"] = f"不认识的周期 {interval}"
        return [], meta
    native = row[_EX_COL[ex]]
    if not native:
        meta["error"] = f"{src_mod.EX_CN.get(ex, ex)} 没有 {iv_key} 这个周期"
        return [], meta

    cap = MAX_LIMIT.get(ex, 300)
    want = max(10, min(int(limit), cap))
    meta["capped"] = int(limit) > cap
    try:
        rows, srv = await _FETCH[ex](src_mod.client(), _sym(ex, symbol, market),
                                     native, want, market)
    except Exception as e:
        log.warning(f"取K线失败 {symbol} {ex} {market} {iv_key}: {e}")
        meta["error"] = f"{src_mod.EX_CN.get(ex, ex)} 取K线失败：{str(e)[:60]}"
        return [], meta
    # 只有 Bybit 回服务器时间，其余用本机时钟——判断「最后一根多旧」够用，
    # 但本机时钟要是偏了，这个判断也会跟着偏，别把它当权威时间源
    meta["server_ms"] = srv or int(time.time() * 1000)

    if not rows:
        meta["error"] = (f"{src_mod.EX_CN.get(ex, ex)}"
                         f"{'永续' if market == SWAP else '现货'}没有 "
                         f"{src_mod.norm(symbol)} 或该周期无数据")
        return [], meta
    meta["got"] = len(rows)
    return rows, meta


def note(meta):
    """给用户看的一句数据来源说明；没什么可说时返回空串。"""
    if not meta.get("got"):
        return ""
    bits = [meta["label"]]
    if meta.get("capped"):
        bits.append(f"单次上限 {meta['got']} 根（要了 {meta['asked']} 根）")
    return "　".join(bits)


# ---------- 全市场清单（扫描类用） ----------
# 四家的成交额和涨跌幅字段又各不相同（2026-08-15 实测）：
#   Bybit  turnover24h(USDT)        price24hPcnt 是**小数**(-0.000155 = -0.0155%)
#   OKX    没有直接的成交额字段，要 volCcy24h(基础量) × last 自己算；
#          涨跌幅也没有，要 (last-open24h)/open24h
#   币安    quoteVolume(USDT)        priceChangePercent 已是百分数
#   Gate   volume_24h_settle(USDT)  change_percentage 已是百分数
# 代币化股票只有 Bybit 和 Gate 有，各自用 symbolType / contract_type 剔除。
_NONCRYPTO = {"at": 0.0, "set": None}
_NONCRYPTO_TTL = 3600


async def noncrypto_bases():
    """所有"不是加密货币"的基名（SNDK、NAVER、XAU…），合并三家的标注。

    为什么要合并：**OKX 的合约接口根本没有品类字段**（实测 SNDK-USDT-SWAP 和
    BTC-USDT-SWAP 的每个字段都一样），而它上了一堆代币化美股，SNDK 的成交额
    还排在全所第二——不剔除的话缓涨扫描第一页会被美股占满。
    好在同一个标的在别家是有标的：
      Bybit  instruments-info.symbolType != ''
      币安    exchangeInfo.underlyingType != 'COIN'（EQUITY/HK_EQUITY/COMMODITY…）
      Gate   contracts.contract_type != ''
    同一个基名在任一家被标成非加密，就当它在四家都是非加密——底层资产是同一个。
    """
    now = time.monotonic()
    if _NONCRYPTO["set"] is not None and now - _NONCRYPTO["at"] < _NONCRYPTO_TTL:
        return _NONCRYPTO["set"]
    out = set()
    c = src_mod.client()
    try:
        from handlers import marketdata as md
        for s, t in (await md.symbol_types()).items():
            # 只认真正的非加密品类：'innovation' 是 Bybit 创新区的**真币**
            if not md.is_crypto_type(t) and s.endswith("USDT"):
                out.add(s[:-4])
    except Exception as e:
        log.debug(f"取 Bybit 品类失败: {e}")
    try:
        r = await c.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
        for x in (r.json().get("symbols") or []):
            if (x.get("underlyingType") or "COIN") != "COIN":
                out.add(x.get("baseAsset") or "")
    except Exception as e:
        log.debug(f"取币安品类失败: {e}")
    try:
        from handlers import gate as gate_mod
        for name, meta in (await gate_mod.contracts()).items():
            if not gate_mod._kind(meta)[0] and name.endswith("_USDT"):
                out.add(name[:-5])
    except Exception as e:
        log.debug(f"取 Gate 品类失败: {e}")
    out.discard("")
    if out:
        _NONCRYPTO.update(at=now, set=out)
    return out


async def universe(ex="bybit", market=SWAP):
    """某家某市场的全部 USDT 交易对 → [{symbol, turnover, change, crypto}]，成交额降序。

    symbol 是**基名**（AKE、BTC），跨所可比；各家的原生写法在这层就抹平了。
    非加密标的（代币化股票/杠杆代币）不在这里剔除，只打 crypto=False 标记——
    调用方要的口径不一样：缓涨扫描要排除它们，而"一共扫了多少个"要算进去。
    """
    c = src_mod.client()
    bad = await noncrypto_bases()
    out = []
    if ex == "bybit":
        from handlers import marketdata as md
        r = await c.get("https://api.bybit.com/v5/market/tickers",
                        params={"category": "linear" if market == SWAP else "spot"})
        types = await md.symbol_types() if market == SWAP else {}
        for t in ((r.json().get("result") or {}).get("list") or []):
            s = t.get("symbol") or ""
            if not s.endswith("USDT"):
                continue
            out.append({"symbol": s[:-4],
                        "turnover": float(t.get("turnover24h") or 0),
                        "change": float(t.get("price24hPcnt") or 0) * 100,
                        "price": float(t.get("lastPrice") or 0),
                        "funding": _maybe_float(t.get("fundingRate")),
                        "crypto": md.is_crypto_type(types.get(s, ""))
                        and s[:-4] not in bad})
    elif ex == "okx":
        r = await c.get("https://www.okx.com/api/v5/market/tickers",
                        params={"instType": "SWAP" if market == SWAP else "SPOT"})
        tail = "-USDT-SWAP" if market == SWAP else "-USDT"
        for t in (r.json().get("data") or []):
            iid = t.get("instId") or ""
            if not iid.endswith(tail):
                continue
            last = float(t.get("last") or 0)
            op = float(t.get("open24h") or 0)
            out.append({"symbol": iid[:-len(tail)],
                        "turnover": float(t.get("volCcy24h") or 0) * last,
                        "change": (last - op) / op * 100 if op else 0.0,
                        "price": last,
                        # OKX 的行情接口不带资金费率，也没有全市场批量接口。
                        # 如实留 None——扫描器对缺失是标"未知"，不是当成 0（中性）
                        "funding": None,
                        # OKX 自己不标品类，只能靠别家标注过的基名反查
                        "crypto": iid[:-len(tail)] not in bad})
    elif ex == "binance":
        url = ("https://fapi.binance.com/fapi/v1/ticker/24hr" if market == SWAP
               else "https://api.binance.com/api/v3/ticker/24hr")
        rows = (await c.get(url)).json()
        if not isinstance(rows, list):
            return []
        funding_map = {}
        if market == SWAP:
            try:
                pr = await c.get("https://fapi.binance.com/fapi/v1/premiumIndex")
                for x in (pr.json() or []):
                    funding_map[x.get("symbol")] = _maybe_float(x.get("lastFundingRate"))
            except Exception as e:
                log.debug(f"取币安资金费率失败: {e}")
        for t in rows:
            s = t.get("symbol") or ""
            if not s.endswith("USDT"):
                continue
            base = s[:-4]
            out.append({"symbol": base,
                        "turnover": float(t.get("quoteVolume") or 0),
                        "change": float(t.get("priceChangePercent") or 0),
                        "price": float(t.get("lastPrice") or 0),
                        "funding": funding_map.get(s),
                        # 杠杆代币（BTCUP/BTCDOWN）不是币，和 Gate 那边同一个理由
                        "crypto": base not in bad
                        and not any(base.endswith(x) for x in _LEV_SUFFIX_BN)})
    elif ex == "gate":
        from handlers import gate as gate_mod
        if market == SWAP:
            rows = (await c.get(
                "https://api.gateio.ws/api/v4/futures/usdt/tickers")).json()
            meta = await gate_mod.contracts()
            for t in rows if isinstance(rows, list) else []:
                pair = t.get("contract") or ""
                if not pair.endswith("_USDT"):
                    continue
                out.append({"symbol": pair[:-5],
                            "turnover": float(t.get("volume_24h_settle") or 0),
                            "change": float(t.get("change_percentage") or 0),
                            "price": float(t.get("last") or 0),
                            "funding": _maybe_float(t.get("funding_rate")),
                            "crypto": gate_mod._kind(meta.get(pair))[0]
                            and pair[:-5] not in bad})
        else:
            rows = (await c.get("https://api.gateio.ws/api/v4/spot/tickers")).json()
            for t in rows if isinstance(rows, list) else []:
                pair = t.get("currency_pair") or ""
                if not pair.endswith("_USDT"):
                    continue
                base = pair[:-5]
                out.append({"symbol": base,
                            "turnover": float(t.get("quote_volume") or 0),
                            "change": float(t.get("change_percentage") or 0),
                            "price": float(t.get("last") or 0),
                            "funding": None,          # 现货没有资金费率
                            "crypto": not gate_mod._is_leveraged(base)
                            and base not in bad})
    out.sort(key=lambda x: -x["turnover"])
    return out


_LEV_SUFFIX_BN = ("UP", "DOWN", "BULL", "BEAR")


def _maybe_float(v):
    """能转就转，转不了返回 None——**不要返回 0**。
    资金费率 0 的意思是"中性"，和"不知道"完全是两回事，扫描器的拥挤度会判反。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


async def fetch_for(chat_id, symbol, interval="15m", limit=300, override=None):
    """按「单条覆盖 > 会话默认 > Bybit 永续」取 K 线。

    默认落回 Bybit 永续而不是「自动」：K 线是给指标和图用的，自动挑源会让
    同一个币今天用这家、明天用那家，画出来的结构对不上，回测结果也不可比。
    """
    if override:
        ex, market = src_mod.split_label(override)
    else:
        ex, market = src_mod.get_pref(chat_id)
    if ex == src_mod.AUTO:
        ex = "bybit"
    if market == src_mod.AUTO:
        market = SWAP
    return await fetch(symbol, interval, limit, ex, market)


# ---------- 合约乘数 / 持仓量 / 盘口（扫描器要的执行质量数据） ----------
# 单位是这里唯一的难点，四家全不一样（2026-08-17 实测 BTC 永续买一档）：
#   Bybit  盘口 size 是**币**            9.869 BTC
#   币安   盘口 qty  是**币**            9.128 BTC
#   OKX    盘口 size 是**张**，ctVal 每个合约不同（BTC 0.01、DOGE 1000）
#   Gate   盘口 s    是**张**，quanto_multiplier 每个合约不同（BTC 0.0001）
# 不换算就直接比，OKX/Gate 的深度会被算成几百倍——扫描器的"执行"维度直接反了。
_UNITS = {"at": 0.0, "data": {}}
_UNITS_TTL = 3600


async def contract_units(ex):
    """{基名: 一张合约等于多少个币}。Bybit/币安盘口本来就是币，返回空表示 1。"""
    if ex in ("bybit", "binance"):
        return {}
    now = time.monotonic()
    cached = _UNITS["data"].get(ex)
    if cached and now - _UNITS["at"] < _UNITS_TTL:
        return cached
    out = {}
    try:
        if ex == "okx":
            r = await src_mod.client().get(
                "https://www.okx.com/api/v5/public/instruments",
                params={"instType": "SWAP"})
            for x in (r.json().get("data") or []):
                iid = x.get("instId") or ""
                if iid.endswith("-USDT-SWAP"):
                    out[iid[:-10]] = float(x.get("ctVal") or 1)
        elif ex == "gate":
            from handlers import gate as gate_mod
            for name, c in (await gate_mod.contracts()).items():
                if name.endswith("_USDT"):
                    out[name[:-5]] = float(c.get("quanto_multiplier") or 1)
    except Exception as e:
        log.warning(f"取 {ex} 合约乘数失败，深度会按张数算错: {e}")
        return {}
    _UNITS["data"][ex] = out
    _UNITS["at"] = now
    return out


async def orderbook(symbol, ex="bybit", market=SWAP, limit=200, units=None):
    """→ (bids, asks)，每档 (价格, **币的数量**)，最优价在前。取不到返回 ([], [])。"""
    base = src_mod.norm(symbol)
    mult = 1.0
    if ex in ("okx", "gate"):
        u = units if units is not None else await contract_units(ex)
        mult = float(u.get(base) or 1.0)
    c = src_mod.client()
    try:
        if ex == "bybit":
            r = await c.get("https://api.bybit.com/v5/market/orderbook",
                            params={"category": "linear" if market == SWAP else "spot",
                                    "symbol": f"{base}USDT", "limit": min(limit, 200)})
            d = (r.json().get("result") or {})
            return ([(float(p), float(s)) for p, s in (d.get("b") or [])],
                    [(float(p), float(s)) for p, s in (d.get("a") or [])])
        if ex == "okx":
            inst = f"{base}-USDT-SWAP" if market == SWAP else f"{base}-USDT"
            r = await c.get("https://www.okx.com/api/v5/market/books",
                            params={"instId": inst, "sz": min(limit, 400)})
            d = (r.json().get("data") or [{}])[0]
            return ([(float(x[0]), float(x[1]) * mult) for x in (d.get("bids") or [])],
                    [(float(x[0]), float(x[1]) * mult) for x in (d.get("asks") or [])])
        if ex == "binance":
            url = ("https://fapi.binance.com/fapi/v1/depth" if market == SWAP
                   else "https://api.binance.com/api/v3/depth")
            # 币安只认 5/10/20/50/100/500/1000 这几个档数，别的会报错
            allowed = [5, 10, 20, 50, 100, 500, 1000]
            lim = min([x for x in allowed if x >= limit] or [1000])
            r = await c.get(url, params={"symbol": f"{base}USDT", "limit": lim})
            d = r.json()
            return ([(float(p), float(q)) for p, q in (d.get("bids") or [])],
                    [(float(p), float(q)) for p, q in (d.get("asks") or [])])
        if ex == "gate":
            r = await c.get("https://api.gateio.ws/api/v4/futures/usdt/order_book",
                            params={"contract": f"{base}_USDT", "limit": min(limit, 200)})
            d = r.json()
            return ([(float(x["p"]), float(x["s"]) * mult) for x in (d.get("bids") or [])],
                    [(float(x["p"]), float(x["s"]) * mult) for x in (d.get("asks") or [])])
    except Exception as e:
        log.debug(f"取盘口失败 {symbol} {ex}: {e}")
    return [], []


# 统一周期 → (rubik 支持的周期, 根数换算比)。例：想要 13 根 15m，
# 就取 13×3=39 根 5m，覆盖的时间跨度一样。
_OKX_OI_PERIOD = {"5m": ("5m", 1), "15m": ("5m", 3), "30m": ("5m", 6),
                  "1h": ("1H", 1), "2h": ("1H", 2), "4h": ("1H", 4),
                  "12h": ("1H", 12), "1d": ("1D", 1)}


async def oi_series(symbol, ex="bybit", interval="15m", limit=13):
    """持仓量序列（**币的数量**，旧→新）。这家给不了就返回 []——
    扫描器对缺失是如实标 None 的，不需要我编一个数出来。"""
    base = src_mod.norm(symbol)
    c = src_mod.client()
    try:
        if ex == "bybit":
            r = await c.get("https://api.bybit.com/v5/market/open-interest",
                            params={"category": "linear", "symbol": f"{base}USDT",
                                    "intervalTime": {"5m": "5min", "15m": "15min",
                                                     "1h": "1h", "4h": "4h",
                                                     "1d": "1d"}.get(interval, "15min"),
                                    "limit": limit})
            rows = ((r.json().get("result") or {}).get("list") or [])[::-1]
            return [float(x["openInterest"]) for x in rows]
        if ex == "binance":
            r = await c.get("https://fapi.binance.com/futures/data/openInterestHist",
                            params={"symbol": f"{base}USDT", "period": interval,
                                    "limit": limit})
            rows = r.json()
            return [float(x["sumOpenInterest"]) for x in rows] \
                if isinstance(rows, list) else []
        if ex == "gate":
            r = await c.get("https://api.gateio.ws/api/v4/futures/usdt/contract_stats",
                            params={"contract": f"{base}_USDT", "interval": interval,
                                    "limit": limit})
            rows = r.json()
            if not isinstance(rows, list):
                return []
            u = await contract_units("gate")
            mult = float(u.get(base) or 1.0)
            return [float(x.get("open_interest") or 0) * mult for x in rows]
        if ex == "okx":
            # OKX 没有"按合约"的持仓量历史，只有 rubik 的**按币种聚合**统计
            # （同一个币的所有合约加总，含币本位）。当代理指标用够了——
            # 扫描器要的是"OI 在涨还是在跌"这个变化率，不是绝对值。
            # 但它给的是**美元名义**，和别家的"币数量"不同口径，
            # 所以只能内部比自己的首尾，绝不能和别家的数直接比。
            # rubik 的 period 只认 5m / 1H / 1D（实测 15m、1h、4H 都报 51000
            # 参数错误）。所以把请求周期折到它支持的档，再按时间跨度换算根数——
            # 直接把 "15m" 传过去会静默拿到空列表，表现成"这家没有持仓量"。
            per, ratio = _OKX_OI_PERIOD.get(norm_interval(interval), ("5m", 3))
            want = max(2, int(limit * ratio))
            r = await c.get(
                "https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume",
                params={"ccy": base, "period": per})
            d = r.json()
            rows = d.get("data") or []
            if d.get("code") != "0" or not rows:
                return []
            # 返回是新→旧；反成旧→新后取够覆盖同样时间跨度的根数
            return [float(x[1]) for x in reversed(rows)][-want:]
    except Exception as e:
        log.debug(f"取持仓量失败 {symbol} {ex}: {e}")
    return []
