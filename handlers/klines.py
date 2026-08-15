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
                        # OKX 自己不标品类，只能靠别家标注过的基名反查
                        "crypto": iid[:-len(tail)] not in bad})
    elif ex == "binance":
        url = ("https://fapi.binance.com/fapi/v1/ticker/24hr" if market == SWAP
               else "https://api.binance.com/api/v3/ticker/24hr")
        rows = (await c.get(url)).json()
        if not isinstance(rows, list):
            return []
        for t in rows:
            s = t.get("symbol") or ""
            if not s.endswith("USDT"):
                continue
            base = s[:-4]
            out.append({"symbol": base,
                        "turnover": float(t.get("quoteVolume") or 0),
                        "change": float(t.get("priceChangePercent") or 0),
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
                            "crypto": not gate_mod._is_leveraged(base)
                            and base not in bad})
    out.sort(key=lambda x: -x["turnover"])
    return out


_LEV_SUFFIX_BN = ("UP", "DOWN", "BULL", "BEAR")


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
