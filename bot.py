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
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

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

# Run headless by default. We override the user-agent (below) so ShipCSX does
# not treat us as a bot, plus the navigation has retries + a menu fallback, which
# together make the Angular form render fine without a real display.
# Set PLAYWRIGHT_HEADLESS=0 to force a visible browser (local debugging only).
HEADLESS = os.environ.get("PLAYWRIGHT_HEADLESS", "1").lower() in ("1", "true", "yes")

# Terminal shortcut letters -> exact label shown in the ShipCSX dropdown.
TERMINALS = {
    "C": "Chambersburg",
    "S": "South Kearny",
}

# Only one browser lookup at a time (keeps memory low on small hosts).
_lookup_lock = asyncio.Lock()


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
    """Navigate to the lookup form and make sure the Angular form has rendered.

    Retries with a reload, and falls back to opening it through the
    Intermodal > Equipment Lookup menu if the direct route comes up blank.
    """
    last_err = None
    for attempt in range(3):
        try:
            await page.goto(LOOKUP_URL, wait_until="domcontentloaded")
            # Let the Angular bundle + first XHRs settle (ignore if it never
            # fully idles because of analytics beacons).
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            try:
                await page.wait_for_selector("p-dropdown", state="visible", timeout=12000)
                return
            except Exception as e:
                last_err = e

            # Fallback: force the Intermodal > Equipment Lookup view.
            try:
                await page.get_by_text("INTERMODAL", exact=True).first.click(timeout=3000)
                await page.wait_for_timeout(600)
                link = page.get_by_role("link", name="EQUIPMENT LOOKUP")
                if await link.count():
                    await link.first.click()
                await page.wait_for_selector("p-dropdown", state="visible", timeout=12000)
                return
            except Exception as e:
                last_err = e

            await page.reload(wait_until="domcontentloaded")
        except Exception as e:
            last_err = e
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

            # --- Fill equipment + reference (first row) ---
            inputs = page.locator("input.p-inputtext")
            equip_field = inputs.nth(0)
            ref_field = inputs.nth(1)

            await equip_field.click()
            await equip_field.fill("")
            await equip_field.type(equipment_text, delay=20)

            await ref_field.click()
            await ref_field.fill("")
            await ref_field.type(reference, delay=20)

            # --- Submit ---
            await page.get_by_role("button", name="Search").click()

            # Wait for the captured response.
            body = await asyncio.wait_for(result_future, timeout=timeout_ms / 1000)
            return body
        finally:
            await browser.close()


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------
def _fmt_date(iso: str) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso.replace("Z", "")).strftime("%m/%d/%y")
    except Exception:
        return iso


def format_result(req: dict, body: dict) -> str:
    failed = body.get("failedSearchCriteria") or []
    shipments = body.get("shipments") or []

    header = f"📦 ShipCSX — {req['terminal']}\nContainer: {req['initial']} {req['number']}"
    if req.get("reference"):
        header += f"\nPickup #: {req['reference']}"

    if not shipments:
        reason = ""
        if failed:
            reason = "\nThe terminal had no match for this container + pickup number."
        return (
            f"{header}\n\n❌ No shipment found.{reason}\n"
            "Double-check the container, pickup number, and terminal."
        )

    s = shipments[0]
    status = s.get("shipmentStatus", "UNKNOWN")
    eq = s.get("equipment", {}) or {}
    chassis = eq.get("chassis") or "-"
    premise = s.get("premise", {}) or {}
    parking = premise.get("parkingLocation") or "-"
    notified = _fmt_date(premise.get("notifiedDate"))
    last_free = _fmt_date(premise.get("lastFreeDate"))
    auth_through = _fmt_date(premise.get("authorizedThroughDate"))

    # Derived readiness: NOTIFIED means the box has been made available.
    ready_line = ""
    if status == "NOTIFIED":
        ready_line = "Out-Gate: ✅ Ready to out-gate\n"

    # Free-time warning.
    warn = ""
    lf = premise.get("lastFreeDate")
    if lf:
        try:
            d = date.fromisoformat(lf)
            days = (d - date.today()).days
            if days < 0:
                warn = "\n⚠️ Past last free day — storage charges may be accruing."
            elif days == 0:
                warn = "\n⚠️ Today is the last free day."
            elif days <= 1:
                warn = f"\n⏳ {days} day of free time left."
        except Exception:
            pass

    now = datetime.now().strftime("%m/%d/%y %H:%M")
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
        f"(checked {now})"
    )


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------
def authorized(chat_id: int) -> bool:
    return not AUTHORIZED_CHAT_IDS or chat_id in AUTHORIZED_CHAT_IDS


HELP_TEXT = (
    "Send me a shipment and I'll check ShipCSX out-gate readiness.\n\n"
    "Format:\n"
    "Container# AZNU 243186\n"
    "PU# 02508796\n"
    "Terminal: C\n\n"
    "Terminals:  C = Chambersburg   S = South Kearny\n"
    "You can send several at once (one per line)."
)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_chat.id):
        return
    await update.message.reply_text(
        f"Your chat ID is {update.effective_chat.id}.\n\n{HELP_TEXT}"
    )


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not authorized(chat_id):
        log.warning("Ignoring message from unauthorized chat %s", chat_id)
        return

    text = update.message.text or ""
    shipments = parse_shipments(text)

    if not shipments or not shipments[0]["reference"] or not shipments[0]["terminal"]:
        await update.message.reply_text(
            "I couldn't read that. I need a container, a pickup number, and a "
            f"terminal (C or S).\n\n{HELP_TEXT}"
        )
        return

    for req in shipments:
        if not req["reference"] or not req["terminal"]:
            await update.message.reply_text(
                f"Skipped {req['initial']} {req['number']}: missing pickup number "
                "or terminal."
            )
            continue

        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            async with _lookup_lock:
                body = await shipcsx_lookup(
                    req["terminal"], req["initial"], req["number"], req["reference"]
                )
            await update.message.reply_text(format_result(req, body))
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"⏱️ Timed out looking up {req['initial']} {req['number']}. "
                "ShipCSX may be slow — try again."
            )
        except Exception as e:  # noqa
            log.exception("Lookup failed")
            await update.message.reply_text(
                f"⚠️ Error looking up {req['initial']} {req['number']}: {e}"
            )


def main():
    log.info("Boot: building application (headless=%s)...", HEADLESS)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    log.info("Bot starting. Authorized chats: %s", AUTHORIZED_CHAT_IDS or "ANYONE")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("FATAL: bot crashed on startup")
        raise
