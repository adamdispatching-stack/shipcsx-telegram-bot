"""
ShipCSX -> Telegram status bot.

You message the bot with a container, pickup number, and terminal.
It drives the public ShipCSX "Find My Shipment" form in a headless browser,
captures the JSON the page receives, and replies with the status.

Runs 24/7 on Railway (or any always-on host). No CSX login required.
"""

import os
import re
import asyncio
import logging
from datetime import datetime, date

from playwright.async_api import async_playwright
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO
)
log = logging.getLogger("shipcsx-bot")

# ---------------------------------------------------------------------------
# Config (set these as environment variables in Railway)
# ---------------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]

# Comma-separated Telegram chat IDs allowed to use the bot.
# Default is EMPTY, which means ANYONE who messages the bot can use it.
# To restrict it later, set AUTHORIZED_CHAT_IDS=123,456 in the environment.
_auth = os.environ.get("AUTHORIZED_CHAT_IDS", "").strip()
AUTHORIZED_CHAT_IDS = {int(x) for x in _auth.split(",") if x.strip()} if _auth else set()

LOOKUP_URL = "https://next.shipcsx.com/#/shipment/lookup"
SEARCH_API_FRAGMENT = "/shipments/search"

# A real Chrome user-agent. ShipCSX serves a blank/limited page to the default
# "HeadlessChrome" UA, so we override it.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Run HEADED by default (a virtual Xvfb display is provided by entrypoint.sh on
# the server). ShipCSX's Angular form renders reliably in a headed browser but
# is flaky in pure headless. Set PLAYWRIGHT_HEADLESS=1 to force headless.
HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "0").lower() in ("1", "true", "yes")

# Terminal shortcut letters -> exact label shown in the ShipCSX dropdown.
TERMINALS = {
    "C": "Chambersburg",
    "S": "South Kearny",
}

# Only one browser lookup at a time (keeps memory low on small hosts).
_lookup_lock = asyncio.Lock()

# Pending lookups awaiting a railyard-button tap: id -> list[shipment dicts].
# In-memory only (cleared on restart), which is fine for this use.
PENDING: dict[int, list] = {}
_pending_counter = 0


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------
CONTAINER_RE = re.compile(r"\b([A-Za-z]{4})\s*[-]?\s*(\d{6,7})\b")


def resolve_terminal(text: str):
    """Return the dropdown label for the requested terminal, or None."""
    t = text.lower()
    if "kearny" in t or "south kearny" in t:
        return "South Kearny"
    if "chambersburg" in t or "chambers" in t:
        return "Chambersburg"
    # Look for a labelled terminal line, e.g. "Terminal: C"
    m = re.search(r"termin\w*\s*[:#-]?\s*([cs])\b", t)
    if m:
        return TERMINALS.get(m.group(1).upper())
    # Look for a bare single letter token on its own line
    for line in text.splitlines():
        s = line.strip().upper()
        if s in ("C", "S"):
            return TERMINALS[s]
    # Last resort: a standalone C or S token anywhere (e.g. "... 02508796 C").
    # Only counts a single letter surrounded by whitespace / string edges, so it
    # won't be confused with the 4-letter container initial.
    m = re.search(r"(?:^|\s)([CS])(?:\s|$)", text.upper())
    if m:
        return TERMINALS[m.group(1)]
    return None


def parse_shipments(text: str):
    """
    Parse one or more shipments from a free-form message.

    Accepts things like:
        Container# AZNU 243186
        PU# 02508796
        Terminal: C
    or:
        AZNU243186 02508796 C
    Multiple containers/pickup numbers (one per line) are paired in order.
    """
    terminal = resolve_terminal(text)

    containers = []  # list of (initial, number)
    spans = []
    for m in CONTAINER_RE.finditer(text):
        containers.append((m.group(1).upper(), m.group(2)))
        spans.append((m.start(), m.end()))

    # Reference / pickup numbers: 5-9 digit numbers that are NOT inside a
    # container match.
    refs = []
    for m in re.finditer(r"\b\d{5,9}\b", text):
        inside = any(s <= m.start() < e for s, e in spans)
        if not inside:
            refs.append(m.group(0))

    shipments = []
    for i, (initial, number) in enumerate(containers):
        ref = refs[i] if i < len(refs) else (refs[0] if refs else None)
        shipments.append(
            {
                "initial": initial,
                "number": number,
                "equipment_id": f"{initial}{number}",
                "reference": ref,
                "terminal": terminal,
            }
        )
    return shipments


