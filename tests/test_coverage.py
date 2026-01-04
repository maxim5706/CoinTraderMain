from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from core.candle_store import CandleStore
from core.coverage import compute_coverage_map
from core.models import Candle, CandleBuffer
from ui import web_server


def test_coverage_map_statuses():
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    buffer = CandleBuffer(symbol="BTC-USD")
    buffer.add_1m(Candle(
        timestamp=now - timedelta(seconds=30),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    ))
    buffer.add_5m_direct(Candle(
        timestamp=now - timedelta(minutes=10),
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
    ))

    data = compute_coverage_map(
        ["BTC-USD"],
        buffer_provider=lambda _: buffer,
        now=now,
        use_store_fallback=False,
    )
    tf = data["symbols"]["BTC-USD"]["timeframes"]
    assert data["computed_symbols"] == ["BTC-USD"]
    assert data["universe_size"] == 1
    assert data["computed_size"] == 1
    assert data["truncated"] is False
    assert tf["1m"]["status"] == "OK"
    assert tf["1m"]["source"] == "buffer"
    assert tf["5m"]["status"] == "STALE"
    assert tf["5m"]["source"] == "buffer"
    assert tf["1h"]["status"] == "MISSING"
    assert tf["1h"]["source"] == "none"


def test_api_coverage_schema(tmp_path, monkeypatch):
    store = CandleStore(base_dir=tmp_path)
    now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    store.write_candles("BTC-USD", [
        Candle(
            timestamp=now,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1.0,
        )
    ], "1m", source="rest")

    import core.candle_store as candle_store_module
    monkeypatch.setattr(candle_store_module, "candle_store", store)

    client = TestClient(web_server.app)
    resp = client.get("/api/coverage")
    assert resp.status_code == 200
    payload = resp.json()
    assert "universe" in payload
    assert "symbols" in payload
    assert "timeframes" in payload
    assert "computed_symbols" in payload
    assert payload["universe_size"] == len(payload["universe"])
    assert payload["computed_size"] == len(payload["computed_symbols"])
    assert isinstance(payload["truncated"], bool)
    assert "BTC-USD" in payload["symbols"]
    assert "1m" in payload["timeframes"]
