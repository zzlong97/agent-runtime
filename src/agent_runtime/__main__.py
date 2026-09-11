"""Run AgentRuntime with a psycopg-compatible event loop."""

import uvicorn


def main() -> None:
    """Start the ASGI service using the project event loop factory."""

    uvicorn.run(
        "agent_runtime.main:app",
        loop="agent_runtime.core.event_loop:psycopg_compatible_loop_factory",
    )


if __name__ == "__main__":
    main()