# ---------------------------------------------------------------------------
# Browser lookup against the public ShipCSX form
# ---------------------------------------------------------------------------
async def _open_lookup_form(page):
    """Navigate to the lookup form and wait until the Angular form has rendered.

    Retries the whole load (goto + reload) several times, since a cold browser
    on a small host can take a while to paint the SPA.
    """
    last_err = None
    for attempt in range(4):
        try:
            if attempt == 0:
                await page.goto(LOOKUP_URL, wait_until="domcontentloaded")
            else:
                await page.goto(LOOKUP_URL, wait_until="domcontentloaded")
                try:
                    await page.reload(wait_until="domcontentloaded")
                except Exception:
                    pass
            # Let the Angular bundle settle (ignore if analytics beacons keep it
            # from ever fully idling).
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            await page.wait_for_selector("p-dropdown", state="visible", timeout=20000)
            return
        except Exception as e:
            last_err = e
            log.warning("Form not ready (attempt %s): %s", attempt + 1, e)
            await page.wait_for_timeout(1500)
    raise last_err or RuntimeError("ShipCSX lookup form did not render")


async def shipcsx_lookup(terminal_label: str, equip_initial: str, equip_number: str,
                         reference: str, timeout_ms: int = 45000) -> dict:
    """
    Fill the public ShipCSX form and return the JSON the page receives from
    the /shipments/search call. Raises on failure.
    """
    equipment_text = f"{equip_initial} {equip_number}"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        try:
            context = await browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = await context.new_page()

            # Capture the search API JSON the page receives.
            result_future: asyncio.Future = asyncio.get_event_loop().create_future()

            async def on_response(resp):
                if SEARCH_API_FRAGMENT in resp.url and resp.request.method == "POST":
                    try:
                        body = await resp.json()
                        if not result_future.done():
                            result_future.set_result(body)
                    except Exception as e:  # noqa
                        if not result_future.done():
                            result_future.set_exception(e)

            page.on("response", lambda r: asyncio.create_task(on_response(r)))

            # Open the lookup form (with retries + menu fallback).
            await _open_lookup_form(page)
            await page.wait_for_timeout(500)

            # --- Select terminal (PrimeNG dropdown) ---
            await page.locator("p-dropdown").first.click()
            await page.wait_for_timeout(300)
            # Option text in the open panel.
            option = page.locator(
                ".p-dropdown-item, li[role='option']", has_text=terminal_label
            ).filter(has_text=terminal_label).first
            await option.scroll_into_view_if_needed()
            await option.click()
            await page.wait_for_timeout(300)

            # --- Fill equipment + (optional) reference (first row) ---
            # Real keystrokes are required: Angular only registers the field as
            # valid (and enables Search) on genuine key events.
            inputs = page.locator("input.p-inputtext")
            equip_field = inputs.nth(0)
            ref_field = inputs.nth(1)

            await equip_field.click()
            await equip_field.fill("")
            await equip_field.type(equipment_text, delay=25)

            if reference:
                await ref_field.click()
                await ref_field.fill("")
                await ref_field.type(reference, delay=25)

            # --- Submit (wait for the button to become enabled first) ---
            search_btn = page.get_by_role("button", name="Search")
            try:
                await search_btn.wait_for(state="visible", timeout=5000)
                # Give Angular a moment to flip the button to enabled.
                for _ in range(20):
                    if await search_btn.is_enabled():
                        break
                    await page.wait_for_timeout(150)
            except Exception:
                pass
            await search_btn.click()

            # Wait for the captured response.
            body = await asyncio.wait_for(result_future, timeout=timeout_ms / 1000)
            return body
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _fmt_date(iso) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "")).strftime("%m/%d/%y")
    except Exception:
        return str(iso)


