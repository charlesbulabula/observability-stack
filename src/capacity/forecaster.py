"""
Capacity forecaster — queries Prometheus and predicts resource exhaustion.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
import httpx
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Forecast:
    metric: str
    namespace: str
    current_value: float
    predicted_value: float
    days_until_threshold: Optional[int]
    threshold_pct: float
    confidence: str  # high / medium / low
    data_points: int


class CapacityForecaster:
    def __init__(self, prometheus_url: str) -> None:
        self._url = prometheus_url.rstrip("/")
        self._client = httpx.Client(timeout=30)

    def query_range(self, query: str, start: datetime, end: datetime, step: str = "1h") -> list[tuple[datetime, float]]:
        resp = self._client.get(
            f"{self._url}/api/v1/query_range",
            params={
                "query": query,
                "start": start.timestamp(),
                "end": end.timestamp(),
                "step": step,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("data", {}).get("result", [])
        if not results:
            return []
        series = results[0].get("values", [])
        return [(datetime.fromtimestamp(float(ts)), float(val)) for ts, val in series]

    def forecast_cpu(self, namespace: str, days_ahead: int = 30, threshold_pct: float = 80.0) -> Forecast:
        end = datetime.utcnow()
        start = end - timedelta(days=90)
        query = f'avg(rate(container_cpu_usage_seconds_total{{namespace="{namespace}"}}[5m])) / avg(kube_pod_container_resource_limits{{namespace="{namespace}",resource="cpu"}})'
        series = self.query_range(query, start, end)
        return self._compute_forecast("cpu_utilization_pct", namespace, series, days_ahead, threshold_pct)

    def forecast_memory(self, namespace: str, days_ahead: int = 30, threshold_pct: float = 80.0) -> Forecast:
        end = datetime.utcnow()
        start = end - timedelta(days=90)
        query = f'avg(container_memory_working_set_bytes{{namespace="{namespace}"}}) / avg(kube_pod_container_resource_limits{{namespace="{namespace}",resource="memory"}})'
        series = self.query_range(query, start, end)
        return self._compute_forecast("memory_utilization_pct", namespace, series, days_ahead, threshold_pct)

    def _compute_forecast(
        self,
        metric: str,
        namespace: str,
        series: list[tuple[datetime, float]],
        days_ahead: int,
        threshold_pct: float,
    ) -> Forecast:
        if len(series) < 10:
            return Forecast(metric=metric, namespace=namespace, current_value=0, predicted_value=0,
                            days_until_threshold=None, threshold_pct=threshold_pct, confidence="low", data_points=len(series))
        xs = np.array([(dt - series[0][0]).total_seconds() / 3600 for dt, _ in series])
        ys = np.array([v * 100 for _, v in series])  # convert to pct
        coeffs = np.polyfit(xs, ys, 1)
        slope, intercept = coeffs
        current = float(np.poly1d(coeffs)(xs[-1]))
        future_x = xs[-1] + days_ahead * 24
        predicted = float(np.poly1d(coeffs)(future_x))
        days_until: Optional[int] = None
        if slope > 0 and current < threshold_pct:
            hours_to_threshold = (threshold_pct - current) / slope if slope > 0 else None
            if hours_to_threshold:
                days_until = max(0, int(hours_to_threshold / 24))
        confidence = "high" if len(series) > 500 else ("medium" if len(series) > 100 else "low")
        return Forecast(
            metric=metric, namespace=namespace,
            current_value=round(current, 1), predicted_value=round(predicted, 1),
            days_until_threshold=days_until, threshold_pct=threshold_pct,
            confidence=confidence, data_points=len(series),
        )

    def generate_report(self, namespaces: list[str]) -> str:
        lines = ["| Namespace | Metric | Current | 30d Forecast | Days to Threshold |", "|---|---|---|---|---|"]
        for ns in namespaces:
            for forecast_fn, label in [(self.forecast_cpu, "CPU"), (self.forecast_memory, "Memory")]:
                try:
                    f = forecast_fn(ns)
                    days = f.days_until_threshold
                    days_str = f"{days}d" if days and days < 30 else (str(days) + "d" if days else "N/A")
                    lines.append(f"| {ns} | {label} | {f.current_value:.1f}% | {f.predicted_value:.1f}% | {days_str} |")
                except Exception as e:
                    log.warning("Forecast failed for %s/%s: %s", ns, label, e)
        return "\n".join(lines)

    def close(self) -> None:
        self._client.close()

# _r 20260620145902-67c8a1b4
