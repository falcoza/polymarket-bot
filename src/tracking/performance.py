"""Strategy performance tracking and analysis."""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.data.storage import DatabaseStorage

logger = logging.getLogger(__name__)


@dataclass
class StrategyMetrics:
    """Performance metrics for a strategy."""

    strategy_name: str
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    avg_pnl_per_trade: float
    max_profit: float
    max_loss: float
    profit_factor: float


class PerformanceTracker:
    """Track and analyze strategy performance."""

    def __init__(self, storage: DatabaseStorage):
        """Initialize performance tracker.

        Args:
            storage: Database storage instance
        """
        self.storage = storage

    def get_strategy_metrics(self, strategy_name: str) -> StrategyMetrics:
        """Calculate comprehensive metrics for a strategy.

        Args:
            strategy_name: Strategy to analyze

        Returns:
            StrategyMetrics with performance data
        """
        perf = self.storage.get_strategy_performance(strategy_name)

        total_trades = perf["total_trades"]
        winning_trades = perf["winning_trades"]
        losing_trades = perf["losing_trades"]
        total_pnl = perf["total_pnl"]

        # Calculate profit factor (gross profit / gross loss)
        profit_factor = self._calculate_profit_factor(
            perf["max_profit"], abs(perf["max_loss"]) if perf["max_loss"] else 0
        )

        return StrategyMetrics(
            strategy_name=strategy_name,
            total_trades=total_trades,
            winning_trades=winning_trades,
            losing_trades=losing_trades,
            win_rate=perf["win_rate"],
            total_pnl=total_pnl,
            avg_pnl_per_trade=perf["avg_pnl"],
            max_profit=perf["max_profit"],
            max_loss=perf["max_loss"],
            profit_factor=profit_factor,
        )

    def compare_strategies(self) -> List[StrategyMetrics]:
        """Compare performance across all strategies.

        Returns:
            List of StrategyMetrics for each strategy
        """
        all_perf = self.storage.get_all_strategy_performance()
        metrics = []

        for perf in all_perf:
            metrics.append(
                StrategyMetrics(
                    strategy_name=perf["strategy_name"],
                    total_trades=perf["total_trades"],
                    winning_trades=perf["winning_trades"],
                    losing_trades=perf["losing_trades"],
                    win_rate=perf["win_rate"],
                    total_pnl=perf["total_pnl"],
                    avg_pnl_per_trade=perf["avg_pnl"],
                    max_profit=perf["max_profit"],
                    max_loss=perf["max_loss"],
                    profit_factor=self._calculate_profit_factor(
                        perf["max_profit"],
                        abs(perf["max_loss"]) if perf["max_loss"] else 0,
                    ),
                )
            )

        # Sort by total P&L descending
        metrics.sort(key=lambda m: m.total_pnl, reverse=True)
        return metrics

    def _calculate_profit_factor(
        self,
        gross_profit: float,
        gross_loss: float,
    ) -> float:
        """Calculate profit factor.

        Args:
            gross_profit: Total profits from winning trades
            gross_loss: Total losses from losing trades (positive value)

        Returns:
            Profit factor (gross profit / gross loss)
        """
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    def generate_report(self) -> str:
        """Generate a text performance report.

        Returns:
            Formatted performance report string
        """
        metrics = self.compare_strategies()

        if not metrics:
            return "No trading data available yet."

        lines = [
            "=" * 60,
            "STRATEGY PERFORMANCE REPORT",
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
            "=" * 60,
            "",
        ]

        for m in metrics:
            lines.extend(
                [
                    f"Strategy: {m.strategy_name}",
                    "-" * 40,
                    f"  Total Trades:     {m.total_trades}",
                    f"  Winning Trades:   {m.winning_trades}",
                    f"  Losing Trades:    {m.losing_trades}",
                    f"  Win Rate:         {m.win_rate:.1%}",
                    f"  Total P&L:        ${m.total_pnl:,.2f}",
                    f"  Avg P&L/Trade:    ${m.avg_pnl_per_trade:,.2f}",
                    f"  Max Profit:       ${m.max_profit:,.2f}",
                    f"  Max Loss:         ${m.max_loss:,.2f}",
                    f"  Profit Factor:    {m.profit_factor:.2f}",
                    "",
                ]
            )

        # Summary
        total_pnl = sum(m.total_pnl for m in metrics)
        total_trades = sum(m.total_trades for m in metrics)

        lines.extend(
            [
                "=" * 60,
                "OVERALL SUMMARY",
                "=" * 60,
                f"  Total Strategies: {len(metrics)}",
                f"  Total Trades:     {total_trades}",
                f"  Combined P&L:     ${total_pnl:,.2f}",
                "=" * 60,
            ]
        )

        return "\n".join(lines)

    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics.

        Returns:
            Dictionary with summary stats
        """
        metrics = self.compare_strategies()

        if not metrics:
            return {
                "strategy_count": 0,
                "total_trades": 0,
                "total_pnl": 0,
                "best_strategy": None,
                "worst_strategy": None,
            }

        return {
            "strategy_count": len(metrics),
            "total_trades": sum(m.total_trades for m in metrics),
            "total_pnl": sum(m.total_pnl for m in metrics),
            "best_strategy": metrics[0].strategy_name if metrics else None,
            "worst_strategy": metrics[-1].strategy_name if metrics else None,
            "strategies": [
                {
                    "name": m.strategy_name,
                    "trades": m.total_trades,
                    "pnl": m.total_pnl,
                    "win_rate": m.win_rate,
                }
                for m in metrics
            ],
        }
