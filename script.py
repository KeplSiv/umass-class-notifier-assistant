import time
import asyncio
import sys
import re
import json
import os
import subprocess
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright

def now_et():
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S")


CONFIG_FILE = os.getenv("CONFIG_FILE", "config.json")
WATCH_LIST = []
CHECK_INTERVAL = 300
SESSION_FILE = "session.json"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")
SESSION_USER = os.getenv("WATCHER_NAME")
LAST_NOTIFIED_STATES = {}
STATUS_MESSAGE_ID = None
CHECK_COUNT = 0
LAST_AUTO_LOGIN_ERROR = None

SPIRE_USERNAME = None
SPIRE_PASSWORD = None
DISCORD_BOT_TOKEN = None
DISCORD_CHANNEL_ID = None
DISCORD_USER_ID = None

GUILD_ID = None
SIGNUP_CHANNEL_ID = None
CATEGORY_ID = None
BOT_USER_ID = None

CONFIGS_DIR = "configs"
SESSIONS_DIR = "sessions"
DEBUG_LOOKUP_DIR = "debug_lookup"
ONBOARDING_STATES = {}
USER_PROCESSES = {}
LAST_BOT_MESSAGE_IDS = {}
LOGIN_FAILURE_NOTIFIED = set()
LOGIN_REQUIRED_NOTIFIED = set()
AUTH_IN_PROGRESS = set()
AUTH_LAST_ATTEMPT_AT = {}
RETRY_LOGIN = "__retry_login__"

_API_BACKOFF_UNTIL = 0.0
_API_CONSECUTIVE_FAILURES = 0
_API_LAST_ERROR_MSG = ""
_API_LAST_ERROR_TIME = 0.0

TERM        = "1267"
INSTITUTION = "UMAMH"
BASE_URL    = "https://www.spire.umass.edu"


class SessionExpired(Exception):
    pass

def load_config():
    global WATCH_LIST, CHECK_INTERVAL, SESSION_FILE
    global DISCORD_WEBHOOK_URL, SESSION_USER, TERM, INSTITUTION, BASE_URL
    global SPIRE_USERNAME, SPIRE_PASSWORD, DISCORD_BOT_TOKEN
    global DISCORD_CHANNEL_ID, DISCORD_USER_ID

    if not os.path.exists(CONFIG_FILE):
        return

    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

    if "classes" in config:
        WATCH_LIST = []
        for item in dedupe_classes(config["classes"]):
            if isinstance(item, dict):
                WATCH_LIST.append((str(item["class_nbr"]), str(item["crse_id"])))
            else:
                WATCH_LIST.append((str(item[0]), str(item[1])))

    CHECK_INTERVAL = int(config.get("check_interval", CHECK_INTERVAL))
    SESSION_FILE = config.get("session_file", SESSION_FILE)
    DISCORD_WEBHOOK_URL = config.get("discord_webhook_url") or DISCORD_WEBHOOK_URL
    SESSION_USER = config.get("name") or SESSION_USER
    TERM = str(config.get("term", TERM))
    INSTITUTION = config.get("institution", INSTITUTION)
    BASE_URL = config.get("base_url", BASE_URL)
    SPIRE_USERNAME = config.get("spire_username") or SPIRE_USERNAME
    SPIRE_PASSWORD = config.get("spire_password") or SPIRE_PASSWORD
    DISCORD_BOT_TOKEN = config.get("discord_bot_token") or DISCORD_BOT_TOKEN
    DISCORD_CHANNEL_ID = config.get("discord_channel_id") or DISCORD_CHANNEL_ID
    DISCORD_USER_ID = config.get("discord_user_id") or DISCORD_USER_ID


def validate_config():
    if not WATCH_LIST:
        print(f"No classes configured. Copy config.json.example to {CONFIG_FILE} and add classes.")
        return False
    return True


def class_url(class_nbr, crse_id):
    return (
        f"{BASE_URL}/psc/heproda/EMPLOYEE/SA/c/"
        f"SSR_STUDENT_FL.SSR_CRSE_INFO_FL.GBL"
        f"?Page=SSR_CRSE_INFO_FL&Action=U&ACAD_CAREER=UGRD"
        f"&CRSE_ID={crse_id}&CRSE_OFFER_NBR=1"
        f"&INSTITUTION={INSTITUTION}&STRM={TERM}&RVF_SW=1"
    )


def spire_start_page_url(base_url):
    return f"{base_url}/psc/heproda/EMPLOYEE/SA/s/WEBLIB_PTBR.ISCRIPT1.FieldFormula.IScript_StartPage"


def spire_landing_page_url(base_url):
    return f"{base_url}/psc/heproda/EMPLOYEE/SA/c/NUI_FRAMEWORK.PT_LANDINGPAGE.GBL"


def session_base_from_url(url):
    match = re.search(r'/(?:psc|psp)/([^/]+)/', url)
    if match:
        return f"/psc/{match.group(1)}/"
    return "/psc/heproda/"


async def warm_spire_session(page, base_url):
    for url in (spire_start_page_url(base_url), spire_landing_page_url(base_url)):
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2000)
        title = await page.title()
        if "login.microsoftonline.com" in page.url or "sign in" in title.lower():
            return False
    return True


async def click_first_available(page, selectors, timeout=5000):
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() > 0 and await locator.is_visible(timeout=1000):
                await locator.click(timeout=timeout)
                return selector
        except Exception:
            pass
    return None


async def initialize_class_search_context(page, base_url):
    if not await warm_spire_session(page, base_url):
        return False

    clicked = await click_first_available(
        page,
        [
            "text=Manage Classes",
            "a:has-text('Manage Classes')",
            "button:has-text('Manage Classes')",
            "[aria-label*='Manage Classes' i]",
        ],
        timeout=8000,
    )
    if clicked:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

    clicked = await click_first_available(
        page,
        [
            "text=Class Search and Enroll",
            "text=Class Search",
            "a:has-text('Class Search')",
            "button:has-text('Class Search')",
            "[aria-label*='Class Search' i]",
        ],
        timeout=8000,
    )
    if clicked:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)

    title = await page.title()
    return not ("login.microsoftonline.com" in page.url or "sign in" in title.lower())


async def save_session(playwright=None):
    if playwright:
        await save_session_with_playwright(playwright)
        return

    async with async_playwright() as p:
        await save_session_with_playwright(p)


async def save_session_with_playwright(playwright):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto(BASE_URL)
    print("Log into SPIRE in the browser window, then press Enter here...")
    input()
    await context.storage_state(path=SESSION_FILE)
    print(f"Session saved to {SESSION_FILE}")
    await browser.close()


async def start_watcher_browser(playwright):
    browser = await playwright.chromium.launch(headless=True)
    context = await browser.new_context(storage_state=SESSION_FILE)
    page = await context.new_page()
    return browser, context, page


async def detect_session_user(page):
    envinfo = await page.query_selector("#pt_envinfo")
    if envinfo:
        user = await envinfo.get_attribute("user")
        if user:
            return user.strip()

    match = re.search(r"User=([^;]+);", await page.content())
    return match.group(1).strip() if match else None


async def check_class(page, class_nbr, crse_id):
    global SESSION_USER

    await page.goto(class_url(class_nbr, crse_id), wait_until="domcontentloaded", timeout=90000)
    try:
        await page.wait_for_selector("#SSR_CRSE_INFO_V_SSS_SUBJ_CATLG, #DERIVED_SSR_FL_SSR_DTL_FIELD1\\$0, tr.ps_grid-row", timeout=10000)
    except Exception:
        await page.wait_for_timeout(3000)
    title = await page.title()
    print(f"  DEBUG landed on: {title} | {page.url}")

    if "login.microsoftonline.com" in page.url or "sign in" in title.lower():
        raise SessionExpired

    if not SESSION_USER:
        SESSION_USER = await detect_session_user(page)
        if SESSION_USER:
            print(f"  DEBUG session user: {SESSION_USER}")


    async def safe_text(locator):
        try:
            if await locator.count() > 0:
                return (await locator.first.inner_text()).strip()
        except Exception:
            pass
        return ""

    row = page.locator("tr.ps_grid-row").filter(has_text=f"Class {class_nbr}:").first
    if await row.count() > 0:
        course_code = await safe_text(page.locator("#SSR_CRSE_INFO_V_SSS_SUBJ_CATLG"))
        course_title = await safe_text(page.locator("#SSR_CRSE_INFO_V_COURSE_TITLE_LONG"))
        class_text = await safe_text(row.locator(f"text=Class {class_nbr}:"))
        status = await safe_text(row.locator("[id^='SSR_DER_CS_GRP_SSR_DESCR']"))
        seats_text = await safe_text(row.locator("[id^='SSR_CLSRCH_F_WK_SSR_DESCR50_1']"))
        course_name = f"{course_code}: {course_title}" if course_code else f"Class #{class_nbr}"
        print(f"  DEBUG class row: {course_name} | {class_text} | {status} | {seats_text}")

        available_seats = 0
        capacity = 0
        waitlist_cap = 0
        waitlist_space = 0

        m = re.search(r"Open Seats\s+(\d+)\s+of\s+(\d+)", seats_text, re.I)
        if m:
            available_seats = int(m.group(1))
            capacity = int(m.group(2))

        wl = re.search(r"Waitlist\s+(\d+)\s+of\s+(\d+)", seats_text, re.I)
        if wl:
            waitlist_space = int(wl.group(1))
            waitlist_cap = int(wl.group(2))

        return [{
            "course_name": course_name,
            "name": class_text,
            "status": status,
            "capacity": capacity,
            "enrolled": max(capacity - available_seats, 0),
            "available_seats": available_seats,
            "waitlist_cap": waitlist_cap,
            "waitlist_total": waitlist_cap - waitlist_space,
            "waitlist_space": waitlist_space,
        }]


    field1 = await page.query_selector("#DERIVED_SSR_FL_SSR_DTL_FIELD1\\$0")
    if field1:
        keys = ["name", "status", "capacity", "enrolled",
                "available_seats", "waitlist_cap", "waitlist_total", "waitlist_space"]
        data = {}
        for i, key in enumerate(keys, 1):
            el = await page.query_selector(f"#DERIVED_SSR_FL_SSR_DTL_FIELD{i}\\$0")
            data[key] = (await el.inner_text()).strip() if el else "0"
        for key in ["capacity", "enrolled", "available_seats", "waitlist_cap", "waitlist_total", "waitlist_space"]:
            data[key] = int(data[key] or 0)
        return [data]


    seats_el = await page.query_selector("[id^='win26divSEATS']")
    if seats_el:
        seats_text = (await seats_el.inner_text()).strip()
        print(f"  [{class_nbr}] Raw seats text: {seats_text}")

    # Save debug HTML if nothing matched
    with open(f"debug_{class_nbr}.html", "w") as f:
        f.write(await page.content())
    print(f"  [{class_nbr}] No structured data — saved debug_{class_nbr}.html")
    return []


