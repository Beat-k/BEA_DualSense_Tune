#!/usr/bin/env python3
# Copyright (c) 2026 Jeremy Frank Jackson, BEATEK Holdings, LLC
# Licensed under the MIT License. See the LICENSE file.
"""
bea_drift_monitor.py - log controller stick drift while a game is running.

Reads the controller (read-only) and checks whether a game is running and in
focus. Every few seconds it records the raw stick readings, labelled with the
game state, so you can compare drift with the game closed, running in the
background, and in focus.

Only "idle" windows count as drift data: no buttons, no trigger pull, sticks
steady and near center. Windows where you are playing are logged but not
treated as drift.

  python bea_drift_monitor.py            run until Ctrl+C, then print a summary
  python bea_drift_monitor.py selftest   check the math, no controller needed

It does not touch the game: it lists running processes by name and reads the
title of the foreground window. It sends nothing to the controller or the game.
Requires bea_dualsense_tune.py in the same folder, and `pip install pygame`.
Use `calibrate` first so there is a saved profile to compare against.
"""
import argparse
import csv
import json
import statistics
import subprocess
import sys
import time
from datetime import datetime

import bea_dualsense_tune as bdt

STICKS = ("lx", "ly", "rx", "ry")
IDLE_RANGE = 0.03         # a stick moving more than this inside a window is "in use"
IDLE_MAX_DEFLECT = 0.25   # a stick held steady this far from rest is not "at rest"
TRIGGER_REST = -0.9       # SDL trigger value below this counts as released
FLAG_DELTA = 0.01         # idle reading this far from the saved profile gets a '*'


# ---------------------------------------------------------------- math ----
def load_offsets(path):
    """Saved resting offsets per axis; zeros (and False) if there is no profile."""
    try:
        with open(path) as f:
            profile = json.load(f)
    except FileNotFoundError:
        return {k: 0.0 for k in STICKS}, False
    off = {}
    for side, (kx, ky) in (("left", ("lx", "ly")), ("right", ("rx", "ry"))):
        o = profile.get("sticks", {}).get(side, {}).get("offset", [0.0, 0.0])
        off[kx], off[ky] = o
    return off, True


def analyze_window(rows, mapping, buttons_active, offsets, l2=4, r2=5):
    """Summarize one window of axis rows. Pure function."""
    cols = {k: [r[mapping[k]] for r in rows] for k in STICKS}
    means = {k: statistics.fmean(v) for k, v in cols.items()}
    ranges = {k: max(v) - min(v) for k, v in cols.items()}
    stds = {k: statistics.pstdev(v) for k, v in cols.items()}
    delta = {k: means[k] - offsets[k] for k in STICKS}
    trig_max = -1.0
    if rows and len(rows[0]) > max(l2, r2):
        trig_max = max(max(r[l2], r[r2]) for r in rows)
    idle = (buttons_active == 0
            and trig_max < TRIGGER_REST
            and max(ranges.values()) < IDLE_RANGE
            and all(abs(delta[k]) < IDLE_MAX_DEFLECT for k in STICKS))
    return {"idle": idle, "means": means, "stds": stds, "delta": delta,
            "buttons": buttons_active}


def summarize(records):
    """records: list of (state, analysis). Per game state, idle drift stats."""
    out = {}
    for state, res in records:
        s = out.setdefault(state, {"windows": 0, "idle": 0, "deltas": {k: [] for k in STICKS}})
        s["windows"] += 1
        if res["idle"]:
            s["idle"] += 1
            for k in STICKS:
                s["deltas"][k].append(res["delta"][k])
    return out


# ------------------------------------------------------------ game state ----
def game_running(game):
    """True/False if a process name contains `game`; None if the list can't be read."""
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return None
    return game.lower() in out.lower()


def foreground_title():
    """Title of the focused window. Reads the title only; does not open any process."""
    if sys.platform != "win32":
        return ""
    try:
        import ctypes
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, buf, 512)
        return buf.value
    except Exception:
        return ""


def game_state(game, title):
    running = game_running(game)
    if running is None:
        return "unknown"
    if not running:
        return "closed"
    return "focused" if title.lower() in foreground_title().lower() else "background"


# ------------------------------------------------------------------ run ----
def print_summary(records):
    print("\nSummary (idle windows only count as drift data)")
    summ = summarize(records)
    if not summ:
        print("  no windows recorded")
        return
    for state in ("closed", "background", "focused", "unknown"):
        s = summ.get(state)
        if not s:
            continue
        print("  %-10s %d windows, %d idle" % (state, s["windows"], s["idle"]))
        if s["idle"]:
            for k in STICKS:
                d = s["deltas"][k]
                print("      %s drift vs profile: mean %+.3f  min %+.3f  max %+.3f"
                      % (k, statistics.fmean(d), min(d), max(d)))
        else:
            print("      no idle windows: set the controller down to get drift data")


