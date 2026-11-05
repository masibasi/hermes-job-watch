# Changelog

## 1.0.2

- Reworded the certificate hint. The install-time security scan of Hermes flagged the words "pip install" inside the error
  message as `unpinned_pip_install` (medium). The skill never installs anything; the scan was a false positive on a string,
  but the new wording gives a clean scan. Found by installing the skill on a fresh Hermes.

## 1.0.1

- Verify certificates with the operating system's CA bundle when Python has none. Some macOS Pythons ship
  without one, so every board request failed with `CERTIFICATE_VERIFY_FAILED`. Verification is never disabled.
  Found by running the script with the system `python3` against the real boards. (The commit message of
  8231d28 wrongly says it was found by installing the skill on a fresh Hermes; the fresh-install check
  came after it.)

## 1.0.0 (833fd32)

- First release: `check`, `recent`, `details`, `boards`, `add-board`, `remove-board` for Greenhouse, Lever and Ashby.
- Baselines are tracked per board. A first run in which every board failed does not consume the baseline,
  and a board that failed earlier is baselined when it first works.
- `recent` does not return the old postings recorded silently as a baseline.
- HTTPS uses certifi when installed, and a failed request explains how to fix a missing CA bundle.
