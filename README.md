# HOTAS Bridge

**Use a Saitek X52 / X52 Pro on an Xbox Series X|S — wirelessly, with one Orange Pi.**

No adapter, no second board, no soldering, no Dev Mode. The Pi reads your stick
and streams its inputs to the free **Oberon Remote** app on the Xbox, which
injects them as a controller.

```
X52  ──USB──►  Orange Pi Zero 3  ──WiFi──►  Oberon app on Xbox  ──►  game
```

Ships with tuned layouts for **Star Wars: Squadrons**, **Ace Combat 7**,
**Battlefield 6** and **MSFS 2024** — switch between them with a button on the
throttle, and edit them from a browser on your network.

---

## What's where

```
hotas-bridge/
├── install.sh                     one-shot installer (sudo ./install.sh oberon)
├── x52_layout.png                 the layout diagram below
├── layouts/                       ← game layouts (JSON, with inheritance)
│   ├── default.json               the base every game inherits from
│   ├── squadrons.json             Star Wars: Squadrons
│   ├── ac7.json                   Ace Combat 7: Skies Unknown
│   ├── bf6.json                   Battlefield 6 (aircraft)
│   └── msfs2024.json              MS Flight Simulator 2024
├── tools/
│   └── wifi_tune.sh               kills WiFi power save (the latency fix)
└── oberon/
    ├── oberon_server.py           the server: reads X52, talks to Oberon
    ├── layouts.py                 layout loading, inheritance, validation
    ├── webapp.py                  the LAN status page + layout editor
    ├── web/index.html             its front end
    ├── mfd.py                     throttle-screen + LED status (libx52)
    ├── build_libx52.sh            builds libx52 from source (Armbian/arm64)
    ├── calibrate.py               calibration wizard
    └── hotas-oberon.service       starts the server on boot (menu mode)
```

The `proxy/`, `receiver/` and `sender/` folders belong to an alternative
USB-hardware mode and aren't needed for Oberon.

---

## Quick start

**1. Install the Xbox app.** Microsoft Store → search **Oberon Remote Input**
(by SamsidParty) → install. Retail mode is fine, no Dev Mode needed.

**2. Install on the Pi.** Copy this folder to the Orange Pi, then:

```bash
cd hotas-bridge
sudo ./install.sh oberon
```

It installs dependencies and a service that starts on boot. It also offers to
set up the optional throttle-screen display (see below).

**3. Calibrate to your stick** (recommended — X52 and X52 Pro report axes under
different codes):

```bash
sudo python3 /opt/hotas-bridge/oberon/calibrate.py --game squadrons
```

Follow the prompts. It detects your real axes, learns their ranges, skips any
that jitter on their own, lets you pick the menu-disable button, and applies the
result automatically.

**4. Connect.** On the Xbox open **Oberon Remote**, enter the Pi's IP (printed
on startup, shown on the throttle screen, and on the web page), press
**Connect**, and fly.

---

## Switching games

Press the **layout button** on the throttle — by default `BTN_BASE4`, the lower
half of the first T-rocker — to cycle through the installed layouts:

```
DEFAULT → SQUADRNS → AC7 → BF6 → MSFS → (back to DEFAULT)
```

The switch is instant and safe: the axes publish neutral and any held buttons
are dropped on the changeover, so a layout switch can never flush a phantom
input into the game.

**Which one am I on?** The active game shows in three places:

- the **throttle screen** (X52 Pro), on the bottom line next to the menu state:

  ```
  192.168.1.69       ← Pi IP (enter this in the Oberon app)
  XBOX:ON    45ms    ← Oberon connected + poll ping
  MENU:ON       AC7  ← menu mode  |  active game layout
  ```

- the **web page** at `http://<pi-ip>:8088`
- the service log: `journalctl -u hotas-oberon -f`

You can also switch from the web page, or start on a given layout:

```bash
sudo python3 /opt/hotas-bridge/oberon/oberon_server.py --layout ac7
```

The choice is remembered across restarts (in `layouts/.active`).

---

## The web app

Every board runs a status page and layout editor on **port 8088**, reachable from
any browser on the same network — phone included, which is the point: you sit at
the stick with your phone and map it.

```
http://<pi-ip>:8088
```

Three tabs:

- **Status** — which game you're on (big, because that's the question you
  actually have), Oberon connected or not, poll ping, uptime, and a menu-mode
  switch.
