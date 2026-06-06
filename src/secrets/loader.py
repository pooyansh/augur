"""Secrets loader — reads sops-decrypted YAML files from the tmpfs mount.

The entrypoint (``docker/entrypoint.sh``) decrypts ``secrets/*.enc.yaml``
into ``/run/secrets/*.yaml`` on a tmpfs mount before exec'ing the app.
This module reads those plaintext files.  Callers must treat the returned
values as sensitive and pass them to :func:`~src.secrets.install_redaction`
before any logging occurs.

The base directory is configurable so tests can point at a fixture directory
without needing a real mount.
"""

from __future__ import annotations

__all__ = ["DEFAULT_SECRETS_DIR", "Secrets", "load_secrets"]

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SECRETS_DIR = Path(os.environ.get("SECRETS_DIR", "/run/secrets"))


def load_secrets(
    base_dir: Path = DEFAULT_SECRETS_DIR,
) -> dict[str, dict[str, Any]]:
    """Load all YAML files from ``base_dir`` and return a merged dict.

    Each file is keyed by its stem (e.g. ``exchanges.yaml`` → ``"exchanges"``).
    Values within each file can be nested dicts; leaf string values are what
    the redaction filter will mask.

    Args:
        base_dir: Directory containing the decrypted secret YAML files.
            Defaults to :data:`DEFAULT_SECRETS_DIR` (the tmpfs mount).

    Returns:
        Mapping of ``{stem: {key: value, ...}, ...}``.  Returns an empty dict
        if ``base_dir`` does not exist (expected in unit tests that do not
        mount secrets).

    Raises:
        ValueError: If a file cannot be parsed as YAML.
    """
    result: dict[str, dict[str, Any]] = {}

    if not base_dir.exists():
        logger.debug("Secrets directory %s does not exist — returning empty.", base_dir)
        return result

    for yaml_path in sorted(base_dir.glob("*.yaml")):
        stem = yaml_path.stem
        try:
            raw = yaml_path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw)
        except Exception as exc:
            raise ValueError(f"Failed to parse secrets file {yaml_path}: {exc}") from exc

        if not isinstance(data, dict):
            logger.warning("Secrets file %s did not parse to a dict — skipping.", yaml_path)
            continue

        result[stem] = data
        logger.debug("Loaded secrets file: %s (%d top-level keys)", yaml_path.name, len(data))

    return result


class Secrets:
    """Structured wrapper around the loaded secrets mapping.

    Loaded once at startup via :meth:`load`; individual bot subprocesses
    call :meth:`slice_for` to extract only the credentials slice they need.

    Usage::

        secrets = Secrets.load()
        slice_ = secrets.slice_for("exchanges.polymarket.disposable")

    Args:
        data: Mapping returned by :func:`load_secrets`.
            Keyed by filename stem (e.g. ``"exchanges"``, ``"alerts"``).
    """

    def __init__(self, data: dict[str, dict[str, Any]]) -> None:
        self._data = data

    @classmethod
    def load(cls, base_dir: Path = DEFAULT_SECRETS_DIR) -> Secrets:
        """Load all secrets from ``base_dir`` and wrap in a :class:`Secrets` instance.

        Args:
            base_dir: Directory containing the decrypted secret YAML files.

        Returns:
            :class:`Secrets` instance backed by all loaded files.
        """
        return cls(load_secrets(base_dir))

    def slice_for(self, ref: str | Any) -> Any:
        """Resolve a dotted-path reference into the secrets tree.

        ``ref`` is either a plain dotted string (e.g.
        ``"exchanges.polymarket.disposable"``) or a
        :class:`~src.manager.config.SecretRef` model (which exposes
        ``.exchange_credentials``).

        The first segment selects the file stem; subsequent segments traverse
        nested dicts.

        Args:
            ref: Dotted path string or a :class:`~src.manager.config.SecretRef`.

        Returns:
            The resolved leaf value (dict or scalar).

        Raises:
            KeyError: If any segment of the path is missing.
        """
        # Accept SecretRef objects as well as plain strings.
        # Duck-typed: accepts SecretRef (has .exchange_credentials attribute).
        path_str: str = ref if isinstance(ref, str) else str(ref.exchange_credentials)

        parts = path_str.split(".")
        node: Any = self._data
        for part in parts:
            if not isinstance(node, dict):
                raise KeyError(
                    f"Cannot traverse into non-dict at segment {part!r} in path {path_str!r}"
                )
            if part not in node:
                raise KeyError(f"Segment {part!r} not found in secrets (path={path_str!r})")
            node = node[part]
        return node

    def raw(self) -> dict[str, dict[str, Any]]:
        """Return the full underlying secrets mapping (all stems).

        Returns:
            Mapping of ``{stem: {key: value, ...}, ...}``.
        """
        return self._data
