"""Trading signal data models."""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class SignalType(str, Enum):
    """Trading signal type."""

    STRONG_BUY = "strong_buy"
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"
    STRONG_SELL = "strong_sell"


class TradingSignal(BaseModel):
    """Trading signal from a strategy."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    market_id: str = Field(description="Market identifier")
    token_id: str = Field(description="Token identifier")

    signal_type: SignalType = Field(description="Signal type")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence level 0-1")

    # Recommended action
    suggested_side: Optional[str] = Field(
        default=None,
        description="Suggested side: BUY or SELL",
    )
    suggested_size_pct: Optional[float] = Field(
        default=None,
        description="Suggested size as percentage of max position",
    )
    suggested_price: Optional[float] = Field(
        default=None,
        description="Suggested limit price",
    )

    # Strategy info
    strategy_name: str = Field(description="Strategy that generated this signal")

    # Analysis details
    reasoning: Optional[str] = Field(
        default=None,
        description="Explanation for the signal",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional signal metadata",
    )

    timestamp: datetime = Field(default_factory=datetime.utcnow)

    @property
    def is_actionable(self) -> bool:
        """Check if signal suggests taking action."""
        return self.signal_type in [
            SignalType.STRONG_BUY,
            SignalType.BUY,
            SignalType.SELL,
            SignalType.STRONG_SELL,
        ]

    @property
    def is_bullish(self) -> bool:
        """Check if signal is bullish."""
        return self.signal_type in [SignalType.STRONG_BUY, SignalType.BUY]

    @property
    def is_bearish(self) -> bool:
        """Check if signal is bearish."""
        return self.signal_type in [SignalType.STRONG_SELL, SignalType.SELL]