- **Layouts** — activate, edit, duplicate or delete any layout.
- **Editor** — bindings as a plain list you can read at a glance
  (`Main trigger → B`), one row per control, per mode-dial layer. Inherited rows
  are drawn dashed, so you can always see the whole effective mapping and what
  came from where. Axes get sliders for deadzone and expo rather than typing
  numbers. There's a raw-JSON view and a resolved preview under Advanced.

Saving the *active* layout re-applies it live — no restart, no reconnect. Nothing
is written until it validates.

### Mapping by pressing the thing

You don't have to know that the pinkie trigger is `BTN_PINKIE`. Tap **Add a
button**, and the bridge listens:

1. Press the button (or sweep the axis) you want to map.
2. It tells you what you touched — *Pinkie trigger · BTN_PINKIE*.
3. Pick what it should do on the Xbox from a grid of real controller buttons.

**Output to the Xbox is paused while it's listening**, so pressing buttons to
identify them can't fire a shot or scroll the dashboard. Anything you were
holding is dropped when it stops, so nothing leaks through afterwards.

Pick **more than one** target to press them together — that's how Ace Combat's
flares (LS + RS) and its high-G turn (LT + RT) are built.

Axes offer one extra path: **use it as buttons**, which walks you through "what
should this do one way / the other way". That's the AC7 twist-as-yaw mapping,
and it works for any axis you'd rather have behave like a bumper pair.

The same listen-and-learn flow sets the menu-freeze and layout-switch buttons
under **Setup → Special buttons**.

> **No password.** Anyone on your LAN can edit layouts, toggle menu mode and
> briefly pause the controller output. That's deliberate — it's a tool for your
> own network. Don't port-forward it. To turn it off entirely, add
> `--web-port 0` to the service's `ExecStart`.

---

## Layouts and inheritance

A layout is one JSON file in `layouts/`. It can **inherit** from another layout
(nearly always `default`) and override only what differs — so a game layout stays
short and readable, and a fix to the base mapping reaches every game at once.

```json
{
  "display_name": "Ace Combat 7",
  "short_name": "AC7",
  "inherits": "default",
  "order": 20,
  "axes":    { "ABS_Y": { "target": "ly" } },
  "buttons": { "mode1": { "BTN_TRIGGER": "b" } }
}
```

Merge rules, child over parent:

| In the child | Result |
|---|---|
| a value | replaces the inherited one |
| an object | merges key-by-key, recursively |
| `null` | **deletes** the inherited key |

So `"BTN_BASE": null` in a mode removes a button the parent had bound, and
`"ABS_RZ": { "target": null, "button_low": "lb" }` turns an inherited analog
axis into a pair of digital buttons.

**Keys:**

- **`display_name`** — full name, shown in the web UI.
- **`short_name`** — up to 8 characters, shown on the throttle screen.
- **`order`** — position in the switch-button cycle (ties break by name).
- **`axes`** — evdev axis name → mapping:
  - `target` — `lx ly rx ry lt rt`, or `split_lt_rt` (see below), or `null`.
  - `invert`, `deadzone` (0–1), `expo` (0–1, softens the middle of the throw).
  - `button_low` / `button_high` — press an Xbox button while the axis is past
    ∓`button_threshold` (default 0.5). Turns an analog axis into digital
    buttons — e.g. twist → LB/RB for Ace Combat's yaw.
