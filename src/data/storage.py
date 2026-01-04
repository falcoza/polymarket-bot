"""SQLite persistence layer for orders, trades, and positions."""

import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.models.order import Order, OrderSide, OrderStatus, OrderType, Trade
from src.models.position import PortfolioSnapshot, Position, PositionSide

logger = logging.getLogger(__name__)


class DatabaseStorage:
    """SQLite persistence layer."""

    def __init__(self, db_path: str = "data/trading.db"):
        """Initialize database storage.

        Args:
            db_path: Path to SQLite database file
        """
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Initialize database schema."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                order_type TEXT NOT NULL,
                price REAL,
                size REAL,
                amount REAL,
                status TEXT NOT NULL,
                strategy_name TEXT,
                exchange_order_id TEXT,
                filled_price REAL,
                filled_size REAL,
                created_at TEXT NOT NULL,
                submitted_at TEXT,
                filled_at TEXT
            )
        """)

        # Trades table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id TEXT PRIMARY KEY,
                order_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                size REAL NOT NULL,
                value_usd REAL NOT NULL,
                strategy_name TEXT,
                timestamp TEXT NOT NULL,
                realized_pnl REAL,
                FOREIGN KEY (order_id) REFERENCES orders (id)
            )
        """)

        # Positions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id TEXT PRIMARY KEY,
                market_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                market_question TEXT,
                side TEXT NOT NULL,
                size REAL NOT NULL,
                avg_entry_price REAL NOT NULL,
                current_price REAL NOT NULL,
                total_cost REAL NOT NULL,
                current_value REAL NOT NULL,
                unrealized_pnl REAL,
                unrealized_pnl_pct REAL,
                stop_loss_price REAL,
                take_profit_price REAL,
                strategy_name TEXT,
                opened_at TEXT NOT NULL,
                last_updated TEXT NOT NULL,
                is_open INTEGER DEFAULT 1
            )
        """)

        # Portfolio snapshots
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                total_value REAL NOT NULL,
                cash_balance REAL NOT NULL,
                positions_value REAL NOT NULL,
                total_unrealized_pnl REAL,
                total_realized_pnl REAL,
                daily_pnl REAL,
                position_count INTEGER
            )
        """)

        # Trade audits table for execution quality tracking
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trade_audits (
                id TEXT PRIMARY KEY,
                trade_id TEXT NOT NULL,
                market_id TEXT NOT NULL,
                strategy_name TEXT,
                edge_type TEXT,
                expected_price REAL,
                actual_price REAL,
                slippage REAL,
                slippage_pct REAL,
                thesis_correct INTEGER,
                trade_profitable INTEGER,
                realized_pnl REAL,
                entry_timestamp TEXT NOT NULL,
                exit_timestamp TEXT,
                time_in_position_seconds REAL,
                liquidity_at_trade REAL,
                spread_at_trade REAL,
                volume_24hr_at_trade REAL,
                metadata TEXT
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_market ON orders(market_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(is_open)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audits_strategy ON trade_audits(strategy_name)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audits_edge ON trade_audits(edge_type)")

        conn.commit()
        conn.close()
        logger.info(f"Database initialized at {self.db_path}")

    def _get_connection(self) -> sqlite3.Connection:
        """Get database connection."""
        return sqlite3.connect(self.db_path)

    # Order methods
    def save_order(self, order: Order) -> None:
        """Save or update an order."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO orders
            (id, market_id, token_id, side, order_type, price, size, amount,
             status, strategy_name, exchange_order_id, filled_price, filled_size,
             created_at, submitted_at, filled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order.id, order.market_id, order.token_id,
            order.side.value, order.order_type.value,
            order.price, order.size, order.amount,
            order.status.value, order.strategy_name,
            order.exchange_order_id, order.filled_price, order.filled_size,
            order.created_at.isoformat(),
            order.submitted_at.isoformat() if order.submitted_at else None,
            order.filled_at.isoformat() if order.filled_at else None,
        ))

        conn.commit()
        conn.close()

    def get_order(self, order_id: str) -> Optional[Order]:
        """Get order by ID."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        return self._row_to_order(row, cursor.description)

    def get_orders_by_status(self, status: OrderStatus) -> List[Order]:
        """Get orders by status."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM orders WHERE status = ?", (status.value,))
        rows = cursor.fetchall()
        description = cursor.description
        conn.close()

        return [self._row_to_order(row, description) for row in rows]

    def _row_to_order(self, row: tuple, description: Any) -> Order:
        """Convert database row to Order."""
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))

        return Order(
            id=data["id"],
            market_id=data["market_id"],
            token_id=data["token_id"],
            side=OrderSide(data["side"]),
            order_type=OrderType(data["order_type"]),
            price=data["price"],
            size=data["size"],
            amount=data["amount"],
            status=OrderStatus(data["status"]),
            strategy_name=data["strategy_name"],
            exchange_order_id=data["exchange_order_id"],
            filled_price=data["filled_price"],
            filled_size=data["filled_size"],
            created_at=datetime.fromisoformat(data["created_at"]),
            submitted_at=datetime.fromisoformat(data["submitted_at"]) if data["submitted_at"] else None,
            filled_at=datetime.fromisoformat(data["filled_at"]) if data["filled_at"] else None,
        )

    # Position methods
    def save_position(self, position: Position, is_open: bool = True) -> None:
        """Save or update a position."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO positions
            (id, market_id, token_id, market_question, side, size, avg_entry_price,
             current_price, total_cost, current_value, unrealized_pnl, unrealized_pnl_pct,
             stop_loss_price, take_profit_price, strategy_name, opened_at, last_updated, is_open)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            position.id, position.market_id, position.token_id,
            position.market_question, position.side.value, position.size,
            position.avg_entry_price, position.current_price, position.total_cost,
            position.current_value, position.unrealized_pnl, position.unrealized_pnl_pct,
            position.stop_loss_price, position.take_profit_price, position.strategy_name,
            position.opened_at.isoformat(), position.last_updated.isoformat(),
            1 if is_open else 0,
        ))

        conn.commit()
        conn.close()

    def get_open_positions(self) -> List[Position]:
        """Get all open positions."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM positions WHERE is_open = 1")
        rows = cursor.fetchall()
        description = cursor.description
        conn.close()

        return [self._row_to_position(row, description) for row in rows]

    def close_position(self, position_id: str) -> None:
        """Mark a position as closed."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute(
            "UPDATE positions SET is_open = 0, last_updated = ? WHERE id = ?",
            (datetime.utcnow().isoformat(), position_id),
        )

        conn.commit()
        conn.close()

    def _row_to_position(self, row: tuple, description: Any) -> Position:
        """Convert database row to Position."""
        cols = [d[0] for d in description]
        data = dict(zip(cols, row))

        return Position(
            id=data["id"],
            market_id=data["market_id"],
            token_id=data["token_id"],
            market_question=data["market_question"] or "",
            side=PositionSide(data["side"]),
            size=data["size"],
            avg_entry_price=data["avg_entry_price"],
            current_price=data["current_price"],
            total_cost=data["total_cost"],
            current_value=data["current_value"],
            unrealized_pnl=data["unrealized_pnl"] or 0,
            unrealized_pnl_pct=data["unrealized_pnl_pct"] or 0,
            stop_loss_price=data["stop_loss_price"],
            take_profit_price=data["take_profit_price"],
            strategy_name=data["strategy_name"] or "",
            opened_at=datetime.fromisoformat(data["opened_at"]),
            last_updated=datetime.fromisoformat(data["last_updated"]),
        )

    # Trade methods
    def save_trade(self, trade: Trade) -> None:
        """Save a trade."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT OR REPLACE INTO trades
            (id, order_id, market_id, token_id, side, price, size, value_usd,
             strategy_name, timestamp, realized_pnl)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.id, trade.order_id, trade.market_id, trade.token_id,
            trade.side.value, trade.price, trade.size, trade.value_usd,
            trade.strategy_name, trade.timestamp.isoformat(), trade.realized_pnl,
        ))

        conn.commit()
        conn.close()

    # Portfolio methods
    def save_portfolio_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        """Save portfolio state snapshot."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO portfolio_snapshots
            (timestamp, total_value, cash_balance, positions_value,
             total_unrealized_pnl, total_realized_pnl, daily_pnl, position_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            snapshot.timestamp.isoformat(),
            snapshot.total_value, snapshot.cash_balance,
            snapshot.positions_value, snapshot.total_unrealized_pnl,
            snapshot.total_realized_pnl, snapshot.daily_pnl,
            snapshot.position_count,
        ))

        conn.commit()
        conn.close()

    # Performance methods
    def get_strategy_performance(self, strategy_name: str) -> Dict[str, Any]:
        """Get performance metrics for a strategy."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(realized_pnl) as total_pnl,
                AVG(realized_pnl) as avg_pnl,
                MAX(realized_pnl) as max_profit,
                MIN(realized_pnl) as max_loss
            FROM trades
            WHERE strategy_name = ?
        """, (strategy_name,))

        row = cursor.fetchone()
        conn.close()

        total = row[0] or 0
        winning = row[1] or 0

        return {
            "strategy_name": strategy_name,
            "total_trades": total,
            "winning_trades": winning,
            "losing_trades": total - winning,
            "total_pnl": row[2] or 0,
            "avg_pnl": row[3] or 0,
            "max_profit": row[4] or 0,
            "max_loss": row[5] or 0,
            "win_rate": (winning / total) if total > 0 else 0,
        }

    def get_all_strategy_performance(self) -> List[Dict[str, Any]]:
        """Get performance for all strategies."""
        conn = self._get_connection()
        cursor = conn.cursor()

        cursor.execute("SELECT DISTINCT strategy_name FROM trades")
        strategies = [row[0] for row in cursor.fetchall()]
        conn.close()

        return [self.get_strategy_performance(s) for s in strategies if s]

    # Audit methods
    def save_trade_audit(self, audit: Any) -> None:
        """Save a trade audit record.

        Args:
            audit: TradeAudit object to save
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        time_in_position = None
        if audit.time_in_position:
            time_in_position = audit.time_in_position.total_seconds()

        cursor.execute("""
            INSERT OR REPLACE INTO trade_audits
            (id, trade_id, market_id, strategy_name, edge_type,
             expected_price, actual_price, slippage, slippage_pct,
             thesis_correct, trade_profitable, realized_pnl,
             entry_timestamp, exit_timestamp, time_in_position_seconds,
             liquidity_at_trade, spread_at_trade, volume_24hr_at_trade, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            audit.id, audit.trade_id, audit.market_id,
            audit.strategy_name, audit.edge_type,
            audit.expected_price, audit.actual_price,
            audit.slippage, audit.slippage_pct,
            1 if audit.thesis_correct else 0,
            1 if audit.trade_profitable else 0,
            audit.realized_pnl,
            audit.entry_timestamp.isoformat(),
            audit.exit_timestamp.isoformat() if audit.exit_timestamp else None,
            time_in_position,
            audit.liquidity_at_trade, audit.spread_at_trade,
            audit.volume_24hr_at_trade,
            json.dumps(audit.metadata) if audit.metadata else None,
        ))

        conn.commit()
        conn.close()

    def get_trade_audits(
        self,
        strategy_name: Optional[str] = None,
        edge_type: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Get trade audit records.

        Args:
            strategy_name: Optional filter by strategy
            edge_type: Optional filter by edge type
            limit: Max records to return

        Returns:
            List of audit records as dicts
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        query = "SELECT * FROM trade_audits WHERE 1=1"
        params = []

        if strategy_name:
            query += " AND strategy_name = ?"
            params.append(strategy_name)
        if edge_type:
            query += " AND edge_type = ?"
            params.append(edge_type)

        query += " ORDER BY entry_timestamp DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()
        description = cursor.description
        conn.close()

        cols = [d[0] for d in description]
        return [dict(zip(cols, row)) for row in rows]

    def get_audit_summary(self) -> Dict[str, Any]:
        """Get summary statistics from audit data.

        Returns:
            Dict with audit summary stats
        """
        conn = self._get_connection()
        cursor = conn.cursor()

        # Overall stats
        cursor.execute("""
            SELECT
                COUNT(*) as total_audits,
                AVG(slippage_pct) as avg_slippage,
                SUM(CASE WHEN trade_profitable = 1 THEN 1 ELSE 0 END) as profitable_trades,
                SUM(realized_pnl) as total_pnl
            FROM trade_audits
            WHERE exit_timestamp IS NOT NULL
        """)
        row = cursor.fetchone()

        # By edge type
        cursor.execute("""
            SELECT
                edge_type,
                COUNT(*) as count,
                AVG(slippage_pct) as avg_slippage,
                SUM(CASE WHEN trade_profitable = 1 THEN 1 ELSE 0 END) as profitable,
                SUM(realized_pnl) as total_pnl
            FROM trade_audits
            WHERE exit_timestamp IS NOT NULL
            GROUP BY edge_type
        """)
        edge_rows = cursor.fetchall()

        conn.close()

        by_edge_type = {}
        for edge_row in edge_rows:
            edge_type = edge_row[0] or "unknown"
            by_edge_type[edge_type] = {
                "count": edge_row[1],
                "avg_slippage": edge_row[2],
                "profitable": edge_row[3],
                "total_pnl": edge_row[4],
                "win_rate": edge_row[3] / edge_row[1] if edge_row[1] > 0 else 0,
            }

        return {
            "total_audits": row[0] or 0,
            "avg_slippage": row[1] or 0,
            "profitable_trades": row[2] or 0,
            "total_pnl": row[3] or 0,
            "win_rate": (row[2] / row[0]) if row[0] and row[0] > 0 else 0,
            "by_edge_type": by_edge_type,
        }
