#!/usr/bin/env python3
# Copyright (c) 2026 Jeremy Frank Jackson, BEATEK Holdings, LLC
# Licensed under the MIT License. See the LICENSE file.
"""
bea_dualsense_tune.py - DualSense drift diagnosis and stick calibration.

What it does
  calibrate  Hands off the controller for a few seconds. Measures each stick's
             resting offset, noise and slow creep, tells you what kind of fault
             it looks like, and saves a correction profile (JSON).
  live       Shows raw vs corrected stick values using the saved profile.
  log        Writes every sample to CSV (for your own analysis, or to catch ghost input).
  watch      Prints raw resting stick readings every few seconds (hands off) so
             you can see whether anything changes when a game or app starts.
  buttons    Prints button indices as you press them (to check the bridge map).
  bridge     Sends corrected values to a virtual Xbox 360 pad (needs ViGEmBus
             + `pip install vgamepad`). Games then see the corrected pad.
  selftest   Runs the math on synthetic data. No controller needed.

Needs: Python 3.9+, and `pip install pygame` for the controller commands.
Works on Windows and Linux (SDL reads the DualSense over USB or Bluetooth).

Axis numbering differs between drivers. Run `log --seconds 2` first: it prints
the axis count and first values. If sticks do not line up, pass --map, e.g.
  --map lx=0,ly=1,rx=3,ry=4,l2=2,r2=5      (raw Linux evdev order)
Triggers rest near -1 on SDL, so they are excluded from stick calibration.
`bridge` reads triggers from axes 4 and 5 unless the map gives l2= and r2=.
Use --pad N to pick a controller other than the first one SDL lists.

`live` corrects values inside this tool only. `bridge` is the step that feeds the
corrected values to games, through a virtual Xbox 360 pad. It does not change
what a PS5 console sees.
"""
import argparse
import csv
import json
import math
import os
import statistics
import sys
import time

DEFAULT_MAP = "lx=0,ly=1,rx=2,ry=3"
PROFILE_PATH = "dualsense_profile.json"

# Thresholds (units: stick range -1..1). Starting points, tune with real data.
OFFSET_FLAG = 0.02      # resting offset worth correcting
NOISE_FLAG = 0.02       # stick std dev worth calling "jittery"
CREEP_FLAG = 0.02       # total slow movement over the window
DEADZONE_MIN = 0.02
DEADZONE_MAX = 0.20


# ---------------------------------------------------------------- math ----
def parse_map(text):
    m = {}
    try:
        for part in text.split(","):
            k, v = part.split("=")
            m[k.strip()] = int(v)
    except ValueError:
        sys.exit("Bad --map %r. Expected something like lx=0,ly=1,rx=2,ry=3" % text)
    missing = [k for k in ("lx", "ly", "rx", "ry") if k not in m]
    if missing:
        sys.exit("--map is missing: %s" % ", ".join(missing))
    return m


def positive_int(text):
    v = int(text)
    if v < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return v


def percentile(values, p):
    s = sorted(values)
    if not s:
        return 0.0
    k = (len(s) - 1) * p
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def slope(ts, vs):
    """Least-squares slope of v against t (units per second)."""
    n = len(ts)
    if n < 2:
        return 0.0
    mt, mv = sum(ts) / n, sum(vs) / n
    den = sum((t - mt) ** 2 for t in ts)
    if den == 0:
        return 0.0
    return sum((t - mt) * (v - mv) for t, v in zip(ts, vs)) / den


def analyze_stick(ts, xs, ys):
    """Return stats and a plain-language finding for one stick at rest."""
    ox, oy = statistics.fmean(xs), statistics.fmean(ys)
    sx = statistics.pstdev(xs)
    sy = statistics.pstdev(ys)
    radial = [math.hypot(x - ox, y - oy) for x, y in zip(xs, ys)]
    dz = min(DEADZONE_MAX, max(DEADZONE_MIN, percentile(radial, 0.99) * 1.25))
    span = (ts[-1] - ts[0]) if len(ts) > 1 else 0.0
    creep_x = slope(ts, xs) * span
    creep_y = slope(ts, ys) * span
    findings = []
    off = math.hypot(ox, oy)
    if off >= OFFSET_FLAG:
        direction = []
        if abs(ox) >= OFFSET_FLAG:
            direction.append("left" if ox < 0 else "right")
        if abs(oy) >= OFFSET_FLAG:
            direction.append("up" if oy < 0 else "down")  # SDL: up is negative
        findings.append(
            "steady offset of %.3f leaning %s - worn or off-center sensor; "
            "correctable by recentering" % (off, "/".join(direction)))
    if max(sx, sy) >= NOISE_FLAG:
        findings.append(
            "jitter (std %.3f) - dirt or a worn contact; clean first, "
            "a deadzone is the only software cover" % max(sx, sy))
    if max(abs(creep_x), abs(creep_y)) >= CREEP_FLAG:
        findings.append(
            "slow creep of %.3f during the test - value is still moving, "
            "so recenter may not hold; repeat after warm-up"
            % max(abs(creep_x), abs(creep_y)))
    if not findings:
        findings.append("looks clean at rest")
    return {
        "offset": [round(ox, 4), round(oy, 4)],
        "std": [round(sx, 4), round(sy, 4)],
        "creep": [round(creep_x, 4), round(creep_y, 4)],
        "deadzone": round(dz, 3),
        "findings": findings,
    }