def check_and_alert(class_nbr, data):
    if not data:
        print(f"  [{class_nbr}] No data returned")
        return None

    s        = data[0]
    course   = s.get("course_name", f"Class #{class_nbr}")
    status   = s["status"]
    avail    = s["available_seats"]
    wl_space = s["waitlist_space"]
    wl_cap   = s["waitlist_cap"]
    changed = False
    change_label = None

    print(f"  [{class_nbr}] {course} | Status={status} | Avail={avail} | WL_space={wl_space}/{wl_cap}")

    if status == "Open" and avail > 0:
        notify_type = "seat_open"
        state = ("seat_open", avail, status)
        should_notify, previous_state = update_class_state(class_nbr, state)
        if should_notify:
            if previous_state is not None:
                changed = True
                change_label = describe_state_change(previous_state, state)
        else:
            print(f"  [{class_nbr}] Seat-open alert already sent; staying quiet.")
    elif status in ("Wait List", "Waitlist") and wl_space > 0:
        notify_type = "waitlist_open"
        state = ("waitlist_open", wl_space, wl_cap, status)
        should_notify, previous_state = update_class_state(class_nbr, state)
        if should_notify:
            if previous_state is not None:
                changed = True
                change_label = describe_state_change(previous_state, state)
        else:
            print(f"  [{class_nbr}] Waitlist-open alert already sent; staying quiet.")
    else:
        notify_type = "full"
        state = ("full", status, avail, wl_space, wl_cap)
        should_notify, previous_state = update_class_state(class_nbr, state)
        if should_notify:
            if previous_state is not None:
                changed = True
                change_label = describe_state_change(previous_state, state)
        else:
            print(f"  [{class_nbr}] Full/closed alert already sent; staying quiet.")

    return {
        "class_nbr": str(class_nbr),
        "course": course,
        "status": status,
        "avail": avail,
        "wl_space": wl_space,
        "wl_cap": wl_cap,
        "notify_type": notify_type,
        "should_notify": should_notify,
        "changed": changed,
        "change_label": change_label,
    }


def update_class_state(class_nbr, state):
    previous_state = LAST_NOTIFIED_STATES.get(class_nbr)
    LAST_NOTIFIED_STATES[class_nbr] = state
    return previous_state is None or previous_state != state, previous_state


def state_value_for_diff(state):
    kind = state[0]
    if kind == "seat_open":
        return f"{state[1]} seat(s)"
    if kind == "waitlist_open":
        return f"{state[1]}/{state[2]} waitlist"
    status = state[1] or "Unknown"
    return f"{status}, {state[2]} seat(s), WL {state[3]}/{state[4]}"


def describe_state_change(previous_state, state):
    if not previous_state:
        return "Changed"

    previous_kind = previous_state[0]
    kind = state[0]
    if previous_kind == kind == "seat_open":
        return f"Seats {previous_state[1]} -> {state[1]}"
    if previous_kind == kind == "waitlist_open":
        return f"Waitlist {previous_state[1]}/{previous_state[2]} -> {state[1]}/{state[2]}"
    if previous_kind == kind == "full":
        if previous_state[1] != state[1]:
            return f"Status {previous_state[1]} -> {state[1]}"
        if previous_state[2] != state[2]:
            return f"Seats {previous_state[2]} -> {state[2]}"
        if previous_state[3:5] != state[3:5]:
            return f"Waitlist {previous_state[3]}/{previous_state[4]} -> {state[3]}/{state[4]}"
        return "Changed"

    return f"{state_value_for_diff(previous_state)} -> {state_value_for_diff(state)}"


def notify_class_status(course, class_nbr, status, avail, wl_space, wl_cap):
    status_color = 0x2ECC71 if status == "Open" and avail > 0 else 0x95A5A6
    notify(
        f"{course} (Class #{class_nbr}) checked.",
        embed={
            "title": "Class Check",
            "description": f"**{course}**\nClass #{class_nbr}",
            "color": status_color,
            "fields": [
                {"name": "Status", "value": status or "Unknown", "inline": True},
                {"name": "Available Seats", "value": str(avail), "inline": True},
                {"name": "Waitlist Space", "value": f"{wl_space}/{wl_cap}", "inline": True},
            ],
            "footer": {"text": f"Checked at {now_et()}"},
        },
    )


def notify_session_expired():
    if SPIRE_USERNAME and SPIRE_PASSWORD and DISCORD_BOT_TOKEN:
        description = "Send `/login` in Discord to re-authenticate."
    else:
        description = "Log in again in the opened browser, then press Enter in the terminal."
    if DISCORD_CHANNEL_ID and DISCORD_BOT_TOKEN:
        send_login_required_once(
            DISCORD_CHANNEL_ID,
            title="SPIRE Session Expired",
            message=description,
        )
        return
    notify(
        f"SPIRE Session Expired — {description}",
        embed={
            "title": "SPIRE Session Expired",
            "description": description,
            "color": 0xE74C3C,
            "footer": {"text": f"Detected at {now_et()}"},
        },
    )


def notify_class_full(course, class_nbr, status, avail, wl_space, wl_cap):
    notify(
        "Class status changed.",
        embed={
            "title": "Class Full",
            "description": f"**{course}**\nClass #{class_nbr}",
            "color": 0x95A5A6,
            "fields": [
                {"name": "Status", "value": status or "Unknown", "inline": True},
                {"name": "Available Seats", "value": str(avail), "inline": True},
                {"name": "Waitlist Space", "value": f"{wl_space}/{wl_cap}", "inline": True},
            ],
            "footer": {"text": f"Checked at {now_et()}"},
        },
    )


def notify_seat_open(course, class_nbr, avail, status):
    notify(
        f"SEAT OPEN: {course} (Class #{class_nbr}) — {avail} seat(s) available",
        embed={
            "title": "Seat Open",
            "description": f"**{course}**\nClass #{class_nbr}",
            "color": 0x2ECC71,
            "fields": [
                {"name": "Available Seats", "value": str(avail), "inline": True},
                {"name": "Status", "value": status or "Open", "inline": True},
            ],
            "footer": {"text": f"Checked at {now_et()}"},
        },
    )


def notify_waitlist_open(course, class_nbr, wl_space, wl_cap, status):
    notify(
        f"WAITLIST OPEN: {course} (Class #{class_nbr}) — {wl_space}/{wl_cap} spots",
        embed={
            "title": "Waitlist Open",
            "description": f"**{course}**\nClass #{class_nbr}",
            "color": 0xF1C40F,
            "fields": [
                {"name": "Waitlist Space", "value": f"{wl_space}/{wl_cap}", "inline": True},
                {"name": "Status", "value": status or "Wait List", "inline": True},
            ],
            "footer": {"text": f"Checked at {now_et()}"},
        },
    )


def notify_watcher_snapshot(all_results, changed_count):
    # Determine embed color: green > yellow > gray based on changed classes only
    notify_types = {r["notify_type"] for r in all_results if r.get("changed")}
    if "seat_open" in notify_types:
        color = 0x2ECC71
    elif "waitlist_open" in notify_types:
        color = 0xF1C40F
    else:
        color = 0x95A5A6

    fields = []
    for r in all_results:
        status = r["status"]
        avail = r["avail"]
        wl_space = r["wl_space"]
        wl_cap = r["wl_cap"]
        notify_type = r["notify_type"]

        if notify_type == "seat_open":
            emoji = "✅"
        elif notify_type == "waitlist_open":
            emoji = "⚠️"
        else:
            emoji = "🔴"

        value_parts = [f"Status: {status}", f"Seats: {avail}"]
        if wl_cap:
            value_parts.append(f"Waitlist: {wl_space}/{wl_cap}")
        value_lines = [" | ".join(value_parts)]
        if r.get("changed") and r.get("change_label"):
            value_lines.append(r["change_label"])

        fields.append({
            "name": f"{emoji} {r['course']}  #{r['class_nbr']}",
            "value": "\n".join(value_lines),
            "inline": False,
        })

    n = len(WATCH_LIST)
    change_word = "change" if changed_count == 1 else "changes"
    footer_text = f"Check #{CHECK_COUNT} · Watching {n} class{'es' if n != 1 else ''} · {now_et()}"

    notify(
        f"Watcher Update — {changed_count} {change_word}",
        embed={
            "title": f"📋 Watcher Update — {changed_count} {change_word}",
            "color": color,
            "fields": fields,
            "footer": {"text": footer_text},
        },
    )


