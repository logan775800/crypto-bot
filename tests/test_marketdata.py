

# ── 清算取不到时要分清"源挂了"和"这个币没上 OKX" ──────────────
def test_unlisted_coin_gets_a_human_explanation():
    """真机：群友问 BTR 空头清算，回了句「OKX 聚合源返回 400 Bad Request」。
    技术上没错，但那是内部异常——实测 BTC-USDT 好好的、BTR-USDT 回
    `51000 Parameter instFamily error`，意思是 **OKX 根本没上这个币**。
    这不是故障，而且必须给替代方案。"""
    import asyncio
    from handlers import marketdata as M

    async def _boom(sym):
        raise RuntimeError("HTTP 400 51000 Parameter instFamily error")

    import handlers.okx as O
    old = O.build_liq_text
    O.build_liq_text = _boom
    try:
        txt = asyncio.run(M.liquidation_analysis("BTR"))
    finally:
        O.build_liq_text = old
    assert "没有在 OKX 上市" in txt
    assert "不是故障" in txt
    assert "/liqmap" in txt, "要给一条还能用的替代路径"
    assert "不可" in txt, "仍然不许模型编出具体笔数"


def test_a_real_outage_still_reads_as_an_outage():
    """源真挂了（超时/500）不能说成"这个币没上"——两种成因说的话必须不同。"""
    import asyncio
    from handlers import marketdata as M

    async def _boom(sym):
        raise RuntimeError("ReadTimeout")

    import handlers.okx as O
    old = O.build_liq_text
    O.build_liq_text = _boom
    try:
        txt = asyncio.run(M.liquidation_analysis("BTC"))
    finally:
        O.build_liq_text = old
    assert "取数失败" in txt
    assert "没有在 OKX 上市" not in txt
