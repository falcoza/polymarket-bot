"""Data models for whale scanner."""

from datetime import datetime
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class AlertType(str, Enum):
    """Type of whale alert."""

    FRESH_WALLET = "fresh_wallet"
    LARGE_BET = "large_bet"
    REPEAT_PATTERN = "repeat_pattern"
    LIQUIDITY_GRAB = "liquidity_grab"


class WhaleTrade(BaseModel):
    """A trade detected from the activity feed."""

    id: str = Field(description="Trade ID")
    wallet_address: str = Field(description="Trader wallet address")
    market_id: str = Field(description="Market ID")
    market_question: str = Field(description="Market question")
    market_slug: str = Field(default="", description="Market slug for URL")

    side: str = Field(description="BUY or SELL")
    outcome: str = Field(description="YES or NO")
    price: float = Field(description="Trade price")
    size: float = Field(description="Number of shares")
    value_usd: float = Field(description="USD value of trade")

    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Market context
    market_liquidity: Optional[float] = Field(default=None)
    market_category: Optional[str] = Field(default=None)


class WalletProfile(BaseModel):
    """Profile information about a wallet."""

    address: str = Field(description="Wallet address")

    # Activity metrics
    total_trades: int = Field(default=0)
    total_volume_usd: float = Field(default=0)
    total_profit_usd: float = Field(default=0)

    # Timing
    first_trade_date: Optional[datetime] = Field(default=None)
    last_trade_date: Optional[datetime] = Field(default=None)

    # Positions
    open_positions: int = Field(default=0)
    markets_traded: int = Field(default=0)

    @property
    def wallet_age_days(self) -> int:
        """Days since first trade."""
        if not self.first_trade_date:
            return 0
        delta = datetime.utcnow() - self.first_trade_date
        return delta.days

    @property
    def is_fresh(self) -> bool:
        """Is this a fresh wallet (< 30 days old)."""
        return self.wallet_age_days < 30

    @property
    def is_new_trader(self) -> bool:
        """Is this a new trader (< 10 trades)."""
        return self.total_trades < 10


class WhaleAlert(BaseModel):
    """Alert for detected whale activity."""

    id: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # Trade info
    trade: WhaleTrade = Field(description="The trade that triggered alert")
    wallet: WalletProfile = Field(description="Wallet profile")

    # Alert classification
    alert_types: List[AlertType] = Field(description="Types of suspicious activity")
    confidence: float = Field(default=0.5, description="Confidence score 0-1")

    # Context
    reasons: List[str] = Field(default_factory=list)

    def format_console(self) -> str:
        """Format alert for console output."""
        types_str = " + ".join([t.value.replace("_", " ").title() for t in self.alert_types])
        wallet_short = f"{self.trade.wallet_address[:6]}...{self.trade.wallet_address[-4:]}"

        lines = [
            "",
            "━" * 50,
            "🐋 WHALE ALERT",
            "━" * 50,
            f"Wallet:  {wallet_short}",
            f"Type:    {types_str}",
            f"Market:  \"{self.trade.market_question[:45]}...\"" if len(self.trade.market_question) > 45 else f"Market:  \"{self.trade.market_question}\"",
            f"Action:  {self.trade.side} {self.trade.outcome} @ ${self.trade.price:.2f}",
            f"Size:    ${self.trade.value_usd:,.0f}",
            "━" * 50,
            f"Wallet Age:    {self.wallet.wallet_age_days} days",
            f"Prior Trades:  {self.wallet.total_trades}",
            f"Polymarket:    https://polymarket.com/@{self.trade.wallet_address}",
            "━" * 50,
        ]

        if self.reasons:
            lines.append("Reasons:")
            for reason in self.reasons:
                lines.append(f"  • {reason}")
            lines.append("━" * 50)

        return "\n".join(lines)
