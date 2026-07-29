# Start here

Plain-English setup. Follow it top to bottom. Every command is copy-paste.

There are three parts:

| Part | What you get | How long |
|---|---|---|
| **1** | The dashboard running on your computer | ~10 min |
| **2** | It on your iPhone home screen | ~2 min |
| **3** | The strap connected, so alarms can buzz | ~15 min, and it might not work |

Do them in order. Part 1 works on its own — if you stop after it, you still have
a working dashboard. Part 3 is the only fiddly bit, and it is honestly the one
that might fail; there is a whole section on what to do if it does.

---

# Part 1 — Get the dashboard running

## Step 1. Open a terminal in the project folder

**Mac:** press `Cmd+Space`, type "Terminal", press Enter. Then type `cd `
(with a space), drag the `whoop` folder from Finder into the window, press Enter.

**Windows:** open the `whoop` folder, click the address bar, type `powershell`,
press Enter.

To check you are in the right place, run:

```bash
ls
```

You should see `README.md`, `app`, `web`, `tools`. If not, you are in the wrong
folder — go back and try the drag trick again.

## Step 2. Set up Python (once)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

On Windows the middle line is `.venv\Scripts\activate` instead.

Your prompt should now start with `(.venv)`. **That matters** — it means you are
using this project's Python. If you close the terminal, you have to run the
`source .venv/bin/activate` line again next time. That is normal.

## Step 3. See it work with pretend data first

Before touching anything real, let's make sure the thing runs.

```bash
python tools/make_fixture.py
cp .env.example .env
```

Now open `.env` in any text editor (TextEdit, Notepad, VS Code) and change the
NOOP line to exactly this:

```
NOOP_DB_PATH=./data/fixture_noop.sqlite3
```

Save it. Then:

```bash
python -m app.main
```

Open **http://localhost:8765** in your browser.

You should see a dashboard with a recovery ring and some charts. **The numbers
are fake** — made up by a random number generator so you can see the layout.
Click through the tabs at the bottom: Today, Trends, Journal, Workouts, Alarms,
More.

Press `Ctrl+C` in the terminal to stop it.

> **If the page does not load:** check the terminal for a red error. The most
> common one is that you skipped `source .venv/bin/activate`.

## Step 4. Point it at your real NOOP data

**First: open NOOP and let it sync your strap at least once.** If NOOP has never
run, there is no database to find.

Then quit NOOP (fully — `Cmd+Q` on a Mac) and run:

```bash
python -m app.probe --find
```

This searches your computer for NOOP's database file. You should see something
like:

```
  [MATCH] /Users/you/Library/Application Support/NOOP/noop.sqlite3  (48.2 MB)
          daily metrics -> daily_metrics (17 fields)
```

**Copy that whole path.** Open `.env` again and replace the fixture line:

```
NOOP_DB_PATH=/Users/you/Library/Application Support/NOOP/noop.sqlite3
```

> If the path has spaces in it, that is fine — do **not** add quotes.

### Step 4b. Check it found the right things

```bash
python -m app.probe
```

This prints every table in NOOP's database and which ones it understood. What you
want to see:

```
--- resolution ---
  daily metrics    -> daily_metrics
  sleep sessions   -> sleep_sessions
  heart rate       -> hr_samples
```

**If it says `UNRESOLVED` for daily metrics**, the dashboard cannot read your
data. This is expected to be possible — NOOP's database layout is not published,
so the app guesses at it. Run:

```bash
python -m app.probe --sql > schema.txt
```

Send me `schema.txt` and I will teach it your exact layout. Do not try to fix
this yourself; it takes me two minutes.

## Step 5. Run it for real

```bash
python -m app.main
```

Open **http://localhost:8765** again. Now the numbers are yours.

Things that are normal and not bugs:

- **"Strap battery: not stored by NOOP"** — NOOP probably does not save this.
  Part 3 may fix it.
