"""In-memory price history storage with periodic persistence."""

import logging
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from src.models.market import PriceSnapshot

logger = logging.getLogger(__name__)


class PriceHistoryStore:
    """In-memory price history storage with thread safety."""

    def __init__(self, max_history_hours: int = 168):  # 1 week default
        """Initialize price history store.

        Args:
            max_history_hours: Maximum hours of history to retain
        """
        self._history: Dict[str, List[PriceSnapshot]] = defaultdict(list)
        self._lock = threading.Lock()
        self.max_history = timedelta(hours=max_history_hours)

    def add_snapshot(self, snapshot: PriceSnapshot) -> None:
        """Add a price snapshot.

        Args:
            snapshot: Price snapshot to add
        """
        with self._lock:
            self._history[snapshot.token_id].append(snapshot)
            self._cleanup_old_data(snapshot.token_id)

    def add_snapshots(self, snapshots: List[PriceSnapshot]) -> None:
        """Add multiple price snapshots.

        Args:
            snapshots: List of snapshots to add
        """
        with self._lock:
            for snapshot in snapshots:
                self._history[snapshot.token_id].append(snapshot)

            # Cleanup for each token
            token_ids = set(s.token_id for s in snapshots)
            for token_id in token_ids:
                self._cleanup_old_data(token_id)

    def get_history(
        self,
        token_id: str,
        hours: int = 24,
    ) -> List[PriceSnapshot]:
        """Get price history for a token.

        Args:
            token_id: Token identifier
            hours: Number of hours of history to return

        Returns:
            List of price snapshots, oldest first
        """
        cutoff = datetime.utcnow() - timedelta(hours=hours)

        with self._lock:
            history = [
                s for s in self._history.get(token_id, [])
                if s.timestamp >= cutoff
            ]
            return sorted(history, key=lambda x: x.timestamp)

    def get_latest(self, token_id: str) -> Optional[PriceSnapshot]:
        """Get most recent snapshot for a token.

        Args:
            token_id: Token identifier

        Returns:
            Latest snapshot or None
        """
        with self._lock:
            history = self._history.get(token_id, [])
            if not history:
                return None
            return max(history, key=lambda x: x.timestamp)

    def get_prices(self, token_id: str, hours: int = 24) -> List[float]:
        """Get just the prices for a token.

        Args:
            token_id: Token identifier
            hours: Number of hours of history

        Returns:
            List of mid prices, oldest first
        """
        history = self.get_history(token_id, hours)
        return [h.mid_price for h in history]

    def get_volumes(self, token_id: str, hours: int = 24) -> List[float]:
        """Get volumes for a token.

        Args:
            token_id: Token identifier
            hours: Number of hours of history

        Returns:
            List of volumes, oldest first
        """
        history = self.get_history(token_id, hours)
        return [h.volume or 0 for h in history]

    def has_sufficient_data(
        self,
        token_id: str,
        min_points: int,
        hours: int = 24,
    ) -> bool:
        """Check if we have sufficient data for analysis.

        Args:
            token_id: Token identifier
            min_points: Minimum number of data points needed
            hours: Time window to check

        Returns:
            True if sufficient data exists
        """
        history = self.get_history(token_id, hours)
        return len(history) >= min_points

    def get_price_change(
        self,
        token_id: str,
        hours: int = 24,
    ) -> Optional[float]:
        """Calculate price change over a period.

        Args:
            token_id: Token identifier
            hours: Time period in hours

        Returns:
            Percentage change or None if insufficient data
        """
        prices = self.get_prices(token_id, hours)
        if len(prices) < 2:
            return None

        first_price = prices[0]
        last_price = prices[-1]

        if first_price <= 0:
            return None

        return (last_price - first_price) / first_price

    def _cleanup_old_data(self, token_id: str) -> None:
        """Remove data older than max_history.

        Args:
            token_id: Token to cleanup
        """
        cutoff = datetime.utcnow() - self.max_history
        self._history[token_id] = [
            s for s in self._history[token_id]
            if s.timestamp >= cutoff
        ]

    def clear(self, token_id: Optional[str] = None) -> None:
        """Clear price history.

        Args:
            token_id: Specific token to clear, or None for all
        """
        with self._lock:
            if token_id:
                self._history.pop(token_id, None)
            else:
                self._history.clear()

    @property
    def token_count(self) -> int:
        """Get number of tokens with history."""
        with self._lock:
            return len(self._history)

    @property
    def total_snapshots(self) -> int:
        """Get total number of snapshots stored."""
        with self._lock:
            return sum(len(h) for h in self._history.values())
