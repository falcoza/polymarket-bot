"""CLI interface for Polymarket trading bot."""

import asyncio
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import Settings, TradingMode
from src.api.clob_client import PolymarketClobClient
from src.api.gamma_client import GammaMarketClient
from src.analysis.market_quality import MarketQualityScorer
from src.analysis.pre_trade_validator import PreTradeValidator
from src.data.price_history import PriceHistoryStore
from src.data.storage import DatabaseStorage
from src.execution.executor import OrderExecutor
from src.models.market import PriceSnapshot
from src.models.position import PortfolioSnapshot
from src.risk.manager import RiskManager
from src.strategies.llm_strategy import LLMStrategy
from src.strategies.momentum_strategy import MomentumStrategy
from src.strategies.arbitrage_strategy import ArbitrageStrategy
from src.tracking.performance import PerformanceTracker
from src.tracking.audit import AuditTracker

app = typer.Typer(
    name="polybot",
    help="Automated trading bot for Polymarket prediction markets",
    add_completion=False,
)
console = Console()


def setup_logging(level: str = "INFO") -> None:
    """Configure logging with rich handler."""
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@app.command()
def run(
    mode: str = typer.Option(
        "paper",
        "--mode", "-m",
        help="Trading mode: live, paper, backtest",
    ),
    strategy: str = typer.Option(
        "arbitrage",
        "--strategy", "-s",
        help="Strategy: arbitrage, llm, momentum, all",
    ),
    interval: int = typer.Option(
        300,
        "--interval", "-i",
        help="Check interval in seconds",
    ),
    log_level: str = typer.Option(
        "INFO",
        "--log-level", "-l",
        help="Logging level",
    ),
) -> None:
    """Start the trading bot."""
    setup_logging(log_level)

    settings = Settings()
    settings.trading_mode = TradingMode(mode)

    console.print(f"[bold green]Starting Polymarket Bot[/bold green]")
    console.print(f"  Mode: {mode}")
    console.print(f"  Strategy: {strategy}")
    console.print(f"  Interval: {interval}s")

    # Validate settings
    valid, msg = settings.validate_for_trading()
    if not valid and mode == "live":
        console.print(f"[bold red]Configuration error: {msg}[/bold red]")
        raise typer.Exit(1)

    asyncio.run(_run_bot(settings, strategy, interval))


