"""Collection phase — drive Playwright against the frontend and capture responses.

Usage:
    PYTHONPATH=. python tests/eval/collect.py --scenario all --host <IP> --port 9000
    PYTHONPATH=. python tests/eval/collect.py --scenario financial_deep_dive
"""

import argparse
import asyncio
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import yaml
from playwright.async_api import async_playwright, Page

SCENARIOS_DIR = Path(__file__).parent / "scenarios"
GOLDEN_DIR = Path(__file__).parent / "golden"
RESULTS_DIR = Path(__file__).parent / "results"

FRONTEND_URL = "http://localhost:5173"

# DB-mode login credentials (override via env). Used by ensure_logged_in().
DEFAULT_EVAL_EMAIL = "kshitij@nuvantaai.com"
DEFAULT_EVAL_PASSWORD = "Test@1234"

# Follow-up map for clarification responses (reuse pattern from e2e_live)
FOLLOWUP_MAP = {
    "trial balance": "Show trial balance for the current financial year",
    "balance": "Show the trial balance",
    "which": "The current financial year",
    "specify": "For the current financial year April 2025 to March 2026",
    "date": "From April 2025 to March 2026",
    "period": "Current financial year",
    "company": "The currently loaded company",
}


def load_scenario(name: str, tally_mode: str = "live") -> dict:
    """Load a scenario YAML file by name. Prefers *_mock.yaml in mock mode."""
    if tally_mode == "mock":
        mock_path = SCENARIOS_DIR / f"{name}_mock.yaml"
        if mock_path.exists():
            print(f"  Using mock variant: {mock_path.name}")
            return yaml.safe_load(mock_path.read_text())
    path = SCENARIOS_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Scenario not found: {path}")
    return yaml.safe_load(path.read_text())


def load_golden(scenario_name: str) -> dict | None:
    """Load golden fixture data if available."""
    path = GOLDEN_DIR / f"{scenario_name}.json"
    if path.exists():
        return json.loads(path.read_text())
    return None


def generate_followup(clarification_msg: str, original_query: str) -> str:
    """Generate follow-up for clarification responses.

    Always incorporates the original query intent so we don't accidentally
    redirect (e.g. asking for P&L but following up with trial balance).
    """
    return f"{original_query} for the current financial year April 2025 to March 2026"


async def wait_for_response(page: Page, message_count_before: int, timeout: float = 480) -> None:
    """Wait for a new non-loading assistant message to appear.

    Waits until:
    1. A new message bubble appears (count increases)
    2. The loading indicator (bounce dots) disappears
    """
    start = time.time()
    while time.time() - start < timeout:
        # Count current message bubbles (both user and assistant)
        count = await page.locator("[class*='justify-start'], [class*='justify-end']").count()
        if count > message_count_before:
            # Check that loading dots are gone
            loading = await page.locator(".animate-bounce").count()
            if loading == 0:
                # Give a small buffer for DOM to settle
                await page.wait_for_timeout(500)
                return
        await page.wait_for_timeout(500)
    raise TimeoutError(f"No response after {timeout}s")


