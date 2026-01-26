"""Track market resolutions and update copy trade P&L."""

import logging
from datetime import datetime
from typing import Optional, Dict, Any, List
import httpx

from src.data.storage import DatabaseStorage

logger = logging.getLogger(__name__)


class ResolutionTracker:
    """Tracks market resolutions and calculates P&L for copy trades."""

    def __init__(self, db_path: str = "data/trading.db"):
        """Initialize resolution tracker.

        Args:
            db_path: Path to the SQLite database
        """
        self.db = DatabaseStorage(db_path)
        self.gamma_host = "https://gamma-api.polymarket.com"
        self.clob_host = "https://clob.polymarket.com"

    async def check_and_resolve_trades(self) -> Dict[str, Any]:
        """Check all open trades and resolve any that have completed.

        Returns:
            Summary of resolutions processed
        """
        open_trades = self.db.get_open_copy_trades()

        if not open_trades:
            logger.info("No open trades to check")
            return {"checked": 0, "resolved": 0, "total_pnl": 0}

        logger.info(f"Checking {len(open_trades)} open trades for resolution")

        resolved_count = 0
        total_pnl = 0.0

        async with httpx.AsyncClient(timeout=30.0) as client:
            for trade in open_trades:
                try:
                    result = await self._check_single_trade(client, trade)
                    if result["resolved"]:
                        resolved_count += 1
                        total_pnl += result["pnl"]
                except Exception as e:
                    logger.error(f"Error checking trade {trade['id']}: {e}")

        return {
            "checked": len(open_trades),
            "resolved": resolved_count,
            "total_pnl": total_pnl,
        }

    async def _check_single_trade(
        self,
        client: httpx.AsyncClient,
        trade: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Check if a single trade's market has resolved.

        Args:
            client: HTTP client
            trade: The copy trade record

        Returns:
            Dict with resolution status and P&L
        """
        market_id = trade.get("market_id")
        condition_id = trade.get("condition_id")
        market_slug = trade.get("market_slug")

        # Try to get market info
        market_info = await self._get_market_info(
            client, market_id, condition_id, market_slug
        )

        if not market_info:
            return {"resolved": False, "pnl": 0}

        # Check if market is resolved/closed
        is_closed = market_info.get("closed", False)
        is_resolved = market_info.get("resolved", False)

        if not (is_closed or is_resolved):
            return {"resolved": False, "pnl": 0}

        # Determine the outcome
        resolved_outcome = self._determine_outcome(market_info, trade)

        if resolved_outcome is None:
            return {"resolved": False, "pnl": 0}

        # Calculate P&L
        pnl = self._calculate_pnl(trade, resolved_outcome)

        # Update in database
        self.db.resolve_copy_trade(
            trade_id=trade["id"],
            resolved_outcome=resolved_outcome["outcome_name"],
            exit_price=resolved_outcome["exit_price"],
            realized_pnl=pnl
        )

        logger.info(
            f"Resolved trade {trade['id'][:8]}...: "
            f"{trade['market_question'][:30]}... -> {resolved_outcome['outcome_name']} "
            f"P&L: ${pnl:+.2f}"
        )

        return {"resolved": True, "pnl": pnl}

    async def _get_market_info(
        self,
        client: httpx.AsyncClient,
        market_id: Optional[str],
        condition_id: Optional[str],
        market_slug: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Get market information from Polymarket API.

        Tries multiple endpoints to find the market.
        """
        # Try by condition ID first (most reliable)
        if condition_id:
            try:
                url = f"{self.gamma_host}/markets/{condition_id}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                logger.debug(f"Failed to fetch by condition_id: {e}")

        # Try by slug
        if market_slug:
            try:
                url = f"{self.gamma_host}/markets"
                params = {"slug": market_slug}
                resp = await client.get(url, params=params)
                if resp.status_code == 200:
                    markets = resp.json()
                    if markets:
                        return markets[0]
            except Exception as e:
                logger.debug(f"Failed to fetch by slug: {e}")

        # Try CLOB endpoint
        if market_id:
            try:
                url = f"{self.clob_host}/markets/{market_id}"
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as e:
                logger.debug(f"Failed to fetch from CLOB: {e}")

        return None

    def _determine_outcome(
        self,
        market_info: Dict[str, Any],
        trade: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """Determine the resolution outcome and if our trade won.

        Args:
            market_info: Market data from API
            trade: Our copy trade record

        Returns:
            Dict with outcome info or None if can't determine
        """
        # Get the winning outcome from market info
        winning_outcome = market_info.get("outcome")
        outcome_prices = market_info.get("outcomePrices", [])

        our_side = trade.get("side", "").upper()
        our_outcome = trade.get("outcome", "")

        # For Yes/No markets
        if winning_outcome in ["Yes", "No", "yes", "no"]:
            winning_outcome = winning_outcome.capitalize()

            # Check if we bet on the winning outcome
            if our_outcome.lower() == "yes":
                we_bet_yes = True
            elif our_outcome.lower() == "no":
                we_bet_yes = False
            else:
                # Outcome is a team/option name
                # BUY = betting it wins, SELL = betting it loses
                we_bet_yes = our_side == "BUY"

            if winning_outcome == "Yes":
                won = we_bet_yes
            else:
                won = not we_bet_yes

            return {
                "outcome_name": winning_outcome,
                "exit_price": 1.0 if won else 0.0,
                "won": won,
            }

        # For multi-outcome markets (sports, etc.)
        if winning_outcome:
            # Check if our outcome matches the winner
            our_outcome_lower = our_outcome.lower().strip()
            winning_lower = winning_outcome.lower().strip()

            # Direct match
            if our_outcome_lower == winning_lower:
                won = our_side == "BUY"
            elif our_outcome_lower in winning_lower or winning_lower in our_outcome_lower:
                won = our_side == "BUY"
            else:
                # Our outcome didn't win
                won = our_side == "SELL"

            return {
                "outcome_name": winning_outcome,
                "exit_price": 1.0 if won else 0.0,
                "won": won,
            }

        return None

    def _calculate_pnl(
        self,
        trade: Dict[str, Any],
        outcome: Dict[str, Any]
    ) -> float:
        """Calculate realized P&L for a resolved trade.

        Args:
            trade: The copy trade record
            outcome: The resolution outcome

        Returns:
            Realized P&L in USD
        """
        entry_price = trade.get("entry_price", 0)
        shares = trade.get("shares", 0)
        won = outcome.get("won", False)

        if won:
            # Won: receive $1 per share, paid entry_price per share
            pnl = (1.0 - entry_price) * shares
        else:
            # Lost: receive $0 per share, paid entry_price per share
            pnl = -entry_price * shares

        return pnl

    def get_pnl_report(self) -> Dict[str, Any]:
        """Generate a P&L report for all copy trades.

        Returns:
            Comprehensive P&L report
        """
        summary = self.db.get_copy_trade_summary()
        open_trades = self.db.get_open_copy_trades()
        resolved_trades = self.db.get_copy_trades(status="resolved", limit=50)

        # Calculate unrealized P&L estimate for open trades
        # Assume current market price is still entry price (conservative)
        open_value = sum(t.get("size_usd", 0) for t in open_trades)

        report = {
            "summary": summary,
            "open_trades_count": len(open_trades),
            "open_trades_value": open_value,
            "recent_resolved": [
                {
                    "market": t.get("market_question", "")[:50],
                    "outcome": t.get("outcome"),
                    "entry_price": t.get("entry_price"),
                    "resolved_outcome": t.get("resolved_outcome"),
                    "pnl": t.get("realized_pnl"),
                    "resolved_at": t.get("resolved_at"),
                }
                for t in resolved_trades[:10]
            ],
        }

        return report
