"""Pre-trade validation checks for order feasibility.

This validator works alongside RiskManager (position limits, circuit breaker)
to validate market quality and execution feasibility before orders are submitted.

Based on top trader analysis:
1. Resolution clarity - avoid ambiguous markets
2. Liquidity adequate for position size
3. Spread acceptable for strategy
4. Position not correlated with existing
5. Daily loss budget available
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Set, Tuple

from config.settings import Settings
from src.analysis.market_quality import MarketQualityScore, MarketQualityScorer
from src.models.market import Market
from src.models.position import Position
from src.models.signal import TradingSignal

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of pre-trade validation."""

    passed: bool
    reason: str
    checks_passed: List[str]
    checks_failed: List[str]
    quality_score: Optional[MarketQualityScore] = None

    @property
    def summary(self) -> str:
        """Get summary of validation result."""
        status = "PASSED" if self.passed else "FAILED"
        passed_count = len(self.checks_passed)
        failed_count = len(self.checks_failed)
        return f"{status}: {passed_count} passed, {failed_count} failed - {self.reason}"


class PreTradeValidator:
    """Validates trades before execution.

    Complements RiskManager by checking:
    - Market quality (resolution clarity, liquidity, spread)
    - Correlation with existing positions
    - Execution feasibility
    """

    def __init__(self, settings: Optional[Settings] = None):
        """Initialize validator with settings."""
        self.settings = settings or Settings()
        self.quality_scorer = MarketQualityScorer(settings)

        # Track correlated positions by category/theme
        self._category_exposure: dict[str, float] = {}

    def validate(
        self,
        market: Market,
        signal: TradingSignal,
        positions: Optional[List[Position]] = None,
        daily_pnl: float = 0.0,
    ) -> ValidationResult:
        """Run all pre-trade validation checks.

        Args:
            market: Market to trade
            signal: Trading signal with amount
            positions: Current open positions
            daily_pnl: Current day P&L

        Returns:
            ValidationResult with pass/fail and reasons
        """
        positions = positions or []
        checks_passed = []
        checks_failed = []

        # 1. Market quality score
        quality_score = self.quality_scorer.score_market(market)
        passed, reason = self._check_market_quality(quality_score)
        if passed:
            checks_passed.append(f"Quality score: {quality_score.total_score:.0f}/100")
        else:
            checks_failed.append(reason)

        # 2. Resolution clarity
        passed, reason = self._check_resolution_clarity(quality_score)
        if passed:
            checks_passed.append("Resolution clarity adequate")
        else:
            checks_failed.append(reason)

        # 3. Liquidity for position size
        passed, reason = self._check_liquidity_adequate(market, signal)
        if passed:
            checks_passed.append(f"Liquidity adequate: ${market.liquidity:,.0f}")
        else:
            checks_failed.append(reason)

        # 4. Spread acceptable
        passed, reason = self._check_spread_acceptable(market, signal)
        if passed:
            checks_passed.append("Spread acceptable")
        else:
            checks_failed.append(reason)

        # 5. Correlation check
        passed, reason = self._check_not_correlated(market, positions)
        if passed:
            checks_passed.append("Position correlation check passed")
        else:
            checks_failed.append(reason)

        # 6. Position size within limits
        passed, reason = self._check_position_size_limits(signal)
        if passed:
            checks_passed.append(f"Position size within limits: ${signal.suggested_size_pct or 0.0:.2%}")
        else:
            checks_failed.append(reason)

        # 7. Daily loss budget
        passed, reason = self._check_daily_loss_budget(daily_pnl)
        if passed:
            checks_passed.append("Daily loss budget available")
        else:
            checks_failed.append(reason)

        # 8. Market active and tradeable
        passed, reason = self._check_market_tradeable(market)
        if passed:
            checks_passed.append("Market active and tradeable")
        else:
            checks_failed.append(reason)

        # Overall result
        all_passed = len(checks_failed) == 0
        if all_passed:
            final_reason = "All pre-trade checks passed"
        else:
            final_reason = checks_failed[0]  # Primary failure reason

        return ValidationResult(
            passed=all_passed,
            reason=final_reason,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            quality_score=quality_score,
        )

    def _check_market_quality(self, score: MarketQualityScore) -> Tuple[bool, str]:
        """Check overall market quality score."""
        if score.total_score >= self.settings.min_market_quality_score:
            return True, f"Quality score {score.total_score:.0f} >= {self.settings.min_market_quality_score}"
        return False, f"Quality score too low: {score.total_score:.0f} < {self.settings.min_market_quality_score}"

    def _check_resolution_clarity(self, score: MarketQualityScore) -> Tuple[bool, str]:
        """Check resolution clarity score specifically."""
        min_clarity = 15.0  # Minimum clarity score (out of 40)
        if score.resolution_clarity_score >= min_clarity:
            return True, f"Clarity score {score.resolution_clarity_score:.0f}/40"
        return False, f"Resolution clarity too low: {score.resolution_clarity_score:.0f}/40 < {min_clarity}"

    def _check_liquidity_adequate(
        self, market: Market, signal: TradingSignal
    ) -> Tuple[bool, str]:
        """Check if liquidity is adequate for position size."""
        # Need at least 10x the position size in liquidity
        position_size = self._get_signal_size(signal)
        min_liquidity = max(position_size * 10, self.settings.min_liquidity_threshold)

        if market.liquidity >= min_liquidity:
            return True, f"Liquidity ${market.liquidity:,.0f} >= ${min_liquidity:,.0f}"
        return False, f"Insufficient liquidity: ${market.liquidity:,.0f} < ${min_liquidity:,.0f}"

    def _check_spread_acceptable(
        self, market: Market, signal: TradingSignal
    ) -> Tuple[bool, str]:
        """Check if spread is acceptable for the strategy."""
        # Calculate effective spread
        spread = market.spread
        if spread is None:
            spread = abs(1.0 - (market.yes_price + market.no_price))

        # For arbitrage, we need tighter spreads (handled separately)
        # For other strategies, allow wider spreads
        max_spread = self.settings.max_spread_threshold

        # If it's an arbitrage signal, use stricter threshold
        if signal.strategy_name and "arbitrage" in signal.strategy_name.lower():
            max_spread = self.settings.arbitrage_min_spread

        if spread <= max_spread:
            return True, f"Spread {spread:.1%} <= {max_spread:.1%}"
        return False, f"Spread too wide: {spread:.1%} > {max_spread:.1%}"

    def _check_not_correlated(
        self, market: Market, positions: List[Position]
    ) -> Tuple[bool, str]:
        """Check that position isn't too correlated with existing."""
        if not positions:
            return True, "No existing positions"

        # Check same market
        same_market_positions = [p for p in positions if p.market_id == market.id]
        if same_market_positions:
            return False, f"Already have position in this market"

        # Check category concentration
        if market.category:
            category = market.category.lower()
            category_exposure = sum(
                p.current_value for p in positions
                if hasattr(p, 'category') and p.category and p.category.lower() == category
            )
            # Max 50% exposure to single category
            total_exposure = sum(p.current_value for p in positions)
            if total_exposure > 0 and category_exposure / total_exposure > 0.5:
                return False, f"Too much exposure to category: {category}"

        return True, "Position correlation acceptable"

    def _check_position_size_limits(self, signal: TradingSignal) -> Tuple[bool, str]:
        """Check position size is within strategy limits."""
        position_size = self._get_signal_size(signal)

        # Check against arbitrage limits if applicable
        if signal.strategy_name and "arbitrage" in signal.strategy_name.lower():
            if position_size < self.settings.arbitrage_min_position:
                return False, f"Position too small for arbitrage: ${position_size:.2f}"
            if position_size > self.settings.arbitrage_max_position:
                return False, f"Position too large for arbitrage: ${position_size:.2f}"
        else:
            if position_size < self.settings.min_order_size_usd:
                return False, f"Position below minimum: ${position_size:.2f}"
            if position_size > self.settings.max_position_size_usd:
                return False, f"Position above maximum: ${position_size:.2f}"

        return True, f"Position size ${position_size:.2f} within limits"

    def _check_daily_loss_budget(self, daily_pnl: float) -> Tuple[bool, str]:
        """Check if daily loss budget is still available."""
        if daily_pnl >= -self.settings.daily_loss_limit_usd:
            remaining = self.settings.daily_loss_limit_usd + daily_pnl
            return True, f"Daily loss budget remaining: ${remaining:.2f}"
        return False, f"Daily loss limit reached: ${daily_pnl:.2f}"

    def _check_market_tradeable(self, market: Market) -> Tuple[bool, str]:
        """Check if market is active and tradeable."""
        if not market.active:
            return False, "Market is not active"
        if market.closed:
            return False, "Market is closed"
        if not market.clob_token_ids or len(market.clob_token_ids) < 2:
            return False, "Market missing token IDs"
        if not market.is_tradeable:
            return False, "Market is not tradeable"
        return True, "Market is active and tradeable"

    def _get_signal_size(self, signal: TradingSignal) -> float:
        """Extract position size from signal."""
        # Try to get size from signal
        if hasattr(signal, 'amount') and signal.amount:
            return signal.amount
        if hasattr(signal, 'suggested_size_pct') and signal.suggested_size_pct:
            return signal.suggested_size_pct * self.settings.initial_capital
        return self.settings.min_order_size_usd

    def validate_arbitrage(
        self,
        market: Market,
        yes_price: float,
        no_price: float,
        position_size: float,
    ) -> ValidationResult:
        """Validate an arbitrage opportunity specifically.

        Args:
            market: Market to arbitrage
            yes_price: Current YES price
            no_price: Current NO price
            position_size: Size per leg in USD

        Returns:
            ValidationResult
        """
        checks_passed = []
        checks_failed = []

        # 1. Quality score for arbitrage
        quality_score = self.quality_scorer.score_market(market)
        if quality_score.is_arbitrage_candidate:
            checks_passed.append(f"Arbitrage candidate: score {quality_score.total_score:.0f}")
        else:
            checks_failed.append(f"Not arbitrage candidate: score {quality_score.total_score:.0f}")

        # 2. Spread check (YES + NO < $1.00)
        price_sum = yes_price + no_price
        spread = 1.0 - price_sum
        if spread >= self.settings.arbitrage_min_spread:
            checks_passed.append(f"Arbitrage spread: {spread:.1%}")
        else:
            checks_failed.append(f"Spread too small: {spread:.1%} < {self.settings.arbitrage_min_spread:.1%}")

        # 3. Position size limits
        if position_size >= self.settings.arbitrage_min_position:
            checks_passed.append(f"Position size: ${position_size:.2f}")
        else:
            checks_failed.append(f"Position too small: ${position_size:.2f}")

        if position_size <= self.settings.arbitrage_max_position:
            checks_passed.append("Within max position limit")
        else:
            checks_failed.append(f"Position too large: ${position_size:.2f}")

        # 4. Liquidity check per side
        min_liquidity_per_side = self.settings.arbitrage_min_liquidity
        if market.liquidity >= min_liquidity_per_side * 2:
            checks_passed.append(f"Liquidity adequate: ${market.liquidity:,.0f}")
        else:
            checks_failed.append(f"Insufficient liquidity: ${market.liquidity:,.0f}")

        # 5. Crypto 15-min market check
        if quality_score.is_crypto_15min:
            checks_passed.append("15-min crypto market confirmed")
        elif not self.settings.arbitrage_crypto_only:
            checks_passed.append("Non-crypto arbitrage allowed")
        else:
            checks_failed.append("Not a 15-min crypto market")

        # 6. Slippage estimate
        estimated_slippage = self._estimate_slippage(market, position_size)
        max_slippage = self.settings.arbitrage_max_slippage
        if estimated_slippage <= max_slippage:
            checks_passed.append(f"Estimated slippage: {estimated_slippage:.2%}")
        else:
            checks_failed.append(f"Estimated slippage too high: {estimated_slippage:.2%}")

        # Overall result
        all_passed = len(checks_failed) == 0
        if all_passed:
            net_profit = spread - estimated_slippage
            final_reason = f"Arbitrage valid: ~{net_profit:.2%} net profit expected"
        else:
            final_reason = checks_failed[0]

        return ValidationResult(
            passed=all_passed,
            reason=final_reason,
            checks_passed=checks_passed,
            checks_failed=checks_failed,
            quality_score=quality_score,
        )

    def _estimate_slippage(self, market: Market, position_size: float) -> float:
        """Estimate slippage based on position size and liquidity."""
        if market.liquidity <= 0:
            return 0.10  # 10% default if no liquidity data

        # Slippage estimate: position_size / liquidity * impact_factor
        # Impact factor of 2-5x for smaller markets
        impact_factor = 3.0 if market.liquidity < 10000 else 2.0
        estimated_slippage = (position_size / market.liquidity) * impact_factor

        # Cap at reasonable maximum
        return min(estimated_slippage, 0.05)
