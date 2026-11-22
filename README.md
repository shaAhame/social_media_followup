# iDealz Lead Tracker

One place to log **every inquiry from every WhatsApp hotline and social channel**, give
each one an owner and a follow-up date, and make sure nobody is forgotten.

Built for iDealz Lanka (Pvt) Ltd: a coordinator logs each inquiry in about 20 seconds
(contact number, product, buying time period, source). The system then:

- **Recognises returning customers** by phone number across all hotlines and Facebook,
  Instagram and TikTok (`077 123 4567`, `+94771234567` and `94771234567` are the same person),
  and warns before a duplicate is created.
- **Schedules follow-ups automatically** from the customer's buying time period
  (Today / This week / This month / Just checking, or a specific date).
- Gives each salesperson a **call list**: overdue, due today, needs decision, upcoming, with
  one-tap **WhatsApp** (pre-filled message) and **Call** buttons.
- Keeps a **full history** of every call, message, status change and reassignment.
- **Reports** leads, sales and conversion by source, salesperson, product and time period,
  plus lost-sale reasons and response speed. Managers can export to CSV.
- **Phone notifications**: each salesperson gets their call list at 9:45 every morning, and
  managers get a daily report at 19:30 (today's inquiries, sales, lost reasons, overdue per
  person). Tapping a notification opens the right page.
- **Installs like an app** on Android and iPhone (Add to Home Screen): icon, full screen.
- Records **marketing consent** per customer (Sri Lanka PDPA).
- Has an **API** so website forms, chatbots or a future WhatsApp Cloud API bridge can add
  leads automatically. Retries are safe because duplicate submissions are detected.

Works on desktop and mobile. Staff guide: [docs/COORDINATOR_GUIDE.md](docs/COORDINATOR_GUIDE.md).
Technical design: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Follow-up rules

| Time period | 1st follow-up | Then | Max automatic follow-ups |
|---|---|---|---|
| Today | +2 hours (within business hours) | next day, +2 days, +4 days | 4 |
| This week | next morning | +2 days, +4 days, +7 days | 4 |
| This month | +3 days | +7 days, +7 days, +14 days | 4 |
| Just checking | +7 days | +14 days, +30 days | 3 |
| Specific date (Pick a date) | 10:00 the day before that date (or within 2 hours if the date is today or tomorrow) | next day, +2 days, +4 days | 4 (the reminder counts as one) |

Day-based follow-ups land at 10:00 so each salesperson gets one clean list every morning.
After the last automatic follow-up, the inquiry moves to **Needs decision**: mark it sold or
lost, or set a date manually. Staff can always override the date when logging a follow-up.
Business hours, the maximum number of attempts and the WhatsApp message are configurable.

## Roles

| Role | Can |
|---|---|
| Sales | See all leads, work their own and unassigned leads (working a lead claims it), see their call list |
| Coordinator | Log and edit all inquiries, assign leads, see everyone's call lists and reports |
| Manager | Everything above, plus CSV export, lead sources and product list |
| Admin | Everything, plus staff accounts |

---

## Deploy on Railway (about 15 minutes)

1. **Create the project**: in Railway, choose *New Project → Deploy from GitHub repo* and pick
   this repository. Railway detects the `Dockerfile` and `railway.toml`.
2. **Add a database**: in the project, choose *New → Database → PostgreSQL*.
3. **Set variables** on the app service (*Variables → Raw Editor*):

   ```
   ENVIRONMENT=production
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   SECRET_KEY=<paste output of: python -c "import secrets;print(secrets.token_urlsafe(48))">
   SESSION_HTTPS_ONLY=true
   INITIAL_ADMIN_EMAIL=you@idealz.lk
   INITIAL_ADMIN_PASSWORD=<a temporary password>
   INITIAL_ADMIN_NAME=Your Name
   ```

4. **Get a URL**: go to *Settings → Networking → Generate Domain* (or add your own, e.g.
   `leads.idealz.lk`).
5. **Deploy.** Each deploy runs the database migrations (`alembic upgrade head`), then starts
   the app. The health check is `/healthz`.
6. **First login**: open the URL, log in with the admin email and password, then:
   - change your password (top right),
   - remove `INITIAL_ADMIN_EMAIL` and `INITIAL_ADMIN_PASSWORD` from the variables,
   - go to **Settings → Lead sources** and rename "WhatsApp Hotline 1-5" to your real
     numbers and branches,
   - (optional) add your product list under **Settings → Products**,
   - add staff under **Settings → Staff accounts**.

