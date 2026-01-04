"""Technical/Momentum trading strategy based on price movements."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import Settings
from src.data.price_history import PriceHistoryStore
from src.models.market import Market
from src.models.position import Position
from src.models.signal import SignalType, TradingSignal
from src.strategies.base import BaseStrategy

logger = logging.getLogger(__name__)


class MomentumStrategy(BaseStrategy):
    """Technical/Momentum trading strategy based on price movements."""

    def __init__(
        self,
        settings: Settings,
        price_history: PriceHistoryStore,
        config: Optional[Dict[str, Any]] = None,
    ):
        """Initialize momentum strategy.

        Args:
            settings: Application settings
            price_history: Price history store
            config: Optional configuration overrides
        """
        super().__init__(name="momentum", config=config)
        self.settings = settings
        self.price_history = price_history

        # Strategy parameters
        self.lookback_hours = self.get_config(
            "lookback_hours",
            settings.momentum_lookback_periods,
        )
        self.momentum_threshold = self.get_config(
            "momentum_threshold",
            settings.momentum_threshold,
        )
        self.volume_spike_threshold = self.get_config("volume_spike_threshold", 2.0)
        self.min_data_points = self.get_config("min_data_points", 3)

        # Technical indicators config
        self.rsi_oversold = self.get_config("rsi_oversold", 30)
        self.rsi_overbought = self.get_config("rsi_overbought", 70)
        self.rsi_period = self.get_config("rsi_period", 14)

        # Min requirements
        self.min_liquidity = self.get_config("min_liquidity", 2000.0)
        self.max_markets_per_run = self.get_config("max_markets_per_run", 20)

    def filter_markets(self, markets: List[Market]) -> List[Market]:
        """Filter for markets with sufficient history and liquidity."""
        filtered = []
        for market in markets:
            if not market.active or market.closed:
                continue

            # Need sufficient liquidity
            if market.liquidity < self.min_liquidity:
                continue

            # Check we have enough price history
            if self.price_history.has_sufficient_data(
                market.yes_token_id,
                self.min_data_points,
                self.lookback_hours,
            ):
                filtered.append(market)

        return filtered[:self.max_markets_per_run]

    async def generate_signals(
        self,
        markets: List[Market],
        positions: List[Position],
    ) -> List[TradingSignal]:
        """Generate momentum-based trading signals."""
        signals = []
        self.last_run = datetime.utcnow()

        # Get markets we don't already have positions in
        position_market_ids = {p.market_id for p in positions}
        eligible_markets = [
            m for m in self.filter_markets(markets)
            if m.id not in position_market_ids
        ]

        logger.info(f"Analyzing {len(eligible_markets)} markets for momentum signals")

        for market in eligible_markets:
            signal = self._analyze_momentum(market)
            if signal:
                signals.append(signal)
                logger.info(
                    f"Momentum signal: {signal.signal_type.value} on "
                    f"{market.question[:50]} (confidence: {signal.confidence:.2f})"
                )

        return signals

    def _analyze_momentum(self, market: Market) -> Optional[TradingSignal]:
        """Analyze momentum indicators for a single market."""
        token_id = market.yes_token_id
        prices = self.price_history.get_prices(token_id, hours=self.lookback_hours)
        volumes = self.price_history.get_volumes(token_id, hours=self.lookback_hours)

        if len(prices) < self.min_data_points:
            return None

        # Calculate indicators
        momentum = self._calculate_momentum(prices)
        rsi = self._calculate_rsi(prices)
        volume_ratio = self._calculate_volume_ratio(volumes)

        # Generate signal based on multiple factors
        signal_strength = 0
        reasoning_parts = []

        # Momentum signal
        if momentum > self.momentum_threshold:
            signal_strength += 1
            reasoning_parts.append(f"Positive momentum: {momentum:.2%}")
        elif momentum < -self.momentum_threshold:
            signal_strength -= 1
            reasoning_parts.append(f"Negative momentum: {momentum:.2%}")

        # RSI signal
        if rsi < self.rsi_oversold:
            signal_strength += 1
            reasoning_parts.append(f"RSI oversold: {rsi:.1f}")
        elif rsi > self.rsi_overbought:
            signal_strength -= 1
            reasoning_parts.append(f"RSI overbought: {rsi:.1f}")

        # Volume confirmation
        if volume_ratio > self.volume_spike_threshold:
            reasoning_parts.append(f"Volume spike: {volume_ratio:.1f}x average")
            # Volume confirms the direction
            if signal_strength != 0:
                signal_strength = int(signal_strength * 1.5)

        # Convert to signal
        if signal_strength >= 2:
            return TradingSignal(
                market_id=market.id,
                token_id=token_id,
                signal_type=SignalType.BUY,
                confidence=min(0.9, 0.5 + signal_strength * 0.1),
                suggested_side="BUY",
                strategy_name=self.name,
                reasoning="; ".join(reasoning_parts),
                metadata={
                    "momentum": momentum,
                    "rsi": rsi,
                    "volume_ratio": volume_ratio,
                    "signal_strength": signal_strength,
                },
            )
        elif signal_strength <= -2:
            return TradingSignal(
                market_id=market.id,
                token_id=token_id,
                signal_type=SignalType.SELL,
                confidence=min(0.9, 0.5 + abs(signal_strength) * 0.1),
                suggested_side="SELL",
                strategy_name=self.name,
                reasoning="; ".join(reasoning_parts),
                metadata={
                    "momentum": momentum,
                    "rsi": rsi,
                    "volume_ratio": volume_ratio,
                    "signal_strength": signal_strength,
                },
            )

        return None

    def _calculate_momentum(self, prices: List[float]) -> float:
        """Calculate price momentum (rate of change)."""
        if len(prices) < 2:
            return 0.0
        if prices[0] <= 0:
            return 0.0
        return (prices[-1] - prices[0]) / prices[0]

    def _calculate_rsi(self, prices: List[float]) -> float:
        """Calculate Relative Strength Index."""
        period = min(self.rsi_period, len(prices) - 1)

        if period < 1:
            return 50.0  # Neutral

        deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))

        return rsi

    def _calculate_volume_ratio(self, volumes: List[float]) -> float:
        """Calculate recent volume vs average."""
        if len(volumes) < 2:
            return 1.0

        recent_vol = volumes[-1]
        avg_vol = sum(volumes[:-1]) / len(volumes[:-1])

        if avg_vol == 0:
            return 1.0

        return recent_vol / avg_vol

    def should_exit_position(
        self,
        position: Position,
        market: Market,
    ) -> Optional[TradingSignal]:
        """Check for momentum reversal signals."""
        prices = self.price_history.get_prices(
            position.token_id,
            hours=self.lookback_hours,
        )

        if len(prices) < self.min_data_points:
            return None

        momentum = self._calculate_momentum(prices)
        rsi = self._calculate_rsi(prices)

        # Exit on momentum reversal
        if position.side.value == "long":
            # Exit long if momentum turns negative or RSI overbought
            if momentum < -self.momentum_threshold or rsi > self.rsi_overbought:
                return TradingSignal(
                    market_id=position.market_id,
                    token_id=position.token_id,
                    signal_type=SignalType.SELL,
                    confidence=0.7,
                    suggested_side="SELL",
                    strategy_name=self.name,
                    reasoning=f"Exit signal: momentum={momentum:.2%}, RSI={rsi:.1f}",
                )
        else:
            # Exit short if momentum turns positive or RSI oversold
            if momentum > self.momentum_threshold or rsi < self.rsi_oversold:
                return TradingSignal(
                    market_id=position.market_id,
                    token_id=position.token_id,
                    signal_type=SignalType.BUY,
                    confidence=0.7,
                    suggested_side="BUY",
                    strategy_name=self.name,
                    reasoning=f"Exit signal: momentum={momentum:.2%}, RSI={rsi:.1f}",
                )

        return None
