"""Arbitrage strategy for 15-minute crypto markets.

Detects and executes arbitrage opportunities where YES + NO prices < $1.00.
This is a risk-minimized strategy that profits from market inefficiencies
without requiring prediction.

Based on analysis of top traders (@Account88888, @distinct-baguette):
- Success hinges on capturing market pricing errors, not prediction
- High frequency, small position sizes
- Target 15-min crypto markets for fast capital turnover
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from config.settings import Settings
from src.analysis.market_quality import MarketQualityScorer
from src.analysis.pre_trade_validator import PreTradeValidator
from src.models.market import Market
from src.models.position import Position
from src.models.signal import SignalType, TradingSignal
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass
class ArbitrageOpportunity:
    """Represents a detected arbitrage opportunity."""

    market_id: str
    market_question: str
    yes_token_id: str
    no_token_id: str
    yes_price: float
    no_price: float
    spread: float  # 1.0 - (yes_price + no_price)
    estimated_profit_pct: float
    estimated_profit_usd: float
    liquidity: float
    quality_score: float
    is_crypto_15min: bool
    detected_at: datetime

    @property
    def is_profitable(self) -> bool:
        """Check if opportunity meets minimum profit threshold."""
        return self.spread >= 0.03  # 3% minimum

    @property
    def summary(self) -> str:
        """Get summary of the opportunity."""
        return (
            f"{self.market_question[:50]}... | "
            f"Spread: {self.spread:.2%} | "
            f"Est. profit: ${self.estimated_profit_usd:.2f} | "
            f"Liquidity: ${self.liquidity:,.0f}"
        )


class ArbitrageStrategy(BaseStrategy):
    """Arbitrage strategy for binary prediction markets.

    Profits from market inefficiencies where YES + NO < $1.00.
    On market resolution, one outcome pays $1.00 and the other $0.00.
    If we hold both, we're guaranteed $1.00 regardless of outcome.

    Example:
    - Buy YES @ $0.48
    - Buy NO @ $0.49
    - Total cost: $0.97
    - Guaranteed payout: $1.00
    - Profit: $0.03 (3.1%)
    """

    def __init__(
        self,
        settings: Settings,
        config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize arbitrage strategy.

        Args:
            settings: Application settings
            config: Optional configuration overrides
        """
        super().__init__(name="arbitrage", config=config)
        self.settings = settings

        # Initialize helpers
        self.quality_scorer = MarketQualityScorer(settings)
        self.validator = PreTradeValidator(settings)

        # Strategy parameters from settings
        self.min_spread = self.get_config("min_spread", settings.arbitrage_min_spread)
        self.max_slippage = self.get_config("max_slippage", settings.arbitrage_max_slippage)
        self.min_liquidity = self.get_config("min_liquidity", settings.arbitrage_min_liquidity)
        self.min_position = self.get_config("min_position", settings.arbitrage_min_position)
        self.max_position = self.get_config("max_position", settings.arbitrage_max_position)
        self.max_concurrent = self.get_config("max_concurrent", settings.arbitrage_max_concurrent)
        self.crypto_only = self.get_config("crypto_only", settings.arbitrage_crypto_only)

        # Tracking
        self.opportunities_found = 0
        self.opportunities_executed = 0
        self._active_arbs: Dict[str, ArbitrageOpportunity] = {}

    def filter_markets(self, markets: List[Market]) -> List[Market]:
        """Filter markets suitable for arbitrage.

        Criteria:
        - Active and not closed
        - Has both YES and NO token IDs
        - Meets minimum liquidity
        - Is crypto 15-min market (if crypto_only enabled)
        """
        filtered = []
        for market in markets:
            if not market.active or market.closed:
                continue
            if len(market.clob_token_ids) < 2:
                continue
            if market.liquidity < self.min_liquidity:
                continue

            # Score the market
            score = self.quality_scorer.score_market(market)

            # Skip if crypto_only and not crypto 15-min
            if self.crypto_only and not score.is_crypto_15min:
                continue

            # Must meet minimum quality
            if not score.is_tradeable:
                continue

            filtered.append(market)

        return filtered

    async def generate_signals(
        self,
        markets: List[Market],
        positions: List[Position],
    ) -> List[TradingSignal]:
        """Scan markets for arbitrage opportunities.

        Returns paired signals (YES + NO) for each opportunity.
        """
        self.last_run = datetime.utcnow()
        signals = []

        # Check if we're at max concurrent arbs
        active_arb_count = len(self._active_arbs)
        if active_arb_count >= self.max_concurrent:
            logger.info(
                f"At max concurrent arbitrage positions ({active_arb_count}/{self.max_concurrent})"
            )
            return signals

        # Get position market IDs to avoid duplicates
        position_market_ids = {p.market_id for p in positions}

        # Filter eligible markets
        eligible_markets = self.filter_markets(markets)
        logger.info(f"Scanning {len(eligible_markets)} markets for arbitrage opportunities")

        # Find opportunities
        opportunities = self._find_opportunities(eligible_markets, position_market_ids)

        # Sort by spread (best opportunities first)
        opportunities.sort(key=lambda o: o.spread, reverse=True)

        # Generate signals for top opportunities
        slots_available = self.max_concurrent - active_arb_count
        for opp in opportunities[:slots_available]:
            if opp.is_profitable:
                signal_pair = self._create_arbitrage_signals(opp)
                if signal_pair:
                    signals.extend(signal_pair)
                    self.opportunities_found += 1
                    logger.info(f"Arbitrage opportunity: {opp.summary}")

        return signals

    def _find_opportunities(
        self,
        markets: List[Market],
        exclude_market_ids: set,
    ) -> List[ArbitrageOpportunity]:
        """Scan markets for arbitrage opportunities.

        An opportunity exists when YES_price + NO_price < 1.0
        """
        opportunities = []

        for market in markets:
            if market.id in exclude_market_ids:
                continue

            yes_price = market.yes_price
            no_price = market.no_price
            price_sum = yes_price + no_price

            # Calculate spread (discount from $1.00)
            spread = 1.0 - price_sum

            # Skip if spread too small
            if spread < self.min_spread:
                continue

            # Calculate profit
            position_size = min(self.max_position, market.liquidity * 0.01)
            position_size = max(position_size, self.min_position)

            # Profit = spread * position_size - estimated_slippage
            slippage_estimate = self._estimate_slippage(market, position_size)
            net_profit_pct = spread - slippage_estimate
            net_profit_usd = net_profit_pct * position_size

            # Skip if not profitable after slippage
            if net_profit_usd < 0.01:  # Min 1 cent profit
                continue

            # Get quality score
            score = self.quality_scorer.score_market(market)

            opp = ArbitrageOpportunity(
                market_id=market.id,
                market_question=market.question,
                yes_token_id=market.yes_token_id,
                no_token_id=market.no_token_id,
                yes_price=yes_price,
                no_price=no_price,
                spread=spread,
                estimated_profit_pct=net_profit_pct,
                estimated_profit_usd=net_profit_usd,
                liquidity=market.liquidity,
                quality_score=score.total_score,
                is_crypto_15min=score.is_crypto_15min,
                detected_at=datetime.utcnow(),
            )
            opportunities.append(opp)

        return opportunities

    def _estimate_slippage(self, market: Market, position_size: float) -> float:
        """Estimate slippage for position size."""
        if market.liquidity <= 0:
            return 0.05  # 5% default

        # Base slippage on position/liquidity ratio
        impact_factor = 3.0 if market.liquidity < 10000 else 2.0
        estimated = (position_size / market.liquidity) * impact_factor

        return min(estimated, self.max_slippage)

    def _create_arbitrage_signals(
        self,
        opportunity: ArbitrageOpportunity,
    ) -> Optional[Tuple[TradingSignal, TradingSignal]]:
        """Create paired BUY signals for YES and NO tokens."""
        # Calculate position size per leg
        position_per_leg = min(
            self.max_position,
            opportunity.liquidity * 0.01,  # Max 1% of liquidity
        )
        position_per_leg = max(position_per_leg, self.min_position)

        # Ensure we have enough for both legs
        total_cost = (opportunity.yes_price + opportunity.no_price) * position_per_leg
        if total_cost > self.settings.initial_capital * 0.66:  # Max 66% of capital
            position_per_leg = (self.settings.initial_capital * 0.66) / (
                opportunity.yes_price + opportunity.no_price
            )

        if position_per_leg < self.min_position:
            logger.warning(
                f"Position size too small for arbitrage: ${position_per_leg:.2f}"
            )
            return None

        # Size in shares (position_per_leg is USD, price is per share)
        yes_shares = position_per_leg / opportunity.yes_price
        no_shares = position_per_leg / opportunity.no_price

        # Create YES signal
        yes_signal = TradingSignal(
            market_id=opportunity.market_id,
            token_id=opportunity.yes_token_id,
            signal_type=SignalType.STRONG_BUY,
            confidence=0.95,  # High confidence for arb
            suggested_side="BUY",
            suggested_price=opportunity.yes_price,
            suggested_size_pct=position_per_leg / self.settings.initial_capital,
            strategy_name=self.name,
            reasoning=f"Arbitrage YES leg: spread={opportunity.spread:.2%}",
            metadata={
                "arb_type": "yes_leg",
                "pair_market_id": opportunity.market_id,
                "spread": opportunity.spread,
                "shares": yes_shares,
                "cost_usd": position_per_leg,
                "paired_with": opportunity.no_token_id,
            },
        )

        # Create NO signal
        no_signal = TradingSignal(
            market_id=opportunity.market_id,
            token_id=opportunity.no_token_id,
            signal_type=SignalType.STRONG_BUY,
            confidence=0.95,
            suggested_side="BUY",
            suggested_price=opportunity.no_price,
            suggested_size_pct=position_per_leg / self.settings.initial_capital,
            strategy_name=self.name,
            reasoning=f"Arbitrage NO leg: spread={opportunity.spread:.2%}",
            metadata={
                "arb_type": "no_leg",
                "pair_market_id": opportunity.market_id,
                "spread": opportunity.spread,
                "shares": no_shares,
                "cost_usd": position_per_leg,
                "paired_with": opportunity.yes_token_id,
            },
        )

        # Track active arb
        self._active_arbs[opportunity.market_id] = opportunity

        return (yes_signal, no_signal)

    def should_exit_position(
        self,
        position: Position,
        market: Market,
    ) -> Optional[TradingSignal]:
        """Check if arbitrage position should be exited.

        For arbitrage, we typically hold until resolution.
        Only exit early if:
        - Market is about to close without resolution
        - Spread has inverted (can exit at profit)
        - Emergency exit needed
        """
        # Check if market is closed/resolved
        if market.closed:
            # Market resolved, position will settle automatically
            if position.market_id in self._active_arbs:
                del self._active_arbs[position.market_id]
            return None

        # Check for early profit opportunity (spread inversion)
        current_spread = 1.0 - (market.yes_price + market.no_price)

        # If spread has inverted significantly, consider exit
        if current_spread < -0.02:  # Prices sum to > $1.02
            # Can sell both sides at profit
            return TradingSignal(
                market_id=position.market_id,
                token_id=position.token_id,
                signal_type=SignalType.SELL,
                confidence=0.8,
                suggested_side="SELL",
                strategy_name=self.name,
                reasoning=f"Early exit: spread inverted to {current_spread:.2%}",
                metadata={
                    "exit_type": "spread_inversion",
                    "current_spread": current_spread,
                },
            )

        # Hold until resolution
        return None

    def get_active_opportunities(self) -> List[ArbitrageOpportunity]:
        """Get list of currently active arbitrage positions."""
        return list(self._active_arbs.values())

    def scan_opportunities(self, markets: List[Market]) -> List[ArbitrageOpportunity]:
        """Public method to scan for opportunities without generating signals.

        Useful for CLI display and monitoring.
        """
        eligible = self.filter_markets(markets)
        return self._find_opportunities(eligible, set())

    def calculate_potential_profit(
        self,
        yes_price: float,
        no_price: float,
        position_size: float,
        liquidity: float,
    ) -> Dict[str, float]:
        """Calculate potential profit for given prices.

        Args:
            yes_price: YES token price
            no_price: NO token price
            position_size: USD per leg
            liquidity: Market liquidity

        Returns:
            Dict with profit calculations
        """
        spread = 1.0 - (yes_price + no_price)
        slippage = (position_size / liquidity) * 2.5 if liquidity > 0 else 0.05

        gross_profit_pct = spread
        net_profit_pct = spread - slippage
        gross_profit_usd = spread * position_size
        net_profit_usd = net_profit_pct * position_size
        total_cost = (yes_price + no_price) * position_size

        return {
            "spread": spread,
            "slippage_estimate": slippage,
            "gross_profit_pct": gross_profit_pct,
            "net_profit_pct": net_profit_pct,
            "gross_profit_usd": gross_profit_usd,
            "net_profit_usd": net_profit_usd,
            "total_cost_usd": total_cost,
            "guaranteed_payout": position_size,  # Winner pays $1 per share
            "roi": (net_profit_usd / total_cost) * 100 if total_cost > 0 else 0,
        }

    @property
    def status(self) -> Dict[str, Any]:
        """Get strategy status."""
        base_status = super().status
        base_status.update({
            "opportunities_found": self.opportunities_found,
            "opportunities_executed": self.opportunities_executed,
            "active_arbs": len(self._active_arbs),
            "max_concurrent": self.max_concurrent,
            "min_spread": self.min_spread,
            "crypto_only": self.crypto_only,
        })
        return base_status
