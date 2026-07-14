"""Subprocess supervisor — spawns, monitors, and rehydrates bot processes.

Each bot runs as a separate subprocess launched with
``uv run python -m src.bots.runner --bot-id <id>``.  The supervisor:

1. Reads the roster and spawns all bots.
2. Runs a heartbeat server (unix sockets) that each bot writes to each tick.
3. Runs a watchdog loop every 5 seconds; dead or silent bots are respawned.
4. Enforces the live allow-list (invariant 1 — fail-closed).
5. Supports incremental ``reload`` (diff new roster vs running set).
6. Drains all bots gracefully on shutdown.
"""

from __future__ import annotations

__all__ = [
    "BotProcess",
    "BotStatus",
    "ReloadReport",
    "Supervisor",
    "SupervisorDeps",
]

import asyncio
import collections
import contextlib
import json
import logging
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.bots.base import Clock
from src.manager.config import BotEntry, BotsRoster
from src.manager.heartbeat import HeartbeatServer
from src.state.repository import AuditLogger, KillSwitchReader, StateRepository

# ---------------------------------------------------------------------------
# Signal validation helper
# ---------------------------------------------------------------------------


def validate_bot_signals(entry: BotEntry) -> None:
    """Validate that all signals declared by a bot entry are registered.

    Called before spawning a bot subprocess.  If any signal name is unknown
    this raises a ``ValueError`` which causes the bot to fail to spawn with a
    clear, actionable error.

    Args:
        entry: The bot roster entry whose ``signals`` list will be checked.

    Raises:
        ValueError: If any signal name in ``entry.signals`` is not registered
            in the signals registry.
    """
    from src.signals.registry import signals as signal_registry

    unknown = []
    for sub in entry.signals:
        try:
            signal_registry.get(sub.name)
        except KeyError:
            unknown.append(sub.name)

    if unknown:
        available = signal_registry.names
        raise ValueError(
            f"Bot '{entry.id}' declares unknown signal(s) {unknown!r}. "
            f"Registered signals: {available}. "
            "Register the signal or remove it from the bot's config."
        )


def validate_bot_winning_rule(entry: BotEntry) -> None:
    """Validate a bot entry's optional provisional winning rule reference.

    Called before spawning a bot subprocess, mirroring
    :func:`validate_bot_signals`. If ``entry.winning_rule`` is unset this is
    a no-op — the feature is opt-in. If set, the dotted name must be
    registered, and its ``<venue>`` prefix must match ``entry.market.exchange``
    (catches a copy-paste config mistake, e.g. a BTC bot referencing an
    ETH-series rule).

    Args:
        entry: The bot roster entry whose ``winning_rule`` (if any) will be
            checked.

    Raises:
        ValueError: If the rule name is not registered, or if its venue
            prefix doesn't match ``entry.market.exchange``.
    """
    if entry.winning_rule is None:
        return

    from src.rules.registry import rules as rule_registry

    name = entry.winning_rule.name
    try:
        rule_registry.get(name)
    except KeyError:
        available = rule_registry.names
        raise ValueError(
            f"Bot '{entry.id}' declares unknown winning rule {name!r}. "
            f"Registered winning rules: {available}. "
            "Register the rule or remove it from the bot's config."
        ) from None

    venue = name.split(".", 1)[0]
    if venue != entry.market.exchange:
        raise ValueError(
            f"Bot '{entry.id}' references winning rule {name!r} (venue={venue!r}) "
            f"but trades on exchange={entry.market.exchange!r}. "
            "The winning rule's venue prefix must match the bot's market exchange."
        )


logger = logging.getLogger(__name__)

# Watchdog tick interval in seconds.
_WATCHDOG_INTERVAL_S: float = 5.0

# Heartbeat interval the bots are expected to beat at (manager's expectation).
_HEARTBEAT_INTERVAL_S: float = float(os.environ.get("HEARTBEAT_INTERVAL_S", "30"))

# Maximum restarts in the burst window before entering cooldown.
_MAX_RESTARTS_IN_BURST = 3
_BURST_WINDOW_S: float = 60.0

# Cooldown period after excessive restarts.
_COOLDOWN_S: float = 300.0


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class BotProcess:
    """A running bot subprocess with its heartbeat and restart metadata.

    Args:
        entry: The roster entry this process was spawned from.
        proc: The asyncio subprocess handle.
        spawned_at: UTC datetime when the process was launched.
        restart_count: Cumulative restart count (0 = first spawn).
        mode_override: If set, the mode the supervisor forced (paper downgrade).
    """

    entry: BotEntry
    proc: asyncio.subprocess.Process
    spawned_at: datetime
    restart_count: int = 0
    mode_override: str | None = None


