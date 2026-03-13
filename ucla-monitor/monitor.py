#!/usr/bin/env python3
"""
UCLA Enrollment Monitor - Refreshes MyUCLA enrollment page and posts course availability to Slack.
"""

import argparse
import logging
import os
import re
import sys
import time
from pathlib import Path

try:
    import winsound
except ImportError:
    winsound = None

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# Add slack-notifier to path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "slack-notifier"))

# Load .env from slack-notifier if python-dotenv available
try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / "slack-notifier" / ".env")
except ImportError:
    pass

from slack_notifier import SlackBotClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

UCLA_ENROLLMENT_URL = "https://be.my.ucla.edu/ClassPlanner/ClassPlan.aspx"
DEFAULT_INTERVAL = 15

# (display_name, class_code) — class code appears on line 2 as "[code] - [description]"
COURSES = [
    ("English 4w", "4w"),
]


def _get_class_blocks(page_text: str) -> list[str]:
    """Split page by 'Class N:' headers. Each block = line 1 + line 2 + content until next Class N:."""
    parts = re.split(r"(Class \d+:\s*)", page_text, flags=re.IGNORECASE)
    blocks = []
    for i in range(1, len(parts), 2):
        header = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        blocks.append((header + content).strip())
    return blocks


def _block_matches_class_code(block: str, class_code: str) -> bool:
    """True if line 2 starts with [class_code] - [description]."""
    lines = block.strip().split("\n")
    if len(lines) < 2:
        return False
    line2 = lines[1].strip()
    # Line 2 format: "[class code] - [brief description]"
    escaped = re.escape(class_code)
    return bool(re.match(rf"^\s*{escaped}\s*-\s*", line2))


def _get_lec_lab_rows(block: str) -> list[str]:
    """From block content (lines 3+), return Lec/Lab rows. Status is on the line after section (Lab 1, Open: 84...)."""
    lines = block.strip().split("\n")
    rows = []
    content_lines = lines[2:]  # Skip line 1 (Class N:) and line 2 (code - description)
    i = 0
    while i < len(content_lines):
        stripped = content_lines[i].strip()
        if not stripped:
            i += 1
            continue
        if stripped.lower().startswith("lec ") or stripped.lower().startswith("lab "):
            # Include this line + next line (status: Open/Closed/Waitlist)
            row_text = stripped
            if i + 1 < len(content_lines):
                next_line = content_lines[i + 1].strip()
                if next_line and not (next_line.lower().startswith("lec ") or next_line.lower().startswith("lab ") or next_line.lower().startswith("dis ")):
                    row_text += " " + next_line
                    i += 1
            rows.append(row_text)
        i += 1
    return rows


def _parse_lec_lab_status(rows: list[str]) -> tuple[str, int | None]:
    """
    Parse Lec/Lab rows. Priority: Closed > Open > Waitlist.
    Returns (status, count) e.g. ("closed", None), ("open", 84), ("waitlist", 5).
    """
    open_seats = []
    waitlist_seats = []
    for row in rows:
        t = row.lower()
        if "closed" in t or "class full" in t:
            return ("closed", None)
        if "open:" in t:
            m = re.search(r"open:\s*(\d+)\s+of\s+\d+\s*left", t)
            if m:
                open_seats.append(int(m.group(1)))
        if "waitlist" in t:
            m = re.search(r"waitlist[:\s]*(\d+)", t)
            if m:
                waitlist_seats.append(int(m.group(1)))
    if open_seats:
        return ("open", max(open_seats))
    if waitlist_seats:
        return ("waitlist", max(waitlist_seats))
    return ("closed", None)


def _is_session_expired(driver) -> bool:
    """Return True if the page looks like a login/session-timeout page."""
    url = driver.current_url.lower()
    if "login" in url or "signin" in url or "sso" in url or "logon" in url:
        return True
    try:
        body = driver.find_element(By.TAG_NAME, "body").text.lower()
        login_signals = ["sign in", "username", "password", "session has expired", "timed out", "log in"]
        if sum(1 for s in login_signals if s in body) >= 2:
            return True
    except Exception:
        pass
    return False


def _wait_for_relogin(driver) -> None:
    """Navigate back to enrollment URL and block until user re-logs in."""
    logger.warning("Session expired — navigating back to enrollment page. Please log back in.")
    if winsound:
        try:
            winsound.MessageBeep(winsound.MB_ICONHAND)
        except Exception:
            pass
    driver.get(UCLA_ENROLLMENT_URL)
    input("Press Enter after you've logged back in and the enrollment page is visible...")
    logger.info("Resuming monitor...")


