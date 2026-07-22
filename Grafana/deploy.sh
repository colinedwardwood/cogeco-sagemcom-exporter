#!/usr/bin/env bash
# Push Cogeco modem dashboard + alerts to Grafana Cloud (Network folder).
#
# Required:
#   GRAFANA_ORG_SLUG   Grafana Cloud stack slug (URL becomes https://<slug>.grafana.net)
#   GRAFANA_API_KEY    Grafana Cloud API token with dashboard/alert write access
#
# Optional:
#   GRAFANA_URL        Override the full base URL
#   TOKEN_DIR          Directory with grafana-api.key (and optional grafana-api.instance)
#                      Default: ~/.tokens/grafana-<GRAFANA_ORG_SLUG>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

GRAFANA_ORG_SLUG="${GRAFANA_ORG_SLUG:-}"
if [[ -z "${GRAFANA_URL:-}" ]]; then
  if [[ -z "$GRAFANA_ORG_SLUG" ]]; then
    echo "ERROR: Set GRAFANA_ORG_SLUG (or GRAFANA_URL)." >&2
    echo "  Example: GRAFANA_ORG_SLUG=<org-slug> GRAFANA_API_KEY=... ./Grafana/deploy.sh" >&2
    exit 1
  fi
  GRAFANA_URL="https://${GRAFANA_ORG_SLUG}.grafana.net"
fi

TOKEN_DIR="${TOKEN_DIR:-$HOME/.tokens/grafana-${GRAFANA_ORG_SLUG:-cloud}}"
GRAFANA_API_KEY="${GRAFANA_API_KEY:-$(cat "$TOKEN_DIR/grafana-api.key" 2>/dev/null || true)}"

# Prefer URL from token dir instance file when GRAFANA_URL was not explicitly set
# and GRAFANA_ORG_SLUG was empty but TOKEN_DIR was provided externally.
if [[ -z "${GRAFANA_ORG_SLUG}" && -f "$TOKEN_DIR/grafana-api.instance" ]]; then
  GRAFANA_URL="$(awk '/^url:/{print $2}' "$TOKEN_DIR/grafana-api.instance")"
fi

FOLDER_TITLE="Network"
FOLDER_UID="network"
ALERT_GROUP="Cogeco%20Modem"

if [[ -z "$GRAFANA_API_KEY" ]]; then
  echo "ERROR: GRAFANA_API_KEY is not set and $TOKEN_DIR/grafana-api.key not found." >&2
  exit 1
fi

AUTH="Authorization: Bearer $GRAFANA_API_KEY"

log()  { printf '\n\033[1;34m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

log "Checking Grafana API connectivity..."
curl -sf -H "$AUTH" "$GRAFANA_URL/api/folders/$FOLDER_UID" >/dev/null \
  || fail "Cannot reach folder '$FOLDER_UID' on $GRAFANA_URL"
ok "Connected to $GRAFANA_URL / folder $FOLDER_TITLE ($FOLDER_UID)"

log "Pushing dashboards..."
for DASH_FILE in "$SCRIPT_DIR/dashboards/"*.json; do
  DASH_NAME=$(basename "$DASH_FILE" .json)
  PAYLOAD=$(python3 - "$DASH_FILE" "$FOLDER_UID" <<'PYEOF'
import json, sys
with open(sys.argv[1]) as f:
    d = json.load(f)
d.pop("id", None)
payload = {
    "dashboard": d,
    "folderUid": sys.argv[2],
    "overwrite": True,
    "message": "deployed via Grafana/deploy.sh",
}
print(json.dumps(payload))
PYEOF
)
  RESP=$(echo "$PAYLOAD" | curl -sf -X POST \
    -H "$AUTH" -H "Content-Type: application/json" \
    -d @- \
    "$GRAFANA_URL/api/dashboards/db")
  URL=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('url',''))")
  ok "$DASH_NAME → $GRAFANA_URL$URL"
done

log "Pushing alert rules..."
ALERTS_FILE="$SCRIPT_DIR/alerts/cogeco-alerts.json"
HTTP_STATUS=$(curl -s -o /tmp/cogeco-alert-resp.json -w "%{http_code}" -X PUT \
  -H "$AUTH" -H "Content-Type: application/json" \
  --data @"$ALERTS_FILE" \
  "$GRAFANA_URL/api/v1/provisioning/folder/$FOLDER_UID/rule-groups/$ALERT_GROUP")

if [[ "$HTTP_STATUS" == "202" || "$HTTP_STATUS" == "200" ]]; then
  RULE_COUNT=$(python3 -c "import json; print(len(json.load(open('$ALERTS_FILE'))['rules']))")
  ok "$RULE_COUNT alert rules pushed to folder '$FOLDER_TITLE' / group 'Cogeco Modem'."
else
  echo "Response body:" >&2
  cat /tmp/cogeco-alert-resp.json >&2
  fail "Alert rule push failed with HTTP $HTTP_STATUS."
fi

log "Done."
echo ""
echo "  Dashboard: $GRAFANA_URL/d/cogeco-sagemcom-modem/cogeco-sagemcom-modem"
echo "  Folder:    $GRAFANA_URL/dashboards/f/$FOLDER_UID"
echo "  Alerts:    $GRAFANA_URL/alerting/list?search=Cogeco"
echo ""
echo "Note: cogeco_* metrics are not in Grafana Cloud yet. Start the exporter and"
echo "add an Alloy scrape (job custom/cogeco_sagemcom_exporter → :9488/metrics)."
echo ""
