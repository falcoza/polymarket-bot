"""Trade audit tracking for execution quality analysis.

Tracks post-trade metrics to distinguish skill from variance:
- Edge type (arb, momentum, LLM, speed)
- Entry/exit quality (slippage vs expected)
- Whether thesis was correct
- Whether trade was profitable (different from thesis)
- Time in position

Based on top trader analysis:
"Goal: Distinguish skill from variance"
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class TradeAudit:
    """Audit record for a single trade."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trade_id: str = ""
    market_id: str = ""
    strategy_name: str = ""

    # Edge classification
    edge_type: str = ""  # arb | momentum | llm | speed | other

    # Execution quality
    expected_price: float = 0.0
    actual_price: float = 0.0
    slippage: float = 0.0  # actual - expected (negative = better than expected)
    slippage_pct: float = 0.0

    # Outcome analysis
    thesis_correct: bool = False  # Was the prediction right?
    trade_profitable: bool = False  # Did trade make money?
    realized_pnl: float = 0.0

    # Timing
    entry_timestamp: datetime = field(default_factory=datetime.utcnow)
    exit_timestamp: Optional[datetime] = None
    time_in_position: Optional[timedelta] = None

    # Market conditions at trade
    liquidity_at_trade: float = 0.0
    spread_at_trade: float = 0.0
    volume_24hr_at_trade: float = 0.0

    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def execution_quality(self) -> str:
        """Categorize execution quality."""
        if self.slippage_pct <= -0.005:  # Better than expected
            return "excellent"
        elif self.slippage_pct <= 0.005:  # Within 0.5%
            return "good"
        elif self.slippage_pct <= 0.01:  # Within 1%
            return "acceptable"
        else:
            return "poor"

    @property
    def skill_vs_luck(self) -> str:
        """Assess if profit came from skill or luck."""
        if self.edge_type == "arb":
            return "structural"  # Arb profits are structural, not luck
        elif self.thesis_correct and self.trade_profitable:
            return "skill"
        elif not self.thesis_correct and self.trade_profitable:
            return "luck"
        elif self.thesis_correct and not self.trade_profitable:
            return "execution_error"
        else:
            return "wrong"


