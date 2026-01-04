"""Anthropic Claude API client for market analysis."""

import json
import logging
from typing import List, Optional

import anthropic

from config.settings import Settings
from src.models.market import Market
from src.models.signal import SignalType, TradingSignal

logger = logging.getLogger(__name__)

MARKET_ANALYSIS_PROMPT = """You are an expert prediction market analyst and trader. Analyze the following market and provide a trading recommendation.

Market Question: {question}
Description: {description}
Category: {category}

Current Prices:
- YES: {yes_price:.2%}
- NO: {no_price:.2%}

Market Statistics:
- 24h Volume: ${volume_24hr:,.2f}
- Total Liquidity: ${liquidity:,.2f}
- Spread: {spread:.4f}

Additional Context (if provided):
{context}

Analyze this market considering:
1. The implied probability vs your estimated true probability
2. Recent news or events that might affect the outcome
3. Market efficiency - is this market likely mispriced?
4. Time until resolution and uncertainty factors
5. Risk/reward ratio for potential trades

You must respond with ONLY a valid JSON object in this exact format (no other text):
{{
    "signal": "strong_buy" | "buy" | "hold" | "sell" | "strong_sell",
    "confidence": 0.0-1.0,
    "target_outcome": "yes" | "no",
    "estimated_probability": 0.0-1.0,
    "reasoning": "Your brief analysis...",
    "key_factors": ["factor1", "factor2", ...]
}}
"""


class AnthropicAnalysisClient:
    """Claude API client for market analysis."""

    def __init__(self, settings: Settings):
        """Initialize the Anthropic client."""
        self.settings = settings
        if settings.anthropic_api_key:
            self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        else:
            self.client = None
            logger.warning("No Anthropic API key provided - LLM analysis disabled")
        self.model = settings.claude_model

    def is_available(self) -> bool:
        """Check if the client is configured."""
        return self.client is not None

    async def analyze_market(
        self,
        market: Market,
        additional_context: Optional[str] = None,
    ) -> TradingSignal:
        """Analyze a market using Claude and return a trading signal."""
        if not self.client:
            return self._create_hold_signal(market, "LLM client not configured")

        prompt = MARKET_ANALYSIS_PROMPT.format(
            question=market.question,
            description=market.description or "No description provided",
            category=market.category or "Unknown",
            yes_price=market.yes_price,
            no_price=market.no_price,
            volume_24hr=market.volume_24hr or 0,
            liquidity=market.liquidity,
            spread=market.spread or 0,
            context=additional_context or "None",
        )

        try:
            message = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )

            # Parse JSON response
            response_text = message.content[0].text

            # Extract JSON from response (handle markdown code blocks)
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]

            analysis = json.loads(response_text.strip())

            # Map to SignalType
            signal_map = {
                "strong_buy": SignalType.STRONG_BUY,
                "buy": SignalType.BUY,
                "hold": SignalType.HOLD,
                "sell": SignalType.SELL,
                "strong_sell": SignalType.STRONG_SELL,
            }

            # Determine token based on target outcome
            target_outcome = analysis.get("target_outcome", "yes")
            token_id = (
                market.yes_token_id if target_outcome == "yes" else market.no_token_id
            )

            # Determine side based on signal
            signal_type = signal_map.get(analysis.get("signal", "hold"), SignalType.HOLD)
            suggested_side = None
            if signal_type in [SignalType.STRONG_BUY, SignalType.BUY]:
                suggested_side = "BUY"
            elif signal_type in [SignalType.STRONG_SELL, SignalType.SELL]:
                suggested_side = "SELL"

            return TradingSignal(
                market_id=market.id,
                token_id=token_id,
                signal_type=signal_type,
                confidence=float(analysis.get("confidence", 0.5)),
                suggested_side=suggested_side,
                strategy_name="llm_claude",
                reasoning=analysis.get("reasoning"),
                metadata={
                    "estimated_probability": analysis.get("estimated_probability"),
                    "key_factors": analysis.get("key_factors", []),
                    "target_outcome": target_outcome,
                    "model": self.model,
                },
            )

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse LLM response as JSON: {e}")
            return self._create_hold_signal(market, f"JSON parse error: {e}")
        except anthropic.APIError as e:
            logger.error(f"Anthropic API error: {e}")
            return self._create_hold_signal(market, f"API error: {e}")
        except Exception as e:
            logger.error(f"Error analyzing market {market.id}: {e}")
            return self._create_hold_signal(market, f"Analysis error: {e}")

    def _create_hold_signal(self, market: Market, reason: str) -> TradingSignal:
        """Create a neutral hold signal."""
        return TradingSignal(
            market_id=market.id,
            token_id=market.yes_token_id,
            signal_type=SignalType.HOLD,
            confidence=0.0,
            strategy_name="llm_claude",
            reasoning=reason,
            metadata={"error": reason},
        )

    async def analyze_multiple_markets(
        self,
        markets: List[Market],
        max_concurrent: int = 3,
    ) -> List[TradingSignal]:
        """Analyze multiple markets with rate limiting."""
        import asyncio

        semaphore = asyncio.Semaphore(max_concurrent)

        async def analyze_with_limit(market: Market) -> TradingSignal:
            async with semaphore:
                # Add small delay between calls to respect rate limits
                await asyncio.sleep(0.5)
                return await self.analyze_market(market)

        tasks = [analyze_with_limit(m) for m in markets]
        return await asyncio.gather(*tasks)
