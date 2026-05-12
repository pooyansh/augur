"""Manager CLI — ``python -m src.manager``.

Subcommands:
  start   Load roster, spawn all bots, run the supervisor forever.
  reload  Send SIGHUP to the manager PID (manager reloads config on SIGHUP).
  drain   Send SIGTERM to the manager PID (manager runs Supervisor.drain).
  status  Print the supervisor status JSON to stdout.
  inspect-state  Pretty-print the latest snapshot for a bot.
"""

from __future__ import annotations

__all__ = ["cli"]

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import click

from src.manager.config import load_roster

logger = logging.getLogger(__name__)

# Default paths (overridable via env).
_DEFAULT_SOCK_DIR = Path(os.environ.get("MANAGER_SOCKET_DIR", "/run/manager"))
_DEFAULT_PID_FILE = _DEFAULT_SOCK_DIR / "manager.pid"
_DEFAULT_STATUS_FILE = _DEFAULT_SOCK_DIR / "status.json"
_DEFAULT_BOTS_CONFIG = Path(os.environ.get("BOTS_CONFIG", "config/bots.yaml"))


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """Prediction-market bot manager.

    Manages the lifecycle of all bot subprocesses.  Run ``start`` to launch
    the supervisor.  Use ``reload``, ``drain``, and ``status`` to interact
    with a running manager process.
    """


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--bots-file",
    default=str(_DEFAULT_BOTS_CONFIG),
    show_default=True,
    help="Path to config/bots.yaml.",
)
@click.option(
    "--sock-dir",
    default=str(_DEFAULT_SOCK_DIR),
    show_default=True,
    help="Directory for UNIX heartbeat sockets and status files.",
)
@click.option(
    "--secrets-dir",
    default=os.environ.get("SECRETS_DIR", "/run/secrets"),
    show_default=True,
    help="Directory containing decrypted secret YAML files.",
)
def start(bots_file: str, sock_dir: str, secrets_dir: str) -> None:
    """Load the bot roster and run the supervisor until SIGTERM.

    Validates config/bots.yaml fully before spawning any bot.  An invalid
    entry fails the whole start — zero bots are spawned.

    Writes the manager PID to ``<sock-dir>/manager.pid`` so that ``reload``
    and ``drain`` can find the process.
    """
    from src.bots.base import Clock
    from src.manager.supervisor import Supervisor, SupervisorDeps, _default_spawn
    from src.secrets.install import configure_logging_with_redaction
    from src.secrets.loader import load_secrets

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    loaded_secrets = load_secrets(Path(secrets_dir))
    configure_logging_with_redaction(loaded_secrets)

    # Load and validate roster — exit non-zero on any validation error.
    try:
        roster = load_roster(Path(bots_file))
    except Exception as exc:
        click.echo(f"ERROR: Invalid roster: {exc}", err=True)
        sys.exit(1)

    # Load live allow-list.
    live_allowlist: set[str] = _load_live_allowlist(Path(secrets_dir))

    # Build infrastructure.
    sock_path = Path(sock_dir)
    sock_path.mkdir(parents=True, exist_ok=True)

    session_factory = _build_session_factory()
    state: object
    kill_switch: object
    audit: object
    if session_factory is not None:
        from src.state.repository import AuditLogger, KillSwitchReader, StateRepository

        state = StateRepository(session_factory)  # type: ignore[arg-type]
        kill_switch = KillSwitchReader(session_factory)  # type: ignore[arg-type]
        audit = AuditLogger(session_factory)  # type: ignore[arg-type]
    else:
        logger.warning("POSTGRES_HOST not set — using in-memory stubs.")
        from tests.fixtures.state import (
            InMemoryAuditLogger,
            InMemoryKillSwitch,
            InMemoryStateRepository,
        )

        state = InMemoryStateRepository()
        kill_switch = InMemoryKillSwitch()
        audit = InMemoryAuditLogger()

    deps = SupervisorDeps(
        state=state,  # type: ignore[arg-type]
        kill_switch=kill_switch,  # type: ignore[arg-type]
        audit=audit,  # type: ignore[arg-type]
        clock=Clock(),
        spawn=_default_spawn,
    )

    supervisor = Supervisor(
        roster=roster,
        sock_dir=sock_path,
        live_allowlist=live_allowlist,
        deps=deps,
    )

    async def _run() -> None:
        import httpx

        from src.signals.registry import signals as signal_registry
        from src.signals.runner import SignalsRuntime
        from src.signals.storage import SignalStorage

        # Auto-discover all registered signals.
        signal_registry.autodiscover()

        # Build the signals runtime (no-op storage stub if no Postgres).
        signals_runtime: SignalsRuntime | None = None
        http_client: httpx.AsyncClient | None = None

        if session_factory is not None:
            from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

            sf: async_sessionmaker[AsyncSession] = session_factory  # type: ignore[assignment]
            sig_storage = SignalStorage(sf)
            http_client = httpx.AsyncClient()
            signals_runtime = SignalsRuntime(
                registry=signal_registry,
                storage=sig_storage,
                clock=Clock(),
                http=http_client,
            )
            # Pre-subscribe all signals declared in the roster.
            for entry in roster.bots:
                for sub in entry.signals:
                    signals_runtime.subscribe(sub.name, dict(sub.params))
            await signals_runtime.start()
            logger.info("SignalsRuntime started.")
        else:
            logger.warning("POSTGRES_HOST not set — signals runtime disabled.")

        # Write PID file.
        pid_file = sock_path / "manager.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        loop = asyncio.get_running_loop()
        stop_event = asyncio.Event()

        # SIGTERM → drain and exit.
        def _on_sigterm() -> None:
            logger.info("Manager received SIGTERM — draining.")
            stop_event.set()

        # SIGHUP → reload roster.
        reload_file = sock_path / "reload.path"

        def _on_sighup() -> None:
            asyncio.get_event_loop().create_task(_do_reload(supervisor, reload_file, sock_path))

        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
        loop.add_signal_handler(signal.SIGHUP, _on_sighup)

        await supervisor.start()
        logger.info("Manager started (PID=%d). Roster: %d bots.", os.getpid(), len(roster.bots))

        # Wait for stop signal.
        await stop_event.wait()
        await supervisor.stop()

        # Shut down signals runtime.
        if signals_runtime is not None:
            await signals_runtime.stop()
            logger.info("SignalsRuntime stopped.")
        if http_client is not None:
            await http_client.aclose()

        pid_file.unlink(missing_ok=True)
        logger.info("Manager stopped.")

    import contextlib

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