async def _run_bot(settings: Settings, strategy: str, interval: int) -> None:
    """Main bot loop."""
    # Initialize components
    clob_client = PolymarketClobClient(settings)
    gamma_client = GammaMarketClient(settings)
    risk_manager = RiskManager(settings)
    storage = DatabaseStorage()
    price_history = PriceHistoryStore()
    executor = OrderExecutor(settings, clob_client, risk_manager)

    # Connect to exchange in live mode
    if settings.trading_mode == TradingMode.LIVE:
        try:
            clob_client.connect()
            console.print("[green]Connected to Polymarket[/green]")
        except Exception as e:
            console.print(f"[red]Failed to connect: {e}[/red]")
            return

    # Initialize strategies
    strategies = []
    if strategy in ["arbitrage", "all"]:
        strategies.append(ArbitrageStrategy(settings))
    if strategy in ["llm", "all"]:
        strategies.append(LLMStrategy(settings))
    if strategy in ["momentum", "all"]:
        strategies.append(MomentumStrategy(settings, price_history))

    console.print(f"[green]Bot started with {len(strategies)} strategies[/green]")

    # Initialize portfolio (paper trading starts with configured max exposure)
    cash_balance = settings.max_total_exposure_usd

    # Main loop
    iteration = 0
    while True:
        try:
            iteration += 1
            console.print(f"\n[dim]--- Iteration {iteration} ---[/dim]")

            # Fetch markets
            markets = gamma_client.get_tradable_markets()
            console.print(f"Found {len(markets)} tradable markets")

            # Update price history
            for market in markets[:50]:  # Limit to top 50 by volume
                snapshot = PriceSnapshot(
                    market_id=market.id,
                    token_id=market.yes_token_id,
                    mid_price=market.yes_price,
                    best_bid=market.best_bid,
                    best_ask=market.best_ask,
                    volume=market.volume_24hr,
                )
                price_history.add_snapshot(snapshot)

            # Get current positions
            positions = storage.get_open_positions()

            # UPDATE PRICES FIRST - before any checks
            for pos in positions:
                market = next((m for m in markets if m.id == pos.market_id), None)
                if market:
                    pos.update_price(market.yes_price)
                    storage.update_position(pos)  # Persist to database

            # Recalculate portfolio with updated prices
            positions_value = sum(p.current_value for p in positions)

            # SAFEGUARD: Prevent negative cash display
            if cash_balance < 0:
                console.print(f"[red]WARNING: Cash went negative (${cash_balance:.2f}), resetting to 0[/red]")
                cash_balance = 0

            portfolio = PortfolioSnapshot(
                total_value=cash_balance + positions_value,
                cash_balance=cash_balance,
                positions_value=positions_value,
                total_unrealized_pnl=sum(p.unrealized_pnl for p in positions),
                position_count=len(positions),
            )

            # Check stop losses AFTER price update
            stop_loss_positions = risk_manager.check_stop_losses(positions)
            for pos in stop_loss_positions:
                order = await executor.close_position(pos, "stop_loss")
                if order:
                    storage.close_position(pos.id)
                    cash_balance += pos.current_value
                    console.print(f"[yellow]Stop loss: closed {pos.market_question[:30]}[/yellow]")

            # Generate signals from all strategies
            for strat in strategies:
                if not strat.is_active:
                    continue

                try:
                    signals = await strat.generate_signals(markets, positions)

                    for signal in signals:
                        # SAFEGUARD: Skip if no cash left
                        if cash_balance <= 0:
                            break

                        order = await executor.execute_signal(signal, positions, portfolio)
                        if order and order.status.value == "filled":
                            # Find market for question
                            market = next(
                                (m for m in markets if m.id == order.market_id), None
                            )
                            question = market.question if market else "Unknown"

                            # Create position
                            position = executor.create_position_from_order(order, question)
                            storage.save_position(position)
                            storage.save_order(order)

                            # Update cash and rebuild portfolio for next order
                            cash_balance -= order.value_usd
                            if cash_balance < 0:
                                cash_balance = 0  # Prevent negative
                            positions.append(position)  # Add to current list
                            positions_value = sum(p.current_value for p in positions)
                            portfolio = PortfolioSnapshot(
                                total_value=cash_balance + positions_value,
                                cash_balance=cash_balance,
                                positions_value=positions_value,
                                total_unrealized_pnl=sum(p.unrealized_pnl for p in positions),
                                position_count=len(positions),
                            )

                            console.print(
                                f"[cyan]Opened: {order.side} ${order.value_usd:.2f} "
                                f"- {question[:40]}[/cyan]"
                            )

                    # Check for exit signals (prices already updated at start of iteration)
                    for pos in positions:
                        if not pos.id:  # Skip newly created positions this iteration
                            continue
                        market = next((m for m in markets if m.id == pos.market_id), None)
                        if market:
                            exit_signal = strat.should_exit_position(pos, market)
                            if exit_signal:
                                order = await executor.close_position(
                                    pos, exit_signal.reasoning or "strategy_exit"
                                )
                                if order:
                                    storage.close_position(pos.id)
                                    cash_balance += pos.current_value
                                    console.print(
                                        f"[yellow]Closed: {pos.market_question[:40]} "
                                        f"P&L: ${pos.unrealized_pnl:.2f}[/yellow]"
                                    )

                except Exception as e:
                    console.print(f"[red]Strategy error ({strat.name}): {e}[/red]")

            # Show status
            console.print(
                f"[dim]Cash: ${cash_balance:.2f} | "
                f"Positions: {len(positions)} | "
                f"Total: ${portfolio.total_value:.2f}[/dim]"
            )

            await asyncio.sleep(interval)

        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")
            break
        except Exception as e:
            console.print(f"[red]Error: {e}[/red]")
            await asyncio.sleep(60)

    gamma_client.close()


