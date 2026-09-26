from uuid import uuid4


def test_request_fingerprint_is_stable_for_equivalent_json_objects() -> None:
    from agent_runtime.runtime.fingerprints import build_request_fingerprint

    session_id = uuid4()
    first = build_request_fingerprint(
        run_type="normal",
        session_id=session_id,
        request_payload={
            "message": {
                "content": "保留原始空格  ",
                "metadata": {"language": "zh", "priority": 1},
            },
            "flags": [True, None],
        },
    )
    second = build_request_fingerprint(
        run_type="normal",
        session_id=session_id,
        request_payload={
            "flags": [True, None],
            "message": {
                "metadata": {"priority": 1, "language": "zh"},
                "content": "保留原始空格  ",
            },
        },
    )

    assert first == second
    assert len(first) == 64
    assert "保留原始空格" not in first
    int(first, 16)


def test_request_fingerprint_distinguishes_semantic_request_changes() -> None:
    from agent_runtime.runtime.fingerprints import build_request_fingerprint

    first_session_id = uuid4()
    payload = {"message": {"content": "同一请求"}}
    baseline = build_request_fingerprint(
        run_type="normal",
        session_id=first_session_id,
        request_payload=payload,
    )
    assert baseline != build_request_fingerprint(
        run_type="normal",
        session_id=first_session_id,
        request_payload={"message": {"content": "不同请求"}},
    )
    assert baseline != build_request_fingerprint(
        run_type="regenerate",
        session_id=first_session_id,
        request_payload=payload,
    )
    assert baseline != build_request_fingerprint(
        run_type="normal",
        session_id=uuid4(),
        request_payload=payload,
    )
