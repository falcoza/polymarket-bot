"""Client for Polymarket Gamma API - market metadata."""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from config.settings import Settings
from src.models.market import Market, PriceSnapshot

logger = logging.getLogger(__name__)


class GammaMarketClient:
    """Client for Polymarket Gamma API."""

    def __init__(self, settings: Settings):
        """Initialize the Gamma client."""
        self.base_url = settings.gamma_host
        self._http_client = httpx.Client(timeout=30.0)

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Any:
        """Make GET request to Gamma API."""
        url = f"{self.base_url}{endpoint}"
        try:
            response = self._http_client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching {url}: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching {url}: {e}")
            raise

    def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool = False,
        order: str = "volume24hr",
        ascending: bool = False,
    ) -> List[Dict[str, Any]]:
        """Fetch markets with filters."""
        params = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
            "order": order,
            "ascending": str(ascending).lower(),
        }
        return self._get("/markets", params)

    def get_market_by_id(self, market_id: str) -> Optional[Dict[str, Any]]:
        """Fetch single market by ID."""
        try:
            return self._get(f"/markets/{market_id}")
        except Exception:
            return None

    def get_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """Fetch single market by slug."""
        try:
            markets = self._get("/markets", {"slug": slug})
            return markets[0] if markets else None
        except Exception:
            return None

    def get_tradable_markets(
        self,
        min_liquidity: float = 1000.0,
        min_volume_24hr: float = 100.0,
        limit: int = 50,
    ) -> List[Market]:
        """Get markets suitable for trading."""
        raw_markets = self.get_markets(
            limit=limit,
            active=True,
            closed=False,
            order="volume24hr",
        )

        markets = []
        for raw in raw_markets:
            # Filter by liquidity and volume
            liquidity = float(raw.get("liquidityNum", 0) or 0)
            volume_24hr = float(raw.get("volume24hr", 0) or 0)

            if liquidity < min_liquidity or volume_24hr < min_volume_24hr:
                continue

            # Parse into Market model
            try:
                market = self._parse_market(raw)
                if market.is_tradeable:
                    markets.append(market)
            except Exception as e:
                logger.warning(f"Failed to parse market {raw.get('id')}: {e}")

        return markets

    def _parse_market(self, raw: Dict[str, Any]) -> Market:
        """Parse raw API response into Market model."""
        # Parse string arrays that come from API
        clob_token_ids = raw.get("clobTokenIds", [])
        if isinstance(clob_token_ids, str):
            try:
                clob_token_ids = json.loads(clob_token_ids)
            except json.JSONDecodeError:
                clob_token_ids = []

        outcome_prices = raw.get("outcomePrices", [])
        if isinstance(outcome_prices, str):
            try:
                outcome_prices = [float(p) for p in json.loads(outcome_prices)]
            except (json.JSONDecodeError, ValueError):
                outcome_prices = [0.5, 0.5]
        else:
            try:
                outcome_prices = [float(p) for p in outcome_prices]
            except (TypeError, ValueError):
                outcome_prices = [0.5, 0.5]

        outcomes = raw.get("outcomes", ["Yes", "No"])
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = ["Yes", "No"]

        # Parse end date
        end_date = None
        if raw.get("endDate"):
            try:
                end_date_str = raw["endDate"]
                if end_date_str.endswith("Z"):
                    end_date_str = end_date_str[:-1] + "+00:00"
                end_date = datetime.fromisoformat(end_date_str)
            except (ValueError, TypeError):
                pass

        return Market(
            id=raw.get("id", ""),
            question=raw.get("question", ""),
            condition_id=raw.get("conditionId", ""),
            slug=raw.get("slug", ""),
            description=raw.get("description"),
            clob_token_ids=clob_token_ids,
            outcomes=outcomes,
            outcome_prices=outcome_prices,
            liquidity=float(raw.get("liquidityNum", 0) or 0),
            volume=float(raw.get("volumeNum", 0) or 0),
            volume_24hr=float(raw.get("volume24hr", 0) or 0),
            volume_1wk=float(raw.get("volume1wk", 0) or 0) if raw.get("volume1wk") else None,
            best_bid=float(raw["bestBid"]) if raw.get("bestBid") else None,
            best_ask=float(raw["bestAsk"]) if raw.get("bestAsk") else None,
            spread=float(raw["spread"]) if raw.get("spread") else None,
            active=raw.get("active", False),
            closed=raw.get("closed", False),
            end_date=end_date,
            category=raw.get("category"),
            image=raw.get("image"),
        )

    def get_events(self, limit: int = 50, active: bool = True) -> List[Dict]:
        """Fetch events (groups of related markets)."""
        params = {
            "limit": limit,
            "active": str(active).lower(),
        }
        return self._get("/events", params)

    def get_price_snapshot(self, market: Market) -> PriceSnapshot:
        """Get current price snapshot for a market."""
        return PriceSnapshot(
            market_id=market.id,
            token_id=market.yes_token_id,
            mid_price=market.yes_price,
            best_bid=market.best_bid,
            best_ask=market.best_ask,
            volume=market.volume_24hr,
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._http_client.close()
