"""Integration tests for the Supervisor using an injectable spawn function.

No real subprocesses are launched — the ``spawn`` parameter of
``SupervisorDeps`` is replaced with an async callable that returns a
``FakeProcess``.

Tests:
1. Fail-closed allow-list: mode=live bot not in allow-list spawns as paper
   and emits a critical audit row.
2. Reload diff: start 3 bots [A, B, C], reload with [A, C, D]; B removed, D
   added, A and C untouched.
3. Invalid bots.yaml: BotsRoster validation raises with a clear message before
   any supervisor action.
4. Drain: start 2 bots, call drain; both processes receive SIGTERM.
5. Watchdog: bot process "dies" (exit code != 0); supervisor respawns it.
"""

from __future__ import annotations

import shutil
import signal
import tempfile
from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from src.bots.base import Clock
from src.manager.config import BotEntry, BotsRoster, MarketRef, RiskOverride, SecretRef
from src.manager.supervisor import Supervisor, SupervisorDeps

from tests.fixtures.clocks import ManualClock
from tests.fixtures.state import InMemoryAuditLogger, InMemoryKillSwitch, InMemoryStateRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def short_tmp() -> Any:
    """Return a short tmp directory path to avoid AF_UNIX path-length limits.

    UNIX socket paths are capped at ~104 chars on macOS.  pytest's tmp_path
    uses very long paths; this fixture uses tempfile.mkdtemp() instead.
    """
    d = tempfile.mkdtemp(prefix="sup_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(
    bot_id: str,
    mode: str = "paper",
    strategy: str = "null_strategy",
) -> BotEntry:
    """Build a minimal BotEntry for testing."""
    return BotEntry(
        id=bot_id,
        strategy=strategy,
        market=MarketRef(exchange="echo", market_id="ECHO-TEST"),
        mode=mode,  # type: ignore[arg-type]
        schedule="every:60s",
        risk=RiskOverride(
            max_position_notional=Decimal("100"),
            max_daily_loss=Decimal("10"),
            max_orders_per_minute=5,
        ),
        secrets=SecretRef(exchange_credentials="echo.dev"),
    )


def _roster(*entries: BotEntry) -> BotsRoster:
    return BotsRoster(bots=list(entries))


class FakeProcess:
    """Minimal stand-in for asyncio.subprocess.Process.

    Args:
        pid: Fake PID for the process.
        exit_code: If set, ``returncode`` is pre-populated (simulates
            a process that exited immediately).
    """

    def __init__(self, pid: int = 1234, exit_code: int | None = None) -> None:
        self.pid = pid
        self.returncode: int | None = exit_code
        self.stdout: Any = None
        self.stderr: Any = None
        self._signals_received: list[int] = []

    def send_signal(self, sig: int) -> None:
        self._signals_received.append(sig)
        if sig in (signal.SIGTERM, signal.SIGKILL):
            self.returncode = 0

    async def wait(self) -> int:
        return self.returncode or 0


def _make_fake_spawn(
    exit_code: int | None = None,
) -> tuple[list[tuple[BotEntry, dict[str, str]]], Callable[..., Awaitable[Any]]]:
    """Return a spawn call log and an async spawn function that records calls."""
    calls: list[tuple[BotEntry, dict[str, str]]] = []
    _pid_counter = [0]

    async def _spawn(entry: BotEntry, env: dict[str, str]) -> FakeProcess:
        _pid_counter[0] += 1
        calls.append((entry, env))
        return FakeProcess(pid=_pid_counter[0], exit_code=exit_code)

    return calls, _spawn


def _make_deps(
    clock: Clock | None = None,
    spawn: Callable[..., Awaitable[Any]] | None = None,
) -> SupervisorDeps:
    if clock is None:
        clock = ManualClock()
    if spawn is None:
        _, spawn = _make_fake_spawn()
    return SupervisorDeps(
        state=InMemoryStateRepository(),  # type: ignore[arg-type]
        kill_switch=InMemoryKillSwitch(),  # type: ignore[arg-type]
        audit=InMemoryAuditLogger(),  # type: ignore[arg-type]
        clock=clock,
        spawn=spawn,
    )


