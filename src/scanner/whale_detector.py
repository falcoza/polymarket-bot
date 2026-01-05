"""Whale detection logic for identifying suspicious trading activity."""

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from config.settings import Settings
from src.scanner.activity_client import ActivityClient
from src.scanner.models import AlertType, WhaleAlert, WhaleTrade, WalletProfile

logger = logging.getLogger(__name__)


class WhaleDetector:
    """Detects whale and suspicious trading activity."""

    def __init__(self, settings: Settings, activity_client: ActivityClient):
        """Initialize whale detector.

        Args:
            settings: Application settings
            activity_client: Client for fetching trade data
        """
        self.settings = settings
        self.activity_client = activity_client

        # Detection thresholds
        self.min_bet_usd = getattr(settings, "whale_min_bet_usd", 10000)
        self.fresh_wallet_days = getattr(settings, "whale_fresh_wallet_days", 30)
        self.max_prior_trades = getattr(settings, "whale_max_prior_trades", 10)
        self.repeat_pattern_threshold = 3  # Trades in same category

        # Track patterns across scans
        self._wallet_category_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._alerted_trades: Set[str] = set()

    def scan_for_whales(self, limit: int = 100) -> List[WhaleAlert]:
        """Scan recent trades for whale activity.

        Args:
            limit: Number of recent trades to scan

        Returns:
            List of whale alerts
        """
        trades = self.activity_client.get_recent_trades(limit=limit)
        logger.info(f"Scanning {len(trades)} recent trades for whale activity")

        alerts = []
        for trade in trades:
            # Skip if already alerted
            if trade.id in self._alerted_trades:
                continue

            alert = self.analyze_trade(trade)
            if alert:
                alerts.append(alert)
                self._alerted_trades.add(trade.id)

        return alerts

    def analyze_trade(self, trade: WhaleTrade) -> Optional[WhaleAlert]:
        """Analyze a single trade for whale indicators.

        Args:
            trade: Trade to analyze

        Returns:
            WhaleAlert if suspicious, None otherwise
        """
        # Skip small trades
        if trade.value_usd < self.min_bet_usd:
            return None

        # Get wallet profile
        wallet = self.activity_client.get_wallet_profile(trade.wallet_address)

        # Check for alert conditions
        alert_types: List[AlertType] = []
        reasons: List[str] = []
        confidence = 0.5

        # 1. Large bet check
        if trade.value_usd >= self.min_bet_usd:
            alert_types.append(AlertType.LARGE_BET)
            reasons.append(f"Large bet: ${trade.value_usd:,.0f}")
            confidence += 0.1

        # 2. Fresh wallet check
        if wallet.is_fresh:
            alert_types.append(AlertType.FRESH_WALLET)
            reasons.append(f"Fresh wallet: {wallet.wallet_age_days} days old")
            confidence += 0.2

        # 3. New trader check (few prior trades)
        if wallet.is_new_trader:
            if AlertType.FRESH_WALLET not in alert_types:
                alert_types.append(AlertType.FRESH_WALLET)
            reasons.append(f"New trader: only {wallet.total_trades} prior trades")
            confidence += 0.15

        # 4. Repeat pattern check
        if trade.market_category:
            self._wallet_category_counts[trade.wallet_address][trade.market_category] += 1
            category_count = self._wallet_category_counts[trade.wallet_address][trade.market_category]

            if category_count >= self.repeat_pattern_threshold:
                alert_types.append(AlertType.REPEAT_PATTERN)
                reasons.append(f"Repeat pattern: {category_count} trades in {trade.market_category}")
                confidence += 0.15

        # 5. Liquidity grab check (if we have liquidity data)
        if trade.market_liquidity and trade.market_liquidity > 0:
            liquidity_pct = trade.value_usd / trade.market_liquidity
            if liquidity_pct > 0.05:  # Taking >5% of liquidity
                alert_types.append(AlertType.LIQUIDITY_GRAB)
                reasons.append(f"Liquidity grab: {liquidity_pct:.1%} of market liquidity")
                confidence += 0.2

        # Generate alert if we have at least one alert type
        if alert_types:
            # Higher confidence for fresh wallet + large bet combo
            if AlertType.FRESH_WALLET in alert_types and AlertType.LARGE_BET in alert_types:
                confidence = min(confidence + 0.2, 1.0)
                reasons.insert(0, "🔥 High signal: Fresh wallet making large bet")

            return WhaleAlert(
                trade=trade,
                wallet=wallet,
                alert_types=alert_types,
                confidence=min(confidence, 1.0),
                reasons=reasons,
            )

        return None

    def get_top_whales(self, hours: int = 24, min_volume: float = 50000) -> List[Dict]:
        """Get top whale wallets by volume in recent period.

        Args:
            hours: Lookback period in hours
            min_volume: Minimum volume to include

        Returns:
            List of whale wallet summaries
        """
        # This would require aggregating trades by wallet
        # For now, return empty - could be enhanced later
        return []

    def reset_patterns(self) -> None:
        """Reset pattern tracking (e.g., at start of new day)."""
        self._wallet_category_counts.clear()

    def reset_alerts(self) -> None:
        """Reset alerted trades (for new scan session)."""
        self._alerted_trades.clear()


def run_whale_scanner(
    settings: Settings,
    interval_seconds: int = 60,
    min_bet_usd: float = 10000,
) -> None:
    """Run continuous whale scanning loop.

    Args:
        settings: Application settings
        interval_seconds: Seconds between scans
        min_bet_usd: Minimum bet to trigger alert
    """
    import asyncio
    from rich.console import Console

    console = Console()

    # Override settings
    settings.whale_min_bet_usd = min_bet_usd

    # Initialize components
    activity_client = ActivityClient(settings)
    detector = WhaleDetector(settings, activity_client)

    console.print(f"[bold green]🐋 Whale Scanner Started[/bold green]")
    console.print(f"  Min bet: ${min_bet_usd:,.0f}")
    console.print(f"  Interval: {interval_seconds}s")
    console.print(f"  Press Ctrl+C to stop\n")

    iteration = 0
    total_alerts = 0

    try:
        while True:
            iteration += 1
            timestamp = datetime.now().strftime("%H:%M:%S")

            try:
                # Scan for whales
                alerts = detector.scan_for_whales(limit=100)

                if alerts:
                    total_alerts += len(alerts)
                    for alert in alerts:
                        console.print(alert.format_console())
                else:
                    console.print(f"[dim][{timestamp}] Scan #{iteration}: No whale activity detected[/dim]")

            except Exception as e:
                console.print(f"[red]Scan error: {e}[/red]")

            # Wait for next scan
            import time
            time.sleep(interval_seconds)

    except KeyboardInterrupt:
        console.print(f"\n[yellow]Scanner stopped. Total alerts: {total_alerts}[/yellow]")
    finally:
        activity_client.close()
