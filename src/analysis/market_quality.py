"""Market quality scoring for trading decisions.

Scores markets on a 0-100 scale based on:
- Resolution clarity (0-40 points)
- Liquidity (0-30 points)
- Spread quality (0-20 points)
- Time to resolution (0-10 points)
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

from config.settings import Settings
from src.models.market import Market

logger = logging.getLogger(__name__)


# Keywords indicating clear resolution criteria
CLEAR_RESOLUTION_KEYWORDS = {
    "official",
    "announced",
    "published",
    "reported",
    "according to",
    "data from",
    "price on",
    "closes above",
    "closes below",
    "reaches",
    "hits",
    "trading at",
    "coingecko",
    "coinmarketcap",
    "binance",
    "coinbase",
}

# Keywords indicating ambiguous resolution
AMBIGUOUS_KEYWORDS = {
    "will",
    "might",
    "could",
    "should",
    "may",
    "believe",
    "think",
    "opinion",
    "experts say",
    "sources say",
    "rumor",
    "allegedly",
}

# Categories with typically clear resolution
CLEAR_CATEGORIES = {"crypto", "sports", "finance", "economics"}

# Crypto-specific keywords for 15-min markets
CRYPTO_KEYWORDS = {"btc", "eth", "bitcoin", "ethereum", "price", "15 min", "15min", "minute"}


@dataclass
class MarketQualityScore:
    """Detailed breakdown of market quality score."""

    market_id: str
    total_score: float
    resolution_clarity_score: float  # 0-40
    liquidity_score: float  # 0-30
    spread_score: float  # 0-20
    time_to_resolution_score: float  # 0-10

    # Additional metadata
    is_crypto_15min: bool
    days_to_resolution: Optional[float]
    reasons: List[str]

    @property
    def is_tradeable(self) -> bool:
        """Check if market meets minimum quality threshold."""
        return self.total_score >= 60.0

    @property
    def is_arbitrage_candidate(self) -> bool:
        """Check if market is suitable for arbitrage."""
        return (
            self.is_tradeable
            and self.is_crypto_15min
            and self.liquidity_score >= 20  # At least $10k liquidity
            and self.spread_score >= 10  # Spread < 5%
        )


class MarketQualityScorer:
    """Scores markets for trading quality.

    Based on analysis of top Polymarket traders, success depends on:
    1. Resolution clarity - avoiding ambiguous markets
    2. Sufficient liquidity - for clean entry/exit
    3. Tight spreads - minimize execution cost
    4. Near-term resolution - faster capital turnover
    """

    def __init__(self, settings: Optional[Settings] = None):
        """Initialize the scorer with optional settings."""
        self.settings = settings or Settings()
        self.min_quality_score = self.settings.min_market_quality_score
        self.min_liquidity = self.settings.min_liquidity_threshold

    def score_market(self, market: Market) -> MarketQualityScore:
        """Score a market on quality factors.

        Returns a MarketQualityScore with breakdown by category.
        """
        reasons = []

        # 1. Resolution Clarity (0-40 points)
        resolution_score = self._score_resolution_clarity(market, reasons)

        # 2. Liquidity (0-30 points)
        liquidity_score = self._score_liquidity(market, reasons)

        # 3. Spread Quality (0-20 points)
        spread_score = self._score_spread(market, reasons)

        # 4. Time to Resolution (0-10 points)
        time_score, days_to_resolution = self._score_time_to_resolution(market, reasons)

        # Check if crypto 15-min market
        is_crypto_15min = self._is_crypto_15min_market(market)
        if is_crypto_15min:
            reasons.append("Crypto 15-min market detected")

        total_score = resolution_score + liquidity_score + spread_score + time_score

        return MarketQualityScore(
            market_id=market.id,
            total_score=total_score,
            resolution_clarity_score=resolution_score,
            liquidity_score=liquidity_score,
            spread_score=spread_score,
            time_to_resolution_score=time_score,
            is_crypto_15min=is_crypto_15min,
            days_to_resolution=days_to_resolution,
            reasons=reasons,
        )

    def _score_resolution_clarity(self, market: Market, reasons: List[str]) -> float:
        """Score resolution clarity (0-40 points).

        Highest scores for markets with:
        - Single authoritative data source
        - Clear, objective resolution criteria
        - No ambiguity in wording
        """
        score = 0.0
        text = f"{market.question} {market.description or ''}".lower()

        # Check for clear resolution keywords
        clear_count = sum(1 for kw in CLEAR_RESOLUTION_KEYWORDS if kw in text)
        ambiguous_count = sum(1 for kw in AMBIGUOUS_KEYWORDS if kw in text)

        # Strong indicators of clear resolution
        if any(source in text for source in ["coingecko", "coinmarketcap", "binance", "coinbase"]):
            score += 25
            reasons.append("Has authoritative data source")
        elif clear_count >= 2:
            score += 20
            reasons.append(f"Multiple clear resolution indicators ({clear_count})")
        elif clear_count >= 1:
            score += 15
            reasons.append("Has clear resolution indicator")
        else:
            score += 5
            reasons.append("No clear resolution source identified")

        # Penalty for ambiguous language
        if ambiguous_count >= 2:
            penalty = min(10, ambiguous_count * 3)
            score = max(0, score - penalty)
            reasons.append(f"Ambiguous language detected (-{penalty})")

        # Category bonus
        if market.category and market.category.lower() in CLEAR_CATEGORIES:
            score += 10
            reasons.append(f"Clear category: {market.category}")

        # Price-based markets are typically clearer
        if re.search(r"\$[\d,]+|\d+%|above|below|reaches|hits", text):
            score += 5
            reasons.append("Quantitative resolution criteria")

        return min(40.0, score)

    def _score_liquidity(self, market: Market, reasons: List[str]) -> float:
        """Score liquidity (0-30 points).

        Higher liquidity = easier entry/exit with less slippage.
        """
        liquidity = market.liquidity

        if liquidity >= 50000:
            score = 30.0
            reasons.append(f"Excellent liquidity: ${liquidity:,.0f}")
        elif liquidity >= 25000:
            score = 25.0
            reasons.append(f"Good liquidity: ${liquidity:,.0f}")
        elif liquidity >= 10000:
            score = 20.0
            reasons.append(f"Adequate liquidity: ${liquidity:,.0f}")
        elif liquidity >= 5000:
            score = 15.0
            reasons.append(f"Moderate liquidity: ${liquidity:,.0f}")
        elif liquidity >= 1000:
            score = 10.0
            reasons.append(f"Low liquidity: ${liquidity:,.0f}")
        else:
            score = 5.0
            reasons.append(f"Very low liquidity: ${liquidity:,.0f}")

        return score

    def _score_spread(self, market: Market, reasons: List[str]) -> float:
        """Score spread quality (0-20 points).

        Tighter spreads = lower execution cost.
        """
        # Calculate spread from prices if not provided
        spread = market.spread
        if spread is None:
            # Estimate from YES/NO prices
            yes_no_sum = market.yes_price + market.no_price
            spread = abs(1.0 - yes_no_sum)

        if spread < 0.02:  # < 2%
            score = 20.0
            reasons.append(f"Excellent spread: {spread:.1%}")
        elif spread < 0.03:  # < 3%
            score = 17.0
            reasons.append(f"Good spread: {spread:.1%}")
        elif spread < 0.05:  # < 5%
            score = 12.0
            reasons.append(f"Acceptable spread: {spread:.1%}")
        elif spread < 0.10:  # < 10%
            score = 7.0
            reasons.append(f"Wide spread: {spread:.1%}")
        else:
            score = 3.0
            reasons.append(f"Very wide spread: {spread:.1%}")

        return score

    def _score_time_to_resolution(
        self, market: Market, reasons: List[str]
    ) -> tuple[float, Optional[float]]:
        """Score time to resolution (0-10 points).

        Shorter time = faster capital turnover.
        """
        if not market.end_date:
            reasons.append("No end date specified")
            return 5.0, None

        now = datetime.now(timezone.utc)
        end_date = market.end_date
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        delta = end_date - now
        days = delta.total_seconds() / 86400

        if days < 0:
            reasons.append("Market may have already ended")
            return 2.0, days
        elif days < 1:  # < 1 day (includes 15-min markets)
            score = 10.0
            reasons.append(f"Very short duration: {days * 24:.1f} hours")
        elif days < 7:  # < 1 week
            score = 8.0
            reasons.append(f"Short duration: {days:.1f} days")
        elif days < 30:  # < 1 month
            score = 5.0
            reasons.append(f"Medium duration: {days:.0f} days")
        elif days < 90:  # < 3 months
            score = 3.0
            reasons.append(f"Long duration: {days:.0f} days")
        else:
            score = 1.0
            reasons.append(f"Very long duration: {days:.0f} days")

        return score, days

    def _is_crypto_15min_market(self, market: Market) -> bool:
        """Check if market is a 15-minute crypto resolution market."""
        text = f"{market.question} {market.description or ''} {market.category or ''}".lower()

        # Must have crypto indicators
        has_crypto = any(kw in text for kw in ["btc", "eth", "bitcoin", "ethereum", "crypto"])

        # Must have short duration indicator
        has_short_duration = any(
            pattern in text for pattern in ["15 min", "15min", "minute", "hourly", "1 hour"]
        )

        # Or check end_date for very short duration
        if market.end_date:
            now = datetime.now(timezone.utc)
            end_date = market.end_date
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            hours_to_end = (end_date - now).total_seconds() / 3600
            if hours_to_end > 0 and hours_to_end <= 1:
                has_short_duration = True

        return has_crypto and has_short_duration

    def is_tradeable(self, market: Market) -> bool:
        """Quick check if market meets minimum quality threshold."""
        score = self.score_market(market)
        return score.is_tradeable

    def get_arbitrage_candidates(self, markets: List[Market]) -> List[MarketQualityScore]:
        """Filter markets suitable for arbitrage strategy.

        Returns scored markets that are:
        - 15-min crypto markets
        - Quality score >= 60
        - Sufficient liquidity
        """
        candidates = []
        for market in markets:
            score = self.score_market(market)
            if score.is_arbitrage_candidate:
                candidates.append(score)

        # Sort by total score descending
        candidates.sort(key=lambda s: s.total_score, reverse=True)
        return candidates

    def score_markets(self, markets: List[Market]) -> List[MarketQualityScore]:
        """Score multiple markets and return sorted by quality."""
        scores = [self.score_market(m) for m in markets]
        scores.sort(key=lambda s: s.total_score, reverse=True)
        return scores