async def extract_last_response(page: Page) -> dict:
    """Extract the last assistant message content from the page.

    Returns dict with: message, has_table, table_data, has_chart, chart_spec
    """
    result = {
        "response_message": "",
        "has_table": False,
        "table_data": None,
        "has_chart": False,
        "chart_spec": None,
    }

    # Get all assistant message bubbles (left-aligned)
    assistant_msgs = page.locator("[class*='justify-start']")
    count = await assistant_msgs.count()
    if count == 0:
        return result

    last_msg = assistant_msgs.nth(count - 1)

    # Extract text content
    prose_el = last_msg.locator(".prose")
    if await prose_el.count() > 0:
        result["response_message"] = (await prose_el.first.inner_text()).strip()

    # Check for data table
    table_el = last_msg.locator("table")
    if await table_el.count() > 0:
        result["has_table"] = True
        # Extract table headers
        headers = []
        th_els = table_el.first.locator("th")
        for i in range(await th_els.count()):
            headers.append(await th_els.nth(i).inner_text())
        # Extract table rows
        rows = []
        tr_els = table_el.first.locator("tbody tr")
        for i in range(await tr_els.count()):
            row = []
            td_els = tr_els.nth(i).locator("td")
            for j in range(await td_els.count()):
                row.append(await td_els.nth(j).inner_text())
            rows.append(row)
        result["table_data"] = {"headers": headers, "rows": rows}

    # Check for chart (via data-chart-spec attribute)
    chart_el = last_msg.locator("[data-chart-spec]")
    if await chart_el.count() > 0:
        result["has_chart"] = True
        spec_str = await chart_el.first.get_attribute("data-chart-spec")
        if spec_str:
            try:
                result["chart_spec"] = json.loads(spec_str)
            except json.JSONDecodeError:
                result["chart_spec"] = None

    return result


async def ensure_logged_in(page: Page, frontend_url: str) -> None:
    """Log in if the app is in DB mode (shows a LoginPage), else no-op.

    DB-mode (VITE_DB_MODE=true) renders LoginPage at "/" with email/password
    inputs (#email, #password) and a "Sign in" submit button. Legacy mode has
    no login form and goes straight to the chat UI — in that case this returns
    immediately so existing query scenarios keep working.

    Credentials come from EVAL_EMAIL / EVAL_PASSWORD env vars, falling back to
    DEFAULT_EVAL_EMAIL / DEFAULT_EVAL_PASSWORD.
    """
    await page.goto(frontend_url)

    email_input = page.locator("#email")
    try:
        await email_input.wait_for(state="visible", timeout=5000)
    except Exception:
        # No login form -> legacy mode (or already authenticated). Nothing to do.
        print("No login form detected — assuming legacy mode / already authenticated.")
        return

    email = os.environ.get("EVAL_EMAIL", DEFAULT_EVAL_EMAIL)
    password = os.environ.get("EVAL_PASSWORD", DEFAULT_EVAL_PASSWORD)

    await email_input.fill(email)
    await page.locator("#password").fill(password)
    await page.get_by_role("button", name="Sign in").click()

    # On success the app navigates away from /login to the chat UI. Wait for the
    # login form to disappear as the signal that auth succeeded.
    try:
        await email_input.wait_for(state="hidden", timeout=30000)
    except Exception as exc:
        # Surface any inline login error to make failures debuggable.
        err = page.locator("div.bg-red-50")
        detail = ""
        if await err.count() > 0:
            detail = (await err.first.inner_text()).strip()
        raise RuntimeError(
            f"Login did not complete for {email!r}"
            + (f": {detail}" if detail else " (login form still visible after 30s)")
        ) from exc
    await page.wait_for_load_state("networkidle", timeout=15000)
    print(f"Logged in as {email}")


async def ensure_conversation(page: Page, timeout: float = 30000) -> None:
    """Ensure a chat ``textarea`` is available after login.

    In DB mode the app lands on a workspace page (/w/:id) and ChatWindow renders
    its ChatInput textarea immediately. If the textarea isn't present (e.g. the
    app showed a workspace landing without an open conversation), click the
    "+ New Chat" affordance so a conversation/textarea renders.

    The sidebar New Chat button uses test id ``sidebar-new-chat-active`` (active
    workspace landing) or ``sidebar-new-chat``. Raises TimeoutError with a clear
    message if no textarea ever appears.
    """
    textarea = page.locator("textarea")
    try:
        await textarea.first.wait_for(state="visible", timeout=5000)
        return
    except Exception:
        pass

    # No textarea yet — try to open/create a conversation via the sidebar.
    for testid in ("sidebar-new-chat-active", "sidebar-new-chat"):
        new_chat = page.get_by_test_id(testid)
        if await new_chat.count() > 0:
            try:
                await new_chat.first.click()
                break
            except Exception:
                continue

    try:
        await textarea.first.wait_for(state="visible", timeout=timeout)
    except Exception as exc:
        raise TimeoutError(
            "No chat textarea appeared after login. Tried clicking the "
            "'+ New Chat' affordance (sidebar-new-chat-active / sidebar-new-chat) "
            f"but no <textarea> became visible within {timeout / 1000:.0f}s."
        ) from exc


