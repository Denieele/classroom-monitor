"""24/7 Google Classroom monitor with Telegram alerts for new assignments and tests/KRs."""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

LOGGER = logging.getLogger("classroom_tg_monitor")

CLASSROOM_TODO_URL = "https://classroom.google.com/u/0/a/not-turned-in/all"
CLASSROOM_TURNED_IN_URL = "https://classroom.google.com/u/0/a/turned-in/all"
DEFAULT_SESSION_DIR = Path(r"C:\Users\Public\classroom_session")
DEFAULT_STATE_FILE = Path("classroom_monitor_state.json")

URGENT_PATTERN = re.compile(
    r"(тест|к/?р\b|мкр\b|мк\s*\d|контрольн|залік|екзамен|іспит|quiz|exam|forms\.gle|docs\.google\.com/forms)",
    re.IGNORECASE,
)
GRADE_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?\s*/\s*\d+|оцінено|повернуто|graded|returned)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AssignmentItem:
    """Normalized representation of a Classroom assignment."""

    task_id: str
    title: str
    course: str
    due: str
    url: str
    is_urgent: bool


@dataclass(frozen=True)
class GradedItem:
    """Normalized representation of a graded/returned Classroom assignment."""

    task_id: str
    title: str
    course: str
    grade: str
    url: str


def is_urgent_assessment(text: str) -> bool:
    """Return True if title/description looks like a test, KR, MKR, or exam."""
    return bool(URGENT_PATTERN.search(text))


def load_env_file(env_path: Path) -> dict[str, str]:
    """Parse KEY=VALUE pairs from a .env file without overwriting existing os.environ."""
    parsed: dict[str, str] = {}
    if not env_path.exists():
        return parsed
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in parsed:
            parsed[key] = val
    return parsed


def resolve_telegram_config(env_path: Path | None = None) -> tuple[str, str]:
    """Load and validate TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (or ALLOWED_USER_IDS)."""
    file_env = load_env_file(env_path) if env_path is not None else {}
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or file_env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = (
        os.environ.get("TELEGRAM_CHAT_ID")
        or file_env.get("TELEGRAM_CHAT_ID")
        or os.environ.get("ALLOWED_USER_IDS", "")
        or file_env.get("ALLOWED_USER_IDS", "")
    )
    if "," in chat_id:
        chat_id = chat_id.split(",", 1)[0].strip()
    if not token or ":" not in token:
        raise ValueError("Missing or invalid TELEGRAM_BOT_TOKEN")
    if not chat_id:
        raise ValueError("Missing TELEGRAM_CHAT_ID (or ALLOWED_USER_IDS)")
    return token, chat_id


def send_telegram_message(
    token: str,
    chat_id: str,
    text: str,
    timeout_s: float = 15.0,
    max_attempts: int = 3,
) -> None:
    """Send a Telegram message via Bot API with explicit timeout and 429 retry."""
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")

    for attempt in range(1, max_attempts + 1):
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                if resp.status == 200:
                    return
                raise RuntimeError(f"Telegram API unexpected status: {resp.status}")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < max_attempts:
                retry_after = float(exc.headers.get("Retry-After", "2") or "2")
                LOGGER.warning("Telegram rate limited (429), retrying in %.1fs", retry_after)
                time.sleep(min(retry_after, 10.0))
                continue
            if 500 <= exc.code < 600 and attempt < max_attempts:
                time.sleep(1.5 * attempt)
                continue
            raise RuntimeError(f"Telegram API HTTP error {exc.code}") from exc
        except urllib.error.URLError as exc:
            if attempt < max_attempts:
                time.sleep(1.5 * attempt)
                continue
            raise RuntimeError(f"Telegram network error: {exc.reason}") from exc


