"""群升级迁移 + data.json 清理——迁移漏字段会让订阅静默失效（踩过）。"""
import time
import storage

OLD, NEW = -123456789, -1003950673952


def _seed():
    storage.data.update({
        "broadcast_chats": [OLD, 999], "market_watch": [str(OLD)], "contract_watch": [OLD],
        "news_subs": [OLD], "summary_subs": [OLD], "analysis_subs": [], "unlock_subs": [],
        "watchpct": [{"chat_id": OLD, "symbol": "AKE"}, {"chat_id": 999, "symbol": "BTC"}],
        "alerts": [{"chat_id": OLD, "symbol": "BTC"}], "ti_alerts": [{"chat_id": OLD}],
        "gas_subs": {str(OLD): {"threshold": 15}}, "arb_subs": {str(OLD): {"threshold": 1}},
        "whale_addr": {str(OLD): {"0xabc": {}}}, "whale_min": {str(OLD): 10000},
        "holding_watch": {"777": OLD},
        "rtrade_alert": {"enabled": True, "chat_id": OLD},
        "vtrade": {"777": {"balance": 10000, "chat_id": OLD}},
    })


def test_migrate_chat_moves_every_structure():
    _seed()
    moved = storage.migrate_chat(OLD, NEW)
    d = storage.data
    assert moved > 0
    assert NEW in d["broadcast_chats"] and 999 in d["broadcast_chats"]   # 无关会话不动
    assert d["market_watch"] == [NEW]                                    # str 混存也要迁
    assert d["watchpct"][0]["chat_id"] == NEW
    assert d["watchpct"][1]["chat_id"] == 999                            # 别人的不动
    assert d["alerts"][0]["chat_id"] == NEW and d["ti_alerts"][0]["chat_id"] == NEW
    assert str(NEW) in d["gas_subs"] and str(OLD) not in d["gas_subs"]
    assert str(NEW) in d["whale_addr"] and str(NEW) in d["whale_min"]
    assert d["holding_watch"]["777"] == NEW
    assert d["rtrade_alert"]["chat_id"] == NEW
    assert d["vtrade"]["777"]["chat_id"] == NEW


def test_migrate_chat_is_idempotent():
    _seed()
    storage.migrate_chat(OLD, NEW)
    assert storage.migrate_chat(OLD, NEW) == 0      # 再迁一次没东西可迁


def test_prune_drops_expired_and_caps_history():
    now = time.time()
    storage.data.update({
        "alerted_coins": {"OLD": now - 30 * 86400, "NEW": now},
        "contract_alerted": {"A:down:90": now - 10 * 86400, "B:up:20": now},
        "contract_tiers": {"STALE": {"ts": now - 10 * 86400}, "FRESH": {"ts": now}},
        "pushed_news": [f"link{i}" for i in range(900)],
        "vtrade": {"777": {"history": [{"pnl": 1} for _ in range(500)]}},
        "rtrade_alert": {"cooldown": {"OLDSYM": now - 10 * 86400, "NEWSYM": now}},
    })
    storage.prune_data(now=now)
    d = storage.data
    assert "OLD" not in d["alerted_coins"] and "NEW" in d["alerted_coins"]
    assert "A:down:90" not in d["contract_alerted"] and "B:up:20" in d["contract_alerted"]
    assert "STALE" not in d["contract_tiers"] and "FRESH" in d["contract_tiers"]
    assert len(d["pushed_news"]) == 500 and d["pushed_news"][-1] == "link899"  # 保留最新
    assert len(d["vtrade"]["777"]["history"]) == 200
    assert "OLDSYM" not in d["rtrade_alert"]["cooldown"]
