#!/usr/bin/env python3
"""1401 Probe for Linux: make the scanner stick, and find the report afterwards.

    python3 1401-probe-linux.py            make the stick (asks before it erases anything)
    python3 1401-probe-linux.py --report   after the scan: copy the report off the stick, open the upload page
    python3 1401-probe-linux.py --selftest

What it does, step by step, and it says each step on screen as it goes:
  1. Finds the stick image: 1401-probe-img.zip next to this file, or downloads it from nullmothsystems.com.
     Either way the zip must match the SHA-256 built into this file, or nothing is written.
  2. Lists USB drives only. Internal disks, the disk Linux is running from, and anything mounted as / or /boot
     are never offered.
  3. You type the drive's name (for example sdb) to confirm. Everything on that stick is erased.
  4. Writes the image, then reads it back and checks it byte for byte.
  5. Tells you what to do in the BIOS (Secure Boot off, boot from USB) and how to get the report back.

It needs root only to write the stick (it re-runs itself with sudo or pkexec for that one step).
Free software, GPL-3.0. Never uploads anything: the report goes to the site only when you send it.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

VERSION = "@VERSION@"
IMG_ZIP_SHA = "@IMG_ZIP_SHA@"  # filled in by the build: the exact zip this release serves
IMG_URL = "https://nullmothsystems.com/download/1401-probe-img.zip"
UPLOAD_URL = "https://nullmothsystems.com/#send"
LABEL = "PROBE1401"
IMG_NAME = "1401-probe.img"
CHUNK = 4 << 20


def say(s=""):
    print(s, flush=True)


def step(n, text):
    say("")
    say("Step %d. %s" % (n, text))


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def lsblk():
    """Every block device as lsblk sees it (JSON). MOUNTPOINTS is newer util-linux; MOUNTPOINT is the fallback."""
    for cols in ("NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MODEL,VENDOR,LABEL,MOUNTPOINTS",
                 "NAME,PATH,SIZE,TYPE,TRAN,RM,HOTPLUG,MODEL,VENDOR,LABEL,MOUNTPOINT"):
        p = subprocess.run(["lsblk", "-J", "-b", "-o", cols], capture_output=True, text=True)
        if p.returncode == 0:
            return json.loads(p.stdout).get("blockdevices", [])
    raise SystemExit("lsblk failed: %s" % p.stderr.strip())


def mounts_of(dev):
    """All mount points of a device and its partitions."""
    mp = dev.get("mountpoints", dev.get("mountpoint"))
    out = [m for m in (mp if isinstance(mp, list) else [mp]) if m]
    for c in dev.get("children") or []:
        out.extend(mounts_of(c))
    return out


def candidates(devs):
    """USB whole disks that are safe to erase. A disk holding /, /boot, /home or swap is never one, even on USB:
    that is the system this script is running on."""
    ok, refused = [], []
    for d in devs:
        if d.get("type") != "disk":
            continue
        name = d.get("name")
        usb = d.get("tran") == "usb" or (str(d.get("rm")) in ("1", "True", "true") and str(d.get("hotplug")) in ("1", "True", "true"))
        mps = mounts_of(d)
        system = [m for m in mps if m in ("/", "/boot", "/boot/efi", "/efi", "/home", "/usr", "/var", "[SWAP]")]
        size = int(d.get("size") or 0)
        if not usb:
            continue
        if system:
            refused.append((name, "Linux is running from it (%s)" % ", ".join(system)))
        elif size < 1 << 30:
            refused.append((name, "smaller than 1 GB"))
        elif size > 512 << 30:
            refused.append((name, "bigger than 512 GB: that is a drive, not a stick"))
        else:
            ok.append(d)
    return ok, refused


def describe(d):
    gb = int(d.get("size") or 0) / 1e9
    who = " ".join(x for x in ((d.get("vendor") or "").strip(), (d.get("model") or "").strip()) if x) or "USB drive"
    return "%-8s %6.1f GB  %s" % (d["name"], gb, who)


def find_zip():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "1401-probe-img.zip"), os.path.join(os.getcwd(), "1401-probe-img.zip"),
              os.path.expanduser("~/Downloads/1401-probe-img.zip")):
        if os.path.isfile(p):
            return p
    return None


def get_zip():
    p = find_zip()
    if p:
        say("  Found %s" % p)
    else:
        p = os.path.join(tempfile.gettempdir(), "1401-probe-img-%s.zip" % VERSION)
        say("  Not found next to this file. Downloading it (about 430 MB) from nullmothsystems.com ...")
        import urllib.request  # noqa: PLC0415 - only this path needs the network
        tmp = p + ".part"
        with urllib.request.urlopen(IMG_URL, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            for b in iter(lambda: r.read(CHUNK), b""):
                f.write(b)
                got += len(b)
                if total:
                    print("\r  %3d%%" % (got * 100 // total), end="", flush=True)
        print()
        os.replace(tmp, p)
    say("  Checking its SHA-256 ...")
    got = sha256_file(p)
    if got != IMG_ZIP_SHA:
        raise SystemExit("  STOP: %s does not match this release (%s, expected %s). Nothing was written.\n"
                         "  Download it again from https://nullmothsystems.com/probe" % (p, got[:16], IMG_ZIP_SHA[:16]))
    say("  Matches this release.")
    return p


def write_image(zpath, dev):
    """Stream the image out of the zip onto the stick, then read it back and compare SHA-256s."""
    with zipfile.ZipFile(zpath) as z:
        info = z.getinfo(IMG_NAME)
        size = info.file_size
        h = hashlib.sha256()
        fd = os.open(dev, os.O_WRONLY | getattr(os, "O_EXCL", 0))
        try:
            with z.open(info) as src:
                done = 0
                for b in iter(lambda: src.read(CHUNK), b""):
                    os.write(fd, b)
                    h.update(b)
                    done += len(b)
                    print("\r  writing %3d%%" % (done * 100 // size), end="", flush=True)
            os.fsync(fd)
        finally:
            os.close(fd)
    print()
    want = h.hexdigest()
    say("  Reading it back to check ...")
    r = hashlib.sha256()
    with open(dev, "rb", buffering=0) as f:
        left = size
        while left:
            b = f.read(min(CHUNK, left))
            if not b:
                break
            r.update(b)
            left -= len(b)
    if r.hexdigest() != want:
        raise SystemExit("  STOP: the stick does not read back what was written. Try another stick or USB port.")
    say("  Verified: the stick holds exactly the image.")


def unmount_all(d):
    for m in mounts_of(d):
        subprocess.run(["umount", m], capture_output=True)


def make_stick():
    say("1401 Probe %s for Linux: makes the scanner stick." % VERSION)
    step(1, "The stick image")
    zpath = get_zip()
    step(2, "Choose the USB stick (it will be erased)")
    ok, refused = candidates(lsblk())
    for name, why in refused:
        say("  not offered: %s (%s)" % (name, why))
    if not ok:
        raise SystemExit("  No USB stick found. Plug one in (1 GB or bigger) and run this again.")
    for d in ok:
        say("  " + describe(d))
    names = {d["name"]: d for d in ok}
    pick = input("\n  Type the name of the stick to erase (for example %s), or press Enter to stop: " % ok[0]["name"]).strip()
    if pick.startswith("/dev/"):
        pick = pick[5:]
    if pick not in names:
        raise SystemExit("  Stopped. Nothing was written.")
    d = names[pick]
    say("  EVERYTHING on %s will be erased." % describe(d).strip())
    if input("  Type ERASE to continue: ").strip() != "ERASE":
        raise SystemExit("  Stopped. Nothing was written.")
    step(3, "Writing the stick")
    if os.geteuid() != 0:
        say("  Writing a whole drive needs administrator rights; asking for them now.")
        helper = shutil.which("sudo") or shutil.which("pkexec")
        if not helper:
            raise SystemExit("  Neither sudo nor pkexec is available. Run this as root.")
        rc = subprocess.call([helper, sys.executable, os.path.abspath(__file__), "--write", zpath, d["path"]])
        if rc:
            raise SystemExit("  Writing failed (code %d)." % rc)
    else:
        unmount_all(d)
        write_image(zpath, d["path"])
    step(4, "Start the PC from the stick")
    say("  1. Restart. Open the BIOS setup (usually Del or F2 while it starts).")
    say("  2. Turn Secure Boot OFF (the scanner is not signed by Microsoft) and pick the USB stick to boot from.")
    say("  3. The scan runs by itself and turns the PC off when it is done. It never touches your drives.")
    say("  4. Back in Linux, plug the stick in and run:  python3 %s --report" % os.path.basename(__file__))
    say("  5. Turn Secure Boot back on if you use it.")


def find_report():
    say("1401 Probe %s for Linux: finds your report." % VERSION)
    for d in lsblk():
        for part in [d] + (d.get("children") or []):
            if part.get("label") != LABEL:
                continue
            mps = mounts_of(part)
            if not mps and shutil.which("udisksctl"):
                subprocess.run(["udisksctl", "mount", "-b", part["path"]], capture_output=True)
                mps = mounts_of(next((c for c in [x for dd in lsblk() for x in [dd] + (dd.get("children") or [])]
                                      if c.get("path") == part["path"]), part))
            if not mps:
                raise SystemExit("  Found the stick (%s) but could not open it. Mount it, then run this again." % part["path"])
            src = os.path.join(mps[0], "REPORTS")
            files = sorted(f for f in os.listdir(src) if f.endswith((".zip", ".txt"))) if os.path.isdir(src) else []
            if not files:
                raise SystemExit("  The stick has no report yet. Start the PC from it first.")
            dst = os.path.expanduser("~/1401-Probe-Reports")
            os.makedirs(dst, exist_ok=True)
            for f in files:
                shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
                say("  copied %s" % f)
            say("\n  Your report is in %s" % dst)
            say("  Send BOTH files (the .zip and the .txt) at %s" % UPLOAD_URL)
            if shutil.which("xdg-open"):
                subprocess.run(["xdg-open", UPLOAD_URL], capture_output=True)
            return
    raise SystemExit("  No %s stick found. Plug in the stick you scanned with." % LABEL)


def selftest():
    fails = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + (("   [%s]" % detail) if detail else ""))
        if not cond:
            fails.append(name)
    gb = 1 << 30
    devs = [
        {"name": "nvme0n1", "path": "/dev/nvme0n1", "type": "disk", "tran": "nvme", "rm": False, "hotplug": False, "size": 500 * gb,
         "children": [{"name": "nvme0n1p2", "type": "part", "mountpoints": ["/"]}]},
        {"name": "sdb", "path": "/dev/sdb", "type": "disk", "tran": "usb", "rm": True, "hotplug": True, "size": 16 * gb,
         "children": [{"name": "sdb1", "type": "part", "mountpoints": ["/media/u/STICK"]}]},
        {"name": "sdc", "path": "/dev/sdc", "type": "disk", "tran": "usb", "rm": False, "hotplug": True, "size": 64 * gb,
         "children": [{"name": "sdc1", "type": "part", "mountpoints": ["/"]}]},
        {"name": "sdd", "path": "/dev/sdd", "type": "disk", "tran": "usb", "rm": False, "hotplug": True, "size": 2000 * gb},
        {"name": "sde", "path": "/dev/sde", "type": "disk", "tran": "usb", "rm": True, "hotplug": True, "size": 256 << 20},
        {"name": "sda", "path": "/dev/sda", "type": "disk", "tran": "sata", "rm": False, "hotplug": False, "size": 1000 * gb},
        {"name": "sdf", "path": "/dev/sdf", "type": "disk", "tran": None, "rm": "1", "hotplug": "1", "size": 8 * gb,
         "children": [{"name": "sdf1", "type": "part", "mountpoint": None}]},
    ]
    ok, refused = candidates(devs)
    names = sorted(d["name"] for d in ok)
    rnames = dict(refused)
    check("a mounted 16 GB USB stick is offered (it is unmounted before writing)", "sdb" in names, names)
    check("a removable hot-plug disk with no transport reported is offered", "sdf" in names, names)
    check("the internal NVMe holding / is never offered", "nvme0n1" not in names)
    check("a SATA disk is never offered", "sda" not in names)
    check("a USB disk Linux is running from is refused, and says why", "sdc" in rnames and "running from" in rnames["sdc"], rnames.get("sdc"))
    check("a 2 TB USB drive is refused as a drive, not a stick", "sdd" in rnames, rnames.get("sdd"))
    check("a stick under 1 GB is refused", "sde" in rnames, rnames.get("sde"))
    # write + read-back on a file standing in for the stick
    t = tempfile.mkdtemp()
    img = os.urandom(3 * CHUNK + 12345)
    zp = os.path.join(t, "z.zip")
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(IMG_NAME, img)
    dev = os.path.join(t, "stick")
    open(dev, "wb").close()
    real_open = os.open
    os.open = lambda p, fl, *a: real_open(p, fl & ~getattr(os, "O_EXCL", 0), *a)  # a plain file cannot be opened O_EXCL twice
    try:
        write_image(zp, dev)
        check("the image is written byte for byte and verified by reading it back", open(dev, "rb").read() == img)
    finally:
        os.open = real_open
        shutil.rmtree(t, ignore_errors=True)
    check("the build filled in the release's image SHA-256", len(IMG_ZIP_SHA) == 64 and "@" not in IMG_ZIP_SHA
          or IMG_ZIP_SHA == "@IMG_ZIP_SHA@", IMG_ZIP_SHA[:16])
    print("\n%d failed" % len(fails) if fails else "\nall checks passed")
    return 1 if fails else 0


def main():
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        return selftest()
    if a[:1] == ["--report"]:
        return find_report()
    if a[:1] == ["--write"] and len(a) == 3:  # the root half of make_stick, re-run through sudo/pkexec
        zpath, devpath = a[1], a[2]
        if sha256_file(zpath) != IMG_ZIP_SHA:
            raise SystemExit("  STOP: the image changed since it was checked. Nothing was written.")
        d = next((x for x in candidates(lsblk())[0] if x.get("path") == devpath), None)
        if d is None:
            raise SystemExit("  STOP: %s is no longer a USB stick this tool may erase. Nothing was written." % devpath)
        unmount_all(d)
        write_image(zpath, devpath)
        return 0
    if a:
        say(__doc__)
        return 2
    make_stick()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit("\n  Stopped.")