def discord_request(payload_data, url=None, method="POST"):
    if not DISCORD_WEBHOOK_URL:
        return None

    payload = json.dumps(payload_data).encode("utf-8")
    request = urllib.request.Request(
        url or DISCORD_WEBHOOK_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "ClassNotifier/1.0",
        },
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")
            if response.status >= 300:
                print(f"  Discord webhook failed with status {response.status}")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("  Discord webhook error: 403 Forbidden — webhook is invalid/deleted/regenerated or copied wrong.")
        else:
            print(f"  Discord webhook error: HTTP {e.code}")
    except Exception as e:
        print(f"  Discord webhook error: {e}")
    return None


def _log_api_error(msg):
    global _API_LAST_ERROR_MSG, _API_LAST_ERROR_TIME
    now = time.time()
    if msg == _API_LAST_ERROR_MSG and now - _API_LAST_ERROR_TIME < 60:
        return
    _API_LAST_ERROR_MSG = msg
    _API_LAST_ERROR_TIME = now
    print(f"  Discord API error: {msg}")


def discord_bot_api(path, data=None, method="GET"):
    global _API_BACKOFF_UNTIL, _API_CONSECUTIVE_FAILURES

    if not DISCORD_BOT_TOKEN:
        return None

    now = time.time()
    if now < _API_BACKOFF_UNTIL:
        return None

    url = f"https://discord.com/api/v10{path}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
            "Content-Type": "application/json",
            "User-Agent": "ClassNotifier/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            resp_body = response.read().decode("utf-8")
            _API_CONSECUTIVE_FAILURES = 0
            return json.loads(resp_body) if resp_body else None
    except urllib.error.HTTPError as e:
        details = e.read().decode("utf-8", errors="ignore")
        if e.code in (500, 503):
            _API_CONSECUTIVE_FAILURES += 1
            delay = min(30 * (2 ** (_API_CONSECUTIVE_FAILURES - 1)), 300)
            _API_BACKOFF_UNTIL = time.time() + delay
            _log_api_error(
                f"{method} {path} -> HTTP {e.code} — backing off {delay}s "
                f"(failure #{_API_CONSECUTIVE_FAILURES})"
            )
        else:
            _API_CONSECUTIVE_FAILURES = 0
            _log_api_error(f"{method} {path} -> HTTP {e.code} {details}")
    except Exception as e:
        _API_CONSECUTIVE_FAILURES += 1
        delay = min(30 * (2 ** (_API_CONSECUTIVE_FAILURES - 1)), 300)
        _API_BACKOFF_UNTIL = time.time() + delay
        _log_api_error(
            f"{method} {path} -> {e} — backing off {delay}s "
            f"(failure #{_API_CONSECUTIVE_FAILURES})"
        )
    return None


def discord_bot_request(path):
    return discord_bot_api(path)


def discord_bot_post(path, data):
    return discord_bot_api(path, data=data, method="POST")


def discord_bot_patch(path, data):
    return discord_bot_api(path, data=data, method="PATCH")


def send_channel_message(channel_id, content=None, embed=None):
    payload = {}
    if content:
        payload["content"] = content
    if embed:
        payload["embeds"] = [embed]
    if not payload:
        return None
    return discord_bot_post(f"/channels/{channel_id}/messages", payload)


def send_login_failure_once(channel_id, detail=None, retry_message="Send `/login` when you want to try again."):
    channel_id = str(channel_id)
    if channel_id in LOGIN_FAILURE_NOTIFIED:
        print(f"  Suppressing duplicate login failure message for channel {channel_id}")
        return
    LOGIN_FAILURE_NOTIFIED.add(channel_id)
    if detail:
        send_channel_message(channel_id, f"Login failed: {detail}")
    send_channel_message(channel_id, f"Login did not finish. {retry_message}")


def clear_login_failure_notice(channel_id):
    LOGIN_FAILURE_NOTIFIED.discard(str(channel_id))


def send_login_required_once(channel_id, title="Login Required", message="Send `/login` in this channel to start authentication."):
    channel_id = str(channel_id or DISCORD_CHANNEL_ID or "")
    if not channel_id:
        return
    if channel_id in LOGIN_REQUIRED_NOTIFIED:
        print(f"  Suppressing duplicate login-required message for channel {channel_id}")
        return
    LOGIN_REQUIRED_NOTIFIED.add(channel_id)
    notify(
        message,
        embed={
            "title": title,
            "description": message,
            "color": 0xE74C3C,
            "footer": {"text": f"Waiting since {now_et()}"},
        },
    )


def clear_login_required_notice(channel_id):
    LOGIN_REQUIRED_NOTIFIED.discard(str(channel_id or DISCORD_CHANNEL_ID or ""))


def auth_is_active(channel_id):
    return str(channel_id) in AUTH_IN_PROGRESS


def mark_auth_started(channel_id):
    channel_id = str(channel_id)
    AUTH_IN_PROGRESS.add(channel_id)
    AUTH_LAST_ATTEMPT_AT[channel_id] = time.time()


def mark_auth_finished(channel_id):
    AUTH_IN_PROGRESS.discard(str(channel_id))


def auth_retry_too_soon(channel_id, cooldown=30):
    last_attempt = AUTH_LAST_ATTEMPT_AT.get(str(channel_id), 0)
    return time.time() - last_attempt < cooldown


