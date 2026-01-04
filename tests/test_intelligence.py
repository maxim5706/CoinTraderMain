"""Tests for intelligence layer modules."""

import pytest
from datetime import datetime, timezone


class TestRegime:
    """Tests for regime detection."""
    
    def test_regime_detector_import(self):
        from logic.regime import regime_detector, RegimeDetector
        assert isinstance(regime_detector, RegimeDetector)
    
    def test_regime_normal_by_default(self):
        from logic.regime import RegimeDetector
        detector = RegimeDetector()
        assert detector.regime == "normal"
        assert detector.is_safe_to_trade
    
    def test_regime_caution_on_btc_dump(self):
        from logic.regime import RegimeDetector
        detector = RegimeDetector()
        detector.update_btc_trend(-2.0)
        assert detector.regime == "caution"
        assert not detector.is_safe_to_trade
    
    def test_regime_risk_off_on_btc_crash(self):
        from logic.regime import RegimeDetector
        detector = RegimeDetector()
        detector.update_btc_trend(-4.0)
        assert detector.regime == "risk_off"
        assert not detector.is_safe_to_trade
    
    def test_session_detector(self):
        from logic.regime import session_detector
        info = session_detector.get_session_info()
        assert "session" in info
        assert "hour_utc" in info
        assert "size_multiplier" in info
        assert info["size_multiplier"] > 0


class TestLimits:
    """Tests for position limits."""
    
    def test_sector_map_exists(self):
        from logic.limits import SECTOR_MAP
        assert "BTC" in SECTOR_MAP
        assert SECTOR_MAP["BTC"] == "major"
        assert SECTOR_MAP["SOL"] == "L1"
    
    def test_limit_checker_sector(self):
        from logic.limits import limit_checker
        assert limit_checker.get_sector("BTC-USD") == "major"
        assert limit_checker.get_sector("SOL-USD") == "L1"
        assert limit_checker.get_sector("UNKNOWN-USD") == "other"
    
    def test_limit_check_empty_positions(self):
        from logic.limits import LimitChecker
        checker = LimitChecker()
        allowed, reason = checker.check_limits("BTC-USD", 10.0, {})
        assert allowed
        assert reason == "OK"


class TestMLCache:
    """Tests for ML cache."""
    
    def test_cache_import(self):
        from logic.ml_cache import indicator_cache, IndicatorCache
        assert isinstance(indicator_cache, IndicatorCache)
    
    def test_cache_empty_by_default(self):
        from logic.ml_cache import IndicatorCache
        cache = IndicatorCache()
        assert cache.get_ml("BTC-USD") is None
        assert cache.get_indicators("BTC-USD") is None
    
    def test_freshness_stats_empty(self):
        from logic.ml_cache import IndicatorCache
        cache = IndicatorCache()
        stats = cache.get_freshness_stats()
        assert stats["total_count"] == 0
        assert stats["fresh_pct"] == 0


class TestScoring:
    """Tests for entry scoring."""
    
    def test_entry_score_creation(self):
        from logic.scoring import EntryScore
        score = EntryScore(symbol="BTC-USD")
        assert score.symbol == "BTC-USD"
        assert score.total_score == 0.0
        assert not score.should_enter
    
    def test_canonical_gate_order(self):
        from logic.scoring import CANONICAL_GATE_ORDER
        assert len(CANONICAL_GATE_ORDER) == 7
        assert "warmth" in CANONICAL_GATE_ORDER
        assert "ml_boost" in CANONICAL_GATE_ORDER


class TestIntelligence:
    """Tests for main intelligence coordinator."""
    
    def test_intelligence_import(self):
        from logic.intelligence import intelligence, IntelligenceLayer
        assert isinstance(intelligence, IntelligenceLayer)
    
    def test_intelligence_regime_delegation(self):
        from logic.intelligence import IntelligenceLayer
        intel = IntelligenceLayer()
        assert intel._market_regime == "normal"
        intel.update_btc_trend(-4.0)
        assert intel._market_regime == "risk_off"
    
    def test_intelligence_sector_delegation(self):
        from logic.intelligence import intelligence
        assert intelligence.get_sector("BTC-USD") == "major"
        assert intelligence.get_sector("DOGE-USD") == "meme"
    
    def test_intelligence_session_info(self):
        from logic.intelligence import intelligence
        info = intelligence.get_session_info()
        assert "session" in info
        assert info["size_multiplier"] > 0
    
    def test_daily_pnl_tracking(self):
        from logic.intelligence import IntelligenceLayer
        intel = IntelligenceLayer()
        assert intel.get_daily_pnl() == 0.0
        intel.record_trade_result(10.0, "test", is_win=True)
        assert intel.get_daily_pnl() == 10.0
        intel.record_trade_result(-5.0, "test", is_win=False)
        assert intel.get_daily_pnl() == 5.0
    
    def test_strategy_stats(self):
        from logic.intelligence import IntelligenceLayer
        intel = IntelligenceLayer()
        intel.record_trade_result(10.0, "burst_flag", is_win=True)
        intel.record_trade_result(-5.0, "burst_flag", is_win=False)
        stats = intel.get_strategy_stats()
        assert "burst_flag" in stats
        assert stats["burst_flag"]["wins"] == 1
        assert stats["burst_flag"]["losses"] == 1
        assert stats["burst_flag"]["total_pnl"] == 5.0
