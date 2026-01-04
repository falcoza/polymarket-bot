"""Market and price data models."""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


class Market(BaseModel):
    """Polymarket market data model."""

    id: str = Field(description="Unique market identifier")
    question: str = Field(description="Market question text")
    condition_id: str = Field(default="", description="Condition ID for the market")
    slug: str = Field(default="", description="URL slug for the market")
    description: Optional[str] = Field(default=None, description="Detailed description")

    # Token IDs for trading
    clob_token_ids: List[str] = Field(
        default_factory=list,
        description="Token IDs for YES and NO outcomes",
    )

    # Pricing
    outcomes: List[str] = Field(
        default_factory=lambda: ["Yes", "No"],
        description="Outcome labels",
    )
    outcome_prices: List[float] = Field(
        default_factory=lambda: [0.5, 0.5],
        description="Current prices for each outcome",
    )

    # Liquidity & Volume
    liquidity: float = Field(default=0.0, description="Market liquidity in USD")
    volume: float = Field(default=0.0, description="Total volume in USD")
    volume_24hr: Optional[float] = Field(default=None, description="24-hour volume")
    volume_1wk: Optional[float] = Field(default=None, description="1-week volume")

    # Best quotes
    best_bid: Optional[float] = Field(default=None, description="Best bid price")
    best_ask: Optional[float] = Field(default=None, description="Best ask price")
    spread: Optional[float] = Field(default=None, description="Bid-ask spread")

    # Status
    active: bool = Field(default=True, description="Whether market is active")
    closed: bool = Field(default=False, description="Whether market is closed")
    end_date: Optional[datetime] = Field(default=None, description="Market end date")

    # Metadata
    category: Optional[str] = Field(default=None, description="Market category")
    image: Optional[str] = Field(default=None, description="Market image URL")

    @property
    def yes_token_id(self) -> str:
        """Get the YES token ID."""
        return self.clob_token_ids[0] if self.clob_token_ids else ""

    @property
    def no_token_id(self) -> str:
        """Get the NO token ID."""
        return self.clob_token_ids[1] if len(self.clob_token_ids) > 1 else ""

    @property
    def yes_price(self) -> float:
        """Get the YES outcome price."""
        return self.outcome_prices[0] if self.outcome_prices else 0.5

    @property
    def no_price(self) -> float:
        """Get the NO outcome price."""
        return self.outcome_prices[1] if len(self.outcome_prices) > 1 else 0.5

    @property
    def is_tradeable(self) -> bool:
        """Check if market is tradeable."""
        return (
            self.active
            and not self.closed
            and len(self.clob_token_ids) >= 2
            and self.liquidity > 0
        )


class PriceSnapshot(BaseModel):
    """Point-in-time price data for a token."""

    market_id: str = Field(description="Market identifier")
    token_id: str = Field(description="Token identifier")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow,
        description="Snapshot timestamp",
    )
    mid_price: float = Field(description="Mid-market price")
    best_bid: Optional[float] = Field(default=None, description="Best bid price")
    best_ask: Optional[float] = Field(default=None, description="Best ask price")
    volume: Optional[float] = Field(default=None, description="Volume at snapshot")