def _parse_inr(text: str) -> float | None:
    """Parse an Indian-formatted rupee string (₹12,34,567.00 / Rs. 5,900.00) to a float."""
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


async def wait_for_voucher_card(page: Page, timeout: float = 480) -> str:
    """Wait for a voucher review card to appear and return its entry id.

    Polls for any element with a ``voucher-review-<id>`` test id and for the
    loading dots to clear. Raises TimeoutError with a readable message on failure.
    """
    start = time.time()
    while time.time() - start < timeout:
        cards = page.locator("[data-testid^='voucher-review-']")
        if await cards.count() > 0:
            # Ensure the agent finished thinking (bounce dots gone)
            loading = await page.locator(".animate-bounce").count()
            if loading == 0:
                await page.wait_for_timeout(500)
                testid = await cards.last.get_attribute("data-testid")
                return (testid or "voucher-review-").replace("voucher-review-", "", 1)
        await page.wait_for_timeout(500)
    raise TimeoutError(
        f"No voucher review card ([data-testid^='voucher-review-']) appeared after {timeout}s"
    )


async def extract_voucher_card(page: Page, entry_id: str) -> dict:
    """Extract structured fields from a voucher review card into the transcript.

    Expands the card first so GST lines (rendered only in the expanded detail
    view) are captured. Returns a dict with voucher_type, party, amount, date,
    narration, gst_lines, warnings.
    """
    data: dict = {
        "voucher_type": None,
        "party": None,
        "amount": None,
        "amount_text": None,
        "date": None,
        "narration": None,
        "gst_lines": [],
        "warnings": [],
        "status": None,
    }

    card = page.locator(f"[data-testid='voucher-review-{entry_id}']")
    if await card.count() == 0:
        return data

    # Voucher type from the badge
    badge = card.locator(f"[data-testid='voucher-type-badge-{entry_id}']")
    if await badge.count() > 0:
        data["voucher_type"] = (await badge.first.inner_text()).strip()

    # Status label sits next to the badge ("Draft"/"Pending"/"Written"/"Discarded")
    status_label = card.locator("span.text-sm.font-medium").first
    if await status_label.count() > 0:
        data["status"] = (await status_label.inner_text()).strip()

    # Field rows render as "Label: value" — read the whole card text and parse.
    card_text = await card.inner_text()
    for line in card_text.splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith(("party:", "vendor:")) and data["party"] is None:
            data["party"] = line.split(":", 1)[1].strip()
        elif low.startswith("date:") and data["date"] is None:
            data["date"] = line.split(":", 1)[1].strip()
        elif low.startswith("amount:") and data["amount_text"] is None:
            amt_text = line.split(":", 1)[1].strip()
            data["amount_text"] = amt_text
            data["amount"] = _parse_inr(amt_text)

    # Warnings (if rendered)
    warn_el = card.locator(f"[data-testid='voucher-warnings-{entry_id}']")
    if await warn_el.count() > 0:
        warn_text = (await warn_el.first.inner_text()).strip()
        data["warnings"] = [w.strip() for w in warn_text.splitlines() if w.strip()]

    # Expand to reveal narration + GST lines (only present in expanded view)
    show_details = card.locator("button:has-text('Show details')")
    if await show_details.count() > 0:
        try:
            await show_details.first.click()
            await page.wait_for_timeout(300)
        except Exception:
            pass

    expanded = card.locator(f"[data-testid='voucher-expanded-{entry_id}']")
    if await expanded.count() > 0:
        exp_text = await expanded.first.inner_text()
        for line in exp_text.splitlines():
            line = line.strip()
            if line.lower().startswith("narration:"):
                data["narration"] = line.split(":", 1)[1].strip()
            elif line.upper().startswith("GST:"):
                # Format: "GST: CGST: ₹450.00, SGST: ₹450.00"
                gst_body = line.split(":", 1)[1].strip()
                for chunk in gst_body.split(","):
                    if ":" in chunk:
                        ledger, amt = chunk.rsplit(":", 1)
                        data["gst_lines"].append({
                            "ledger": ledger.strip(),
                            "amount": _parse_inr(amt),
                            "amount_text": amt.strip(),
                        })

    return data