def load_state(state_file: Path) -> dict[str, Any]:
    """Load persisted monitor state from disk."""
    if not state_file.exists():
        return {"seen_ids": {}, "seen_grades": {}, "auth_alert_sent": False, "last_check": None}
    data = json.loads(state_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {"seen_ids": {}, "seen_grades": {}, "auth_alert_sent": False, "last_check": None}
    data.setdefault("seen_ids", {})
    data.setdefault("seen_grades", {})
    data.setdefault("auth_alert_sent", False)
    return data


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    """Persist monitor state atomically."""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(state_file)


def find_new_assignments(
    current: list[AssignmentItem],
    seen_ids: dict[str, Any],
) -> list[AssignmentItem]:
    """Return assignments not yet recorded in seen_ids."""
    return [item for item in current if item.task_id not in seen_ids]


def find_new_grades(
    current: list[GradedItem],
    seen_grades: dict[str, str],
) -> list[GradedItem]:
    """Return graded items whose grade/status is new or changed."""
    return [item for item in current if seen_grades.get(item.task_id) != item.grade]


def format_alert_message(item: AssignmentItem) -> str:
    """Format a concise Ukrainian Telegram alert for a new assignment."""
    header = "🚨 НОВИЙ ТЕСТ / КР у Classroom!" if item.is_urgent else "📌 Нове завдання у Classroom"
    due_str = item.due or "Без терміну"
    course_str = item.course or "Курс 4-го курсу"
    return (
        f"{header}\n"
        f"Предмет: {course_str}\n"
        f"Завдання: {item.title}\n"
        f"Термін: {due_str}\n"
        f"Посилання: {item.url}"
    )


def format_grade_message(item: GradedItem) -> str:
    """Format a concise Ukrainian Telegram alert for a newly graded assignment."""
    course_str = item.course or "Курс 4-го курсу"
    return (
        "🎓 Оцінено роботу у Classroom!\n"
        f"Предмет: {course_str}\n"
        f"Завдання: {item.title}\n"
        f"Оцінка / Статус: {item.grade}\n"
        f"Посилання: {item.url}"
    )


def _extract_raw_cards(page: Page) -> list[dict[str, Any]]:
    """Extract raw assignment card links and text lines from a Classroom To-Do/Turned-in page."""
    return page.evaluate(
        """() => {
            const results = [];
            const seen = new Set();
            const links = Array.from(document.querySelectorAll('a[href*="/c/"][href*="/a/"]'));
            for (const a of links) {
                const href = a.href.split('?')[0];
                if (!href || seen.has(href)) continue;
                seen.add(href);
                const container = a.closest('li, [role="listitem"], div[data-item-id]') || a.parentElement;
                const lines = (container ? container.innerText : a.innerText || '')
                    .split('\\n')
                    .map(s => s.trim())
                    .filter(Boolean);
                const title = (a.innerText || '').trim().split('\\n')[0] || lines[0] || 'Без назви';
                const course = lines.length > 1 ? lines[1] : '';
                results.push({ href, title, course, lines });
            }
            return results;
        }"""
    )


def _extract_assignments_from_page(page: Page) -> list[AssignmentItem]:
    """Extract assignment cards from Google Classroom To-Do page."""
    raw_items = _extract_raw_cards(page)
    items: list[AssignmentItem] = []
    for row in raw_items:
        url = str(row["href"])
        match = re.search(r"/c/([^/]+)/a/([^/]+)", url)
        task_id = f"{match.group(1)}:{match.group(2)}" if match else url
        title = str(row["title"])
        course = str(row["course"])
        lines: list[str] = [str(x) for x in row.get("lines", [])]
        due = next(
            (ln for ln in lines if re.search(r"термін|здати|сьогодні|завтра|nov|dec|січ|лис|гру|\d{1,2}:\d{2}", ln, re.IGNORECASE)),
            "",
        )
        items.append(
            AssignmentItem(
                task_id=task_id,
                title=title,
                course=course,
                due=due,
                url=url,
                is_urgent=is_urgent_assessment(f"{title} {course}"),
            )
        )
    return items


def _extract_graded_from_page(page: Page) -> list[GradedItem]:
    """Extract graded/returned items from Google Classroom Turned-in page."""
    raw_items = _extract_raw_cards(page)
    graded: list[GradedItem] = []
    for row in raw_items:
        url = str(row["href"])
        match = re.search(r"/c/([^/]+)/a/([^/]+)", url)
        task_id = f"{match.group(1)}:{match.group(2)}" if match else url
        title = str(row["title"])
        course = str(row["course"])
        lines: list[str] = [str(x) for x in row.get("lines", [])]
        grade_lines = [ln for ln in lines if GRADE_PATTERN.search(ln)]
        if not grade_lines:
            continue
        graded.append(
            GradedItem(
                task_id=task_id,
                title=title,
                course=course,
                grade=" | ".join(dict.fromkeys(grade_lines)),
                url=url,
            )
        )
    return graded


def _expand_sections(page: Page, limit: int = 8) -> None:
    """Expand collapsed Classroom sections."""
    for toggle in page.locator('[aria-expanded="false"]').all()[:limit]:
        try:
            toggle.click(timeout=1500)
            page.wait_for_timeout(300)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("Skipping non-clickable toggle: %s", exc)


def fetch_classroom_snapshot(
    session_dir: Path | None,
    storage_state_path: Path | None,
    timeout_ms: int = 30000,
) -> tuple[list[AssignmentItem], list[GradedItem]]:
    """Fetch both To-Do assignments and Turned-in graded items in one browser session."""
    with sync_playwright() as p:
        if storage_state_path is not None and storage_state_path.exists():
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(storage_state=str(storage_state_path))
        elif session_dir is not None and session_dir.exists():
            browser = None
            ctx = p.chromium.launch_persistent_context(
                str(session_dir),
                headless=True,
                channel="chrome",
            )
        else:
            raise FileNotFoundError("Neither storage_state.json nor persistent classroom_session exists")

        try:
            page = ctx.new_page()
            page.goto(CLASSROOM_TODO_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(4000)
            if "accounts.google.com" in page.url.lower():
                raise PermissionError("Google Classroom session expired (redirected to login)")
            _expand_sections(page)
            todo_items = _extract_assignments_from_page(page)

            page.goto(CLASSROOM_TURNED_IN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(4000)
            if "accounts.google.com" in page.url.lower():
                raise PermissionError("Google Classroom session expired (redirected to login)")
            _expand_sections(page)
            graded_items = _extract_graded_from_page(page)

            return todo_items, graded_items
        finally:
            ctx.close()
            if browser is not None:
                browser.close()


def export_storage_state(session_dir: Path, out_b64_path: Path) -> None:
    """Export Playwright storage_state from persistent Chrome profile to base64 file."""
    if not session_dir.exists():
        raise FileNotFoundError(f"Session directory not found: {session_dir}")
    out_b64_path.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(session_dir),
            headless=True,
            channel="chrome",
        )
        try:
            state = ctx.storage_state()
        finally:
            ctx.close()
    raw_bytes = json.dumps(state, ensure_ascii=False).encode("utf-8")
    out_b64_path.write_text(base64.b64encode(raw_bytes).decode("ascii"), encoding="utf-8")
    LOGGER.info("Exported storage_state base64 (%d bytes) to %s", len(raw_bytes), out_b64_path)


def run_monitor(
    state_file: Path,
    env_file: Path | None,
    session_dir: Path | None,
    storage_state_path: Path | None,
    init_baseline: bool,
    dry_run: bool,
    only_urgent: bool,
) -> int:
    """Execute one monitor cycle. Returns number of alerts sent."""
    token, chat_id = resolve_telegram_config(env_file)
    state = load_state(state_file)
    seen_ids: dict[str, Any] = state["seen_ids"]
    seen_grades: dict[str, str] = state["seen_grades"]

    try:
        current, current_graded = fetch_classroom_snapshot(
            session_dir=session_dir,
            storage_state_path=storage_state_path,
        )
    except PermissionError as exc:
        LOGGER.error("Auth error: %s", exc)
        if not state.get("auth_alert_sent") and not dry_run:
            send_telegram_message(
                token,
                chat_id,
                "⚠️ Сесія Google Classroom протухла! Онови CLASSROOM_STORAGE_STATE_B64 у GitHub Secrets.",
            )
            state["auth_alert_sent"] = True
            save_state(state_file, state)
        raise

    state["auth_alert_sent"] = False
    state["last_check"] = datetime.now(UTC).isoformat()

    new_items = find_new_assignments(current, seen_ids)
    new_grades = find_new_grades(current_graded, seen_grades)
    LOGGER.info(
        "Scanned %d active (%d new), %d graded (%d new/updated)",
        len(current),
        len(new_items),
        len(current_graded),
        len(new_grades),
    )

    alerts_sent = 0
    for item in new_items:
        seen_ids[item.task_id] = asdict(item)
        if init_baseline:
            continue
        if only_urgent and not item.is_urgent:
            LOGGER.info("Skipping non-urgent item: %s", item.title)
            continue
        msg = format_alert_message(item)
        if dry_run:
            LOGGER.info("[DRY-RUN] Would send alert for %s (%s)", item.title, item.task_id)
        else:
            send_telegram_message(token, chat_id, msg)
            alerts_sent += 1

    for graded in new_grades:
        seen_grades[graded.task_id] = graded.grade
        if init_baseline or only_urgent:
            continue
        msg = format_grade_message(graded)
        if dry_run:
            LOGGER.info("[DRY-RUN] Would send grade alert for %s (%s)", graded.title, graded.grade)
        else:
            send_telegram_message(token, chat_id, msg)
            alerts_sent += 1

    save_state(state_file, state)
    return alerts_sent


def main() -> None:
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="24/7 Google Classroom Telegram monitor")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--session-dir", type=Path, default=DEFAULT_SESSION_DIR)
    parser.add_argument("--storage-state", type=Path, default=Path("storage_state.json"))
    parser.add_argument("--init-baseline", action="store_true", help="Record current tasks without alerting")
    parser.add_argument("--dry-run", action="store_true", help="Scan without sending Telegram messages")
    parser.add_argument("--only-urgent", action="store_true", help="Alert only on tests/KR/MKR/exams")
    parser.add_argument("--export-state", type=Path, default=None, help="Export base64 storage_state to path")
    args = parser.parse_args()

    if args.export_state is not None:
        export_storage_state(args.session_dir, args.export_state)
        return

    sent = run_monitor(
        state_file=args.state_file,
        env_file=args.env_file,
        session_dir=args.session_dir,
        storage_state_path=args.storage_state,
        init_baseline=args.init_baseline,
        dry_run=args.dry_run,
        only_urgent=args.only_urgent,
    )
    LOGGER.info("Monitor cycle finished, alerts_sent=%d", sent)


if __name__ == "__main__":
    main()
