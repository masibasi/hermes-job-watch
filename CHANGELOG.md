# Changelog

## 1.0.2

- Verify certificates with the operating system's CA bundle when Python has none. Some macOS Pythons ship
  without one, so every board request failed with `CERTIFICATE_VERIFY_FAILED`. Verification is never disabled.
  This was found by running the script with the system `python3` against the real boards. (The commit
  message of this change wrongly says it was found by installing the skill on a fresh Hermes. The
  fresh-install check came after it.)

## 1.0.1

- Baselines are tracked per board. A first run in which every board failed no longer consumes the baseline,
  and a board that failed earlier is baselined when it first works.
- `recent` no longer returns baseline roles recorded silently on the first run.
- A failed request now explains how to fix a missing CA bundle.

## 1.0.0

- First release: `check`, `recent`, `details`, `boards`, `add-board`, `remove-board` for Greenhouse, Lever and Ashby.
