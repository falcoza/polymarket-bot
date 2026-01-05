"""Client for Polymarket Data API - trade activity and profiles."""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

import httpx

from config.settings import Settings
from src.scanner.models import WalletProfile, WhaleTrade

logger = logging.getLogger(__name__)


class ActivityClient:
    """Client for Polymarket Data API to fetch trade activity."""

    def __init__(self, settings: Settings):
        """Initialize the activity client."""
        self.base_url = "https://data-api.polymarket.com"
        self._http_client = httpx.Client(timeout=30.0)
        self.settings = settings

        # Cache for wallet profiles to reduce API calls
        self._wallet_cache: Dict[str, WalletProfile] = {}
        self._seen_trades: Set[str] = set()

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        """Make GET request to Data API."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self._http_client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching {url}: {e}")
            return []
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            return []

    def get_recent_trades(self, limit: int = 100) -> List[WhaleTrade]:
        """Fetch recent trades across all markets.

        Args:
            limit: Maximum number of trades to fetch

        Returns:
            List of WhaleTrade objects
        """
        raw_trades = self._get("/trades", {"limit": limit})

        trades = []
        for raw in raw_trades:
            try:
                trade = self._parse_trade(raw)
                # Skip if we've already seen this trade
                if trade.id in self._seen_trades:
                    continue
                self._seen_trades.add(trade.id)
                trades.append(trade)
            except Exception as e:
                logger.warning(f"Failed to parse trade: {e}")

        return trades

    def get_wallet_activity(
        self,
        address: str,
        limit: int = 100,
    ) -> List[WhaleTrade]:
        """Fetch trade history for a specific wallet.

        Args:
            address: Wallet address
            limit: Maximum trades to fetch

        Returns:
            List of trades for this wallet
        """
        raw_activity = self._get("/activity", {"user": address, "limit": limit})

        trades = []
        for raw in raw_activity:
            if raw.get("type") == "TRADE":
                try:
                    trade = self._parse_trade(raw)
                    trades.append(trade)
                except Exception as e:
                    logger.warning(f"Failed to parse wallet trade: {e}")

        return trades

    def get_wallet_profile(self, address: str) -> WalletProfile:
        """Get profile information for a wallet.

        Args:
            address: Wallet address

        Returns:
            WalletProfile with activity stats
        """
        # Check cache first
        if address in self._wallet_cache:
            return self._wallet_cache[address]

        # Fetch activity history
        activity = self._get("/activity", {"user": address, "limit": 500})

        if not activity:
            profile = WalletProfile(address=address)
            self._wallet_cache[address] = profile
            return profile

        # Calculate stats from activity
        trades = [a for a in activity if a.get("type") == "TRADE"]

        total_trades = len(trades)
        total_volume = sum(float(t.get("usdcSize", 0) or 0) for t in trades)

        # Get timestamps
        timestamps = [t.get("timestamp", 0) for t in trades if t.get("timestamp")]
        first_trade = None
        last_trade = None
        if timestamps:
            first_trade = datetime.fromtimestamp(min(timestamps))
            last_trade = datetime.fromtimestamp(max(timestamps))

        # Count unique markets
        unique_markets = len(set(t.get("conditionId", "") for t in trades))

        profile = WalletProfile(
            address=address,
            total_trades=total_trades,
            total_volume_usd=total_volume,
            first_trade_date=first_trade,
            last_trade_date=last_trade,
            markets_traded=unique_markets,
        )

        self._wallet_cache[address] = profile
        return profile

    def _parse_trade(self, raw: Dict[str, Any]) -> WhaleTrade:
        """Parse raw API response into WhaleTrade model."""
        # Calculate USD value
        size = float(raw.get("size", 0) or 0)
        price = float(raw.get("price", 0) or 0)
        usdc_size = float(raw.get("usdcSize", 0) or 0)

        # Use usdcSize if available, otherwise calculate
        value_usd = usdc_size if usdc_size > 0 else size * price

        # Parse timestamp
        timestamp = datetime.fromtimestamp(raw.get("timestamp", 0))

        return WhaleTrade(
            id=raw.get("transactionHash", ""),
            wallet_address=raw.get("proxyWallet", ""),
            market_id=raw.get("conditionId", ""),
            market_question=raw.get("title", "Unknown Market"),
            market_slug=raw.get("slug", ""),
            side=raw.get("side", "BUY"),
            outcome=raw.get("outcome", "Unknown"),
            price=price,
            size=size,
            value_usd=value_usd,
            timestamp=timestamp,
            market_category=raw.get("eventSlug", "").split("-")[0] if raw.get("eventSlug") else None,
        )

    def clear_cache(self) -> None:
        """Clear the wallet profile cache."""
        self._wallet_cache.clear()

    def clear_seen_trades(self) -> None:
        """Clear the seen trades set (for new scan session)."""
        self._seen_trades.clear()

    def close(self) -> None:
        """Close the HTTP client."""
        self._http_client.close()
