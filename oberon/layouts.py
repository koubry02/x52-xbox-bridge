"""
layouts.py — game-layout store with inheritance for HOTAS Bridge.

A *layout* is one JSON file in the layouts/ directory that describes how the
X52 maps to the virtual Xbox controller for one game. Layouts can inherit
from each other with the "inherits" key (usually from "default"), so a game
layout only lists what it changes:

    {
      "name": "ac7",
      "display_name": "Ace Combat 7",
      "short_name": "AC7",              # shown on the throttle MFD (<= 8 chars)
      "inherits": "default",
      "order": 20,                      # position in the cycle order
      "axes":    { ... only the differences ... },
      "buttons": { ... only the differences ... }
    }

Merge semantics (deep merge, applied child-over-parent):
  * dict values merge recursively
  * a value of null DELETES the inherited key
  * any other value replaces the inherited one

Axis entry schema (all keys optional except target-or-button):
  target            "lx ly rx ry lt rt" or "split_lt_rt"
                    (split_lt_rt: forward half of the axis drives RT, the
                     back half drives LT — e.g. an AC7 throttle where forward
                     = accelerate and back = brake)
  invert            bool
  deadzone          0..1
  expo              0..1
  button_low        Xbox button pressed while the axis is below -threshold
  button_high       Xbox button pressed while the axis is above +threshold
  button_threshold  0..1, default 0.5 (e.g. twist -> lb / rb for digital yaw)

Button targets: a b x y lb rb ls rs view menu dpad_up dpad_down dpad_left
dpad_right lt_button rt_button select_mode1..3 — or a LIST of targets to
press several at once (e.g. ["ls", "rs"] = AC7 flares on one button).
"""

import json
import os
import re

AXIS_TARGETS   = ("lx", "ly", "rx", "ry", "lt", "rt")
SPLIT_TARGET   = "split_lt_rt"
BUTTON_TARGETS = (
    "a", "b", "x", "y", "lb", "rb", "ls", "rs", "menu", "view",
    "dpad_up", "dpad_down", "dpad_left", "dpad_right",
    "lt_button", "rt_button",
)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,31}$")

# Keys a layout file may contain at the top level (unknown keys are kept but
# flagged by validate() so typos don't fail silently).
KNOWN_TOP_KEYS = {
    "name", "display_name", "short_name", "inherits", "order", "_readme",
    "device_match", "hat_to_dpad", "axes", "buttons", "suspend_button",
    "layout_switch_button", "throttle_axis", "brightness_axis",
    "led_brightness_axis", "host", "port", "web_port",
}


def default_dir():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "layouts"))


def _path(layouts_dir, name):
    return os.path.join(layouts_dir, name + ".json")


def deep_merge(base, override):
    """Child-over-parent merge. dicts recurse, null deletes, rest replaces."""
    out = dict(base)
    for k, v in override.items():
        if v is None:
            out.pop(k, None)
        elif isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def list_layouts(layouts_dir):
    """Names of all layouts on disk, unresolved."""
    if not os.path.isdir(layouts_dir):
        return []
    names = []
    for fn in sorted(os.listdir(layouts_dir)):
        if fn.endswith(".json") and not fn.startswith((".", "_")):
            names.append(fn[:-5])
    return names


def load_raw(layouts_dir, name):
    """One layout file as written, without inheritance applied."""
    with open(_path(layouts_dir, name)) as f:
        return json.load(f)


def resolve(layouts_dir, name, _seen=None):
    """Layout with its whole inheritance chain merged in."""
    seen = _seen or []
    if name in seen:
        raise ValueError(f"inheritance cycle: {' -> '.join(seen + [name])}")
    raw = load_raw(layouts_dir, name)
    parent = raw.get("inherits")
    if parent:
        base = resolve(layouts_dir, parent, seen + [name])
        merged = deep_merge(base, {k: v for k, v in raw.items() if k != "inherits"})
        merged["inherits"] = parent
    else:
        merged = raw
    merged["name"] = name
    return merged


def cycle_order(layouts_dir):
    """Layout names in switch-button cycle order: by "order" key, then name."""
    entries = []
    for name in list_layouts(layouts_dir):
        try:
            raw = load_raw(layouts_dir, name)
            order = raw.get("order", 50)
        except (OSError, ValueError):
            order = 50
        entries.append((order if isinstance(order, (int, float)) else 50, name))
    entries.sort()
    return [n for _, n in entries]


def _valid_button_target(t):
    if isinstance(t, list):
        return bool(t) and all(_valid_button_target(x) for x in t)
    return isinstance(t, str) and (
        t in BUTTON_TARGETS or t.startswith("select_mode"))


