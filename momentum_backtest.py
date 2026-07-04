"""加密「动量轮动」策略回测（独立脚本，无需启动 bot）。

把游资『只追最强主线、不碰弱势』的思路量化：
  每隔 --rebalance 天，按过去 --lookback 天涨幅给全体币排名，
  等权持有最强的 --hold 个币；开启 --cash-filter 时，动量为负的仓位空仓(持币不入场)。
最后对比三条曲线：策略 / 死拿BTC / 等权持有全体，看这套『追强势』在币圈是否真有超额收益。

用法：
  python momentum_backtest.py                     # 默认：Top30宇宙, 1年, 30天动量, 持3, 周调
  python momentum_backtest.py --lookback 20 --hold 5 --rebalance 3
  python momentum_backtest.py --no-cash-filter    # 不做趋势过滤(始终满仓最强K个)

数据源：CoinGecko 公共接口，无需 API key。⚠️ 回测≠未来，不构成投资建议。
"""

import argparse
import sys
import time
import datetime as dt
import httpx

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE = "https://api.coingecko.com/api/v3"

# 稳定币 / 包装币 / 质押衍生品：无独立行情，剔除
SKIP = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD",
    "USDD", "GUSD", "USDP", "FRAX", "LUSD", "BUSD", "EURC", "USD1",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "WBETH", "RETH",
    "CBBTC", "LBTC", "SOLVBTC", "BSC-USD", "WBNB", "JITOSOL", "MSOL",
}


