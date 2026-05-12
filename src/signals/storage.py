"""Signal sample storage — append-only writes and replay reads.

The ``signal_samples`` table is append-only (mirrors the ``audit_log``
invariant).  No UPDATE or DELETE is ever issued against it.

``SignalStorage`` uses the same ``async_sessionmaker`` pattern as
``src/state/repository.py``.
"""

from __future__ import annotations

__all__ = ["SignalSample", "SignalStorage"]

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.state.models import SignalSample as _OrmSignalSample


@dataclass(frozen=True)
class SignalSample:
    """A single fetched signal observation, as returned by :meth:`SignalStorage.replay`.

    Attributes:
        signal: Signal name (e.g. ``"btc_15min"``).
        params_hash: blake2s(8) hex of the sorted params dict.
        source: ``SignalSource.name`` that produced this sample.
        observed_at: UTC datetime when the sample was fetched.
        payload: Canonical parsed value (output of ``Signal.parse``).
        latency_ms: Round-trip fetch latency in milliseconds.
    """

    signal: str
    params_hash: str
    source: str
    observed_at: datetime
    payload: dict[str, Any]
    latency_ms: int


class SignalStorage:
    """Append-only storage for signal observations.

    Args:
        session_factory: SQLAlchemy async session factory bound to a running
            engine.  Matches the pattern in ``src/state/repository.py``.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def append(
        self,
        signal: str,
        params_hash: str,
        source: str,
        observed_at: datetime,
        payload: Mapping[str, Any],
        latency_ms: int,
    ) -> None:
        """Insert one sample row.

        INVARIANT: This is the only write operation permitted on ``signal_samples``.
        No UPDATE or DELETE is ever issued by this class.

        Args:
            signal: Signal name (e.g. ``"btc_15min"``).
            params_hash: blake2s(8) hex of sorted params — deduplication key.
            source: ``SignalSource.name`` of the source that produced the data.
            observed_at: UTC datetime when the sample was fetched.
            payload: Canonical parsed value (output of ``Signal.parse``).
            latency_ms: Round-trip fetch latency in milliseconds.
        """
        async with self._sf() as session:
            session.add(
                _OrmSignalSample(
                    signal=signal,
                    params_hash=params_hash,
                    source=source,
                    observed_at=observed_at,
                    payload=dict(payload),
                    latency_ms=latency_ms,
                )
            )
            await session.commit()

    async def replay(
        self,
        signal: str,
        params_hash: str,
        start: datetime,
        end: datetime,
    ) -> AsyncIterator[SignalSample]:
        """Stream historical samples in ascending ``observed_at`` order.

        Args:
            signal: Signal name to query.
            params_hash: Params hash to match (identifies a unique subscription).
            start: Inclusive lower bound on ``observed_at``.
            end: Inclusive upper bound on ``observed_at``.

        Yields:
            :class:`SignalSample` rows in chronological order.
        """
        async with self._sf() as session:
            result = await session.execute(
                select(_OrmSignalSample)
                .where(
                    _OrmSignalSample.signal == signal,
                    _OrmSignalSample.params_hash == params_hash,
                    _OrmSignalSample.observed_at >= start,
                    _OrmSignalSample.observed_at <= end,
                )
                .order_by(_OrmSignalSample.observed_at.asc())
            )
            rows = result.scalars().all()

        for row in rows:
            # SQLAlchemy typed columns are Column[T] at the class level but
            # resolve to T on instances.  Cast here to satisfy mypy.
            observed: datetime = row.observed_at  # type: ignore[assignment]
            yield SignalSample(
                signal=str(row.signal),
                params_hash=str(row.params_hash),
                source=str(row.source),
                observed_at=observed,
                payload=dict(row.payload),
                latency_ms=int(row.latency_ms),
            )
