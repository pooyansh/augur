"""Smoke test: verify the src package structure is importable.

Phase 2 deliverable — no business logic tested yet.
"""


def test_src_packages_importable() -> None:
    """All top-level src packages must be importable with zero side effects."""
    import src.alerts
    import src.bots
    import src.exchanges
    import src.manager
    import src.risk
    import src.secrets
    import src.signals
    import src.state

    # Trivially assert we got module objects (not None)
    assert src.alerts is not None
    assert src.bots is not None
    assert src.exchanges is not None
    assert src.manager is not None
    assert src.risk is not None
    assert src.secrets is not None
    assert src.signals is not None
    assert src.state is not None