async def screenshot_element(page: Page, selector: str, path: Path) -> bool:
    """Take a screenshot of a specific element. Returns True if successful."""
    el = page.locator(selector)
    if await el.count() > 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        await el.first.screenshot(path=str(path))
        return True
    return False


async def _capture_last_message_screenshot(
    page: Page, screenshots_dir: Path, scenario_name: str, turn_idx: int
) -> str | None:
    """Screenshot the last assistant message bubble. Returns path or None."""
    assistant_msgs = page.locator("[class*='justify-start']")
    last_msg_count = await assistant_msgs.count()
    if last_msg_count == 0:
        return None
    last_msg = assistant_msgs.nth(last_msg_count - 1)
    full_path = screenshots_dir / f"{scenario_name}_turn{turn_idx + 1}_full.png"
    try:
        await last_msg.screenshot(path=str(full_path))
        return str(full_path)
    except Exception:
        return None


async def _run_query_turn(
    page: Page,
    turn_def: dict,
    turn_idx: int,
    total_turns: int,
    scenario_name: str,
    screenshots_dir: Path,
    expect: dict,
    ground_truth: dict | None,
) -> dict:
    """Run a standard NL query turn (default turn type). Behavior unchanged."""
    query = turn_def["query"]
    print(f"  Turn {turn_idx + 1}/{total_turns}: {query[:60]}...")

    msg_count_before = await page.locator(
        "[class*='justify-start'], [class*='justify-end']"
    ).count()

    textarea = page.locator("textarea")
    await textarea.fill(query)
    await textarea.press("Enter")

    start_time = time.time()
    try:
        await wait_for_response(page, msg_count_before + 1)  # +1 for user msg
        latency = time.time() - start_time
    except TimeoutError:
        latency = time.time() - start_time
        print(f"    TIMEOUT after {latency:.1f}s")

    response = await extract_last_response(page)

    # Clarification follow-up handling
    is_clarification = False
    msg_lower = response["response_message"].lower()
    has_data = response.get("has_table") or response.get("has_chart")
    if not has_data:
        clarification_keywords = ["specify", "clarif", "could you provide", "what type", "more specific", "which one"]
        if any(kw in msg_lower for kw in clarification_keywords):
            is_clarification = True
    if is_clarification:
        followup = generate_followup(response["response_message"], query)
        print(f"    Clarification detected, following up: {followup[:60]}...")
        msg_count_before = await page.locator(
            "[class*='justify-start'], [class*='justify-end']"
        ).count()
        await textarea.fill(followup)
        await textarea.press("Enter")
        try:
            await wait_for_response(page, msg_count_before + 1)
            latency += time.time() - start_time
        except TimeoutError:
            pass
        response = await extract_last_response(page)

    # Screenshots
    chart_screenshot = None
    table_screenshot = None
    full_screenshot = None

    assistant_msgs = page.locator("[class*='justify-start']")
    last_msg_count = await assistant_msgs.count()
    if last_msg_count > 0:
        last_msg = assistant_msgs.nth(last_msg_count - 1)

        if response["has_chart"]:
            chart_path = screenshots_dir / f"{scenario_name}_turn{turn_idx + 1}_chart.png"
            chart_el = last_msg.locator("[data-testid='chart-container']")
            if await chart_el.count() > 0:
                await chart_el.first.screenshot(path=str(chart_path))
                chart_screenshot = str(chart_path)

        if response["has_table"]:
            table_path = screenshots_dir / f"{scenario_name}_turn{turn_idx + 1}_table.png"
            table_el = last_msg.locator("table")
            if await table_el.count() > 0:
                table_container = last_msg.locator(".overflow-x-auto")
                if await table_container.count() > 0:
                    await table_container.first.evaluate("el => el.style.overflow = 'visible'")
                await table_el.first.screenshot(path=str(table_path))
                table_screenshot = str(table_path)

        full_path = screenshots_dir / f"{scenario_name}_turn{turn_idx + 1}_full.png"
        await last_msg.screenshot(path=str(full_path))
        full_screenshot = str(full_path)

    print(f"    Done ({latency:.1f}s) — table={response['has_table']}, chart={response['has_chart']}")

    return {
        "turn_index": turn_idx + 1,
        "type": "query",
        "query": query,
        "is_clarification": is_clarification,
        "response_message": response["response_message"],
        "response_data": response["table_data"],
        "has_table": response["has_table"],
        "has_chart": response["has_chart"],
        "chart_spec": response["chart_spec"],
        "screenshot_chart": chart_screenshot,
        "screenshot_table": table_screenshot,
        "screenshot_full": full_screenshot,
        "latency_seconds": round(latency, 2),
        "expected": expect,
        "ground_truth": ground_truth,
    }