def correct(x, y, offset, deadzone):
    """Subtract offset, apply a radial deadzone, rescale so full push reaches 1.

    The rescale uses the travel actually left in the push direction: with the
    rest point off-center, the stick can only move 1 - offset one way and
    1 + offset the other, and both ends map to 1.
    """
    x -= offset[0]
    y -= offset[1]
    r = math.hypot(x, y)
    if r <= deadzone:
        return 0.0, 0.0
    ux, uy = x / r, y / r
    d = offset[0] * ux + offset[1] * uy
    o2 = offset[0] ** 2 + offset[1] ** 2
    # Distance from the rest point to the unit circle along (ux, uy).
    reach = -d + math.sqrt(max(0.0, d * d - o2 + 1.0))
    reach = max(reach, deadzone + 1e-6)
    scaled = min(1.0, (r - deadzone) / (reach - deadzone))
    return ux * scaled, uy * scaled


# ------------------------------------------------------------ hardware ----
def open_pad(index=0):
    index = index or 0
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    # No window here, so let SDL deliver joystick data while "in the background".
    os.environ.setdefault("SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS", "1")
    os.environ.setdefault("SDL_JOYSTICK_HIDAPI_PS5_RUMBLE", "0")
    try:
        import pygame
    except ImportError:
        sys.exit("pygame is not installed. Run: pip install pygame")
    pygame.init()
    pygame.joystick.init()
    if pygame.joystick.get_count() == 0:
        sys.exit("No controller found. Connect the DualSense (USB for testing) and retry.")
    if index >= pygame.joystick.get_count():
        sys.exit("--pad %d out of range: %d controller(s) found." % (
            index, pygame.joystick.get_count()))
    pad = pygame.joystick.Joystick(index)
    pad.init()
    print("Controller: %s | axes %d | buttons %d | hats %d" % (
        pad.get_name(), pad.get_numaxes(), pad.get_numbuttons(), pad.get_numhats()))
    return pygame, pad


def read_state(pygame, pad):
    pygame.event.pump()
    axes = [pad.get_axis(i) for i in range(pad.get_numaxes())]
    buttons = [pad.get_button(i) for i in range(pad.get_numbuttons())]
    hats = [pad.get_hat(i) for i in range(pad.get_numhats())]
    return axes, buttons, hats


def collect(pygame, pad, seconds):
    """Poll for `seconds`; return timestamps, axis rows, and any button/hat activity."""
    ts, rows, activity = [], [], 0
    changed = 0
    last = None
    # Warm-up: the first poll can return zeros before the pad's first report.
    w0 = time.perf_counter()
    while time.perf_counter() - w0 < 0.3:
        read_state(pygame, pad)
        time.sleep(0.005)
    t0 = time.perf_counter()
    while True:
        now = time.perf_counter() - t0
        if now >= seconds:
            break
        axes, buttons, hats = read_state(pygame, pad)
        ts.append(now)
        rows.append(axes)
        if any(buttons) or any(h != (0, 0) for h in hats):
            activity += 1
        if last is not None and axes != last:
            changed += 1
        last = axes
        time.sleep(0.001)
    return ts, rows, activity, changed / max(seconds, 1e-9)


