from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.sdk._logs.export import ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    PeriodicExportingMetricReader,
    ConsoleMetricExporter,
)
from opentelemetry import _logs
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk.metrics.export import AggregationTemporality
from opentelemetry.sdk.metrics import Counter, Histogram
import logging

import os


def _call_once(fn):
    called = False
    result = None

    def wrapper(*args, **kwargs):
        nonlocal called, result
        if not called:
            result = fn(*args, **kwargs)
            called = True
        return result

    return wrapper


attributes = {
    SERVICE_NAME: "room-server",
}

if os.getenv("MESHAGENT_PROJECT_ID") is not None:
    attributes["project"] = os.getenv("MESHAGENT_PROJECT_ID")

if os.getenv("MESHAGENT_SESSION_ID") is not None:
    attributes["session"] = os.getenv("MESHAGENT_SESSION_ID")

if os.getenv("MESHAGENT_ROOM") is not None:
    attributes["room"] = os.getenv("MESHAGENT_ROOM")

resource = Resource.create(attributes=attributes)

logger_provider = None
tracer_provider = None
meter_provider = None

add_console_exporters = False

otel_endpoint = os.getenv("OTEL_ENDPOINT")

if otel_endpoint is not None:
    otel_logs_endpoint = otel_endpoint + "/v1/logs"
    otel_traces_endpoint = otel_endpoint + "/v1/traces"
    otel_metrics_endpoint = otel_endpoint + "/v1/metrics"

    if otel_logs_endpoint is not None:
        logs_exporter = OTLPLogExporter(
            endpoint=otel_logs_endpoint,
        )
        logger_provider = LoggerProvider(resource=resource)
        _logs.set_logger_provider(logger_provider)

        logger_provider.add_log_record_processor(BatchLogRecordProcessor(logs_exporter))

        if add_console_exporters:
            logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(ConsoleLogExporter())
            )

    if otel_traces_endpoint is not None:
        tracer_provider = TracerProvider(resource=resource)
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=otel_traces_endpoint))
        tracer_provider.add_span_processor(processor)
        if add_console_exporters:
            tracer_provider.add_span_processor(
                BatchSpanProcessor(ConsoleSpanExporter())
            )
        trace.set_tracer_provider(tracer_provider)

    if otel_metrics_endpoint is not None:
        reader = PeriodicExportingMetricReader(
            exporter=OTLPMetricExporter(
                endpoint=otel_metrics_endpoint,
                preferred_temporality={
                    Counter: AggregationTemporality.DELTA,
                    Histogram: AggregationTemporality.DELTA,
                },
            ),
            export_interval_millis=1000,
        )

        readers = [
            reader,
        ]
        if add_console_exporters:
            readers.append(PeriodicExportingMetricReader(ConsoleMetricExporter()))

        meter_provider = MeterProvider(resource=resource, metric_readers=readers)
        metrics.set_meter_provider(meter_provider)


@_call_once
def init(level):
    if logger_provider is not None:
        logging_handler = LoggingHandler(level=level, logger_provider=logger_provider)
        root = logging.getLogger()
        root.setLevel(level)
        root.addHandler(logging_handler)
    else:
        logging.basicConfig(level=level)