# ---------------------------------------------------------------------------
# Test 1: fail-closed allow-list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_downgrade_when_not_in_allowlist(short_tmp: Path) -> None:
    """Bot configured mode=live but absent from allow-list spawns as paper.

    Expects:
    - BOT_MODE_OVERRIDE=paper in the child env.
    - A 'live_downgrade' critical audit row is written.
    """
    calls, fake_spawn = _make_fake_spawn()
    audit = InMemoryAuditLogger()
    deps = SupervisorDeps(
        state=InMemoryStateRepository(),  # type: ignore[arg-type]
        kill_switch=InMemoryKillSwitch(),  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        clock=ManualClock(),
        spawn=fake_spawn,
    )

    roster = _roster(_entry("live-bot", mode="live"))
    supervisor = Supervisor(
        roster=roster,
        sock_dir=short_tmp / "s",
        live_allowlist=set(),  # empty — bot NOT allowed
        deps=deps,
    )

    await supervisor.start()

    # The bot should have been spawned.
    assert len(calls) == 1
    entry_spawned, env_used = calls[0]
    assert entry_spawned.id == "live-bot"

    # BOT_MODE_OVERRIDE must be set to "paper".
    assert env_used.get("BOT_MODE_OVERRIDE") == "paper"

    # A live_downgrade audit row must have been written.
    downgrade_rows = [r for r in audit.rows if r["kind"] == "live_downgrade"]
    assert len(downgrade_rows) == 1
    assert downgrade_rows[0]["bot_id"] == "live-bot"
    assert downgrade_rows[0]["payload"]["effective_mode"] == "paper"

    await supervisor.stop()


@pytest.mark.asyncio
async def test_live_mode_allowed_when_in_allowlist(short_tmp: Path) -> None:
    """Bot in the allow-list with mode=live spawns without BOT_MODE_OVERRIDE."""
    calls, fake_spawn = _make_fake_spawn()
    audit = InMemoryAuditLogger()
    deps = SupervisorDeps(
        state=InMemoryStateRepository(),  # type: ignore[arg-type]
        kill_switch=InMemoryKillSwitch(),  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        clock=ManualClock(),
        spawn=fake_spawn,
    )

    roster = _roster(_entry("live-bot", mode="live"))
    supervisor = Supervisor(
        roster=roster,
        sock_dir=short_tmp / "s",
        live_allowlist={"live-bot"},  # bot IS allowed
        deps=deps,
    )

    await supervisor.start()

    assert len(calls) == 1
    _, env_used = calls[0]
    assert "BOT_MODE_OVERRIDE" not in env_used

    downgrade_rows = [r for r in audit.rows if r["kind"] == "live_downgrade"]
    assert len(downgrade_rows) == 0

    await supervisor.stop()


# ---------------------------------------------------------------------------
# Test 2: reload diff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reload_diff_adds_removes_leaves_unchanged(short_tmp: Path) -> None:
    """Reload with [A, C, D] from [A, B, C]: B removed, D added, A/C unchanged.

    The unchanged bots must keep their original FakeProcess instance (same
    object — proves the supervisor didn't restart them).
    """
    calls, fake_spawn = _make_fake_spawn()
    deps = _make_deps(spawn=fake_spawn)

    # Initial roster: A, B, C
    roster_abc = _roster(
        _entry("bot-a"),
        _entry("bot-b"),
        _entry("bot-c"),
    )
    supervisor = Supervisor(
        roster=roster_abc,
        sock_dir=short_tmp / "s",
        live_allowlist=set(),
        deps=deps,
    )
    await supervisor.start()
    assert len(calls) == 3

    # Capture original process objects for A and C.
    proc_a_before = supervisor._running["bot-a"].proc
    proc_c_before = supervisor._running["bot-c"].proc

    # Reload to [A, C, D].
    roster_acd = _roster(
        _entry("bot-a"),
        _entry("bot-c"),
        _entry("bot-d"),
    )
    report = await supervisor.reload(roster_acd)

    assert sorted(report.added) == ["bot-d"]
    assert sorted(report.removed) == ["bot-b"]
    assert sorted(report.unchanged) == ["bot-a", "bot-c"]

    # B is gone.
    assert "bot-b" not in supervisor._running

    # D is running.
    assert "bot-d" in supervisor._running

    # A and C are the same process objects (not restarted).
    assert supervisor._running["bot-a"].proc is proc_a_before
    assert supervisor._running["bot-c"].proc is proc_c_before

    await supervisor.stop()