async def _run_upload_turn(
    page: Page,
    turn_def: dict,
    turn_idx: int,
    scenario_name: str,
    screenshots_dir: Path,
    expect: dict,
    ground_truth: dict | None,
) -> dict:
    """Run an upload turn: set a file on the hidden file input, optionally send a
    message, then wait for the voucher review card and extract its fields.
    """
    message = turn_def.get("message", "")
    file_path = turn_def.get("file_path")
    print(f"  Turn {turn_idx + 1} [upload]: {file_path}")

    resolved = Path(file_path)
    if not resolved.is_absolute():
        # file_path is relative to repo root (where collect.py is invoked)
        resolved = Path.cwd() / file_path
    if not resolved.exists():
        raise FileNotFoundError(f"Upload fixture not found: {resolved}")

    error = None
    voucher_data = None
    entry_id = None
    start_time = time.time()

    try:
        file_input = page.locator("[data-testid='file-input']")
        if await file_input.count() == 0:
            raise RuntimeError("file-input not found — is FileAttachButton rendered?")
        await file_input.first.set_input_files(str(resolved))

        # Optionally add a textual instruction alongside the upload
        if message:
            textarea = page.locator("textarea")
            await textarea.fill(message)
            await textarea.press("Enter")

        entry_id = await wait_for_voucher_card(page)
        voucher_data = await extract_voucher_card(page, entry_id)
        latency = time.time() - start_time
        print(
            f"    Card extracted ({latency:.1f}s) — type={voucher_data.get('voucher_type')}, "
            f"party={voucher_data.get('party')}, amount={voucher_data.get('amount')}"
        )
    except (TimeoutError, RuntimeError, Exception) as e:  # noqa: BLE001
        latency = time.time() - start_time
        error = f"{type(e).__name__}: {e}"
        print(f"    UPLOAD FAILED after {latency:.1f}s: {error}")

    response = await extract_last_response(page)
    full_screenshot = await _capture_last_message_screenshot(
        page, screenshots_dir, scenario_name, turn_idx
    )

    return {
        "turn_index": turn_idx + 1,
        "type": "upload",
        "query": message or f"[upload {Path(file_path).name}]",
        "file_path": str(file_path),
        "voucher_entry_id": entry_id,
        "response_message": response["response_message"],
        "response_data": voucher_data,
        "has_table": False,
        "has_chart": False,
        "chart_spec": None,
        "screenshot_chart": None,
        "screenshot_table": None,
        "screenshot_full": full_screenshot,
        "latency_seconds": round(latency, 2),
        "error": error,
        "expected": expect,
        "ground_truth": ground_truth,
    }


