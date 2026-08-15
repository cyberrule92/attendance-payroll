# Attendance & Payroll

Attendance capture and monthly payroll for a dry cleaning factory and its five
stores.

- **Kiosk** — a shared tablet at each location. Staff tap their name, enter a
  4-digit PIN, and the camera checks their face against the one on file before
  the punch is confirmed. Works offline.
- **Admin** — a browser console on the owner's Windows laptop: live attendance
  with photos, corrections, leaves, advances, and a monthly payroll run that
  implements the shop's pay rules exactly, with payslips.
- **Staff portal** — `/me` on an employee's own phone: their days, hours,
  overtime, leaves, advances and pay. Read-only, and only ever their own.

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
    auth.py              admin + staff sessions, device tokens, throttling
    routers/             kiosk, admin, employees, attendance, records,
                         payroll, portal
    services/
      payroll.py         THE RULE ENGINE - pure functions, no I/O
      attendance.py      punches -> daily records (work-date attribution)
      payroll_run.py     running and freezing a month
      face.py            ANTI-PROXY - detect, liveness, match. No I/O
      face_store.py      enrolments and verdicts (the database half)
      exports.py         payslip PDFs, Excel export
      photos.py          downscale, strip EXIF, store
    static/              kiosk.html, admin.html, portal.html, app.css, sw.js
  tests/                 152 tests
  scripts/               seed, demo_data, backup, check_frontend,
                         fetch_face_models
scripts/                 start-windows.bat, start-tunnel.bat
data/                    attendance.db + photos + models  (gitignored)
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

## Proxy attendance

The PIN proves someone knows a secret. It does not prove who is standing at the
tablet, and a PIN shared between friends is the obvious way to be paid for a
shift you did not work. So the face is checked too, and a punch only counts as
confirmed when both agree.

Three stages, in `backend/app/services/face.py`, all of them **server side** —
a kiosk is a shared tablet in a shop and cannot be trusted to grade its own
punch:

| Stage | What it answers | How |
|---|---|---|
| Detect | Is there a face? | YuNet, five landmarks |
| Liveness | Is it a person, or a picture of one? | motion + parallax across a burst |
| Match | Is it *this* employee? | SFace embedding, cosine ≥ 0.363 |

**Liveness comes before identity.** A photo of the right person must fail, so
there is no point holding up a colleague's picture. The kiosk captures a short
burst with a randomly chosen prompt ("turn your head left", "blink twice"), and
the server checks two things a still image cannot fake: that the face keeps
changing *after* the frames are aligned on the eyes and nose, and that it moves
independently of the background. A phone held up to the camera is one rigid
object — face and background move together.

**A failed check is never a locked-out employee.** Two more attempts are offered
on the spot, because bad light, a cap or a wet face at a dry cleaner all clear
up on a second try. After that the punch is *recorded and flagged*, not refused:
an unexplained gap in someone's attendance is a worse failure than a punch the
owner has been told to look at. Flagged punches appear in red on the Today board
and in the existing review queue. Set `ATTENDANCE_FACE_STRICT=true` to refuse
instead.

**Nobody is locked out on day one.** An employee with no face enrolled punches
normally, marked `NOT_ENROLLED` — an administrative gap, deliberately kept
distinct from a face that actively failed. The employee list shows who is still
outstanding.

> ### The trained anti-spoof model is not installed
>
> Motion and parallax defeat a **still** photo. They do not defeat a **video**
> replay, which moves. Closing that needs a trained print/replay classifier, and
> the pluggable stage for one is built and tested — but no model ships.
>
> OpenCV's zoo has none. The canonical implementation (minivision
> Silent-Face-Anti-Spoofing, Apache-2.0) ships PyTorch weights that need
> converting, and every ONNX conversion on public model hubs is an unvetted
> re-upload with no provenance.
>
> The blocker is verification, not effort. Validating a spoof classifier needs
> genuine live *and* genuine attack photos of real people. Without them a
> converted model cannot be shown to work — and one that misfires rejects real
> employees every morning. Wiring an unvalidated classifier into the control
> that decides pay would be worse than the honest gap.
>
> To add one: drop an ONNX file at
> `data/models/face_antispoof_minifasnet.onnx` (3-class output: print, live,
> replay) and restart. It is picked up automatically, and becomes decisive on
> its own. Validate it against your own staff first.

**Enrolment is an admin action.** Adding a reference photo is the act of saying
"this face is this person", and every later decision rests on it — so it is done
from the admin console, never at the kiosk, where the first person to reach the
tablet could otherwise register their own face against somebody else's name.

---

## The staff portal

`/me`, on an employee's own phone. Their days and hours, which nights counted as
night duty, their leaves, their advances, and what they are due — read-only, and
only ever their own.

**A separate PIN from the kiosk.** The kiosk PIN is 4 digits and is typed in the
open on a shared tablet dozens of times a day; it is shoulder-surfed as a matter
of course. If it also opened the portal, watching a colleague clock in would be
enough to read their salary. So the portal has its own 6-digit PIN, issued from
the admin console, and an employee can replace it with a password of their own —
after which the PIN the office knows stops working. Setting a new PIN clears
their password, which is how somebody who forgets one gets back in.

**Nothing to tamper with.** There is no employee id anywhere in the portal API.
Every query is scoped through the signed-in session, so there is no parameter to
change to see someone else's month. A test asserts the API surface stays that
way.

**Separate from the admin session** — its own cookie, its own signing salt, its
own lockout. An admin cookie cannot read the portal and a staff cookie cannot
read the admin console; both directions are tested. Revoking access takes effect
on the employee's next request, not whenever their cookie happens to expire.

**Finalised months are the frozen figures.** A closed month is read back from
`payroll_lines`, so what an employee sees on their phone is exactly the payslip
they were handed. The current month is computed live and labelled an estimate in
plain words, because it moves every time somebody punches — and a figure that
looks settled but is not is how arguments start.

Advances are shown even though nobody asked for them: net pay is unexplainable
without them, and an employee seeing a smaller number than the total says, with
nothing accounting for the gap, is worse than showing the deduction.

Owner-facing flags — "salary not prorated", "check the kiosk at this location" —
are filtered out. They are notes for whoever runs payroll and would only alarm
the person the row is about.

---

## Development

```bash
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/python scripts/seed.py              # schema, locations, admin account
.venv/bin/python scripts/fetch_face_models.py # face models (39 MB, once)
.venv/bin/python scripts/demo_data.py         # a sample month to click around

.venv/bin/python -m uvicorn app.main:app --reload --port 8080
```

The face models are not in the repository — 39 MB of binary that never changes.
Without them the system runs, but punches are recorded `UNAVAILABLE` rather than
verified.

Then <http://127.0.0.1:8080/admin>, <http://127.0.0.1:8080/kiosk>
and <http://127.0.0.1:8080/me>.

### Checks

```bash
.venv/bin/python -m pytest tests/ -q          # 152 tests
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
- Per-store manager logins. There is a single admin account.
- **A trained anti-spoof model.** The stage exists and loads a model if one is
  installed, but none ships. See the warning under *Proxy attendance*.
