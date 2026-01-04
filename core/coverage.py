"""Coverage map computation for per-symbol/per-timeframe data visibility."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, Iterable, Optional

from core.config import settings
from core.candle_store import candle_store as _default_store

DEFAULT_TIMEFRAMES = ("1m", "5m", "1h", "1d")
DEFAULT_STALE_THRESHOLDS = {
    "1m": 90,         # 1.5 minutes
    "5m": 8 * 60,     # 8 minutes
    "1h": 2 * 3600,   # 2 hours
    "1d": 2 * 86400,  # 2 days
}
DEFAULT_MAX_COMPUTED_SYMBOLS = 200


class CoverageStatus(str, Enum):
    OK = "OK"
    STALE = "STALE"
    MISSING = "MISSING"
    FAILING = "FAILING"


@dataclass
class TimeframeCoverage:
    last_candle_ts: Optional[str] = None
    age_seconds: Optional[float] = None
    bars_available: int = 0
    status: str = CoverageStatus.MISSING.value
    reasons: list[str] = field(default_factory=list)
    source: str = "none"


def _safe_ts(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _extract_symbols_from_state(state) -> list[str]:
    symbols: list[str] = []
    if not state:
        return symbols

    if isinstance(state, dict):
        positions = state.get("positions", []) or []
        burst = state.get("burst_leaderboard", []) or []
    else:
        positions = getattr(state, "positions", []) or []
        burst = getattr(state, "burst_leaderboard", []) or []

    for pos in positions:
        if isinstance(pos, dict):
            sym = pos.get("symbol")
        else:
            sym = getattr(pos, "symbol", None)
        if sym:
            symbols.append(sym)

    for row in burst:
        if isinstance(row, dict):
            sym = row.get("symbol")
        else:
            sym = getattr(row, "symbol", None)
        if sym:
            symbols.append(sym)

    return symbols


def _extract_symbol_info(scanner, symbol: str) -> dict:
    if not scanner:
        return {}
    info = getattr(scanner, "universe", {}).get(symbol) if hasattr(scanner, "universe") else None
    if not info:
        return {}
    return {
        "spread_bps": getattr(info, "avg_spread_bps", None),
        "volume_24h_usd": getattr(info, "volume_24h_usd", None),
    }


def _get_universe_symbols(state=None, scanner=None, store=None) -> list[str]:
    symbols: list[str] = []

    if scanner:
        try:
            eligible = scanner.get_eligible_symbols()
            symbols.extend(eligible)
        except Exception:
            symbols.extend(list(getattr(scanner, "universe", {}).keys()))

    symbols.extend(_extract_symbols_from_state(state))

    if store:
        try:
            symbols.extend(store.list_symbols())
        except Exception:
            pass

    try:
        symbols.extend(settings.coins)
    except Exception:
        pass

    # Dedupe while preserving order
    seen = set()
    ordered: list[str] = []
    for sym in symbols:
        if sym and sym not in seen:
            seen.add(sym)
            ordered.append(sym)
    return ordered


def compute_coverage_map(
    symbols: Iterable[str],
    *,
    buffer_provider: Optional[Callable[[str], object]] = None,
    store=None,
    now: Optional[datetime] = None,
    timeframes: Iterable[str] = DEFAULT_TIMEFRAMES,
    stale_thresholds: Optional[Dict[str, int]] = None,
    ws_ok: Optional[bool] = None,
    rest_ok: Optional[bool] = None,
    scanner=None,
    max_symbols: Optional[int] = DEFAULT_MAX_COMPUTED_SYMBOLS,
    use_store_fallback: bool = True,
) -> dict:
    """Compute coverage for a set of symbols."""
    current = now or datetime.now(timezone.utc)
    thresholds = dict(DEFAULT_STALE_THRESHOLDS)
    if stale_thresholds:
        thresholds.update(stale_thresholds)

    all_symbols = list(symbols)
    universe_size = len(all_symbols)
    limit = max_symbols if max_symbols and max_symbols > 0 else universe_size
    computed_symbols = all_symbols[:limit]
    truncated = len(computed_symbols) < universe_size
    tf_list = list(timeframes)
    coverage: dict = {}
    summary = {tf: {k: 0 for k in CoverageStatus.__members__} for tf in tf_list}

    for symbol in computed_symbols:
        tf_map: dict[str, dict] = {}
        buffer = buffer_provider(symbol) if buffer_provider else None
        symbol_info = _extract_symbol_info(scanner, symbol)

        for tf in tf_list:
            reasons: list[str] = []
            bars = 0
            last_ts: Optional[datetime] = None
            source = "none"

            if buffer is not None:
                candles = getattr(buffer, f"candles_{tf}", [])
                bars = len(candles)
                if candles:
                    last_ts = candles[-1].timestamp
                    source = "buffer"
                else:
                    reasons.append("no_candles")
            else:
                reasons.append("no_buffer")

            if last_ts is None:
                if use_store_fallback and store is not None:
                    last_ts = store.get_last_candle_ts(symbol, tf)
                    if last_ts is None:
                        reasons.append("no_store")
                    else:
                        source = "store"
                elif store is not None and not use_store_fallback:
                    reasons.append("store_fallback_disabled")

            if last_ts is None:
                status = CoverageStatus.MISSING
                age_seconds = None
            else:
                age_seconds = (current - last_ts).total_seconds()
                threshold = thresholds.get(tf, 0)
                status = CoverageStatus.OK if age_seconds <= threshold else CoverageStatus.STALE
                if status == CoverageStatus.STALE:
                    reasons.append("stale_age>threshold")

            upstream_down = False
            if ws_ok is False and tf in ("1m", "5m"):
                reasons.append("ws_down")
                upstream_down = True
            if rest_ok is False and tf in ("1h", "1d"):
                reasons.append("rest_down")
                upstream_down = True
            if upstream_down and status != CoverageStatus.MISSING:
                status = CoverageStatus.FAILING

            entry = TimeframeCoverage(
                last_candle_ts=_safe_ts(last_ts),
                age_seconds=age_seconds,
                bars_available=bars,
                status=status.value,
                reasons=reasons,
                source=source,
            )
            tf_map[tf] = asdict(entry)
            summary[tf][status.name] += 1

        coverage[symbol] = {
            "timeframes": tf_map,
            **symbol_info,
        }

    return {
        "ts": current.isoformat(),
        "timeframes": tf_list,
        "summary": summary,
        "symbols": coverage,
        "computed_symbols": computed_symbols,
        "computed_size": len(computed_symbols),
        "universe_size": universe_size,
        "truncated": truncated,
    }


def build_coverage_snapshot(
    *,
    state=None,
    scanner=None,
    buffer_provider: Optional[Callable[[str], object]] = None,
    store=None,
    now: Optional[datetime] = None,
    max_symbols: Optional[int] = DEFAULT_MAX_COMPUTED_SYMBOLS,
    use_store_fallback: bool = True,
) -> dict:
    """Build a coverage snapshot with universe metadata and summary counts."""
    universe = _get_universe_symbols(state=state, scanner=scanner, store=store or _default_store)

    ws_ok = None
    rest_ok = None
    if state is not None:
        if isinstance(state, dict):
            ws_ok = state.get("ws_ok")
            rest_ok = not bool(state.get("rest_rate_degraded", False))
        else:
            try:
                ws_ok = getattr(state, "ws_ok", None)
            except Exception:
                ws_ok = None
            try:
                rest_ok = not bool(getattr(state, "rest_rate_degraded", False))
            except Exception:
                rest_ok = None

    payload = compute_coverage_map(
        universe,
        buffer_provider=buffer_provider,
        store=store,
        now=now,
        ws_ok=ws_ok,
        rest_ok=rest_ok,
        scanner=scanner,
        max_symbols=max_symbols,
        use_store_fallback=use_store_fallback,
    )
    payload["universe"] = universe
    payload["universe_size"] = len(universe)
    payload["computed_size"] = len(payload.get("computed_symbols", []))
    payload["truncated"] = payload["computed_size"] < payload["universe_size"]
    return payload
