#!/bin/bash
# Serve the FastAPI app directly on port 8000 (Substrait's required backend port).
# A previous revision proxied through stock nginx for larger body/timeout limits,
# but the platform drops all Linux capabilities, so nginx's startup chown() fails
# with "Operation not permitted" and every pod stays NOT READY (ingress 502).
# uvicorn alone is sufficient: keep-alive 300s + 2 workers for large manifests.

# Start uvicorn with increased keep-alive and workers
# Use 2 workers for 100k rows, timeout 300
exec uvicorn main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 300 --workers 2