@dataclass
class BotStatus:
    """Snapshot of a single bot's runtime status, emitted by ``status()``.

    Args:
        bot_id: Stable identifier.
        strategy: Strategy name.
        mode: Effective mode (paper/live).
        pid: Current process PID, or ``None`` if not running.
        restart_count: Times this bot has been restarted.
        heartbeat_age_s: Seconds since the last heartbeat.
        snapshot_lag_s: Seconds since the last snapshot.
        last_error: Most recent error string from the bot, if any.
        spawned_at: When the current process was started.
    """

    bot_id: str
    strategy: str
    mode: str
    pid: int | None
    restart_count: int
    heartbeat_age_s: float
    snapshot_lag_s: float
    last_error: str | None
    spawned_at: datetime


@dataclass
class ReloadReport:
    """Summary of changes applied during a reload.

    Args:
        added: Bot ids that were added (new spawn).
        removed: Bot ids that were removed (SIGTERM'd).
        unchanged: Bot ids that were left running.
    """

    added: list[str]
    removed: list[str]
    unchanged: list[str]


@dataclass
class SupervisorDeps:
    """Infrastructure dependencies injected into the Supervisor.

    Args:
        state: Bot state repository.
        kill_switch: Read-only kill-switch interface.
        audit: Append-only audit logger.
        clock: Injectable clock.
        spawn: Async callable that launches a bot subprocess.  Injectable for
            tests so no real subprocess is needed.
    """

    state: StateRepository
    kill_switch: KillSwitchReader
    audit: AuditLogger
    clock: Clock
    spawn: Callable[[BotEntry, dict[str, str]], Awaitable[asyncio.subprocess.Process]]


# ---------------------------------------------------------------------------
# Default spawn implementation
# ---------------------------------------------------------------------------


async def _default_spawn(entry: BotEntry, env: dict[str, str]) -> asyncio.subprocess.Process:
    """Launch a bot subprocess with ``uv run python -m src.bots.runner``.

    Args:
        entry: The bot entry to spawn.
        env: Environment variables for the child process.

    Returns:
        The started :class:`asyncio.subprocess.Process`.
    """
    return await asyncio.create_subprocess_exec(
        "uv",
        "run",
        "python",
        "-m",
        "src.bots.runner",
        "--bot-id",
        entry.id,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )


# ---------------------------------------------------------------------------
# Supervisor
# ---------------------------------------------------------------------------