def _fmt_dt_local(iso, tz_name) -> str:
    """A UTC ISO timestamp in the terminal's local time, as MM/DD HH:MM."""
    if not iso:
        return "-"
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if tz_name:
            try:
                from zoneinfo import ZoneInfo
                dt = dt.astimezone(ZoneInfo(tz_name))
            except Exception:
                pass
        return dt.strftime("%m/%d %H:%M")
    except Exception:
        return str(iso)


def _now() -> str:
    return datetime.now().strftime("%m/%d/%y %H:%M")


def _format_ingate(header: str, s: dict, status: str) -> str:
    """Driver is DROPPING a load — show in-gate readiness + gate window."""
    wb = s.get("waybill", {}) or {}
    res = s.get("reservation", {}) or {}
    term = s.get("terminal", {}) or {}
    tz = term.get("timezoneID")

    load_empty = wb.get("shipmentType") or "-"
    waybill_date = _fmt_date(wb.get("waybillDate"))
    rdy = res.get("ingateReadiness", {}) or {}
    rdy_text = rdy.get("statusText") or "-"
    mark = "✅ " if rdy.get("statusCode") == "RTIG" else ""
    gate_to = _fmt_dt_local(res.get("ingateDateTo"), tz)
    res_id = res.get("reservationId")

    lines = [
        header, "",
        f"Status: {status}",
        f"In-Gate: {mark}{rdy_text}",
        f"Load/Empty: {load_empty}",
        f"Waybill Date: {waybill_date}",
        f"Gate Window: expires {gate_to}",
    ]
    if res_id:
        lines.append(f"Reservation: {res_id}")
    lines += ["", f"(checked {_now()})"]
    return "\n".join(lines)


def _format_outgate(header: str, s: dict, status: str) -> str:
    """Driver is PICKING UP — show out-gate readiness + storage / free time."""
    eq = s.get("equipment", {}) or {}
    chassis = eq.get("chassis") or "-"
    premise = s.get("premise", {}) or {}
    parking = premise.get("parkingLocation") or "-"
    notified = _fmt_date(premise.get("notifiedDate"))
    last_free = _fmt_date(premise.get("lastFreeDate"))
    auth_through = _fmt_date(premise.get("authorizedThroughDate"))

    ready_line = ""
    if status == "NOTIFIED":
        ready_line = "Out-Gate: ✅ Ready to out-gate\n"

    warn = ""
    lf = premise.get("lastFreeDate")
    if lf:
        try:
            days = (date.fromisoformat(lf) - date.today()).days
            if days < 0:
                warn = "\n⚠️ Past last free day — storage charges may be accruing."
            elif days == 0:
                warn = "\n⚠️ Today is the last free day."
            elif days <= 1:
                warn = f"\n⏳ {days} day of free time left."
        except Exception:
            pass

    return (
        f"{header}\n\n"
        f"Status: {status}\n"
        f"{ready_line}"
        f"Parking: {parking}\n"
        f"Chassis: {chassis}\n"
        f"Notified: {notified}\n"
        f"Last Free Day: {last_free}\n"
        f"Authorized Through: {auth_through}"
        f"{warn}\n\n"
        f"(checked {_now()})"
    )


def format_result(req: dict, body: dict) -> str:
    failed = body.get("failedSearchCriteria") or []
    shipments = body.get("shipments") or []

    header = f"📦 ShipCSX — {req['terminal']}\nContainer: {req['initial']} {req['number']}"
    if req.get("reference"):
        header += f"\nPickup #: {req['reference']}"

    if not shipments:
        desc = ""
        if failed:
            desc = (failed[0] or {}).get("failedSearchDescription") or ""
        reason = f"\n{desc}." if desc else ""
        return (
            f"{header}\n\n❌ Not found at this terminal.{reason}\n"
            "Check the container number and try the other railyard."
        )

    s = shipments[0]
    status = s.get("shipmentStatus", "UNKNOWN")

    # In-gate (dropping a load) vs out-gate (picking up).
    if status == "INGATE" or s.get("reservation"):
        return _format_ingate(header, s, status)
    return _format_outgate(header, s, status)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
