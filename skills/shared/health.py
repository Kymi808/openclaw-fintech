"""
Health checks and liveness probes for all services.
Exposes a /health endpoint and checks dependencies.
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional
import httpx

from .config import get_logger
from .resilience import binance_circuit, coinbase_circuit, alchemy_circuit

logger = get_logger("health")


@dataclass
class HealthStatus:
    service: str
    healthy: bool
    latency_ms: float
    message: str
    checked_at: float


class HealthChecker:
    """Checks health of all dependencies."""

    def __init__(self):
        self._last_results: dict[str, HealthStatus] = {}

    async def check_ollama(self) -> HealthStatus:
        """Check if local Ollama LLM is running."""
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get("http://localhost:11434/api/tags")
                resp.raise_for_status()
                models = resp.json().get("models", [])
                return HealthStatus(
                    service="ollama",
                    healthy=True,
                    latency_ms=(time.time() - start) * 1000,
                    message=f"OK — {len(models)} models loaded",
                    checked_at=time.time(),
                )
        except Exception as e:
            return HealthStatus(
                service="ollama",
                healthy=False,
                latency_ms=(time.time() - start) * 1000,
                message=f"UNHEALTHY: {e}",
                checked_at=time.time(),
            )

    async def check_exchange(self, name: str, url: str) -> HealthStatus:
        """Check if an exchange API is reachable."""
        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                return HealthStatus(
                    service=name,
                    healthy=True,
                    latency_ms=(time.time() - start) * 1000,
                    message="OK",
                    checked_at=time.time(),
                )
        except Exception as e:
            return HealthStatus(
                service=name,
                healthy=False,
                latency_ms=(time.time() - start) * 1000,
                message=f"UNHEALTHY: {e}",
                checked_at=time.time(),
            )

    async def check_database(self) -> HealthStatus:
        """Check if SQLite database is accessible."""
        start = time.time()
        try:
            from .database import db
            db._get_connection().execute("SELECT 1").fetchone()
            return HealthStatus(
                service="database",
                healthy=True,
                latency_ms=(time.time() - start) * 1000,
                message="OK",
                checked_at=time.time(),
            )
        except Exception as e:
            return HealthStatus(
                service="database",
                healthy=False,
                latency_ms=(time.time() - start) * 1000,
                message=f"UNHEALTHY: {e}",
                checked_at=time.time(),
            )

    async def check_all(self) -> dict:
        """Run all health checks concurrently."""
        checks = await asyncio.gather(
            self.check_ollama(),
            self.check_exchange("binance", "https://api.binance.com/api/v3/ping"),
            self.check_exchange("coinbase", "https://api.coinbase.com/v2/time"),
            self.check_database(),
            return_exceptions=True,
        )

        results = {}
        for check in checks:
            if isinstance(check, HealthStatus):
                results[check.service] = {
                    "healthy": check.healthy,
                    "latency_ms": round(check.latency_ms, 1),
                    "message": check.message,
                }
                self._last_results[check.service] = check
            elif isinstance(check, Exception):
                logger.error(f"Health check error: {check}")

        # Add circuit breaker states
        results["circuits"] = {
            "binance": binance_circuit.stats,
            "coinbase": coinbase_circuit.stats,
            "alchemy": alchemy_circuit.stats,
        }

        overall_healthy = all(
            r.get("healthy", False) for k, r in results.items()
            if k != "circuits"
        )

        return {
            "status": "healthy" if overall_healthy else "degraded",
            "services": results,
            "timestamp": time.time(),
        }

    def format_report(self, results: dict) -> str:
        """Format health check results for messaging."""
        status_icon = "✅" if results["status"] == "healthy" else "⚠️"
        lines = [f"{status_icon} System Health: {results['status'].upper()}"]

        for service, info in results.get("services", {}).items():
            if service == "circuits":
                continue
            icon = "✅" if info.get("healthy") else "❌"
            lines.append(
                f"  {icon} {service}: {info.get('message', 'unknown')} "
                f"({info.get('latency_ms', 0):.0f}ms)"
            )

        # Circuit breakers
        circuits = results.get("services", {}).get("circuits", {})
        if circuits:
            lines.append("  Circuit breakers:")
            for name, stats in circuits.items():
                state = stats.get("state", "unknown")
                icon = "✅" if state == "closed" else ("🟡" if state == "half_open" else "🔴")
                lines.append(f"    {icon} {name}: {state}")

        return "\n".join(lines)


# Singleton
health_checker = HealthChecker()
