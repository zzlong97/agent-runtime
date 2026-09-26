from pathlib import Path


def test_compose_redis_is_local_ephemeral_and_health_checked() -> None:
    compose = Path("compose.yaml").read_text(encoding="utf-8")
    redis_section = compose.split("  redis:\n", maxsplit=1)[1].split(
        "\nvolumes:", maxsplit=1
    )[0]

    assert "redis:8-alpine" in redis_section
    assert '"127.0.0.1:${REDIS_PORT:-6379}:6379"' in redis_section
    assert '"--save", ""' in redis_section
    assert '"--appendonly", "no"' in redis_section
    assert "redis-cli" in redis_section
    assert "volumes:" not in redis_section
