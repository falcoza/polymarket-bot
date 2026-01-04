"""Stop loss calculation and management."""

import logging
from typing import List

from config.settings import Settings
from src.models.order import OrderSide
from src.models.position import Position, PositionSide

logger = logging.getLogger(__name__)


class StopLossManager:
    """Manage stop loss calculations and triggers."""

    def __init__(self, settings: Settings):
        """Initialize with default stop loss percentage."""
        self.default_stop_pct = settings.stop_loss_pct

    def calculate_stop_loss(
        self,
        entry_price: float,
        side: OrderSide,
        stop_pct: float = None,
    ) -> float:
        """Calculate stop loss price for a position.

        Args:
            entry_price: The entry price of the position
            side: The order side (BUY for long, SELL for short)
            stop_pct: Optional custom stop loss percentage

        Returns:
            The stop loss trigger price
        """
        stop_pct = stop_pct or self.default_stop_pct

        if side == OrderSide.BUY:
            # Long position - stop below entry
            return entry_price * (1 - stop_pct)
        else:
            # Short position - stop above entry
            return entry_price * (1 + stop_pct)

    def check_positions(self, positions: List[Position]) -> List[Position]:
        """Return positions that have hit their stop loss.

        Args:
            positions: List of positions to check

        Returns:
            List of positions that should be closed due to stop loss
        """
        triggered = []

        for position in positions:
            if position.stop_loss_price is None:
                continue

            if position.should_stop_loss:
                logger.warning(
                    f"Stop loss triggered for {position.market_id}: "
                    f"price {position.current_price:.4f} hit stop {position.stop_loss_price:.4f}"
                )
                triggered.append(position)

        return triggered

    def update_trailing_stop(
        self,
        position: Position,
        trail_pct: float = None,
    ) -> float:
        """Update trailing stop loss based on current price.

        Args:
            position: The position to update
            trail_pct: Trailing stop percentage (defaults to stop_loss_pct)

        Returns:
            New stop loss price (or existing if no update needed)
        """
        trail_pct = trail_pct or self.default_stop_pct

        if position.side == PositionSide.LONG:
            # For long positions, trail stop up as price increases
            new_stop = position.current_price * (1 - trail_pct)
            if position.stop_loss_price is None or new_stop > position.stop_loss_price:
                logger.info(
                    f"Trailing stop updated for {position.market_id}: "
                    f"{position.stop_loss_price:.4f} -> {new_stop:.4f}"
                )
                return new_stop
        else:
            # For short positions, trail stop down as price decreases
            new_stop = position.current_price * (1 + trail_pct)
            if position.stop_loss_price is None or new_stop < position.stop_loss_price:
                logger.info(
                    f"Trailing stop updated for {position.market_id}: "
                    f"{position.stop_loss_price:.4f} -> {new_stop:.4f}"
                )
                return new_stop

        return position.stop_loss_price or self.calculate_stop_loss(
            position.avg_entry_price,
            OrderSide.BUY if position.side == PositionSide.LONG else OrderSide.SELL,
        )
