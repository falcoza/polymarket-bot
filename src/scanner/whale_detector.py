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

    # Memory limits to prevent OOM on Railway
    MAX_ALERTED_TRADES = 5000
    MAX_WALLET_ENTRIES = 500
    MAX_ALERT_TIMES = 1000

    def __init__(self, settings: Settings, activity_client: ActivityClient):
        """Initialize whale detector.

        Args:
            settings: Application settings
            activity_client: Client for fetching trade data
        """
        self.settings = settings
        self.activity_client = activity_client

        # Detection thresholds - stricter criteria for insider detection
        self.min_bet_usd = getattr(settings, "whale_min_bet_usd", 2000)  # Floor threshold
        self.fresh_wallet_days = getattr(settings, "whale_fresh_wallet_days", 7)  # 7 days = fresh
        self.ultra_fresh_hours = 24  # < 1 day = ultra fresh (highest signal)
        self.max_markets_traded = 3  # < 3 unique markets = suspicious
        self.max_trade_age_minutes = getattr(settings, "whale_max_trade_age_minutes", 5)  # 5 MINUTES not hours!
        self.repeat_pattern_threshold = 3  # Trades in same category

        # Resolution timing thresholds
        self.imminent_resolution_hours = 6  # < 6 hours = imminent
        self.soon_resolution_hours = 24  # < 24 hours = soon
        self.near_resolution_hours = 72  # < 72 hours = near
        self.min_resolution_hours = 0.5  # Skip if < 30 min to resolution (too risky)

        # Contrarian threshold
        self.contrarian_threshold = 0.70  # 70% consensus = contrarian bet

        # Bot detection thresholds
        self.bot_rapid_trade_seconds = 30  # Trades within 30 seconds = bot pattern

        # PRICE FILTERS - Critical for ROI
        self.max_entry_price = getattr(settings, "whale_max_entry_price", 0.85)  # Skip prices above this
        self.min_entry_price = getattr(settings, "whale_min_entry_price", 0.15)  # Skip prices below this
        # R:R minimum lowered to 0.20 to not conflict with price filter (0.15-0.85 range)
        self.min_risk_reward = getattr(settings, "whale_min_risk_reward", 0.20)  # Min 1:5 R:R ratio

        # CONFIDENCE THRESHOLDS
        self.min_confidence = getattr(settings, "whale_min_confidence", 0.65)  # Raised from ~0.50

        # HIGH-EDGE CATEGORIES
        self.high_edge_categories = {"crypto", "sports", "finance", "economics"}

        # Track patterns across scans (with memory limits)
        self._wallet_category_counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._alerted_trades: Set[str] = set()

        # Track wallet trade timestamps for bot detection
        self._wallet_trade_times: Dict[str, List[datetime]] = defaultdict(list)

        # RATE LIMITING - prevent copying same wallet repeatedly
        self._wallet_alert_times: Dict[str, datetime] = {}
        self.wallet_rate_limit_seconds = 3600  # 1 hour between alerts for same wallet

        # Daily reset tracking
        self._last_daily_reset = datetime.utcnow()

    def _check_daily_reset(self) -> None:
        """Reset caches daily to prevent memory leaks on Railway."""
        now = datetime.utcnow()
        hours_since_reset = (now - self._last_daily_reset).total_seconds() / 3600

        if hours_since_reset >= 24:
            logger.info("Performing daily reset of whale detector caches")
            self.reset_patterns()
            self.reset_alerts()
            self._wallet_alert_times.clear()
            self._last_daily_reset = now

    def _enforce_memory_limits(self) -> None:
        """Enforce memory limits on all data structures."""
        # Limit alerted trades
        if len(self._alerted_trades) > self.MAX_ALERTED_TRADES:
            logger.info(f"Clearing alerted trades (exceeded {self.MAX_ALERTED_TRADES})")
            self._alerted_trades.clear()

        # Limit wallet category counts
        if len(self._wallet_category_counts) > self.MAX_WALLET_ENTRIES:
            logger.info(f"Pruning wallet category counts to {self.MAX_WALLET_ENTRIES}")
            # Keep only most recent entries
            wallets = list(self._wallet_category_counts.keys())
            for wallet in wallets[:-self.MAX_WALLET_ENTRIES]:
                del self._wallet_category_counts[wallet]

        # Limit wallet trade times
        if len(self._wallet_trade_times) > self.MAX_WALLET_ENTRIES:
            logger.info(f"Pruning wallet trade times to {self.MAX_WALLET_ENTRIES}")
            wallets = list(self._wallet_trade_times.keys())
            for wallet in wallets[:-self.MAX_WALLET_ENTRIES]:
                del self._wallet_trade_times[wallet]

        # Limit wallet alert times
        if len(self._wallet_alert_times) > self.MAX_ALERT_TIMES:
            logger.info(f"Pruning wallet alert times to {self.MAX_ALERT_TIMES}")
            # Remove oldest entries
            sorted_wallets = sorted(
                self._wallet_alert_times.items(),
                key=lambda x: x[1]
            )
            for wallet, _ in sorted_wallets[:-self.MAX_ALERT_TIMES]:
                del self._wallet_alert_times[wallet]

    def get_memory_stats(self) -> Dict[str, int]:
        """Get current memory usage stats for monitoring."""
        return {
            "alerted_trades": len(self._alerted_trades),
            "wallet_category_counts": len(self._wallet_category_counts),
            "wallet_trade_times": len(self._wallet_trade_times),
            "wallet_alert_times": len(self._wallet_alert_times),
        }

    def scan_for_whales(self, limit: int = 100) -> List[WhaleAlert]:
        """Scan recent trades for whale activity.

        Args:
            limit: Number of recent trades to scan

        Returns:
            List of whale alerts
        """
        # Check for daily reset and enforce memory limits
        self._check_daily_reset()
        self._enforce_memory_limits()

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

    def _detect_bot_pattern(self, wallet_address: str, trade_time: datetime) -> bool:
        """Detect if wallet shows bot-like trading patterns.

        Args:
            wallet_address: Wallet address
            trade_time: Current trade timestamp

        Returns:
            True if bot pattern detected
        """
        # Add current trade time
        self._wallet_trade_times[wallet_address].append(trade_time)

        # Keep only last 10 trades
        times = self._wallet_trade_times[wallet_address][-10:]
        self._wallet_trade_times[wallet_address] = times

        # Check for rapid-fire trades (< 30 seconds apart)
        if len(times) >= 2:
            for i in range(1, len(times)):
                time_diff = abs((times[i] - times[i - 1]).total_seconds())
                if time_diff < self.bot_rapid_trade_seconds:
                    return True

        return False

    def _calculate_risk_reward(self, entry_price: float) -> float:
        """Calculate risk/reward ratio for a position.

        For a YES position: risk is losing entry_price, reward is (1 - entry_price)

        Args:
            entry_price: The entry price

        Returns:
            Risk/reward ratio (higher is better)
        """
        if entry_price <= 0 or entry_price >= 1:
            return 0
        risk = entry_price
        reward = 1.0 - entry_price
        return reward / risk

    def _check_wallet_rate_limit(self, wallet_address: str) -> bool:
        """Check if wallet is rate-limited.

        Args:
            wallet_address: Wallet to check

        Returns:
            True if should skip (rate limited), False if OK to proceed
        """
        last_alert = self._wallet_alert_times.get(wallet_address)
        if last_alert:
            seconds_since = (datetime.utcnow() - last_alert).total_seconds()
            if seconds_since < self.wallet_rate_limit_seconds:
                return True  # Rate limited
        return False

    def _record_wallet_alert(self, wallet_address: str) -> None:
        """Record that we alerted on this wallet."""
        self._wallet_alert_times[wallet_address] = datetime.utcnow()

    def analyze_trade(self, trade: WhaleTrade) -> Optional[WhaleAlert]:
        """Analyze a single trade for whale indicators.

        Detection signals:
        - Ultra-fresh wallet (< 1 day) = highest signal
        - Low market count (< 3 markets) = suspicious
        - Quick to trade after wallet creation (wc/tx < 20%)
        - Trade recency (< 5 minutes - CHANGED from hours)
        - Imminent resolution (< 24 hours) = strong insider signal
        - Contrarian bet (against 70%+ consensus) = insider signal
        - Bot pattern detection = reduces confidence

        NEW FILTERS for ROI improvement:
        - Price extremes filter (skip 0.85+ or 0.15-)
        - Risk/reward filter (minimum 1:3)
        - Resolution timing (skip if < 30 min to resolution)
        - Wallet rate limiting (1 alert per wallet per hour)
        - Dynamic bet threshold (% of market liquidity)

        Args:
            trade: Trade to analyze

        Returns:
            WhaleAlert if suspicious, None otherwise
        """
        # ===== NEW FILTER 1: EXTREME PRICE FILTER =====
        # This is the #1 cause of losses - tiny upside, massive downside
        if trade.price > self.max_entry_price:
            logger.debug(f"Skipping trade: price {trade.price:.2f} > max {self.max_entry_price}")
            return None
        if trade.price < self.min_entry_price:
            logger.debug(f"Skipping trade: price {trade.price:.2f} < min {self.min_entry_price}")
            return None

        # ===== NEW FILTER 2: RISK/REWARD FILTER =====
        rr_ratio = self._calculate_risk_reward(trade.price)
        if rr_ratio < self.min_risk_reward:
            logger.debug(f"Skipping trade: R:R ratio {rr_ratio:.2f} < min {self.min_risk_reward}")
            return None

        # ===== NEW FILTER 3: RESOLUTION TIMING FILTER =====
        # Skip markets about to resolve - too risky, no time to react
        hours_until = trade.hours_until_resolution
        if hours_until is not None and hours_until < self.min_resolution_hours:
            logger.debug(f"Skipping trade: only {hours_until:.2f}h until resolution")
            return None

        # ===== NEW FILTER 4: DYNAMIC BET THRESHOLD =====
        # Use % of market liquidity, not just fixed amount
        min_bet = self.min_bet_usd
        if trade.market_liquidity and trade.market_liquidity > 0:
            liquidity_based_min = trade.market_liquidity * 0.02  # 2% of market
            min_bet = max(self.min_bet_usd, liquidity_based_min)

        if trade.value_usd < min_bet:
            logger.debug(f"Skipping trade: ${trade.value_usd:.0f} < min ${min_bet:.0f}")
            return None

        # ===== NEW FILTER 5: TRADE STALENESS (5 MINUTES not hours) =====
        trade_age_minutes = (datetime.utcnow() - trade.timestamp).total_seconds() / 60
        if trade_age_minutes > self.max_trade_age_minutes:
            logger.debug(f"Skipping trade: {trade_age_minutes:.1f} min old > max {self.max_trade_age_minutes}")
            return None

        # ===== NEW FILTER 6: WALLET RATE LIMITING =====
        if self._check_wallet_rate_limit(trade.wallet_address):
            logger.debug(f"Skipping trade: wallet rate limited")
            return None

        # Get wallet profile
        wallet = self.activity_client.get_wallet_profile(trade.wallet_address)

        # Check for bot pattern (reduces confidence)
        is_bot = self._detect_bot_pattern(trade.wallet_address, trade.timestamp)

        # Check for alert conditions with stricter criteria
        alert_types: List[AlertType] = []
        reasons: List[str] = []
        confidence = 0.3  # Start lower, build up with signals

        # 1. ULTRA FRESH WALLET CHECK (< 1 day) - HIGHEST SIGNAL
        if wallet.is_ultra_fresh:
            alert_types.append(AlertType.FRESH_WALLET)
            reasons.append(f"🔥 Ultra-fresh wallet: {wallet.wallet_age_hours:.1f} hours old")
            confidence += 0.35  # Huge boost

        # 2. Fresh wallet check (< 7 days)
        elif wallet.is_fresh:
            alert_types.append(AlertType.FRESH_WALLET)
            reasons.append(f"Fresh wallet: {wallet.wallet_age_days} days old")
            confidence += 0.2

        # 3. Quick to trade after wallet creation (wc/tx ratio < 20%)
        if wallet.is_quick_to_trade:
            if AlertType.FRESH_WALLET not in alert_types:
                alert_types.append(AlertType.FRESH_WALLET)
            reasons.append(f"Quick to trade: wc/tx ratio {wallet.wc_tx_ratio:.0%}")
            confidence += 0.25

        # 4. Low market count (< 3 unique markets) - suspicious focus
        if wallet.is_low_market_count:
            if AlertType.FRESH_WALLET not in alert_types:
                alert_types.append(AlertType.FRESH_WALLET)
            reasons.append(f"Low market diversity: only {wallet.markets_traded} markets traded")
            confidence += 0.2

        # 5. IMMINENT RESOLUTION CHECK - VERY STRONG INSIDER SIGNAL
        hours_until = trade.hours_until_resolution
        if hours_until is not None:
            if hours_until < self.imminent_resolution_hours:
                alert_types.append(AlertType.IMMINENT_RESOLUTION)
                reasons.append(f"🚨 IMMINENT: Market resolves in {hours_until:.1f} hours!")
                confidence += 0.25  # Huge boost for imminent resolution
            elif hours_until < self.soon_resolution_hours:
                alert_types.append(AlertType.IMMINENT_RESOLUTION)
                reasons.append(f"⚠️ SOON: Market resolves in {hours_until:.0f} hours")
                confidence += 0.15
            elif hours_until < self.near_resolution_hours:
                reasons.append(f"Near resolution: {hours_until:.0f} hours")
                confidence += 0.05

        # 6. CONTRARIAN BET CHECK - BETTING AGAINST CONSENSUS
        if trade.is_contrarian_bet:
            alert_types.append(AlertType.CONTRARIAN_BET)
            yes_pct = (trade.market_yes_price or 0) * 100
            if trade.outcome.upper() == "NO":
                reasons.append(f"🔄 CONTRARIAN: Buying NO against {yes_pct:.0f}% YES consensus")
            else:
                reasons.append(f"🔄 CONTRARIAN: Buying YES at only {yes_pct:.0f}%")
            confidence += 0.15

        # 7. Large bet check (still relevant but less weight)
        if trade.value_usd >= 10000:  # Higher threshold for "large bet" label
            alert_types.append(AlertType.LARGE_BET)
            reasons.append(f"Large bet: ${trade.value_usd:,.0f}")
            confidence += 0.1
        elif trade.value_usd >= 5000:
            reasons.append(f"Significant bet: ${trade.value_usd:,.0f}")
            confidence += 0.05

        # 8. Repeat pattern check
        if trade.market_category:
            self._wallet_category_counts[trade.wallet_address][trade.market_category] += 1
            category_count = self._wallet_category_counts[trade.wallet_address][trade.market_category]

            if category_count >= self.repeat_pattern_threshold:
                alert_types.append(AlertType.REPEAT_PATTERN)
                reasons.append(f"Repeat pattern: {category_count} trades in {trade.market_category}")
                confidence += 0.15

        # 9. Liquidity grab check (if we have liquidity data)
        if trade.market_liquidity and trade.market_liquidity > 0:
            liquidity_pct = trade.value_usd / trade.market_liquidity
            if liquidity_pct > 0.05:  # Taking >5% of liquidity
                alert_types.append(AlertType.LIQUIDITY_GRAB)
                reasons.append(f"Liquidity grab: {liquidity_pct:.1%} of market liquidity")
                confidence += 0.15

        # 10. BOT PATTERN CHECK - REDUCES CONFIDENCE
        if is_bot:
            confidence -= 0.30  # Significant penalty for bot-like behavior
            reasons.append("⚠️ Bot pattern detected: rapid-fire trades")

        # 11. NEW MARKET CHECK - REDUCES CONFIDENCE
        market_age = trade.market_age_hours
        if market_age is not None and market_age < 24:
            confidence -= 0.10  # New market is less suspicious
            reasons.append(f"New market: only {market_age:.0f} hours old")

        # REQUIRE fresh wallet signal for alert (stricter criteria)
        # We only care about fresh wallets - that's the insider signal
        if AlertType.FRESH_WALLET not in alert_types:
            return None

        # Boost confidence for ultra-fresh + low market count combo
        if wallet.is_ultra_fresh and wallet.is_low_market_count:
            confidence = min(confidence + 0.15, 1.0)
            reasons.insert(0, "⚠️ HIGH SIGNAL: Ultra-fresh wallet with narrow focus")

        # Boost for quick-to-trade + ultra-fresh combo
        if wallet.is_quick_to_trade and wallet.is_ultra_fresh:
            confidence = min(confidence + 0.1, 1.0)
            reasons.insert(0, "🚨 VERY HIGH SIGNAL: Wallet created and trading immediately")

        # MEGA BOOST for ultra-fresh + imminent resolution + contrarian
        if (wallet.is_ultra_fresh and
            AlertType.IMMINENT_RESOLUTION in alert_types and
            AlertType.CONTRARIAN_BET in alert_types):
            confidence = min(confidence + 0.2, 1.0)
            reasons.insert(0, "💎 EXTREMELY HIGH SIGNAL: Fresh wallet + imminent resolution + contrarian bet")

        # NOTE: Time decay removed - redundant with 5-minute staleness filter
        # Trades > 5 min old are already filtered out at the top of this method

        # ===== NEW: CATEGORY CONFIDENCE PENALTY =====
        # Low-edge categories get penalized
        if trade.market_category and trade.market_category.lower() not in self.high_edge_categories:
            confidence *= 0.7  # 30% penalty for unclear categories
            reasons.append(f"Category penalty: {trade.market_category} not in high-edge list")

        # Ensure confidence stays in valid range
        confidence = max(0.1, min(confidence, 1.0))

        # ===== NEW: MINIMUM CONFIDENCE CHECK =====
        if confidence < self.min_confidence:
            logger.debug(f"Skipping trade: confidence {confidence:.2f} < min {self.min_confidence}")
            return None

        # Record that we alerted on this wallet (for rate limiting)
        self._record_wallet_alert(trade.wallet_address)

        return WhaleAlert(
            trade=trade,
            wallet=wallet,
            alert_types=alert_types,
            confidence=confidence,
            reasons=reasons,
            signals=[r for r in reasons if not r.startswith("Time decay") and not r.startswith("Category penalty")],
        )

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
        self._wallet_trade_times.clear()

    def reset_alerts(self) -> None:
        """Reset alerted trades (for new scan session)."""
        self._alerted_trades.clear()


