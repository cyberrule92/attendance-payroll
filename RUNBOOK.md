# Running the attendance system

Plain instructions for day-to-day use. No programming needed.

---

## One-time setup

### 1. On your laptop

1. Install **Python 3.12** from <https://www.python.org/downloads/>.
   During setup, tick **"Add python.exe to PATH"**. This matters — without it
   nothing else works.
2. Copy the whole `attendance` folder to the laptop, for example `C:\attendance`.
3. Double-click `scripts\start-windows.bat`.

   The first run takes a few minutes. It will print an **admin username and
   password**. Write these down — the password is not stored anywhere readable
   and cannot be shown again.

4. Open <http://127.0.0.1:8080/admin> and sign in.

### 2. Give the kiosks an address

The kiosks are in five different shops, so they reach the laptop over the
internet. Two things make that work: a tunnel (your laptop has no fixed
internet address) and https (phone browsers refuse to switch the camera on
over a plain address).

1. Install Cloudflare's tool. Open PowerShell and run:

   ```
   winget install --id Cloudflare.cloudflared
   ```

2. Double-click `scripts\start-tunnel.bat` in a **second** window, leaving the
   first one running.
3. It prints an address like `https://something-random.trycloudflare.com`.
   That is the address the kiosks use.

> **Worth doing properly:** the quick tunnel above gives a *new address every
> time it restarts*, which means re-pairing every kiosk. For a permanent
> address, see "A permanent web address" at the end.

### 3. Add your locations and staff

In the admin console:

1. **Kiosks → Add a kiosk** — the setup already created one per location.
2. **Employees → Add employee** for each person. For each one set:
   - Monthly salary
   - A 4-digit PIN (tell them what it is)
   - **Night duty above**: `4` hours for Sita and Shama, `5` for everyone else
   - **Overtime given back per night**: `1.5` for Sita and Shama, `0` for everyone else
   - Weekly off: Thursday
3. **Add each person's face.** On the Employees screen press **Face** next to
   their name, then either **Use camera** to take the photo on the laptop with
   them standing there, or add photos from a file.

   Take two or three: front on, eyes open, ordinary indoor light, no cap or
   sunglasses. This is the single thing that decides how smoothly the kiosk
   works every morning — one poor photo and that person gets asked to try again
   day after day.

   Anyone without a face on file can still clock in. Their punches are simply
   marked as unverified, and the Employees screen tells you who is outstanding.
4. **Kiosks → New code** to get a 6-digit pairing code.
5. On the tablet at that shop, open your https address followed by `/kiosk`,
   type the code, and press Pair. Do this once per tablet.
6. On the tablet, use the browser menu → **Add to Home Screen**. It then opens
   like an app.

### 4. Give staff access to their own record (optional)

Employees can look up their own hours, overtime and pay on their own phones, at
your https address followed by `/me`. They cannot see anyone else's, and they
cannot change anything.

For each person: **Employees → Portal → Set PIN**. A 6-digit PIN is suggested;
write it down and hand it over. They sign in with **their phone number and that
PIN**, so the person must have a phone number saved under Edit.

> This is **not** their kiosk PIN, and must not be made the same. The kiosk PIN
> gets typed in the open on a shared tablet all day and everyone in the queue
> sees it. If it also opened the portal, watching someone clock in would be
> enough to read their salary.

They can set their own password from the portal, which then replaces the PIN. If
they forget it, just set a new PIN — that clears the password.

To take access away: **Employees → Portal → Remove access**. It stops working
immediately, not at their next sign-in.

---

## Every day

**You do nothing.** Leave `start-windows.bat` and `start-tunnel.bat` running
while the shops are open.

**Staff** can check their own hours and pay any time at your https address
followed by `/me`, on their own phones. Nothing for you to do.

**Staff** tap their name → type their PIN → the camera takes a few photos while
the screen asks them to turn their head or blink → done. The screen shows a
ticket confirming the time. The same steps check them out.

The short prompt is what stops someone holding up a photo of a colleague. It
changes each time, so it cannot be filmed once and replayed.