# ------------------------------------------------------------ commands ----
def calibrate_from(ts, rows, mapping, activity, update_hz, out):
    profile = {"sticks": {}, "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    for name, (kx, ky) in (("left", ("lx", "ly")), ("right", ("rx", "ry"))):
        if kx not in mapping or ky not in mapping:
            continue
        xs = [r[mapping[kx]] for r in rows]
        ys = [r[mapping[ky]] for r in rows]
        res = analyze_stick(ts, xs, ys)
        profile["sticks"][name] = res
        print("\n%s stick" % name.upper())
        print("  offset x,y : %+.3f, %+.3f   (negative x = left)" % tuple(res["offset"]))
        print("  noise  x,y : %.3f, %.3f" % tuple(res["std"]))
        print("  creep  x,y : %+.3f, %+.3f over the test" % tuple(res["creep"]))
        print("  deadzone   : %.3f suggested after recentering" % res["deadzone"])
        for f in res["findings"]:
            print("  -> " + f)
    print("\nButtons/D-pad active with hands off: %s" % (
        "NO" if activity == 0 else "YES (%d samples) - ghost input, see notes" % activity))
    print("Value-change rate seen by this tool: ~%.0f updates/s (measure, don't assume)" % update_hz)
    with open(out, "w") as f:
        json.dump(profile, f, indent=2)
    print("\nSaved profile: %s" % out)
    if activity:
        print("Ghost button input with hands off usually means a stuck button, "
              "a software remap (Steam Input, DS4Windows) or another app sending "
              "commands. Quit those apps and rerun before blaming the controller.")


def cmd_calibrate(a):
    mapping = parse_map(a.map)
    pygame, pad = open_pad(a.pad)
    print("Set the controller down, hands OFF, sticks untouched. Starting in 3 s...")
    time.sleep(3)
    print("Measuring for %d s..." % a.seconds)
    ts, rows, activity, hz = collect(pygame, pad, a.seconds)
    calibrate_from(ts, rows, mapping, activity, hz, a.out)


def cmd_live(a):
    mapping = parse_map(a.map)
    with open(a.profile) as f:
        profile = json.load(f)
    pygame, pad = open_pad(a.pad)
    print("Live view. Ctrl+C to stop.\n")
    try:
        while True:
            axes, _, _ = read_state(pygame, pad)
            parts = []
            for name, (kx, ky) in (("L", ("lx", "ly")), ("R", ("rx", "ry"))):
                st = profile["sticks"].get("left" if name == "L" else "right")
                if not st or kx not in mapping:
                    continue
                x, y = axes[mapping[kx]], axes[mapping[ky]]
                cx, cy = correct(x, y, st["offset"], st["deadzone"])
                parts.append("%s raw(%+.2f,%+.2f) fixed(%+.2f,%+.2f)" % (name, x, y, cx, cy))
            print("\r" + "   ".join(parts) + "   ", end="", flush=True)
            time.sleep(0.02)
    except KeyboardInterrupt:
        print()


def cmd_log(a):
    pygame, pad = open_pad(a.pad)
    print("Logging %d s to %s" % (a.seconds, a.out))
    ts, rows, activity, hz = collect(pygame, pad, a.seconds)
    n = len(rows[0]) if rows else 0
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s"] + ["axis%d" % i for i in range(n)])
        for t, r in zip(ts, rows):
            w.writerow(["%.4f" % t] + ["%.5f" % v for v in r])
    print("%d samples, ~%.0f value changes/s, button activity samples: %d" % (len(ts), hz, activity))
    if rows:
        print("First sample:", ["%+.3f" % v for v in rows[0]])


def cmd_selftest(_):
    import random
    random.seed(7)
    # Synthetic stick leaning left (-0.07) with a little noise, 5 s at 250 Hz.
    ts = [i / 250 for i in range(1250)]
    xs = [-0.07 + random.gauss(0, 0.004) for _ in ts]
    ys = [0.00 + random.gauss(0, 0.004) for _ in ts]
    res = analyze_stick(ts, xs, ys)
    assert -0.08 < res["offset"][0] < -0.06, res
    assert any("left" in f for f in res["findings"]), res
    cx, cy = correct(-0.07, 0.0, res["offset"], res["deadzone"])
    assert (cx, cy) == (0.0, 0.0), (cx, cy)
    # A real push right must survive correction and reach full scale.
    cx, cy = correct(1.0, 0.0, res["offset"], res["deadzone"])
    assert cx > 0.95 and abs(cy) < 0.01, (cx, cy)
    # Full push in either direction must reach 1.0 even with the rest point off-center.
    off = [0.067, 0.012]
    for px, py in ((1.0, 0.012), (-1.0, 0.012), (0.067, 1.0), (0.067, -1.0)):
        cx, cy = correct(px, py, off, 0.03)
        assert math.hypot(cx, cy) > 0.99, (px, py, cx, cy)
    # Clean stick: no findings other than "clean".
    clean = analyze_stick(ts, [random.gauss(0, 0.002) for _ in ts], [random.gauss(0, 0.002) for _ in ts])
    assert clean["findings"] == ["looks clean at rest"], clean
    # Creeping stick: slow ramp must be flagged.
    creep = analyze_stick(ts, [i * 0.00004 for i in range(len(ts))], [0.0] * len(ts))
    assert any("creep" in f for f in creep["findings"]), creep
    # Bridge mapping: stick up (SDL y=-1) must become XInput up (+1); triggers 0..1.
    prof = {"sticks": {"right": {"offset": [0.067, 0.012], "deadzone": 0.03}}}
    mp = parse_map(DEFAULT_MAP)
    rest = [0.0, 0.0, 0.067, 0.012, -1.0, -1.0]
    _, (rx, ry), lt, rt, pr = to_x360(rest, [0] * 17, prof, mp)
    assert (rx, ry, lt, rt, pr) == (0.0, 0.0, 0.0, 0.0, set()), (rx, ry, lt, rt, pr)
    _, (rx, ry), lt, rt, pr = to_x360([0, 0, 0.067, -1.0, 1.0, 0.0], [1] + [0] * 16, prof, mp)
    assert ry > 0.9 and abs(rx) < 0.05 and lt == 1.0 and abs(rt - 0.5) < 1e-9 and pr == {"A"}, (rx, ry, lt, rt, pr)
    m = window_means([[0.1, 0.2, 0.3, 0.4], [0.3, 0.4, 0.5, 0.6]], parse_map(DEFAULT_MAP))
    assert all(abs(x - y) < 1e-9 for x, y in zip(m, [0.2, 0.3, 0.4, 0.5])), m
    print("selftest ok:", json.dumps(res["findings"]))


# -------------------------------------------------------------- bridge ----
# SDL button index -> XUSB button name. This is SDL's controller order for a
# DualSense (Cross=A, Circle=B, ...). Check yours with the `buttons` command.
BUTTON_MAP = {
    0: "A", 1: "B", 2: "X", 3: "Y", 4: "BACK", 5: "GUIDE", 6: "START",
    7: "LEFT_THUMB", 8: "RIGHT_THUMB", 9: "LEFT_SHOULDER", 10: "RIGHT_SHOULDER",
    11: "DPAD_UP", 12: "DPAD_DOWN", 13: "DPAD_LEFT", 14: "DPAD_RIGHT",
}


def trigger01(v):
    """SDL triggers rest at -1 and reach +1; X360 wants 0..1."""
    return min(1.0, max(0.0, (v + 1.0) / 2.0))


def to_x360(axes, buttons, profile, mapping):
    """Pure function: SDL state in, X360 report values out (no hardware)."""
    sticks = {}
    for side, (kx, ky) in (("left", ("lx", "ly")), ("right", ("rx", "ry"))):
        x, y = axes[mapping[kx]], axes[mapping[ky]]
        st = profile.get("sticks", {}).get(side)
        if st:
            x, y = correct(x, y, st["offset"], st["deadzone"])
        sticks[side] = (x, -y)  # SDL up is negative, XInput up is positive
    lt = trigger01(axes[mapping.get("l2", 4)])
    rt = trigger01(axes[mapping.get("r2", 5)])
    pressed = {BUTTON_MAP[i] for i, b in enumerate(buttons) if b and i in BUTTON_MAP}
    return sticks["left"], sticks["right"], lt, rt, pressed


def cmd_buttons(a):
    pygame, pad = open_pad(a.pad)
    print("Press buttons. Ctrl+C to stop.")
    prev = set()
    try:
        while True:
            _, buttons, _ = read_state(pygame, pad)
            now = {i for i, b in enumerate(buttons) if b}
            for i in sorted(now - prev):
                print("button %d pressed  (bridge sends: %s)" % (i, BUTTON_MAP.get(i, "nothing")))
            prev = now
            time.sleep(0.01)
    except KeyboardInterrupt:
        print()


def cmd_bridge(a):
    mapping = parse_map(a.map)
    try:
        with open(a.profile) as f:
            profile = json.load(f)
    except FileNotFoundError:
        sys.exit("No profile at %s. Run `calibrate` first." % a.profile)
    try:
        import vgamepad as vg
    except ImportError:
        sys.exit("vgamepad is not installed. Run: pip install vgamepad "
                 "(it also installs the ViGEmBus driver).")
    pygame, pad = open_pad(a.pad)
    if sys.platform == "win32":
        try:  # ask Windows for 1 ms timer ticks so the loop is not stuck near 64 Hz
            import ctypes
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.3:  # first polls can be zeros
        read_state(pygame, pad)
        time.sleep(0.005)
    xb = {i: getattr(vg.XUSB_BUTTON, "XUSB_GAMEPAD_" + n) for i, n in BUTTON_MAP.items()}
    vpad = vg.VX360Gamepad()
    print("Bridge running: virtual Xbox 360 pad is live. Ctrl+C to stop.")
    n, t_rate = 0, time.perf_counter()
    try:
        while True:
            axes, buttons, _ = read_state(pygame, pad)
            (lx, ly), (rx, ry), lt, rt, pressed = to_x360(axes, buttons, profile, mapping)
            vpad.left_joystick_float(x_value_float=lx, y_value_float=ly)
            vpad.right_joystick_float(x_value_float=rx, y_value_float=ry)
            vpad.left_trigger_float(value_float=lt)
            vpad.right_trigger_float(value_float=rt)
            for i, b in xb.items():
                if BUTTON_MAP[i] in pressed:
                    vpad.press_button(button=b)
                else:
                    vpad.release_button(button=b)
            vpad.update()
            n += 1
            if time.perf_counter() - t_rate >= 5:
                print("\r~%.0f reports/s   " % (n / (time.perf_counter() - t_rate)), end="", flush=True)
                n, t_rate = 0, time.perf_counter()
            time.sleep(0.002)
    except KeyboardInterrupt:
        print()
    except pygame.error as e:
        print("\nController read failed (%s). Was it disconnected?" % e)
    finally:
        vpad.reset()
        vpad.update()


def window_means(rows, mapping):
    """Mean raw value of lx, ly, rx, ry over one window."""
    return [statistics.fmean(r[mapping[k]] for r in rows) for k in ("lx", "ly", "rx", "ry")]


def cmd_watch(a):
    mapping = parse_map(a.map)
    pygame, pad = open_pad(a.pad)
    print("Watching raw sticks every %d s, hands OFF. Ctrl+C to stop. CSV: %s" % (a.window, a.out))
    print("A '*' marks a window that moved 0.01 or more from the first window.\n")
    base = None
    t_start = time.perf_counter()
    with open(a.out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "lx", "ly", "rx", "ry", "button_samples"])
        try:
            while True:
                ts, rows, activity, _ = collect(pygame, pad, a.window)
                m = window_means(rows, mapping)
                if base is None:
                    base = m
                moved = max(abs(v - b) for v, b in zip(m, base)) >= 0.01
                t = time.perf_counter() - t_start
                w.writerow(["%.1f" % t] + ["%.5f" % v for v in m] + [activity])
                f.flush()
                print("%6.0fs  L(%+.3f,%+.3f)  R(%+.3f,%+.3f)  buttons:%d %s" % (
                    t, m[0], m[1], m[2], m[3], activity, "*" if moved else ""))
        except KeyboardInterrupt:
            print()


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate")
    c.add_argument("--seconds", type=positive_int, default=6)
    c.add_argument("--map", default=DEFAULT_MAP)
    c.add_argument("--out", default=PROFILE_PATH)
    c.add_argument("--pad", type=int, default=0, help="controller index (default 0)")
    c.set_defaults(fn=cmd_calibrate)
    l = sub.add_parser("live")
    l.add_argument("--profile", default=PROFILE_PATH)
    l.add_argument("--map", default=DEFAULT_MAP)
    l.add_argument("--pad", type=int, default=0, help="controller index (default 0)")
    l.set_defaults(fn=cmd_live)
    g = sub.add_parser("log")
    g.add_argument("--seconds", type=positive_int, default=10)
    g.add_argument("--out", default="dualsense_log.csv")
    g.add_argument("--pad", type=int, default=0, help="controller index (default 0)")
    g.set_defaults(fn=cmd_log)
    b = sub.add_parser("buttons")
    b.add_argument("--pad", type=int, default=0, help="controller index (default 0)")
    b.set_defaults(fn=cmd_buttons)
    br = sub.add_parser("bridge")
    br.add_argument("--profile", default=PROFILE_PATH)
    br.add_argument("--map", default=DEFAULT_MAP)
    br.add_argument("--pad", type=int, default=0, help="controller index (default 0)")
    br.set_defaults(fn=cmd_bridge)
    wt = sub.add_parser("watch")
    wt.add_argument("--window", type=positive_int, default=5, help="seconds per reading")
    wt.add_argument("--map", default=DEFAULT_MAP)
    wt.add_argument("--out", default="dualsense_watch.csv")
    wt.add_argument("--pad", type=int, default=0, help="controller index (default 0)")
    wt.set_defaults(fn=cmd_watch)
    s = sub.add_parser("selftest")
    s.set_defaults(fn=cmd_selftest)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()