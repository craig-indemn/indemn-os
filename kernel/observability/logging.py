"""Structured JSON logging for all kernel services."""

import json
import logging
from datetime import datetime, timezone


class StructuredFormatter(logging.Formatter):
    """Format log records as JSON for shipping to Grafana Cloud."""

    def format(self, record):
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
        }
        if hasattr(record, "entity_type"):
            log_entry["entity_type"] = record.entity_type
        if hasattr(record, "entity_id"):
            log_entry["entity_id"] = record.entity_id
        if hasattr(record, "correlation_id"):
            log_entry["correlation_id"] = record.correlation_id
        return json.dumps(log_entry)


def setup_logging():
    """Configure structured logging for the application."""
    handler = logging.StreamHandler()
    handler.setFormatter(StructuredFormatter())
    logging.root.addHandler(handler)
    logging.root.setLevel(logging.INFO)
