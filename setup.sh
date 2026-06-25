#!/bin/bash
# Setup script for mcp-opensearch
# Creates ~/.config/mcp-opensearch/config.json interactively

set -e

CONFIG_DIR="$HOME/.config/mcp-opensearch"
CONFIG_FILE="$CONFIG_DIR/config.json"

echo "=== mcp-opensearch setup ==="
echo ""
echo "Checking Python dependencies..."
MISSING=0
for mod in requests fastmcp dotenv; do
    if python3 -c "import $mod" 2>/dev/null; then
        echo "  ok $mod"
    else
        echo "  MISSING $mod"
        MISSING=1
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "Install missing dependencies with:"
    echo "  pip3 install -r $(dirname "$0")/requirements.txt"
fi

if [ -f "$CONFIG_FILE" ]; then
    echo ""
    echo "Config already exists: $CONFIG_FILE"
    exit 0
fi

mkdir -p "$CONFIG_DIR"
echo ""
echo "Creating $CONFIG_FILE"
echo ""

read -p "OpenSearch Dashboards URL (e.g. https://opensearch.example.com) [leave blank to skip]: " DASHBOARDS_URL
read -p "Direct OpenSearch URL (e.g. https://opensearch.example.com:9200) [leave blank to skip]: " OPENSEARCH_URL

if [ -z "$DASHBOARDS_URL" ] && [ -z "$OPENSEARCH_URL" ]; then
    echo "Error: at least one URL is required." >&2
    exit 1
fi

read -p "Username: " USERNAME
read -sp "Password: " PASSWORD
echo ""

read -p "Verify SSL certificate? [Y/n]: " VERIFY_SSL
case "$VERIFY_SSL" in
    [nN]|[nN][oO]) VERIFY_SSL_BOOL="false" ;;
    *) VERIFY_SSL_BOOL="true" ;;
esac

_D="$DASHBOARDS_URL" _O="$OPENSEARCH_URL" _U="$USERNAME" _P="$PASSWORD" _S="$VERIFY_SSL_BOOL" \
python3 <<'PYEOF' > "$CONFIG_FILE"
import json, os
cfg = {}
if os.environ.get("_D"):
    cfg["dashboards_url"] = os.environ["_D"]
if os.environ.get("_O"):
    cfg["opensearch_url"] = os.environ["_O"]
cfg.update({
    "username":   os.environ.get("_U", ""),
    "password":   os.environ.get("_P", ""),
    "verify_ssl": os.environ["_S"] == "true",
    "timeout":    60,
})
print(json.dumps(cfg, indent=4))
PYEOF

chmod 600 "$CONFIG_FILE"
echo "Config saved to $CONFIG_FILE (chmod 600)"
echo ""
echo "Or use env vars (e.g. in a .env file):"
echo "  OPENSEARCH_DASHBOARDS_URL=https://opensearch.example.com"
echo "  OPENSEARCH_URL=https://opensearch.example.com:9200"
echo "  OPENSEARCH_USERNAME=..."
echo "  OPENSEARCH_PASSWORD=..."
echo "  OPENSEARCH_VERIFY_SSL=true"
echo ""
echo "Done. Build the Docker image with:"
echo "  make -C $(dirname "$0") build"
