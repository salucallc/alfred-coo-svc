#!/usr/bin/env bash
# deploy/deploy.sh - idempotent installer for alfred-coo-svc on Oracle VM.
# Run from the cloned repo root: sudo deploy/deploy.sh [--dry-run]
set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

INSTALL_DIR=/opt/alfred-coo
ENV_DIR=/etc/alfred-coo
LOG_DIR=/var/log/alfred-coo
STATE_DIR=/var/lib/alfred-coo
SERVICE_USER=alfredcoo
UNIT_FILE=/etc/systemd/system/alfred-coo.service

run() { if $DRY_RUN; then echo "[dry-run] $*"; else echo "+ $*"; eval "$@"; fi }

# 1. system user
if ! id "$SERVICE_USER" &>/dev/null; then
    run "useradd --system --no-create-home --shell /usr/sbin/nologin $SERVICE_USER"
fi

# 2. directories
run "install -d -o $SERVICE_USER -g $SERVICE_USER -m 0755 $INSTALL_DIR $LOG_DIR $STATE_DIR"
run "install -d -o root -g $SERVICE_USER -m 0750 $ENV_DIR"
# SAL-3557: gh CLI config dir must live under a ReadWritePath so the
# cockpit_router's `gh pr list` shell-out works under ProtectHome=true.
run "install -d -o $SERVICE_USER -g $SERVICE_USER -m 0700 $STATE_DIR/gh-config"

# 3. copy code (assume CWD is the cloned repo)
run "rsync -a --delete --exclude='.git' --exclude='__pycache__' --exclude='.venv' ./ $INSTALL_DIR/"
run "chown -R $SERVICE_USER:$SERVICE_USER $INSTALL_DIR"

# 4. virtualenv + install
if [[ ! -d $INSTALL_DIR/venv ]]; then
    run "sudo -u $SERVICE_USER python3 -m venv $INSTALL_DIR/venv"
fi
run "sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/pip install --upgrade pip"
run "sudo -u $SERVICE_USER $INSTALL_DIR/venv/bin/pip install -e $INSTALL_DIR"

# 5. env file (idempotent: do not overwrite if present)
if [[ ! -f $ENV_DIR/.env ]]; then
    run "install -o root -g $SERVICE_USER -m 0640 $INSTALL_DIR/deploy/.env.template $ENV_DIR/.env"
    echo "WARNING: $ENV_DIR/.env created from template. Fill in secrets before starting service."
fi

# 6. systemd unit
run "install -o root -g root -m 0644 $INSTALL_DIR/deploy/alfred-coo.service $UNIT_FILE"
run "systemctl daemon-reload"
run "systemctl enable alfred-coo.service"

# 7. start (only if not dry-run)
if ! $DRY_RUN; then
    if systemctl is-active --quiet alfred-coo; then
        echo "Restarting service..."
        run "systemctl restart alfred-coo"
    else
        echo "Starting service..."
        run "systemctl start alfred-coo"
    fi
    sleep 3
    systemctl status alfred-coo --no-pager || true
    echo ""
    echo "Smoke check:"
    curl -sS -o /dev/null -w "  /healthz HTTP %{http_code}\n" http://localhost:8090/healthz || echo "  /healthz: not yet ready"
fi

echo ""
echo "Deploy complete."
echo "Logs: journalctl -u alfred-coo -f"
echo "Health: curl http://localhost:8090/healthz"