class Supervisor:
    """Manages the lifecycle of all bot subprocesses.

    Args:
        roster: Validated bot roster from ``config/bots.yaml``.
        sock_dir: Directory for UNIX heartbeat sockets.
        live_allowlist: Set of bot ids permitted to run in live mode.
        deps: Injected infrastructure dependencies.
    """

    def __init__(
        self,
        roster: BotsRoster,
        sock_dir: Path,
        live_allowlist: set[str],
        deps: SupervisorDeps,
    ) -> None:
        self._roster = roster
        self._sock_dir = sock_dir
        self._live_allowlist = live_allowlist
        self._deps = deps

        # Running processes keyed by bot_id.
        self._running: dict[str, BotProcess] = {}

        # Restart timestamps per bot for burst-rate-limit logic.
        # bot_id -> deque of restart timestamps (as floats).
        self._restart_times: dict[str, collections.deque[float]] = collections.defaultdict(
            lambda: collections.deque(maxlen=100)
        )

        # Cooldown expiry per bot (epoch float).
        self._cooldown_until: dict[str, float] = {}

        # Per-bot stdout/stderr ring buffers (last 4 KiB).
        self._stdout_ring: dict[str, bytearray] = {}
        self._stderr_ring: dict[str, bytearray] = {}

        self._heartbeat_server = HeartbeatServer(sock_dir, deps.clock)
        self._watchdog_task: asyncio.Task[None] | None = None
        self._status_task: asyncio.Task[None] | None = None
        self._stopping = False

        # Status JSON path (written every 5 s via atomic rename).
        self._status_path = sock_dir / "status.json"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Spawn all bots from the roster, start the heartbeat server, and run the watchdog.

        Returns after all bots are spawned.  The watchdog loop runs as a
        background task until :meth:`stop` is called.
        """
        bot_ids = [e.id for e in self._roster.bots]
        await self._heartbeat_server.start(bot_ids)

        for entry in self._roster.bots:
            await self._spawn(entry)

        self._watchdog_task = asyncio.create_task(self._watchdog_loop(), name="supervisor-watchdog")
        self._status_task = asyncio.create_task(self._status_loop(), name="supervisor-status")
        logger.info(
            "Supervisor started %d bot(s): %s",
            len(self._roster.bots),
            [e.id for e in self._roster.bots],
        )

    async def stop(self) -> None:
        """Cancel the watchdog loop and SIGTERM all children."""
        self._stopping = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._watchdog_task
        if self._status_task is not None:
            self._status_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._status_task
        await self.drain(grace_s=float(os.environ.get("DRAIN_GRACE_S", "30")))
        await self._heartbeat_server.close()

    async def drain(self, grace_s: float = 30.0) -> None:
        """SIGTERM all children and wait up to ``grace_s`` seconds for exit.

        Any survivors after the grace period receive SIGKILL.

        Args:
            grace_s: Grace period in seconds before SIGKILL.
        """
        if not self._running:
            return

        logger.info("Draining %d bot(s)...", len(self._running))
        for bp in self._running.values():
            _send_signal(bp.proc, signal.SIGTERM)

        deadline = asyncio.get_event_loop().time() + grace_s
        for bot_id, bp in list(self._running.items()):
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                remaining = 0.0
            try:
                await asyncio.wait_for(bp.proc.wait(), timeout=remaining)
                logger.info("Bot %s exited cleanly during drain.", bot_id)
            except TimeoutError:
                logger.warning("Bot %s did not exit within grace period — sending SIGKILL.", bot_id)
                _send_signal(bp.proc, signal.SIGKILL)
                await bp.proc.wait()

        self._running.clear()
        logger.info("Drain complete.")

    async def reload(self, new_roster: BotsRoster) -> ReloadReport:
        """Apply a new roster incrementally without disrupting unchanged bots.

        Computes the diff of new vs running bots:
        - Added: spawn.
        - Removed: SIGTERM and await exit.
        - Unchanged: leave alone (same PID continues running).

        The live allow-list is NOT updated here — that requires a full restart.

        Args:
            new_roster: The new :class:`BotsRoster` to apply.

        Returns:
            :class:`ReloadReport` describing the changes.
        """
        old_ids = set(self._running)
        new_ids = {e.id for e in new_roster.bots}
        new_entries = {e.id: e for e in new_roster.bots}

        added_ids = new_ids - old_ids
        removed_ids = old_ids - new_ids
        unchanged_ids = old_ids & new_ids

        # Remove bots no longer in the roster.
        for bot_id in removed_ids:
            bp = self._running.pop(bot_id)
            _send_signal(bp.proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(bp.proc.wait(), timeout=30.0)
            except TimeoutError:
                _send_signal(bp.proc, signal.SIGKILL)
                await bp.proc.wait()
            self._heartbeat_server.remove_bot(bot_id)
            logger.info("Reload: removed bot %s", bot_id)

        # Spawn new bots.
        for bot_id in added_ids:
            entry = new_entries[bot_id]
            self._heartbeat_server.add_bot(bot_id)
            await self._spawn(entry)
            logger.info("Reload: added bot %s", bot_id)

        self._roster = new_roster
        return ReloadReport(
            added=sorted(added_ids),
            removed=sorted(removed_ids),
            unchanged=sorted(unchanged_ids),
        )

    def status(self) -> list[BotStatus]:
        """Return the current status of all supervised bots.

        Returns:
            List of :class:`BotStatus` for each bot in the roster.
        """
        result: list[BotStatus] = []
        for bot_id, bp in self._running.items():
            health = self._heartbeat_server.health(bot_id)
            effective_mode = bp.mode_override if bp.mode_override else bp.entry.mode
            result.append(
                BotStatus(
                    bot_id=bot_id,
                    strategy=bp.entry.strategy,
                    mode=effective_mode,
                    pid=bp.proc.pid,
                    restart_count=bp.restart_count,
                    heartbeat_age_s=health.age_s,
                    snapshot_lag_s=health.snapshot_lag_s,
                    last_error=health.last_error,
                    spawned_at=bp.spawned_at,
                )
            )
        return result

    # ------------------------------------------------------------------
    # Internal spawn logic
    # ------------------------------------------------------------------

    async def _spawn(self, entry: BotEntry) -> None:
        """Spawn a single bot subprocess.

        Enforces the live allow-list (invariant 1 — fail-closed): a bot
        configured ``mode: live`` but absent from the allow-list is downgraded
        to paper via ``BOT_MODE_OVERRIDE=paper`` in the child env, and a
        ``critical`` audit row is written.

        Args:
            entry: The bot entry to spawn.
        """
        # Validate signal subscriptions before doing anything else.
        # Unknown signals fail fast with a clear error (invariant: fail-closed).
        validate_bot_signals(entry)
        # Validate the optional provisional winning rule reference, if any.
        validate_bot_winning_rule(entry)

        env = self._build_env(entry)
        mode_override: str | None = None

        if entry.mode == "live" and entry.id not in self._live_allowlist:
            logger.critical(
                "Bot %s configured mode=live but NOT in live allow-list — "
                "downgrading to paper (invariant 1).",
                entry.id,
            )
            env["BOT_MODE_OVERRIDE"] = "paper"
            mode_override = "paper"
            await self._deps.audit.write(
                bot_id=entry.id,
                kind="live_downgrade",
                payload={
                    "reason": "bot_id not in live_allowlist",
                    "configured_mode": "live",
                    "effective_mode": "paper",
                    "invariant": "1",
                },
            )

        restart_count = 0
        if entry.id in self._running:
            restart_count = self._running[entry.id].restart_count + 1

        proc = await self._deps.spawn(entry, env)

        # Start background tasks to drain stdout/stderr into ring buffers.
        # Store references to prevent garbage collection before the task finishes.
        self._drain_tasks = getattr(self, "_drain_tasks", [])
        self._drain_tasks.append(
            asyncio.create_task(
                self._drain_pipe(entry.id, proc.stdout, self._stdout_ring, "stdout"),
                name=f"drain-stdout-{entry.id}",
            )
        )
        self._drain_tasks.append(
            asyncio.create_task(
                self._drain_pipe(entry.id, proc.stderr, self._stderr_ring, "stderr"),
                name=f"drain-stderr-{entry.id}",
            )
        )

        self._running[entry.id] = BotProcess(
            entry=entry,
            proc=proc,
            spawned_at=self._deps.clock.now(),
            restart_count=restart_count,
            mode_override=mode_override,
        )
        logger.info(
            "Spawned bot %s (PID=%s, mode=%s, restart=%d)",
            entry.id,
            proc.pid,
            mode_override or entry.mode,
            restart_count,
        )

    async def _respawn(self, entry: BotEntry) -> None:
        """Rate-limited respawn with burst protection and cooldown.

        Max 3 restarts in 60 s; after that, enter a 5-minute cooldown and
        emit a ``critical`` alert.

        Args:
            entry: The bot entry to respawn.
        """
        bot_id = entry.id
        now_ts = time.monotonic()

        # Check if in cooldown.
        cooldown_exp = self._cooldown_until.get(bot_id, 0.0)
        if now_ts < cooldown_exp:
            remaining = cooldown_exp - now_ts
            logger.warning(
                "Bot %s in cooldown — not respawning for %.0f more seconds.",
                bot_id,
                remaining,
            )
            return

        # Purge stale restart timestamps.
        dq = self._restart_times[bot_id]
        while dq and (now_ts - dq[0]) > _BURST_WINDOW_S:
            dq.popleft()

        if len(dq) >= _MAX_RESTARTS_IN_BURST:
            logger.critical(
                "Bot %s exceeded %d restarts in %ds — entering %.0fs cooldown.",
                bot_id,
                _MAX_RESTARTS_IN_BURST,
                int(_BURST_WINDOW_S),
                _COOLDOWN_S,
            )
            self._cooldown_until[bot_id] = now_ts + _COOLDOWN_S
            await self._deps.audit.write(
                bot_id=bot_id,
                kind="bot_cooldown",
                payload={
                    "reason": "exceeded_restart_burst",
                    "restarts_in_window": len(dq),
                    "cooldown_s": _COOLDOWN_S,
                },
            )
            return

        dq.append(now_ts)
        logger.info("Respawning bot %s (restart #%d).", bot_id, len(dq))
        await self._spawn(entry)

    def _build_env(self, entry: BotEntry) -> dict[str, str]:
        """Build the child process environment.

        Inherits the current environment and sets supervisor-specific vars.

        Args:
            entry: The bot entry being spawned.

        Returns:
            Dict suitable for ``asyncio.create_subprocess_exec(env=...)``.
        """
        env = dict(os.environ)
        sock_dir = str(self._sock_dir)
        env.setdefault("MANAGER_SOCKET_DIR", sock_dir)
        env.setdefault("SECRETS_DIR", "/run/secrets")
        # Pass through Postgres credentials from supervisor env.
        _pg_keys = (
            "POSTGRES_HOST",
            "POSTGRES_PORT",
            "POSTGRES_DB",
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
        )
        for k in _pg_keys:
            if k in os.environ:
                env[k] = os.environ[k]
        return env

    # ------------------------------------------------------------------
    # Watchdog loop
    # ------------------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """Periodically check all bots' heartbeat age and process exit status."""
        while not self._stopping:
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL_S)
                await self._check_bots()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Watchdog loop error: %s", exc)

    async def _check_bots(self) -> None:
        """Inspect all running bots; respawn dead or silent ones."""
        stale_threshold = 2 * _HEARTBEAT_INTERVAL_S

        for bot_id, bp in list(self._running.items()):
            # Check if the process has exited.
            if bp.proc.returncode is not None:
                exit_code = bp.proc.returncode
                if exit_code != 0:
                    logger.warning("Bot %s exited with code %d — respawning.", bot_id, exit_code)
                    await self._respawn(bp.entry)
                else:
                    # Clean exit — remove from running (drain path).
                    logger.info("Bot %s exited cleanly (code 0).", bot_id)
                    self._running.pop(bot_id, None)
                continue

            # Check heartbeat staleness.
            health = self._heartbeat_server.health(bot_id)
            if health.age_s > stale_threshold:
                logger.warning(
                    "Bot %s heartbeat stale (age=%.1fs > %.1fs) — respawning.",
                    bot_id,
                    health.age_s,
                    stale_threshold,
                )
                _send_signal(bp.proc, signal.SIGKILL)
                await bp.proc.wait()
                await self._respawn(bp.entry)

    # ------------------------------------------------------------------
    # Status JSON writer
    # ------------------------------------------------------------------

    async def _status_loop(self) -> None:
        """Write status.json every 5 seconds via atomic rename."""
        while not self._stopping:
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL_S)
                self._write_status_json()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Status write error (non-fatal): %s", exc)

    def _write_status_json(self) -> None:
        """Atomically write the current status to ``status.json``."""
        statuses = self.status()
        data: list[dict[str, Any]] = []
        for s in statuses:
            data.append(
                {
                    "bot_id": s.bot_id,
                    "strategy": s.strategy,
                    "mode": s.mode,
                    "pid": s.pid,
                    "restart_count": s.restart_count,
                    "heartbeat_age_s": s.heartbeat_age_s,
                    "snapshot_lag_s": s.snapshot_lag_s,
                    "last_error": s.last_error,
                    "spawned_at": s.spawned_at.isoformat(),
                }
            )
        tmp_path = self._status_path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(self._status_path)

    # ------------------------------------------------------------------
    # I/O ring buffer drainer
    # ------------------------------------------------------------------

    @staticmethod
    async def _drain_pipe(
        bot_id: str,
        stream: asyncio.StreamReader | None,
        ring: dict[str, bytearray],
        label: str,
    ) -> None:
        """Read from a subprocess pipe into a 4 KiB ring buffer.

        Args:
            bot_id: Bot identifier for the ring buffer key.
            stream: The asyncio stream reader (stdout or stderr).
            ring: Shared ring buffer dict.
            label: "stdout" or "stderr" for logging.
        """
        if stream is None:
            return
        if bot_id not in ring:
            ring[bot_id] = bytearray()
        buf = ring[bot_id]
        try:
            while True:
                chunk = await stream.read(1024)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > 4096:
                    del buf[: len(buf) - 4096]
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.debug("Pipe drain error (%s, %s): %s", bot_id, label, exc)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _send_signal(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """Send a signal to a subprocess, ignoring ProcessLookupError.

    Args:
        proc: The subprocess to signal.
        sig: The signal to send.
    """
    with contextlib.suppress(ProcessLookupError):
        proc.send_signal(sig)  # Process already gone — safe to ignore.
