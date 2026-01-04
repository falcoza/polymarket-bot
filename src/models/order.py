"""Order and trade data models."""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class OrderSide(str, Enum):
    """Order side: buy or sell."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    """Order type."""

    GTC = "GTC"  # Good till cancelled (limit order)
    FOK = "FOK"  # Fill or kill (market order)
    GTD = "GTD"  # Good till date


class OrderStatus(str, Enum):
    """Order status."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    FAILED = "failed"


class Order(BaseModel):
    """Internal order representation."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    market_id: str = Field(description="Market identifier")
    token_id: str = Field(description="Token identifier")

    side: OrderSide = Field(description="Order side: BUY or SELL")
    order_type: OrderType = Field(default=OrderType.GTC, description="Order type")

    # For limit orders (GTC)
    price: Optional[float] = Field(default=None, description="Limit price")
    size: Optional[float] = Field(default=None, description="Number of shares")

    # For market orders (FOK)
    amount: Optional[float] = Field(default=None, description="USD amount")

    # Tracking
    status: OrderStatus = Field(default=OrderStatus.PENDING, description="Order status")
    strategy_name: str = Field(default="manual", description="Strategy that created this order")

    # Exchange response
    exchange_order_id: Optional[str] = Field(
        default=None,
        description="Order ID from exchange",
    )
    filled_price: Optional[float] = Field(default=None, description="Actual fill price")
    filled_size: Optional[float] = Field(default=None, description="Actual filled size")

    # Timestamps
    created_at: datetime = Field(default_factory=datetime.utcnow)
    submitted_at: Optional[datetime] = Field(default=None)
    filled_at: Optional[datetime] = Field(default=None)

    @property
    def is_complete(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.FAILED,
        ]

    @property
    def value_usd(self) -> float:
        """Calculate order value in USD."""
        if self.amount:
            return self.amount
        if self.price and self.size:
            return self.price * self.size
        return 0.0


class Trade(BaseModel):
    """Executed trade record."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_id: str = Field(description="Associated order ID")
    market_id: str = Field(description="Market identifier")
    token_id: str = Field(description="Token identifier")

    side: OrderSide = Field(description="Trade side")
    price: float = Field(description="Execution price")
    size: float = Field(description="Number of shares traded")
    value_usd: float = Field(description="Trade value in USD")

    strategy_name: str = Field(description="Strategy that generated this trade")
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # P&L tracking (filled when position closed)
    realized_pnl: Optional[float] = Field(
        default=None,
        description="Realized P&L when position is closed",
    )
