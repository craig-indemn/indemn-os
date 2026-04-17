#!/bin/bash
# Set up the indemn CLI for connecting to the OS.
# Run this once per terminal session, or add to your shell profile.
#
# Usage:
#   source scripts/setup-cli.sh          # Connect to dev
#   source scripts/setup-cli.sh prod     # Connect to prod (when it exists)

ENV="${1:-dev}"

if [ "$ENV" = "dev" ]; then
    export INDEMN_API_URL="https://indemn-api-production.up.railway.app"
    echo "Connected to dev: $INDEMN_API_URL"
elif [ "$ENV" = "local" ]; then
    export INDEMN_API_URL="http://localhost:8000"
    echo "Connected to local: $INDEMN_API_URL"
else
    echo "Unknown environment: $ENV (use dev or local)"
    return 1
fi

# Check connection
echo "Testing connection..."
indemn platform health 2>/dev/null
if [ $? -eq 0 ]; then
    echo "✓ Connected"
else
    echo "✗ API not reachable at $INDEMN_API_URL"
    echo ""
    echo "To authenticate, either:"
    echo "  export INDEMN_SERVICE_TOKEN=indemn_xxx   # Service token"
    echo "  # or login interactively (not yet implemented)"
fi