- **"Calibrating"** instead of a recovery score — NOOP needs about 4 nights
  before it will give one. It refuses to guess, and so do we.
- **Gaps in the charts** — days you did not wear the strap. They are shown as
  holes on purpose rather than being smoothed over.

**Leave this terminal window open.** Closing it stops the dashboard.

---

# Part 2 — Put it on your phone

## Step 6. Find your computer's address on your home network

**Mac:**
```bash
ipconfig getifaddr en0
```
**Windows:**
```bash
ipconfig
```
(look for "IPv4 Address" under your Wi-Fi adapter)

**Linux:**
```bash
hostname -I | awk '{print $1}'
```

You will get something like `192.168.1.42`. Write it down.

## Step 7. Open it on your iPhone

Your phone must be on the **same Wi-Fi** as your computer.

In Safari, go to:

```
http://192.168.1.42:8765
```

(your number, not mine)

Then tap the **Share** button (square with an arrow), scroll down, tap
**Add to Home Screen**, tap **Add**.

You now have a Strap icon on your home screen that opens full-screen with no
browser bars.

> **Two honest notes.**
>
> 1. It only works while your computer is on, awake, and running the dashboard.
>    There is no cloud — that is the point.
> 2. There is **no password**. Anyone else on your Wi-Fi could open it and see
>    your biometrics. On your home network that is probably fine. On a shared
>    flat or office Wi-Fi, think about it.

---

# Part 3 — Connect the strap

This is the part that makes alarms buzz. It is also the part that might not work,
and I would rather you know that going in than be confused later.

## Read this before you start

Your WHOOP strap can only be **properly paired with one device at a time**. Right
now that is almost certainly the WHOOP app on your phone. To pair it with your
computer, you have to take that away — and **you will probably have to re-pair
your phone afterwards.**

If that sounds annoying, you can stop here. Everything in Parts 1 and 2 keeps
working. You just will not have strap alarms.

There is one trap worth knowing: **heart rate keeps working even when pairing has
failed.** So "I can see my heart rate" does *not* mean it worked. Only a buzz
means it worked.

## Step 8. Install the Bluetooth library

```bash
pip install bleak
```

## Step 9. Let your terminal use Bluetooth (Mac only)

1. Apple menu → System Settings → Privacy & Security → **Bluetooth**
2. Turn on the switch next to **Terminal** (or iTerm, or VS Code — whichever you
   are using). If it is not in the list, skip ahead; it will appear the first
   time you run Step 11.
3. **Quit Terminal completely and reopen it.** macOS will not apply this to a
   window that is already open.

Then get back into the project:

```bash
cd /path/to/whoop
source .venv/bin/activate
```

## Step 10. Free the strap

In this order:

1. **On your phone:** force-quit the WHOOP app. Swipe up and flick it away — not
   just switching apps. Easiest and most reliable: **turn your phone's Bluetooth
   off entirely** while you do this.
2. **On your computer:** quit NOOP completely.

## Step 11. Find the strap

```bash
python tools/bench_strap.py --scan
```

Wear the strap, or tap its face firmly a few times to wake it up. You will get a
list:

```
  A1B2C3D4-5E6F-7890-ABCD-EF1234567890   -52 dBm  (unnamed)   <-- likely the strap
```

**A WHOOP strap usually shows up with no name.** That is why it says "(unnamed)".
The tool spots it by the heart-rate signature instead. On a Mac you get a long
code instead of a proper address — that is normal, Apple hides real addresses.

**Copy the address.** If nothing shows up, go back to Step 10 — something is
still holding the strap.

## Step 12. Look, without touching

```bash
python tools/bench_strap.py --address PASTE_THE_ADDRESS_HERE
```

This connects, has a look around, and disconnects. It writes nothing.

**Good result** — you see four green `[ OK ]` lines like:

