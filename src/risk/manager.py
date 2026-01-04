"""Central risk management coordinator."""

import logging
from typing import List, Tuple

from config.settings import Settings
from src.models.order import Order, OrderSide
from src.models.position import PortfolioSnapshot, Position
from src.models.signal import TradingSignal
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.position_limits import PositionLimiter
from src.risk.stop_loss import StopLossManager

logger = logging.getLogger(__name__)


class RiskManager:
    """Central risk management coordinator."""

    def __init__(self, settings: Settings):
        """Initialize risk manager with settings."""
        self.settings = settings

        # Sub-components
        self.position_limiter = PositionLimiter(settings)
        self.stop_loss_manager = StopLossManager(settings)
        self.circuit_breaker = CircuitBreaker(settings)

    def check_order_allowed(
        self,
        order: Order,
        positions: List[Position],
        portfolio: PortfolioSnapshot,
    ) -> Tuple[bool, str]:
        """Check if an order passes all risk checks.

        Args:
            order: The order to check
            positions: Current open positions
            portfolio: Current portfolio state

        Returns:
            (allowed: bool, reason: str)
        """
        # 1. Circuit breaker check
        if self.circuit_breaker.is_triggered():
            return False, f"Circuit breaker active: {self.circuit_breaker.trigger_reason}"

        # 2. Daily loss limit check
        if not self.circuit_breaker.check_daily_loss(portfolio.daily_pnl):
            return False, f"Daily loss limit reached: ${portfolio.daily_pnl:.2f}"

        # 3. Trade count check
        if not self.circuit_breaker.check_trade_count():
            return False, "Daily trade limit reached"

        # 4. Position size limits
        order_value = self._calculate_order_value(order)

        if not self.position_limiter.check_single_trade_limit(order_value):
            return False, f"Order exceeds max trade size: ${order_value:.2f}"

        # 5. Total exposure check
        current_exposure = sum(p.current_value for p in positions)
        new_exposure = current_exposure + order_value

        if not self.position_limiter.check_total_exposure(new_exposure):
            return False, f"Would exceed max exposure: ${new_exposure:.2f}"

        # 6. Per-market concentration check
        market_exposure = sum(
            p.current_value for p in positions if p.market_id == order.market_id
        )
        new_market_exposure = market_exposure + order_value

        if not self.position_limiter.check_market_concentration(
            new_market_exposure, portfolio.total_value
        ):
            pct = (new_market_exposure / portfolio.total_value) * 100 if portfolio.total_value > 0 else 100
            return False, f"Would exceed market concentration limit: {pct:.1f}%"

        # 7. Check sufficient cash
        if order.side == OrderSide.BUY and order_value > portfolio.available_to_trade:
            return False, f"Insufficient funds: need ${order_value:.2f}, have ${portfolio.available_to_trade:.2f}"

        return True, "Order approved"

    def _calculate_order_value(self, order: Order) -> float:
        """Calculate the USD value of an order."""
        if order.amount:  # Market order
            return order.amount
        elif order.price and order.size:  # Limit order
            return order.price * order.size
        return 0.0

    def calculate_position_size(
        self,
        signal: TradingSignal,
        current_price: float,
        portfolio: PortfolioSnapshot,
        positions: List[Position],
    ) -> float:
        """Calculate appropriate position size based on signal and risk limits.

        Args:
            signal: Trading signal with confidence
            current_price: Current market price
            portfolio: Current portfolio state
            positions: Current positions

        Returns:
            Recommended position size in USD
        """
        # Get current exposure
        current_exposure = sum(p.current_value for p in positions)
        market_exposure = sum(
            p.current_value for p in positions if p.market_id == signal.market_id
        )

        # Calculate max trade size given limits
        max_size = self.position_limiter.calculate_max_trade_size(
            current_exposure, market_exposure, portfolio.total_value
        )

        # Scale by confidence
        confidence_factor = signal.confidence

        # Base size adjusted by confidence
        base_amount = max_size * confidence_factor

        # Apply suggested size if provided
        if signal.suggested_size_pct:
            base_amount = min(base_amount, max_size * signal.suggested_size_pct)

        # Ensure minimum viable trade
        base_amount = max(base_amount, self.settings.min_order_size_usd)

        # Cap at max size
        base_amount = min(base_amount, max_size)

        # Cap at available cash for buys
        if signal.suggested_side == "BUY":
            base_amount = min(base_amount, portfolio.available_to_trade)

        # Round to reasonable precision
        return round(base_amount, 2)

    def get_stop_loss_price(
        self,
        entry_price: float,
        side: OrderSide,
    ) -> float:
        """Calculate stop loss price for a position."""
        return self.stop_loss_manager.calculate_stop_loss(
            entry_price, side, self.settings.stop_loss_pct
        )

    def check_stop_losses(self, positions: List[Position]) -> List[Position]:
        """Check which positions have hit their stop loss."""
        return self.stop_loss_manager.check_positions(positions)

    def record_trade(self, pnl: float = 0.0) -> None:
        """Record a completed trade for daily tracking."""
        self.circuit_breaker.record_trade(pnl)

    def reset_daily_stats(self) -> None:
        """Reset daily statistics (call at start of each day)."""
        self.circuit_breaker.reset_daily()

    @property
    def is_trading_allowed(self) -> bool:
        """Check if trading is currently allowed."""
        return not self.circuit_breaker.is_triggered()

    @property
    def status(self) -> dict:
        """Get risk manager status."""
        return {
            "trading_allowed": self.is_trading_allowed,
            "circuit_breaker": self.circuit_breaker.status,
            "max_position_size": self.settings.max_position_size_usd,
            "max_total_exposure": self.settings.max_total_exposure_usd,
            "stop_loss_pct": self.settings.stop_loss_pct,
        }
