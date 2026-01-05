"""Copy trading execution for following whale trades."""

import logging
from datetime import datetime
from typing import Optional

from config.settings import Settings, TradingMode
from src.api.clob_client import PolymarketClobClient
from src.scanner.models import WhaleAlert

logger = logging.getLogger(__name__)


class CopyTrader:
    """Executes copy trades following whale activity."""

    def __init__(self, settings: Settings):
        """Initialize copy trader.

        Args:
            settings: Application settings
        """
        self.settings = settings
        self.trading_mode = settings.trading_mode
        self.max_position_usd = getattr(settings, "whale_copy_max_usd", 10.0)

        # Initialize CLOB client for live trading
        self.clob_client: Optional[PolymarketClobClient] = None
        if self.trading_mode == TradingMode.LIVE:
            try:
                self.clob_client = PolymarketClobClient(settings)
                self.clob_client.connect()
                logger.info("Copy trader connected to Polymarket")
            except Exception as e:
                logger.error(f"Failed to connect copy trader: {e}")

        # Track executed copies
        self._executed_copies: list = []

    async def execute_copy(self, alert: WhaleAlert, size_usd: float) -> bool:
        """Execute a copy trade following a whale.

        Args:
            alert: The whale alert to copy
            size_usd: USD amount to trade

        Returns:
            True if trade executed successfully
        """
        trade = alert.trade

        # Cap size at max
        size_usd = min(size_usd, self.max_position_usd)

        logger.info(
            f"Executing copy trade: {trade.side} {trade.outcome} "
            f"${size_usd:.2f} on {trade.market_question[:40]}..."
        )

        if self.trading_mode == TradingMode.PAPER:
            return await self._paper_execute(alert, size_usd)
        elif self.trading_mode == TradingMode.LIVE:
            return await self._live_execute(alert, size_usd)
        else:
            logger.warning(f"Unknown trading mode: {self.trading_mode}")
            return False

    async def _paper_execute(self, alert: WhaleAlert, size_usd: float) -> bool:
        """Simulate copy trade in paper mode."""
        trade = alert.trade

        # Calculate shares
        shares = size_usd / trade.price if trade.price > 0 else 0

        # Record the trade
        copy_record = {
            "timestamp": datetime.utcnow().isoformat(),
            "mode": "paper",
            "whale_wallet": trade.wallet_address,
            "market_id": trade.market_id,
            "market_question": trade.market_question,
            "side": trade.side,
            "outcome": trade.outcome,
            "price": trade.price,
            "size_usd": size_usd,
            "shares": shares,
            "whale_size_usd": trade.value_usd,
            "status": "filled",
        }
        self._executed_copies.append(copy_record)

        logger.info(
            f"[PAPER] Copy trade executed: {trade.side} {trade.outcome} "
            f"{shares:.2f} shares @ ${trade.price:.2f} = ${size_usd:.2f}"
        )

        return True

    async def _live_execute(self, alert: WhaleAlert, size_usd: float) -> bool:
        """Execute copy trade on live exchange."""
        if not self.clob_client or not self.clob_client.is_connected:
            logger.error("CLOB client not connected for live trading")
            return False

        trade = alert.trade

        try:
            # Determine token ID based on outcome
            # This requires fetching market data to get the correct token
            # For now, we'll use the token from the whale's trade if available

            # Create market order
            order = self.clob_client.create_market_order(
                token_id=trade.market_id,  # May need adjustment based on API
                side=trade.side,
                amount_usd=size_usd,
            )

            # Submit order
            response = self.clob_client.submit_order(order, order_type="FOK")

            if response.get("success") or response.get("status") == "filled":
                # Record the trade
                copy_record = {
                    "timestamp": datetime.utcnow().isoformat(),
                    "mode": "live",
                    "whale_wallet": trade.wallet_address,
                    "market_id": trade.market_id,
                    "market_question": trade.market_question,
                    "side": trade.side,
                    "outcome": trade.outcome,
                    "price": trade.price,
                    "size_usd": size_usd,
                    "whale_size_usd": trade.value_usd,
                    "order_id": response.get("orderID") or response.get("id"),
                    "status": "filled",
                }
                self._executed_copies.append(copy_record)

                logger.info(f"[LIVE] Copy trade executed: {response}")
                return True
            else:
                logger.warning(f"Copy trade failed: {response}")
                return False

        except Exception as e:
            logger.error(f"Copy trade error: {e}")
            return False

    def get_copy_history(self) -> list:
        """Get history of executed copy trades."""
        return self._executed_copies.copy()

    def get_total_copied_usd(self) -> float:
        """Get total USD value of copy trades."""
        return sum(c.get("size_usd", 0) for c in self._executed_copies)

    def close(self) -> None:
        """Close connections."""
        if self.clob_client:
            # CLOB client doesn't have explicit close method
            pass
