"""Abstract base class for trading strategies."""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.models.market import Market
from src.models.position import Position
from src.models.signal import TradingSignal

logger = logging.getLogger(__name__)


class BaseStrategy(ABC):
    """Abstract base class for trading strategies."""

    def __init__(self, name: str, config: Optional[Dict[str, Any]] = None):
        """Initialize the strategy.

        Args:
            name: Strategy identifier
            config: Optional configuration overrides
        """
        self.name = name
        self.config = config or {}
        self.is_active = True
        self.last_run: Optional[datetime] = None

        # Performance tracking
        self.total_signals = 0
        self.profitable_signals = 0

    @abstractmethod
    async def generate_signals(
        self,
        markets: List[Market],
        positions: List[Position],
    ) -> List[TradingSignal]:
        """Generate trading signals for given markets.

        Args:
            markets: List of active markets to analyze
            positions: Current open positions

        Returns:
            List of trading signals (can be empty)
        """
        pass

    @abstractmethod
    def should_exit_position(
        self,
        position: Position,
        market: Market,
    ) -> Optional[TradingSignal]:
        """Check if an existing position should be closed.

        Args:
            position: Current position
            market: Current market data

        Returns:
            Exit signal if position should be closed, None otherwise
        """
        pass

    def filter_markets(self, markets: List[Market]) -> List[Market]:
        """Filter markets suitable for this strategy.

        Override in subclasses for strategy-specific filtering.

        Args:
            markets: All available markets

        Returns:
            Filtered list of markets
        """
        return [m for m in markets if m.active and not m.closed and m.is_tradeable]

    def update_performance(self, signal: TradingSignal, profitable: bool) -> None:
        """Update strategy performance metrics.

        Args:
            signal: The signal that was acted upon
            profitable: Whether the trade was profitable
        """
        self.total_signals += 1
        if profitable:
            self.profitable_signals += 1

    @property
    def win_rate(self) -> float:
        """Calculate win rate."""
        if self.total_signals == 0:
            return 0.0
        return self.profitable_signals / self.total_signals

    def get_config(self, key: str, default: Any = None) -> Any:
        """Get configuration value.

        Args:
            key: Configuration key
            default: Default value if not found

        Returns:
            Configuration value
        """
        return self.config.get(key, default)

    def activate(self) -> None:
        """Activate the strategy."""
        self.is_active = True
        logger.info(f"Strategy {self.name} activated")

    def deactivate(self) -> None:
        """Deactivate the strategy."""
        self.is_active = False
        logger.info(f"Strategy {self.name} deactivated")

    @property
    def status(self) -> Dict[str, Any]:
        """Get strategy status."""
        return {
            "name": self.name,
            "is_active": self.is_active,
            "last_run": self.last_run,
            "total_signals": self.total_signals,
            "profitable_signals": self.profitable_signals,
            "win_rate": self.win_rate,
        }

    def __repr__(self) -> str:
        """String representation."""
        return f"<{self.__class__.__name__}(name={self.name}, active={self.is_active})>"