Resources: one app container (about 150 MB RAM) plus Railway PostgreSQL. To add workers, set
`WEB_CONCURRENCY=2`. The login rate limit is kept per worker in memory, which is fine for one
or two workers.

**Backups:** turn on backups for the PostgreSQL service in Railway (or run `pg_dump` on a
schedule). Customer phone numbers are personal data under the PDPA. Only give manager and
admin roles to people who need them, because those roles can export.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # set DATABASE_URL (PostgreSQL), or use sqlite:///./leads.db
alembic upgrade head
python -m app.cli create-user --email you@example.com --name "You" --role admin
python -m app.cli seed-demo   # optional: 120 sample inquiries for training
uvicorn app.main:app --reload
```

Open http://localhost:8000.

Run the tests (on SQLite by default; set `TEST_DATABASE_URL` to also test on PostgreSQL):

```bash
ruff check . && pytest
TEST_DATABASE_URL=postgresql://postgres@localhost:5432/leads_test pytest
```

## Configuration

All settings are environment variables; see [.env.example](.env.example).

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./leads.db` | PostgreSQL in production (`postgres://` URLs accepted) |
| `SECRET_KEY` | (dev value) | Signs session cookies; **must** be set (32+ characters) in production |
| `ENVIRONMENT` | `development` | `production` enforces a safe secret and PostgreSQL, and hides `/docs` |
| `SESSION_HTTPS_ONLY` | `false` | `true` behind HTTPS |
| `TIMEZONE` | `Asia/Colombo` | Business timezone for dates and follow-ups |
| `DEFAULT_PHONE_REGION` | `LK` | How local numbers like `077…` are read |
| `BUSINESS_DAY_START_HOUR` / `_END_HOUR` | `10` / `20` | Follow-ups are scheduled inside these hours |
| `FOLLOWUP_MAX_ATTEMPTS` | `4` | Automatic follow-ups before "Needs decision" |
| `WHATSAPP_FOLLOWUP_TEMPLATE` | see file | Pre-filled WhatsApp text: `{name}` `{agent}` `{company}` `{product}` |
| `API_KEY` | empty (API off) | Enables `POST /api/v1/inquiries` |
| `NOTIFICATIONS_ENABLED` | `true` | Phone notifications on/off for everyone |
| `MORNING_DIGEST_TIME` / `EVENING_REPORT_TIME` | `09:45` / `19:30` | Local times for the daily call list and manager report |
| `VAPID_SUBJECT` | `mailto:leads@idealz.lk` | Contact address sent to push services (any address you own) |
| `RUN_SCHEDULER` | `true` | Runs the daily jobs inside the web process (each job runs once per day, even with several workers) |
| `INITIAL_ADMIN_*` | — | Creates the first admin when there are no users |
| `SEED_DEFAULT_CHANNELS` | `true` | Creates the default lead sources on the first run |

## Phone notifications

1. Each person opens the app **on their phone**, taps **🔔 Notifications** (top right) and
   **Turn on notifications on this phone**.
   - **iPhone:** first add the app to the Home Screen (Safari → Share → Add to Home Screen),
     open it from the icon, then turn notifications on. Needs iOS 16.4 or newer.
   - **Android:** works in Chrome directly; adding to the home screen is optional.
2. Use **Send a test** to check. Managers can see who has notifications on under
   **Settings → Staff accounts**.

No setup is needed on Railway: the app creates its own signing keys on first use and stores
them in the database. The morning call list is skipped for people with nothing due.

## Lead intake API

```bash
curl -X POST https://leads.example.com/api/v1/inquiries \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"phone":"0771234567","source":"Instagram","product":"iPhone 16 Pro","timeframe":"this_week",
       "customer_name":"Nimal","external_ref":"ig-msg-123"}'
```

- `source` must match a lead source name (case-insensitive).
- `timeframe` is one of `today`, `this_week` (default), `this_month`, `just_checking` or
  `on_date` (then also send `"planned_date": "YYYY-MM-DD"`).
- If you send `external_ref` (for example a Meta message id), repeating the same request
  returns the original inquiry (`"created": false`) instead of creating a duplicate.

## Project layout

```
app/
  main.py            app factory, middleware, error pages
  config.py          settings (env vars)
  db.py              engine, sessions, UTC datetime type
  models.py          tables and enums
  security.py        passwords, sessions, roles, CSRF, login throttling
  services/          business logic (no web code): inquiries, follow-up rules, phone, reports
  routers/           web pages and API endpoints
  templates/ static/ server-rendered UI (no build step)
  cli.py             create-user, seed-demo
alembic/             database migrations
tests/               109 tests (services, web flows, API, reports, migrations)
```
