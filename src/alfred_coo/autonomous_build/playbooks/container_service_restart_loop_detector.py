"""Playbook: detect container/service restart loops.

Today's failure mode (2026-05-02): the ``soul-svc-oracle`` docker
container went into a 6-hour crash loop (10:48Z → 16:55Z) after PR #70
introduced a regression. None of the existing alfred-doctor playbooks
watched docker container health or systemd unit restart counts, so the
loop went unnoticed until manual triage. Same evening the ``alfred-coo``
systemd unit was restarted mid-orchestrator-run by an auto-merge,
killing in-flight builders — same class of failure mode.

This playbook closes the loop. Every doctor tick it polls a small
watchlist of docker containers + systemd units and emits a Slack alert
when a target trips one of two thresholds:

* **Restart count delta ≥ 3 in last 10 minutes** for a systemd unit
  (counted via ``systemctl show -p NRestarts``) or for a docker
  container (counted via ``docker inspect ... .State.RestartCount``).
* **Continuous ``health=unhealthy`` for ≥ 5 minutes** for a docker
  container with a defined healthcheck.

Per-target cooldown of 10 minutes between alerts mirrors substrate
fix #87 from ``restart_stalled_chains``: doctor surveillance runs every
5 minutes, so without a cooldown a single crash loop would emit a
Slack alert every tick until the operator silenced it manually.

State (last RestartCount + first-unhealthy timestamp + last-alerted
timestamp per target) lives at
``/var/lib/alfred-coo/restart_loop_state.json`` so daemon restarts
don't reset the cooldown or lose the previous tick's restart count.

Watchlist is configurable via the
``ALFRED_DOCTOR_RESTART_LOOP_WATCHLIST`` env var as a comma-separated
list of ``<type>:<name>`` entries. ``<type>`` is one of ``docker`` or
``systemd``. ``<name>`` may be a literal name or a glob pattern ending
in ``*`` for docker (matched against ``docker ps --format '{{.Names}}'``).
Default watchlist:

  ``docker:soul-svc-oracle,docker:tiresias-*,systemd:alfred-coo``

Why this is a doctor playbook and not its own systemd timer: the
doctor already runs surveillance on a 5-minute cadence, already owns
the Slack channel + token, and already has the cooldown / state-on-
disk machinery. Adding another timer would duplicate all of it.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from .base import Playbook, PlaybookResult


logger = logging.getLogger(
    "alfred_coo.autonomous_build.playbooks.container_service_restart_loop_detector"
)


# Default watchlist tuned to the 2026-05-02 incident: soul-svc-oracle
# was the regressed container, alfred-coo is the daemon itself, and
# any tiresias-* container belongs to the partner-portal stack which
# shares the same blast radius if it crash-loops.
DEFAULT_WATCHLIST = (
    "docker:soul-svc-oracle,docker:tiresias-*,systemd:alfred-coo"
)

# Slack channel: #batcave (mirrors ``DEFAULT_SLACK_CHANNEL`` in doctor.py).
DEFAULT_SLACK_CHANNEL = "C0ASAKFTR1C"

# Detection thresholds.
DEFAULT_RESTART_DELTA_THRESHOLD = 3
DEFAULT_RESTART_WINDOW_SEC = 600         # 10 min
DEFAULT_UNHEALTHY_DURATION_SEC = 300     # 5 min

# Per-target cooldown between Slack alerts (mirrors substrate fix #87).
DEFAULT_ALERT_COOLDOWN_SEC = 600         # 10 min

# State persistence path.
DEFAULT_STATE_PATH = "/var/lib/alfred-coo/restart_loop_state.json"

# Subprocess timeout — generous because docker/systemctl can be slow on
# a loaded host but bounded so a stuck shellout never wedges the doctor.
DEFAULT_SUBPROCESS_TIMEOUT_SEC = 15


# ── Watchlist parsing ─────────────────────────────────────────────────────


def parse_watchlist(spec: str) -> list[tuple[str, str]]:
    """Parse ``<type>:<name>,<type>:<name>,...`` into a list of pairs.

    Empty / malformed entries are dropped silently — the doctor must
    never crash on a misconfigured env var. Glob patterns (``foo-*``)
    are NOT expanded here; expansion happens at scan time so a
    container appearing mid-session is picked up on the next tick.
    """
    out: list[tuple[str, str]] = []
    if not spec:
        return out
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry or ":" not in entry:
            continue
        kind, _, name = entry.partition(":")
        kind = kind.strip().lower()
        name = name.strip()
        if kind not in ("docker", "systemd") or not name:
            continue
        out.append((kind, name))
    return out


def expand_docker_targets(
    name_or_glob: str,
    *,
    binary: str = "docker",
    timeout: int = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
) -> list[str]:
    """Expand a docker name (or ``foo-*`` glob) to live container names.

    Literal names pass through unchanged so a container that's currently
    stopped is still polled (its restart count + unhealthy state still
    matter). Globs are matched against ``docker ps --format '{{.Names}}'``
    output; if docker isn't installed (test host, non-prod) the call
    returns an empty list and the playbook simply skips the target.
    """
    if "*" not in name_or_glob:
        return [name_or_glob]
    try:
        proc = subprocess.run(
            [binary, "ps", "--format", "{{.Names}}", "--all"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    names = [
        n.strip() for n in (proc.stdout or "").splitlines() if n.strip()
    ]
    return sorted(fnmatch.filter(names, name_or_glob))


# ── State persistence ────────────────────────────────────────────────────


def _load_state(path: str) -> dict[str, dict]:
    """Load per-target state from JSON. Empty dict on any error so a
    corrupt file or missing directory never crashes the doctor."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning(
            "restart_loop_detector: state load failed (path=%s); "
            "starting fresh", path,
        )
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            out[str(k)] = v
    return out


