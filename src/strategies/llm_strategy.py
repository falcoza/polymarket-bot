"""Trading strategy powered by Claude LLM analysis."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import Settings
from src.api.anthropic_client import AnthropicAnalysisClient
from src.models.market import Market
from src.models.position import Position
from src.models.signal import SignalType, TradingSignal
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class LLMStrategy(BaseStrategy):
    """Trading strategy powered by Claude LLM analysis."""

    def __init__(
        self,
        settings: Settings,
        config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize LLM strategy.

        Args:
            settings: Application settings
            config: Optional configuration overrides
        """
        super().__init__(name="llm_claude", config=config)
        self.settings = settings
        self.anthropic_client = AnthropicAnalysisClient(settings)

        # Strategy parameters
        self.confidence_threshold = self.get_config(
            "confidence_threshold",
            settings.llm_confidence_threshold,
        )
        self.min_liquidity = self.get_config("min_liquidity", 5000.0)
        self.min_volume_24hr = self.get_config("min_volume_24hr", 500.0)
        self.max_markets_per_run = self.get_config("max_markets_per_run", 10)

        # Position exit thresholds
        self.take_profit_threshold = self.get_config("take_profit_threshold", 0.30)
        self.reanalyze_threshold = self.get_config("reanalyze_threshold", 0.20)

    def filter_markets(self, markets: List[Market]) -> List[Market]:
        """Filter for markets suitable for LLM analysis."""
        filtered = []
        for market in markets:
            # Basic activity check
            if not market.active or market.closed:
                continue

            # Liquidity and volume requirements
            if market.liquidity < self.min_liquidity:
                continue
            if (market.volume_24hr or 0) < self.min_volume_24hr:
                continue

            # Skip markets with extreme prices (already decided)
            if market.yes_price > 0.95 or market.yes_price < 0.05:
                continue

            filtered.append(market)

        # Sort by volume and take top N
        filtered.sort(key=lambda m: m.volume_24hr or 0, reverse=True)
        return filtered[: self.max_markets_per_run]

    async def generate_signals(
        self,
        markets: List[Market],
        positions: List[Position],
    ) -> List[TradingSignal]:
        """Generate signals using Claude analysis."""
        signals = []
        self.last_run = datetime.utcnow()

        if not self.anthropic_client.is_available():
            logger.warning("Anthropic client not configured, skipping LLM analysis")
            return signals

        # Get markets we don't already have positions in
        position_market_ids = {p.market_id for p in positions}
        eligible_markets = [
            m
            for m in self.filter_markets(markets)
            if m.id not in position_market_ids
        ]

        if not eligible_markets:
            logger.info("No eligible markets for LLM analysis")
            return signals

        logger.info(f"Analyzing {len(eligible_markets)} markets with Claude")

        # Analyze markets
        try:
            all_signals = await self.anthropic_client.analyze_multiple_markets(
                eligible_markets
            )

            # Filter by confidence threshold
            for signal in all_signals:
                if signal.confidence >= self.confidence_threshold:
                    if signal.signal_type in [SignalType.STRONG_BUY, SignalType.BUY]:
                        signals.append(signal)
                        logger.info(
                            f"LLM signal: {signal.signal_type.value} on market "
                            f"{signal.market_id} (confidence: {signal.confidence:.2f})"
                        )
                    elif signal.signal_type in [SignalType.STRONG_SELL, SignalType.SELL]:
                        # Only add sell signals if we can short (future feature)
                        logger.debug(
                            f"LLM sell signal on {signal.market_id} - shorting not implemented"
                        )

        except Exception as e:
            logger.error(f"Error during LLM analysis: {e}")

        return signals

    def should_exit_position(
        self,
        position: Position,
        market: Market,
    ) -> Optional[TradingSignal]:
        """Check if position should be exited based on P&L or re-analysis.

        For LLM strategy, we primarily exit on profit targets.
        Re-analysis is expensive so we do it sparingly.
        """
        # Exit if price has moved significantly toward target (take profit)
        if position.unrealized_pnl_pct >= self.take_profit_threshold:
            return TradingSignal(
                market_id=position.market_id,
                token_id=position.token_id,
                signal_type=SignalType.SELL,
                confidence=0.8,
                suggested_side="SELL",
                strategy_name=self.name,
                reasoning=f"Taking profit at {position.unrealized_pnl_pct:.1%} gain",
                metadata={
                    "exit_reason": "take_profit",
                    "pnl_pct": position.unrealized_pnl_pct,
                },
            )

        # Market has moved against us significantly - consider cutting losses
        # This is in addition to stop-loss (which is price-based, not P&L based)
        if position.unrealized_pnl_pct <= -0.25:  # 25% loss
            return TradingSignal(
                market_id=position.market_id,
                token_id=position.token_id,
                signal_type=SignalType.SELL,
                confidence=0.7,
                suggested_side="SELL",
                strategy_name=self.name,
                reasoning=f"Cutting loss at {position.unrealized_pnl_pct:.1%}",
                metadata={
                    "exit_reason": "cut_loss",
                    "pnl_pct": position.unrealized_pnl_pct,
                },
            )

        return None

    async def reanalyze_position(
        self,
        position: Position,
        market: Market,
    ) -> Optional[TradingSignal]:
        """Re-analyze a position using Claude.

        This is an expensive operation, use sparingly.

        Args:
            position: Current position
            market: Current market data

        Returns:
            Exit signal if analysis suggests exiting
        """
        if not self.anthropic_client.is_available():
            return None

        context = (
            f"Current position: {position.side.value} at {position.avg_entry_price:.2%}, "
            f"current P&L: {position.unrealized_pnl_pct:.1%}"
        )

        try:
            signal = await self.anthropic_client.analyze_market(market, context)

            # If new analysis suggests opposite direction, exit
            if position.side.value == "long" and signal.is_bearish:
                return TradingSignal(
                    market_id=position.market_id,
                    token_id=position.token_id,
                    signal_type=SignalType.SELL,
                    confidence=signal.confidence,
                    suggested_side="SELL",
                    strategy_name=self.name,
                    reasoning=f"Re-analysis suggests exit: {signal.reasoning}",
                    metadata={
                        "exit_reason": "reanalysis",
                        "new_signal": signal.signal_type.value,
                    },
                )

        except Exception as e:
            logger.error(f"Error re-analyzing position: {e}")

        return None
