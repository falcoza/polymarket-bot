"""Order execution engine for paper and live trading."""

import logging
from datetime import datetime
from typing import List, Optional

from config.settings import Settings, TradingMode
from src.api.clob_client import PolymarketClobClient
from src.models.order import Order, OrderSide, OrderStatus, OrderType
from src.models.position import PortfolioSnapshot, Position, PositionSide
from src.models.signal import TradingSignal
from src.risk.manager import RiskManager

logger = logging.getLogger(__name__)


class OrderExecutor:
    """Handles order creation, submission, and tracking."""

    def __init__(
        self,
        settings: Settings,
        clob_client: PolymarketClobClient,
        risk_manager: RiskManager,
    ):
        """Initialize the order executor."""
        self.settings = settings
        self.clob_client = clob_client
        self.risk_manager = risk_manager
        self.trading_mode = settings.trading_mode

    async def execute_signal(
        self,
        signal: TradingSignal,
        positions: List[Position],
        portfolio: PortfolioSnapshot,
    ) -> Optional[Order]:
        """Convert a trading signal into an executed order.

        Args:
            signal: Trading signal to execute
            positions: Current open positions
            portfolio: Current portfolio state

        Returns:
            Executed order or None if not executed
        """
        if signal.suggested_side is None:
            logger.debug(f"Signal has no suggested side, skipping")
            return None

        # Get current market price
        if self.trading_mode == TradingMode.LIVE and self.clob_client.is_connected:
            current_price = self.clob_client.get_midpoint(signal.token_id)
        else:
            # Use suggested price or default for paper/backtest
            current_price = signal.suggested_price or 0.5

        if current_price <= 0:
            logger.warning(f"Invalid price {current_price} for {signal.token_id}")
            return None

        # Calculate position size
        amount = self.risk_manager.calculate_position_size(
            signal, current_price, portfolio, positions
        )

        if amount < self.settings.min_order_size_usd:
            logger.debug(f"Calculated amount ${amount:.2f} below minimum, skipping")
            return None

        # Create order
        order = Order(
            market_id=signal.market_id,
            token_id=signal.token_id,
            side=OrderSide(signal.suggested_side),
            order_type=OrderType(self.settings.default_order_type),
            amount=amount,
            price=current_price,
            strategy_name=signal.strategy_name,
        )

        # Risk check
        allowed, reason = self.risk_manager.check_order_allowed(
            order, positions, portfolio
        )

        if not allowed:
            logger.warning(f"Order rejected by risk manager: {reason}")
            order.status = OrderStatus.REJECTED
            return order

        # Execute based on mode
        if self.trading_mode == TradingMode.PAPER:
            return await self._paper_execute(order)
        elif self.trading_mode == TradingMode.LIVE:
            return await self._live_execute(order)
        else:
            # Backtest mode - simulate fill
            logger.info(f"[BACKTEST] Order simulated: {order.side} ${order.amount}")
            order.status = OrderStatus.FILLED
            order.filled_price = current_price
            order.filled_size = amount / current_price
            order.filled_at = datetime.utcnow()
            return order

    async def _live_execute(self, order: Order) -> Order:
        """Execute order on live exchange."""
        try:
            order.status = OrderStatus.SUBMITTED
            order.submitted_at = datetime.utcnow()

            if order.order_type == OrderType.FOK:
                # Market order
                signed_order = self.clob_client.create_market_order(
                    token_id=order.token_id,
                    side=order.side,
                    amount_usd=order.amount,
                )
                response = self.clob_client.submit_order(signed_order, order.order_type)
            else:
                # Limit order
                size = order.amount / order.price if order.price else 0
                signed_order = self.clob_client.create_limit_order(
                    token_id=order.token_id,
                    side=order.side,
                    price=order.price,
                    size=size,
                )
                response = self.clob_client.submit_order(signed_order, order.order_type)

            # Parse response
            order.exchange_order_id = response.get("orderID") or response.get("id")

            status = response.get("status", "").lower()
            if status == "filled" or response.get("success"):
                order.status = OrderStatus.FILLED
                order.filled_at = datetime.utcnow()
                order.filled_price = float(response.get("avgPrice") or order.price or 0)
                order.filled_size = float(response.get("filledSize") or 0)
                self.risk_manager.record_trade()
            elif status in ["live", "open", "pending"]:
                order.status = OrderStatus.SUBMITTED
            else:
                order.status = OrderStatus.FAILED

            logger.info(f"Order executed: {order.id} -> {order.status}")

        except Exception as e:
            logger.error(f"Order execution failed: {e}")
            order.status = OrderStatus.FAILED

        return order

    async def _paper_execute(self, order: Order) -> Order:
        """Simulate order execution for paper trading."""
        order.status = OrderStatus.FILLED
        order.submitted_at = datetime.utcnow()
        order.filled_at = datetime.utcnow()
        order.filled_price = order.price
        order.filled_size = order.amount / order.price if order.price and order.price > 0 else 0

        self.risk_manager.record_trade()

        logger.info(
            f"[PAPER] Order filled: {order.side} ${order.amount:.2f} @ {order.price:.4f}"
        )

        return order

    async def close_position(
        self,
        position: Position,
        reason: str,
        current_price: Optional[float] = None,
    ) -> Optional[Order]:
        """Close an existing position.

        Args:
            position: Position to close
            reason: Reason for closing (e.g., "stop_loss", "take_profit")
            current_price: Current market price (fetched if not provided)

        Returns:
            Exit order if executed
        """
        # Determine exit side (opposite of position)
        exit_side = OrderSide.SELL if position.side == PositionSide.LONG else OrderSide.BUY

        # Get current price if not provided
        if current_price is None:
            if self.trading_mode == TradingMode.LIVE and self.clob_client.is_connected:
                current_price = self.clob_client.get_midpoint(position.token_id)
            else:
                current_price = position.current_price

        # Create exit order
        order = Order(
            market_id=position.market_id,
            token_id=position.token_id,
            side=exit_side,
            order_type=OrderType.FOK,  # Market order for exits
            amount=position.current_value,
            size=position.size,
            price=current_price,
            strategy_name=position.strategy_name,
        )

        logger.info(f"Closing position {position.id}: {reason}")

        if self.trading_mode == TradingMode.LIVE:
            return await self._live_execute(order)
        else:
            return await self._paper_execute(order)

    def create_position_from_order(
        self,
        order: Order,
        market_question: str,
    ) -> Position:
        """Create a position record from a filled order.

        Args:
            order: The filled order
            market_question: Market question for display

        Returns:
            New Position object
        """
        if order.status != OrderStatus.FILLED:
            raise ValueError("Cannot create position from unfilled order")

        side = PositionSide.LONG if order.side == OrderSide.BUY else PositionSide.SHORT
        entry_price = order.filled_price or order.price or 0
        size = order.filled_size or (order.amount / entry_price if entry_price > 0 else 0)
        total_cost = size * entry_price

        position = Position(
            market_id=order.market_id,
            token_id=order.token_id,
            market_question=market_question,
            side=side,
            size=size,
            avg_entry_price=entry_price,
            current_price=entry_price,
            total_cost=total_cost,
            current_value=total_cost,
            strategy_name=order.strategy_name,
            opened_at=order.filled_at or datetime.utcnow(),
        )

        # Set stop loss
        position.stop_loss_price = self.risk_manager.get_stop_loss_price(
            entry_price, order.side
        )

        return position
