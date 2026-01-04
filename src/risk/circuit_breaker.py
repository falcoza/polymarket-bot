"""Circuit breaker for emergency trading halts."""

import logging
from datetime import date, datetime
from typing import Optional

from config.settings import Settings

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """Circuit breaker for emergency trading halts."""

    def __init__(self, settings: Settings):
        """Initialize circuit breaker with limits."""
        self.daily_loss_limit = settings.daily_loss_limit_usd
        self.max_daily_trades = 100  # Hard limit on daily trades

        # State
        self._triggered = False
        self.trigger_reason: Optional[str] = None
        self.trigger_time: Optional[datetime] = None

        # Daily tracking
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._current_date = date.today()

    def is_triggered(self) -> bool:
        """Check if circuit breaker is currently active."""
        self._check_date_rollover()
        return self._triggered

    def trigger(self, reason: str) -> None:
        """Manually trigger the circuit breaker."""
        self._triggered = True
        self.trigger_reason = reason
        self.trigger_time = datetime.utcnow()
        logger.critical(f"CIRCUIT BREAKER TRIGGERED: {reason}")

    def reset(self) -> None:
        """Manually reset the circuit breaker."""
        if self._triggered:
            logger.info(
                f"Circuit breaker reset. Was triggered at {self.trigger_time} "
                f"for: {self.trigger_reason}"
            )
        self._triggered = False
        self.trigger_reason = None
        self.trigger_time = None

    def check_daily_loss(self, daily_pnl: float) -> bool:
        """Check if daily loss limit has been hit.

        Args:
            daily_pnl: Current day's P&L (negative for losses)

        Returns:
            True if trading should continue, False if limit hit
        """
        if daily_pnl <= -self.daily_loss_limit:
            self.trigger(f"Daily loss limit reached: ${daily_pnl:.2f}")
            return False
        return True

    def check_trade_count(self) -> bool:
        """Check if daily trade limit has been hit.

        Returns:
            True if trading should continue, False if limit hit
        """
        self._check_date_rollover()
        if self._daily_trades >= self.max_daily_trades:
            self.trigger(f"Max daily trades reached: {self._daily_trades}")
            return False
        return True

    def record_trade(self, pnl: float = 0.0) -> None:
        """Record a trade for daily tracking.

        Args:
            pnl: P&L from the trade (can be 0 for new positions)
        """
        self._check_date_rollover()
        self._daily_pnl += pnl
        self._daily_trades += 1

        logger.debug(
            f"Trade recorded. Daily stats: trades={self._daily_trades}, "
            f"pnl=${self._daily_pnl:.2f}"
        )

        # Check limits after recording
        self.check_daily_loss(self._daily_pnl)
        self.check_trade_count()

    def reset_daily(self) -> None:
        """Reset daily counters."""
        self._daily_pnl = 0.0
        self._daily_trades = 0
        self._current_date = date.today()
        logger.info("Daily trading stats reset")

    def _check_date_rollover(self) -> None:
        """Auto-reset on new day."""
        today = date.today()
        if today != self._current_date:
            logger.info(f"New trading day: {today}")
            self.reset_daily()
            if self._triggered:
                logger.info("New day - resetting circuit breaker")
                self.reset()

    @property
    def daily_pnl(self) -> float:
        """Get current daily P&L."""
        self._check_date_rollover()
        return self._daily_pnl

    @property
    def daily_trades(self) -> int:
        """Get current daily trade count."""
        self._check_date_rollover()
        return self._daily_trades

    @property
    def remaining_loss_budget(self) -> float:
        """Get remaining loss budget for the day."""
        return self.daily_loss_limit + self._daily_pnl

    @property
    def status(self) -> dict:
        """Get circuit breaker status."""
        self._check_date_rollover()
        return {
            "triggered": self._triggered,
            "trigger_reason": self.trigger_reason,
            "trigger_time": self.trigger_time,
            "daily_pnl": self._daily_pnl,
            "daily_trades": self._daily_trades,
            "remaining_loss_budget": self.remaining_loss_budget,
            "max_daily_trades": self.max_daily_trades,
        }
