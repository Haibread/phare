"""The shared fallback convention helper (review G1, mission M2.1)."""

from __future__ import annotations

import logging

from opentelemetry import metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from phare.core.fallback import record_fallback


def test_record_fallback_logs_component_reason_and_fields(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        record_fallback("widget", "blew_up", detail="42")
    rec = next(r for r in caplog.records if r.message == "widget.fallback")
    assert rec.reason == "blew_up"
    assert rec.detail == "42"  # extra structured fields ride along


def test_record_fallback_increments_the_metric() -> None:
    reader = InMemoryMetricReader()
    metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))
    record_fallback("widget", "blew_up")
    points = [
        point
        for rm in (reader.get_metrics_data().resource_metrics or [])
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == "phare.fallback"
        for point in metric.data.data_points
    ]
    assert any(
        p.attributes.get("component") == "widget" and p.attributes.get("reason") == "blew_up"
        for p in points
    )
