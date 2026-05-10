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

__all__ = ["DEFAULT_SECRETS_DIR", "load_secrets"]

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

DEFAULT_SECRETS_DIR = Path("/run/secrets")


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