async def _run_action_turn(
    page: Page,
    turn_def: dict,
    turn_idx: int,
    scenario_name: str,
    screenshots_dir: Path,
    expect: dict,
    ground_truth: dict | None,
) -> dict:
    """Run an action turn: click a voucher card button ("Write to Tally" /
    "Discard" / "Edit Entry") and capture the resulting message + card status.
    """
    button = turn_def.get("button", "Write to Tally")
    print(f"  Turn {turn_idx + 1} [action]: click '{button}'")

    error = None
    card_status = None
    voucher_data = None
    start_time = time.time()

    try:
        btn = page.locator(f"button:has-text('{button}')").first
        if await btn.count() == 0:
            raise RuntimeError(f"Action button '{button}' not found on page")

        # Identify the card we're acting on (last review card) before clicking
        cards = page.locator("[data-testid^='voucher-review-']")
        entry_id = None
        if await cards.count() > 0:
            testid = await cards.last.get_attribute("data-testid")
            entry_id = (testid or "").replace("voucher-review-", "", 1)

        await btn.click()

        # Wait for the action to resolve: spinner gone AND status no longer Draft/Pending.
        # Terminal statuses are "Written" (approve) or "Discarded" (discard).
        timeout = 480
        deadline = time.time() + timeout
        while time.time() < deadline:
            spinner = await page.locator("[data-testid='spinner']").count()
            status_text = ""
            if entry_id:
                card = page.locator(f"[data-testid='voucher-review-{entry_id}']")
                if await card.count() > 0:
                    label = card.locator("span.text-sm.font-medium").first
                    if await label.count() > 0:
                        status_text = (await label.inner_text()).strip()
            if spinner == 0 and status_text in ("Written", "Discarded"):
                card_status = status_text
                break
            await page.wait_for_timeout(500)
        else:
            # Loop exhausted without break
            card_status = status_text or "unknown"
            raise TimeoutError(
                f"Action '{button}' did not resolve within {timeout}s (last status: {card_status!r})"
            )

        # Re-extract card fields for the judge (now in terminal state)
        if entry_id:
            voucher_data = await extract_voucher_card(page, entry_id)
        latency = time.time() - start_time
        print(f"    Action resolved ({latency:.1f}s) — status={card_status}")
    except (TimeoutError, RuntimeError, Exception) as e:  # noqa: BLE001
        latency = time.time() - start_time
        error = f"{type(e).__name__}: {e}"
        print(f"    ACTION FAILED after {latency:.1f}s: {error}")

    response = await extract_last_response(page)
    full_screenshot = await _capture_last_message_screenshot(
        page, screenshots_dir, scenario_name, turn_idx
    )

    return {
        "turn_index": turn_idx + 1,
        "type": "action",
        "query": f"[action: {button}]",
        "button": button,
        "card_status": card_status,
        "response_message": response["response_message"],
        "response_data": voucher_data,
        "has_table": False,
        "has_chart": False,
        "chart_spec": None,
        "screenshot_chart": None,
        "screenshot_table": None,
        "screenshot_full": full_screenshot,
        "latency_seconds": round(latency, 2),
        "error": error,
        "expected": expect,
        "ground_truth": ground_truth,
    }


