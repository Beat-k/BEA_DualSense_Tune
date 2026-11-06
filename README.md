# BEA DualSense Tune

Diagnose PS5 DualSense stick drift and correct it in software. One Python file, no config.

Hands off the controller for six seconds and the tool measures each stick's resting offset, noise and slow creep. It tells you what kind of fault the numbers look like, saves a correction profile, and can send the corrected values to games through a virtual Xbox 360 pad.

```
RIGHT stick
  offset x,y : +0.067, +0.012   (negative x = left)
  noise  x,y : 0.000, 0.000
  creep  x,y : +0.000, +0.000 over the test
  deadzone   : 0.020 suggested after recentering
  -> steady offset of 0.068 leaning right - worn or off-center sensor; correctable by recentering
```

## Requirements

- Python 3.9+
- `pip install pygame`
- A DualSense over USB or Bluetooth
- Windows or Linux. Tested on Windows with Python 3.12, pygame 2.6.1 (SDL 2.28.4).
- For `bridge` only: Windows, `pip install vgamepad` (which installs the ViGEmBus driver)

## Quick start

```
pip install pygame
python bea_dualsense_tune.py selftest
python bea_dualsense_tune.py log --seconds 2
python bea_dualsense_tune.py calibrate
python bea_dualsense_tune.py live
```

`selftest` checks the math on synthetic data and needs no controller. `log --seconds 2` prints the controller name and axis count so you can confirm the stick mapping before calibrating. For `calibrate`, set the controller flat on a desk and don't touch it.

## Commands

| Command | What it does |
|---|---|
| `calibrate` | Measures resting offset, noise and creep for both sticks. Prints findings and saves `dualsense_profile.json`. `--seconds N` (default 6, at least 1), `--map`, `--out`, `--pad`. |
| `live` | Shows raw and corrected stick values side by side using the saved profile. `--profile`, `--map`, `--pad`. |
| `log` | Writes every sample to CSV (`dualsense_log.csv`). Also counts button and D-pad activity, which exposes ghost input. `--seconds N` (default 10, at least 1), `--out`, `--pad`. |
| `buttons` | Prints each button index as you press it, and what `bridge` will send for it. `--pad`. |
| `bridge` | Sends corrected values to a virtual Xbox 360 pad. `--profile`, `--map`, `--pad`. See [Using it in games](#using-it-in-games). |
| `selftest` | Runs the math and the bridge mapping on synthetic data. No controller needed. |

Run `python bea_dualsense_tune.py <command> --help` for the full options.

## How the correction works

The saved profile holds each stick's resting offset and a suggested deadzone. `live` and `bridge` apply three steps:

1. Subtract the offset, so the resting point reads 0,0.
2. Zero anything inside the radial deadzone.
3. Rescale the rest so a full push reaches 1.0 in every direction, even though an off-center stick has less travel on one side.

## Reading the results

Values are on the stick range -1 to 1. SDL reports up as negative.

| Finding | Trigger | Usually means | Software fix |
|---|---|---|---|
| Steady offset | resting distance from center ≥ 0.02 | worn or off-center sensor or spring | recentering works if it is repeatable |
| Jitter | std dev ≥ 0.02 | dirt or a worn contact | clean first; a deadzone is the only cover |
| Creep | slow movement ≥ 0.02 over the test | value still settling or heating up | repeat after warm-up; recentering may not hold |

The thresholds are starting points. They sit at the top of the script, so tune them with your own data.

Run `calibrate` two or three times, letting each stick spring back from a different direction. A real offset comes back the same each time. If it wanders, the problem is mechanical and no software offset will hold.

Notes:

- DualSense sticks report 8-bit values, so a stick at rest can read one exact number with zero noise. That is normal, not a frozen feed.
- Triggers rest near -1 on SDL, so they are left out of stick calibration.
- The update rate that `calibrate` prints measures this tool's polling loop. On Windows the default timer is coarse, so it tends to read about 64 updates/s. It is not the controller's report rate.
- If the sticks don't line up, pass `--map`. For example, `--map lx=0,ly=1,rx=3,ry=4,l2=2,r2=5` is the raw Linux evdev order. The default `lx=0,ly=1,rx=2,ry=3` matches SDL on Windows. `lx`, `ly`, `rx` and `ry` are required. `bridge` reads the triggers from axes 4 and 5 unless the map gives `l2=` and `r2=`. Use the same `--map` for `calibrate`, `live` and `bridge`.
- If another gamepad is connected, pass `--pad N` (0 is the first controller SDL lists) to pick the DualSense. Run `log --seconds 2 --pad N` to check which one you got.
- If the controller disconnects while `bridge` is running, it prints a message and resets the virtual pad.

## Using it in games

`live` and `calibrate` only change values inside this tool. To make a game see corrected input, `bridge` reads the real DualSense, applies the profile, and drives a virtual Xbox 360 pad.

1. `pip install vgamepad` and accept the ViGEmBus driver install.
2. Run `calibrate`, then `buttons` to confirm the button map, then `bridge`.
3. Hide the physical DualSense from the game so it doesn't read both pads. [HidHide](https://github.com/nefarius/HidHide) is the usual tool: hide the DualSense and allow `python.exe`.
4. Start the game after the bridge is running.

Limits to know about:

- **It's an Xbox 360 pad to the game.** Gyro, touchpad, adaptive triggers, haptics and the light bar don't pass through. Games show Xbox prompts.
- **Anti-cheat.** Virtual-driver software can trip anti-cheat in online games. Check the game's rules before using it there. You use it at your own risk.
- **ViGEmBus is no longer actively developed upstream.** Check its repository for current status before depending on it.
- **Status:** the `bridge` mapping logic is covered by `selftest`. It has not yet been tested end to end against a game. Report what you find.

## Not a hardware fix

If the offset grows or the jitter doesn't clean up, the stick module needs replacing. This tool measures the fault and masks it; it doesn't repair it. It also doesn't change what a PS5 console sees.

## Ghost input

If `calibrate` reports button or D-pad activity with your hands off, check for a stuck button first. Then look for software that remaps or injects input (Steam Input, DS4Windows, other apps sending commands). Quit those and run it again before blaming the controller.

## Files

| File | Purpose |
|---|---|
| `bea_dualsense_tune.py` | the tool |
| `LICENSE` | MIT license |
| `CONTRIBUTING.md` | how to report issues and send changes |
| `dualsense_profile.json` | your saved correction profile (generated) |
| `dualsense_log.csv` | raw samples from `log` (generated) |

---

BEATEK Holdings, LLC · Jeremy Frank Jackson

License: [MIT](LICENSE). Contributions welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).
