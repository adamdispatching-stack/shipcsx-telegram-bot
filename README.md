# ShipCSX → Telegram status bot

A Telegram bot that checks **ShipCSX "Find My Shipment"** out-gate readiness and
replies to you in Telegram. It runs 24/7 on Railway, so it works even when your
PC is off — you just message the bot.

You send (pickup number is **optional** — leave it out when dropping/ingating):

```
AZNU243186
02508796
```

The bot replies with **buttons** asking which railyard:

> Which railyard?   [ Chambersburg ]  [ South Kearny ]

Tap one, and it replies:

```
📦 ShipCSX — Chambersburg
Container: AZNU 243186
Pickup #: 02508796

Status: NOTIFIED
Out-Gate: ✅ Ready to out-gate
Parking: WPA,1,122
Chassis: DDRZ 601871
Notified: 06/04/26
Last Free Day: 06/07/26
Authorized Through: 06/07/26

(checked 06/05/26 02:39)
```

- **Line 1** = container number, **line 2** = pickup number (optional).
- Send several containers (one per line) and the railyard you pick applies to all.
- You can still type the railyard inline (`C` / `S`) to skip the buttons.

---

## How it works

ShipCSX's data API requires a CSX login token that the website fetches
internally, so the bot doesn't call the API directly. Instead it opens the
**public** "Find My Shipment" form in a headless Chromium browser, fills in your
container / pickup / terminal, submits, and reads the structured JSON the page
receives back. No CSX account or login is needed.

Files:

| File | Purpose |
|------|---------|
| `bot.py` | The bot: Telegram long-polling + headless-browser lookup |
| `requirements.txt` | Python dependencies |
| `Dockerfile` | Build image (based on Playwright's official image) |
| `.env.example` | The environment variables you need to set |

---

## Deploy: GitHub → Railway

### Step 1 — Push to GitHub

1. Install Git: https://git-scm.com/download/win
2. Create a **new, empty** repo on GitHub (no README, no .gitignore).
3. Double-click **`push_to_github.bat`**, paste the repo URL when asked. It
   commits everything and pushes. (A GitHub login window may pop up the first
   time so Git can authenticate.)

> Future changes: edit the files, run `push_to_github.bat` again. Railway will
> auto-redeploy on each push.

### Step 2 — Connect Railway to the repo

1. Go to https://railway.app → **New Project → Deploy from GitHub repo** → pick
   your repo. Railway detects the `Dockerfile` and builds it automatically.
2. Open the service → **Variables** and add:
   - `BOT_TOKEN` = your bot token from @BotFather
   - *(Do **not** set `AUTHORIZED_CHAT_IDS`.)* Leaving it out means **anyone who
     messages the bot can use it.** To lock it down later, set it to a
     comma-separated list of chat IDs.
3. Deploy. Watch the **Deploy Logs** for `Bot starting. Authorized chats: ANYONE`.
4. In Telegram, message your bot — send `/start`, then a shipment.

> **Important:** This bot uses **long polling**, not a webhook, so it needs no
> public URL or port. Make sure only **one** copy runs at a time (don't run it
> locally and on Railway at once) or Telegram throws a `Conflict` error.

### Railway CLI alternative (skip GitHub)

```bash
npm i -g @railway/cli
railway login
cd shipcsx-telegram-bot
railway init
railway up
railway variables --set BOT_TOKEN=xxxx
```

(Or just run `deploy.bat`.)

---

## Run locally (optional, for testing)

```bash
pip install -r requirements.txt
python -m playwright install chromium
set BOT_TOKEN=xxxx
python bot.py
```

(Or just run `run_local.bat`.)

---

## Notes & limits

- Each lookup takes ~10–20s (it loads the real ShipCSX page each time).
- The bot processes one lookup at a time to keep memory low on a small instance.
  A Railway "Hobby" instance is plenty.
- If ShipCSX changes their page markup, the form selectors in `shipcsx_lookup()`
  may need a small update. The selectors are clearly marked in `bot.py`.
- **Access:** by default (no `AUTHORIZED_CHAT_IDS` set) **anyone** who finds the
  bot can run lookups. That's intended here. To restrict it later, set
  `AUTHORIZED_CHAT_IDS` to a comma-separated list of allowed chat IDs.
- The browser runs **headed inside Xvfb** (a virtual display), because ShipCSX's
  form does not render reliably in pure headless mode. The `Dockerfile` handles
  this with `xvfb-run`.

## Want scheduled checks too?

Right now the bot is request/response (you message it, it replies). If you also
want it to **auto-check certain containers every morning** and only ping you when
one becomes *Ready to out-gate*, that's a small addition — a scheduler loop plus
a watchlist. Ask and it can be added.