- **`buttons`** — `mode1`/`mode2`/`mode3` (the mode dial) → evdev button name →
  target. A target is `a b x y lb rb ls rs view menu dpad_*`, `lt_button` /
  `rt_button` for a trigger tap, `select_mode1..3`, or a **list** to press
  several at once (`["ls", "rs"]` = Ace Combat's flares).
- **`suspend_button`** — freezes the throttle for menus (see below).
- **`layout_switch_button`** — cycles layouts.
- **`throttle_axis`** — which axis the menu freeze applies to (default `ABS_Z`).
- **`brightness_axis` / `led_brightness_axis`** — throttle rotaries for MFD and
  LED brightness (`""` disables). Only active while unmapped as a game axis.
- **`hat_to_dpad`** — POV hat drives the d-pad when true.

**`split_lt_rt`** splits one lever across both triggers: forward half drives RT,
back half drives LT, centre is neither. That's how Ace Combat's
accelerate/brake-on-one-throttle works.

Find any button or axis code on your unit with:

```bash
sudo python3 /opt/hotas-bridge/oberon/oberon_server.py --probe
```

---

## The layouts

### Star Wars: Squadrons

Matches the game's **default** control scheme — set the scheme to Default and
fly, no in-game rebinding. This is also the base layout everything inherits.

![X52 Pro layout](x52_layout.png)

| Xbox | HOTAS control | Squadrons function |
|------|---------------|--------------------|
| Left stick Y | Throttle lever | Throttle (up = forward) |
| Left stick X | Stick left / right | Roll |
| Right stick Y | Stick fwd / back | Pitch |
| Right stick X | Twist | Yaw |
| RT | Main trigger | Fire |
| RB | Thumb FIRE button | Fire Right Auxiliary |
| LB | Pinkie trigger | Fire Left Auxiliary |
| A | C head button | Cycle Targets |
| B | B head button | Deploy Countermeasures |
| RS | A head button | Free Look |
| LT | D button (throttle) | Select Target Ahead |
| LS | I button (throttle) | Boost |
| Menu | T1 rocker | Menu |
| View | T2 rocker | Show Loadout |
| D-pad | POV hat | Power: up=weapon, left=engine, down=balance, right=shield |

The throttle sits on the **left stick**, where it can never fire a weapon.

### Ace Combat 7: Skies Unknown

Matches AC7's default Xbox scheme (Standard *or* Expert — they share the same
buttons; only the in-game stick behaviour differs). No rebinding needed.

| Xbox | HOTAS control | AC7 function |
|------|---------------|--------------|
| Left stick | Main stick | Pitch + roll |
| RT / LT | **Throttle lever, split** | Forward = accelerate, back = brake |
| LB / RB | Twist left / right | Yaw left / right (digital) |
| B | Main trigger | Fire missile / special weapon |
| A | Thumb FIRE button | Machine gun |
| Y | C head button | Change target |
| X | B head button | Change weapon |
| RS | A head button | Change view |
| LS + RS | Pinkie trigger | **Flares** (both sticks at once) |
| LT + RT | D button (throttle) | High-G turn (both triggers) |
| D-pad | POV hat | Radio replies |
| Menu / View | T1 rocker | Pause / map |

The split throttle is the interesting bit: AC7 puts accelerate and brake on
opposite triggers, so one physical lever drives both — push past centre to
accelerate, pull back past centre to brake.

### Battlefield 6 (aircraft)

Matches BF6's **default** Aircraft controller preset. You do *not* need the
in-game "Alternate" preset — that exists to work around a gamepad having too few
axes, and the HOTAS doesn't have that problem. Leave it on Default.

| Xbox | HOTAS control | BF6 function |
|------|---------------|--------------|
| Left stick Y | Throttle lever | Throttle |
| Left stick X | Twist | Yaw |
| Right stick | Main stick | Pitch + roll (forward = nose down) |
| RT | Main trigger | Fire |
| LT | Pinkie trigger | Zoom |
| LS | I button (throttle) | Afterburner |
| RS | A head button | Camera / view |
| Y | C head button | Switch weapon |
| X | B head button | Reload (hold to exit) |
| A | D button (throttle) | Switch seat |
| B | Thumb FIRE button | B |
| D-pad | POV hat | Equipment slots (countermeasures) |
| Menu / View | T1 rocker | Pause / scoreboard |

### MS Flight Simulator 2024

Throttle on the **right trigger** as an absolute 0–100% axis — the closest thing
to real HOTAS behaviour on a console. In-game: *Controls → Throttle → bind
THROTTLE AXIS to the right trigger*; do **not** use the incremental
throttle-up/down bindings, which is what makes a lever feel wrong.

This is also the one layout that uses the **mode dial**: M1 flight buttons,
M2 a d-pad/systems layer, M3 menus.

---

## Latency — measured, not estimated

The bridge measures your real input latency: **from the moment the stick
physically moved to the moment the Xbox has it.** Both halves are genuine
measurements, not guesses.

**On this board** comes from the kernel's own timestamp on the input event —
the instant the hardware reported the movement, before any of this code ran —
compared against the moment that input goes out on the wire. It's mostly the
wait for the Xbox to ask for the next packet.

**Trip to the Xbox** is a real round trip. The Oberon client's loop is strictly
synchronous — send, wait for the reply, inject, send again, with no timer at
all ([SocketClient.cs][oberon-src]) — so the gap between our reply going out
and the next request landing *is* the round trip. The input rides the outbound
half of it.