def run_whale_scanner(
    settings: Settings,
    interval_seconds: int = 300,  # 5 minutes default (was 60s) - prevents API blocking
    min_bet_usd: float = 10000,
) -> None:
    """Run continuous whale scanning loop.

    Args:
        settings: Application settings
        interval_seconds: Seconds between scans (default 300 = 5 min to prevent blocking)
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

    console.print(f"[bold green]🐋 Smart Money Scanner v3.1 (Memory Safe + ROI Optimized)[/bold green]")
    console.print(f"[bold]Detection Signals:[/bold]")
    console.print(f"  [cyan]Wallet Analysis:[/cyan]")
    console.print(f"    • Ultra-fresh: < 24 hours old (+35%)")
    console.print(f"    • Fresh: < 7 days old (+20%)")
    console.print(f"    • Low market count: < 3 markets (+20%)")
    console.print(f"  [cyan]Market Context:[/cyan]")
    console.print(f"    • Imminent resolution: < 6 hours (+25%)")
    console.print(f"    • Soon resolution: < 24 hours (+15%)")
    console.print(f"    • Contrarian bet: against 70%+ consensus (+15%)")
    console.print(f"  [bold yellow]ROI Filters:[/bold yellow]")
    console.print(f"    • Trade freshness: < 5 MINUTES")
    console.print(f"    • Price filter: 0.15-0.85 only (skip extremes)")
    console.print(f"    • Min R:R ratio: 1:5 (skip tiny upside trades)")
    console.print(f"    • Min confidence: 65% threshold")
    console.print(f"    • Min resolution: > 30 min (skip about-to-resolve)")
    console.print(f"    • Wallet rate limit: 1 alert/wallet/hour")
    console.print(f"    • Position scaling: by price distance")
    console.print(f"  [bold blue]Memory Safety:[/bold blue]")
    console.print(f"    • Daily cache reset (prevents OOM)")
    console.print(f"    • LRU caches with size limits")
    console.print(f"    • Rate limiting (30 req/min)")
    console.print(f"  [cyan]Other Filters:[/cyan]")
    console.print(f"    • Min bet: ${min_bet_usd:,.0f} or 2% of liquidity")
    console.print(f"    • Bot pattern detection: -30% confidence")
    console.print(f"  [cyan]Scan interval: {interval_seconds}s[/cyan]")
    console.print(f"[dim]Press Ctrl+C to stop[/dim]\n")

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
