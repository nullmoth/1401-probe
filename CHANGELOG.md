# Changelog

Every release stays up, on nullmothsystems.com/probe and on the GitHub releases page, with its SHA-256.

## 2.0.2 - 2026-10-09

- **No more freezes in the last step on laptops.** Every driver test in the last step now runs under the same
  automatic power-off as the graphics test: if one hangs, the PC turns itself off and the next start skips it.
  The HDMI audio of an NVIDIA card that is not driving the screen is no longer tested; it hung RTX 4050 laptops.
- **A freeze while listing PCI devices no longer loses the graphics card.** Before, every later scan skipped all of
  PCI and the report had no GPU in it.

## 2.0.1 - 2026-10-09

Fixes the errors some of you saw on 2.0.0. If you made a 2.0.0 stick, make it again with 2.0.1.

- **Driver trace errors fixed.** On 2.0.0 the last step printed "Bad file descriptor" / "write to closed file"
  errors and stalled for about a minute per device. The trace reader now stops cleanly.
- **NVIDIA's own driver now actually loads in the trace.** 2.0.0 tried to unload nouveau while it was driving the
  screen, which fails, so the NVIDIA trace recorded nothing. The card is now handed over directly.
- Instructions everywhere (site, stick, Windows app) match what the scan does: the Wi-Fi/Bluetooth question at the
  start, about 5 to 20 minutes, a black screen of up to 10 minutes near the end.
- New: `1401-probe-linux.py` makes the stick from Linux and finds your report afterwards.

## 2.0.0 - 2026-10-08

The probe now maps the whole machine, down to how each driver talks to its hardware, so the installer can build the
right setup for your exact PC.

- **Full system map.** Which GPU drives each screen and the laptop panel, switchable-graphics hints, GPU features
  (architecture, CUDA level, RT and tensor cores), PCIe slots, memory modules (no serials), storage ports and modes
  (AHCI/RAID/VMD), fans and sensors, USB roles (Bluetooth, camera, RGB, fingerprint), touchpad and keyboard bus,
  CPU core layout, IOMMU and the firmware memory map.
- **Build report.** The stick also writes a `Report.json` in the format the EFI builder reads (collector from
  Hardware-Sniffer, BSD-3).
- **Driver trace.** At the end, each graphics and audio driver is restarted while the kernel records every register
  read and write it makes. NVIDIA cards get NVIDIA's own driver for their generation, carried on the stick:
  open 610 for Turing and newer, 580 for Maxwell/Pascal/Volta (Kepler and older: nouveau for now).
  The screen may go black for a few minutes during this step.
- **Wi-Fi, Bluetooth and Ethernet: your choice.** The stick still has no network drivers in it. At the start it asks
  (default: no). If you press Y, it loads only the drivers for your own chips, records them starting, switches each
  radio off the moment it appears, and removes the drivers again. Nothing is configured or sent.
- Traces are redacted like the rest of the report (including a network chip's address as it appears in its
  registers) and capped at 40 MB.

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
