"""Strategy registry — maps strategy names to :class:`~src.bots.base.BaseBot` subclasses.

Auto-discovery walks ``src/bots/*/strategy.py`` modules, imports each, and
expects them to call :meth:`StrategyRegistry.register` (directly or via the
``@registry.strategy`` decorator) at module load time.

Usage::

    registry = StrategyRegistry()
    registry.autodiscover()
    BotClass = registry.get("momentum_v1")
"""

from __future__ import annotations

__all__ = ["StrategyRegistry", "registry"]

import importlib
import logging
import pkgutil
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.bots.base import BaseBot

logger = logging.getLogger(__name__)


class StrategyRegistry:
    """Central registry mapping strategy name strings to :class:`BaseBot` subclasses.

    Each module in ``src/bots/*/strategy.py`` is expected to register its
    class at import time.  The registry fails loudly on duplicate names so
    silent shadowing is impossible.
    """

    def __init__(self) -> None:
        self._registry: dict[str, type[BaseBot]] = {}

    def register(self, cls: type[BaseBot]) -> None:
        """Register a :class:`BaseBot` subclass by its ``name`` class attribute.

        Args:
            cls: The strategy class to register.

        Raises:
            ValueError: If a class with the same ``name`` is already registered
                (different class object — re-registering the same class is a
                no-op to tolerate hot-reload patterns).
            AttributeError: If the class does not define a ``name`` class attribute.
        """
        strategy_name: str = cls.name
        if strategy_name in self._registry:
            existing = self._registry[strategy_name]
            if existing is cls:
                return  # idempotent re-register
            raise ValueError(
                f"Duplicate strategy name '{strategy_name}': "
                f"already registered by {existing!r}, cannot register {cls!r}"
            )
        self._registry[strategy_name] = cls
        logger.debug("Registered strategy: %s -> %r", strategy_name, cls)

    def get(self, name: str) -> type[BaseBot]:
        """Look up a registered strategy by name.

        Args:
            name: The strategy's ``name`` class attribute value.

        Returns:
            The :class:`BaseBot` subclass.

        Raises:
            KeyError: If ``name`` has not been registered.
        """
        try:
            return self._registry[name]
        except KeyError:
            available = sorted(self._registry)
            raise KeyError(
                f"Strategy '{name}' not found. Registered strategies: {available}"
            ) from None

    def autodiscover(self, package: str = "src.bots") -> None:
        """Walk ``src/bots/*/strategy.py`` and import each module.

        Each module is expected to call :meth:`register` (or use the
        ``@registry.strategy`` decorator) at module scope.  Import errors and
        duplicate-name errors are both fatal — loud failure prevents a
        misconfigured bot from silently not registering.

        Args:
            package: Dotted package path to walk.  Defaults to ``"src.bots"``.
        """
        try:
            pkg: ModuleType = importlib.import_module(package)
        except ImportError as exc:
            raise ImportError(f"Cannot import bots package '{package}': {exc}") from exc

        pkg_path = getattr(pkg, "__path__", [])

        for _finder, subpkg_name, is_pkg in pkgutil.iter_modules(pkg_path):
            if not is_pkg:
                continue  # only walk sub-packages (bot directories)

            strategy_module_name = f"{package}.{subpkg_name}.strategy"
            try:
                importlib.import_module(strategy_module_name)
                logger.debug("Auto-discovered strategy module: %s", strategy_module_name)
            except ModuleNotFoundError:
                # This sub-package has no strategy.py — skip silently.
                logger.debug(
                    "No strategy.py in %s.%s — skipping",
                    package,
                    subpkg_name,
                )
            except Exception as exc:
                raise ImportError(
                    f"Failed to import strategy module '{strategy_module_name}': {exc}"
                ) from exc

    def strategy(self, cls: type[BaseBot]) -> type[BaseBot]:
        """Class decorator that registers the decorated class.

        Equivalent to calling ``registry.register(cls)`` at module scope.

        Args:
            cls: The strategy class to register.

        Returns:
            The same class, unmodified.

        Example::

            @registry.strategy
            class MomentumV1(BaseBot):
                name = "momentum_v1"
                ...
        """
        self.register(cls)
        return cls

    @property
    def names(self) -> list[str]:
        """Sorted list of all registered strategy names."""
        return sorted(self._registry)


#: Module-level singleton registry.  Strategies import and call ``registry.register``
#: or use the ``@registry.strategy`` decorator at module scope.
registry: StrategyRegistry = StrategyRegistry()
