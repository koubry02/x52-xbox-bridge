# HOTAS Bridge

> Use a Saitek X52 / X52 Pro on an Xbox Series X|S — wirelessly, with one Orange Pi

No adapter, no second board, no soldering, no Dev Mode. The Pi reads your stick
over USB and streams it to the free **Oberon Remote** app on the Xbox, which
injects it as a controller. Ships with tuned layouts for Star Wars: Squadrons,
Ace Combat 7, Battlefield 6 and MSFS 2024 — switch between them with a button
on the throttle, and edit them from your phone.

```
X52  ──USB──►  Orange Pi Zero 3  ──WiFi──►  Oberon app on Xbox  ──►  game
```

## Installing / Getting started

You need an Orange Pi (or any Linux SBC) on the same network as the Xbox, with
the stick plugged into it over USB.

**1. Install the Xbox app.** Microsoft Store → search **Oberon Remote Input**
(by SamsidParty) → install. Retail mode is fine, no Dev Mode needed.

**2. Install on the Pi.**

```bash
git clone https://github.com/koubry02/x52-xbox-bridge
cd x52-xbox-bridge
sudo ./install.sh oberon
```

This installs the Python dependencies, sets up a systemd service that starts on
boot, turns off WiFi power save (see [Latency](#latency)), and offers to build
[libx52](https://github.com/nirenjan/libx52) for the throttle screen. It prints
the board's IP when it finishes.

**3. Connect.** On the Xbox open **Oberon Remote**, enter the Pi's IP, press
**Connect**, and fly.

The service starts in *menu mode* with the throttle frozen so it can't scroll
the dashboard. Press the **E button** on the throttle once you are in the game.

## Initial Configuration

**Calibrate to your stick** (recommended — the X52 and X52 Pro report their
axes under different codes):

```bash
sudo python3 /opt/hotas-bridge/oberon/calibrate.py --game squadrons
```

It detects your real axes, learns their ranges, skips any that jitter on their
own, lets you pick the menu-disable button, and applies the result.

**Check the link.** WiFi power save is the single biggest source of lag. The
installer disables it; this shows whether that took:

```bash
sudo /opt/hotas-bridge/tools/wifi_tune.sh --status
```

## Developing

Pure Python 3 plus `python3-evdev` and `websockets`. No build step.

```bash
git clone https://github.com/koubry02/x52-xbox-bridge
cd x52-xbox-bridge
sudo apt install python3-evdev iw
pip3 install --break-system-packages websockets
```

Run it straight from the checkout — reading the stick needs root:

```bash
sudo python3 oberon/oberon_server.py --menu
```

Useful while working on a mapping:

```bash
sudo python3 oberon/oberon_server.py --list     # which device is the stick?
sudo python3 oberon/oberon_server.py --probe    # print event names as you press
```

### Deploying

`install.sh` copies the checkout to `/opt/hotas-bridge` and restarts the
service. It backs the layouts folder up to `layouts-backup-<timestamp>.tgz`
first and protects layouts you created from deletion, so an upgrade cannot eat
a profile you built in the editor.

```bash
sudo ./install.sh oberon
sudo systemctl restart hotas-oberon
journalctl -u hotas-oberon -f
```

## Features

### Game layouts, switched from the stick

Mappings are JSON files in `layouts/`. Press the **layout button** on the
throttle (default `BTN_BASE4`, the lower half of the first T-rocker) to cycle
them:

```
DEFAULT → SQUADRNS → AC7 → BF6 → MSFS → (back to DEFAULT)
```

The switch is instant and safe: the axes publish neutral and any held buttons
are dropped on the changeover, so a switch can never flush a phantom input into
the game. The active layout is remembered across reboots.

Layouts **inherit** from one another, so a game file lists only what it changes
and a fix to the base mapping reaches every game at once. See
[Configuration](#configuration).

### A web app for your phone

Every board serves a status page and layout editor on port 8088:

```
http://<pi-ip>:8088
```

- **Status** — which game you are on, whether the Xbox is connected, live input
  latency and where it goes, and a menu-mode switch.
- **Layouts** — activate, edit, duplicate or delete.
- **Editor** — bindings as a plain list (`Main trigger → B`), one row per
  control, per mode-dial layer. Inherited rows are drawn dashed so the whole
  effective mapping is visible. Axes get sliders instead of number fields.

Saving the layout that is currently active re-applies it live — no restart, no
reconnect. Nothing is written until it validates.

> **No password.** Anyone on your LAN can edit layouts, toggle menu mode and
> briefly pause the controller output. That is deliberate — it is a tool for
> your own network. Do not port-forward it. `--web-port 0` turns it off.

### Mapping a control by pressing it

You do not need to know that the pinkie trigger is `BTN_PINKIE`. Tap **Add a
button** and the bridge listens:

1. Press the button (or sweep the axis) you want to map.
2. It tells you what you touched — *Pinkie trigger · BTN_PINKIE*.
3. Pick what it does on the Xbox from a grid of real controller buttons.

**Output to the Xbox is paused while it listens**, so pressing buttons to
identify them cannot fire a shot or scroll the dashboard, and anything held is
dropped when it stops.

Pick **more than one** target to press them together — that is how Ace Combat's
flares (LS + RS) and its high-G turn (LT + RT) are built. Axes also offer *use
it as buttons*, which walks you through "what should this do one way / the
other way" — the AC7 twist-as-yaw mapping.

### Menu mode

A throttle rests wherever you leave it. Mapped to a stick axis, a raised
throttle reads as a stick held off-centre, which scrolls menus and steers
radial wheels you are trying to use.

The service starts in menu mode, where the throttle is frozen to neutral while
the flight sticks stay live. Navigate menus, launch your match, then press the
**E button** to fly. The throttle screen shows `MENU:ON`/`OFF`, the button LEDs
go **amber** in menu mode and **green** when flying, and the web page has a
toggle. The freeze follows the layout — including both halves of a split
throttle.

### Throttle display and LEDs (X52 Pro, optional)

Needs [libx52](https://github.com/nirenjan/libx52), which the installer builds
for you. Without it everything else runs exactly the same.

```
192.168.1.69       ← Pi IP (enter this in the Oberon app)
XBOX:ON    12ms    ← connected + input latency: stick moved → Xbox has it
MENU:ON       AC7  ← menu mode + active game layout
```

| Shows | Means |
|---|---|
| `12ms` | your input reached the Xbox 12 ms after you moved the stick |
| `1.4s` | real lag |
| `STALL` | connected, but nothing has arrived for over 1.5s |
| `waiting` | no Oberon client connected |

The screen is deliberately quiet — every character costs a process spawn and a
USB round-trip — so the figure only moves when it moves meaningfully. The two
throttle rotaries adjust MFD and button-LED brightness live.

### The shipped layouts

![X52 Pro layout](x52_layout.png)

**Star Wars: Squadrons** — matches the game's default scheme, no rebinding
needed. This is also the base every other layout inherits.

| Xbox | HOTAS control | Function |
|---|---|---|
| Left stick Y | Throttle lever | Throttle (up = forward) |
| Left stick X | Stick left / right | Roll |
| Right stick Y | Stick fwd / back | Pitch |
| Right stick X | Twist | Yaw |
| RT | Main trigger | Fire |
| RB / LB | Thumb FIRE / pinkie | Right / left auxiliary |
| A / B / RS | C / B / A head buttons | Cycle targets / countermeasures / free look |
| LT / LS | D / i (throttle) | Select target ahead / boost |
| D-pad | POV hat | Power management |

**Ace Combat 7** — matches the default Xbox scheme (Standard *or* Expert).

| Xbox | HOTAS control | Function |
|---|---|---|
| Left stick | Main stick | Pitch + roll |
| RT / LT | **Throttle lever, split** | Forward = accelerate, back = brake |
| LB / RB | Twist left / right | Yaw (digital) |
| B / A | Trigger / thumb FIRE | Missile / machine gun |
| Y / X / RS | C / B / A head buttons | Change target / weapon / view |
| LS + RS | Pinkie trigger | **Flares** |
| LT + RT | D (throttle) | High-G turn |

**Battlefield 6** (aircraft) — matches the **default** Aircraft preset. You do
not need the in-game "Alternate" preset.

| Xbox | HOTAS control | Function |
|---|---|---|
| Left stick Y / X | Throttle lever / twist | Throttle / yaw |
| Right stick | Main stick | Pitch + roll |
| RT / LT | Trigger / pinkie | Fire / zoom |
| LS / RS | i (throttle) / A head | Afterburner / camera |
| Y / X / A | C / B head, D (throttle) | Switch weapon / reload / switch seat |
| D-pad | POV hat | Equipment slots |

**MSFS 2024** — throttle on the right trigger as an absolute 0–100% axis. In
game: *Controls → Throttle → bind THROTTLE AXIS to the right trigger*. This is
the one layout that uses the mode dial (M1 flight, M2 d-pad, M3 menus).

### Latency

The bridge measures your real input latency — from the moment the stick moved
to the moment the Xbox has it — and both halves are genuine measurements.

**On this board** comes from the kernel's own timestamp on the input event,
compared against the moment it goes on the wire. **Trip to the Xbox** is a real
round trip: the Oberon client polls strictly synchronously with no timer
([SocketClient.cs][oberon-src]), so the gap between our reply going out and the
next request landing *is* the round trip, and the input rides the outbound half.

[oberon-src]: https://github.com/SamsidParty/OberonRemote/blob/main/Oberon/SocketClient.cs

Because the client is synchronous, an input waits on average half a round trip
before it can be sent — so **total input latency is roughly one round trip, and
the link sets it**. The software barely matters:

| | cost |
|---|---|
| everything the bridge does per poll | well under 1 ms |
| WiFi round trip, clean 5 GHz | 1–3 ms |
| **WiFi with power save on** | **100–200 ms spikes** |

So, in the order that actually helps:

1. **Turn off WiFi power save** — `sudo tools/wifi_tune.sh`. The installer does
   this and keeps it off across reboots and reconnects.
2. **Use 5 GHz**, on a channel your neighbours are not using.
3. **Or wire the Pi to the router.** The Xbox can stay wireless — removing one
   of the two radios is most of the win.
4. **Give the Pi a static lease**, so a DHCP renewal cannot land mid-match.

Watch the **worst in the last 5s** figure on the web page as much as the
average: a single 200 ms spike is what you feel.

Bandwidth is a non-issue — the whole link runs at about 0.4 Mbps.

## Configuration

### Layout files

A layout is one JSON file in `layouts/`. It can inherit from another and
override only what differs.

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

So `"BTN_BASE": null` removes a button the parent bound, and
`"ABS_RZ": { "target": null, "button_low": "lb" }` turns an inherited analog
axis into a pair of digital buttons.

#### display_name / short_name

Type: `String`
Default: the file name

Full name for the web UI. `short_name` is at most 8 characters and appears on
the throttle screen.

#### order

Type: `Number`
Default: `50`

Position in the switch-button cycle. Ties break by name.

#### axes

Type: `Object` — evdev axis name → mapping

- `target` — `lx ly rx ry lt rt`, `split_lt_rt`, or `null`.
  `split_lt_rt` puts one lever across both triggers: forward half drives RT,
  back half LT, centre neither. That is Ace Combat's accelerate/brake.
- `invert`, `deadzone` (0–1), `expo` (0–1, softens the middle of the throw).
- `button_low` / `button_high` — press an Xbox button while the axis is past
  ∓`button_threshold` (default `0.5`), turning an axis into digital buttons.

#### buttons

Type: `Object` — `mode1`/`mode2`/`mode3` (the mode dial) → evdev name → target

A target is `a b x y lb rb ls rs view menu dpad_*`, `lt_button` / `rt_button`
for a trigger tap, `select_mode1..3`, or a **list** to press several at once
(`["ls", "rs"]`).

#### suspend_button / layout_switch_button

Type: `String` (evdev button name)
Default: `BTN_BASE2` / `BTN_BASE4`

The menu/throttle-freeze toggle, and the layout cycler.

#### throttle_axis / brightness_axis / led_brightness_axis / hat_to_dpad

Type: `String` / `String` / `String` / `Boolean`
Default: `ABS_Z` / `ABS_RY` / `ABS_RX` / `true`

Which axis the menu freeze applies to, the two rotaries (`""` disables; only
active while unmapped as a game axis), and whether the POV hat drives the
d-pad.

Find any code on your unit with `sudo python3 oberon/oberon_server.py --probe`,
or just press it in the web editor.

### Command line

| Flag | Default | What it does |
|---|---|---|
| `--layout NAME` | last active | Layout to start on |
| `--layouts-dir DIR` | `../layouts` | Where layouts live |
| `--config FILE` | — | Legacy single-config mode; layout switching off |
| `--menu` | off | Start with the throttle frozen (the service uses this) |
| `--web-port N` | `8088` | Web app port; `0` disables it |
| `--port N` | `26401` | Oberon protocol port |
| `--device PATH` | auto | `/dev/input/eventX` instead of matching by name |
| `--dscp` | off | Mark packets DSCP EF / priority 6 to claim a high-priority WiFi queue. Helps on some APs, hurts on others — measure it |
| `--list` / `--probe` | — | List devices / print live event names |
| `--verbose` | off | Log state on every poll. Do not leave it on while playing |

## Troubleshooting

**Menu cursor drifts / throttle scrolls the dashboard.** You are not in menu
mode. Press the E button, or use the toggle on the web page.

**Input feels laggy or stutters.** Check the link first:
`sudo /opt/hotas-bridge/tools/wifi_tune.sh --status`. If power save is on, run
the script without `--status`. See [Latency](#latency).

**A control does the wrong thing.** Your stick's codes may not match the
layout. Open the web editor, tap **Add a button**, and press the control — it
tells you what your unit calls it and you can bind it there.

**Pitch is backwards.** Flip **Invert** on that axis in the editor; it applies
live on save. Which way is correct depends on the game and your unit, so it is
deliberately not guessed.

**The switch button does nothing.** That layout has no
`layout_switch_button`, or the name does not match your unit. It is read from
the *active* layout, so if you switch into one that lacks it you will need the
web page to get back.

**Xbox will not connect.** Pi and Xbox must share a network. Re-enter the IP.
Check the service: `systemctl status hotas-oberon`.

**The throttle screen stays blank.** libx52 is not built, or `x52cli` is not on
PATH. Test with `sudo x52cli mfd 0 "TEST"`; if that errors, run
`sudo /opt/hotas-bridge/oberon/build_libx52.sh` and replug the stick.

**Watch what is happening:** `journalctl -u hotas-oberon -f` logs layout
switches, mode switches, menu toggles and connection state.

## Contributing

PRs and issues are welcome. Fork the repository and use a feature branch.

Layouts are the easiest place to help: a good mapping for a game that is not
here is a single small JSON file. Keep it inheriting from `default` so it stays
readable.

If you change the server, please keep the input path clean — nothing that
blocks (files, subprocesses, synchronous logging) belongs in the evdev reader
or the poll response. The commit history has some cautionary tales.

## Links

- Repository: https://github.com/koubry02/x52-xbox-bridge
- Issue tracker: https://github.com/koubry02/x52-xbox-bridge/issues
- Related projects:
  - [OberonRemote](https://github.com/SamsidParty/OberonRemote) — the Xbox-side
    app this talks to, and the source of the protocol
  - [libx52](https://github.com/nirenjan/libx52) — drives the X52 Pro's screen
    and LEDs

## Licensing

The code in this project is licensed under the [MIT license](LICENSE) — use it,
change it, ship it, sell it, no permission needed. Keep the copyright notice
and don't expect a warranty; that's the whole of it.

`proxy/`, `receiver/`, `sender/` and `overlays/` belong to an alternative
USB-hardware mode and are not used by the Oberon setup described here.
[usb-proxy](https://github.com/AristoChen/usb-proxy), which that mode builds
against, is cloned at install time and carries its own licence — nothing from
it is included in this repository.
