"""Configuration management using pydantic-settings."""

from enum import Enum
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(str, Enum):
    """Trading mode options."""

    LIVE = "live"
    PAPER = "paper"
    BACKTEST = "backtest"


class SignatureType(int, Enum):
    """Wallet signature types for Polymarket."""

    EOA = 0  # MetaMask/hardware wallet
    POLY_PROXY = 1  # Polymarket proxy wallet
    POLY_GNOSIS_SAFE = 2  # Gnosis Safe


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Polymarket API Configuration
    polygon_wallet_private_key: str = Field(
        default="",
        description="Polygon wallet private key for signing transactions",
    )
    funder_address: Optional[str] = Field(
        default=None,
        description="Optional funder address for proxy wallets",
    )
    signature_type: SignatureType = Field(
        default=SignatureType.EOA,
        description="Wallet signature type",
    )
    chain_id: int = Field(
        default=137,
        description="Polygon mainnet chain ID",
    )

    # API Endpoints
    clob_host: str = Field(
        default="https://clob.polymarket.com",
        description="Polymarket CLOB API endpoint",
    )
    gamma_host: str = Field(
        default="https://gamma-api.polymarket.com",
        description="Polymarket Gamma API endpoint",
    )

    # Anthropic API
    anthropic_api_key: str = Field(
        default="",
        description="Anthropic API key for Claude",
    )
    claude_model: str = Field(
        default="claude-sonnet-4-20250514",
        description="Claude model to use for analysis",
    )

    # Trading Mode
    trading_mode: TradingMode = Field(
        default=TradingMode.PAPER,
        description="Trading mode: paper, live, or backtest",
    )

    # Risk Management (scaled for small capital - adjust as capital grows)
    max_position_size_usd: float = Field(
        default=5.0,
        description="Maximum position size per trade in USD",
    )
    max_total_exposure_usd: float = Field(
        default=10.0,
        description="Maximum total portfolio exposure in USD",
    )
    max_position_per_market_pct: float = Field(
        default=0.33,
        description="Maximum percentage of portfolio per market",
    )
    daily_loss_limit_usd: float = Field(
        default=3.0,
        description="Daily loss limit that triggers circuit breaker",
    )
    stop_loss_pct: float = Field(
        default=0.15,
        description="Stop loss percentage (0.15 = 15%)",
    )

    # Strategy Parameters
    momentum_lookback_periods: int = Field(
        default=24,
        description="Lookback period in hours for momentum calculation",
    )
    momentum_threshold: float = Field(
        default=0.05,
        description="Minimum price change to generate momentum signal",
    )
    llm_confidence_threshold: float = Field(
        default=0.7,
        description="Minimum confidence for LLM signals",
    )

    # Capital Settings (for $15 start, scaling up)
    initial_capital: float = Field(
        default=15.0,
        description="Starting capital in USD",
    )
    weekly_target_growth: float = Field(
        default=0.20,
        description="Weekly growth target (0.20 = 20%)",
    )

    # Arbitrage Strategy Settings (small capital optimized)
    arbitrage_min_spread: float = Field(
        default=0.03,
        description="Minimum spread for arbitrage (0.03 = 3%)",
    )
    arbitrage_max_slippage: float = Field(
        default=0.01,
        description="Maximum acceptable slippage (0.01 = 1%)",
    )
    arbitrage_min_liquidity: float = Field(
        default=1000.0,
        description="Minimum liquidity per side in USD",
    )
    arbitrage_min_position: float = Field(
        default=1.0,
        description="Minimum position size per leg in USD",
    )
    arbitrage_max_position: float = Field(
        default=5.0,
        description="Maximum position size per leg in USD",
    )
    arbitrage_execution_timeout: int = Field(
        default=30,
        description="Seconds to wait for both legs to fill",
    )
    arbitrage_crypto_only: bool = Field(
        default=True,
        description="Only trade 15-min crypto resolution markets",
    )
    arbitrage_max_concurrent: int = Field(
        default=2,
        description="Maximum concurrent arbitrage positions",
    )
    arbitrage_scan_interval: int = Field(
        default=60,
        description="Seconds between market scans for opportunities",
    )

    # Market Quality Settings
    min_market_quality_score: float = Field(
        default=60.0,
        description="Minimum quality score to trade (0-100)",
    )
    min_liquidity_threshold: float = Field(
        default=1000.0,
        description="Minimum market liquidity in USD",
    )
    max_spread_threshold: float = Field(
        default=0.05,
        description="Maximum spread for non-arb strategies (0.05 = 5%)",
    )

    # Enhanced Risk Settings (conservative for small capital)
    max_weekly_loss_usd: float = Field(
        default=5.0,
        description="Maximum weekly loss limit in USD",
    )
    circuit_breaker_consecutive_losses: int = Field(
        default=10,
        description="Pause trading after this many consecutive losses",
    )

    # Execution
    default_order_type: str = Field(
        default="GTC",
        description="Default order type: GTC or FOK",
    )
    min_order_size_usd: float = Field(
        default=1.0,
        description="Minimum order size in USD",
    )

    # Logging
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )
    log_file: str = Field(
        default="logs/trading.log",
        description="Log file path",
    )

    def validate_for_trading(self) -> tuple[bool, str]:
        """Validate settings are sufficient for trading."""
        if not self.polygon_wallet_private_key:
            return False, "POLYGON_WALLET_PRIVATE_KEY is required for trading"
        if self.trading_mode == TradingMode.LIVE:
            if not self.anthropic_api_key and self.llm_confidence_threshold > 0:
                return False, "ANTHROPIC_API_KEY required for LLM strategy in live mode"
        return True, "Settings valid"


def get_settings() -> Settings:
    """Get settings instance with environment variables loaded."""
    return Settings()
