# 1401 Probe

A free hardware scanner from [NullMoth Systems](https://nullmothsystems.com). A Windows app turns a USB stick
into a small offline scanner. You start your PC from the stick, it reads what hardware the machine has, and it
saves one report to the stick. You read the report, then upload it yourself if you want to.

Download and how-to: **[nullmothsystems.com/probe](https://nullmothsystems.com/probe)**

## What it does to your computer

- The Windows app **erases the USB stick you pick**, after asking twice. It only lists USB drives.
- The scanner has **no network, Wi-Fi or Bluetooth drivers**. It cannot connect to anything.
- Before it scans, it marks **every drive except its own stick read-only**. It never mounts your internal drives.
- It never changes BIOS/UEFI settings. The report has serial numbers, UUIDs, MAC addresses and the Windows product
  key removed before it is saved.

## Check your download

The installer is not code-signed yet, so Windows SmartScreen will warn you. Check its fingerprint instead. The
expected SHA-256 is published in DNS, signed with DNSSEC, separately from the download server:

```
dig +short TXT _probe-sha256.nullmothsystems.com
```

On Windows, compare it against the file you downloaded:

```
Get-FileHash .\1401-Probe-Setup.exe -Algorithm SHA256
```

If they differ, do not run it.

## Build it yourself

Everything builds in Docker containers pinned by digest. Nothing is installed on the host.

```
NM_ALLOW_NO_PRIVATE=1 ./build.sh all
```

- `out/usb/` and `out/1401-probe.img`: the scanner stick (Alpine Linux, offline)
- `out/windows/1401-Probe-Setup.exe`: the Windows app and installer

`NM_ALLOW_NO_PRIVATE=1` skips one release check that only the maintainer's machine can run.
The first build makes a seal key at `~/.config/nullmoth/seal/<version>.key` (your own; reports from your build are
sealed with it). `NM_ALLOW_NO_SEAL=1` builds without one, and its reports say `"sealed": false`.
What changed in each version: `CHANGELOG.md`.
`./test/qemu-test.sh` boots the real stick image on an emulated PC and fails if a planted serial number survives
into the report, if any network interface appears, or if the emulated SSD changes by one byte (it needs QEMU with
UEFI firmware).

| Path | What |
|---|---|
| `probe/` | the scanner: image build, boot config, `probe.py` (scan, redact, report) |
| `windows/` | the Windows app (.NET Framework 4.8), the NSIS installer, their build |
| `docs/` | the licence shown in the app, instructions, third-party and firmware notices |
| `test/` | the QEMU end-to-end test |

## License

1401 Probe is free software under the **GNU General Public License v3.0 or later** ([`LICENSE`](LICENSE)). The
parts it is built from (Linux, GRUB, BusyBox, Python, Alpine packages, firmware files) keep their own licenses,
listed in `THIRD-PARTY-NOTICES.txt` on every stick.

The names **NullMoth** and **NullMoth Systems** and the moth logo are © 2026 NullMoth Systems and are not licensed
(GPL-3.0 section 7(e)). Unmodified copies may carry them as shipped; a modified version must drop them.