[oberon-src]: https://github.com/SamsidParty/OberonRemote/blob/main/Oberon/SocketClient.cs

Because the client is synchronous, an input waits on average half a round trip
before it can even be sent — so **total input latency lands at roughly one
round trip**, and the link is what sets it. Which is why the software side
barely matters:

| | cost |
|---|---|
| build the controller packet | ~18 µs |
| read the shared state | ~3 µs |
| **everything the bridge does** | **well under 1 ms** |
| WiFi round trip, clean 5 GHz | 1–3 ms |
| **WiFi with power save on** | **100–200 ms spikes** |

Power save parks the radio between beacons. That's invisible on a web page and
ruinous for a flight stick, and it's usually **on by default**. The installer
now turns it off and keeps it off across reboots and reconnects. To check or
re-apply by hand:

```bash
sudo /opt/hotas-bridge/tools/wifi_tune.sh --status   # what's the link doing?
sudo /opt/hotas-bridge/tools/wifi_tune.sh            # fix it, and on every boot
```

After that, in the order that actually helps:

1. **Put the Pi on 5 GHz**, on a channel your neighbours aren't using. 2.4 GHz
   shares the air with every doorbell and microwave nearby.
2. **Or wire the Pi to the router.** The Xbox can stay wireless — removing one
   of the two radios is most of the win.
3. **Give the Pi a static lease**, so a DHCP renewal can't land mid-match.

You can see all of this working on the throttle screen and the web page. Change
one thing at a time and watch the numbers move — and watch the **worst in the
last 5s** figure as much as the average, because a single 200 ms spike mid-turn
is what you actually feel.

A note on the Python side: the interpreter's default GIL switch interval is
5 ms, which is longer than this program's whole latency budget — one thread
doing a burst of work could delay the stick or the reply by more than the
network does. The server drops it to 0.5 ms at startup, which measured an 8×
improvement in how long a thread waits to be scheduled under load. The
throttle-screen thread also runs at a lower priority than the input path, so
its display updates can never outrank your controls.

**Bandwidth is a non-issue** — the whole link runs at about 0.4 Mbps. There is
nothing to save there, so the bridge spends its effort on *consistency*
instead: packets are marked DSCP EF and priority 6 so WiFi puts them in a
high-priority queue rather than behind someone's download, and WebSocket
compression is off because 9 bytes and 100 bytes occupy the same radio frame.

---

## Menu mode (why the throttle doesn't wreck menus)

A throttle rests wherever you leave it — it never re-centers. Mapped to a stick
axis, a raised throttle reads as a stick held off-center, which scrolls menus
and steers radial wheels you're trying to use.

**The fix is built in.** The service starts in *menu mode*, where the throttle
is frozen to neutral while the flight sticks stay live. Navigate menus, launch
your match, then press the **E button** once to unfreeze and fly. Press it again
whenever you're back in a menu. The throttle screen shows `MENU:ON`/`OFF`, the
web page shows it with a toggle, and the button LEDs turn **amber** in menu mode,
**green** when flying.

The freeze follows the layout: it always freezes whatever that layout put the
throttle lever on, including both halves of a `split_lt_rt` throttle.

---

## Throttle display & LEDs (X52 Pro, optional)