def get_discord_channel_id():
    global DISCORD_CHANNEL_ID
    if DISCORD_CHANNEL_ID:
        return DISCORD_CHANNEL_ID
    if not DISCORD_WEBHOOK_URL:
        return None
    match = re.search(r"/webhooks/(\d+)/([^/?]+)", DISCORD_WEBHOOK_URL)
    if not match:
        return None
    webhook_id, webhook_token = match.group(1), match.group(2)
    url = f"https://discord.com/api/v10/webhooks/{webhook_id}/{webhook_token}"
    request = urllib.request.Request(url, headers={"User-Agent": "ClassNotifier/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
            DISCORD_CHANNEL_ID = str(data.get("channel_id", ""))
            return DISCORD_CHANNEL_ID
    except Exception as e:
        print(f"  Could not fetch webhook channel ID: {e}")
    return None


def get_latest_discord_message_id():
    channel_id = get_discord_channel_id()
    if not channel_id:
        return None
    messages = discord_bot_request(f"/channels/{channel_id}/messages?limit=1")
    if messages and isinstance(messages, list) and messages:
        return messages[0]["id"]
    return None


def is_retry_login_command(content):
    return content.strip().lower() in ("/login", "login", "/resend", "resend", "/retry", "retry")


def parse_otp(content):
    content = content.strip()
    match = re.match(r"^!?(?:otp\s+)?(\d{4,8})$", content, re.IGNORECASE)
    return match.group(1) if match else None


async def poll_discord_for_login_command(timeout=3600):
    """Poll Discord for a /login message. Returns True when received."""
    channel_id = get_discord_channel_id()
    if not channel_id:
        return False
    after_id = get_latest_discord_message_id()
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(10)
        path = f"/channels/{channel_id}/messages?limit=10"
        if after_id:
            path += f"&after={after_id}"
        messages = discord_bot_request(path)
        if messages and isinstance(messages, list):
            for msg in reversed(messages):
                if is_retry_login_command(msg.get("content", "")):
                    return True
    return False


async def poll_discord_for_otp(after_id, timeout=300):
    channel_id = get_discord_channel_id()
    if not channel_id:
        return None
    deadline = time.time() + timeout
    while time.time() < deadline:
        await asyncio.sleep(5)
        path = f"/channels/{channel_id}/messages?limit=10"
        if after_id:
            path += f"&after={after_id}"
        messages = discord_bot_request(path)
        if messages and isinstance(messages, list):
            for msg in reversed(messages):
                content = msg.get("content", "").strip()
                if is_retry_login_command(content):
                    print(f"  Auto-login: received retry command: {content}")
                    return RETRY_LOGIN
                otp = parse_otp(content)
                if otp:
                    print("  Auto-login: received OTP from Discord.")
                    return otp
    return None


def notify_otp_requested():
    notify(
        "OTP required for SPIRE login. Reply !otp CODE in Discord.",
        embed={
            "title": "OTP Required",
            "description": "An SMS code was sent to your phone.\nReply with `!otp CODE` in this channel to continue login.\n\nNo code? Send `/resend` to restart the full auth flow.",
            "color": 0xE67E22,
            "footer": {"text": f"Requested at {now_et()} — times out in 5 minutes"},
        },
    )


async def first_visible_locator(page, selectors, timeout=45000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        for selector in selectors:
            locator = page.locator(selector).first
            try:
                if await locator.count() > 0 and await locator.is_visible(timeout=500):
                    return locator, selector
            except Exception:
                pass
        await page.wait_for_timeout(500)
    return None, None


async def auto_login_with_otp(playwright):
    global LAST_AUTO_LOGIN_ERROR

    LAST_AUTO_LOGIN_ERROR = None
    if not (SPIRE_USERNAME and SPIRE_PASSWORD and DISCORD_BOT_TOKEN):
        LAST_AUTO_LOGIN_ERROR = "Missing SPIRE username, password, or Discord bot token."
        return False
    browser = None
    try:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = await context.new_page()

        # Go directly to the PeopleSoft login endpoint to trigger the SSO redirect
        await page.goto(f"{BASE_URL}/psp/heproda/?cmd=login", wait_until="domcontentloaded", timeout=60000)
        print(f"  Auto-login: after goto → {page.url}")

        # Page 1: email — wait for Microsoft SSO redirect to settle
        await page.wait_for_selector("#i0116", timeout=60000)
        print(f"  Auto-login: email page ready → {page.url}")
        await page.fill("#i0116", SPIRE_USERNAME)
        await page.click("#idSIButton9")

        # Page 2: password
        await page.wait_for_selector("#i0118", timeout=15000)
        print(f"  Auto-login: password page ready → {page.url}")
        await page.fill("#i0118", SPIRE_PASSWORD)
        await page.click("#idSIButton9")
        print(f"  Auto-login: password submitted → {page.url}")
        await page.wait_for_timeout(1500)
        if await page.locator("text=incorrect").count() > 0:
            LAST_AUTO_LOGIN_ERROR = "Microsoft rejected the username or password."
            await browser.close()
            return False

        # Page 3: method picker (may not always appear)
        try:
            await page.wait_for_selector("text=Verify your identity", timeout=8000)
            print(f"  Auto-login: method picker visible → {page.url}")
            for selector in [
                "[data-value='OneWaySMS']",
                "div[data-value*='Phone']",
                "li:has-text('Text')",
                "text=Text +",
            ]:
                el = page.locator(selector).first
                if await el.count() > 0:
                    await el.click()
                    print(f"  Auto-login: selected SMS via {selector}")
                    break
            # Some flows show a Next/Send button after selection (not always present)
            for btn_sel in ["#idSIButton9", "button:has-text('Next')", "button:has-text('Send')"]:
                try:
                    btn = page.locator(btn_sel).first
                    if await btn.count() > 0:
                        await btn.click(timeout=3000)
                        print(f"  Auto-login: clicked post-selection button {btn_sel}")
                        break
                except Exception:
                    pass
        except Exception as e:
            print(f"  Auto-login: method picker skipped ({e})")

        print(f"  Auto-login: waiting for OTP input → {page.url}")
        debug_path = f"autologin_debug_{SESSION_USER or 'user'}.png"
        await page.screenshot(path=debug_path)

        # Page 4: OTP entry
        otp_input, otp_selector = await first_visible_locator(
            page,
            [
                "input[type='tel'][placeholder*='Code']",
                "input[type='tel']",
                "input[name='otc']",
                "#idTxtBx_SAOTCC_OTC",
                "input[aria-label*='code' i]",
                "input[placeholder*='code' i]",
                "input[type='text'][autocomplete='one-time-code']",
            ],
            timeout=45000,
        )
        if not otp_input:
            LAST_AUTO_LOGIN_ERROR = f"Could not find OTP/code input on Microsoft page. Screenshot saved as {debug_path}."
            print(f"  Auto-login failed: {LAST_AUTO_LOGIN_ERROR} URL={page.url}")
            await browser.close()
            return False
        print(f"  Auto-login: OTP input ready via {otp_selector}")
        after_id = get_latest_discord_message_id()
        notify_otp_requested()
        otp = await poll_discord_for_otp(after_id)
        if otp == RETRY_LOGIN:
            print("  Auto-login: user requested full auth retry.")
            notify(
                "Restarting SPIRE login and requesting a fresh code.",
                embed={
                    "title": "Restarting Login",
                    "description": "Starting a new auth flow now.",
                    "color": 0x3498DB,
                },
            )
            await browser.close()
            return RETRY_LOGIN
        if not otp:
            print("  Auto-login: OTP timeout — no code received within 5 minutes.")
            await browser.close()
            return False
        await otp_input.fill(otp)
        try:
            checkbox = await page.query_selector("input[type='checkbox']")
            if checkbox and not await checkbox.is_checked():
                await checkbox.check()
        except Exception:
            pass
        for verify_sel in ["#idSubmit_SAOTCC_Continue", "input[type='submit']", "button:has-text('Verify')"]:
            btn = page.locator(verify_sel).first
            if await btn.count() > 0:
                await btn.click()
                print(f"  Auto-login: clicked verify via {verify_sel}")
                break

        # Page 5: KMSI "Stay signed in?"
        try:
            await page.wait_for_selector("#idSIButton9", timeout=8000)
            if "kmsi" in page.url.lower() or await page.locator("text=Stay signed in").count() > 0:
                await page.click("#idSIButton9")
        except Exception:
            pass

        # Wait until redirected away from Microsoft
        await page.wait_for_function(
            "() => !window.location.hostname.includes('microsoftonline.com')",
            timeout=15000,
        )
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
        if not await warm_spire_session(page, BASE_URL):
            LAST_AUTO_LOGIN_ERROR = "SPIRE redirected back to Microsoft while warming the session."
            await browser.close()
            return False
        await context.storage_state(path=SESSION_FILE)
        print(f"  Auto-login successful. Session saved to {SESSION_FILE}")
        await browser.close()
        return True
    except Exception as e:
        LAST_AUTO_LOGIN_ERROR = str(e)
        print(f"  Auto-login failed: {LAST_AUTO_LOGIN_ERROR}")
        if browser:
            await browser.close()
        return False


async def wait_for_login_then_auto_login(playwright):
    while True:
        login_requested = await poll_discord_for_login_command()
        if not login_requested:
            print("  Still waiting for /login before sending a new auth request.")
            send_login_required_once(
                DISCORD_CHANNEL_ID,
                title="Login Still Required",
                message="Send `/login` in this channel when you are ready to authenticate.",
            )
            continue

        clear_login_required_notice(DISCORD_CHANNEL_ID)
        clear_login_failure_notice(DISCORD_CHANNEL_ID)
        success = await auto_login_with_otp(playwright)
        if success == RETRY_LOGIN:
            continue
        if success:
            clear_login_required_notice(DISCORD_CHANNEL_ID)
            clear_login_failure_notice(DISCORD_CHANNEL_ID)
            return True

        send_login_failure_once(DISCORD_CHANNEL_ID, LAST_AUTO_LOGIN_ERROR)


def update_status_message(check_count, changed_classes=None):
    global STATUS_MESSAGE_ID

    changed_classes = changed_classes or {}

    # Build course name lookup from config
    course_names = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config = json.load(f)
            for item in dedupe_classes(config.get("classes", [])):
                nbr = str(item["class_nbr"])
                course_names[nbr] = item.get("course_name") or item.get("class_name") or ""
        except Exception:
            pass

    def class_status_line(class_nbr):
        class_nbr = str(class_nbr)
        name = course_names.get(class_nbr, f"Class #{class_nbr}")
        state = LAST_NOTIFIED_STATES.get(class_nbr)
        if state is None:
            emoji = "❓"
            detail = "Not yet checked"
        elif state[0] == "seat_open":
            emoji = "✅"
            detail = f"Open | {state[1]} seat{'s' if state[1] != 1 else ''}"
        elif state[0] == "waitlist_open":
            emoji = "⚠️"
            detail = f"Waitlist {state[1]}/{state[2]}"
        else:
            status_str = state[1] or "Closed"
            if status_str in ("Wait List", "Waitlist"):
                emoji = "⚠️"
                detail = "Waitlist (full)"
            else:
                emoji = "🔴"
                detail = status_str
        changed_tag = " ← changed" if class_nbr in changed_classes else ""
        return f"{emoji} {name} `#{class_nbr}` — {detail}{changed_tag}"

    embed = {
        "title": "Watcher Running",
        "description": "Monitoring SPIRE classes.",
        "color": 0x3498DB,
        "fields": [
            {"name": "Checks Completed", "value": str(check_count), "inline": True},
            {"name": "Watching", "value": f"{len(WATCH_LIST)} class(es)", "inline": True},
            {"name": "Interval", "value": f"{CHECK_INTERVAL}s", "inline": True},
            {"name": "Last Check", "value": now_et(), "inline": True},
        ],
    }

    if SESSION_USER:
        embed["fields"].append({"name": "User", "value": SESSION_USER, "inline": True})

    class_lines = [class_status_line(class_nbr) for class_nbr, _ in WATCH_LIST]
    if class_lines:
        embed["fields"].append(
            {
                "name": "Classes",
                "value": "\n".join(class_lines)[:1024],
                "inline": False,
            }
        )

    payload_data = {"embeds": [embed]}

    if DISCORD_CHANNEL_ID and DISCORD_BOT_TOKEN:
        if STATUS_MESSAGE_ID:
            discord_bot_patch(f"/channels/{DISCORD_CHANNEL_ID}/messages/{STATUS_MESSAGE_ID}", payload_data)
            return

        response = send_channel_message(DISCORD_CHANNEL_ID, embed=embed)
        if response and response.get("id"):
            STATUS_MESSAGE_ID = response["id"]
        return

    if not DISCORD_WEBHOOK_URL:
        return

    if STATUS_MESSAGE_ID:
        edit_url = f"{DISCORD_WEBHOOK_URL.rstrip('/')}/messages/{STATUS_MESSAGE_ID}"
        discord_request(payload_data, url=edit_url, method="PATCH")
        return

    response = discord_request(
        payload_data,
        url=f"{DISCORD_WEBHOOK_URL.rstrip('/')}?wait=true",
    )
    if response and response.get("id"):
        STATUS_MESSAGE_ID = response["id"]


def notify(message, embed=None):
    if SESSION_USER:
        message = f"[{SESSION_USER}] {message}"

    print(f"\n{'='*50}")
    print(f"  NOTIFICATION: {message}")
    print(f"{'='*50}\n")

    payload_data = {}
    if not embed:
        payload_data["content"] = message
    if embed:
        payload_data["embeds"] = [embed]

    if DISCORD_CHANNEL_ID and DISCORD_BOT_TOKEN:
        send_channel_message(DISCORD_CHANNEL_ID, None if embed else message, embed)
        return

    if not DISCORD_WEBHOOK_URL:
        return

    discord_request(payload_data)


def normalize_username(value):
    value = value.strip().lower()
    value = value.split("@", 1)[0]
    value = re.sub(r"[^a-z0-9_-]+", "-", value)
    value = value.strip("-_")
    return value or "user"


def channel_name_for(username):
    return f"watch-{normalize_username(username)}"


def load_bot_config():
    global DISCORD_BOT_TOKEN, GUILD_ID, SIGNUP_CHANNEL_ID, CATEGORY_ID, TERM, INSTITUTION, BASE_URL, BOT_USER_ID

    with open("bot_config.json", "r") as f:
        config = json.load(f)

    DISCORD_BOT_TOKEN = config.get("bot_token") or config.get("discord_bot_token") or DISCORD_BOT_TOKEN
    GUILD_ID = str(config.get("guild_id") or "")
    SIGNUP_CHANNEL_ID = str(config.get("signup_channel_id") or config.get("command_channel_id") or "")
    CATEGORY_ID = str(config.get("category_id") or "")
    TERM = str(config.get("term", TERM))
    INSTITUTION = config.get("institution", INSTITUTION)
    BASE_URL = config.get("base_url", BASE_URL)

    me = discord_bot_request("/users/@me")
    if me:
        BOT_USER_ID = str(me.get("id"))


def config_path_for(username):
    return os.path.join(CONFIGS_DIR, f"config_{normalize_username(username)}.json")


def read_user_config(username):
    path = config_path_for(username)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        config = json.load(f)
    if "classes" in config:
        config["classes"] = dedupe_classes(config["classes"])
    return config


def write_user_config(username, config):
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    if "classes" in config:
        config["classes"] = dedupe_classes(config["classes"])
    path = config_path_for(username)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    return path


def create_user_config(username, email, password, user_id, channel_id, classes):
    username = normalize_username(username)
    config = {
        "name": username,
        "spire_username": email,
        "spire_password": password,
        "discord_bot_token": DISCORD_BOT_TOKEN,
        "discord_user_id": str(user_id),
        "discord_channel_id": str(channel_id),
        "session_file": os.path.join(SESSIONS_DIR, f"{username}.json"),
        "term": TERM,
        "institution": INSTITUTION,
        "classes": dedupe_classes(classes),
    }
    path = write_user_config(username, config)
    return path, config


def normalize_class_item(item):
    if isinstance(item, dict):
        normalized = {
            "class_nbr": str(item["class_nbr"]),
            "crse_id": str(item["crse_id"]),
        }
        for key in ("course_name", "class_name"):
            if item.get(key):
                normalized[key] = item[key]
        return normalized

    class_nbr, crse_id = item[0], item[1]
    normalized = {"class_nbr": str(class_nbr), "crse_id": str(crse_id)}
    if len(item) > 2 and isinstance(item[2], dict):
        for key in ("course_name", "class_name"):
            if item[2].get(key):
                normalized[key] = item[2][key]
    return normalized


def class_key(item):
    item = normalize_class_item(item)
    return item["class_nbr"], item["crse_id"]


def class_exists_in_list(classes, class_nbr, crse_id):
    key = (str(class_nbr), str(crse_id))
    return any(class_key(item) == key for item in classes)


def dedupe_classes(classes):
    deduped = []
    by_key = {}
    for item in classes:
        normalized = normalize_class_item(item)
        key = (normalized["class_nbr"], normalized["crse_id"])
        if key in by_key:
            existing = by_key[key]
            for field in ("course_name", "class_name"):
                if normalized.get(field) and not existing.get(field):
                    existing[field] = normalized[field]
            continue
        by_key[key] = normalized
        deduped.append(normalized)
    return deduped


def find_user_by_channel(channel_id):
    if not os.path.isdir(CONFIGS_DIR):
        return None, None
    for filename in os.listdir(CONFIGS_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(CONFIGS_DIR, filename)
        try:
            with open(path, "r") as f:
                config = json.load(f)
            if str(config.get("discord_channel_id")) == str(channel_id):
                return config.get("name"), config
        except Exception:
            pass
    return None, None


def find_user_by_discord_id(user_id):
    if not os.path.isdir(CONFIGS_DIR):
        return None, None
    for filename in os.listdir(CONFIGS_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(CONFIGS_DIR, filename)
        try:
            with open(path, "r") as f:
                config = json.load(f)
            if str(config.get("discord_user_id")) == str(user_id):
                return config.get("name"), config
        except Exception:
            pass
    return None, None


def create_private_channel(user_id, username):
    if not (GUILD_ID and CATEGORY_ID):
        return None
    overwrites = [
        {
            "id": GUILD_ID,
            "type": 0,
            "deny": str(1024),
        },
        {
            "id": str(user_id),
            "type": 1,
            "allow": str(1024 | 2048 | 65536),
        },
    ]
    if BOT_USER_ID:
        overwrites.append(
            {
                "id": str(BOT_USER_ID),
                "type": 1,
                "allow": str(1024 | 2048 | 65536 | 8192),
            }
        )
    channel = discord_bot_post(
        f"/guilds/{GUILD_ID}/channels",
        {
            "name": channel_name_for(username),
            "type": 0,
            "parent_id": CATEGORY_ID,
            "permission_overwrites": overwrites,
        },
    )
    if channel and channel.get("id"):
        return str(channel["id"])
    return None


def pid_file_for(username):
    os.makedirs("logs", exist_ok=True)
    return os.path.join("logs", f"watcher_{username}.pid")


def _find_watcher_pids_by_env(username):
    """Return all PIDs whose /proc environ contains WATCHER_NAME=<username> (Linux only)."""
    import glob
    target = f"WATCHER_NAME={username}".encode()
    pids = []
    for environ_path in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(environ_path, "rb") as f:
                if target in f.read():
                    pids.append(int(environ_path.split("/")[2]))
        except (PermissionError, FileNotFoundError, OSError, ValueError):
            pass
    return pids


def _kill_pid(pid):
    try:
        os.kill(pid, 15)  # SIGTERM
        import time as _time
        for _ in range(20):
            _time.sleep(0.5)
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
        os.kill(pid, 9)  # SIGKILL if still alive after 10s
    except (ProcessLookupError, PermissionError):
        pass


def kill_by_pid_file(username):
    """Kill watcher via PID file, then fall back to /proc search for orphaned processes."""
    path = pid_file_for(username)
    killed_pids = set()

    # Primary: PID file
    if os.path.exists(path):
        try:
            with open(path) as f:
                pid = int(f.read().strip())
            _kill_pid(pid)
            killed_pids.add(pid)
        except (ValueError, OSError):
            pass
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    # Fallback: scan /proc for any orphaned watcher with matching WATCHER_NAME
    for pid in _find_watcher_pids_by_env(username):
        if pid not in killed_pids and pid != os.getpid():
            _kill_pid(pid)


def start_user_watcher(username, config_file=None):
    username = normalize_username(username)
    if username in USER_PROCESSES and USER_PROCESSES[username].poll() is None:
        return

    # Kill any orphaned watcher left over from a previous bot instance
    kill_by_pid_file(username)

    config_file = config_file or config_path_for(username)
    env = os.environ.copy()
    env["CONFIG_FILE"] = config_file
    env["WATCHER_NAME"] = username
    os.makedirs("logs", exist_ok=True)
    log_file = open(os.path.join("logs", f"watcher_{username}.log"), "a")
    proc = subprocess.Popen(
        [sys.executable, "-u", __file__],
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    USER_PROCESSES[username] = proc
    with open(pid_file_for(username), "w") as f:
        f.write(str(proc.pid))


def stop_user_watcher(username):
    username = normalize_username(username)
    process = USER_PROCESSES.get(username)
    if process and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
    USER_PROCESSES.pop(username, None)
    # Kill any orphaned process tracked by the PID file (handles bot restarts)
    kill_by_pid_file(username)


def start_existing_watchers():
    if not os.path.isdir(CONFIGS_DIR):
        return
    for filename in os.listdir(CONFIGS_DIR):
        if not filename.endswith(".json"):
            continue
        path = os.path.join(CONFIGS_DIR, filename)
        try:
            with open(path, "r") as f:
                config = json.load(f)
            username = config.get("name") or filename.removeprefix("config_").removesuffix(".json")
            if config.get("classes"):
                cleaned = dedupe_classes(config["classes"])
                if cleaned != config["classes"]:
                    config["classes"] = cleaned
                    write_user_config(username, config)
            if config.get("classes"):
                start_user_watcher(username, path)
        except Exception as e:
            print(f"Could not start watcher from {path}: {e}")


def format_classes(classes):
    classes = dedupe_classes(classes)
    if not classes:
        return "No classes yet. Use `/add CLASS_NBR`."
    lines = []
    for index, item in enumerate(classes, 1):
        label = item.get("course_name") or item.get("class_name") or "Name not saved yet"
        lines.append(f"{index}. {label}\n   Class `{item['class_nbr']}`")
    return "\n".join(lines)


def class_url_from_config(config, class_nbr, crse_id):
    base_url = config.get("base_url", BASE_URL)
    institution = config.get("institution", INSTITUTION)
    term = str(config.get("term", TERM))
    return (
        f"{base_url}/psc/heproda/EMPLOYEE/SA/c/"
        f"SSR_STUDENT_FL.SSR_CRSE_INFO_FL.GBL"
        f"?Page=SSR_CRSE_INFO_FL&Action=U&ACAD_CAREER=UGRD"
        f"&CRSE_ID={crse_id}&CRSE_OFFER_NBR=1"
        f"&INSTITUTION={institution}&STRM={term}&RVF_SW=1"
    )


def safe_debug_name(value):
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", str(value or "unknown")).strip("-") or "unknown"


async def save_lookup_debug(page, config, class_nbr, reason):
    os.makedirs(DEBUG_LOOKUP_DIR, exist_ok=True)
    username = safe_debug_name(config.get("name") or config.get("spire_username") or "user")
    reason = safe_debug_name(reason)
    timestamp = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d_%H%M%S")
    prefix = os.path.join(DEBUG_LOOKUP_DIR, f"{timestamp}_{username}_{class_nbr}_{reason}")

    try:
        title = await page.title()
    except Exception as e:
        title = f"<title unavailable: {e}>"

    cards = []
    try:
        for index, card in enumerate(await page.query_selector_all("li[onclick]")):
            onclick = await card.get_attribute("onclick") or ""
            text = (await card.inner_text()).strip()
            cards.append({"index": index, "onclick": onclick, "text": text[:1000]})
    except Exception as e:
        cards.append({"error": f"could not read li[onclick] cards: {e}"})

    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
    except Exception as e:
        body_text = f"<body text unavailable: {e}>"

    metadata = {
        "class_nbr": str(class_nbr),
        "reason": reason,
        "user": config.get("name"),
        "spire_username": config.get("spire_username"),
        "session_file": config.get("session_file"),
        "term": str(config.get("term", TERM)),
        "institution": config.get("institution", INSTITUTION),
        "url": page.url,
        "title": title,
        "li_onclick_count": len(cards),
        "li_onclick_cards": cards,
        "body_text_preview": body_text[:5000],
    }

    with open(f"{prefix}.json", "w") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")

    try:
        with open(f"{prefix}.html", "w") as f:
            f.write(await page.content())
    except Exception as e:
        with open(f"{prefix}.html.error.txt", "w") as f:
            f.write(str(e))

    try:
        await page.screenshot(path=f"{prefix}.png", full_page=True)
    except Exception as e:
        with open(f"{prefix}.png.error.txt", "w") as f:
            f.write(str(e))

    print(f"  Lookup debug saved: {prefix}.json / .html / .png")
    return prefix


def page_has_class_search_state_error(body_text):
    return (
        "bIsFromConfirmation" in body_text
        or "SSR_CLSRCH_ES_FL.OnExecute" in body_text
        or "An error has occurred that has stopped this transaction" in body_text
    )


async def validate_class_exists(config, class_nbr, crse_id):
    session_file = config.get("session_file")
    if not session_file or not os.path.exists(session_file):
        return False, "No SPIRE session exists yet. Finish login first.", None

    browser = None
    page = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=session_file)
            page = await context.new_page()
            await page.goto(
                class_url_from_config(config, class_nbr, crse_id),
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(1500)
            title = await page.title()
            if "login.microsoftonline.com" in page.url or "sign in" in title.lower():
                await browser.close()
                return False, "Your SPIRE session expired. Send `/login` first, then try `/add` again.", None

            row = page.locator("tr.ps_grid-row").filter(has_text=f"Class {class_nbr}:").first
            details = await page.query_selector("#DERIVED_SSR_FL_SSR_DTL_FIELD1\\$0")
            if await row.count() > 0:
                course_code = await page.locator("#SSR_CRSE_INFO_V_SSS_SUBJ_CATLG").first.inner_text()
                course_title = await page.locator("#SSR_CRSE_INFO_V_COURSE_TITLE_LONG").first.inner_text()
                class_text = await row.locator(f"text=Class {class_nbr}:").first.inner_text()
                metadata = {
                    "course_name": f"{course_code.strip()}: {course_title.strip()}",
                    "class_name": class_text.strip(),
                }
                await browser.close()
                return True, None, metadata
            if details:
                class_name = (await details.inner_text()).strip()
                metadata = {"class_name": class_name}
                await browser.close()
                return True, None, metadata

            await browser.close()
            return False, f"I could not find class `{class_nbr}` with CRSE `{crse_id}` for term `{config.get('term', TERM)}`.", None
    except Exception as e:
        if browser:
            await browser.close()
        return False, f"I could not validate that class right now: {e}", None


async def lookup_class_with_crse_id(config, class_nbr):
    session_file = config.get("session_file")
    if not session_file or not os.path.exists(session_file):
        return False, "No SPIRE session exists yet. Finish login first.", None

    base_url = config.get("base_url", BASE_URL)
    institution = config.get("institution", INSTITUTION)
    term = str(config.get("term", TERM))

    browser = None
    page = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(storage_state=session_file)
            page = await context.new_page()

            # Step 1: Warm SPIRE so PeopleSoft initializes session-scoped class-search state.
            if not await warm_spire_session(page, base_url):
                await browser.close()
                return False, "Your SPIRE session expired. Send `/login` first, then try `/add` again.", None

            # Step 2: Check for login redirect
            title = await page.title()
            if "login.microsoftonline.com" in page.url or "sign in" in title.lower():
                await browser.close()
                return False, "Your SPIRE session expired. Send `/login` first, then try `/add` again.", None

            # Step 3: Extract session base from current URL (e.g. /psc/heproda_30/)
            session_base = session_base_from_url(page.url)

            # Step 4: Navigate directly to fluid class search results (deterministic GET URL)
            search_url = (
                f"{base_url}{session_base}EMPLOYEE/SA/c/"
                f"SSR_STUDENT_FL.SSR_CLSRCH_ES_FL.GBL"
                f"?Page=SSR_CLSRCH_ES_FL"
                f"&SEARCH_GROUP=SSR_CLASS_SEARCH_LFF"
                f"&SEARCH_TEXT={class_nbr}"
                f"&ES_INST={institution}"
                f"&ES_STRM={term}"
                f"&ES_ADV=N"
                f"&INVOKE_SEARCHAGAIN=PTSF_GBLSRCH_FLUID"
            )
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2000)

            # Check login again after navigation
            title = await page.title()
            if "login.microsoftonline.com" in page.url or "sign in" in title.lower():
                await browser.close()
                return False, "Your SPIRE session expired. Send `/login` first, then try `/add` again.", None

            try:
                body_text = await page.locator("body").inner_text(timeout=3000)
            except Exception:
                body_text = ""
            if page_has_class_search_state_error(body_text):
                await save_lookup_debug(page, config, class_nbr, "class-search-state-error")
                if await initialize_class_search_context(page, base_url):
                    session_base = session_base_from_url(page.url)
                    search_url = (
                        f"{base_url}{session_base}EMPLOYEE/SA/c/"
                        f"SSR_STUDENT_FL.SSR_CLSRCH_ES_FL.GBL"
                        f"?Page=SSR_CLSRCH_ES_FL"
                        f"&SEARCH_GROUP=SSR_CLASS_SEARCH_LFF"
                        f"&SEARCH_TEXT={class_nbr}"
                        f"&ES_INST={institution}"
                        f"&ES_STRM={term}"
                        f"&ES_ADV=N"
                        f"&INVOKE_SEARCHAGAIN=PTSF_GBLSRCH_FLUID"
                    )
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    await page.wait_for_timeout(2000)

            # Step 6: Find the result card whose onclick contains class_nbr
            cards = await page.query_selector_all('li[onclick]')
            target = None
            for c in cards:
                onclick_attr = await c.get_attribute('onclick') or ''
                if class_nbr in onclick_attr:
                    target = c
                    break

            if target is None:
                debug_prefix = await save_lookup_debug(page, config, class_nbr, "class-not-found")
                await browser.close()
                return False, f"I could not find class `{class_nbr}` for term `{term}`. Debug saved: `{debug_prefix}`.", None

            # Step 7: Grab course name before clicking (card text disappears after nav)
            card_text = (await target.inner_text()).strip()
            course_name = next((line.strip() for line in card_text.split('\n') if line.strip()), class_nbr)

            # Step 8: Click the card and wait until CRSE_ID appears in the URL
            await target.click()
            await page.wait_for_url(re.compile(r'CRSE_ID='), timeout=15000)

            crse_id_match = re.search(r'CRSE_ID=(\d+)', page.url)
            if crse_id_match is None:
                await browser.close()
                return False, "Found class but could not extract its course ID. Please try again.", None

            crse_id = crse_id_match.group(1)
            await browser.close()
            return True, None, {"crse_id": crse_id, "course_name": course_name}

    except Exception as e:
        if page:
            try:
                debug_prefix = await save_lookup_debug(page, config, class_nbr, "lookup-error")
                print(f"  Lookup error debug saved for {class_nbr}: {debug_prefix}")
            except Exception as debug_error:
                print(f"  Could not save lookup error debug for {class_nbr}: {debug_error}")
        if browser:
            await browser.close()
        return False, f"I could not validate that class right now: {e}", None


async def enrich_missing_class_names(username, config):
    changed = False
    classes = dedupe_classes(config.get("classes", []))
    for item in classes:
        if item.get("course_name") or item.get("class_name"):
            continue
        if item.get("crse_id"):
            ok, error, metadata = await validate_class_exists(config, item["class_nbr"], item["crse_id"])
        else:
            ok, error, metadata = await lookup_class_with_crse_id(config, item["class_nbr"])
            if ok and metadata and metadata.get("crse_id"):
                item["crse_id"] = metadata["crse_id"]
        if ok and metadata:
            item.update(metadata)
            changed = True
    if changed or classes != config.get("classes", []):
        config["classes"] = classes
        write_user_config(username, config)
    return classes


async def handle_signup_channel_command(msg):
    user_id = str(msg["author"]["id"])
    existing_username, existing_config = find_user_by_discord_id(user_id)
    if existing_config:
        send_channel_message(
            SIGNUP_CHANNEL_ID,
            f"<@{user_id}> you already have a private channel: <#{existing_config['discord_channel_id']}>",
        )
        return

    ONBOARDING_STATES[user_id] = {
        "step": "email",
        "channel_id": None,
        "data": {"classes": []},
    }
    send_channel_message(
        SIGNUP_CHANNEL_ID,
        f"<@{user_id}> signup started. Reply here with your UMass SPIRE email; I will create your private channel after that.",
    )


def get_watcher_status(username):
    username = normalize_username(username)
    if username in AUTH_IN_PROGRESS:
        return "authenticating"
    process = USER_PROCESSES.get(username)
    if process is None:
        return "inactive"
    rc = process.poll()
    if rc is None:
        return "active"
    return "error" if rc != 0 else "inactive"


async def handle_private_channel_command(msg, username, config):
    channel_id = str(msg["channel_id"])
    user_id = str(msg["author"]["id"])
    content = msg.get("content", "").strip()
    parts = content.split()
    cmd = parts[0].lower() if parts else ""

    if cmd == "/add":
        if len(parts) < 2:
            send_channel_message(channel_id, "Usage: `/add CLASS_NBR`")
            return
        config.setdefault("classes", [])
        class_nbr = parts[1]
        existing = next((i for i in config["classes"] if str(i.get("class_nbr") if isinstance(i, dict) else i[0]) == class_nbr), None)
        if existing:
            send_channel_message(channel_id, f"Class `{class_nbr}` is already in your watch list.")
            return
        send_channel_message(channel_id, f"Checking class `{class_nbr}`...")
        ok, error, metadata = await lookup_class_with_crse_id(config, class_nbr)
        if not ok:
            send_channel_message(channel_id, error)
            return
        class_item = {"class_nbr": class_nbr, "crse_id": metadata.get("crse_id", "")}
        if metadata:
            class_item.update(metadata)
        config["classes"].append(class_item)
        write_user_config(username, config)
        stop_user_watcher(username)
        start_user_watcher(username)
        label = f" - {metadata.get('course_name') or metadata.get('class_name')}" if metadata else ""
        send_channel_message(channel_id, f"Added class `{class_nbr}`{label} and restarted your watcher.")
    elif cmd == "/remove":
        if len(parts) < 2:
            send_channel_message(channel_id, "Usage: `/remove CLASS_NBR`")
            return
        before = len(config.get("classes", []))
        config["classes"] = dedupe_classes([
            item for item in config.get("classes", [])
            if str(item.get("class_nbr") if isinstance(item, dict) else item[0]) != parts[1]
        ])
        write_user_config(username, config)
        stop_user_watcher(username)
        if config["classes"]:
            start_user_watcher(username)
        removed = before - len(config["classes"])
        send_channel_message(channel_id, f"Removed `{parts[1]}`." if removed else f"I did not find `{parts[1]}`.")
    elif cmd == "/classes":
        classes = await enrich_missing_class_names(username, config)
        send_channel_message(channel_id, format_classes(classes))
    elif cmd == "/kill":
        stop_user_watcher(username)
        send_channel_message(channel_id, "Stopped your watcher.")
    elif cmd == "/status":
        status = get_watcher_status(username)
        emoji = {"active": "✅", "inactive": "⏸️", "error": "❌", "authenticating": "🔐"}.get(status, "❓")
        send_channel_message(channel_id, f"Watcher status: {emoji} **{status}**")
    elif cmd == "/logout":
        stop_user_watcher(username)
        session_file = config.get("session_file")
        if session_file and os.path.exists(session_file):
            os.remove(session_file)
            send_channel_message(channel_id, "Logged out. Session deleted. Use `/login` to re-authenticate.")
        else:
            send_channel_message(channel_id, "No active session to delete.")
    elif cmd == "/watch":
        start_user_watcher(username)
        send_channel_message(channel_id, "Started your watcher.")
    elif cmd == "/help":
        send_channel_message(
            channel_id,
            "Commands: `/add CLASS_NBR`, `/remove CLASS_NBR`, `/classes`, `/watch`, `/kill`, `/status`, `/logout`, `/login`, `/resend`, `!otp CODE`",
        )
    elif is_retry_login_command(content):
        clear_login_required_notice(channel_id)
        await reauthenticate_user(username, config, channel_id)
    elif parse_otp(content):
        return
    else:
        state = ONBOARDING_STATES.get(user_id)
        if state:
            await handle_channel_reply(user_id, channel_id, content)


async def handle_channel_reply(user_id, channel_id, content):
    state = ONBOARDING_STATES.get(str(user_id))
    if not state:
        return

    step = state["step"]
    if step == "email":
        if "@" not in content or "." not in content:
            send_channel_message(channel_id, "That does not look like a valid UMass email. Try again.")
            return
        email = content.strip().lower()
        username = normalize_username(email)
        private_channel_id = create_private_channel(user_id, username)
        if not private_channel_id:
            send_channel_message(channel_id, "I could not create your private channel. Check my Manage Channels permission.")
            return
        state["channel_id"] = private_channel_id
        state["data"]["email"] = email
        state["data"]["username"] = username
        state["step"] = "password"
        send_channel_message(channel_id, f"<@{user_id}> your private channel is ready: <#{private_channel_id}>")
        send_channel_message(
            private_channel_id,
            f"Hi <@{user_id}>. Got it, `{username}`. Send your SPIRE password here. After that I will log in and ask for your OTP.",
        )
    elif step == "password":
        clear_login_required_notice(channel_id)
        clear_login_failure_notice(channel_id)
        state["data"]["password"] = content.strip()
        state["step"] = "logging_in"
        send_channel_message(
            channel_id,
            "Password saved locally on the VM config. Starting SPIRE login now. If UMass sends a code, reply here with `!otp CODE`. No code? Send `/resend`.",
        )
        success = await complete_onboarding_login(user_id, channel_id, state)
        if success:
            state["step"] = "adding_classes"
            send_channel_message(
                channel_id,
                "Login worked. Now add classes with `/add CLASS_NBR`. When finished, send `/done`.",
            )
        else:
            state["step"] = "password"
            send_login_failure_once(
                channel_id,
                LAST_AUTO_LOGIN_ERROR,
                "Send your SPIRE password again and I will retry.",
            )
    elif step == "logging_in":
        if parse_otp(content) or is_retry_login_command(content):
            return
        send_channel_message(channel_id, "I am still working on login. Send `!otp CODE`, or `/resend` for a fresh auth code.")
    elif step == "adding_classes":
        parts = content.split()
        cmd = parts[0].lower() if parts else ""
        if cmd == "/add" and len(parts) >= 2:
            config = read_user_config(state["data"]["username"])
            if not config:
                send_channel_message(channel_id, "I lost your temporary config. Send your SPIRE password again and I will retry login.")
                state["step"] = "password"
                return
            class_nbr = parts[1]
            existing = next((i for i in state["data"]["classes"] if str(i.get("class_nbr") if isinstance(i, dict) else i[0]) == class_nbr), None)
            if existing:
                send_channel_message(channel_id, f"Class `{class_nbr}` is already in your watch list.")
                return
            send_channel_message(channel_id, f"Checking class `{class_nbr}`...")
            ok, error, metadata = await lookup_class_with_crse_id(config, class_nbr)
            if not ok:
                send_channel_message(channel_id, error)
                return
            class_item = {"class_nbr": class_nbr, "crse_id": metadata.get("crse_id", "")}
            if metadata:
                class_item.update(metadata)
            state["data"]["classes"].append(class_item)
            label = f" - {metadata.get('course_name') or metadata.get('class_name')}" if metadata else ""
            send_channel_message(channel_id, f"Added class `{class_nbr}`{label}. Add another or send `/done`.")
        elif cmd == "/done":
            data = state["data"]
            path, config = create_user_config(
                data["username"],
                data["email"],
                data["password"],
                user_id,
                state["channel_id"],
                data["classes"],
            )
            start_user_watcher(data["username"], path)
            ONBOARDING_STATES.pop(str(user_id), None)
            send_channel_message(channel_id, f"Done. Watching {len(data['classes'])} class(es) for `{data['username']}`.")
        else:
            send_channel_message(channel_id, "Use `/add CLASS_NBR`, then `/done` when finished.")


async def complete_onboarding_login(user_id, channel_id, state):
    global SPIRE_USERNAME, SPIRE_PASSWORD, DISCORD_CHANNEL_ID, SESSION_FILE, SESSION_USER

    if auth_is_active(channel_id):
        print(f"  Ignoring duplicate onboarding auth for channel {channel_id}; auth already running.")
        return False
    mark_auth_started(channel_id)

    data = state["data"]
    username = data["username"]
    path, config = create_user_config(
        username,
        data["email"],
        data["password"],
        user_id,
        channel_id,
        data["classes"],
    )

    SPIRE_USERNAME = config["spire_username"]
    SPIRE_PASSWORD = config["spire_password"]
    DISCORD_CHANNEL_ID = str(channel_id)
    SESSION_FILE = config["session_file"]
    SESSION_USER = username

    try:
        async with async_playwright() as p:
            while True:
                success = await auto_login_with_otp(p)
                if success != RETRY_LOGIN:
                    break

        if success:
            clear_login_required_notice(channel_id)
            clear_login_failure_notice(channel_id)
            write_user_config(username, config)
            send_channel_message(channel_id, f"Session saved for `{username}`.")
        else:
            send_login_failure_once(
                channel_id,
                LAST_AUTO_LOGIN_ERROR,
                "Send your SPIRE password again and I will retry.",
            )
            try:
                os.remove(SESSION_FILE)
            except FileNotFoundError:
                pass
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return success
    finally:
        mark_auth_finished(channel_id)


async def reauthenticate_user(username, config, channel_id):
    global SPIRE_USERNAME, SPIRE_PASSWORD, DISCORD_CHANNEL_ID, SESSION_FILE, SESSION_USER

    if not (config.get("spire_username") and config.get("spire_password")):
        send_channel_message(channel_id, "I do not have SPIRE credentials saved for this user. Run `/signup` again.")
        return False
    if auth_is_active(channel_id):
        print(f"  Ignoring duplicate auth request for channel {channel_id}; auth already running.")
        return False
    if auth_retry_too_soon(channel_id):
        print(f"  Ignoring auth retry for channel {channel_id}; retry cooldown active.")
        return False

    mark_auth_started(channel_id)
    clear_login_required_notice(channel_id)
    clear_login_failure_notice(channel_id)
    send_channel_message(channel_id, "Starting a fresh SPIRE login. If UMass sends a code, reply with `!otp CODE`. No code? Send `/resend`.")

    try:
        stop_user_watcher(username)

        SPIRE_USERNAME = config["spire_username"]
        SPIRE_PASSWORD = config["spire_password"]
        DISCORD_CHANNEL_ID = str(channel_id)
        SESSION_FILE = config.get("session_file", os.path.join(SESSIONS_DIR, f"{normalize_username(username)}.json"))
        SESSION_USER = username

        async with async_playwright() as p:
            while True:
                success = await auto_login_with_otp(p)
                if success != RETRY_LOGIN:
                    break

        if success:
            clear_login_required_notice(channel_id)
            clear_login_failure_notice(channel_id)
            send_channel_message(channel_id, "Login refreshed. Restarting your watcher.")
            start_user_watcher(username)
            return True

        send_login_failure_once(channel_id, LAST_AUTO_LOGIN_ERROR)
        if config.get("classes"):
            start_user_watcher(username)
        return False
    finally:
        mark_auth_finished(channel_id)


def message_is_from_bot(msg):
    author = msg.get("author", {})
    return author.get("bot") or (BOT_USER_ID and str(author.get("id")) == str(BOT_USER_ID))


async def poll_channel(channel_id):
    path = f"/channels/{channel_id}/messages?limit=20"
    after_id = LAST_BOT_MESSAGE_IDS.get(str(channel_id))
    if after_id:
        path += f"&after={after_id}"
    messages = discord_bot_request(path)
    if not isinstance(messages, list):
        return []
    if messages:
        LAST_BOT_MESSAGE_IDS[str(channel_id)] = max(messages, key=lambda m: int(m["id"]))["id"]
    return list(reversed(messages))


def prime_channel_cursor(channel_id):
    messages = discord_bot_request(f"/channels/{channel_id}/messages?limit=1")
    if isinstance(messages, list) and messages:
        LAST_BOT_MESSAGE_IDS[str(channel_id)] = messages[0]["id"]


def prime_existing_channel_cursors():
    if SIGNUP_CHANNEL_ID:
        prime_channel_cursor(SIGNUP_CHANNEL_ID)
    for channel_id in private_channel_ids():
        prime_channel_cursor(channel_id)


def private_channel_ids():
    channel_ids = set()
    if os.path.isdir(CONFIGS_DIR):
        for filename in os.listdir(CONFIGS_DIR):
            if not filename.endswith(".json"):
                continue
            try:
                with open(os.path.join(CONFIGS_DIR, filename), "r") as f:
                    config = json.load(f)
                if config.get("discord_channel_id"):
                    channel_ids.add(str(config["discord_channel_id"]))
            except Exception:
                pass
    for state in ONBOARDING_STATES.values():
        if state.get("channel_id"):
            channel_ids.add(str(state["channel_id"]))
    return sorted(channel_ids)


async def run_bot():
    load_bot_config()
    os.makedirs(CONFIGS_DIR, exist_ok=True)
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    start_existing_watchers()

    if not (DISCORD_BOT_TOKEN and GUILD_ID and SIGNUP_CHANNEL_ID and CATEGORY_ID):
        print("bot_config.json needs bot_token, guild_id, signup_channel_id, category_id.")
        return

    prime_existing_channel_cursors()
    print("Discord bot running. Watching signup and private channels.")
    while True:
        for msg in await poll_channel(SIGNUP_CHANNEL_ID):
            if message_is_from_bot(msg):
                continue
            content = msg.get("content", "").strip()
            user_id = str(msg["author"]["id"])
            if content.lower().startswith("/signup"):
                await handle_signup_channel_command(msg)
            elif user_id in ONBOARDING_STATES and ONBOARDING_STATES[user_id].get("step") == "email":
                await handle_channel_reply(user_id, SIGNUP_CHANNEL_ID, content)

        for channel_id in private_channel_ids():
            username, config = find_user_by_channel(channel_id)
            for msg in await poll_channel(channel_id):
                if message_is_from_bot(msg):
                    continue
                user_id = str(msg["author"]["id"])
                content = msg.get("content", "").strip()
                if user_id in ONBOARDING_STATES:
                    await handle_channel_reply(user_id, channel_id, content)
                elif username and config:
                    await handle_private_channel_command(msg, username, config)

        await asyncio.sleep(3)




async def main():
    global CHECK_COUNT

    # Write our own PID file so the bot can always find and kill this process
    if SESSION_USER:
        os.makedirs("logs", exist_ok=True)
        with open(pid_file_for(SESSION_USER), "w") as f:
            f.write(str(os.getpid()))

    if not os.path.exists(SESSION_FILE):
        print("No session found.")
        async with async_playwright() as p:
            if SPIRE_USERNAME and SPIRE_PASSWORD and DISCORD_BOT_TOKEN:
                send_login_required_once(
                    DISCORD_CHANNEL_ID,
                    title="Login Required",
                    message="No SPIRE session found. Send `/login` in this channel to authenticate.",
                )
                await wait_for_login_then_auto_login(p)
            else:
                print("Opening login browser...")
                await save_session(p)

    print(f"Watching {len(WATCH_LIST)} class(es). Checking every {CHECK_INTERVAL}s...\n")

    async with async_playwright() as p:
        browser, context, page = await start_watcher_browser(p)
        update_status_message(CHECK_COUNT)

        while True:
            CHECK_COUNT += 1
            print(f"[{now_et()}] Checking...")
            session_was_refreshed = False
            all_results = []
            changed_classes = {}

            for class_nbr, crse_id in WATCH_LIST:
                try:
                    data = await check_class(page, class_nbr, crse_id)
                    result = check_and_alert(class_nbr, data)
                    if result:
                        all_results.append(result)
                        if result.get("changed"):
                            changed_classes[str(class_nbr)] = result.get("change_label") or "Changed"
                except SessionExpired:
                    print("  Session expired. Waiting for /login in Discord...")
                    notify_session_expired()
                    await context.close()
                    await browser.close()
                    if SPIRE_USERNAME and SPIRE_PASSWORD and DISCORD_BOT_TOKEN:
                        await wait_for_login_then_auto_login(p)
                    else:
                        await save_session(p)
                    browser, context, page = await start_watcher_browser(p)
                    session_was_refreshed = True
                    break
                except Exception as e:
                    print(f"  Error checking {class_nbr}: {e}")

            changed_count = sum(1 for r in all_results if r.get("changed"))
            if changed_count > 0:
                notify_watcher_snapshot(all_results, changed_count)

            update_status_message(CHECK_COUNT, changed_classes)
            if session_was_refreshed:
                print("  Session refreshed. Checks will resume on the next cycle.")

            await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--bot":
        asyncio.run(run_bot())
    elif len(sys.argv) > 1 and sys.argv[1] == "--login":
        load_config()
        asyncio.run(save_session())
    else:
        load_config()
        asyncio.run(main())
        
