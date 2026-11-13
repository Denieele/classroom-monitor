"""Unit tests for scripts/classroom_tg_monitor.py."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.classroom_tg_monitor import (
    AssignmentItem,
    find_new_assignments,
    format_alert_message,
    is_urgent_assessment,
    resolve_telegram_config,
    run_monitor,
)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Тест: Модульний контроль 2", True),
        ("МКР 1 / ПЗ 4", True),
        ("КР №1 з дисципліни", True),
        ("Підсумковий залік", True),
        ("Google Forms опитування https://forms.gle/abc", True),
        ("Практична робота №4", False),
        ("Лекція 5", False),
    ],
)
def test_is_urgent_assessment(title: str, expected: bool) -> None:
    assert is_urgent_assessment(title) is expected


def test_find_new_assignments_filters_seen() -> None:
    items = [
        AssignmentItem("c1:a1", "ПЗ 1", "Бізнес", "", "https://classroom.google.com/c/c1/a/a1", False),
        AssignmentItem("c1:a2", "Тест 2", "ЕМПІ", "Завтра", "https://classroom.google.com/c/c1/a/a2", True),
    ]
    new_items = find_new_assignments(items, {"c1:a1": {"title": "ПЗ 1"}})
    assert len(new_items) == 1
    assert new_items[0].task_id == "c1:a2"


def test_format_alert_message_marks_urgent() -> None:
    item = AssignmentItem("c1:a2", "МКР 2", "ЕМПІ", "14.11 08:00", "https://example.com", True)
    msg = format_alert_message(item)
    assert "🚨 НОВИЙ ТЕСТ / КР" in msg
    assert "ЕМПІ" in msg
    assert "МКР 2" in msg


def test_resolve_telegram_config_reads_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("ALLOWED_USER_IDS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123456:ABC\nALLOWED_USER_IDS=987654321,111\n", encoding="utf-8")
    token, chat_id = resolve_telegram_config(env_file)
    assert token == "123456:ABC"
    assert chat_id == "987654321"


def test_run_monitor_sends_only_new_alerts(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_BOT_TOKEN=123456:ABC\nTELEGRAM_CHAT_ID=987654321\n", encoding="utf-8")
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"seen_ids": {"c1:a1": {}}, "auth_alert_sent": False}), encoding="utf-8")

    mocked_items = [
        AssignmentItem("c1:a1", "ПЗ 1", "Бізнес", "", "https://u/1", False),
        AssignmentItem("c1:a2", "Тест 3", "Фін", "18:00", "https://u/2", True),
    ]
    with (
        patch("scripts.classroom_tg_monitor.fetch_classroom_todo", return_value=mocked_items),
        patch("scripts.classroom_tg_monitor.send_telegram_message") as mock_send,
    ):
        sent = run_monitor(
            state_file=state_file,
            env_file=env_file,
            session_dir=None,
            storage_state_path=None,
            init_baseline=False,
            dry_run=False,
            only_urgent=False,
        )

    assert sent == 1
    mock_send.assert_called_once()
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert "c1:a2" in saved["seen_ids"]