def run(a):
    mapping = bdt.parse_map(a.map)
    offsets, have_profile = load_offsets(a.profile)
    pygame, pad = bdt.open_pad(a.pad)
    out = a.out or datetime.now().strftime("drift_monitor_%Y%m%d_%H%M%S.csv")
    if not have_profile:
        print("No profile at %s: drift is measured against zero. Run `calibrate` for better data." % a.profile)
    print("Watching for '%s' (window title '%s'). Windows: %d s. CSV: %s" % (
        a.game, a.title, a.window, out))
    print("Set the controller down now and then so idle windows get recorded. Ctrl+C for the summary.\n")
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.3:  # first polls can be zeros
        bdt.read_state(pygame, pad)
        time.sleep(0.005)
    records = []
    t_start = time.perf_counter()
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "t_s", "game_state", "idle"] + list(STICKS)
                   + ["d_" + k for k in STICKS] + ["std_max", "button_samples"])
        try:
            while True:
                rows, active = [], 0
                t_end = time.perf_counter() + a.window
                while time.perf_counter() < t_end:
                    axes, buttons, hats = bdt.read_state(pygame, pad)
                    rows.append(axes)
                    if any(buttons) or any(h != (0, 0) for h in hats):
                        active += 1
                    time.sleep(0.005)
                res = analyze_window(rows, mapping, active, offsets,
                                     mapping.get("l2", 4), mapping.get("r2", 5))
                state = game_state(a.game, a.title)
                records.append((state, res))
                t = time.perf_counter() - t_start
                w.writerow([datetime.now().isoformat(timespec="seconds"), "%.1f" % t, state,
                            int(res["idle"])]
                           + ["%.5f" % res["means"][k] for k in STICKS]
                           + ["%.5f" % res["delta"][k] for k in STICKS]
                           + ["%.5f" % max(res["stds"].values()), active])
                f.flush()
                if res["idle"]:
                    flag = "*" if max(abs(v) for v in res["delta"].values()) >= FLAG_DELTA else ""
                    print("%6.0fs %-10s idle  drift L(%+.3f,%+.3f) R(%+.3f,%+.3f) %s" % (
                        t, state, res["delta"]["lx"], res["delta"]["ly"],
                        res["delta"]["rx"], res["delta"]["ry"], flag))
                else:
                    print("%6.0fs %-10s in use" % (t, state))
        except KeyboardInterrupt:
            print()
        except pygame.error as e:
            print("\nController read failed (%s). Was it disconnected?" % e)
    print_summary(records)
    print("\nSaved: %s" % out)


def selftest():
    mp = bdt.parse_map(bdt.DEFAULT_MAP)
    off = {"lx": 0.0, "ly": 0.0, "rx": 0.067, "ry": 0.012}
    rest = [[0.0, 0.0, 0.067, 0.012, -1.0, -1.0]] * 50
    r = analyze_window(rest, mp, 0, off)
    assert r["idle"] and max(abs(v) for v in r["delta"].values()) < 1e-9, r
    shifted = [[0.0, 0.0, 0.077, 0.012, -1.0, -1.0]] * 50
    r = analyze_window(shifted, mp, 0, off)
    assert r["idle"] and abs(r["delta"]["rx"] - 0.01) < 1e-9, r
    assert not analyze_window(rest, mp, 3, off)["idle"]                      # buttons
    moving = [[0.0, 0.0, 0.067 + i * 0.01, 0.012, -1.0, -1.0] for i in range(50)]
    assert not analyze_window(moving, mp, 0, off)["idle"]                    # stick moving
    held = [[0.5, 0.0, 0.067, 0.012, -1.0, -1.0]] * 50
    assert not analyze_window(held, mp, 0, off)["idle"]                      # held, not at rest
    trig = [[0.0, 0.0, 0.067, 0.012, 0.2, -1.0]] * 50
    assert not analyze_window(trig, mp, 0, off)["idle"]                      # trigger pulled
    summ = summarize([("closed", analyze_window(rest, mp, 0, off)),
                      ("focused", analyze_window(shifted, mp, 0, off)),
                      ("focused", analyze_window(rest, mp, 5, off))])
    assert summ["closed"]["idle"] == 1 and summ["focused"]["windows"] == 2 and summ["focused"]["idle"] == 1, summ
    assert abs(summ["focused"]["deltas"]["rx"][0] - 0.01) < 1e-9, summ
    print("selftest ok")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", nargs="?", default="run", choices=["run", "selftest"])
    p.add_argument("--game", default="RainbowSix", help="process name to look for (substring)")
    p.add_argument("--title", default="Rainbow Six", help="window title that means the game is in focus")
    p.add_argument("--window", type=bdt.positive_int, default=3, help="seconds per reading")
    p.add_argument("--profile", default=bdt.PROFILE_PATH)
    p.add_argument("--map", default=bdt.DEFAULT_MAP)
    p.add_argument("--pad", type=int, default=0, help="controller index (default 0)")
    p.add_argument("--out", default=None, help="CSV path (default: drift_monitor_<time>.csv)")
    a = p.parse_args()
    selftest() if a.command == "selftest" else run(a)


if __name__ == "__main__":
    main()
