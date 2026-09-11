def test_application_error_exposes_stable_error_fields() -> None:
    from agent_runtime.core.errors import ApplicationError

    error = ApplicationError(
        code="SESSION_BUSY",
        message="The session already has an active run.",
        status_code=409,
        retryable=True,
    )

    assert str(error) == "The session already has an active run."
    assert error.code == "SESSION_BUSY"
    assert error.message == "The session already has an active run."
    assert error.status_code == 409
    assert error.retryable is True
