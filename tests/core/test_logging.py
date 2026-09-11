import logging


def test_configure_logging_sets_agent_runtime_level() -> None:
    from agent_runtime.core.logging import configure_logging

    configure_logging("DEBUG")

    assert logging.getLogger("agent_runtime").level == logging.DEBUG
