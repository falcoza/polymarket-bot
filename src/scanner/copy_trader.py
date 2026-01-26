"""Copy trading execution for following whale trades."""

import logging
import uuid
from datetime import datetime
from typing import Optional

from config.settings import Settings, TradingMode
from src.api.clob_client import PolymarketClobClient
from src.data.storage import DatabaseStorage
from src.scanner.models import WhaleAlert

logger = logging.getLogger(__name__)


class CopyTrader:
    """Executes copy trades following whale activity."""

    def __init__(self, settings: Settings, db_path: str = "data/trading.db"):
        """Initialize copy trader.

        Args:
            settings: Application settings
            db_path: Path to the SQLite database
        """
        self.settings = settings
        self.trading_mode = settings.trading_mode
        self.max_position_usd = getattr(settings, "whale_copy_max_usd", 10.0)

        # Initialize database storage for persistent P&L tracking
        self.db = DatabaseStorage(db_path)
        logger.info(f"Copy trader initialized with database at {db_path}")

        # Initialize CLOB client for live trading
        self.clob_client: Optional[PolymarketClobClient] = None
        if self.trading_mode == TradingMode.LIVE:
            try:
                self.clob_client = PolymarketClobClient(settings)
                self.clob_client.connect()
                logger.info("Copy trader connected to Polymarket")
            except Exception as e:
                logger.error(f"Failed to connect copy trader: {e}")

        # Track executed copies (in-memory for quick lookup, persisted to DB)
        # Limited to 100 entries to prevent memory leaks on Railway
        self._executed_copies: list = []
        self._max_in_memory_copies = 100

    def _calculate_position_scale(self, entry_price: float) -> float:
        """Scale position size based on price extremity.

        Full size at 0.50 (balanced odds), reduces toward extremes.
        This prevents over-sizing on low-probability bets.

        Args:
            entry_price: The entry price (0-1)

        Returns:
            Scale factor (0.1 to 1.0)
        """
        # Distance from center (0.5)
        distance_from_center = abs(entry_price - 0.50)
        # Scale: 1.0 at center, reduces by 2x distance
        # At 0.50 -> 1.0, at 0.75 -> 0.5, at 0.90 -> 0.2
        scale = max(0.1, 1.0 - (distance_from_center * 2))
        return scale

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

        # ===== NEW: POSITION SCALING BY PRICE =====
        # Scale down position for prices further from 0.50
        scale = self._calculate_position_scale(trade.price)
        original_size = size_usd
        size_usd = size_usd * scale

        if scale < 1.0:
            logger.info(
                f"Position scaled: ${original_size:.2f} -> ${size_usd:.2f} "
                f"(scale={scale:.0%} at price ${trade.price:.2f})"
            )

        # ===== NEW: MINIMUM SIZE CHECK =====
        if size_usd < 1.0:
            logger.info(f"Skipping copy: scaled size ${size_usd:.2f} too small")
            return False

        # ===== NEW: LIQUIDITY CHECK =====
        if trade.market_liquidity and trade.market_liquidity < size_usd * 20:
            logger.warning(
                f"Skipping copy: insufficient liquidity "
                f"(${trade.market_liquidity:.0f} < ${size_usd * 20:.0f} required)"
            )
            return False

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

        # Generate unique ID for this trade
        trade_id = str(uuid.uuid4())

        # Record the trade with full details for P&L tracking
        copy_record = {
            "id": trade_id,
            "timestamp": datetime.utcnow().isoformat(),
            "mode": "paper",
            "whale_wallet": trade.wallet_address,
            "market_id": trade.market_id,
            "market_slug": getattr(trade, "market_slug", None),
            "market_question": trade.market_question,
            "condition_id": getattr(trade, "condition_id", None),
            "token_id": getattr(trade, "token_id", None),
            "side": trade.side,
            "outcome": trade.outcome,
            "entry_price": trade.price,
            "size_usd": size_usd,
            "shares": shares,
            "whale_size_usd": trade.value_usd,
            "confidence": alert.confidence,
            "signals": alert.signals if hasattr(alert, "signals") else [],
            "status": "open",
            "created_at": datetime.utcnow().isoformat(),
        }

        # Save to database for persistent tracking
        try:
            self.db.save_copy_trade(copy_record)
            logger.info(f"Copy trade saved to database with ID: {trade_id}")
        except Exception as e:
            logger.error(f"Failed to save copy trade to database: {e}")

        # Also keep in memory (with limit to prevent OOM)
        self._executed_copies.append(copy_record)
        if len(self._executed_copies) > self._max_in_memory_copies:
            self._executed_copies = self._executed_copies[-self._max_in_memory_copies:]

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
        """Get history of executed copy trades from database."""
        try:
            return self.db.get_copy_trades(limit=100)
        except Exception as e:
            logger.error(f"Failed to get copy history from DB: {e}")
            return self._executed_copies.copy()

    def get_total_copied_usd(self) -> float:
        """Get total USD value of copy trades."""
        try:
            summary = self.db.get_copy_trade_summary()
            return summary.get("total_deployed_usd", 0)
        except Exception:
            return sum(c.get("size_usd", 0) for c in self._executed_copies)

    def get_pnl_summary(self) -> dict:
        """Get P&L summary for all copy trades.

        Returns:
            Dict with P&L statistics
        """
        try:
            return self.db.get_copy_trade_summary()
        except Exception as e:
            logger.error(f"Failed to get P&L summary: {e}")
            return {
                "total_trades": len(self._executed_copies),
                "total_deployed_usd": self.get_total_copied_usd(),
                "open_trades": len(self._executed_copies),
                "resolved_trades": 0,
                "total_realized_pnl": 0,
            }

    def get_open_trades(self) -> list:
        """Get all open (unresolved) copy trades."""
        try:
            return self.db.get_open_copy_trades()
        except Exception as e:
            logger.error(f"Failed to get open trades: {e}")
            return []

    def resolve_trade(
        self,
        trade_id: str,
        resolved_outcome: str,
        won: bool
    ) -> float:
        """Resolve a copy trade and calculate P&L.

        Args:
            trade_id: The copy trade ID
            resolved_outcome: What the market resolved to
            won: Whether our position won

        Returns:
            The realized P&L
        """
        # Get the trade details
        trades = self.db.get_copy_trades()
        trade = next((t for t in trades if t["id"] == trade_id), None)

        if not trade:
            logger.error(f"Trade {trade_id} not found")
            return 0.0

        entry_price = trade["entry_price"]
        size_usd = trade["size_usd"]
        shares = trade["shares"]

        # Calculate P&L
        if won:
            # Won: receive $1 per share, paid entry_price per share
            exit_price = 1.0
            realized_pnl = (1.0 - entry_price) * shares
        else:
            # Lost: receive $0 per share, paid entry_price per share
            exit_price = 0.0
            realized_pnl = -entry_price * shares

        # Update in database
        self.db.resolve_copy_trade(
            trade_id=trade_id,
            resolved_outcome=resolved_outcome,
            exit_price=exit_price,
            realized_pnl=realized_pnl
        )

        logger.info(
            f"Trade {trade_id} resolved: {resolved_outcome}, "
            f"P&L: ${realized_pnl:+.2f}"
        )

        return realized_pnl

    def close(self) -> None:
        """Close connections."""
        if self.clob_client:
            # CLOB client doesn't have explicit close method
            pass