If the face does not match the one on file, the kiosk asks them to try again
twice. After that the punch is still saved, marked for you to check — nobody is
ever left unable to clock in because a camera would not cooperate.

**If the laptop is off or the internet drops**, the kiosk still works. Punches
are saved on the tablet and upload by themselves once the laptop is back on.
An amber "N waiting to upload" badge shows when anything is queued.

---

## Every month

1. Go to **Payroll** and pick the month.
2. Look at anything marked **Check** — usually a missed punch out. Fix them on
   the **Attendance sheet** by clicking the square for that day.
3. Enter any **advances** under Leaves & advances. They are deducted from that
   month's payout automatically.
4. When the numbers look right, press **Finalise this month**. This freezes
   them, so later corrections cannot change a payslip you already handed out.
5. Print payslips (**All payslips**) or download the spreadsheet (**Excel**).

If you need to change a finalised month, press **Reopen**, fix it, and finalise
again — but any payslip already given out will no longer match.

**What staff see.** Until you press Finalise, anyone looking at `/me` sees the
month clearly marked "not final yet — this will keep changing". Once you
finalise, they see the frozen figures, exactly matching the payslip you hand
over, and they can download their own copy. Reopening a month puts it back to
showing an estimate.

---

## When something is wrong

**"The camera does not work" on a kiosk**
The tablet is on a plain `http://` address. It must be the `https://` tunnel
address. Browsers block the camera otherwise. This is the single most common
problem.

**A kiosk says "This device is not paired"**
The tunnel address changed (quick tunnels do this on every restart), or the
device was unpaired. Generate a new code under Kiosks and pair it again.

**Someone forgot to punch out**
The day shows as red with a "Check" mark. Open **Attendance sheet**, click
that square, and either add the missing punch or type the correct hours. You
must give a reason; it is kept in the record.

**Someone forgot their PIN**
Employees → Edit → set a new PIN. Five wrong tries locks that person out of
the kiosk for five minutes; nobody else is affected.

**A punch was recorded twice**
On the **Today** screen, press the small × on the wrong punch and give a
reason. It stays on record but stops counting.

**A payroll number looks wrong**
Payroll → **Why** next to that person. It shows the day-by-day working: hours,
overtime, which days counted as night duty, and how the bonus was decided.

**A punch is marked "Face?" in red**
The camera could not confirm the person was who the punch says. Open **Today**,
click the photo on that punch, and compare it with the employee it is recorded
against.

Nearly always it is innocent — poor light, a cap, someone half out of frame, or
their photos on file being too old. If it keeps happening to one person, replace
their photos: Employees → **Face** → remove the old ones and take new ones.

If the photo genuinely shows somebody else, that is a proxy punch. Void it on
the Today screen with a reason; the record is kept.

**Someone is asked to try again every morning**
Their reference photos are the problem, not them. Employees → **Face**, delete
what is there, and take two or three fresh ones in the light they actually
arrive in.

**Someone cannot sign in to `/me`**
Check three things, in this order. Do they have a phone number saved under
Employees → Edit? Is the number they are typing exactly the one saved, with no
spaces or country code? Are they using their **6-digit portal PIN** and not
their 4-digit kiosk PIN?

Five wrong tries locks that person out for five minutes. It locks only the
portal — they can still clock in at the kiosk perfectly normally.

If they have forgotten a password they set themselves, **Employees → Portal →
Set PIN** issues a fresh PIN and clears the old password.

**Someone says the pay shown on their phone is wrong**
If the month is not finalised, the screen says so — the figure moves every time
anyone punches, and the attendance bonus in particular can come and go right up
to the end of the month. Once you press Finalise it is fixed and matches their
payslip exactly.

If a finalised figure is genuinely wrong, Reopen the month, fix the attendance,
and finalise again.

**"Face checking is not set up on the server"**
The models were never downloaded. Run:

```
backend\.venv\Scripts\python.exe backend\scripts\fetch_face_models.py
```