def get_course_availability(driver, label: str, class_code: str) -> tuple[str, str]:
    """
    Find block(s) where line 2 starts with [class_code] - [description].
    Only look at Lec/Lab rows (ignore Dis). Closed has priority. Then Open (seats), then Waitlist (seats).
    """
    try:
        page_text = driver.find_element(By.TAG_NAME, "body").text
        if class_code not in page_text:
            return f"[{label} - NOT FOUND]", ""

        blocks = _get_class_blocks(page_text)
        matching_blocks = [b for b in blocks if _block_matches_class_code(b, class_code)]

        if not matching_blocks:
            return f"[{label} - NOT FOUND]", ""

        # Collect Lec/Lab rows from all matching blocks
        all_lec_lab_rows = []
        for block in matching_blocks:
            all_lec_lab_rows.extend(_get_lec_lab_rows(block))

        if not all_lec_lab_rows:
            return f"[{label} - NOT FOUND]", matching_blocks[0][:600]

        status, count = _parse_lec_lab_status(all_lec_lab_rows)

        if status == "closed":
            return f"[{label} - CLOSED]", matching_blocks[0][:600]
        if status == "open" and count is not None:
            return f"[{label} - {count} SEATS OPEN]", matching_blocks[0][:600]
        if status == "waitlist" and count is not None:
            return f"[{label} - {count} WAITLIST SEATS]", matching_blocks[0][:600]
        return f"[{label} - CLOSED]", matching_blocks[0][:600]
    except Exception as e:
        logger.warning("Extract availability failed for %s: %s", label, e)
        return f"[{label} - ERROR]", str(e)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UCLA Enrollment Monitor")
    parser.add_argument("--interval", type=int, default=int(os.environ.get("UCLA_MONITOR_INTERVAL", DEFAULT_INTERVAL)),
                        help=f"Refresh interval in seconds (default: {DEFAULT_INTERVAL})")
    parser.add_argument("--headless", action="store_true",
                        default=os.environ.get("UCLA_MONITOR_HEADLESS", "").lower() in ("1", "true", "yes"),
                        help="Run Chrome in headless mode")
    parser.add_argument("--no-sound", action="store_true",
                        default=os.environ.get("UCLA_MONITOR_SOUND", "1").lower() in ("0", "false", "no"),
                        help="Disable sound notifications")
    parser.add_argument("--channel", default=os.environ.get("SLACK_CHANNEL"),
                        help="Slack channel to post to (overrides SLACK_CHANNEL env var)")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.channel:
        logger.error("SLACK_CHANNEL required (set in .env or pass --channel)")
        return

    opts = Options()
    if args.headless:
        opts.add_argument("--headless=new")
    driver = webdriver.Chrome(options=opts)
    slack = SlackBotClient.from_env()

    # Track last known status per course to avoid spamming Slack on every refresh
    last_status: dict[str, str] = {}

    try:
        logger.info("Opening UCLA enrollment page...")
        driver.get(UCLA_ENROLLMENT_URL)

        input("Press Enter after you've logged in and the enrollment page is visible...")

        logger.info("Starting monitor loop (refresh every %ds, courses=%s)", args.interval, [f"{c[0]}({c[1]})" for c in COURSES])

        while True:
            time.sleep(args.interval)
            driver.refresh()

            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except Exception as e:
                logger.warning("Wait for page failed: %s", e)
                continue

            if _is_session_expired(driver):
                _wait_for_relogin(driver)
                continue

            for label, class_code in COURSES:
                status, _ = get_course_availability(driver, label, class_code)
                logger.info(status)

                prev = last_status.get(label)
                if status == prev:
                    continue  # no change — skip Slack and sound

                last_status[label] = status

                # Only notify for available classes (Open or Waitlist)
                if "CLOSED" not in status and "NOT FOUND" not in status and "ERROR" not in status:
                    msg = f"*{status}*"
                    logger.info("Posting %s to %s", status, args.channel)
                    slack.post(msg)
                    if winsound and not args.no_sound:
                        try:
                            for _ in range(3):
                                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
                                time.sleep(0.2)
                        except Exception:
                            pass

    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
