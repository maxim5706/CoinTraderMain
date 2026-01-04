from datetime import datetime, timedelta, timezone

from ui import web_server


def test_compute_health_ok():
    now = datetime.now(timezone.utc)
    snapshot = {
        "ts": now.isoformat(),
        "heartbeats": {"ws": 1, "scanner": 2, "order_router": 2, "candles_1m": 10},
        "rest_rate_degraded": False,
    }
    health = web_server._compute_health(snapshot)
    assert health["status"] == "OK"
    assert health["reasons"] == []


def test_compute_health_stale_state_age():
    old = datetime.now(timezone.utc) - timedelta(seconds=20)
    snapshot = {"ts": old.isoformat(), "heartbeats": {}}
    health = web_server._compute_health(snapshot)
    assert health["status"] == "STALE"
    assert "state_stale" in health["reasons"]


def test_compute_health_degraded_component():
    now = datetime.now(timezone.utc)
    snapshot = {
        "ts": now.isoformat(),
        "heartbeats": {"ws": 1, "scanner": 40, "order_router": 5, "candles_1m": 10},
        "rest_rate_degraded": False,
    }
    health = web_server._compute_health(snapshot)
    assert health["status"] == "DEGRADED"
    assert "scanner_heartbeat_old" in health["reasons"]


def test_state_snapshot_contract(monkeypatch):
    # Force fallback snapshot
    monkeypatch.setattr(web_server, "_bot_state", None)
    monkeypatch.setattr(
        web_server, "get_bot_status", lambda: {"running": False, "pid": None, "mode": "paper", "status": "stopped"}
    )
    snap = web_server.get_state_snapshot()
    assert "ts" in snap
    assert "heartbeats" in snap
    assert "health" in snap
    assert "state_age_s" in snap
    assert "capabilities" in snap
    assert "process_control" in snap["capabilities"]