class AuditTracker:
    """Tracks and analyzes trade execution quality."""

    def __init__(self, storage=None):
        """Initialize audit tracker.

        Args:
            storage: Optional DatabaseStorage instance
        """
        self.storage = storage
        self._audits: Dict[str, TradeAudit] = {}

    def record_entry(
        self,
        trade_id: str,
        market_id: str,
        strategy_name: str,
        edge_type: str,
        expected_price: float,
        actual_price: float,
        liquidity: float = 0.0,
        spread: float = 0.0,
        volume_24hr: float = 0.0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> TradeAudit:
        """Record trade entry for auditing.

        Args:
            trade_id: Unique trade identifier
            market_id: Market identifier
            strategy_name: Strategy that generated the trade
            edge_type: Type of edge (arb, momentum, llm, speed)
            expected_price: Expected execution price
            actual_price: Actual fill price
            liquidity: Market liquidity at trade time
            spread: Bid-ask spread at trade time
            volume_24hr: 24hr volume at trade time
            metadata: Additional audit metadata

        Returns:
            TradeAudit record
        """
        slippage = actual_price - expected_price
        slippage_pct = slippage / expected_price if expected_price > 0 else 0

        audit = TradeAudit(
            trade_id=trade_id,
            market_id=market_id,
            strategy_name=strategy_name,
            edge_type=edge_type,
            expected_price=expected_price,
            actual_price=actual_price,
            slippage=slippage,
            slippage_pct=slippage_pct,
            liquidity_at_trade=liquidity,
            spread_at_trade=spread,
            volume_24hr_at_trade=volume_24hr,
            entry_timestamp=datetime.utcnow(),
            metadata=metadata or {},
        )

        self._audits[trade_id] = audit

        # Persist if storage available
        if self.storage:
            self._save_audit(audit)

        logger.info(
            f"Audit entry recorded: {trade_id} | "
            f"Slippage: {slippage_pct:.2%} | "
            f"Quality: {audit.execution_quality}"
        )

        return audit

    def record_exit(
        self,
        trade_id: str,
        thesis_correct: bool,
        realized_pnl: float,
        exit_price: Optional[float] = None,
    ) -> Optional[TradeAudit]:
        """Record trade exit and calculate final metrics.

        Args:
            trade_id: Trade to update
            thesis_correct: Whether the prediction was correct
            realized_pnl: Actual P&L from the trade
            exit_price: Optional exit price for slippage calculation

        Returns:
            Updated TradeAudit or None if not found
        """
        audit = self._audits.get(trade_id)
        if not audit:
            logger.warning(f"No audit record found for trade {trade_id}")
            return None

        audit.exit_timestamp = datetime.utcnow()
        audit.time_in_position = audit.exit_timestamp - audit.entry_timestamp
        audit.thesis_correct = thesis_correct
        audit.realized_pnl = realized_pnl
        audit.trade_profitable = realized_pnl > 0

        # Update exit slippage if exit_price provided
        if exit_price is not None:
            audit.metadata["exit_price"] = exit_price

        # Persist update
        if self.storage:
            self._save_audit(audit)

        logger.info(
            f"Audit exit recorded: {trade_id} | "
            f"P&L: ${realized_pnl:.2f} | "
            f"Skill: {audit.skill_vs_luck}"
        )

        return audit

    def get_audit(self, trade_id: str) -> Optional[TradeAudit]:
        """Get audit record by trade ID."""
        return self._audits.get(trade_id)

    def get_execution_quality_report(self) -> Dict[str, Any]:
        """Generate execution quality analysis report.

        Returns:
            Dict with execution quality metrics
        """
        if not self._audits:
            return {
                "total_trades": 0,
                "avg_slippage_pct": 0,
                "quality_breakdown": {},
            }

        audits = list(self._audits.values())

        # Calculate average slippage
        slippages = [a.slippage_pct for a in audits]
        avg_slippage = sum(slippages) / len(slippages) if slippages else 0

        # Quality breakdown
        quality_counts = {"excellent": 0, "good": 0, "acceptable": 0, "poor": 0}
        for audit in audits:
            quality_counts[audit.execution_quality] += 1

        # By edge type
        by_edge_type = {}
        for audit in audits:
            if audit.edge_type not in by_edge_type:
                by_edge_type[audit.edge_type] = {
                    "count": 0,
                    "total_slippage": 0,
                    "profitable": 0,
                }
            by_edge_type[audit.edge_type]["count"] += 1
            by_edge_type[audit.edge_type]["total_slippage"] += audit.slippage_pct
            if audit.trade_profitable:
                by_edge_type[audit.edge_type]["profitable"] += 1

        # Calculate averages per edge type
        for edge_type, data in by_edge_type.items():
            if data["count"] > 0:
                data["avg_slippage"] = data["total_slippage"] / data["count"]
                data["win_rate"] = data["profitable"] / data["count"]
                del data["total_slippage"]

        return {
            "total_trades": len(audits),
            "avg_slippage_pct": avg_slippage,
            "quality_breakdown": quality_counts,
            "by_edge_type": by_edge_type,
        }

    def get_strategy_accuracy_report(self) -> Dict[str, Any]:
        """Analyze thesis accuracy vs trade profitability.

        This is key for distinguishing skill from variance.

        Returns:
            Dict with strategy accuracy metrics
        """
        if not self._audits:
            return {"strategies": {}}

        by_strategy = {}
        for audit in self._audits.values():
            if not audit.exit_timestamp:
                continue  # Skip open trades

            strategy = audit.strategy_name
            if strategy not in by_strategy:
                by_strategy[strategy] = {
                    "total": 0,
                    "thesis_correct": 0,
                    "trade_profitable": 0,
                    "skill": 0,
                    "luck": 0,
                    "execution_error": 0,
                    "wrong": 0,
                    "total_pnl": 0,
                }

            by_strategy[strategy]["total"] += 1
            by_strategy[strategy]["total_pnl"] += audit.realized_pnl

            if audit.thesis_correct:
                by_strategy[strategy]["thesis_correct"] += 1
            if audit.trade_profitable:
                by_strategy[strategy]["trade_profitable"] += 1

            # Skill classification
            skill_class = audit.skill_vs_luck
            by_strategy[strategy][skill_class] += 1

        # Calculate rates
        for strategy, data in by_strategy.items():
            total = data["total"]
            if total > 0:
                data["thesis_accuracy"] = data["thesis_correct"] / total
                data["trade_win_rate"] = data["trade_profitable"] / total
                data["skill_rate"] = data["skill"] / total
                data["luck_rate"] = data["luck"] / total
                data["avg_pnl"] = data["total_pnl"] / total

        return {"strategies": by_strategy}

    def get_arbitrage_performance(self) -> Dict[str, Any]:
        """Get specific performance metrics for arbitrage trades.

        Returns:
            Dict with arbitrage-specific metrics
        """
        arb_audits = [a for a in self._audits.values() if a.edge_type == "arb"]

        if not arb_audits:
            return {
                "total_arbs": 0,
                "success_rate": 0,
                "avg_profit": 0,
                "total_profit": 0,
            }

        completed = [a for a in arb_audits if a.exit_timestamp]
        profitable = [a for a in completed if a.trade_profitable]

        total_profit = sum(a.realized_pnl for a in completed)
        avg_profit = total_profit / len(completed) if completed else 0

        # Time analysis
        hold_times = [
            a.time_in_position.total_seconds() / 60
            for a in completed
            if a.time_in_position
        ]
        avg_hold_time = sum(hold_times) / len(hold_times) if hold_times else 0

        return {
            "total_arbs": len(arb_audits),
            "completed": len(completed),
            "profitable": len(profitable),
            "success_rate": len(profitable) / len(completed) if completed else 0,
            "total_profit": total_profit,
            "avg_profit": avg_profit,
            "avg_hold_time_minutes": avg_hold_time,
            "avg_slippage": sum(a.slippage_pct for a in completed) / len(completed) if completed else 0,
        }

    def _save_audit(self, audit: TradeAudit) -> None:
        """Save audit to database."""
        if not self.storage:
            return

        try:
            self.storage.save_trade_audit(audit)
        except Exception as e:
            logger.warning(f"Failed to save audit: {e}")

    def generate_report(self) -> str:
        """Generate comprehensive audit report.

        Returns:
            Formatted report string
        """
        exec_report = self.get_execution_quality_report()
        accuracy_report = self.get_strategy_accuracy_report()
        arb_report = self.get_arbitrage_performance()

        lines = [
            "=" * 60,
            "TRADE AUDIT REPORT",
            f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
            "=" * 60,
            "",
            "EXECUTION QUALITY",
            "-" * 40,
            f"  Total Trades:     {exec_report['total_trades']}",
            f"  Avg Slippage:     {exec_report['avg_slippage_pct']:.2%}",
            "",
            "  Quality Breakdown:",
        ]

        for quality, count in exec_report.get("quality_breakdown", {}).items():
            lines.append(f"    {quality.capitalize():12s} {count}")

        lines.extend([
            "",
            "STRATEGY ACCURACY (Skill vs Luck)",
            "-" * 40,
        ])

        for strategy, data in accuracy_report.get("strategies", {}).items():
            lines.extend([
                f"  {strategy}:",
                f"    Thesis Accuracy: {data.get('thesis_accuracy', 0):.1%}",
                f"    Trade Win Rate:  {data.get('trade_win_rate', 0):.1%}",
                f"    Skill Rate:      {data.get('skill_rate', 0):.1%}",
                f"    Luck Rate:       {data.get('luck_rate', 0):.1%}",
                f"    Total P&L:       ${data.get('total_pnl', 0):,.2f}",
                "",
            ])

        lines.extend([
            "ARBITRAGE PERFORMANCE",
            "-" * 40,
            f"  Total Arbs:       {arb_report['total_arbs']}",
            f"  Completed:        {arb_report['completed']}",
            f"  Success Rate:     {arb_report['success_rate']:.1%}",
            f"  Total Profit:     ${arb_report['total_profit']:,.2f}",
            f"  Avg Profit:       ${arb_report['avg_profit']:,.2f}",
            f"  Avg Hold Time:    {arb_report['avg_hold_time_minutes']:.1f} min",
            "=" * 60,
        ])

        return "\n".join(lines)