# ---------------------------------------------------------------------------
# Test 3: invalid bots.yaml raises with clear message
# ---------------------------------------------------------------------------


def test_invalid_roster_duplicate_ids_raises() -> None:
    """Duplicate bot ids in BotsRoster raise ValidationError with the id."""
    with pytest.raises(ValidationError) as exc_info:
        _roster(_entry("dup"), _entry("dup"))
    assert "dup" in str(exc_info.value)


def test_invalid_roster_bad_schedule_raises() -> None:
    """An unparseable schedule string raises ValidationError."""
    with pytest.raises((ValidationError, NotImplementedError)):
        BotEntry(
            id="bad-bot",
            strategy="null_strategy",
            market=MarketRef(exchange="echo", market_id="TEST"),
            mode="paper",
            schedule="not-a-schedule",
            risk=RiskOverride(
                max_position_notional=Decimal("100"),
                max_daily_loss=Decimal("10"),
                max_orders_per_minute=5,
            ),
            secrets=SecretRef(exchange_credentials="echo.dev"),
        )


def test_invalid_roster_bad_exchange_raises() -> None:
    """An unknown exchange name raises ValidationError."""
    with pytest.raises(ValidationError):
        BotEntry(
            id="bad-bot",
            strategy="null_strategy",
            market=MarketRef(exchange="unknown_exchange", market_id="TEST"),  # type: ignore[arg-type]
            mode="paper",
            schedule="every:60s",
            risk=RiskOverride(
                max_position_notional=Decimal("100"),
                max_daily_loss=Decimal("10"),
                max_orders_per_minute=5,
            ),
            secrets=SecretRef(exchange_credentials="echo.dev"),
        )


# ---------------------------------------------------------------------------
# Test 4: drain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_sends_sigterm_to_all_bots(short_tmp: Path) -> None:
    """Drain sends SIGTERM to each bot process."""
    calls, fake_spawn = _make_fake_spawn()
    deps = _make_deps(spawn=fake_spawn)

    roster = _roster(_entry("bot-x"), _entry("bot-y"))
    supervisor = Supervisor(
        roster=roster,
        sock_dir=short_tmp / "s",
        live_allowlist=set(),
        deps=deps,
    )
    await supervisor.start()
    assert len(calls) == 2

    # Collect the fake processes before drain clears them.
    proc_x = supervisor._running["bot-x"].proc
    proc_y = supervisor._running["bot-y"].proc

    await supervisor.drain(grace_s=1.0)

    # Both processes must have received SIGTERM.
    assert signal.SIGTERM in proc_x._signals_received  # type: ignore[attr-defined]
    assert signal.SIGTERM in proc_y._signals_received  # type: ignore[attr-defined]

    # After drain, the running dict must be empty.
    assert supervisor._running == {}


# ---------------------------------------------------------------------------
# Test 5: watchdog respawns a dead bot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_respawns_dead_bot(short_tmp: Path) -> None:
    """Watchdog detects a non-zero exit and respawns the bot."""
    spawn_call_count = [0]
    procs_created: list[FakeProcess] = []

    async def counting_spawn(entry: BotEntry, env: dict[str, str]) -> FakeProcess:
        spawn_call_count[0] += 1
        # First spawn: process "dies" immediately with code 1.
        # Subsequent spawns: process stays alive.
        exit_code = 1 if spawn_call_count[0] == 1 else None
        proc = FakeProcess(pid=spawn_call_count[0], exit_code=exit_code)
        procs_created.append(proc)
        return proc

    deps = SupervisorDeps(
        state=InMemoryStateRepository(),  # type: ignore[arg-type]
        kill_switch=InMemoryKillSwitch(),  # type: ignore[arg-type]
        audit=InMemoryAuditLogger(),  # type: ignore[arg-type]
        clock=ManualClock(),
        spawn=counting_spawn,
    )

    roster = _roster(_entry("dying-bot"))
    supervisor = Supervisor(
        roster=roster,
        sock_dir=short_tmp / "s",
        live_allowlist=set(),
        deps=deps,
    )
    await supervisor.start()
    assert spawn_call_count[0] == 1

    # Run one watchdog check — the dead process should trigger a respawn.
    await supervisor._check_bots()

    # The bot should have been respawned (second spawn call).
    assert spawn_call_count[0] == 2

    await supervisor.stop()
