"""主流币「弱势 / 横盘」扫描器（独立脚本，无需启动 bot）。

一次性拉取市值 Top N 币，输出三张榜：
  1. 最横盘   —— 7天涨跌接近 0 且 24h 波动小（低波动盘整）
  2. 最弱     —— 7天跌幅最大
  3. 相对抗跌 —— 相对 BTC 的 7 天相对强弱(RS)最强

用法：
  python weak_scan.py                # 默认扫市值前 50
  python weak_scan.py --top 80       # 扫前 80
  python weak_scan.py --vol          # 额外拉日线算 30 天真实波动率(慢，多 N 次请求)

数据源：CoinGecko 公共接口，无需 API key。⚠️ 结果不构成投资建议。
"""

import argparse
import sys
import time
import httpx

# Windows 旧控制台默认非 UTF-8，打印 emoji/箭头会 UnicodeEncodeError，先兜底
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://api.coingecko.com/api/v3"

# 稳定币 / 包装币 / 质押衍生品：不算「主流币行情」，过滤掉
SKIP = {
    # 稳定币
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD",
    "USDD", "GUSD", "USDP", "FRAX", "LUSD", "BUSD", "EURC", "USD1",
    # 包装 / 质押 / LSD 衍生品（价格跟随母币，无独立行情）
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "WBETH", "RETH",
    "CBBTC", "LBTC", "SOLVBTC", "BSC-USD", "WBNB", "JITOSOL", "MSOL",
}


def _get(client, path, params):
    """带简单重试的 GET；命中限流(429)时退避重试。"""
    for attempt in range(4):
        r = client.get(f"{BASE}{path}", params=params)
        if r.status_code == 429:
            wait = 5 * (attempt + 1)
            print(f"  · 触发限流，等待 {wait}s 重试...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def fetch_markets(client, top):
    """拉市值 Top N，带 24h/7d/30d 涨跌 + 24h 高低。"""
    rows = _get(client, "/coins/markets", {
        "vs_currency": "usd",
        "order": "market_cap_desc",
        "per_page": top,
        "page": 1,
        "price_change_percentage": "24h,7d,30d",
    })
    coins = []
    for c in rows:
        sym = c["symbol"].upper()
        if sym in SKIP:
            continue
        price = c.get("current_price") or 0
        hi = c.get("high_24h") or price
        lo = c.get("low_24h") or price
        # 24h 波动幅度 = (高-低)/现价，作为「近期是否安静」的廉价代理
        rng = (hi - lo) / price * 100 if price else 0.0
        coins.append({
            "sym": sym,
            "price": price,
            "rank": c.get("market_cap_rank"),
            "c24": c.get("price_change_percentage_24h_in_currency") or 0.0,
            "c7": c.get("price_change_percentage_7d_in_currency") or 0.0,
            "c30": c.get("price_change_percentage_30d_in_currency") or 0.0,
            "range24": rng,
            "vol30": None,  # --vol 时填真实 30 天日线波动率
        })
    return coins


def real_vol(client, coin_id):
    """真实 30 天波动率 = 日收益率标准差 ×100（年化前的日波动）。"""
    raw = _get(client, f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd", "days": 30, "interval": "daily",
    })
    px = [p[1] for p in raw.get("prices", []) if p[1]]
    if len(px) < 5:
        return None
    rets = [(px[i] - px[i - 1]) / px[i - 1] for i in range(1, len(px))]
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / len(rets)
    return var ** 0.5 * 100


def enrich_vol(client, coins, top):
    """给每个币补真实波动率（逐个请求，受限流影响较慢）。"""
    ids = _get(client, "/coins/markets", {
        "vs_currency": "usd", "order": "market_cap_desc",
        "per_page": top, "page": 1,
    })
    id_map = {c["symbol"].upper(): c["id"] for c in ids}
    for i, c in enumerate(coins, 1):
        cid = id_map.get(c["sym"])
        if not cid:
            continue
        try:
            c["vol30"] = real_vol(client, cid)
        except Exception:
            c["vol30"] = None
        print(f"  · 波动率 {i}/{len(coins)} {c['sym']}", end="\r")
        time.sleep(2.2)  # 尊重 CoinGecko 公共接口限流
    print(" " * 40, end="\r")


