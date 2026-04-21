#!/bin/bash
# Install the Indemn OS CLI
# Requires: gh (GitHub CLI) authenticated, pip/uv
set -euo pipefail

echo "Downloading Indemn OS CLI v0.1.0..."
TMPDIR=$(mktemp -d)
gh release download v0.1.0 --repo craig-indemn/indemn-os --pattern "*.whl" --dir "$TMPDIR"
pip install "$TMPDIR"/indemn_os-*.whl
rm -rf "$TMPDIR"

echo ""
echo "Installed! Configure:"
echo "  export INDEMN_API_URL=https://api.os.indemn.ai"
echo "  indemn auth login --org _platform --email you@indemn.ai --password <password>"
echo ""
echo "Then try:"
echo "  indemn company list"
echo "  indemn deal list"
