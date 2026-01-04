"""Position size and exposure limit enforcement."""

import logging

from config.settings import Settings

logger = logging.getLogger(__name__)


class PositionLimiter:
    """Enforce position size and exposure limits."""

    def __init__(self, settings: Settings):
        """Initialize position limits from settings."""
        self.max_position_size = settings.max_position_size_usd
        self.max_total_exposure = settings.max_total_exposure_usd
        self.max_market_concentration = settings.max_position_per_market_pct

    def check_single_trade_limit(self, trade_value: float) -> bool:
        """Check if trade is within single-trade limit."""
        within_limit = trade_value <= self.max_position_size
        if not within_limit:
            logger.warning(
                f"Trade value ${trade_value:.2f} exceeds max position "
                f"${self.max_position_size:.2f}"
            )
        return within_limit

    def check_total_exposure(self, total_exposure: float) -> bool:
        """Check if total exposure is within limit."""
        within_limit = total_exposure <= self.max_total_exposure
        if not within_limit:
            logger.warning(
                f"Total exposure ${total_exposure:.2f} exceeds max "
                f"${self.max_total_exposure:.2f}"
            )
        return within_limit

    def check_market_concentration(
        self,
        market_exposure: float,
        portfolio_value: float,
    ) -> bool:
        """Check if market concentration is within limit."""
        if portfolio_value <= 0:
            return True

        concentration = market_exposure / portfolio_value
        within_limit = concentration <= self.max_market_concentration

        if not within_limit:
            logger.warning(
                f"Market concentration {concentration:.1%} exceeds max "
                f"{self.max_market_concentration:.1%}"
            )
        return within_limit

    def calculate_max_trade_size(
        self,
        current_exposure: float,
        market_exposure: float,
        portfolio_value: float,
    ) -> float:
        """Calculate maximum trade size given current state."""
        # Limit from max position size
        limit_from_position = self.max_position_size

        # Limit from total exposure
        remaining_exposure = self.max_total_exposure - current_exposure
        limit_from_exposure = max(0, remaining_exposure)

        # Limit from market concentration
        if portfolio_value > 0:
            max_market_value = portfolio_value * self.max_market_concentration
            limit_from_concentration = max(0, max_market_value - market_exposure)
        else:
            limit_from_concentration = self.max_position_size

        return min(limit_from_position, limit_from_exposure, limit_from_concentration)