def validate(layouts_dir, name, raw):
    """Validate one layout dict (as written). Returns a list of problems;
    empty list = OK. Also resolves inheritance to catch cycles / bad parents."""
    problems = []
    if not NAME_RE.match(name or ""):
        problems.append(f"bad layout name '{name}' (a-z 0-9 _ - only)")
    if not isinstance(raw, dict):
        return problems + ["layout must be a JSON object"]

    for k in raw:
        if k not in KNOWN_TOP_KEYS:
            problems.append(f"unknown top-level key '{k}'")

    parent = raw.get("inherits")
    if parent is not None:
        if not isinstance(parent, str) or parent not in list_layouts(layouts_dir):
            problems.append(f"inherits '{parent}': no such layout")
        else:
            # resolve against the on-disk parents, with this raw as the child
            try:
                base = resolve(layouts_dir, parent, [name])
            except (ValueError, OSError, json.JSONDecodeError) as e:
                problems.append(f"inheritance error: {e}")
                base = {}
            merged = deep_merge(base, {k: v for k, v in raw.items() if k != "inherits"})
            problems += _validate_body(merged)
            return problems
    problems += _validate_body(raw)
    return problems


def _validate_body(cfg):
    problems = []
    axes = cfg.get("axes", {})
    if not isinstance(axes, dict):
        problems.append("'axes' must be an object")
        axes = {}
    for axis, acfg in axes.items():
        if not isinstance(acfg, dict):
            problems.append(f"axis {axis}: must be an object")
            continue
        tgt = acfg.get("target")
        has_btn = acfg.get("button_low") or acfg.get("button_high")
        if tgt is not None and tgt not in AXIS_TARGETS and tgt != SPLIT_TARGET:
            problems.append(f"axis {axis}: unknown target '{tgt}'")
        if tgt is None and not has_btn:
            problems.append(f"axis {axis}: no target and no button_low/high "
                            f"(delete the axis or give it a job)")
        for side in ("button_low", "button_high"):
            bt = acfg.get(side)
            if bt is not None and not _valid_button_target(bt):
                problems.append(f"axis {axis}: bad {side} '{bt}'")
        thr = acfg.get("button_threshold")
        if thr is not None and not (isinstance(thr, (int, float)) and 0 < thr < 1):
            problems.append(f"axis {axis}: button_threshold must be 0..1")

    buttons = cfg.get("buttons", {})
    if not isinstance(buttons, dict):
        problems.append("'buttons' must be an object")
        buttons = {}
    for mode_name, bmap in buttons.items():
        if not re.match(r"^mode[0-9]$", mode_name):
            problems.append(f"buttons: bad mode key '{mode_name}' (use mode1..mode3)")
            continue
        if not isinstance(bmap, dict):
            problems.append(f"{mode_name}: must be an object")
            continue
        for bname, target in bmap.items():
            if not _valid_button_target(target):
                problems.append(f"{mode_name}.{bname}: bad target '{target}'")

    sn = cfg.get("short_name")
    if sn is not None and (not isinstance(sn, str) or len(sn) > 8):
        problems.append("short_name must be a string of at most 8 chars "
                        "(it goes on the 16-char MFD line)")
    return problems


def save(layouts_dir, name, raw):
    """Validate and write one layout file. Raises ValueError with the
    problem list on validation failure."""
    problems = validate(layouts_dir, name, raw)
    if problems:
        raise ValueError("; ".join(problems))
    os.makedirs(layouts_dir, exist_ok=True)
    tmp = _path(layouts_dir, name) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")
    os.replace(tmp, _path(layouts_dir, name))


def delete(layouts_dir, name):
    if name == "default":
        raise ValueError("the default layout cannot be deleted")
    for other in list_layouts(layouts_dir):
        if other == name:
            continue
        try:
            if load_raw(layouts_dir, other).get("inherits") == name:
                raise ValueError(f"layout '{other}' inherits from '{name}'")
        except (OSError, json.JSONDecodeError):
            pass
    os.remove(_path(layouts_dir, name))


# ── active-layout persistence ────────────────────────────────────────────────

def _active_path(layouts_dir):
    return os.path.join(layouts_dir, ".active")


def read_active(layouts_dir):
    try:
        with open(_active_path(layouts_dir)) as f:
            name = f.read().strip()
        if name in list_layouts(layouts_dir):
            return name
    except OSError:
        pass
    return "default" if "default" in list_layouts(layouts_dir) else None


def write_active(layouts_dir, name):
    try:
        with open(_active_path(layouts_dir), "w") as f:
            f.write(name + "\n")
    except OSError:
        pass    # persistence is best-effort; switching still works this boot
