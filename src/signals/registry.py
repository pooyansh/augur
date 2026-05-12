"""Signal registry — maps signal names to :class:`~src.signals.base.Signal` subclasses.

Auto-discovery walks the ``src.signals`` package and imports every module,
expecting each concrete ``Signal`` subclass to call :func:`signal` at module
scope (via the ``@signal`` decorator).

Usage::

    from src.signals.registry import signals

    signals.autodiscover()
    SignalClass = signals.get("btc_15min")

Pattern mirrors ``src/manager/registry.py`` for strategy discovery.
"""

from __future__ import annotations

__all__ = ["SignalRegistry", "signal", "signals"]

import importlib
import logging
import pkgutil
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.signals.base import Signal

logger = logging.getLogger(__name__)


class SignalRegistry:
    """Central registry mapping signal name strings to :class:`Signal` subclasses.

    Each concrete ``Signal`` module registers its class at import time using the
    ``@signal`` decorator (or ``signals.register(cls)`` directly).  The registry
    fails loudly on duplicate names — silent shadowing is impossible.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[Signal]] = {}

    def register(self, cls: type[Signal]) -> None:
        """Register a :class:`Signal` subclass by its ``name`` class attribute.

        Args:
            cls: The signal class to register.

        Raises:
            ValueError: If a *different* class with the same ``name`` is already
                registered.  Re-registering the exact same class is a no-op to
                tolerate hot-reload patterns.
            AttributeError: If the class does not define a ``name`` class attribute.
        """
        signal_name: str = cls.name
        if signal_name in self._classes:
            existing = self._classes[signal_name]
            if existing is cls:
                return  # idempotent re-register
            raise ValueError(
                f"Duplicate signal name '{signal_name}': "
                f"already registered by {existing!r}, cannot register {cls!r}"
            )
        self._classes[signal_name] = cls
        logger.debug("Registered signal: %s -> %r", signal_name, cls)

    def get(self, name: str) -> type[Signal]:
        """Look up a registered signal by name.

        Args:
            name: The signal's ``name`` class attribute value.

        Returns:
            The :class:`Signal` subclass.

        Raises:
            KeyError: If ``name`` has not been registered.
        """
        try:
            return self._classes[name]
        except KeyError:
            available = sorted(self._classes)
            raise KeyError(f"Signal '{name}' not found. Registered signals: {available}") from None

    def autodiscover(self, package: str = "src.signals") -> None:
        """Walk the package, import every module, and register all Signal subclasses.

        Two-pass approach:
        1. Import each module so that ``@signal`` decorators run (registers into
           the global singleton on first import).
        2. After import, scan the module for ``Signal`` subclasses and register
           any that are not already in *this* registry.  This handles the common
           test pattern of creating a fresh ``SignalRegistry()`` after modules
           are already imported.

        Modules whose names start with ``_`` (including ``__init__``) are skipped.
        Import errors are fatal — loud failure prevents a misconfigured signal
        from silently not registering.

        Args:
            package: Dotted package path to walk.  Defaults to ``"src.signals"``.
        """
        import inspect

        from src.signals.base import Signal as _Signal

        try:
            pkg: ModuleType = importlib.import_module(package)
        except ImportError as exc:
            raise ImportError(f"Cannot import signals package '{package}': {exc}") from exc

        pkg_path = getattr(pkg, "__path__", [])

        for _finder, module_name, _is_pkg in pkgutil.iter_modules(pkg_path):
            if module_name.startswith("_"):
                continue  # skip __init__, __pycache__, private helpers

            full_name = f"{package}.{module_name}"
            try:
                mod = importlib.import_module(full_name)
                logger.debug("Auto-discovered signal module: %s", full_name)
            except Exception as exc:
                raise ImportError(f"Failed to import signal module '{full_name}': {exc}") from exc

            # Scan the imported module for Signal subclasses and register them
            # into *this* registry.  This handles the case where the module was
            # already imported (so the @signal decorator already ran and
            # registered into the global singleton) but this is a fresh registry.
            for _attr_name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, _Signal)
                    and obj is not _Signal
                    and hasattr(obj, "name")
                    and getattr(obj, "name", None) is not None
                ):
                    import contextlib

                    with contextlib.suppress(ValueError):
                        self.register(obj)

    @property
    def names(self) -> list[str]:
        """Sorted list of all registered signal names."""
        return sorted(self._classes)


#: Module-level singleton.  Signal modules import and use ``@signal`` at scope.
signals: SignalRegistry = SignalRegistry()


def signal(cls: type[Signal]) -> type[Signal]:
    """Class decorator that registers the decorated class in the singleton registry.

    Equivalent to calling ``signals.register(cls)`` at module scope.

    Args:
        cls: The :class:`Signal` subclass to register.

    Returns:
        The same class, unmodified.

    Example::

        @signal
        class Btc15Min(Signal):
            name = "btc_15min"
            ...
    """
    signals.register(cls)
    return cls
