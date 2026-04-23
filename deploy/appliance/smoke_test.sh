#!/usr/bin/env bash
# Mission Control Appliance smoke test — probe each service's health.
# Exit non-zero on any failure so CI + the setup wizard can depend on it.
set -o pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

APPLIANCE_HOSTNAME="${APPLIANCE_HOSTNAME:-localhost}"
BASE_URL="http://${APPLIANCE_HOSTNAME}"

fail_count=0

probe() {
  local name="$1"
  local url="$2"
  local expect_status="${3:-200}"
  printf "  %-26s " "$name"
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" || true)
  if [[ "$status" == "$expect_status" ]]; then
    printf "${GREEN}PASS${NC} (%s)\n" "$status"
  elif [[ -z "$status" || "$status" == "000" ]]; then
    printf "${RED}FAIL${NC} (no response)\n"
    fail_count=$((fail_count + 1))
  else
    printf "${RED}FAIL${NC} (got %s, expected %s)\n" "$status" "$expect_status"
    fail_count=$((fail_count + 1))
  fi
}

echo "Mission Control appliance smoke test"
echo "  target: ${BASE_URL}"
echo ""

echo "Public routes:"
probe "caddy-healthz"          "${BASE_URL}/healthz"              200
probe "portal-root"            "${BASE_URL}/"                     200
probe "soul-svc-healthz"       "${BASE_URL}/soul/v1/healthz"      200
probe "mcp-github-healthz"     "${BASE_URL}/mcp/github/healthz"   200
probe "mcp-slack-healthz"      "${BASE_URL}/mcp/slack/healthz"    200
probe "mcp-linear-healthz"     "${BASE_URL}/mcp/linear/healthz"   200
probe "mcp-notion-healthz"     "${BASE_URL}/mcp/notion/healthz"   200
probe "open-webui-root"        "${BASE_URL}/chat"                 200

echo ""
echo "Container health (docker compose):"
if command -v docker &>/dev/null; then
  unhealthy=$(docker compose ps --format json 2>/dev/null \
    | python -c 'import sys,json
seen=False
for line in sys.stdin:
    if not line.strip(): continue
    try: s=json.loads(line)
    except: continue
    seen=True
    name=s.get("Service",s.get("Name","?"))
    h=s.get("Health","") or s.get("State","?")
    if h.lower() not in ("healthy","running"):
        print(f"  {name}: {h}")
' 2>/dev/null)
  if [[ -n "$unhealthy" ]]; then
    echo -e "${RED}Unhealthy containers:${NC}"
    echo "$unhealthy"
    fail_count=$((fail_count + 1))
  else
    printf "  ${GREEN}all services healthy${NC}\n"
  fi
else
  printf "  ${YELLOW}docker CLI not found; skipping container checks${NC}\n"
fi

echo ""
if [[ $fail_count -eq 0 ]]; then
  echo -e "${GREEN}SMOKE TEST PASSED${NC}"
  exit 0
else
  echo -e "${RED}SMOKE TEST FAILED${NC} ($fail_count failure(s))"
  exit 1
fi
