import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta


def test_configure_logging_sets_agent_runtime_level() -> None:
    from agent_runtime.core.logging import configure_logging

    configure_logging("DEBUG")

    assert logging.getLogger("agent_runtime").level == logging.DEBUG


def test_configure_logging_writes_chinese_business_events_to_daily_file(
    tmp_path,
) -> None:
    from agent_runtime.core.logging import configure_logging, log_business_event

    configure_logging("INFO", log_dir=tmp_path)
    logger = logging.getLogger("agent_runtime.tests.logging")

    log_business_event(
        logger,
        "能力调用完成",
        session_id="session-001",
        capability_id="general_chat",
        api_key="不能写入日志的密钥",
        content="不能写入日志的用户正文",
        access_token="不能写入日志的访问令牌",
        headers={
            "Authorization": "不能写入日志的认证头",
            "X-API-Key": "不能写入日志的请求密钥",
        },
        dsn="postgresql://runtime:不能写入日志的密码@localhost/db",
        connection_string="postgresql://runtime:同样不能写入日志@localhost/db",
        ssh_private_key="不能写入日志的私钥",
        client_credentials="不能写入日志的客户端凭据",
        database_connection_uri="postgresql://runtime:不能写入日志的URI@localhost/db",
    )
    for handler in logging.getLogger("agent_runtime").handlers:
        handler.flush()

    log_file = tmp_path / f"agent-runtime-{datetime.now():%Y-%m-%d}.log"
    text = log_file.read_text(encoding="utf-8")

    assert "能力调用完成" in text
    assert '"session_id": "session-001"' in text
    assert '"capability_id": "general_chat"' in text
    assert text.count("能力调用完成") == 1
    assert "不能写入日志的密钥" not in text
    assert "不能写入日志的用户正文" not in text
    assert "不能写入日志的访问令牌" not in text
    assert "不能写入日志的认证头" not in text
    assert "不能写入日志的请求密钥" not in text
    assert "不能写入日志的密码" not in text
    assert "同样不能写入日志" not in text
    assert "不能写入日志的私钥" not in text
    assert "不能写入日志的客户端凭据" not in text
    assert "不能写入日志的URI" not in text
    assert text.count("<已脱敏>") == 10


def test_reconfiguring_logging_does_not_duplicate_business_events(tmp_path) -> None:
    from agent_runtime.core.logging import configure_logging, log_business_event

    configure_logging("INFO", log_dir=tmp_path)
    configure_logging("INFO", log_dir=tmp_path)

    logger = logging.getLogger("agent_runtime.tests.logging")
    log_business_event(logger, "日志去重验证", session_id="session-002")
    for handler in logging.getLogger("agent_runtime").handlers:
        handler.flush()

    log_file = tmp_path / f"agent-runtime-{datetime.now():%Y-%m-%d}.log"
    assert log_file.read_text(encoding="utf-8").count("日志去重验证") == 1


def test_daily_logging_switches_file_when_record_date_changes(tmp_path) -> None:
    from agent_runtime.core.logging import configure_logging

    configure_logging("INFO", log_dir=tmp_path)
    logger = logging.getLogger("agent_runtime.tests.rollover")
    next_day = datetime.now() + timedelta(days=1)
    record = logger.makeRecord(
        logger.name,
        logging.INFO,
        __file__,
        1,
        "跨日日志验证",
        (),
        None,
    )
    record.created = next_day.timestamp()
    logger.handle(record)
    for handler in logging.getLogger("agent_runtime").handlers:
        handler.flush()

    next_file = tmp_path / f"agent-runtime-{next_day:%Y-%m-%d}.log"
    assert next_file.exists()
    assert "跨日日志验证" in next_file.read_text(encoding="utf-8")


def test_daily_logging_serializes_concurrent_business_events(tmp_path) -> None:
    from agent_runtime.core.logging import configure_logging, log_business_event

    configure_logging("INFO", log_dir=tmp_path)
    logger = logging.getLogger("agent_runtime.tests.concurrent")

    def write_event(index: int) -> None:
        log_business_event(logger, "并发日志验证", event_index=index)

    with ThreadPoolExecutor(max_workers=8) as executor:
        tuple(executor.map(write_event, range(50)))
    for handler in logging.getLogger("agent_runtime").handlers:
        handler.flush()

    log_file = tmp_path / f"agent-runtime-{datetime.now():%Y-%m-%d}.log"
    text = log_file.read_text(encoding="utf-8")
    assert text.count("并发日志验证") == 50
    assert all(f'"event_index": {index}' in text for index in range(50))