def sideways_score(c):
    """横盘分：越小越『没怎么动』。
    = 0.6×|7天涨跌| + 0.4×24h波动幅度（都取绝对值）。"""
    return 0.6 * abs(c["c7"]) + 0.4 * c["range24"]


def fmt_pct(x):
    return f"{x:+6.2f}%"


def bar(x, width=10):
    """小涨跌条：正绿(▲) 负红(▼)，长度按幅度。"""
    n = min(width, int(abs(x) / 2))
    ch = "▲" if x >= 0 else "▼"
    return ch * max(1, n)


def main():
    ap = argparse.ArgumentParser(description="主流币弱势/横盘扫描")
    ap.add_argument("--top", type=int, default=50, help="扫描市值前 N 个币（默认 50）")
    ap.add_argument("--vol", action="store_true", help="额外拉日线算真实 30 天波动率（慢）")
    ap.add_argument("--n", type=int, default=12, help="每张榜显示条数（默认 12）")
    args = ap.parse_args()

    with httpx.Client(timeout=20, headers={"accept": "application/json"}) as client:
        print(f"⏳ 拉取市值 Top {args.top} ...")
        coins = fetch_markets(client, args.top)
        if not coins:
            print("没拿到数据，稍后再试。")
            return

        # 大盘锚：BTC 7 天涨跌，用于算相对强弱
        btc = next((c for c in coins if c["sym"] == "BTC"), None)
        btc7 = btc["c7"] if btc else 0.0

        if args.vol:
            print("⏳ 计算真实波动率（受限流影响，请稍候）...")
            enrich_vol(client, coins, args.top)

    n = args.n

    # ---- 榜1：最横盘（没怎么动）----
    flat = sorted(coins, key=sideways_score)[:n]
    print("\n" + "=" * 62)
    print("😴 最横盘 / 没怎么涨没怎么跌（低波动盘整）")
    print("=" * 62)
    print(f"{'币':<7}{'现价':>12}  {'7天':>8} {'30天':>8}  {'24h幅度':>7}")
    for c in flat:
        vol = f"  σ{c['vol30']:.1f}%" if c["vol30"] is not None else ""
        print(f"{c['sym']:<7}{c['price']:>12,.4g}  {fmt_pct(c['c7'])} "
              f"{fmt_pct(c['c30'])}  {c['range24']:>6.1f}%{vol}")

    # ---- 榜2：最弱（7天跌最多）----
    weak = sorted(coins, key=lambda c: c["c7"])[:n]
    print("\n" + "=" * 62)
    print("💥 最弱势（近 7 天跌幅最大）")
    print("=" * 62)
    print(f"{'币':<7}{'现价':>12}  {'7天':>8} {'趋势':<12}")
    for c in weak:
        print(f"{c['sym']:<7}{c['price']:>12,.4g}  {fmt_pct(c['c7'])} {bar(c['c7'])}")

    # ---- 榜3：相对抗跌（相对 BTC 的 7 天 RS 最强）----
    for c in coins:
        c["rs"] = c["c7"] - btc7
    strong = sorted(coins, key=lambda c: c["rs"], reverse=True)[:n]
    print("\n" + "=" * 62)
    print(f"🛡️ 相对抗跌 / 强于大盘（RS = 该币7天 − BTC7天，BTC={btc7:+.2f}%）")
    print("=" * 62)
    print(f"{'币':<7}{'现价':>12}  {'7天':>8} {'相对BTC':>9}")
    for c in strong:
        print(f"{c['sym']:<7}{c['price']:>12,.4g}  {fmt_pct(c['c7'])} {c['rs']:>+8.2f}%")

    print("\n" + "-" * 62)
    print("说明：横盘≠蓄势，弱市里『没跌』常是『没人碰』；RS 只表示相对强弱，")
    print("      不代表绝对上涨。⚠️ 以上为数据整理，不构成投资建议。")


if __name__ == "__main__":
    main()
