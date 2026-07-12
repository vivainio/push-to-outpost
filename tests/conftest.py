import pytest

from outpost import agent, sessions


@pytest.fixture(autouse=True)
def _reset_module_caches():
    """`agent._last_hashes` and `sessions._last_hashes` are bare module-level
    dicts used for in-process change detection — reset them so state from one
    test can't leak into the next."""
    agent._last_hashes.clear()
    sessions._last_hashes.clear()
    yield
    agent._last_hashes.clear()
    sessions._last_hashes.clear()