then restart. Until then punches are saved but never confirmed.

---

## Backups

`start-windows.bat` makes a backup every time it starts, into the `backups`
folder, and keeps the last 30.

**Do this once:** set the backup folder to a cloud-synced folder (OneDrive,
Google Drive) so a copy exists off the laptop. Edit `.env` and add:

```
ATTENDANCE_BACKUP_DIR=C:\Users\YourName\OneDrive\AttendanceBackups
```

A backup that only exists on the laptop does not help if the laptop is what
fails.

To back up right now, run:

```
backend\.venv\Scripts\python.exe backend\scripts\backup.py
```

---

## Settings you can change

Edit the `.env` file in the main folder, then restart. All optional:

| Setting | Does what | Default |
|---|---|---|
| `ATTENDANCE_BUSINESS_NAME` | Name printed on payslips | Dry Cleaning Franchise |
| `ATTENDANCE_DAY_CUTOVER_HOUR` | When a new work day starts. `5` means a night shift ending at 3am still counts as the previous day's work | `5` |
| `ATTENDANCE_BACKUP_DIR` | Where backups are written | `backups` folder |
| `ATTENDANCE_PIN_MAX_ATTEMPTS` | Wrong PINs before a lockout | `5` |
| `ATTENDANCE_DEFAULT_WEEKLY_OFF_DOW` | Weekly off for new staff (0=Monday, 3=Thursday) | `3` |
| `ATTENDANCE_FACE_ENABLED` | Face checking on or off | `true` |
| `ATTENDANCE_FACE_STRICT` | Refuse a punch the face check fails, instead of saving it flagged. **Read the warning below first** | `false` |
| `ATTENDANCE_FACE_MATCH_THRESHOLD` | How alike the faces must be, 0 to 1. Higher is stricter | `0.363` |
| `ATTENDANCE_FACE_MAX_ATTEMPTS` | Tries at the kiosk before a punch is saved flagged | `3` |
| `ATTENDANCE_PORTAL_MAX_ATTEMPTS` | Wrong portal sign-ins before a 5-minute lockout | `5` |
| `ATTENDANCE_STAFF_SESSION_MAX_AGE_SECONDS` | How long staff stay signed in at `/me` | `28800` (8h) |

Pay rules — the 8h30m shift, the night thresholds, the bonus — are not settings.
They are in the code with tests against them, because getting them wrong costs
real money. Ask before changing them.

**About strict mode.** With `ATTENDANCE_FACE_STRICT=true`, anyone the camera
cannot confirm simply cannot clock in, and you will be adding their punches by
hand. A dry cleaner is steam, bad light and wet faces; expect that to happen to
honest staff. The default saves the punch and flags it instead, which catches
the same proxy attempts without anyone losing a day's pay over a camera.

**About the match threshold.** Raising it makes proxy punching harder and makes
the kiosk fussier with your own staff, in that order. Move it in steps of 0.02
and watch the Today board for a week before moving it again.

---

## A permanent web address

Worth 20 minutes so kiosks never need re-pairing. You need a domain name
(about ₹800/year) added to a free Cloudflare account.

```
cloudflared tunnel login
cloudflared tunnel create attendance
cloudflared tunnel route dns attendance attendance.yourdomain.com
cloudflared tunnel run --url http://127.0.0.1:8080 attendance
```

Then the kiosks always use `https://attendance.yourdomain.com/kiosk`.

**Also do this:** in the Cloudflare dashboard, put **Cloudflare Access** in
front of the `/admin` path with your email address. The admin console is then
unreachable by anyone else even though the address is public. The kiosk path
stays open, but it is useless without a paired device.

---

## Trying it out first

To fill the system with a sample month so you can click around before entering
real data:

```
backend\.venv\Scripts\python.exe backend\scripts\demo_data.py
```

To remove it again:

```
backend\.venv\Scripts\python.exe backend\scripts\demo_data.py --wipe
```

The sample staff all have codes starting with `DEMO`, so the wipe removes
exactly them and nothing of yours.
