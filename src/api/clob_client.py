"""Wrapper around py-clob-client for Polymarket trading."""

import logging
from typing import Any, Dict, List, Optional

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    MarketOrderArgs,
    OpenOrderParams,
    OrderArgs,
    OrderType as ClobOrderType,
)
from py_clob_client.order_builder.constants import BUY, SELL

from config.settings import Settings
from src.models.order import Order, OrderSide, OrderType

logger = logging.getLogger(__name__)


class PolymarketClobClient:
    """Wrapper around py-clob-client with convenience methods."""

    def __init__(self, settings: Settings):
        """Initialize the CLOB client wrapper."""
        self.settings = settings
        self._client: Optional[ClobClient] = None
        self._connected = False

    def connect(self) -> None:
        """Initialize and authenticate the CLOB client."""
        if not self.settings.polygon_wallet_private_key:
            raise ValueError("POLYGON_WALLET_PRIVATE_KEY is required")

        self._client = ClobClient(
            host=self.settings.clob_host,
            key=self.settings.polygon_wallet_private_key,
            chain_id=self.settings.chain_id,
            signature_type=self.settings.signature_type.value,
            funder=self.settings.funder_address,
        )

        # Derive/create API credentials
        try:
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            self._connected = True
            logger.info("CLOB client connected and authenticated")
        except Exception as e:
            logger.error(f"Failed to authenticate with CLOB: {e}")
            raise

    @property
    def client(self) -> ClobClient:
        """Get the underlying CLOB client."""
        if self._client is None:
            raise RuntimeError("Client not connected. Call connect() first.")
        return self._client

    @property
    def is_connected(self) -> bool:
        """Check if client is connected."""
        return self._connected

    # Market Data Methods
    def get_midpoint(self, token_id: str) -> float:
        """Get mid-market price for a token."""
        try:
            result = self.client.get_midpoint(token_id)
            return float(result)
        except Exception as e:
            logger.error(f"Failed to get midpoint for {token_id}: {e}")
            return 0.0

    def get_price(self, token_id: str, side: str) -> float:
        """Get best price for a side (BUY/SELL)."""
        try:
            result = self.client.get_price(token_id, side)
            return float(result)
        except Exception as e:
            logger.error(f"Failed to get price for {token_id}: {e}")
            return 0.0

    def get_order_book(self, token_id: str) -> Dict[str, Any]:
        """Get full order book for a token."""
        try:
            return self.client.get_order_book(token_id)
        except Exception as e:
            logger.error(f"Failed to get order book for {token_id}: {e}")
            return {"bids": [], "asks": []}

    def get_spread(self, token_id: str) -> Dict[str, float]:
        """Get bid-ask spread for a token."""
        book = self.get_order_book(token_id)
        best_bid = float(book["bids"][0]["price"]) if book.get("bids") else 0
        best_ask = float(book["asks"][0]["price"]) if book.get("asks") else 1

        return {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": best_ask - best_bid,
            "mid": (best_bid + best_ask) / 2,
        }

    # Order Methods
    def create_limit_order(
        self,
        token_id: str,
        side: OrderSide,
        price: float,
        size: float,
    ) -> Dict[str, Any]:
        """Create and sign a limit order."""
        clob_side = BUY if side == OrderSide.BUY else SELL

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=clob_side,
        )

        signed_order = self.client.create_order(order_args)
        return signed_order

    def create_market_order(
        self,
        token_id: str,
        side: OrderSide,
        amount_usd: float,
    ) -> Dict[str, Any]:
        """Create and sign a market order (FOK)."""
        clob_side = BUY if side == OrderSide.BUY else SELL

        market_order_args = MarketOrderArgs(
            token_id=token_id,
            amount=amount_usd,
            side=clob_side,
        )

        signed_order = self.client.create_market_order(market_order_args)
        return signed_order

    def submit_order(
        self,
        signed_order: Dict[str, Any],
        order_type: OrderType,
    ) -> Dict[str, Any]:
        """Submit a signed order to the exchange."""
        clob_type = ClobOrderType.GTC if order_type == OrderType.GTC else ClobOrderType.FOK
        try:
            response = self.client.post_order(signed_order, clob_type)
            logger.info(f"Order submitted: {response}")
            return response
        except Exception as e:
            logger.error(f"Failed to submit order: {e}")
            raise

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a specific order."""
        try:
            return self.client.cancel(order_id)
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            raise

    def cancel_all_orders(self) -> Dict[str, Any]:
        """Cancel all open orders."""
        try:
            return self.client.cancel_all()
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            raise

    def get_open_orders(self) -> List[Dict[str, Any]]:
        """Get all open orders."""
        try:
            return self.client.get_orders(OpenOrderParams())
        except Exception as e:
            logger.error(f"Failed to get open orders: {e}")
            return []

    def get_trades(self) -> List[Dict[str, Any]]:
        """Get user trade history."""
        try:
            return self.client.get_trades()
        except Exception as e:
            logger.error(f"Failed to get trades: {e}")
            return []

    def get_balances(self) -> Dict[str, Any]:
        """Get account balances."""
        try:
            return self.client.get_balance_allowance()
        except Exception as e:
            logger.error(f"Failed to get balances: {e}")
            return {}
