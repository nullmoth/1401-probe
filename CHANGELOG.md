# Changelog

Every release stays up, on nullmothsystems.com/probe and on the GitHub releases page, with its SHA-256.

## 1.0.1 - 2026-09-26

Fixes from the first round of test reports. Thanks to everyone who ran 1.0.0.

- **A freeze no longer loses the rest of the scan.** Before it touches a piece of hardware, the probe now writes the
  name of that step to the stick. If the PC locks up, start the stick again and pick the normal scan: it skips the
  one step that froze and finishes everything else. The report says which step was skipped and why.
- **The PCI scan runs one device at a time.** In 1.0.0 one desktop froze inside the PCI stage, and its report kept
  only the three stages before it. Now every device's IDs and addresses are saved first, and each device's full
  decode runs on its own afterwards, so a freeze points at one device instead of taking the whole stage.
- **Laptop graphics BIOS.** On laptops with a second GPU, the firmware often leaves the card's memory decoding off,
  so 1.0.0 could not read the VBIOS or the chip ID. The probe now turns decoding on for that one read and puts the
  original setting back right after.
- **Graphics driver data.** Each GPU's debugfs folder is read once instead of three times. The report now lists what
  the driver actually exposed, and every file it could not read is recorded with the reason.
- **Reports are sealed.** Each report has a `seal.json`: the SHA-256 of every file, plus a keyed check over that
  list. It tells us whether a report came straight off the stick or was changed afterwards. Nothing is sent
  anywhere and nothing in the report changes. Builds from source use their own key or none (`"sealed": false`).
- The PCI stage gets 3 minutes instead of 2.

## 1.0.0 - 2026-09-25

First public release.
