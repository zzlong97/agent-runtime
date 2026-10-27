"""发现过程的来源、认证及静态身份边界。"""

import asyncio

import pytest

from tests.fake_a2a import FakeA2A, remote_transport
from tests.capabilities.test_remote_a2a_client import make_client


@pytest.mark.parametrize(
    "mutation",
    [
        lambda c: c["supportedInterfaces"][0].update(protocolVersion="0.3"),
        lambda c: c["supportedInterfaces"][0].update(protocolBinding="JSONRPC"),
        lambda c: c["supportedInterfaces"][0].update(url="https://evil.example/api"),
        lambda c: c["supportedInterfaces"][0].update(
            url="https://agent.example:444/api"
        ),
        lambda c: c["supportedInterfaces"][0].update(
            url="https://user:pass@agent.example/api"
        ),
        lambda c: c["supportedInterfaces"][0].update(
            url="https://agent.example/api#fragment"
        ),
        lambda c: c["supportedInterfaces"][0].update(url="file:///api"),
        lambda c: c["supportedInterfaces"][0].update(tenant="../outside"),
        lambda c: c.update(defaultInputModes=["application/json"]),
        lambda c: c.update(defaultOutputModes=["image/png"]),
        lambda c: c["capabilities"].update(
            extensions=[{"uri": "https://ext.example/required", "required": True}]
        ),
        lambda c: c.update(securityRequirements=[{"schemes": {"missing": {}}}]),
        lambda c: c.update(name=""),
    ],
)
def test_incompatible_card_is_rejected_before_any_invocation(mutation):
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A()
    mutation(fake.card)

    async def run():
        client = make_client(fake)
        try:
            with pytest.raises(RemoteA2AError):
                await client.discover()
            assert len(fake.requests) == 1
        finally:
            await client.close()

    asyncio.run(run())


def test_effective_origin_normalization_and_refresh_do_not_replace_snapshot():
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A()
    fake.card["supportedInterfaces"][0]["url"] = "https://AGENT.example:443/api"

    async def run():
        client = make_client(fake)
        try:
            initial = await client.discover()
            fake.card["description"] = "更新说明不替换 Manifest 投影"
            assert await client.discover() == initial
            fake.card["supportedInterfaces"][0]["url"] = "https://agent.example/other"
            with pytest.raises(RemoteA2AError):
                await client.discover()
            assert client.snapshot == initial
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize("changed", ["name", "version"])
def test_identity_change_is_unavailable_until_restart(changed):
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A()

    async def run():
        client = make_client(fake)
        try:
            await client.discover()
            fake.card[changed] = "changed"
            with pytest.raises(RemoteA2AError):
                await client.discover()
        finally:
            await client.close()

    asyncio.run(run())


def test_credentials_only_reach_validated_origin_and_never_follow_redirect(caplog):
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A(bearer=True)
    config = remote_transport(auth={"type": "bearer", "token_env": "REMOTE_TOKEN"})

    async def run():
        client = make_client(
            fake, transport=config, environ={"REMOTE_TOKEN": "private-token"}
        )
        try:
            await client.discover()
            await client.send_message(message_id="id", text="敏感正文")
            assert all(
                r.headers["authorization"] == "Bearer private-token"
                for r in fake.requests
            )
            fake.redirect = "https://evil.example/steal"
            with pytest.raises(RemoteA2AError):
                await client.get_task("test-id")
            assert len(fake.requests) == 3
            assert "private-token" not in caplog.text
            assert "敏感正文" not in caplog.text
            assert "private-token" not in repr(client)
        finally:
            await client.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "config",
    [
        remote_transport(auth={"type": "bearer", "token_env": "MISSING"}),
        remote_transport(url="http://agent.example/.well-known/agent-card.json"),
        remote_transport(),
    ],
)
def test_production_or_missing_secret_refusal_happens_without_network(config):
    from agent_runtime.capabilities.remote_a2a.client import (
        RemoteA2AClient,
        RemoteA2AError,
    )
    from agent_runtime.capabilities.manifest import RemoteA2ATransport

    fake = FakeA2A()

    async def run():
        with pytest.raises(RemoteA2AError):
            client = RemoteA2AClient(
                capability_id="remote_test",
                transport=RemoteA2ATransport.model_validate(config),
                http_transport=fake.transport,
                environ={},
            )
            try:
                await client.discover()
            finally:
                await client.close()
        assert fake.requests == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "requirements",
    [
        [{"schemes": {"token": {}, "unsupported": {}}}],
        [{"schemes": {"unsupported": {}}}],
    ],
)
def test_bearer_must_satisfy_one_complete_security_requirement(requirements):
    from agent_runtime.capabilities.remote_a2a.client import RemoteA2AError

    fake = FakeA2A(bearer=True)
    fake.card["securityRequirements"] = requirements

    async def run():
        client = make_client(
            fake,
            transport=remote_transport(auth={"type": "bearer", "token_env": "TOKEN"}),
            environ={"TOKEN": "secret"},
        )
        try:
            with pytest.raises(RemoteA2AError):
                await client.discover()
        finally:
            await client.close()

    asyncio.run(run())


def test_security_requirement_alternatives_and_optional_extension_are_supported():
    fake = FakeA2A(bearer=True)
    fake.card["securityRequirements"].insert(0, {"schemes": {"unsupported": {}}})
    fake.card["capabilities"]["extensions"] = [
        {"uri": "https://extension.example/optional"}
    ]

    async def run():
        client = make_client(
            fake,
            transport=remote_transport(auth={"type": "bearer", "token_env": "TOKEN"}),
            environ={"TOKEN": "secret"},
        )
        try:
            assert (
                await client.discover()
            ).interface_url == "https://agent.example/api"
        finally:
            await client.close()

    asyncio.run(run())