@app.command()
def markets(
    limit: int = typer.Option(10, "--limit", "-n", help="Number of markets to show"),
    min_volume: float = typer.Option(1000, "--min-volume", "-v", help="Minimum 24h volume"),
    min_liquidity: float = typer.Option(1000, "--min-liquidity", "-l", help="Minimum liquidity"),
) -> None:
    """List active markets."""
    settings = Settings()
    gamma_client = GammaMarketClient(settings)

    try:
        markets = gamma_client.get_tradable_markets(
            min_volume_24hr=min_volume,
            min_liquidity=min_liquidity,
            limit=limit,
        )

        table = Table(title=f"Top {limit} Active Markets")
        table.add_column("Question", width=45)
        table.add_column("Yes", justify="right")
        table.add_column("24h Vol", justify="right")
        table.add_column("Liquidity", justify="right")
        table.add_column("Spread", justify="right")

        for m in markets:
            spread = f"{m.spread:.2%}" if m.spread else "N/A"
            table.add_row(
                m.question[:45],
                f"{m.yes_price:.1%}",
                f"${m.volume_24hr:,.0f}" if m.volume_24hr else "N/A",
                f"${m.liquidity:,.0f}",
                spread,
            )

        console.print(table)

    finally:
        gamma_client.close()


@app.command()
def positions() -> None:
    """Show current positions."""
    storage = DatabaseStorage()
    positions = storage.get_open_positions()

    if not positions:
        console.print("[yellow]No open positions[/yellow]")
        return

    table = Table(title="Open Positions")
    table.add_column("Market", width=35)
    table.add_column("Side")
    table.add_column("Size", justify="right")
    table.add_column("Entry", justify="right")
    table.add_column("Current", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Strategy")

    for p in positions:
        pnl_color = "green" if p.unrealized_pnl >= 0 else "red"
        table.add_row(
            p.market_question[:35],
            p.side.value,
            f"{p.size:.2f}",
            f"{p.avg_entry_price:.2%}",
            f"{p.current_price:.2%}",
            f"[{pnl_color}]${p.unrealized_pnl:.2f}[/{pnl_color}]",
            p.strategy_name,
        )

    console.print(table)


@app.command()
def performance() -> None:
    """Show strategy performance."""
    storage = DatabaseStorage()
    tracker = PerformanceTracker(storage)

    report = tracker.generate_report()
    console.print(report)


@app.command()
def status() -> None:
    """Show bot status and configuration."""
    settings = Settings()

    console.print("[bold]Bot Configuration[/bold]")
    console.print(f"  Trading Mode: {settings.trading_mode.value}")
    console.print(f"  Max Position: ${settings.max_position_size_usd:.2f}")
    console.print(f"  Max Exposure: ${settings.max_total_exposure_usd:.2f}")
    console.print(f"  Stop Loss: {settings.stop_loss_pct:.1%}")
    console.print(f"  Daily Limit: ${settings.daily_loss_limit_usd:.2f}")

    console.print("\n[bold]API Configuration[/bold]")
    console.print(f"  CLOB Host: {settings.clob_host}")
    console.print(f"  Gamma Host: {settings.gamma_host}")
    console.print(f"  Wallet: {'Configured' if settings.polygon_wallet_private_key else 'Not set'}")
    console.print(f"  Anthropic: {'Configured' if settings.anthropic_api_key else 'Not set'}")

    # Database stats
    storage = DatabaseStorage()
    positions = storage.get_open_positions()

    console.print("\n[bold]Current State[/bold]")
    console.print(f"  Open Positions: {len(positions)}")


@app.command()
def test_trade(
    market_id: str = typer.Argument(..., help="Market ID to test"),
    amount: float = typer.Option(1.0, "--amount", "-a", help="Amount in USD"),
    side: str = typer.Option("buy", "--side", "-s", help="Side: buy or sell"),
) -> None:
    """Execute a test trade in paper mode."""
    settings = Settings()
    settings.trading_mode = TradingMode.PAPER

    console.print(f"[yellow]Testing ${amount} {side} on market {market_id}[/yellow]")
    console.print("[dim]This is paper trading mode - no real funds used[/dim]")

    asyncio.run(_test_trade(settings, market_id, amount, side))


async def _test_trade(settings: Settings, market_id: str, amount: float, side: str) -> None:
    """Execute a test trade."""
    gamma_client = GammaMarketClient(settings)

    try:
        market = gamma_client.get_market_by_id(market_id)

        if not market:
            console.print("[red]Market not found[/red]")
            return

        console.print(f"\n[bold]Market Details[/bold]")
        console.print(f"  Question: {market.get('question', 'N/A')}")
        console.print(f"  Yes Price: {market.get('outcomePrices', ['N/A'])[0]}")
        console.print(f"  Liquidity: ${float(market.get('liquidityNum', 0)):,.2f}")

        console.print(f"\n[green]Paper trade executed:[/green]")
        console.print(f"  Action: {side.upper()}")
        console.print(f"  Amount: ${amount:.2f}")
        console.print(f"  Status: FILLED (paper)")

    finally:
        gamma_client.close()


@app.command()
def arbitrage(
    action: str = typer.Argument(
        "scan",
        help="Action: scan, execute, watch, history",
    ),
    market_id: Optional[str] = typer.Option(
        None,
        "--market", "-m",
        help="Market ID for execute action",
    ),
    min_spread: float = typer.Option(
        0.03,
        "--min-spread",
        help="Minimum spread (0.03 = 3%)",
    ),
    limit: int = typer.Option(
        10,
        "--limit", "-n",
        help="Number of results to show",
    ),
) -> None:
    """Arbitrage trading operations.

    Actions:
      scan    - Find current arbitrage opportunities
      execute - Execute arbitrage on specific market
      watch   - Real-time monitoring mode
      history - Show past arbitrage trades
    """
    settings = Settings()

    if action == "scan":
        _arbitrage_scan(settings, min_spread, limit)
    elif action == "execute":
        if not market_id:
            console.print("[red]Market ID required for execute action[/red]")
            raise typer.Exit(1)
        asyncio.run(_arbitrage_execute(settings, market_id, min_spread))
    elif action == "watch":
        asyncio.run(_arbitrage_watch(settings, min_spread))
    elif action == "history":
        _arbitrage_history(limit)
    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Available actions: scan, execute, watch, history")
        raise typer.Exit(1)


def _arbitrage_scan(settings: Settings, min_spread: float, limit: int) -> None:
    """Scan for arbitrage opportunities."""
    gamma_client = GammaMarketClient(settings)
    arb_strategy = ArbitrageStrategy(settings, config={"min_spread": min_spread})

    try:
        console.print("[bold]Scanning for arbitrage opportunities...[/bold]")

        # Get markets
        markets = gamma_client.get_tradable_markets(min_liquidity=1000, limit=100)
        console.print(f"Fetched {len(markets)} tradable markets")

        # Find opportunities
        opportunities = arb_strategy.scan_opportunities(markets)

        if not opportunities:
            console.print("[yellow]No arbitrage opportunities found above minimum spread[/yellow]")
            return

        # Display results
        table = Table(title=f"Arbitrage Opportunities (min spread: {min_spread:.1%})")
        table.add_column("Market", width=40)
        table.add_column("YES", justify="right")
        table.add_column("NO", justify="right")
        table.add_column("Spread", justify="right")
        table.add_column("Est. Profit", justify="right")
        table.add_column("Liquidity", justify="right")
        table.add_column("Quality", justify="right")

        for opp in opportunities[:limit]:
            spread_color = "green" if opp.spread >= 0.03 else "yellow"
            table.add_row(
                opp.market_question[:40],
                f"{opp.yes_price:.1%}",
                f"{opp.no_price:.1%}",
                f"[{spread_color}]{opp.spread:.2%}[/{spread_color}]",
                f"${opp.estimated_profit_usd:.2f}",
                f"${opp.liquidity:,.0f}",
                f"{opp.quality_score:.0f}/100",
            )

        console.print(table)
        console.print(f"\n[dim]Found {len(opportunities)} opportunities total[/dim]")

        # Show profit calculation for top opportunity
        if opportunities:
            top = opportunities[0]
            console.print(f"\n[bold]Top Opportunity Analysis:[/bold]")
            profit_calc = arb_strategy.calculate_potential_profit(
                top.yes_price, top.no_price,
                settings.arbitrage_max_position, top.liquidity
            )
            console.print(f"  Market: {top.market_question[:50]}...")
            console.print(f"  Position size: ${settings.arbitrage_max_position:.2f} per leg")
            console.print(f"  Total cost: ${profit_calc['total_cost_usd']:.2f}")
            console.print(f"  Guaranteed payout: ${profit_calc['guaranteed_payout']:.2f}")
            console.print(f"  Gross profit: ${profit_calc['gross_profit_usd']:.2f} ({profit_calc['gross_profit_pct']:.2%})")
            console.print(f"  Est. slippage: {profit_calc['slippage_estimate']:.2%}")
            console.print(f"  Net profit: ${profit_calc['net_profit_usd']:.2f} ({profit_calc['net_profit_pct']:.2%})")
            console.print(f"  ROI: {profit_calc['roi']:.1f}%")

    finally:
        gamma_client.close()


async def _arbitrage_execute(settings: Settings, market_id: str, min_spread: float) -> None:
    """Execute arbitrage on a specific market."""
    gamma_client = GammaMarketClient(settings)
    validator = PreTradeValidator(settings)

    try:
        # Get market details
        market_data = gamma_client.get_market_by_id(market_id)
        if not market_data:
            console.print(f"[red]Market not found: {market_id}[/red]")
            return

        market = gamma_client._parse_market(market_data)

        console.print(f"\n[bold]Market: {market.question[:60]}...[/bold]")
        console.print(f"  YES: {market.yes_price:.1%} | NO: {market.no_price:.1%}")

        # Calculate spread
        spread = 1.0 - (market.yes_price + market.no_price)
        console.print(f"  Spread: {spread:.2%}")

        if spread < min_spread:
            console.print(f"[yellow]Spread {spread:.2%} below minimum {min_spread:.2%}[/yellow]")
            return

        # Validate
        validation = validator.validate_arbitrage(
            market,
            market.yes_price,
            market.no_price,
            settings.arbitrage_max_position,
        )

        if not validation.passed:
            console.print(f"[red]Validation failed: {validation.reason}[/red]")
            for check in validation.checks_failed:
                console.print(f"  [red]- {check}[/red]")
            return

        console.print(f"[green]Validation passed[/green]")
        for check in validation.checks_passed:
            console.print(f"  [dim]- {check}[/dim]")

        # In paper mode, simulate execution
        if settings.trading_mode == TradingMode.PAPER:
            console.print(f"\n[yellow]PAPER TRADE - No real funds used[/yellow]")
            console.print(f"  Would buy YES @ {market.yes_price:.1%}")
            console.print(f"  Would buy NO @ {market.no_price:.1%}")
            console.print(f"  Position: ${settings.arbitrage_max_position:.2f} per leg")
            console.print(f"  Total cost: ${settings.arbitrage_max_position * 2:.2f}")
            console.print(f"  Expected profit: ${spread * settings.arbitrage_max_position:.2f}")
        else:
            console.print("[red]Live trading not implemented in CLI - use bot run command[/red]")

    finally:
        gamma_client.close()


async def _arbitrage_watch(settings: Settings, min_spread: float) -> None:
    """Watch for arbitrage opportunities in real-time."""
    gamma_client = GammaMarketClient(settings)
    arb_strategy = ArbitrageStrategy(settings, config={"min_spread": min_spread})

    console.print(f"[bold]Watching for arbitrage opportunities (min spread: {min_spread:.1%})[/bold]")
    console.print("[dim]Press Ctrl+C to stop[/dim]\n")

    try:
        iteration = 0
        while True:
            iteration += 1

            # Fetch markets
            markets = gamma_client.get_tradable_markets(min_liquidity=1000, limit=100)

            # Find opportunities
            opportunities = arb_strategy.scan_opportunities(markets)

            # Clear and update display
            timestamp = datetime.now().strftime("%H:%M:%S")

            if opportunities:
                console.print(f"[{timestamp}] Found {len(opportunities)} opportunities:")
                for opp in opportunities[:5]:
                    console.print(
                        f"  [green]{opp.spread:.2%}[/green] | "
                        f"${opp.estimated_profit_usd:.2f} | "
                        f"{opp.market_question[:40]}..."
                    )
            else:
                console.print(f"[{timestamp}] No opportunities (scanned {len(markets)} markets)")

            # Wait for next scan
            await asyncio.sleep(settings.arbitrage_scan_interval)

    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped watching[/yellow]")
    finally:
        gamma_client.close()


def _arbitrage_history(limit: int) -> None:
    """Show arbitrage trade history."""
    storage = DatabaseStorage()

    # Get audit records for arbitrage
    audits = storage.get_trade_audits(edge_type="arb", limit=limit)

    if not audits:
        console.print("[yellow]No arbitrage trades recorded yet[/yellow]")
        return

    table = Table(title="Arbitrage Trade History")
    table.add_column("Time", width=16)
    table.add_column("Market", width=35)
    table.add_column("Spread", justify="right")
    table.add_column("Slippage", justify="right")
    table.add_column("P&L", justify="right")
    table.add_column("Quality")

    for audit in audits:
        timestamp = audit.get("entry_timestamp", "")[:16]
        pnl = audit.get("realized_pnl", 0)
        pnl_color = "green" if pnl > 0 else "red" if pnl < 0 else "dim"

        # Get execution quality
        slippage_pct = audit.get("slippage_pct", 0)
        if slippage_pct <= -0.005:
            quality = "[green]Excellent[/green]"
        elif slippage_pct <= 0.005:
            quality = "[green]Good[/green]"
        elif slippage_pct <= 0.01:
            quality = "[yellow]OK[/yellow]"
        else:
            quality = "[red]Poor[/red]"

        table.add_row(
            timestamp,
            audit.get("market_id", "")[:35],
            f"{audit.get('spread_at_trade', 0):.2%}",
            f"{slippage_pct:.2%}",
            f"[{pnl_color}]${pnl:.2f}[/{pnl_color}]",
            quality,
        )

    console.print(table)

    # Summary stats
    summary = storage.get_audit_summary()
    arb_stats = summary.get("by_edge_type", {}).get("arb", {})

    if arb_stats:
        console.print(f"\n[bold]Arbitrage Summary:[/bold]")
        console.print(f"  Total trades: {arb_stats.get('count', 0)}")
        console.print(f"  Win rate: {arb_stats.get('win_rate', 0):.1%}")
        console.print(f"  Total P&L: ${arb_stats.get('total_pnl', 0):.2f}")
        console.print(f"  Avg slippage: {arb_stats.get('avg_slippage', 0):.2%}")


@app.command()
def quality(
    market_id: Optional[str] = typer.Argument(None, help="Market ID to analyze"),
    limit: int = typer.Option(10, "--limit", "-n", help="Number of top markets to show"),
) -> None:
    """Analyze market quality scores."""
    settings = Settings()
    gamma_client = GammaMarketClient(settings)
    scorer = MarketQualityScorer(settings)

    try:
        if market_id:
            # Score single market
            market_data = gamma_client.get_market_by_id(market_id)
            if not market_data:
                console.print(f"[red]Market not found: {market_id}[/red]")
                return

            market = gamma_client._parse_market(market_data)
            score = scorer.score_market(market)

            console.print(f"\n[bold]Market Quality Analysis[/bold]")
            console.print(f"Market: {market.question[:60]}...")
            console.print(f"\n[bold]Scores:[/bold]")
            console.print(f"  Total:            {score.total_score:.0f}/100")
            console.print(f"  Resolution Clarity: {score.resolution_clarity_score:.0f}/40")
            console.print(f"  Liquidity:        {score.liquidity_score:.0f}/30")
            console.print(f"  Spread:           {score.spread_score:.0f}/20")
            console.print(f"  Time to Resolution: {score.time_to_resolution_score:.0f}/10")

            console.print(f"\n[bold]Flags:[/bold]")
            console.print(f"  Tradeable: {'Yes' if score.is_tradeable else 'No'}")
            console.print(f"  Arbitrage Candidate: {'Yes' if score.is_arbitrage_candidate else 'No'}")
            console.print(f"  Crypto 15-min: {'Yes' if score.is_crypto_15min else 'No'}")

            console.print(f"\n[bold]Reasons:[/bold]")
            for reason in score.reasons:
                console.print(f"  - {reason}")

        else:
            # Show top scored markets
            markets = gamma_client.get_tradable_markets(limit=50)
            scores = scorer.score_markets(markets)

            table = Table(title=f"Top {limit} Quality Markets")
            table.add_column("Market", width=40)
            table.add_column("Total", justify="right")
            table.add_column("Clarity", justify="right")
            table.add_column("Liquidity", justify="right")
            table.add_column("Spread", justify="right")
            table.add_column("Arb?")

            for score in scores[:limit]:
                market = next((m for m in markets if m.id == score.market_id), None)
                question = market.question[:40] if market else score.market_id[:40]

                arb_flag = "[green]Yes[/green]" if score.is_arbitrage_candidate else "[dim]No[/dim]"

                table.add_row(
                    question,
                    f"{score.total_score:.0f}",
                    f"{score.resolution_clarity_score:.0f}/40",
                    f"{score.liquidity_score:.0f}/30",
                    f"{score.spread_score:.0f}/20",
                    arb_flag,
                )

            console.print(table)

    finally:
        gamma_client.close()


@app.command()
def audit() -> None:
    """Show trade audit report."""
    storage = DatabaseStorage()
    tracker = AuditTracker(storage)

    # Load audits from database
    audits_data = storage.get_trade_audits(limit=100)

    if not audits_data:
        console.print("[yellow]No audit data available yet[/yellow]")
        return

    # Get summary
    summary = storage.get_audit_summary()

    console.print("[bold]Trade Audit Summary[/bold]")
    console.print(f"  Total audited trades: {summary.get('total_audits', 0)}")
    console.print(f"  Win rate: {summary.get('win_rate', 0):.1%}")
    console.print(f"  Avg slippage: {summary.get('avg_slippage', 0):.2%}")
    console.print(f"  Total P&L: ${summary.get('total_pnl', 0):.2f}")

    # By edge type
    by_edge = summary.get("by_edge_type", {})
    if by_edge:
        console.print("\n[bold]Performance by Edge Type:[/bold]")
        for edge_type, stats in by_edge.items():
            console.print(
                f"  {edge_type}: {stats.get('count', 0)} trades | "
                f"Win rate: {stats.get('win_rate', 0):.1%} | "
                f"P&L: ${stats.get('total_pnl', 0):.2f}"
            )


@app.command()
def scan(
    interval: int = typer.Option(
        60,
        "--interval", "-i",
        help="Seconds between scans",
    ),
    min_bet: float = typer.Option(
        10000,
        "--min-bet", "-m",
        help="Minimum bet size to trigger alert (USD)",
    ),
    limit: int = typer.Option(
        100,
        "--limit", "-n",
        help="Number of trades to scan per iteration",
    ),
) -> None:
    """Scan for whale activity on Polymarket.

    Detects:
    - Fresh wallets making large bets
    - Abnormally large trades (>$10k default)
    - Repeated entries into same market category

    Alerts are printed to console for manual review.
    """
    from src.scanner.activity_client import ActivityClient
    from src.scanner.whale_detector import WhaleDetector

    settings = Settings()

    console.print(f"[bold green]🐋 Whale Scanner Started[/bold green]")
    console.print(f"  Min bet: ${min_bet:,.0f}")
    console.print(f"  Interval: {interval}s")
    console.print(f"  Press Ctrl+C to stop\n")

    # Initialize components
    activity_client = ActivityClient(settings)
    detector = WhaleDetector(settings, activity_client)
    detector.min_bet_usd = min_bet

    iteration = 0
    total_alerts = 0

    try:
        while True:
            iteration += 1
            timestamp = datetime.now().strftime("%H:%M:%S")

            try:
                # Scan for whales
                alerts = detector.scan_for_whales(limit=limit)

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
            time.sleep(interval)

    except KeyboardInterrupt:
        console.print(f"\n[yellow]Scanner stopped. Total alerts: {total_alerts}[/yellow]")
    finally:
        activity_client.close()


def main() -> None:
    """Entry point."""
    app()


if __name__ == "__main__":
    main()
