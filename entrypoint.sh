#!/bin/sh
# Service entrypoint — dispatches based on SERVICE_TYPE env var.
# Each Railway service sets SERVICE_TYPE to its role.

set -e

case "$SERVICE_TYPE" in
  api)
    exec uv run python -m kernel.api.app
    ;;
  queue_processor)
    exec uv run python -m kernel.queue_processor
    ;;
  temporal_worker)
    exec uv run python -m kernel.temporal.worker
    ;;
  *)
    echo "ERROR: Unknown SERVICE_TYPE='$SERVICE_TYPE'"
    echo "Valid values: api, queue_processor, temporal_worker"
    exit 1
    ;;
esac