```
  [ OK ]  CMD_TO_STRAP (write)         handle 0x0010
  [ OK ]  CMD_FROM_STRAP (notify)      handle 0x0012
```

**Bad result** — they all say `NOT PRESENT`. That means the strap connected but
is not *paired* with your computer. Jump to "If it does not work" below.

## Step 13. The real test — put the strap on your wrist first

Seriously, wear it. A buzz on a desk is easy to miss, and a missed buzz looks
exactly like a failure.

```bash
python tools/bench_strap.py --address PASTE_THE_ADDRESS_HERE --alarm 30
```

Now wait 30 seconds, wearing the strap, watching the clock.

**Did it buzz?**

- **YES** → Brilliant. Go to Step 14.
- **NO** → Not your fault, and not something you can fix by trying harder. Send
  me the `bench_report.json` file it created (it is in the same folder) and tell
  me whether you have a **WHOOP 4.0** or a **5.0/MG**. There is a different way
  to talk to the strap that I can switch to.

## Step 14. Tell the dashboard about the strap

Open `.env` and add the address:

```
STRAP_ADDRESS=A1B2C3D4-5E6F-7890-ABCD-EF1234567890
```

Restart the dashboard (`Ctrl+C`, then `python -m app.main` again).

Open the **Alarms** tab. The Strap card should say **"Ready — idle"** with a green
dot. Wear the strap and tap **Test buzz (20s)**.

If it buzzes: you are done. Set alarms and nap timers from your phone.

---

# If it does not work

## "Encryption is insufficient" / "bond refused" / everything says NOT PRESENT

The strap is still paired to something else. Do this:

1. Turn your phone's Bluetooth **off**.
2. Put the strap in pairing mode: **tap the sensor face firmly, repeatedly**,
   until the lights flash **blue**.
3. Pair from your computer:
   - **Mac:** System Settings → Bluetooth, and pair it there first.
   - **Linux:** `bluetoothctl`, then `scan on`, `pair <address>`,
     `trust <address>`, `connect <address>`.
   - **Windows:** Settings → Bluetooth & devices → Add device.
4. Run Step 12 again. It often takes two or three attempts.

## The strap does not show up in the scan at all

- Is it charged?
- Is it awake? Wear it or tap it.
- Is it within a metre of the computer?
- Did you *really* quit the WHOOP app and NOOP? A connected strap often stops
  advertising itself entirely.

## It worked once and now it does not

That is a known thing with this hardware from a computer, not something you did.
Note roughly how many attempts out of how many worked and tell me — it changes
how I build the retry behaviour.

## I want my phone's WHOOP app back

Open it and re-pair the strap normally. You may need to put the strap in pairing
mode again (tap until blue lights). The dashboard will then stop being able to
set alarms, but everything else keeps working.

---

# Everyday use, once it is set up

**To start it:**
```bash
cd /path/to/whoop
source .venv/bin/activate
python -m app.main
```

**To stop it:** `Ctrl+C` in that terminal.

**Where your data lives:** `data/dashboard.sqlite3` — everything you type
(journal, workouts, alarms). Back this file up; it is the only copy. NOOP's own
database is never written to.

**To get everything out:** More tab → Download CSV or JSON. Or just copy that
one file.

---

# A few things worth knowing

- **Nothing leaves your computer.** No accounts, no cloud, no analytics. The
  dashboard talks to your NOOP file and your strap, and nothing else.
- **None of the numbers are medical.** Recovery, HRV, sleep stages and strain are
  approximations that NOOP calculates on your machine from published methods.
  They are not clinically validated and they are not WHOOP's real scores.
- **The correlations screen is not proof.** If it says HRV is lower after
  alcohol, that is a pattern in your own data, not a cause. It says so every
  time, on purpose.
- **Cancelling an alarm that is already on the strap may not un-set it.** Nobody
  has worked out the "cancel" command yet, so the app tells you plainly that the
  strap might still buzz rather than pretending it stopped it.