def _get(client, path, params):
    for attempt in range(5):
        r = client.get(f"{BASE}{path}", params=params)
        if r.status_code == 429:
            wait = 6 * (attempt + 1)
            print(f"  · 限流，等待 {wait}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    r.raise_for_status()


def universe(client, top):
    """市值 Top N 的 (symbol, coin_id) 列表，已过滤稳定/包装币。"""
    rows = _get(client, "/coins/markets", {
        "vs_currency": "usd", "order": "market_cap_desc",
        "per_page": top, "page": 1,
    })
    out = []
    for c in rows:
        sym = c["symbol"].upper()
        if sym in SKIP:
            continue
        out.append((sym, c["id"]))
    return out


def daily_series(client, coin_id, days):
    """{ 'YYYY-MM-DD': close } —— 按 UTC 日期归一，便于跨币对齐。"""
    raw = _get(client, f"/coins/{coin_id}/market_chart", {
        "vs_currency": "usd", "days": days, "interval": "daily",
    })
    series = {}
    for ts_ms, price in raw.get("prices", []):
        if not price:
            continue
        d = dt.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")
        series[d] = price  # 同日多点取最后一个
    return series


def build_panel(client, coins, days):
    """拉全宇宙日线，返回 (dates 升序列表, {sym: {date: price}})。"""
    panel = {}
    for i, (sym, cid) in enumerate(coins, 1):
        print(f"  · 拉日线 {i}/{len(coins)} {sym}       ", end="\r", file=sys.stderr)
        try:
            s = daily_series(client, cid, days)
            if len(s) >= 10:
                panel[sym] = s
        except Exception as e:
            print(f"\n  ! {sym} 拉取失败: {e}", file=sys.stderr)
        time.sleep(2.2)  # 尊重公共接口限流
    print(" " * 50, file=sys.stderr)
    # 用并集日期作为主轴（缺失日在用到时按『无数据』跳过该币）
    all_dates = sorted({d for s in panel.values() for d in s})
    return all_dates, panel


def ret(series, d_now, d_past):
    """d_past→d_now 的收益率；任一端缺数据返回 None。"""
    a = series.get(d_past)
    b = series.get(d_now)
    if a and b and a > 0:
        return b / a - 1
    return None


def max_drawdown(equity):
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return mdd


def stats(equity, n_days):
    """总收益、年化、最大回撤、年化波动、夏普(rf=0)。"""
    total = equity[-1] / equity[0] - 1
    years = n_days / 365.0 if n_days else 1
    cagr = (equity[-1] / equity[0]) ** (1 / years) - 1 if years > 0 else 0
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    if rets:
        mean = sum(rets) / len(rets)
        var = sum((x - mean) ** 2 for x in rets) / len(rets)
        vol_d = var ** 0.5
        ann_vol = vol_d * (365 ** 0.5)
        sharpe = (mean * 365) / ann_vol if ann_vol else 0
    else:
        ann_vol = sharpe = 0
    return {"total": total, "cagr": cagr, "mdd": max_drawdown(equity),
            "vol": ann_vol, "sharpe": sharpe}


def backtest(dates, panel, lookback, hold, rebalance, cash_filter, fee):
    """返回策略每日净值曲线 + 每次调仓记录。"""
    # 只在『有足够历史』的区间回测：从第 lookback 天之后开始
    start_i = lookback
    if start_i >= len(dates):
        raise SystemExit("历史长度不足以覆盖 lookback，减小 --lookback 或增大 --days")

    equity = [1.0]
    curve_dates = [dates[start_i]]
    fee_r = fee / 100.0
    holdings = []          # 当前持仓 symbol 列表
    rebal_log = []
    prev_weight_syms = set()

    for i in range(start_i + 1, len(dates)):
        d_prev, d_now = dates[i - 1], dates[i]

        # 到调仓日：按过去 lookback 天动量重排
        if (i - start_i - 1) % rebalance == 0:
            d_look = dates[i - 1 - lookback]
            ranked = []
            for sym, s in panel.items():
                m = ret(s, d_prev, d_look)
                if m is not None:
                    ranked.append((sym, m))
            ranked.sort(key=lambda x: x[1], reverse=True)
            picks = [sym for sym, m in ranked[:hold]
                     if (m > 0 or not cash_filter)]  # 趋势过滤：动量为负则空这个仓
            holdings = picks
            # 换手费：与上期持仓的差异部分计费
            turnover = len(set(picks) ^ prev_weight_syms)
            equity[-1] *= (1 - fee_r * turnover / max(hold, 1))
            prev_weight_syms = set(picks)
            rebal_log.append((d_prev, list(picks),
                              [round(m * 100, 1) for s, m in ranked[:hold]]))

        # 当日组合收益 = 持仓等权日收益（空仓部分收益0）
        if holdings:
            day_rets = []
            for sym in holdings:
                r = ret(panel[sym], d_now, d_prev)
                day_rets.append(r if r is not None else 0.0)
            # 等权：分母固定为 hold，未填满的算现金(0)，体现『没强势可追就空着』
            port_ret = sum(day_rets) / hold
        else:
            port_ret = 0.0
        equity.append(equity[-1] * (1 + port_ret))
        curve_dates.append(d_now)

    return curve_dates, equity, rebal_log


def bench_curve(dates, series, start_i):
    """基准净值：从 start_i 起按某序列的日收益复利（缺失日按0）。"""
    eq = [1.0]
    for i in range(start_i + 1, len(dates)):
        r = ret(series, dates[i], dates[i - 1])
        eq.append(eq[-1] * (1 + (r if r is not None else 0.0)))
    return eq


def equal_weight_series(dates, panel):
    """等权持有全宇宙的『合成序列』：每日取所有有数据币的平均日收益复利成价格。"""
    synth = {}
    prev_price = 1.0
    for i, d in enumerate(dates):
        if i == 0:
            synth[d] = prev_price
            continue
        rs = [ret(s, d, dates[i - 1]) for s in panel.values()]
        rs = [x for x in rs if x is not None]
        avg = sum(rs) / len(rs) if rs else 0.0
        prev_price *= (1 + avg)
        synth[d] = prev_price
    return synth


def fmt(s):
    return (f"总收益 {s['total']*100:>8.1f}%  |  年化 {s['cagr']*100:>7.1f}%  |  "
            f"最大回撤 {s['mdd']*100:>7.1f}%  |  年化波动 {s['vol']*100:>6.1f}%  |  "
            f"夏普 {s['sharpe']:>5.2f}")


def main():
    ap = argparse.ArgumentParser(description="加密动量轮动策略回测")
    ap.add_argument("--top", type=int, default=30, help="宇宙大小(市值前N，默认30)")
    ap.add_argument("--days", type=int, default=365, help="回测历史天数(默认365)")
    ap.add_argument("--lookback", type=int, default=30, help="动量回看天数(默认30)")
    ap.add_argument("--hold", type=int, default=3, help="持有最强的K个币(默认3)")
    ap.add_argument("--rebalance", type=int, default=7, help="调仓周期天数(默认7)")
    ap.add_argument("--fee", type=float, default=0.1, help="单边换手费%(默认0.1)")
    ap.add_argument("--no-cash-filter", dest="cash_filter", action="store_false",
                    help="关闭趋势过滤(动量为负也满仓最强K个)")
    ap.set_defaults(cash_filter=True)
    args = ap.parse_args()

    with httpx.Client(timeout=25, headers={"accept": "application/json"}) as client:
        print(f"⏳ 构建宇宙(Top {args.top})...", file=sys.stderr)
        coins = universe(client, args.top)
        print(f"⏳ 拉取 {len(coins)} 个币近 {args.days} 天日线...", file=sys.stderr)
        dates, panel = build_panel(client, coins, args.days)

    if len(dates) < args.lookback + args.rebalance + 5:
        raise SystemExit("历史数据太短，增大 --days 或减小 --lookback")

    cd, eq, log = backtest(dates, panel, args.lookback, args.hold,
                           args.rebalance, args.cash_filter, args.fee)
    n_days = len(cd)
    start_i = args.lookback

    # 基准：死拿BTC / 等权持有全体
    btc = panel.get("BTC")
    btc_eq = bench_curve(dates, btc, start_i) if btc else None
    ew_eq = bench_curve(dates, equal_weight_series(dates, panel), start_i)

    print("\n" + "=" * 78)
    print(f"📊 动量轮动回测  |  宇宙Top{args.top}  回看{args.lookback}天  "
          f"持{args.hold}  每{args.rebalance}天调仓  "
          f"趋势过滤{'开' if args.cash_filter else '关'}  费{args.fee}%")
    print(f"   区间：{cd[0]} → {cd[-1]}  （{n_days} 天）")
    print("=" * 78)
    print(f"🚀 动量策略   {fmt(stats(eq, n_days))}")
    if btc_eq:
        print(f"🟠 死拿BTC    {fmt(stats(btc_eq, n_days))}")
    print(f"⚪ 等权全体   {fmt(stats(ew_eq, n_days))}")

    # 超额
    if btc_eq:
        alpha = (eq[-1] / eq[0]) - (btc_eq[-1] / btc_eq[0])
        verdict = "跑赢BTC ✅" if alpha > 0 else "跑输BTC ❌"
        print("-" * 78)
        print(f"   策略 vs 死拿BTC：{verdict}  (差 {alpha*100:+.1f} 个百分点)")

    # 最近几次调仓选了谁
    print("-" * 78)
    print("最近 6 次调仓选中的最强币（括号=当期回看动量）：")
    for d, picks, moms in log[-6:]:
        tags = ", ".join(f"{p}({m:+.0f}%)" for p, m in zip(picks, moms)) or "空仓"
        print(f"  {d}  → {tags}")

    print("-" * 78)
    print("提示：改 --lookback/--hold/--rebalance 会显著改变结果；单一历史区间的")
    print("      好成绩极可能是过拟合。⚠️ 回测≠未来收益，不构成投资建议。")


if __name__ == "__main__":
    main()
