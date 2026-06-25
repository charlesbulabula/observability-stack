"""Prometheus cardinality analyzer — identifies high-cardinality label sets
that inflate TSDB storage and query latency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

PROMETHEUS_URL_DEFAULT = "http://prometheus:9090"
HIGH_CARDINALITY_THRESHOLD = 10_000
CRITICAL_THRESHOLD = 100_000


@dataclass
class MetricCardinality:
    name: str
    series_count: int
    labels: List[str] = field(default_factory=list)
    top_label: Optional[str] = None
    top_label_values: int = 0

    @property
    def severity(self) -> str:
        if self.series_count >= CRITICAL_THRESHOLD:
            return "critical"
        if self.series_count >= HIGH_CARDINALITY_THRESHOLD:
            return "high"
        return "normal"


@dataclass
class CardinalityReport:
    total_series: int
    metrics: List[MetricCardinality]
    recommendations: List[str]

    @property
    def offenders(self) -> List[MetricCardinality]:
        return [m for m in self.metrics if m.severity != "normal"]


class CardinalityAnalyzer:
    """Queries Prometheus TSDB status endpoint and ranks metrics by series count."""

    def __init__(
        self,
        prometheus_url: str = PROMETHEUS_URL_DEFAULT,
        top_n: int = 20,
        timeout: float = 30.0,
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.top_n = top_n
        self._client = httpx.Client(timeout=timeout)

    def analyze(self) -> CardinalityReport:
        status = self._fetch_tsdb_status()
        total = status.get("headStats", {}).get("numSeries", 0)
        metrics_raw: List[Dict] = status.get("seriesCountByMetricName", [])
        label_raw: List[Dict] = status.get("seriesCountByLabelName", [])
        label_counts: Dict[str, int] = {item["name"]: item["value"] for item in label_raw}

        metrics: List[MetricCardinality] = []
        for item in metrics_raw[: self.top_n]:
            name = item["name"]
            count = item["value"]
            top_label, top_label_values = None, 0
            for lname, lcount in label_counts.items():
                if lcount > top_label_values:
                    top_label, top_label_values = lname, lcount
            metrics.append(MetricCardinality(name=name, series_count=count,
                                             top_label=top_label, top_label_values=top_label_values))

        recommendations = self._build_recommendations(metrics, label_counts)
        return CardinalityReport(total_series=total, metrics=metrics, recommendations=recommendations)

    def drop_high_cardinality_labels(self, metric_name: str, labels_to_drop: List[str]) -> Dict:
        return {
            "action": "labeldrop",
            "metric": metric_name,
            "regex": "|".join(labels_to_drop),
            "_note": "Paste into prometheus.yml scrape_configs > metric_relabel_configs",
        }

    def _fetch_tsdb_status(self) -> Dict:
        url = f"{self.prometheus_url}/api/v1/status/tsdb"
        resp = self._client.get(url, params={"limit": self.top_n})
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"Prometheus returned non-success: {data}")
        return data["data"]

    @staticmethod
    def _build_recommendations(metrics: List[MetricCardinality], label_counts: Dict[str, int]) -> List[str]:
        recs: List[str] = []
        critical = [m for m in metrics if m.severity == "critical"]
        if critical:
            recs.append(f"CRITICAL: {[m.name for m in critical[:5]]} exceed {CRITICAL_THRESHOLD:,} series.")
        high = [m for m in metrics if m.severity == "high"]
        if high:
            recs.append(f"HIGH: {[m.name for m in high[:5]]} exceed {HIGH_CARDINALITY_THRESHOLD:,} series.")
        bad_labels = [l for l, c in sorted(label_counts.items(), key=lambda x: -x[1])[:5] if c > HIGH_CARDINALITY_THRESHOLD]
        if bad_labels:
            recs.append(f"High-cardinality labels: {bad_labels}. Use metric_relabel_configs > labeldrop.")
        if not recs:
            recs.append("Cardinality looks healthy.")
        return recs

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "CardinalityAnalyzer":
        return self

    def __exit__(self, *_) -> None:
        self.close()

# _r 20260625095411-81ad9399
