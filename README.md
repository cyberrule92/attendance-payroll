# Attendance & Payroll

Attendance capture and monthly payroll for a dry cleaning factory and its five
stores.

- **Kiosk** — a shared tablet at each location. Staff tap their name, enter a
  4-digit PIN, and the camera records a photo with the punch. Works offline.
- **Admin** — a browser console on the owner's Windows laptop: live attendance
  with photos, corrections, leaves, advances, and a monthly payroll run that
  implements the shop's pay rules exactly, with payslips.

**To run it, read [RUNBOOK.md](RUNBOOK.md).** This file is the technical
overview.

---

## The pay rules

Implemented from `salary.txt` in `backend/app/services/payroll.py`, as pure
functions with no database or HTTP involved, so they can be tested directly.
`backend/tests/test_payroll.py` covers each rule and its boundaries.

| | Rule |
|---|---|
| Shift | 8h30m of worked duration, not a clock window |
| Rates | daily = salary ÷ 30, hourly = salary ÷ 30 ÷ 8.5 — always ÷30 |
| Overtime | worked − 8h30m; a day contributing under 30 min is discarded |
| Night duty | a day whose OT *exceeds* the employee's bar (4h for Sita and Shama, 5h otherwise). Its OT leaves the pool — only night pay applies |
| Night pay | nights × daily rate |
| 4h-bar adjustment | a further 1.5h × nights removed from the pool, clamped at zero |
| Half day | 3h30m–5h00m worked → half a day's rate deducted, no OT |
| Leave | absent, or under 3h30m → one day's rate deducted |
| Week off | Thursday: paid, and not a leave |
| Bonus | flat 2 days' salary when leaves = 0, or nights ≥ leaves + half days |
| Grand total | salary + OT + night + bonus − leave − half-day |
| Net payable | grand total − advances taken that month |

### Decisions the rules did not cover

Each of these was a real gap. They are marked in the code where they apply.

| Situation | What the system does |
|---|---|
| Worked duration | First IN to last OUT. Breaks are not subtracted. |
| Under 3h30m worked | Counts as a full leave. |
| 5h–8h30m worked | Normal paid day: no deduction, no overtime. |
| Advances | Deducted in full from the same calendar month's payout. |
| Pay period | Calendar month, 1st to last day. |
| **Working the weekly off** | Never deducted, but real hours worked on a Thursday still earn OT and can qualify as a night. Otherwise a Thursday night shift would be worked for free. |
| **Night shifts crossing midnight** | A work day runs 05:00 to 05:00, so a 20:00→03:00 shift stays one day. Grouping by calendar date would have split it in half and paid no night duty. |
| **The current month, mid-month** | Days that have not happened yet are not counted as absences. |
| Mid-month joiner or leaver | Days outside their service period are ignored; salary is **not** prorated, and the payroll line is flagged so it can be adjusted by hand. |

---

## How it is built

FastAPI + SQLAlchemy + SQLite, matching the stack already in use on this
machine. The frontend is plain HTML, CSS and JavaScript with **no build step**,
so deploying to the laptop needs Python and nothing else.

```
backend/
  app/
    main.py              FastAPI app, static files, health
    config.py            settings, all overridable from .env
    models.py            ORM models
    auth.py              admin sessions, kiosk device tokens, PIN throttling
    routers/             kiosk, admin, employees, attendance, records, payroll
    services/
      payroll.py         THE RULE ENGINE - pure functions, no I/O
      attendance.py      punches -> daily records (work-date attribution)
      payroll_run.py     running and freezing a month
      exports.py         payslip PDFs, Excel export
      photos.py          downscale, strip EXIF, store
    static/              kiosk.html, admin.html, app.css, sw.js
  tests/                 89 tests
  scripts/               seed, demo_data, backup, check_frontend
scripts/                 start-windows.bat, start-tunnel.bat
data/                    attendance.db + photos  (gitignored)
```

### Points worth knowing

**Punches are idempotent.** Every punch carries a client-generated UUID. A
kiosk retrying an upload from its offline queue can never create a duplicate.

**The kiosk queues locally first.** Punches and photos go into IndexedDB before
any upload is attempted, so a closed laptop cannot lose attendance. The service
worker only caches the app shell — the queue is driven by the page, because
Background Sync is still missing on iOS.

**Corrections are never overwritten.** Admin overrides live in separate
`manual_*` columns; daily records are recomputed from punches around them.

**Finalised payroll is frozen.** Finalising copies every figure into
`payroll_lines`. Editing an old punch afterwards cannot change a payslip that
has already been handed out. There is a test for exactly this.

**Money is `Decimal`, durations are integer minutes.** No binary floats
anywhere in the pay path. Each payslip component is rounded to paise and the
total is the sum of the rounded parts, so a payslip adds up on paper.

**Photos are personal data.** They are downscaled to 640px, re-encoded to strip
EXIF (including any GPS the camera stamped in), and served only through an
authenticated route — never as public static files.

---

## Development

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/seed.py          # schema, locations, admin account
.venv/bin/python scripts/demo_data.py     # a sample month to click around

.venv/bin/python -m uvicorn app.main:app --reload --port 8080
```

Then <http://127.0.0.1:8080/admin> and <http://127.0.0.1:8080/kiosk>.

### Checks

```bash
.venv/bin/python -m pytest tests/ -q          # 89 tests
.venv/bin/python scripts/check_frontend.py    # parses the inline JS
```

Run `check_frontend.py` after touching `static/*.html`. With no build step, a
stray quote would otherwise reach the owner as a blank screen — which is
exactly how it was caught during development.

### Testing the payroll rules against a real month

The acceptance test that matters: take a month you already calculated by hand,
enter it, and compare employee by employee. Payroll → **Why** shows the
day-by-day working behind every figure.

---

## Not included

- Statutory PF, ESI or TDS. The output is the computation in `salary.txt`.
- Public holidays — mark them as approved paid leave.
- Face recognition. The photo is evidence for the owner to review; PIN plus
  photo makes buddy-punching traceable, not impossible.
- Per-store manager logins. There is a single admin account.