def authorized(chat_id: int) -> bool:
    return not AUTHORIZED_CHAT_IDS or chat_id in AUTHORIZED_CHAT_IDS


HELP_TEXT = (
    "Send me a container and I'll check ShipCSX, then ask which railyard.\n\n"
    "Format (pickup # is optional):\n"
    "AZNU243186\n"
    "02508796\n\n"
    "• Line 1 = container number\n"
    "• Line 2 = pickup number — leave it out if you're dropping / ingating\n\n"
    "You can send several containers (one per line). I'll pop up buttons for the "
    "railyard, so you don't have to type C or S."
)


def _yard_keyboard(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Chambersburg", callback_data=f"yard|C|{pid}"),
            InlineKeyboardButton("South Kearny", callback_data=f"yard|S|{pid}"),
        ]]
    )


def _summary(shipments: list) -> str:
    lines = []
    for s in shipments:
        line = f"• {s['initial']} {s['number']}"
        if s.get("reference"):
            line += f"  (PU# {s['reference']})"
        lines.append(line)
    return "\n".join(lines)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return
    await update.message.reply_text(HELP_TEXT)


async def run_and_reply(message, ctx: ContextTypes.DEFAULT_TYPE, shipments: list):
    """Run each shipment lookup and reply with the result."""
    chat_id = message.chat_id
    for req in shipments:
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            async with _lookup_lock:
                body = await shipcsx_lookup(
                    req["terminal"], req["initial"], req["number"],
                    req.get("reference"),
                )
            await message.reply_text(format_result(req, body))
        except asyncio.TimeoutError:
            await message.reply_text(
                f"⏱️ Timed out looking up {req['initial']} {req['number']}. "
                "ShipCSX may be slow — try again."
            )
        except Exception as e:  # noqa
            log.exception("Lookup failed")
            await message.reply_text(
                f"⚠️ Error looking up {req['initial']} {req['number']}: {e}"
            )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global _pending_counter
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        log.warning("Ignoring message from unauthorized chat %s", chat_id)
        return

    shipments = parse_shipments(update.message.text or "")
    if not shipments:
        await update.message.reply_text(
            "I couldn't find a container number in that.\n\n" + HELP_TEXT
        )
        return

    # If the user already typed a terminal (C/S/name), just run it.
    if shipments[0].get("terminal"):
        await run_and_reply(update.message, ctx, shipments)
        return

    # Otherwise ask which railyard with inline buttons.
    _pending_counter += 1
    pid = _pending_counter
    PENDING[pid] = shipments
    await update.message.reply_text(
        "Which railyard?\n" + _summary(shipments),
        reply_markup=_yard_keyboard(pid),
    )


async def on_yard_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, code, pid_s = query.data.split("|")
        pid = int(pid_s)
    except Exception:
        return
    terminal = TERMINALS.get(code)
    shipments = PENDING.pop(pid, None)
    if not shipments or not terminal:
        await query.edit_message_text(
            "That request expired — please resend the container."
        )
        return
    for s in shipments:
        s["terminal"] = terminal
    await query.edit_message_text(f"🔎 Checking {terminal}…\n{_summary(shipments)}")
    await run_and_reply(query.message, ctx, shipments)


def main():
    log.info("Boot: building application (headless=%s)...", HEADLESS)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CallbackQueryHandler(on_yard_callback, pattern=r"^yard\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Bot starting. Authorized chats: %s", AUTHORIZED_CHAT_IDS or "ANYONE")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("FATAL: bot crashed on startup")
        raise