The X52 Pro's throttle screen can show live bridge status, and the button LEDs
can reflect state. This needs the **libx52** driver
(https://github.com/nirenjan/libx52), which the installer sets up for you.

Before you connect, the top line is the IP you need to type into the Oberon
app. Once you're connected that line has done its job, so it switches to
showing where your input latency goes:

```
192.168.1.69       ← Pi IP — until you connect
XBOX:--  waiting

IN5.6ms LNK 12ms   ← after connecting: on this board / round trip
XBOX:ON    12ms    ← stick moved → Xbox has it
MENU:ON       AC7  ← menu mode + active game layout
```

The latency line tells the truth when the link degrades — that's the point:

| Shows | Means |
|---|---|
| `12ms` | your input reached the Xbox 12 ms after you moved the stick |
| `1.4s` | real lag |
| `STALL` | connected, but nothing has arrived for over 1.5s |
| `waiting` | no Oberon client connected |

The web page shows the same figures with the split broken out, plus the worst
reading in the last five seconds — which is what actually ruins a turn.

- **Button LEDs:** green while flying, amber in menu mode. (The X52 Pro's FIRE
  and THROTTLE LEDs are on/off only — hardware limitation — so those don't
  change color; the A/B/D/E/T LEDs do.)
- **Brightness knobs:** the two throttle rotaries adjust MFD brightness (upper)
  and button-LED brightness (lower), live.
- The MFD backlight is green by hardware; you can't change its color, only its
  brightness.

**On Armbian / Debian / arm64** the Ubuntu PPA has no package, so the installer
builds libx52 from source automatically. To do it by hand:

```bash
sudo /opt/hotas-bridge/oberon/build_libx52.sh
sudo systemctl restart hotas-oberon
```

> Don't add the Ubuntu PPA to a Debian/Armbian `sources.list` — it can break
> apt. Build from source instead (the script above does exactly that).

Everything here is optional: if libx52 isn't installed, the bridge runs exactly
the same without the display, LEDs, or brightness knobs — the web page still
shows all the same status.

---

## Making your own layout

Easiest path: open `http://<pi-ip>:8088` on your phone, hit **Duplicate** on
whichever layout is closest, then map controls by pressing them (see above).
**Save**. If you're editing the layout that's currently active, it re-applies
immediately — no restart.

By hand, drop a file in `layouts/`:

```json
{
  "display_name": "Elite Dangerous",
  "short_name": "ELITE",
  "inherits": "default",
  "order": 50,
  "axes": { "ABS_Z": { "target": "rt" } },
  "buttons": { "mode1": { "BTN_TRIGGER": "a" } }
}
```

Then restart, or hit the switch button until it comes round.

**Keep your changes in your own layout, not in the shipped ones.** `install.sh`
refreshes the layouts that ship with this repo on every upgrade (it backs the
whole folder up to `layouts-backup-<timestamp>.tgz` first, and never touches
layouts you created).

---

## Troubleshooting

**Menu cursor drifts / throttle scrolls the dashboard.** You're not in menu
mode. Start the service (it boots in menu mode), press the E button, or hit the
toggle on the web page.

**A control does the wrong thing.** Your stick's codes may not match the layout.
Open the web editor, tap **Add a button**, and press the control — it'll tell you
what your unit actually calls it, and you can bind it right there.

**Pitch is backwards.** Open the axis in the web editor and flip **Invert** — it
applies live on save. Which way is "correct" depends on the game and on your
unit, so it's deliberately not guessed for you; it's one tap either way.

**I don't know which layout I'm on.** Look at the throttle screen's bottom line,
or the web page, or `journalctl -u hotas-oberon -f` (it logs every switch).

**The switch button does nothing.** That layout has no `layout_switch_button`,
or the name doesn't match your unit. Check it with `--probe`, then set it in the
web editor. Note the button is read from the *active* layout, so if you switch
into a layout that lacks it, you'll have to switch back from the web page.

**Web page won't load.** Check the service is up (`systemctl status
hotas-oberon`) and that you're on the same network. The startup log prints the
exact URL. Port 8088 by default.

**Xbox won't connect.** Pi and Xbox must share a network. Re-enter the Pi's IP
in Oberon. Check the server: `systemctl status hotas-oberon`.

**Input feels laggy or stutters.** Check the link first — it's almost always
WiFi power save: `sudo /opt/hotas-bridge/tools/wifi_tune.sh --status`. If it
says power save is on, run the same script without `--status`. See
[Latency](#latency--where-it-actually-comes-from) for what to try next.

**Watch what's happening live:** `journalctl -u hotas-oberon -f` shows layout
switches, mode switches, menu toggles, and connection state. Add `--verbose` to
a manual run to see axis values on every poll.

**The display stays blank.** libx52 isn't built or `x52cli` isn't on PATH. Test
directly: `sudo x52cli mfd 0 "TEST"`. If that errors, run `build_libx52.sh` and
replug the stick to apply the udev rule.

---

## Running one fixed config (the old way)

Layouts replaced the single `sender/sender_config.json`, but that path still
works — pass `--config` and layout switching turns off:

```bash
sudo python3 /opt/hotas-bridge/oberon/oberon_server.py \
    --config /opt/hotas-bridge/sender/sender_config.json --menu
```

This is what `calibrate.py` writes to, so it's a good way to run a freshly
calibrated config before folding your calibration into a layout.
