def test_cli_runs_uvicorn_with_psycopg_compatible_loop(monkeypatch) -> None:
    from agent_runtime import __main__

    captured = {}

    def fake_run(app: str, **kwargs) -> None:
        captured["app"] = app
        captured["kwargs"] = kwargs

    monkeypatch.setattr(__main__.uvicorn, "run", fake_run)

    __main__.main()

    assert captured["app"] == "agent_runtime.main:app"
    assert captured["kwargs"]["loop"] == (
        "agent_runtime.core.event_loop:psycopg_compatible_loop_factory"
    )