async def _do_reload(supervisor: object, reload_file: Path, sock_path: Path) -> None:
    """Reload the roster from the path written to ``reload.path``.

    Args:
        supervisor: The running :class:`~src.manager.supervisor.Supervisor`.
        reload_file: Path to the file containing the new roster path.
        sock_path: Supervisor socket directory.
    """
    from src.manager.supervisor import Supervisor as Sup

    assert isinstance(supervisor, Sup)

    bots_file = Path(
        reload_file.read_text(encoding="utf-8").strip()
        if reload_file.exists()
        else "config/bots.yaml"
    )
    try:
        new_roster = load_roster(bots_file)
        report = await supervisor.reload(new_roster)
        logger.info(
            "Reload complete: added=%s removed=%s unchanged=%s",
            report.added,
            report.removed,
            report.unchanged,
        )
    except Exception as exc:
        logger.error("Reload failed: %s", exc)


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--bots-file",
    default=str(_DEFAULT_BOTS_CONFIG),
    show_default=True,
    help="Path to the new bots.yaml to reload.",
)
@click.option(
    "--sock-dir",
    default=str(_DEFAULT_SOCK_DIR),
    show_default=True,
    help="Manager socket directory.",
)
def reload(bots_file: str, sock_dir: str) -> None:
    """Signal a running manager to reload config/bots.yaml.

    Writes the roster path to ``<sock-dir>/reload.path`` then sends SIGHUP
    to the manager PID.  The manager reloads on SIGHUP.
    """
    sock_path = Path(sock_dir)
    pid_file = sock_path / "manager.pid"

    if not pid_file.exists():
        click.echo("ERROR: Manager PID file not found. Is the manager running?", err=True)
        sys.exit(1)

    pid = int(pid_file.read_text(encoding="utf-8").strip())
    reload_file = sock_path / "reload.path"
    reload_file.write_text(str(bots_file), encoding="utf-8")

    try:
        os.kill(pid, signal.SIGHUP)
        click.echo(f"Sent SIGHUP to manager (PID={pid}). Reload in progress.")
    except ProcessLookupError:
        click.echo(f"ERROR: Process {pid} not found. Stale PID file?", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# drain
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--sock-dir",
    default=str(_DEFAULT_SOCK_DIR),
    show_default=True,
    help="Manager socket directory.",
)
def drain(sock_dir: str) -> None:
    """Signal a running manager to drain all bots and shut down.

    Sends SIGTERM to the manager PID.  The manager drains bots gracefully
    (waits for final snapshots) then exits.
    """
    sock_path = Path(sock_dir)
    pid_file = sock_path / "manager.pid"

    if not pid_file.exists():
        click.echo("ERROR: Manager PID file not found. Is the manager running?", err=True)
        sys.exit(1)

    pid = int(pid_file.read_text(encoding="utf-8").strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to manager (PID={pid}). Drain in progress.")
    except ProcessLookupError:
        click.echo(f"ERROR: Process {pid} not found. Stale PID file?", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@cli.command()
@click.option(
    "--sock-dir",
    default=str(_DEFAULT_SOCK_DIR),
    show_default=True,
    help="Manager socket directory.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=True,
    hidden=True,
    help="(No-op alias — output is always JSON.)",
)
def status(sock_dir: str, output_json: bool) -> None:
    """Print the current supervisor status as JSON.

    Reads ``<sock-dir>/status.json`` written by the manager every 5 seconds.
    The ``--json`` flag is a no-op alias; output is always JSON.
    """
    status_file = Path(sock_dir) / "status.json"
    if not status_file.exists():
        click.echo("[]")
        return
    click.echo(status_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# inspect-state
# ---------------------------------------------------------------------------


@cli.command(name="inspect-state")
@click.argument("bot_id")
def inspect_state(bot_id: str) -> None:
    """Pretty-print the latest snapshot for BOT_ID.

    If BOT_ID is not found, lists all known bot ids.
    """
    from src.manager.inspect_state import run_inspect

    asyncio.run(run_inspect(bot_id))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_live_allowlist(secrets_dir: Path) -> set[str]:
    """Load the live allow-list from ``live_allowlist.yaml``.

    Args:
        secrets_dir: Directory containing decrypted secret YAML files.

    Returns:
        Set of bot ids permitted to run in live mode.  Returns an empty set
        if the file is absent (missing file = empty list, per spec).
    """
    import yaml

    allowlist_path = secrets_dir / "live_allowlist.yaml"
    if not allowlist_path.exists():
        logger.info("Live allow-list not found at %s — treating as empty.", allowlist_path)
        return set()
    try:
        data = yaml.safe_load(allowlist_path.read_text(encoding="utf-8"))
        ids: list[str] = data.get("bot_ids", []) if isinstance(data, dict) else []
        logger.info("Loaded live allow-list: %d bot(s)", len(ids))
        return set(ids)
    except Exception as exc:
        logger.error("Failed to load live allow-list: %s — treating as empty.", exc)
        return set()


def _build_session_factory() -> object:
    """Build a SQLAlchemy async session factory from environment variables.

    Returns:
        An ``async_sessionmaker`` or ``None`` if Postgres vars are absent.
    """
    host = os.environ.get("POSTGRES_HOST")
    if not host:
        return None

    port = os.environ.get("POSTGRES_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "bidder")
    user = os.environ.get("POSTGRES_USER", "bidder")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    url = f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{db}"

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


if __name__ == "__main__":
    cli()
