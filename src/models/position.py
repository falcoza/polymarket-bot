"""Position and portfolio data models."""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class PositionSide(str, Enum):
    """Position side."""

    LONG = "long"  # Bought shares
    SHORT = "short"  # Sold shares


class Position(BaseModel):
    """Active position tracker."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    market_id: str = Field(description="Market identifier")
    token_id: str = Field(description="Token identifier")
    market_question: str = Field(description="Market question for display")

    side: PositionSide = Field(description="Position side: long or short")
    size: float = Field(description="Number of shares held")
    avg_entry_price: float = Field(description="Average entry price")
    current_price: float = Field(description="Current market price")

    # Cost basis
    total_cost: float = Field(description="Total cost basis in USD")
    current_value: float = Field(description="Current market value in USD")

    # P&L
    unrealized_pnl: float = Field(default=0.0, description="Unrealized P&L in USD")
    unrealized_pnl_pct: float = Field(default=0.0, description="Unrealized P&L percentage")

    # Risk management
    stop_loss_price: Optional[float] = Field(
        default=None,
        description="Stop loss trigger price",
    )
    take_profit_price: Optional[float] = Field(
        default=None,
        description="Take profit trigger price",
    )

    # Tracking
    strategy_name: str = Field(description="Strategy that opened this position")
    opened_at: datetime = Field(default_factory=datetime.utcnow)
    last_updated: datetime = Field(default_factory=datetime.utcnow)

    def update_price(self, new_price: float) -> None:
        """Update position with new market price."""
        self.current_price = new_price
        self.current_value = self.size * new_price

        if self.side == PositionSide.LONG:
            self.unrealized_pnl = self.current_value - self.total_cost
        else:
            # For short positions, profit when price goes down
            self.unrealized_pnl = self.total_cost - self.current_value

        if self.total_cost > 0:
            self.unrealized_pnl_pct = self.unrealized_pnl / self.total_cost
        else:
            self.unrealized_pnl_pct = 0.0

        self.last_updated = datetime.utcnow()

    @property
    def is_profitable(self) -> bool:
        """Check if position is currently profitable."""
        return self.unrealized_pnl > 0

    @property
    def should_stop_loss(self) -> bool:
        """Check if stop loss should be triggered."""
        if self.stop_loss_price is None:
            return False
        if self.side == PositionSide.LONG:
            return self.current_price <= self.stop_loss_price
        return self.current_price >= self.stop_loss_price

    @property
    def should_take_profit(self) -> bool:
        """Check if take profit should be triggered."""
        if self.take_profit_price is None:
            return False
        if self.side == PositionSide.LONG:
            return self.current_price >= self.take_profit_price
        return self.current_price <= self.take_profit_price


class PortfolioSnapshot(BaseModel):
    """Portfolio state at a point in time."""

    timestamp: datetime = Field(default_factory=datetime.utcnow)
    total_value: float = Field(description="Total portfolio value in USD")
    cash_balance: float = Field(description="Available cash in USD")
    positions_value: float = Field(description="Total value of positions")
    total_unrealized_pnl: float = Field(default=0.0, description="Total unrealized P&L")
    total_realized_pnl: float = Field(default=0.0, description="Total realized P&L")
    daily_pnl: float = Field(default=0.0, description="P&L for current day")
    position_count: int = Field(default=0, description="Number of open positions")

    @property
    def total_pnl(self) -> float:
        """Total P&L (realized + unrealized)."""
        return self.total_realized_pnl + self.total_unrealized_pnl

    @property
    def available_to_trade(self) -> float:
        """Cash available for new trades."""
        return self.cash_balance