def _save_state(path: str, state: dict[str, dict]) -> None:
    """Persist state. Best-effort: failure is logged and swallowed."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(
            json.dumps(state, separators=(",", ":")), encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "restart_loop_detector: state save failed (path=%s)", path,
        )


# ── Probes ───────────────────────────────────────────────────────────────


def probe_docker_container(
    name: str,
    *,
    binary: str = "docker",
    timeout: int = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
) -> dict[str, Any] | None:
    """Return ``{restart_count, health_status, last_logs}`` for a docker
    container, or ``None`` if docker isn't reachable or the container
    doesn't exist. Never raises.
    """
    fmt = (
        '{{json .RestartCount}}|'
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}'
    )
    try:
        proc = subprocess.run(
            [binary, "inspect", "--format", fmt, name],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(
            "restart_loop_detector: docker inspect %s failed: %s",
            name, type(e).__name__,
        )
        return None
    if proc.returncode != 0:
        return None
    raw = (proc.stdout or "").strip()
    if "|" not in raw:
        return None
    rc_str, _, health = raw.partition("|")
    try:
        restart_count = int(rc_str.strip())
    except ValueError:
        return None
    last_logs = _docker_logs_tail(name, binary=binary, timeout=timeout)
    return {
        "restart_count": restart_count,
        "health_status": health.strip().lower(),
        "last_logs": last_logs,
    }


def _docker_logs_tail(
    name: str, *, binary: str, timeout: int, lines: int = 5,
) -> list[str]:
    try:
        proc = subprocess.run(
            [binary, "logs", "--tail", str(lines), name],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    # docker writes container stdout to subprocess.stdout AND stderr;
    # prefer stderr (where most servers log) but fall back to stdout.
    body = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    return [ln for ln in body.splitlines() if ln][-lines:]


def probe_systemd_unit(
    name: str,
    *,
    systemctl: str = "systemctl",
    journalctl: str = "journalctl",
    timeout: int = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
) -> dict[str, Any] | None:
    """Return ``{restart_count, last_logs}`` for a systemd unit or
    ``None`` if systemctl is unavailable. Never raises."""
    try:
        proc = subprocess.run(
            [systemctl, "show", "-p", "NRestarts", "--value", name],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning(
            "restart_loop_detector: systemctl show %s failed: %s",
            name, type(e).__name__,
        )
        return None
    if proc.returncode != 0:
        return None
    try:
        restart_count = int((proc.stdout or "").strip() or "0")
    except ValueError:
        return None
    last_logs = _journal_tail(name, journalctl=journalctl, timeout=timeout)
    return {
        "restart_count": restart_count,
        "health_status": "n/a",
        "last_logs": last_logs,
    }


def _journal_tail(
    name: str, *, journalctl: str, timeout: int, lines: int = 5,
) -> list[str]:
    try:
        proc = subprocess.run(
            [journalctl, "-u", name, "-n", str(lines),
             "--no-pager", "--output=cat"],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    body = (proc.stdout or "").strip()
    return [ln for ln in body.splitlines() if ln][-lines:]


# ── Detection rules ──────────────────────────────────────────────────────


def detect_restart_loop(
    *,
    target_key: str,
    current_restart_count: int,
    state_for_target: dict,
    now: float,
    delta_threshold: int = DEFAULT_RESTART_DELTA_THRESHOLD,
    window_sec: int = DEFAULT_RESTART_WINDOW_SEC,
) -> tuple[bool, int]:
    """Return ``(triggered, delta)``.

    Triggered iff the restart count grew by ≥ ``delta_threshold`` since
    the earliest sample we hold inside the last ``window_sec`` seconds.
    Samples older than ``window_sec`` are dropped from the history so
    the comparison is always over the rolling window — not over the
    daemon's lifetime.
    """
    history = state_for_target.get("samples") or []
    cutoff = now - window_sec
    history = [
        s for s in history
        if isinstance(s, dict)
        and isinstance(s.get("ts"), (int, float))
        and s["ts"] >= cutoff
    ]
    history.append({"ts": now, "count": current_restart_count})
    state_for_target["samples"] = history

    earliest = min(s["count"] for s in history)
    delta = current_restart_count - earliest
    return delta >= delta_threshold, delta


def detect_unhealthy_duration(
    *,
    target_key: str,
    health_status: str,
    state_for_target: dict,
    now: float,
    duration_threshold_sec: int = DEFAULT_UNHEALTHY_DURATION_SEC,
) -> tuple[bool, int]:
    """Return ``(triggered, duration_sec)``.

    Tracks the timestamp of the FIRST tick where the container reported
    ``unhealthy``. Triggered when ``now - first_unhealthy_at`` ≥
    ``duration_threshold_sec``. Resets when the container leaves the
    unhealthy state.
    """
    if health_status == "unhealthy":
        first = state_for_target.get("first_unhealthy_at")
        if first is None:
            state_for_target["first_unhealthy_at"] = now
            return False, 0
        duration = int(now - float(first))
        return duration >= duration_threshold_sec, duration
    # Healthy or no healthcheck — clear the marker.
    state_for_target.pop("first_unhealthy_at", None)
    return False, 0


# ── Slack post (matches doctor.post_to_slack but local copy so the
#    playbook can be unit-tested without importing the orchestrator). ────


async def post_alert(
    token: str, channel: str, text: str,
    *, client_factory=httpx.AsyncClient,
) -> bool:
    """POST chat.postMessage. Returns True on Slack-confirmed ok, False
    on any failure or missing token. Never raises."""
    if not token:
        logger.warning(
            "restart_loop_detector: no Slack token configured; skipping post",
        )
        return False
    try:
        async with client_factory(timeout=10.0) as client:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                content=json.dumps(
                    {"channel": channel, "text": text}
                ).encode(),
            )
            body = resp.json() if hasattr(resp, "json") else {}
            if not body.get("ok"):
                logger.warning(
                    "restart_loop_detector: slack post not-ok: %s", body,
                )
                return False
            return True
    except Exception:  # noqa: BLE001
        logger.exception("restart_loop_detector: slack post raised")
        return False


def format_alert(
    *,
    target_key: str,
    reason: str,
    delta: int | None,
    duration_sec: int | None,
    last_logs: list[str],
    daemon_head: str = "",
) -> str:
    """Tight Slack alert. ``target_key`` is ``<type>:<name>``; reason is
    one of ``restart-loop`` or ``unhealthy-duration``."""
    bits: list[str] = []
    head_note = f" [head={daemon_head}]" if daemon_head else ""
    bits.append(
        f":rotating_light: *alfred-doctor restart-loop alert* {target_key}{head_note}"
    )
    if reason == "restart-loop" and delta is not None:
        bits.append(
            f"reason: restart count +{delta} in last "
            f"{DEFAULT_RESTART_WINDOW_SEC // 60}min "
            f"(threshold {DEFAULT_RESTART_DELTA_THRESHOLD})"
        )
    elif reason == "unhealthy-duration" and duration_sec is not None:
        bits.append(
            f"reason: continuously unhealthy for {duration_sec}s "
            f"(threshold {DEFAULT_UNHEALTHY_DURATION_SEC}s)"
        )
    else:
        bits.append(f"reason: {reason}")

    if last_logs:
        log_block = "\n".join(last_logs[-5:])
        bits.append(f"last 5 log lines:\n```\n{log_block}\n```")
    else:
        bits.append("last 5 log lines: (unavailable)")
    return "\n".join(bits)


# ── Playbook class ───────────────────────────────────────────────────────


class ContainerServiceRestartLoopDetectorPlaybook(Playbook):
    """See module docstring."""

    kind = "container_service_restart_loop_detector"
    # Cap the per-tick blast radius. With the default 3-target watchlist
    # this is comfortable; a 50-container watchlist would still emit at
    # most this many alerts per tick (rest get held over).
    max_actions_per_tick = 6

    def __init__(
        self,
        *,
        watchlist: str | None = None,
        state_path: str | None = None,
        slack_channel: str = DEFAULT_SLACK_CHANNEL,
        delta_threshold: int = DEFAULT_RESTART_DELTA_THRESHOLD,
        window_sec: int = DEFAULT_RESTART_WINDOW_SEC,
        unhealthy_duration_sec: int = DEFAULT_UNHEALTHY_DURATION_SEC,
        alert_cooldown_sec: int = DEFAULT_ALERT_COOLDOWN_SEC,
        docker_binary: str = "docker",
        systemctl_binary: str = "systemctl",
        journalctl_binary: str = "journalctl",
        subprocess_timeout: int = DEFAULT_SUBPROCESS_TIMEOUT_SEC,
        slack_post=post_alert,
    ):
        spec = (
            watchlist
            if watchlist is not None
            else os.getenv(
                "ALFRED_DOCTOR_RESTART_LOOP_WATCHLIST", DEFAULT_WATCHLIST,
            )
        )
        self.watchlist = parse_watchlist(spec)
        self.state_path = state_path or DEFAULT_STATE_PATH
        self.slack_channel = slack_channel
        self.delta_threshold = delta_threshold
        self.window_sec = window_sec
        self.unhealthy_duration_sec = unhealthy_duration_sec
        self.alert_cooldown_sec = alert_cooldown_sec
        self.docker_binary = docker_binary
        self.systemctl_binary = systemctl_binary
        self.journalctl_binary = journalctl_binary
        self.subprocess_timeout = subprocess_timeout
        self._slack_post = slack_post

    async def execute(
        self,
        *,
        linear_api_key: str = "",
        dry_run: bool,
        mesh: Any = None,
        **_extra: Any,
    ) -> PlaybookResult:
        result = PlaybookResult(kind=self.kind, dry_run=dry_run)
        if not self.watchlist:
            return result  # nothing configured — silent

        state = _load_state(self.state_path)
        now = time.time()

        slack_token = (
            os.getenv("SLACK_BOT_TOKEN_ALFRED")
            or os.getenv("SLACK_BOT_TOKEN")
            or ""
        )
        daemon_head = (os.getenv("ALFRED_COO_HEAD") or "")[:7]

        # Expand globs once per tick so a freshly-launched tiresias-*
        # container is picked up immediately.
        expanded: list[tuple[str, str]] = []
        for kind, name in self.watchlist:
            if kind == "docker":
                names = expand_docker_targets(
                    name,
                    binary=self.docker_binary,
                    timeout=self.subprocess_timeout,
                )
                # Empty expansion = container not currently running and
                # not a literal — silently skip; nothing to probe.
                for n in names:
                    expanded.append((kind, n))
            else:
                expanded.append((kind, name))

        for kind, name in expanded:
            target_key = f"{kind}:{name}"
            try:
                await self._handle_one(
                    kind=kind,
                    name=name,
                    target_key=target_key,
                    state=state,
                    now=now,
                    dry_run=dry_run,
                    slack_token=slack_token,
                    daemon_head=daemon_head,
                    result=result,
                )
            except Exception as e:  # noqa: BLE001
                logger.exception(
                    "restart_loop_detector: handler crashed for %s",
                    target_key,
                )
                result.errors.append(
                    f"{target_key}: handler_crashed: {type(e).__name__}"
                )

        _save_state(self.state_path, state)
        return result

    async def _handle_one(
        self,
        *,
        kind: str,
        name: str,
        target_key: str,
        state: dict,
        now: float,
        dry_run: bool,
        slack_token: str,
        daemon_head: str,
        result: PlaybookResult,
    ) -> None:
        target_state = state.setdefault(target_key, {})

        if kind == "docker":
            probe = probe_docker_container(
                name,
                binary=self.docker_binary,
                timeout=self.subprocess_timeout,
            )
        else:
            probe = probe_systemd_unit(
                name,
                systemctl=self.systemctl_binary,
                journalctl=self.journalctl_binary,
                timeout=self.subprocess_timeout,
            )

        if probe is None:
            # Probe failed / target absent. Don't error-noise the result;
            # just count it as a candidate-skipped so the digest shows
            # surveillance is alive even when nothing is wrong.
            result.actions_skipped += 1
            return

        restart_loop, delta = detect_restart_loop(
            target_key=target_key,
            current_restart_count=int(probe["restart_count"]),
            state_for_target=target_state,
            now=now,
            delta_threshold=self.delta_threshold,
            window_sec=self.window_sec,
        )
        unhealthy, duration = detect_unhealthy_duration(
            target_key=target_key,
            health_status=str(probe.get("health_status") or ""),
            state_for_target=target_state,
            now=now,
            duration_threshold_sec=self.unhealthy_duration_sec,
        )

        if not (restart_loop or unhealthy):
            return

        result.candidates_found += 1

        # Per-target cooldown.
        last_alerted = float(target_state.get("last_alerted_at") or 0.0)
        since_last = now - last_alerted
        if last_alerted > 0 and since_last < self.alert_cooldown_sec:
            wait_sec = int(self.alert_cooldown_sec - since_last)
            result.actions_skipped += 1
            result.notable.append(
                f"{target_key}: cooldown active "
                f"({int(since_last)}s since last alert; wait {wait_sec}s)"
            )
            return

        reason = "restart-loop" if restart_loop else "unhealthy-duration"
        text = format_alert(
            target_key=target_key,
            reason=reason,
            delta=delta if restart_loop else None,
            duration_sec=duration if unhealthy else None,
            last_logs=list(probe.get("last_logs") or []),
            daemon_head=daemon_head,
        )

        if dry_run:
            result.notable.append(
                f"would alert {target_key} ({reason}; "
                f"delta={delta}, unhealthy_dur={duration}s)"
            )
            return

        ok = await self._slack_post(slack_token, self.slack_channel, text)
        if ok:
            result.actions_taken += 1
            target_state["last_alerted_at"] = now
            result.notable.append(
                f"alerted {target_key} ({reason}; "
                f"delta={delta}, unhealthy_dur={duration}s)"
            )
        else:
            result.errors.append(
                f"{target_key}: slack_post_failed"
            )


__all__ = [
    "ContainerServiceRestartLoopDetectorPlaybook",
    "DEFAULT_ALERT_COOLDOWN_SEC",
    "DEFAULT_RESTART_DELTA_THRESHOLD",
    "DEFAULT_RESTART_WINDOW_SEC",
    "DEFAULT_SLACK_CHANNEL",
    "DEFAULT_STATE_PATH",
    "DEFAULT_SUBPROCESS_TIMEOUT_SEC",
    "DEFAULT_UNHEALTHY_DURATION_SEC",
    "DEFAULT_WATCHLIST",
    "detect_restart_loop",
    "detect_unhealthy_duration",
    "expand_docker_targets",
    "format_alert",
    "parse_watchlist",
    "post_alert",
    "probe_docker_container",
    "probe_systemd_unit",
]
