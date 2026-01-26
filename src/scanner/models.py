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
    IMMINENT_RESOLUTION = "imminent_resolution"
    CONTRARIAN_BET = "contrarian_bet"


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

    # Market timing (for resolution timing detection)
    market_end_date: Optional[datetime] = Field(default=None, description="When market resolves")
    market_created_at: Optional[datetime] = Field(default=None, description="When market was created")

    # Market prices (for contrarian detection)
    market_yes_price: Optional[float] = Field(default=None, description="Current YES price (0-1)")

    @property
    def hours_until_resolution(self) -> Optional[float]:
        """Hours until market resolves. None if no end date."""
        if not self.market_end_date:
            return None
        delta = self.market_end_date - datetime.utcnow()
        return max(0, delta.total_seconds() / 3600)

    @property
    def is_imminent_resolution(self) -> bool:
        """Is market resolving within 24 hours?"""
        hours = self.hours_until_resolution
        return hours is not None and hours < 24

    @property
    def market_age_hours(self) -> Optional[float]:
        """Hours since market was created."""
        if not self.market_created_at:
            return None
        delta = datetime.utcnow() - self.market_created_at
        return delta.total_seconds() / 3600

    @property
    def is_contrarian_bet(self) -> bool:
        """Is this bet against strong consensus (>70%)?"""
        if self.market_yes_price is None:
            return False
        # Buying NO when YES > 70% = contrarian
        if self.outcome.upper() == "NO" and self.market_yes_price > 0.70:
            return True
        # Buying YES when YES < 30% = contrarian
        if self.outcome.upper() == "YES" and self.market_yes_price < 0.30:
            return True
        return False

    @property
    def risk_reward_ratio(self) -> float:
        """Calculate risk/reward ratio for this trade.

        For a YES position: risk is losing entry_price, reward is (1 - entry_price)

        Returns:
            Risk/reward ratio (higher is better, 1.0 = even, >1 = favorable)
        """
        if self.price <= 0 or self.price >= 1:
            return 0
        risk = self.price
        reward = 1.0 - self.price
        return reward / risk


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
    wallet_created_date: Optional[datetime] = Field(default=None)

    # Positions
    open_positions: int = Field(default=0)
    markets_traded: int = Field(default=0)  # Unique markets, not total trades

    @property
    def wallet_age_days(self) -> int:
        """Days since first trade."""
        if not self.first_trade_date:
            return 0
        delta = datetime.utcnow() - self.first_trade_date
        return delta.days

    @property
    def wallet_age_hours(self) -> float:
        """Hours since first trade (more granular)."""
        if not self.first_trade_date:
            return 0
        delta = datetime.utcnow() - self.first_trade_date
        return delta.total_seconds() / 3600

    @property
    def is_ultra_fresh(self) -> bool:
        """Is this wallet < 1 day old (highest signal)."""
        return self.wallet_age_hours < 24

    @property
    def is_fresh(self) -> bool:
        """Is this a fresh wallet (< 7 days old)."""
        return self.wallet_age_days < 7

    @property
    def is_new_trader(self) -> bool:
        """Is this a new trader (< 3 unique markets)."""
        return self.markets_traded < 3

    @property
    def is_low_market_count(self) -> bool:
        """Has traded in very few markets (< 3)."""
        return self.markets_traded < 3

    @property
    def wc_tx_ratio(self) -> float:
        """Wallet creation to first transaction ratio (0-1).

        Lower ratio = faster to trade after creation = more suspicious.
        """
        if not self.wallet_created_date or not self.first_trade_date:
            return 1.0  # Unknown, assume not suspicious

        # Time from creation to first trade
        time_to_trade = (self.first_trade_date - self.wallet_created_date).total_seconds()
        # Normalize: 0% = immediate trade, 100% = traded after 24+ hours
        # wc/tx under 20% means they traded within ~5 hours of creation
        ratio = min(time_to_trade / (24 * 3600), 1.0)
        return ratio

    @property
    def is_quick_to_trade(self) -> bool:
        """Did they trade very quickly after wallet creation (wc/tx < 20%)."""
        return self.wc_tx_ratio < 0.20


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
    signals: List[str] = Field(default_factory=list, description="Clean signal list for storage")

    def format_console(self) -> str:
        """Format alert for console output."""
        types_str = " + ".join([t.value.replace("_", " ").title() for t in self.alert_types])
        wallet_short = f"{self.trade.wallet_address[:6]}...{self.trade.wallet_address[-4:]}"

        # Determine confidence emoji
        if self.confidence >= 0.8:
            conf_emoji = "🚨"
        elif self.confidence >= 0.6:
            conf_emoji = "⚠️"
        else:
            conf_emoji = "📊"

        # Wallet age display
        if self.wallet.wallet_age_hours < 24:
            age_str = f"{self.wallet.wallet_age_hours:.1f} hours (ULTRA FRESH)"
        else:
            age_str = f"{self.wallet.wallet_age_days} days"

        # Resolution timing display
        resolution_str = "Unknown"
        if self.trade.hours_until_resolution is not None:
            hours = self.trade.hours_until_resolution
            if hours < 6:
                resolution_str = f"{hours:.1f} hours (🚨 IMMINENT)"
            elif hours < 24:
                resolution_str = f"{hours:.1f} hours (⚠️ SOON)"
            elif hours < 72:
                resolution_str = f"{hours:.0f} hours"
            else:
                resolution_str = f"{hours / 24:.0f} days"

        # Market price display
        price_str = ""
        if self.trade.market_yes_price is not None:
            yes_pct = self.trade.market_yes_price * 100
            if self.trade.is_contrarian_bet:
                price_str = f" (CONTRARIAN vs {yes_pct:.0f}% YES)"
            else:
                price_str = f" (Market: {yes_pct:.0f}% YES)"

        lines = [
            "",
            "━" * 60,
            f"{conf_emoji} SMART MONEY ALERT (Confidence: {self.confidence:.0%})",
            "━" * 60,
            f"Wallet:       {wallet_short}",
            f"Type:         {types_str}",
            f"Market:       \"{self.trade.market_question[:40]}...\"" if len(self.trade.market_question) > 40 else f"Market:       \"{self.trade.market_question}\"",
            f"Action:       {self.trade.side} {self.trade.outcome} @ ${self.trade.price:.2f}{price_str}",
            f"Size:         ${self.trade.value_usd:,.0f}",
            f"Resolves In:  {resolution_str}",
            "━" * 60,
            f"Wallet Age:   {age_str}",
            f"Markets:      {self.wallet.markets_traded} unique markets",
            f"Prior Trades: {self.wallet.total_trades}",
            f"wc/tx Ratio:  {self.wallet.wc_tx_ratio:.0%}" + (" (QUICK)" if self.wallet.is_quick_to_trade else ""),
            "━" * 60,
            f"Polymarket:   https://polymarket.com/@{self.trade.wallet_address}",
            "━" * 60,
        ]

        if self.reasons:
            lines.append("Signals Detected:")
            for reason in self.reasons:
                lines.append(f"  • {reason}")
            lines.append("━" * 55)

        return "\n".join(lines)
