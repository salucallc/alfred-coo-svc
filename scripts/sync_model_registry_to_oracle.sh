#!/usr/bin/env bash
# Sync the model registry from the minipc planning dir to Oracle.
#
# Sub #62 (2026-04-27). Source of truth lives in
#   Z:/_planning/model_registry/registry.yaml
# Oracle daemon reads it at
#   /etc/alfred-coo/model_registry.yaml
# (env var MODEL_REGISTRY_PATH set in deploy/alfred-coo.service).
#
# Hot-swap mechanics: the daemon mtime-checks on every dispatch. After
# this rsync lands, the NEXT dispatch picks up the new model-routing.
# No daemon restart needed.
#
# Usage:
#   bash scripts/sync_model_registry_to_oracle.sh [registry.yaml]
#   # default source: Z:/_planning/model_registry/registry.yaml
#
# Requires:
#   * SSH key at ~/.ssh/oci-saluca (per memory reference_oracle_ssh_from_minipc.md)
#   * Oracle host reachable at 100.105.27.63 (Tailscale)

set -euo pipefail

SRC="${1:-Z:/_planning/model_registry/registry.yaml}"
ORACLE_HOST="ubuntu@100.105.27.63"
ORACLE_DST="/etc/alfred-coo/model_registry.yaml"

if [[ ! -f "$SRC" ]]; then
    echo "ERR: source registry not found at $SRC" >&2
    exit 2
fi

echo "[sync] source: $SRC"
echo "[sync] target: $ORACLE_HOST:$ORACLE_DST"

# Stage to /tmp on Oracle, then sudo-mv into /etc/ so the daemon's
# strict ProtectSystem= settings don't bite us.
scp -i "$HOME/.ssh/oci-saluca" "$SRC" "$ORACLE_HOST:/tmp/model_registry.yaml"

ssh -i "$HOME/.ssh/oci-saluca" "$ORACLE_HOST" \
    "sudo install -m 0644 -o root -g root /tmp/model_registry.yaml $ORACLE_DST && rm /tmp/model_registry.yaml && stat -c 'mtime=%y size=%s' $ORACLE_DST"

echo "[sync] done. Daemon will pick up the new registry on next dispatch."
