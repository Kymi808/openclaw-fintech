"""
Metrics collection for monitoring and alerting.
Exposes Prometheus-compatible metrics via a simple HTTP endpoint.
"""
import time
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional
from http.server import HTTPServer, BaseHTTPRequestHandler

from .config import get_logger

logger = get_logger("metrics")


@dataclass
class Counter:
    name: str
    help: str
    labels: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def inc(self, label: str = "", value: float = 1.0):
        self.labels[label] += value

    def get(self, label: str = "") -> float:
        return self.labels[label]


@dataclass
class Gauge:
    name: str
    help: str
    labels: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def set(self, value: float, label: str = ""):
        self.labels[label] = value

    def get(self, label: str = "") -> float:
        return self.labels[label]


@dataclass
class Histogram:
    name: str
    help: str
    buckets: list[float] = field(default_factory=lambda: [
        0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
    ])
    _observations: list[float] = field(default_factory=list)
    _sum: float = 0.0
    _count: int = 0

    def observe(self, value: float):
        self._observations.append(value)
        self._sum += value
        self._count += 1

    @property
    def count(self) -> int:
        return self._count

    @property
    def sum(self) -> float:
        return self._sum

    def bucket_counts(self) -> dict[float, int]:
        counts = {}
        for b in self.buckets:
            counts[b] = sum(1 for o in self._observations if o <= b)
        counts[float("inf")] = self._count
        return counts


class MetricsRegistry:
    """Central metrics registry."""

    def __init__(self):
        self._counters: dict[str, Counter] = {}
        self._gauges: dict[str, Gauge] = {}
        self._histograms: dict[str, Histogram] = {}
        self._init_fintech_metrics()

    def _init_fintech_metrics(self):
        """Pre-register all fintech-specific metrics."""
        # Trading
        self.counter("trades_total", "Total number of trades attempted")
        self.counter("trades_executed", "Total trades successfully executed")
        self.counter("trades_rejected", "Total trades rejected by risk checks")
        self.counter("trades_approval_requested", "Trades requiring approval")
        self.gauge("daily_volume_usd", "Daily trading volume in USD")
        self.gauge("open_positions_count", "Number of open positions")
        self.histogram("trade_latency_seconds", "Trade execution latency")

        # Portfolio
        self.gauge("portfolio_value_usd", "Total portfolio value")
        self.gauge("portfolio_drift_max_pct", "Maximum allocation drift")
        self.counter("rebalances_total", "Total rebalance operations")

        # DeFi
        self.gauge("defi_total_value_usd", "Total DeFi positions value")
        self.gauge("gas_price_gwei", "Current gas price in gwei")
        self.counter("swaps_total", "Total swap operations")
        self.counter("governance_votes", "Governance votes cast")

        # Finance
        self.counter("receipts_processed", "Total receipts processed")
        self.counter("expenses_total", "Total expenses recorded")
        self.gauge("monthly_spend_usd", "Current month total spend")
        self.gauge("budget_utilization_pct", "Budget utilization percentage")

        # Legal
        self.counter("contracts_analyzed", "Total contracts analyzed")
        self.counter("sec_filings_detected", "SEC filings detected")
        self.counter("gdpr_scans_total", "GDPR scans performed")
        self.gauge("gdpr_issues_open", "Open GDPR compliance issues")
        self.counter("contract_renewal_alerts", "Contract renewal alerts sent")

        # System
        self.counter("approval_requests", "Total approval requests")
        self.counter("approval_granted", "Approvals granted")
        self.counter("approval_denied", "Approvals denied")
        self.counter("errors_total", "Total errors across all agents")
        self.histogram("api_latency_seconds", "External API call latency")
        self.gauge("circuit_breaker_state", "Circuit breaker state (0=closed, 1=half, 2=open)")
        self.counter("rbac_denials", "RBAC permission denials")
        self.counter("heartbeats_total", "Total heartbeat executions")

    def counter(self, name: str, help_text: str = "") -> Counter:
        if name not in self._counters:
            self._counters[name] = Counter(name=name, help=help_text)
        return self._counters[name]

    def gauge(self, name: str, help_text: str = "") -> Gauge:
        if name not in self._gauges:
            self._gauges[name] = Gauge(name=name, help=help_text)
        return self._gauges[name]

    def histogram(self, name: str, help_text: str = "") -> Histogram:
        if name not in self._histograms:
            self._histograms[name] = Histogram(name=name, help=help_text)
        return self._histograms[name]

    def to_prometheus(self) -> str:
        """Export all metrics in Prometheus text format."""
        lines = []

        for c in self._counters.values():
            lines.append(f"# HELP {c.name} {c.help}")
            lines.append(f"# TYPE {c.name} counter")
            for label, value in c.labels.items():
                label_str = f'{{{label}}}' if label else ""
                lines.append(f"{c.name}{label_str} {value}")

        for g in self._gauges.values():
            lines.append(f"# HELP {g.name} {g.help}")
            lines.append(f"# TYPE {g.name} gauge")
            for label, value in g.labels.items():
                label_str = f'{{{label}}}' if label else ""
                lines.append(f"{g.name}{label_str} {value}")

        for h in self._histograms.values():
            lines.append(f"# HELP {h.name} {h.help}")
            lines.append(f"# TYPE {h.name} histogram")
            for bucket, count in h.bucket_counts().items():
                le = f"+Inf" if bucket == float("inf") else f"{bucket}"
                lines.append(f'{h.name}_bucket{{le="{le}"}} {count}')
            lines.append(f"{h.name}_sum {h.sum}")
            lines.append(f"{h.name}_count {h.count}")

        return "\n".join(lines) + "\n"

    def snapshot(self) -> dict:
        """Get a JSON snapshot of all metrics."""
        return {
            "counters": {
                name: dict(c.labels) for name, c in self._counters.items()
            },
            "gauges": {
                name: dict(g.labels) for name, g in self._gauges.items()
            },
            "histograms": {
                name: {"count": h.count, "sum": h.sum}
                for name, h in self._histograms.items()
            },
        }


# Singleton
metrics = MetricsRegistry()


# === Prometheus HTTP Endpoint ===

class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/metrics":
            body = metrics.to_prometheus().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/health":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # Suppress default logging


def start_metrics_server(port: int = 9090):
    """Start Prometheus metrics HTTP server in a background thread."""
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Metrics server started on :{port}/metrics")
    return server


# === Timing decorator ===

def timed(histogram_name: str, counter_name: str = None):
    """Decorator to measure async function execution time."""
    def decorator(func):
        async def wrapper(*args, **kwargs):
            start = time.time()
            try:
                result = await func(*args, **kwargs)
                elapsed = time.time() - start
                metrics.histogram(histogram_name).observe(elapsed)
                if counter_name:
                    metrics.counter(counter_name).inc(label="success")
                return result
            except Exception as e:
                elapsed = time.time() - start
                metrics.histogram(histogram_name).observe(elapsed)
                if counter_name:
                    metrics.counter(counter_name).inc(label="error")
                metrics.counter("errors_total").inc(label=func.__name__)
                raise
        wrapper.__name__ = func.__name__
        wrapper.__qualname__ = func.__qualname__
        return wrapper
    return decorator
