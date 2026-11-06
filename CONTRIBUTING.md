# Contributing to BEA DualSense Tune

Thanks for helping. This is a small, single-file tool, and the goal is to keep it that way: easy to read, no config, few dependencies.

## Useful contributions

- **Test reports.** The `bridge` command has not been tested end to end against a game. Tell us what game, what controller connection (USB or Bluetooth), and what happened.
- **Stick mapping reports.** If the default `--map` is wrong on your OS or SDL version, open an issue with the output of `log --seconds 2`.
- **Threshold tuning.** The finding thresholds are starting points. Real measurements from worn and healthy controllers help.
- **Bug fixes and small features.**

## Reporting a bug

Open an issue and include:

- OS, Python version and pygame/SDL version
- USB or Bluetooth
- The command you ran and its full output
- What you expected to happen

Please don't attach `dualsense_log.csv` files over a few hundred rows; paste a short excerpt instead.

## Making a change

1. Fork the repository and create a branch.
2. Make your change in `bea_dualsense_tune.py`.
3. Run `python bea_dualsense_tune.py selftest`. It must pass. If you change the math or the bridge mapping, extend `selftest` to cover it.
4. If you change a command, flag or threshold, update `README.md` in the same change.
5. Open a pull request describing what changed and why. If you tested with a real controller, say which.

## Guidelines

- Keep it one file, Python 3.9+. `pygame` is the only required dependency; `vgamepad` stays optional and only for `bridge`.
- Don't add config files or required setup steps.
- Keep the output plain and readable. Findings should say what the numbers look like, not overstate a diagnosis.
- Don't claim a hardware fault is fixed in software. The tool measures and masks.
- Match the existing code style.

## License

By contributing, you agree that your contributions are licensed under the [MIT License](LICENSE) that covers this project.
