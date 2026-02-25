#!/usr/bin/env python3
"""
UCLA Enrollment Monitor - Refreshes MyUCLA enrollment page and posts course availability to Slack.
"""

import logging
import os
import re
import sys
import time
from pathlib import Path

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
    ("AERO", "A"),
    ("SCAND60", "60"),
    ("M61", "61"),
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


def main() -> None:
    interval = int(os.environ.get("UCLA_MONITOR_INTERVAL", DEFAULT_INTERVAL))
    channel = os.environ.get("SLACK_CHANNEL")
    if not channel:
        logger.error("SLACK_CHANNEL required in .env")
        return

    opts = Options()
    driver = webdriver.Chrome(options=opts)
    slack = SlackBotClient.from_env()

    try:
        logger.info("Opening UCLA enrollment page...")
        driver.get(UCLA_ENROLLMENT_URL)

        input("Press Enter after you've logged in and the enrollment page is visible...")

        logger.info("Starting monitor loop (refresh every %ds, courses=%s)", interval, [f"{c[0]}({c[1]})" for c in COURSES])

        while True:
            time.sleep(interval)
            driver.refresh()

            try:
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
            except Exception as e:
                logger.warning("Wait for page failed: %s", e)
                continue

            for label, class_code in COURSES:
                status, _ = get_course_availability(driver, label, class_code)
                # Only post and ping for available classes (Open or Waitlist)
                if "CLOSED" not in status and "NOT FOUND" not in status:
                    msg = f"*{status}*"
                    logger.info("Posting %s to %s", status, channel)
                    slack.post(msg)

    except KeyboardInterrupt:
        logger.info("Stopped by user")
    finally:
        driver.quit()


if __name__ == "__main__":
    main()
