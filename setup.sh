#!/bin/bash
# Setup script for mcp-opensearch
#
# Writes two credential files into ~/.config/mcp-opensearch/, because the two
# supported run paths read configuration in different ways:
#
#   config.json  — read by the Python process (PyPI install or `python3 server.py`).
#   .env         — read by `docker --env-file` (and therefore by `make run` /
#                  `make shell` and the documented `docker run` command). It must
#                  be plain KEY=value: docker cannot parse shell syntax, so no
#                  `export`, no quoting, no `source` lines. The Python process
#                  does NOT read this file — Docker turns it into real env vars.
#
# Both files contain the password, so both are created under `umask 077`.

set -e

# Require Python 3.10+
PY_MINOR=$(python3 -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo "0")
PY_MAJOR=$(python3 -c "import sys; print(sys.version_info.major)" 2>/dev/null || echo "0")
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    echo "Error: Python 3.10 or higher is required (found $(python3 --version 2>&1))" >&2
    exit 1
fi

CONFIG_DIR="$HOME/.config/mcp-opensearch"
CONFIG_FILE="$CONFIG_DIR/config.json"
ENV_FILE="$CONFIG_DIR/.env"

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
    if [ ! -f "$ENV_FILE" ]; then
        echo ""
        echo "Note: $ENV_FILE does not exist."
        echo "The Docker path (make run / make shell / docker run --env-file) needs it."
        echo "Create it by hand, plain KEY=value only — no 'export', no quotes, no 'source':"
        echo "  OPENSEARCH_DASHBOARDS_URL=https://opensearch.example.com"
        echo "  OPENSEARCH_URL=https://opensearch.example.com:9200"
        echo "  OPENSEARCH_USERNAME=myuser"
        echo "  OPENSEARCH_PASSWORD=mypassword"
        echo "  OPENSEARCH_VERIFY_SSL=true"
        echo "  OPENSEARCH_TIMEOUT=60"
        echo "then: chmod 600 $ENV_FILE"
        echo ""
        echo "Or delete $CONFIG_FILE and re-run this script to generate both files."
    fi
    exit 0
fi

# Everything below writes credentials to disk. Restrict the mode at CREATION
# time — a later chmod leaves a window in which the secret is world-readable,
# and leaves a 0644 file behind if the write fails under `set -e`.
umask 077

mkdir -p "$CONFIG_DIR"
echo ""
echo "Creating $CONFIG_FILE and $ENV_FILE"
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

# Same values in docker --env-file format: plain KEY=value, one per line, no
# quoting and no `export`. Written by the same quoted heredoc so passwords
# containing " or $ pass through untouched.
_D="$DASHBOARDS_URL" _O="$OPENSEARCH_URL" _U="$USERNAME" _P="$PASSWORD" _S="$VERIFY_SSL_BOOL" \
python3 <<'PYEOF' > "$ENV_FILE"
import os, sys
pairs = [
    ("OPENSEARCH_DASHBOARDS_URL", os.environ.get("_D", "")),
    ("OPENSEARCH_URL",            os.environ.get("_O", "")),
    ("OPENSEARCH_USERNAME",       os.environ.get("_U", "")),
    ("OPENSEARCH_PASSWORD",       os.environ.get("_P", "")),
    ("OPENSEARCH_VERIFY_SSL",     os.environ.get("_S", "true")),
    ("OPENSEARCH_TIMEOUT",        "60"),
]
for key, value in pairs:
    if "\n" in value or "\r" in value:
        sys.exit(
            f"{key} contains a newline, which docker --env-file cannot represent. "
            "Use config.json for this value instead."
        )
print("# docker --env-file format: plain KEY=value only. Do not add 'export',")
print("# quotes or 'source' lines — docker does not parse shell syntax.")
for key, value in pairs:
    if value != "":
        print(f"{key}={value}")
PYEOF

chmod 600 "$ENV_FILE"

echo "Saved (both chmod 600):"
echo "  $CONFIG_FILE  — used by the Python paths (pip install / python3 server.py)"
echo "  $ENV_FILE     — used by the Docker path (docker --env-file, make run, make shell)"
echo ""
echo "Note: the Python process does not read $ENV_FILE itself. It is Docker that"
echo "turns those lines into environment variables inside the container."
echo ""
echo "Next steps:"
echo "  Docker:  make -C $(dirname "$0") build && make -C $(dirname "$0") run"
echo "  Python:  python3 $(dirname "$0")/server.py"
