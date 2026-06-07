"""OpenTelemetry wiring: traces, metrics, and log records over OTLP/HTTP.

Exporters are only configured when an OTLP endpoint is set, so local dev and tests run
without a collector. FastAPI and SQLAlchemy are always instrumented (cheap no-ops without
a provider).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from sqlalchemy import Engine

from phare.core.config import Settings

logger = logging.getLogger(__name__)


def setup_telemetry(app: FastAPI, engine: Engine, settings: Settings) -> None:
    """Wire OTLP exporters (if configured) and instrument FastAPI + SQLAlchemy."""
    if settings.otlp_endpoint:
        resource = Resource.create({"service.name": settings.service_name})

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter())],
        )
        metrics.set_meter_provider(meter_provider)

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(logger_provider)
        logging.getLogger().addHandler(LoggingHandler(logger_provider=logger_provider))
        logger.info("telemetry.enabled", extra={"endpoint": settings.otlp_endpoint})
    else:
        logger.info("telemetry.disabled")

    FastAPIInstrumentor.instrument_app(app)
    SQLAlchemyInstrumentor().instrument(engine=engine)