async def collect_scenario(
    scenario: dict,
    frontend_url: str = FRONTEND_URL,
    golden_data: dict | None = None,
    scenario_file: str | None = None,
    run_dir: Path | None = None,
    tally_mode: str = "live",
) -> dict:
    """Run a full scenario through the frontend and collect responses.

    Args:
        scenario: Parsed YAML scenario dict
        frontend_url: URL of the running frontend
        golden_data: Optional golden fixture data for ground truth
        scenario_file: YAML filename stem (used for matching in judge)
        run_dir: Directory for this run's outputs (screenshots, transcripts)

    Returns:
        Transcript dict with metadata and per-turn results
    """
    scenario_name = scenario_file or scenario["name"].lower().replace(" ", "_").replace("-", "_")
    base_dir = run_dir if run_dir else RESULTS_DIR
    screenshots_dir = base_dir / "screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    transcript = {
        "scenario_name": scenario["name"],
        "scenario_file": scenario_name,
        "timestamp": datetime.now().isoformat(),
        "frontend_url": frontend_url,
        "tally_mode": tally_mode,
        "turns": [],
    }

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        # Navigate to frontend and log in (DB mode). Legacy mode -> no-op.
        await ensure_logged_in(page, frontend_url)

        # Ensure a chat textarea is available (open/create a conversation if needed).
        await ensure_conversation(page)
        # Give extra time for CompanySelector to populate
        await page.wait_for_timeout(2000)

        # Set tally mode via frontend toggle (legacy-mode only; DB mode sets
        # Tally connection per-workspace, so the toggle/label may be absent).
        label = page.get_by_test_id("tally-status-label")
        if await label.count() == 0:
            print(f"Tally mode toggle not present (DB mode) — using workspace's Tally connection; requested mode '{tally_mode}' not toggled.")
            current_label = None
        else:
            toggle = page.get_by_test_id("demo-mode-toggle")
            current_label = await label.inner_text()
        if current_label is None:
            pass
        elif tally_mode == "mock" and current_label != "Demo":
            await toggle.click()
            await page.wait_for_load_state("networkidle", timeout=10000)
            await page.wait_for_selector("textarea", timeout=30000)
            await page.wait_for_timeout(2000)
            print("Tally mode set to: mock")
        elif tally_mode == "live" and current_label == "Demo":
            await toggle.click()
            await page.wait_for_load_state("networkidle", timeout=10000)
            await page.wait_for_selector("textarea", timeout=30000)
            await page.wait_for_timeout(2000)
            print("Tally mode set to: live")
        else:
            print(f"Tally mode already: {tally_mode}")

        for turn_idx, turn_def in enumerate(scenario["turns"]):
            turn_type = turn_def.get("type", "query")
            expect = turn_def.get("expect", {})

            # Resolve ground truth (shared across all turn types)
            ground_truth_key = expect.get("ground_truth_key")
            ground_truth = None
            if ground_truth_key and golden_data:
                ground_truth = golden_data.get(ground_truth_key)

            if turn_type == "upload":
                turn_result = await _run_upload_turn(
                    page, turn_def, turn_idx, scenario_name, screenshots_dir, expect, ground_truth
                )
            elif turn_type == "action":
                turn_result = await _run_action_turn(
                    page, turn_def, turn_idx, scenario_name, screenshots_dir, expect, ground_truth
                )
            else:
                turn_result = await _run_query_turn(
                    page, turn_def, turn_idx, len(scenario["turns"]),
                    scenario_name, screenshots_dir, expect, ground_truth,
                )

            transcript["turns"].append(turn_result)

        # Full page screenshot at end
        final_path = screenshots_dir / f"{scenario_name}_final.png"
        await page.screenshot(path=str(final_path), full_page=True)
        transcript["final_screenshot"] = str(final_path)

        await browser.close()

    return transcript


def save_transcript(scenario_name: str, transcript: dict, run_dir: Path | None = None) -> Path:
    """Save transcript to run_dir/transcripts/ directory."""
    base_dir = run_dir if run_dir else RESULTS_DIR
    transcripts_dir = base_dir / "transcripts"
    transcripts_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{scenario_name}_{timestamp}.json"
    path = transcripts_dir / filename
    path.write_text(json.dumps(transcript, indent=2, default=str))
    print(f"Transcript saved: {path}")
    return path


