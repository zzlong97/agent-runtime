"""Session 产品接口 Schema 契约测试。"""

import pytest
from pydantic import ValidationError


def test_session_list_query_uses_stage_two_defaults_and_limits() -> None:
    from agent_runtime.api.schemas.sessions import SessionListQuery

    query = SessionListQuery()

    assert query.cursor is None
    assert query.limit == 20

    for invalid_limit in (0, 101):
        with pytest.raises(ValidationError):
            SessionListQuery(limit=invalid_limit)


def test_session_list_query_rejects_client_supplied_user_id() -> None:
    from agent_runtime.api.schemas.sessions import SessionListQuery

    with pytest.raises(ValidationError):
        SessionListQuery.model_validate({"user_id": "client-user"})


def test_session_list_schemas_describe_every_field_in_chinese() -> None:
    from agent_runtime.api.schemas.sessions import (
        SessionListItem,
        SessionListQuery,
        SessionListResponse,
    )

    expected_descriptions = {
        SessionListQuery: {
            "cursor": (
                "定位下一页的后端不透明游标；省略或传 null 时读取最新一页，"
                "Stage 2 客户端不得解析、修改或自行拼装。"
            ),
            "limit": (
                "本页最多返回的 Session 数量；允许 1～100，默认 20，"
                "Stage 2 使用游标分页。"
            ),
        },
        SessionListItem: {
            "session_id": (
                "Session 的服务端稳定 UUID，用于后续聊天和 Session 产品操作。"
            ),
            "title": (
                "Session 当前标题；Stage 2 S2-01 仅用于列表展示，"
                "本接口不修改标题。"
            ),
            "created_at": "Session 创建时间，返回带时区的 ISO 8601 时间。",
            "updated_at": (
                "Session 最近更新时间，作为列表首要降序排序键，"
                "并返回带时区的 ISO 8601 时间。"
            ),
        },
        SessionListResponse: {
            "items": (
                "固定本地用户当前页的 Session 项，按 updated_at DESC、"
                "session_id DESC 排列。"
            ),
            "next_cursor": (
                "下一页的后端不透明游标；没有更多 Session 时为 null，"
                "客户端不得解析或自行拼装。"
            ),
        },
    }

    for schema_type, descriptions in expected_descriptions.items():
        schema = schema_type.model_json_schema()
        assert schema["additionalProperties"] is False
        assert {
            name: definition["description"]
            for name, definition in schema["properties"].items()
        } == descriptions
