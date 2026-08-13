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
3. **Kiosks → New code** to get a 6-digit pairing code.
4. On the tablet at that shop, open your https address followed by `/kiosk`,
   type the code, and press Pair. Do this once per tablet.
5. On the tablet, use the browser menu → **Add to Home Screen**. It then opens
   like an app.

---

## Every day

**You do nothing.** Leave `start-windows.bat` and `start-tunnel.bat` running
while the shops are open.

**Staff** tap their name → type their PIN → the camera takes a photo → done.
The screen shows a ticket confirming the time. The same steps check them out.

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

Pay rules — the 8h30m shift, the night thresholds, the bonus — are not settings.
They are in the code with tests against them, because getting them wrong costs
real money. Ask before changing them.

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