def list_scenarios() -> list[str]:
    """List all available scenario names (excludes *_mock.yaml variants)."""
    return [
        p.stem for p in sorted(SCENARIOS_DIR.glob("*.yaml"))
        if not p.stem.endswith("_mock")
    ]


async def main():
    parser = argparse.ArgumentParser(description="Eval collection phase")
    parser.add_argument(
        "--scenario",
        default="all",
        help="Scenario name or 'all' (default: all)",
    )
    parser.add_argument("--frontend-url", default=FRONTEND_URL)
    parser.add_argument("--host", default=None, help="Tally host for live ground truth")
    parser.add_argument("--port", type=int, default=9000, help="Tally port")
    parser.add_argument(
        "--tally-mode",
        choices=["mock", "live"],
        default="live",
        help="Tally mode: mock (uses built-in mock data) or live (real Tally)",
    )
    args = parser.parse_args()

    # Auto-detect Tally from config if --host not provided
    if not args.host:
        try:
            from backend.config import settings
            if settings.TALLY_HOST and settings.TALLY_HOST != "localhost":
                args.host = settings.TALLY_HOST
                args.port = settings.TALLY_PORT
                print(f"Auto-detected Tally at {args.host}:{args.port} from .env")
        except Exception:
            pass

    # Determine which scenarios to run
    if args.scenario == "all":
        scenario_names = list_scenarios()
    else:
        scenario_names = [args.scenario]

    print(f"Running {len(scenario_names)} scenario(s): {', '.join(scenario_names)}")

    # Create timestamped run directory
    run_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RESULTS_DIR / f"run_{run_timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    # Create/update latest symlink
    latest_link = RESULTS_DIR / "latest"
    if latest_link.is_symlink() or latest_link.exists():
        latest_link.unlink()
    latest_link.symlink_to(run_dir.name)

    print(f"Run directory: {run_dir}")

    # Collect ground truth based on tally mode
    live_golden = {}
    if args.tally_mode == "mock":
        mock_golden_path = GOLDEN_DIR / "mock_golden.json"
        if mock_golden_path.exists():
            with open(mock_golden_path) as f:
                live_golden = json.load(f)
            print(f"Loaded mock golden data from {mock_golden_path}")
        else:
            print(f"Warning: mock golden data not found at {mock_golden_path}")
    elif args.host:
        print(f"Collecting live ground truth from Tally at {args.host}:{args.port}...")
        from tally_bridge.client import TallyClient
        from tally_bridge.queries import reports

        client = TallyClient(host=args.host, port=args.port)
        try:
            live_golden["trial_balance"] = (
                await reports.trial_balance(client, "01-04-2025", "31-03-2026")
            ).model_dump(mode="json")
            live_golden["profit_and_loss"] = (
                await reports.profit_and_loss(client, "01-04-2025", "31-03-2026")
            ).model_dump(mode="json")
            live_golden["balance_sheet"] = (
                await reports.balance_sheet(client, "31-03-2026")
            ).model_dump(mode="json")
            live_golden["bills_receivable"] = [
                b.model_dump(mode="json")
                for b in await reports.bills_receivable(client, "31-03-2026")
            ]
            live_golden["stock_summary"] = await reports.stock_summary(client, "31-03-2026")
            print(f"  Collected {len(live_golden)} ground truth datasets")
        finally:
            await client.close()

    for name in scenario_names:
        print(f"\n{'='*60}")
        print(f"Scenario: {name}")
        print(f"{'='*60}")

        scenario = load_scenario(name, tally_mode=args.tally_mode)
        golden = live_golden if live_golden else load_golden(name)

        transcript = await collect_scenario(
            scenario,
            frontend_url=args.frontend_url,
            golden_data=golden,
            scenario_file=name,
            run_dir=run_dir,
            tally_mode=args.tally_mode,
        )

        save_transcript(name, transcript, run_dir=run_dir)

    print(f"\nCollection complete. Results in {run_dir}")


if __name__ == "__main__":
    asyncio.run(main())
