"""Tests for metrics collection."""
import pytest
from skills.shared.metrics import MetricsRegistry, Counter, Gauge, Histogram


class TestCounter:
    def test_increment(self):
        c = Counter(name="test", help="test counter")
        c.inc()
        assert c.get() == 1.0
        c.inc(value=5.0)
        assert c.get() == 6.0

    def test_labels(self):
        c = Counter(name="test", help="test")
        c.inc(label="success")
        c.inc(label="error")
        c.inc(label="success")
        assert c.get("success") == 2.0
        assert c.get("error") == 1.0


class TestGauge:
    def test_set(self):
        g = Gauge(name="test", help="test gauge")
        g.set(42.0)
        assert g.get() == 42.0
        g.set(0.0)
        assert g.get() == 0.0


class TestHistogram:
    def test_observations(self):
        h = Histogram(name="test", help="test histogram")
        h.observe(0.1)
        h.observe(0.5)
        h.observe(2.0)
        assert h.count == 3
        assert h.sum == pytest.approx(2.6)

    def test_bucket_counts(self):
        h = Histogram(name="test", help="test", buckets=[0.1, 1.0, 10.0])
        h.observe(0.05)  # in 0.1 bucket
        h.observe(0.5)   # in 1.0 bucket
        h.observe(5.0)   # in 10.0 bucket
        h.observe(50.0)  # only in +Inf

        buckets = h.bucket_counts()
        assert buckets[0.1] == 1
        assert buckets[1.0] == 2
        assert buckets[10.0] == 3
        assert buckets[float("inf")] == 4


class TestMetricsRegistry:
    def test_fintech_metrics_registered(self):
        registry = MetricsRegistry()

        # Verify key metrics exist
        assert registry.counter("trades_total") is not None
        assert registry.gauge("portfolio_value_usd") is not None
        assert registry.histogram("trade_latency_seconds") is not None
        assert registry.counter("sec_filings_detected") is not None

    def test_prometheus_export(self):
        registry = MetricsRegistry()
        registry.counter("trades_total").inc(label="success")
        registry.gauge("portfolio_value_usd").set(50000.0)

        output = registry.to_prometheus()
        assert "trades_total" in output
        assert "portfolio_value_usd" in output
        assert "50000.0" in output

    def test_snapshot(self):
        registry = MetricsRegistry()
        registry.counter("trades_total").inc(label="success", value=5)
        registry.gauge("portfolio_value_usd").set(75000.0)

        snap = registry.snapshot()
        assert "counters" in snap
        assert "gauges" in snap
        assert "histograms" in snap
