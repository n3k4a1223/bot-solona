"""
Asynchronous Terminal Logging and Live Monitoring Dashboard
===========================================================
Leverages Rich for non-blocking, multi-panel terminal visualization,
displaying live token metrics, dynamic position sizing calculations,
order book liquidity depth, and Solscan execution links.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Dict, List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.types import (
    ComprehensiveRiskScore,
    MomentumMetrics,
    OpenPosition,
    PositionSizeRecommendation,
)


class AsyncBotLogger:
    """Non-blocking terminal logger and telemetry dashboard manager."""

    def __init__(self, dry_run: bool = True):
        self.console = Console(highlight=False)
        self.dry_run = dry_run
        self._log_queue: asyncio.Queue[str] = asyncio.Queue()
        self._recent_logs: List[str] = []
        self._max_recent_logs: int = 12
        self._running: bool = False
        self._worker_task: Optional[asyncio.Task] = None

        # Live telemetry state for dashboard rendering
        self.liquid_balance_sol: float = 0.0
        self.peak_equity_sol: float = 0.0
        self.current_drawdown_pct: float = 0.0
        self.cluster_tps: float = 0.0
        self.cluster_fail_rate_pct: float = 0.0
        self.is_cluster_congested: bool = False
        self.active_positions: Dict[str, OpenPosition] = {}
        self.monitored_tokens: Dict[str, Dict] = {}

    def log_info(self, message: str) -> None:
        """Format and enqueue informational message."""
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[dim]{timestamp}[/] [bold blue]INFO[/]  {message}"
        self._enqueue(formatted)

    def log_success(self, message: str) -> None:
        """Format and enqueue success notification."""
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[dim]{timestamp}[/] [bold green]SUCCESS[/] {message}"
        self._enqueue(formatted)

    def log_warning(self, message: str) -> None:
        """Format and enqueue warning notification."""
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[dim]{timestamp}[/] [bold yellow]WARN[/]  {message}"
        self._enqueue(formatted)

    def log_error(self, message: str) -> None:
        """Format and enqueue error alert."""
        timestamp = time.strftime("%H:%M:%S")
        formatted = f"[dim]{timestamp}[/] [bold red]ERROR[/] {message}"
        self._enqueue(formatted)

    def log_trade(
        self,
        action: str,
        token_mint: str,
        amount_sol: float,
        price_sol: float,
        signature: Optional[str] = None,
        notes: str = "",
    ) -> None:
        """Log trade execution with explorer link."""
        timestamp = time.strftime("%H:%M:%S")
        mint_short = f"{token_mint[:4]}...{token_mint[-4:]}"
        color = "green" if "BUY" in action else "yellow" if "SCALE" in action else "magenta"
        sig_str = (
            f"| [link=https://solscan.io/tx/{signature}]Solscan: {signature[:8]}...[/link]"
            if signature
            else "(Simulated)"
        )
        mode_tag = "[cyan][DRY-RUN][/cyan] " if self.dry_run else "[bold red][LIVE][/bold red] "
        formatted = (
            f"[dim]{timestamp}[/] {mode_tag}[bold {color}]{action:<9}[/] "
            f"Token: [bold cyan]{mint_short}[/] | Amt: [bold white]{amount_sol:.4f} SOL[/] "
            f"| Price: [white]{price_sol:.8f}[/] {sig_str} | {notes}"
        )
        self._enqueue(formatted)

    def log_filter_audit(
        self,
        token_mint: str,
        risk: ComprehensiveRiskScore,
        momentum: Optional[MomentumMetrics] = None,
    ) -> None:
        """Log granular multi-tier anti-scam and momentum verification results."""
        timestamp = time.strftime("%H:%M:%S")
        mint_short = f"{token_mint[:4]}...{token_mint[-4:]}"
        status_tag = (
            "[bold green]PASS[/bold green]"
            if risk.passed_all_filters
            else "[bold red]FAIL[/bold red]"
        )
        score_color = (
            "green" if risk.composite_score >= 80 else "yellow" if risk.composite_score >= 60 else "red"
        )

        details = (
            f"Score: [{score_color}]{risk.composite_score:.1f}/100[/] | "
            f"LP/MC: [white]{risk.liquidity_metrics.lp_to_mc_ratio*100:.1f}%[/] | "
            f"Entropy: [white]{risk.holder_distribution.shannon_entropy:.2f}[/] | "
            f"Top10: [white]{risk.holder_distribution.top10_aggregate_pct:.1f}%[/]"
        )
        if momentum:
            details += (
                f" | Inflow: [magenta]{momentum.unique_buyers_count} buyers ({momentum.buyer_growth_percentile:.1f}%ile)[/]"
            )

        if not risk.passed_all_filters and risk.rejection_reasons:
            reason = risk.rejection_reasons[0]
            details += f" | Reason: [bold red]{reason}[/]"

        formatted = (
            f"[dim]{timestamp}[/] [bold magenta]AUDIT[/] Token: [bold cyan]{mint_short}[/] "
            f"[{status_tag}] {details}"
        )
        self._enqueue(formatted)

    def _enqueue(self, message: str) -> None:
        """Enqueues message and prints immediately if worker isn't running."""
        self._recent_logs.append(message)
        if len(self._recent_logs) > self._max_recent_logs:
            self._recent_logs.pop(0)

        # Print directly to console for real-time visibility
        self.console.print(message)

    def print_banner(self) -> None:
        """Display stylish ASCII boot banner."""
        banner_text = Text()
        banner_text.append("=" * 78 + "\n", style="bold cyan")
        banner_text.append("   QUANTITATIVE SOLANA ADAPTIVE HIGH-FREQUENCY TRADING SYSTEM\n", style="bold white")
        banner_text.append("   Architecture: Multi-RPC WebSocket | Jupiter v6 | Jito MEV Bundles\n", style="bold cyan")
        banner_text.append("   Mathematical Sizing: Modified Fractional Kelly Criterion & ATR Bands\n", style="dim white")
        mode_str = "[DRY-RUN / PAPER TRADING]" if self.dry_run else "[LIVE MAINNET CAPITAL AT RISK]"
        mode_color = "bold yellow" if self.dry_run else "bold red"
        banner_text.append(f"   Execution Mode: {mode_str}\n", style=mode_color)
        banner_text.append("=" * 78, style="bold cyan")
        self.console.print(banner_text)

    def render_dashboard_panel(self) -> Panel:
        """Render consolidated status panel."""
        table = Table.grid(expand=True)
        table.add_column(ratio=1)
        table.add_column(ratio=1)
        table.add_column(ratio=1)

        congestion_status = (
            "[bold red]CONGESTED (Throttled)[/]"
            if self.is_cluster_congested
            else "[bold green]OPTIMAL[/]"
        )
        table.add_row(
            f"Balance: [bold green]{self.liquid_balance_sol:.3f} SOL[/]",
            f"Peak Equity: [bold white]{self.peak_equity_sol:.3f} SOL[/]",
            f"Drawdown: [bold yellow]{self.current_drawdown_pct:.2f}%[/]",
        )
        table.add_row(
            f"Cluster TPS: [bold cyan]{self.cluster_tps:.0f}[/]",
            f"Cluster Fail Rate: [bold white]{self.cluster_fail_rate_pct:.1f}%[/]",
            f"Cluster Health: {congestion_status}",
        )
        table.add_row(
            f"Active Positions: [bold magenta]{len(self.active_positions)}[/]",
            f"Monitored Pools: [bold blue]{len(self.monitored_tokens)}[/]",
            f"Execution Mode: {'[yellow]SIMULATED[/]' if self.dry_run else '[red]LIVE[/]'}",
        )
        return Panel(table, title="[bold cyan]System Telemetry & Risk Overview[/]", border_style="cyan")


# Global singleton instance
_logger_instance: Optional[AsyncBotLogger] = None


def get_logger(dry_run: bool = True) -> AsyncBotLogger:
    """Singleton getter for AsyncBotLogger."""
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = AsyncBotLogger(dry_run=dry_run)
    return _logger_instance
