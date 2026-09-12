import asyncio


def test_stage_one_checkpointers_open_three_independent_savers(monkeypatch) -> None:
    from agent_runtime.core.config import Settings
    from agent_runtime.persistence import checkpointers

    class FakeSaver:
        def __init__(self, name: str) -> None:
            self.name = name
            self.setup_calls = 0
            self.is_open = False

        async def __aenter__(self):
            self.is_open = True
            return self

        async def __aexit__(self, exc_type, exc_value, traceback):
            self.is_open = False

        async def setup(self) -> None:
            self.setup_calls += 1

    savers = [FakeSaver("parent"), FakeSaver("general_chat"), FakeSaver("en_to_zh")]
    pending_savers = iter(savers)
    connection_strings: list[str] = []

    class FakeAsyncPostgresSaver:
        @staticmethod
        def from_conn_string(connection_string: str):
            connection_strings.append(connection_string)
            return next(pending_savers)

    monkeypatch.setattr(
        checkpointers,
        "AsyncPostgresSaver",
        FakeAsyncPostgresSaver,
    )
    settings = Settings(
        _env_file=None,
        database_url="postgresql://runtime:runtime@localhost:5432/agent_runtime",
    )

    async def exercise() -> None:
        async with checkpointers.open_stage_one_checkpointers(settings) as opened:
            assert opened.parent is savers[0]
            assert opened.general_chat is savers[1]
            assert opened.en_to_zh is savers[2]
            assert all(saver.is_open for saver in savers)
            assert [saver.setup_calls for saver in savers] == [1, 1, 1]

        assert not any(saver.is_open for saver in savers)

    asyncio.run(exercise())

    assert connection_strings == [settings.database_connection_string] * 3
