"""Winning-rule registry — maps fully-qualified dotted names to
:class:`~src.rules.base.WinningRule` subclasses.

Rules are stored market-scoped, in a nested directory structure::

    src/rules/<venue>/<series_slug>/<rule_module>.py

and register under a fully-qualified dotted name of the same shape::

    "<venue>.<series_slug>.<rule_name>"

e.g. ``"polymarket.btc_up_or_down_5m.price_compare"``.  This makes collisions
across market families structurally impossible — two different series can
each register a rule literally called ``price_compare`` without conflict —
and makes the registry self-documenting about which market a rule targets.

Auto-discovery recurses into the nested package structure (unlike
``src.signals.registry``, which only needs to walk one flat directory level,
``src.rules`` genuinely needs multi-level recursion to reach
``<venue>/<series_slug>/*.py``).

Usage::

    from src.rules.registry import rules

    rules.autodiscover()
    RuleClass = rules.get("polymarket.btc_up_or_down_5m.price_compare")

Pattern mirrors ``src/signals/registry.py``.
"""

from __future__ import annotations

__all__ = ["WinningRuleRegistry", "rules", "winning_rule"]

import contextlib
import importlib
import inspect
import logging
import pkgutil
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.rules.base import WinningRule

logger = logging.getLogger(__name__)


class WinningRuleRegistry:
    """Central registry mapping fully-qualified dotted rule names to classes.

    Each concrete ``WinningRule`` module registers its class at import time
    using the ``@winning_rule`` decorator (or ``rules.register(cls)``
    directly).  The registry fails loudly on duplicate names — silent
    shadowing is impossible.
    """

    def __init__(self) -> None:
        self._classes: dict[str, type[WinningRule]] = {}

    def register(self, cls: type[WinningRule]) -> None:
        """Register a :class:`~src.rules.base.WinningRule` subclass by its ``name``.

        Args:
            cls: The rule class to register.  ``cls.name`` must be the
                fully-qualified dotted name (``"<venue>.<series_slug>.<rule_name>"``).

        Raises:
            ValueError: If a *different* class with the same ``name`` is
                already registered.  Re-registering the exact same class is a
                no-op to tolerate hot-reload patterns.
            AttributeError: If the class does not define a ``name`` class attribute.
        """
        rule_name: str = cls.name
        if rule_name in self._classes:
            existing = self._classes[rule_name]
            if existing is cls:
                return  # idempotent re-register
            raise ValueError(
                f"Duplicate winning rule name '{rule_name}': "
                f"already registered by {existing!r}, cannot register {cls!r}"
            )
        self._classes[rule_name] = cls
        logger.debug("Registered winning rule: %s -> %r", rule_name, cls)

    def get(self, name: str) -> type[WinningRule]:
        """Look up a registered rule by its fully-qualified dotted name.

        Args:
            name: The rule's ``name`` class attribute value.

        Returns:
            The :class:`~src.rules.base.WinningRule` subclass.

        Raises:
            KeyError: If ``name`` has not been registered.
        """
        try:
            return self._classes[name]
        except KeyError:
            available = sorted(self._classes)
            raise KeyError(
                f"Winning rule '{name}' not found. Registered winning rules: {available}"
            ) from None

    def autodiscover(self, package: str = "src.rules") -> None:
        """Recursively walk the package, import every module, and register rules.

        Unlike ``src.signals.registry.SignalRegistry.autodiscover`` (which
        only needs one flat ``pkgutil.iter_modules`` pass), rules genuinely
        nest as ``<venue>/<series_slug>/*.py``, so this uses
        ``pkgutil.walk_packages`` to recurse through every sub-package level.

        Two-pass approach per module:
        1. Import it so ``@winning_rule`` decorators run (registers into the
           global singleton on first import).
        2. Scan the imported module for ``WinningRule`` subclasses and
           register any not already in *this* registry.  This handles the
           common test pattern of creating a fresh ``WinningRuleRegistry()``
           after modules are already imported.

        Modules/packages whose final path segment starts with ``_``
        (including ``__init__``) are skipped for registration purposes, but
        packages are still imported so recursion into their children works.
        Import errors are fatal — loud failure prevents a misconfigured rule
        from silently not registering.

        Args:
            package: Dotted package path to walk.  Defaults to ``"src.rules"``.
        """
        from src.rules.base import WinningRule as _WinningRule

        try:
            pkg: ModuleType = importlib.import_module(package)
        except ImportError as exc:
            raise ImportError(f"Cannot import rules package '{package}': {exc}") from exc

        pkg_path = getattr(pkg, "__path__", [])

        for _finder, module_name, is_pkg in pkgutil.walk_packages(pkg_path, prefix=f"{package}."):
            leaf_name = module_name.rsplit(".", 1)[-1]
            if leaf_name.startswith("_"):
                continue  # skip __init__, __pycache__, private helpers

            try:
                mod = importlib.import_module(module_name)
                logger.debug("Auto-discovered winning rule module: %s", module_name)
            except Exception as exc:
                raise ImportError(
                    f"Failed to import winning rule module '{module_name}': {exc}"
                ) from exc

            if is_pkg:
                continue  # sub-packages contribute no classes themselves

            # Scan the imported module for WinningRule subclasses and register
            # them into *this* registry (handles fresh-registry test pattern).
            for _attr_name, obj in inspect.getmembers(mod, inspect.isclass):
                if (
                    issubclass(obj, _WinningRule)
                    and obj is not _WinningRule
                    and hasattr(obj, "name")
                    and getattr(obj, "name", None) is not None
                ):
                    with contextlib.suppress(ValueError):
                        self.register(obj)

    @property
    def names(self) -> list[str]:
        """Sorted list of all registered winning rule names."""
        return sorted(self._classes)


#: Module-level singleton.  Rule modules import and use ``@winning_rule`` at scope.
rules: WinningRuleRegistry = WinningRuleRegistry()


def winning_rule(cls: type[WinningRule]) -> type[WinningRule]:
    """Class decorator that registers the decorated class in the singleton registry.

    Equivalent to calling ``rules.register(cls)`` at module scope.

    Args:
        cls: The :class:`~src.rules.base.WinningRule` subclass to register.

    Returns:
        The same class, unmodified.

    Example::

        @winning_rule
        class PriceCompare(WinningRule):
            name = "polymarket.btc_up_or_down_5m.price_compare"
            ...
    """
    rules.register(cls)
    return cls
