#!/usr/bin/env python3
"""1401 Probe: the offline hardware survey that feeds 1401 (the Windows OpenCore installer) and
NullMoth (the NVIDIA Metal driver).

It runs once, as the only program on the live USB stick, reads the machine, and writes one zip to
the stick. It never mounts an internal drive, never writes NVRAM (efivarfs is mounted read-only),
never writes a device register, and the image it runs in carries no network drivers at all.
Two device-side toggles, both put back right after the read: writing "1" to a sysfs `rom` file lets
the kernel map the expansion ROM (the documented way to read a VBIOS), and a GPU the firmware left
with memory decode off (every laptop dGPU so far) gets the decode bit of its PCI command register
set for the length of the ROM and BOOT register reads, then its original command word back.

Freeze-proof. Before each step that touches hardware, the step's name is written to the stick. If
the machine hard-freezes there, the next boot from the same stick finds the name, records it, and
skips that one step, so a second run gets past it and we learn exactly what froze.

Sealed. Official builds carry a per-version key and every checkpoint writes seal.json: the SHA-256
of every file in the report plus an HMAC over that list. A build from source has no key and writes
an unsealed report, which is still a complete report.

Raw first. The stick records raw truth (VBIOS, CPUID, MSRs, ACPI, PCI config, USB port
topology). Interpretation lives in reader/read_probe.py on our side, so a better reader never
needs a tester to boot the stick again.

Safe before risky. Every stage is flushed to the stick before the next one runs, and the one
stage that can blank or hang a screen (loading the real GPU driver) runs last, after the safe
report is already saved.

Selftest (pure functions, runs anywhere): python3 probe.py --selftest
"""
import contextlib
import ctypes
import fcntl
import glob
import hashlib
import hmac
import json
import mmap
import os
import random
import re
import select
import shlex
import struct
import subprocess
import sys
import threading
import time
import traceback
import zipfile

VERSION = "1.0.1"
SCHEMA = "1401-probe/1"
STICK_LABEL = "PROBE1401"
MARKER = "1401-probe.marker"
MNT = "/mnt/usb"
WORK = "/run/probe"
SEAL_KEY = "/usr/lib/1401-probe/seal.key"   # official builds only; see build.sh
STEP_FILE = ".probe-step"   # in REPORTS/: the hardware steps running right now, one per line
SKIP_FILE = ".probe-skip"   # in REPORTS/: steps that froze a previous run on this stick
EFI_GLOBAL = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
OC_VENDOR = "4d1fda02-38c7-4a6a-9cc6-4bcca8b30102"   # OpenCore's own variables: an existing OpenCore install shows here
APPLE_BOOT = "7c436110-ab2a-4bbb-a880-fe41995c9f82"  # boot-args / csr-active-config live under Apple's boot GUID

CMDLINE = ""
MODE = "full"
OUT = WORK + "/pending"
REP = {}
STICK = None
NAME = None
LOG = []
UI = None
LOCK = threading.Lock()

# An OEM that never set a serial ships one of these. Redacting "Default string" everywhere would shred
# the report, and it identifies no one; the reader records that the OEM left identity unset.
PLACEHOLDERS = {
    "none", "not specified", "not applicable", "default string", "to be filled by o.e.m.",
    "to be filled by oem", "system serial number", "chassis serial number", "base board serial number",
    "0123456789", "123456789", "1234567890", "serial", "n/a", "na", "unknown", "empty", "invalid",
    "not present", "no asset tag", "asset tag", "asset-1234567890", "system product name",
    "03000200-0400-0500-0006-000700080009", "default", "o.e.m.", "oem", "sn", "chassis asset tag",
}


# --------------------------------------------------------------------------------------------- utilities

def rd(path, binary=False, limit=8 << 20):
    try:
        with open(path, "rb") as f:
            b = f.read(limit)
    except OSError:
        return None
    return b if binary else b.decode("utf-8", "replace").strip().strip("\x00").strip()


def rdlink(path):
    try:
        return os.path.basename(os.readlink(path))
    except OSError:
        return None


def save(rel, data):
    p = os.path.join(OUT, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if isinstance(data, (dict, list)):
        data = json.dumps(data, indent=1)
    if isinstance(data, str):
        data = data.encode("utf-8", "replace")
    with open(p, "wb") as f:
        f.write(data or b"")
    return p


def sh(rel, argv, timeout=60):
    """Run a tool with a bound. The output file always exists, and the rc rides into report.json."""
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        out, rc = p.stdout, p.returncode
    except subprocess.TimeoutExpired as e:
        out, rc = (e.stdout or b"") + b"\n[1401-probe] TIMEOUT after %ds\n" % timeout, "timeout"
    except OSError as e:
        out, rc = ("[1401-probe] could not run: %s\n" % e).encode(), "missing"
    with LOCK:
        REP.setdefault("commands", []).append({"file": rel, "argv": argv, "rc": rc})
    if rel:
        save(rel, out)
    return out.decode("utf-8", "replace")


def modprobe(*mods, timeout=30):
    return sh(None, ["modprobe", "-a", "-q"] + list(mods), timeout)


def hx(v, w=4):
    return None if v is None else "0x%0*x" % (w, v)


def ival(s, base=0):
    try:
        return int(s, base)
    except (TypeError, ValueError):
        return None


def clean_name(s, n=40):
    s = re.sub(r"\(R\)|\(TM\)|\(tm\)|®|™|CPU|Processor|Corporation|Technologies|Inc\.|Co\.,? ?Ltd\.?", "", s or "")
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-._")
    return (s[:n].strip("-._")) or "unknown"


# --------------------------------------------------------------------------------------------- screen

class Screen:
    def __init__(self):
        self.serial = None
        if "p1401.serial=1" in CMDLINE:  # the QEMU test reads progress off the serial port
            try:
                self.serial = open("/dev/ttyS0", "w", buffering=1)
            except OSError:
                self.serial = None

    def say(self, s="", end="\n"):
        for f in (sys.stdout, self.serial):
            if f:
                try:
                    f.write(s + end)
                    f.flush()
                except OSError:
                    pass
        if end == "\n":
            LOG.append(s)

    def key(self, seconds):
        """One keypress or None after `seconds`. A missing keyboard must never hang the scan."""
        try:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
        except Exception:
            time.sleep(seconds)
            return None
        try:
            tty.setcbreak(fd)
            r, _, _ = select.select([fd], [], [], seconds)
            return os.read(fd, 1).decode("latin-1") if r else None
        except Exception:
            return None
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def countdown(self, seconds, text):
        for left in range(seconds, 0, -1):
            self.say("\r  %s %3ds " % (text, left), end="")
            k = self.key(1)
            if k is not None:
                self.say("")
                return k
        self.say("")
        return None


# --------------------------------------------------------------------------------------------- privacy

class Secrets:
    """Every identifying value found on the machine. Value-based redaction catches a serial wherever
    it surfaces (dmidecode, lsusb, dmesg, a VBIOS); a pattern list only catches the places we thought of."""

    def __init__(self):
        self.values = {}
        self.short = set()

    def add(self, v, src):
        if not v:
            return
        v = v.strip().strip("\x00").strip()
        low = v.lower()
        if len(v) < 6 or low in PLACEHOLDERS or len(set(low.replace("-", "").replace(" ", ""))) <= 1:
            return
        if len(v) < 8 and re.fullmatch(r"[0-9A-Fa-f]+", v):
            # A short all-hex serial ("0001A2") also occurs inside hex dumps and IDs; replacing it everywhere
            # would shred lspci and the VBIOS. It is too short to identify anyone, so it is counted, not replaced.
            self.short.add(v)
            return
        self.values.setdefault(v, src)

    def gather(self):
        for k in ("product_serial", "board_serial", "chassis_serial", "product_uuid",
                  "board_asset_tag", "chassis_asset_tag"):
            self.add(rd("/sys/class/dmi/id/" + k), "dmi")
        for pat, src in (("/sys/bus/usb/devices/*/serial", "usb"), ("/sys/class/nvme/*/serial", "nvme"),
                         ("/sys/block/*/device/serial", "disk"), ("/sys/class/drm/card*/device/serial_number", "gpu"),
                         ("/sys/class/drm/card*/device/unique_id", "gpu"), ("/sys/bus/thunderbolt/devices/*/unique_id", "thunderbolt"),
                         ("/sys/class/power_supply/*/serial_number", "battery")):
            for f in glob.glob(pat):
                self.add(rd(f), src)
        for f in glob.glob("/sys/block/*/device/wwid") + glob.glob("/sys/block/*/wwid"):
            w = rd(f)
            if w:
                self.add(w, "disk")
                self.add(w.split()[-1], "disk")  # t10.ATA <model> <serial>: the serial is the last token
        for f in glob.glob("/sys/class/net/*/address"):
            a = rd(f)
            if a and a != "00:00:00:00:00:00":
                self.add(a, "mac")


SECRETS = Secrets()
BIN_EXT = (".rom", ".aml", ".bin", ".edid", ".dat")
TEXT_RULES = [
    # (name, regex, replacement): patterns for identifiers that have no sysfs value to learn from
    ("mac", re.compile(rb"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])"), b"XX:XX:XX:XX:XX:XX"),
    # PCIe Device Serial Number capability: a NIC derives it from its MAC, an SSD from its serial.
    ("pcie_dsn", re.compile(rb"(Device Serial Number )[0-9A-Fa-f]{2}(?:-[0-9A-Fa-f]{2}){7}"), rb"\1xx-xx-xx-xx-xx-xx-xx-xx"),
    ("vpd_sn", re.compile(rb"(\[SN\] Serial number: ).*"), rb"\1[REDACTED]"),
    ("serial_line", re.compile(rb"(?im)^(\s*(?:Serial Number|SerialNumber|iSerial\s+\d+|UUID|Asset Tag):?[ \t]+)(?!Not Specified|None|Default string|To Be Filled)(\S.*)$"), rb"\1[REDACTED]"),
]


def redact_tree():
    pats = [(re.compile(re.escape(v.encode("utf-8", "replace")), re.I), src, len(v.encode("utf-8", "replace")))
            for v, src in sorted(SECRETS.values.items(), key=lambda kv: -len(kv[0]))]
    hits = REP.setdefault("redaction", {}).setdefault("hits", {})
    for root, _, files in os.walk(OUT):
        for fn in files:
            p = os.path.join(root, fn)
            try:
                with open(p, "rb") as f:
                    b = f.read()
            except OSError:
                continue
            orig = b
            binary = fn.endswith(BIN_EXT)
            for rx, src, n in pats:
                b, k = rx.subn((b"X" * n) if binary else b"[REDACTED]", b)
                if k:
                    hits[src] = hits.get(src, 0) + k
            if not binary:
                for name, rx, rep in TEXT_RULES:
                    b, k = rx.subn(rep, b)
                    if k:
                        hits[name] = hits.get(name, 0) + k
            if b != orig:
                with open(p, "wb") as f:
                    f.write(b)
    REP["redaction"]["known_values"] = len(SECRETS.values)
    REP["redaction"]["short_values_left"] = len(SECRETS.short)
    REP["redaction"]["sources"] = sorted(set(SECRETS.values.values()))


def scrub_edid(e):
    """Zero the EDID serial fields (bytes 12-15 and any 0xFF descriptor) and fix the checksum."""
    if not e or len(e) < 128 or e[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return e
    b = bytearray(e)
    b[12:16] = b"\x00\x00\x00\x00"
    for off in (54, 72, 90, 108):
        if b[off] == 0 and b[off + 1] == 0 and b[off + 3] == 0xFF:
            b[off + 5:off + 18] = b"0\n" + b" " * 11
    b[127] = (-sum(b[0:127])) & 0xFF
    return bytes(b)


def edid_info(e):
    if not e or len(e) < 128 or e[:8] != b"\x00\xff\xff\xff\xff\xff\xff\x00":
        return None
    m = (e[8] << 8) | e[9]
    info = {"vendor": "".join(chr(((m >> s) & 31) + 64) for s in (10, 5, 0)),
            "product": hx(e[10] | (e[11] << 8)), "year": 1990 + e[17], "version": "%d.%d" % (e[18], e[19]),
            "extensions": e[126]}
    for off in (54, 72, 90, 108):
        if e[off] == 0 and e[off + 1] == 0 and e[off + 3] == 0xFC:
            info["name"] = e[off + 5:off + 18].split(b"\n")[0].decode("latin-1").strip()
    if e[54] | e[55]:
        info["preferred"] = "%dx%d" % (e[56] | ((e[58] & 0xF0) << 4), e[59] | ((e[61] & 0xF0) << 4))
    return info


def zero_pcie_dsn(cfg):
    """Extended config space carries the Device Serial Number capability (0x0003); zero its 8 bytes."""
    if not cfg or len(cfg) < 0x104:
        return cfg
    b = bytearray(cfg)
    off, seen = 0x100, set()
    while 0x100 <= off < len(b) - 4 and off not in seen:
        seen.add(off)
        h = struct.unpack_from("<I", b, off)[0]
        if h in (0, 0xFFFFFFFF):
            break
        if h & 0xFFFF == 0x0003 and off + 12 <= len(b):
            b[off + 4:off + 12] = b"\x00" * 8
        off = h >> 20
    return bytes(b)


# --------------------------------------------------------------------------------------------- the stick

# Safety: the tester's drives. Every block device that is not the probe stick is switched read-only in the
#   kernel (BLKROSET) as soon as it appears, before any stage runs, so no bug in this program can write one byte to
#   it: the kernel refuses the write. Issuing the ioctl opens the device node; it reads no data from the drive.
#   Internal drives are never mounted, never handed to blkid, and never read; identity comes from sysfs only.
BLKROSET, BLKROGET = 0x125D, 0x125E   # linux/fs.h _IO(0x12, 93) / _IO(0x12, 94)
SKIP_BLOCK = ("loop", "ram", "zram")
LOCKED = {}        # block device -> "read-only" or the reason it is not
PROBED = set()     # every device blkid was allowed to read (USB / removable only)
STICK_DONE = False  # the stick search is over: from here on even USB drives that are not the stick are locked


def block_names():
    return sorted(n for n in (os.listdir("/sys/class/block") if os.path.isdir("/sys/class/block") else [])
                  if not n.startswith(SKIP_BLOCK))


def whole_disk(n):
    """sdb1 -> sdb, nvme0n1p2 -> nvme0n1; a whole disk names itself."""
    if os.path.exists("/sys/class/block/%s/partition" % n):
        return os.path.basename(os.path.dirname(os.path.realpath("/sys/class/block/" + n)))
    return n


def usb_or_removable(disk):
    real = os.path.realpath("/sys/class/block/" + disk)
    return "/usb" in real or rd(real + "/removable") == "1"


def lock_disks():
    """Before the stick is found, lock every drive that cannot be the stick (NVMe, SATA, anything internal).
    After, lock everything except the stick's own disk, a second USB drive (a backup disk) included."""
    stick_disk = whole_disk(os.path.basename(STICK)) if STICK else None
    for n in block_names():
        if LOCKED.get(n) == "read-only":
            continue
        d = whole_disk(n)
        if d == stick_disk or (not STICK_DONE and usb_or_removable(d)):
            continue
        try:
            fd = os.open("/dev/" + n, os.O_RDONLY | os.O_NONBLOCK)
            try:
                fcntl.ioctl(fd, BLKROSET, struct.pack("i", 1))
                ro = struct.unpack("i", fcntl.ioctl(fd, BLKROGET, struct.pack("i", 0)))[0]
            finally:
                os.close(fd)
            LOCKED[n] = "read-only" if ro == 1 else "WARNING: still writable (BLKROGET=%d)" % ro
        except OSError as e:
            LOCKED[n] = "WARNING: could not lock: %s" % e


def label_argv(cands):
    """blkid with no device list probes every disk on the machine, internal ones included. With no USB /
    removable candidate there is nothing to ask, so there is no command at all."""
    if not cands:
        return None
    return ["blkid", "-t", "LABEL=" + STICK_LABEL, "-o", "device"] + list(cands)


def find_stick(timeout=45):
    """The stick is found by label through blkid on USB / removable devices only, never by mounting candidates,
    because trying every FAT volume would mount the internal EFI partitions."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        lock_disks()  # drives that enumerate late (NVMe, a slow SATA link) are locked on the next pass
        cands = ["/dev/" + n for n in block_names() if usb_or_removable(whole_disk(n))]
        argv = label_argv(cands)
        PROBED.update(cands)
        devs = sh(None, argv, 20).split() if argv else []
        for dev in devs:
            if dev not in cands:
                continue  # only a USB / removable device can be the stick
            os.makedirs(MNT, exist_ok=True)
            r = subprocess.run(["mount", "-t", "vfat", "-o", "rw,noatime,flush,utf8,nosuid,nodev,noexec", dev, MNT],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if r.returncode == 0 and os.path.exists(os.path.join(MNT, MARKER)):
                os.makedirs(os.path.join(MNT, "REPORTS"), exist_ok=True)
                return dev
            subprocess.run(["umount", MNT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
    return None


def safety_state():
    mounts = [l.split()[:3] for l in (rd("/proc/mounts") or "").splitlines() if l.startswith("/dev/")]
    fs = sorted({l.split()[-1] for l in (rd("/proc/filesystems") or "").splitlines() if l.strip()})
    return {"locked_read_only": dict(LOCKED), "label_probed": sorted(PROBED), "block_mounts": mounts,
            "kernel_filesystems": fs, "stick": STICK}


def fsync_path(p):
    try:
        fd = os.open(p, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass


def checkpoint():
    """Redact, then write <name>.zip to the stick atomically. A stage that dies later cannot cost the
    stages before it: they are already on the stick."""
    lock_disks()
    REP["safety"] = safety_state()
    SECRETS.gather()
    REP["meta"]["checkpoint_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save("probe.log", "\n".join(LOG) + "\n")
    save("report.json", REP)
    redact_tree()
    save("report.json", REP)  # carries the redaction counts; itself redacted on the next pass
    redact_tree()
    seal()  # last: it hashes the files exactly as they will be zipped
    if not STICK:
        return False
    d = os.path.join(MNT, "REPORTS")
    tmp, final = os.path.join(d, NAME + ".zip.tmp"), os.path.join(d, NAME + ".zip")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for root, _, files in os.walk(OUT):
                for fn in sorted(files):
                    p = os.path.join(root, fn)
                    z.write(p, os.path.join(NAME, os.path.relpath(p, OUT)))
        fsync_path(tmp)
        os.replace(tmp, final)
        s = os.path.join(OUT, "SUMMARY.txt")
        if os.path.exists(s):
            with open(s, "rb") as f, open(os.path.join(d, NAME + ".txt"), "wb") as g:
                g.write(f.read())
        fsync_path(d)
        os.sync()
        return True
    except OSError as e:
        REP.setdefault("errors", []).append("checkpoint: %s" % e)
        return False


def seal_manifest(root):
    files = {}
    for r, _, fs in os.walk(root):
        for fn in fs:
            p = os.path.join(r, fn)
            rel = os.path.relpath(p, root)
            if rel == "seal.json":
                continue
            with open(p, "rb") as f:
                files[rel] = hashlib.sha256(f.read()).hexdigest()
    return files


def seal_mac(key, name, files):
    msg = name + "\n" + "".join("%s  %s\n" % (files[k], k) for k in sorted(files))
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).hexdigest()


def seal():
    """seal.json: the SHA-256 of every file as it leaves this machine, and an HMAC over that list and the
    report's name. The key never goes into the report; key_id (a hash of the key) only says which one to use."""
    try:
        key = bytes.fromhex(rd(SEAL_KEY) or "")
    except ValueError:
        key = b""
    files = seal_manifest(OUT)
    s = {"format": 1, "name": NAME, "files": files}
    if key:
        s["key_id"] = hashlib.sha256(b"1401-seal-id" + key).hexdigest()[:16]
        s["mac"] = seal_mac(key, NAME, files)
    else:
        s["sealed"] = False  # built from source: no key in this image
    save("seal.json", s)


# Freeze memory. A stage timeout catches a hung program, never a hung machine: on 2026-09-26 a desktop froze
# solid inside the PCI stage and the report kept only the three stages before it, with no clue which device did it.
ACTIVE = []      # hardware steps running right now, oldest first
SKIP = set()     # steps that froze a previous run on this computer
SKIPPED = []     # steps this run skipped because of that
STEP_LOCK = threading.Lock()
MACHINE = ""


def machine_id():
    """Model-level identity (no serials): a stick moved to another computer must not skip that computer's steps."""
    k = "|".join(rd("/sys/class/dmi/id/" + f) or "" for f in ("sys_vendor", "product_name", "board_vendor", "board_name", "bios_version"))
    return hashlib.sha256(k.encode()).hexdigest()[:12]


def write_steps():
    if not STICK:
        return
    try:
        with open(os.path.join(MNT, "REPORTS", STEP_FILE), "w") as f:
            f.write("".join("%s %s\n" % (MACHINE, t) for t in ACTIVE))
            f.flush()
            os.fsync(f.fileno())  # a hard freeze keeps what fsync already pushed to the flash
    except OSError as e:
        if not REP.get("step_file_error"):
            REP["step_file_error"] = str(e)


@contextlib.contextmanager
def step(tag):
    """Run one hardware step under its name. Yields False, and records it, when this step froze a previous run."""
    if tag in SKIP:
        with STEP_LOCK:
            SKIPPED.append(tag)
        yield False
        return
    with STEP_LOCK:
        ACTIVE.append(tag)
        write_steps()
    try:
        yield True
    finally:
        with STEP_LOCK:
            if tag in ACTIVE:
                ACTIVE.remove(tag)
            write_steps()


def load_steps():
    """A step file that is not empty means the last run on this stick never came back from those steps.
    The innermost steps are blamed; a stage is blamed only when nothing inside it was running."""
    d = os.path.join(MNT, "REPORTS")
    mine = lambda txt: [l.split(" ", 1)[1] for l in (txt or "").splitlines() if l.startswith(MACHINE + " ") and " " in l]
    left = mine(rd(os.path.join(d, STEP_FILE)))
    old = mine(rd(os.path.join(d, SKIP_FILE)))
    froze = [t for t in left if not t.startswith("stage:")] or left
    SKIP.update(old + froze)
    if froze:
        REP["meta"]["previous_run_froze_at"] = froze
        keep = [l for l in (rd(os.path.join(d, SKIP_FILE)) or "").splitlines() if l.strip()]
        keep += ["%s %s" % (MACHINE, t) for t in froze]
        try:
            with open(os.path.join(d, SKIP_FILE), "w") as f:
                f.write("\n".join(keep[-100:]) + "\n")  # bounded: one stick, a handful of machines
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            REP.setdefault("errors", []).append("skip file: %s" % e)
    if SKIP:
        REP["meta"]["skipping"] = sorted(SKIP)
    write_steps()  # clears the file for this run
    return froze


# --------------------------------------------------------------------------------------------- stages

def efivar(name, guid=EFI_GLOBAL):
    b = rd("/sys/firmware/efi/efivars/%s-%s" % (name, guid), binary=True, limit=65536)
    return b[4:] if b and len(b) >= 4 else None  # first 4 bytes are the attributes


def net_state():
    """There are no NIC drivers in this image, so only `lo` can exist. If anything else ever shows
    up, it is forced down and reported, never silently tolerated."""
    ifs = sorted(os.listdir("/sys/class/net")) if os.path.isdir("/sys/class/net") else []
    forced = []
    for i in ifs:
        if i != "lo":
            sh(None, ["ip", "link", "set", "dev", i, "down"], 10)
            forced.append(i)
    up = [i for i in ifs if i != "lo" and rd("/sys/class/net/%s/operstate" % i) not in ("down", None)]
    return {"interfaces": ifs, "forced_down": forced, "offline": not up, "checked_utc": time.strftime("%H:%M:%SZ", time.gmtime())}


def st_meta():
    m = REP["meta"]
    m.update(kernel=rd("/proc/version"), cmdline=CMDLINE, alpine=rd("/etc/alpine-release"),
             rtc=rd("/sys/class/rtc/rtc0/date") and (rd("/sys/class/rtc/rtc0/date") + " " + (rd("/sys/class/rtc/rtc0/time") or "")))
    fw = REP["firmware"]
    efi = os.path.isdir("/sys/firmware/efi")
    fw["boot_mode"] = "UEFI" if efi else "BIOS"
    fw["uefi_bitness"] = rd("/sys/firmware/efi/fw_platform_size")
    for k in ("SecureBoot", "SetupMode", "AuditMode", "DeployedMode"):
        v = efivar(k)
        fw[k] = (v[0] if v else None)
    fw["lockdown"] = rd("/sys/kernel/security/lockdown")
    fw["acpi_pm_profile"] = rd("/sys/firmware/acpi/pm_profile")  # 1 desktop, 2 mobile, 3 workstation ...
    REP["network"] = net_state()


def st_dmi():
    txt = sh("dmi/dmidecode.txt", ["dmidecode"], 30)
    for line in txt.splitlines():
        m = re.match(r"\s*(Serial Number|UUID|Asset Tag):\s*(.+)$", line)
        if m:
            SECRETS.add(m.group(2), "dmi")
    d = {}
    for k in ("bios_vendor", "bios_version", "bios_date", "bios_release", "ec_firmware_release", "board_vendor",
              "board_name", "board_version", "sys_vendor", "product_name", "product_family", "product_version",
              "product_sku", "chassis_type", "chassis_vendor", "chassis_version"):
        d[k] = rd("/sys/class/dmi/id/" + k)
    for k in ("product_serial", "board_serial", "product_uuid"):
        v = rd("/sys/class/dmi/id/" + k)
        low = (v or "").lower()
        d[k + "_state"] = "absent" if not v else ("placeholder" if low in PLACEHOLDERS or len(set(low.replace("-", ""))) <= 1 else "set")
    sh("dmi/dmidecode-memory.txt", ["dmidecode", "-t", "17"], 20)
    REP["dmi"] = d


# CPUID leaves that take a subleaf, and how far to walk them
SUBLEAF = {4: "cache", 7: "eax", 0xB: "topo", 0xD: 64, 0xF: 4, 0x10: 4, 0x12: 4, 0x14: "eax", 0x17: "eax",
           0x18: "eax", 0x1D: 2, 0x1E: 2, 0x1F: "topo", 0x20: "eax", 0x23: 4, 0x24: 2,
           0x8000001D: "cache", 0x80000020: 4, 0x80000026: "topo"}
MSR_INTEL = {0x17: "IA32_PLATFORM_ID", 0x1B: "IA32_APIC_BASE", 0x35: "MSR_CORE_THREAD_COUNT", 0x3A: "IA32_FEATURE_CONTROL",
             0x8B: "IA32_BIOS_SIGN_ID", 0x10A: "IA32_ARCH_CAPABILITIES", 0xCE: "MSR_PLATFORM_INFO",
             0xE2: "MSR_PKG_CST_CONFIG_CONTROL", 0xE4: "MSR_PMG_IO_CAPTURE_BASE", 0x198: "IA32_PERF_STATUS",
             0x199: "IA32_PERF_CTL", 0x1A0: "IA32_MISC_ENABLE", 0x1A2: "MSR_TEMPERATURE_TARGET",
             0x1AD: "MSR_TURBO_RATIO_LIMIT", 0x1AE: "MSR_TURBO_RATIO_LIMIT1", 0x606: "MSR_RAPL_POWER_UNIT",
             0x610: "MSR_PKG_POWER_LIMIT", 0x770: "IA32_PM_ENABLE", 0x771: "IA32_HWP_CAPABILITIES"}
MSR_AMD = {0x1B: "APIC_BASE", 0xC0000080: "EFER", 0xC0010015: "HWCR", 0xC0010058: "MMIO_CFG_BASE",
           0xC0010061: "PSTATE_LIMIT", 0xC0010062: "PSTATE_CTL", 0xC0010063: "PSTATE_STATUS",
           0xC0010114: "VM_CR", 0xC0010299: "RAPL_PWR_UNIT", 0xC0011029: "DE_CFG"}
MSR_AMD.update({0xC0010064 + i: "PSTATE_DEF%d" % i for i in range(8)})


def cpuid_walk(q):
    """Walk every leaf the CPU reports. `q(leaf, sub)` -> (eax, ebx, ecx, edx)."""
    rows = []

    def leaf(l):
        first = q(l, 0)
        rows.append((l, 0, first))
        rule = SUBLEAF.get(l)
        if rule is None:
            return
        if rule == "cache":
            for s in range(1, 16):
                r = q(l, s)
                if r[0] & 0x1F == 0:
                    break
                rows.append((l, s, r))
        elif rule == "topo":
            if (first[2] >> 8) & 0xFF == 0:
                return
            for s in range(1, 8):
                r = q(l, s)
                rows.append((l, s, r))
                if (r[2] >> 8) & 0xFF == 0:
                    break
        else:
            n = min(first[0], 16) if rule == "eax" else rule
            for s in range(1, n + 1 if rule == "eax" else n):
                r = q(l, s)
                if any(r) or l != 0xD:
                    rows.append((l, s, r))

    top = q(0, 0)[0]
    for l in range(0, min(top, 0x3F) + 1):
        leaf(l)
    if q(1, 0)[2] & (1 << 31):  # hypervisor present (the QEMU test; never on bare metal)
        hv = q(0x40000000, 0)[0]
        for l in range(0x40000000, min(hv, 0x40000010) + 1):
            leaf(l)
    ext = q(0x80000000, 0)[0]
    if ext & 0x80000000:
        for l in range(0x80000000, min(ext, 0x80000040) + 1):
            leaf(l)
    return rows


def cpu_decode(rows):
    R = {(l, s): r for l, s, r in rows}
    z = (0, 0, 0, 0)
    a0 = R.get((0, 0), z)
    vendor = struct.pack("<3I", a0[1], a0[3], a0[2]).decode("latin-1")
    eax1, _, ecx1, edx1 = R.get((1, 0), z)
    base_fam = (eax1 >> 8) & 0xF
    fam = base_fam + (((eax1 >> 20) & 0xFF) if base_fam == 0xF else 0)
    model = ((eax1 >> 4) & 0xF) | ((((eax1 >> 16) & 0xF) << 4) if base_fam in (6, 0xF) else 0)
    brand = b"".join(struct.pack("<4I", *R.get((l, 0), z)) for l in (0x80000002, 0x80000003, 0x80000004))
    b7, c7, d7 = R.get((7, 0), z)[1], R.get((7, 0), z)[2], R.get((7, 0), z)[3]
    e1 = R.get((0x80000001, 0), z)
    feats = {"sse4_1": ecx1 >> 19 & 1, "sse4_2": ecx1 >> 20 & 1, "avx": ecx1 >> 28 & 1, "fma": ecx1 >> 12 & 1,
             "f16c": ecx1 >> 29 & 1, "aes": ecx1 >> 25 & 1, "rdrand": ecx1 >> 30 & 1, "x2apic": ecx1 >> 21 & 1,
             "popcnt": ecx1 >> 23 & 1, "vmx": ecx1 >> 5 & 1, "hypervisor": ecx1 >> 31 & 1, "avx2": b7 >> 5 & 1,
             "bmi1": b7 >> 3 & 1, "bmi2": b7 >> 8 & 1, "avx512f": b7 >> 16 & 1, "sha": b7 >> 29 & 1,
             "smap": b7 >> 20 & 1, "hybrid": d7 >> 15 & 1, "svm": e1[2] >> 2 & 1, "lzcnt": e1[2] >> 5 & 1,
             "avx_vnni": R.get((7, 1), z)[0] >> 4 & 1, "sse3": ecx1 & 1, "ssse3": ecx1 >> 9 & 1, "sse2": edx1 >> 26 & 1}
    return {"vendor": vendor, "brand": brand.decode("latin-1").strip("\x00 ").strip(), "family": fam, "model": model,
            "stepping": eax1 & 0xF, "signature": hx(eax1, 8), "features": {k: bool(v) for k, v in feats.items()}}


def st_cpu():
    sh("cpu/lscpu.txt", ["lscpu"], 20)
    sh("cpu/lscpu-e.txt", ["lscpu", "-e"], 20)
    sh("cpu/lscpu-caches.txt", ["lscpu", "-C"], 20)
    save("cpu/cpuinfo.txt", rd("/proc/cpuinfo") or "")
    modprobe("cpuid", "msr")
    cpus = sorted((int(m.group(1)) for m in (re.match(r"cpu(\d+)$", d) for d in os.listdir("/sys/devices/system/cpu")) if m))
    lines, per, cpu0 = [], {}, None
    for c in cpus:
        try:
            fd = os.open("/dev/cpu/%d/cpuid" % c, os.O_RDONLY)
        except OSError as e:
            lines.append("CPU %d: cpuid unavailable (%s)" % (c, e))
            continue
        try:
            def q(l, s, fd=fd):
                try:
                    return struct.unpack("<4I", os.pread(fd, 16, (s << 32) | l))
                except OSError:
                    return (0, 0, 0, 0)
            rows = cpuid_walk(q)
        finally:
            os.close(fd)
        lines.append("CPU %d:" % c)
        lines += ["   0x%08x 0x%02x: eax=0x%08x ebx=0x%08x ecx=0x%08x edx=0x%08x" % ((l, s) + r) for l, s, r in rows]
        R = {(l, s): r for l, s, r in rows}
        core_type = (R.get((0x1A, 0), (0, 0, 0, 0))[0] >> 24) & 0xFF
        top = "/sys/devices/system/cpu/cpu%d/topology/" % c
        per[c] = {"core_type": {0x40: "P", 0x20: "E"}.get(core_type, hx(core_type, 2) if core_type else None),
                  "package": ival(rd(top + "physical_package_id")), "die": ival(rd(top + "die_id")),
                  "core": ival(rd(top + "core_id")), "cluster": ival(rd(top + "cluster_id")),
                  "siblings": rd(top + "core_cpus_list"), "apic": R.get((0xB, 0), (0, 0, 0, 0))[3],
                  "microcode": rd("/sys/devices/system/cpu/cpu%d/microcode/version" % c),
                  "max_khz": ival(rd("/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq" % c))}
        if cpu0 is None:
            cpu0 = rows
    save("cpu/cpuid.txt", "\n".join(lines) + "\n")
    info = cpu_decode(cpu0) if cpu0 else {}
    intel = info.get("vendor") == "GenuineIntel"
    table = MSR_INTEL if intel else MSR_AMD
    mlines, msr0 = [], {}
    for c in cpus:
        try:
            fd = os.open("/dev/cpu/%d/msr" % c, os.O_RDONLY)
        except OSError as e:
            mlines.append("cpu%d msr unavailable (%s)" % (c, e))
            continue
        for reg, nm in sorted(table.items()):
            try:
                v = struct.unpack("<Q", os.pread(fd, 8, reg))[0]
                mlines.append("cpu%-3d 0x%08x %-28s 0x%016x" % (c, reg, nm, v))
                if c == cpus[0]:
                    msr0[reg] = v
            except OSError:
                mlines.append("cpu%-3d 0x%08x %-28s unreadable" % (c, reg, nm))
        os.close(fd)
    save("cpu/msr.txt", "\n".join(mlines) + "\n")
    save("cpu/topology.json", per)
    cores = {(p["package"], p["die"], p["core"]) for p in per.values()}
    types = {}
    for key in cores:
        t = next((p["core_type"] for p in per.values() if (p["package"], p["die"], p["core"]) == key), None)
        types[t or "?"] = types.get(t or "?", 0) + 1
    info.update(threads=len(per), cores=len(cores), packages=len({p["package"] for p in per.values()}),
                core_types=types, microcode=per.get(cpus[0], {}).get("microcode") if cpus else None,
                max_mhz=max([p["max_khz"] or 0 for p in per.values()] or [0]) // 1000 or None)
    if intel and 0xE2 in msr0:
        info["cfg_lock"] = bool(msr0[0xE2] >> 15 & 1)  # MSR 0xE2 bit 15 = CFG Lock: decides OpenCore's AppleXcpmCfgLock
    if intel and 0x3A in msr0:
        info["vmx_locked_enabled"] = bool(msr0[0x3A] & 1) and bool(msr0[0x3A] >> 2 & 1)
    if cpu0 is None:
        # the cpuid driver did not load: name the CPU from /proc/cpuinfo rather than report None (raw leaves absent)
        ci = rd("/proc/cpuinfo") or ""
        blocks = [b for b in ci.split("\n\n") if b.strip()]
        f = lambda k, t=ci: (re.search(r"^%s\s*: (.*)$" % k, t, re.M) or [None, None])[1]
        num = lambda x: int(x) if x and x.strip().isdigit() else None
        fl = set((f("flags") or "").split())
        info.update(vendor=f("vendor_id"), brand=f("model name"), family=num(f("cpu family")), model=num(f("model")),
                    stepping=num(f("stepping")), source="/proc/cpuinfo (the cpuid driver was unavailable)",
                    threads=len(blocks), cores=len({"%s/%s" % (f("physical id", b), f("core id", b) or i) for i, b in enumerate(blocks)}),
                    features={k: k in fl for k in ("sse3", "ssse3", "sse4_1", "sse4_2", "avx", "avx2", "fma", "f16c", "aes",
                                                   "avx512f", "bmi1", "bmi2", "sha_ni", "vmx", "svm", "hypervisor")})
    REP["cpu"] = info


def parse_lspci_vmm(txt):
    devs = {}
    for block in txt.strip().split("\n\n"):
        d = {}
        for line in block.splitlines():
            if ":\t" in line:
                k, v = line.split(":\t", 1)
                d[k.strip()] = v.strip()
        if "Slot" in d:
            devs[d["Slot"]] = d
    return devs


def strip_id(s):
    return re.sub(r"\s*\[[0-9a-fA-F]{4}\]$", "", s or "").strip()


def oc_path(real):
    """OpenCore / gfxutil device path from the sysfs path: PciRoot(<host bridge _UID>)/Pci(dev,fn)/..."""
    m = re.search(r"/pci([0-9a-f]{4,5}):([0-9a-f]{2})(/.*)$", real)
    if not m:
        return None
    root = real[:m.start(3)]
    uid = ival(rd(root + "/firmware_node/uid") or "0", 0) or 0
    hops = re.findall(r"/[0-9a-f]{4,5}:[0-9a-f]{2}:([0-9a-f]{2})\.([0-7])", m.group(3))
    if not hops:
        return None
    return "PciRoot(0x%x)" % uid + "".join("/Pci(0x%x,0x%x)" % (int(d, 16), int(f)) for d, f in hops)


def parse_resource(txt):
    bars = []
    for i, line in enumerate((txt or "").splitlines()[:7]):
        parts = line.split()
        if len(parts) != 3:
            continue
        s, e, fl = (int(x, 16) for x in parts)
        if s == 0 and e == 0:
            continue
        bars.append({"index": i if i < 6 else "rom", "start": hx(s, 1), "size": e - s + 1,
                     "type": "io" if fl & 0x100 else "mem", "prefetch": bool(fl & 0x2000), "is64": bool(fl & 0x100000),
                     "above_4g": s >= (1 << 32)})
    return bars


def st_pci():
    # The header-only listings first: make_name() already ran the same reads before any stage, so they are known
    # safe on this machine. The full decode (every capability, VPD) runs per device afterwards, each under its own
    # step name, because one of those froze a whole machine on 2026-09-26 and took the entire stage with it.
    names = parse_lspci_vmm(sh("pci/lspci-vmm.txt", ["lspci", "-vmm", "-nn", "-D", "-k"], 30))
    sh("pci/lspci-tree.txt", ["lspci", "-tvnn"], 30)
    devs = []
    for bdf in sorted(os.listdir("/sys/bus/pci/devices")):
        with step("pci-sysfs:" + bdf) as go:
            if go:
                devs.append(pci_device(bdf, names))
            else:
                devs.append({"bdf": bdf, "skipped": "this read froze a previous run"})
    REP["pci"] = devs
    save("pci/devices.json", devs)
    checkpoint()  # every device's IDs, BARs and paths are on the stick before any full decode runs
    vvv, xxx = [], []
    for d in devs:
        with step("pci-decode:" + d["bdf"]) as go:
            if not go:
                vvv.append("%s [1401-probe] skipped: decoding this device froze a previous run\n\n" % d["bdf"])
                continue
            vvv.append(sh(None, ["lspci", "-nnvvv", "-D", "-s", d["bdf"]], 20))
            xxx.append(sh(None, ["lspci", "-nnD", "-xxx", "-s", d["bdf"]], 20))  # 256 B: extended space (DSN) stays out
    save("pci/lspci-vvv.txt", "".join(vvv))
    save("pci/lspci-xxx.txt", "".join(xxx))


def pci_device(bdf, names):
    p = "/sys/bus/pci/devices/" + bdf
    real = os.path.realpath(p)
    n = names.get(bdf, {})
    d = {"bdf": bdf, "vendor": rd(p + "/vendor"), "device": rd(p + "/device"),
         "subsystem_vendor": rd(p + "/subsystem_vendor"), "subsystem_device": rd(p + "/subsystem_device"),
         "class": rd(p + "/class"), "revision": rd(p + "/revision"), "driver": rdlink(p + "/driver"),
         "vendor_name": strip_id(n.get("Vendor")), "device_name": strip_id(n.get("Device")),
         "sdevice_name": strip_id(n.get("SDevice")), "class_name": strip_id(n.get("Class")),
         "iommu_group": rdlink(p + "/iommu_group"), "numa_node": rd(p + "/numa_node"), "boot_vga": rd(p + "/boot_vga"),
         "power_state": rd(p + "/power_state"), "link": {k: rd(p + "/" + k) for k in (
             "current_link_speed", "current_link_width", "max_link_speed", "max_link_width")},
         "label": rd(p + "/label"), "acpi_index": rd(p + "/acpi_index"),
         "acpi_path": rd(p + "/firmware_node/path"), "acpi_hid": rd(p + "/firmware_node/hid"),
         "oc_path": oc_path(real), "sysfs": real.replace("/sys/devices/", ""),
         "bars": parse_resource(rd(p + "/resource")),
         "rebar": {os.path.basename(f): rd(f) for f in sorted(glob.glob(p + "/resource*_resize"))}}
    return d


def st_usb():
    sh("usb/lsusb.txt", ["lsusb"], 30)
    sh("usb/lsusb-tree.txt", ["lsusb", "-tv"], 30)
    sh("usb/lsusb-v.txt", ["lsusb", "-v"], 60)
    base = "/sys/bus/usb/devices/"
    devs, ports = [], []
    # with no USB host driver bound the bus directory does not exist; that is a finding, not a crash
    for name in (sorted(os.listdir(base)) if os.path.isdir(base) else []):
        p = base + name
        if ":" in name or not os.path.exists(p + "/idVendor"):
            continue
        real = os.path.realpath(p)
        ctrl = re.findall(r"/([0-9a-f]{4,5}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7])", real)
        ifaces = []
        for i in sorted(glob.glob(p + "/" + name + ":*")):
            ifaces.append({"if": os.path.basename(i).split(":")[1], "class": rd(i + "/bInterfaceClass"),
                           "subclass": rd(i + "/bInterfaceSubClass"), "protocol": rd(i + "/bInterfaceProtocol"),
                           "driver": rdlink(i + "/driver")})
        devs.append({"name": name, "vid": rd(p + "/idVendor"), "pid": rd(p + "/idProduct"), "bcd": rd(p + "/bcdDevice"),
                     "class": rd(p + "/bDeviceClass"), "manufacturer": rd(p + "/manufacturer"), "product": rd(p + "/product"),
                     "speed": rd(p + "/speed"), "version": rd(p + "/version"), "removable": rd(p + "/removable"),
                     "maxchild": rd(p + "/maxchild"), "busnum": rd(p + "/busnum"), "devpath": rd(p + "/devpath"),
                     "controller": ctrl[-1] if ctrl else None, "interfaces": ifaces})
    for port in sorted(glob.glob(base + "*/*:1.0/*-port*")):
        pn = os.path.basename(port)
        hub = pn.rsplit("-port", 1)[0]
        num = pn.rsplit("-port", 1)[1]
        child = ("%s-%s" % (hub[3:], num)) if hub.startswith("usb") else ("%s.%s" % (hub, num))
        loc = {k: rd(port + "/physical_location/" + k) for k in ("panel", "vertical_position", "horizontal_position", "dock", "lid")}
        ports.append({"port": pn, "hub": hub, "number": ival(num), "connect_type": rd(port + "/connect_type"),
                      "location": rd(port + "/location"), "physical": loc if any(loc.values()) else None,
                      "peer": rdlink(port + "/peer"), "over_current": rd(port + "/over_current_count"),
                      "connected": child if os.path.exists(base + child) else None,
                      "hub_speed": rd(base + hub + "/speed")})
    REP["usb"] = {"devices": devs, "ports": ports}
    if not os.path.isdir(base):
        REP["usb"]["error"] = "no USB bus in sysfs: no USB host controller driver bound"
    save("usb/devices.json", devs)
    save("usb/ports.json", ports)


def st_acpi():
    base = "/sys/firmware/acpi/tables"
    tables, skipped = [], []
    for root, _, files in os.walk(base):
        for f in sorted(files):
            src = os.path.join(root, f)
            rel = os.path.relpath(src, base)
            if f[:4] in ("MSDM", "SLIC"):
                skipped.append(rel)  # MSDM holds the machine's Windows product key; it never leaves the machine
                continue
            data = rd(src, binary=True, limit=64 << 20)
            if not data:
                continue
            save("acpi/tables/" + rel.replace("/", "_") + ".aml", data)
            if len(data) >= 36:
                tables.append({"file": rel, "sig": data[:4].decode("latin-1"), "length": len(data), "revision": data[8],
                               "oem_id": data[10:16].decode("latin-1").strip("\x00 "),
                               "oem_table_id": data[16:24].decode("latin-1").strip("\x00 "),
                               "oem_revision": hx(struct.unpack_from("<I", data, 24)[0], 8)})
    devs = []
    for d in sorted(glob.glob("/sys/bus/acpi/devices/*")):
        phys = [os.path.realpath(x).replace("/sys/devices/", "") for x in sorted(glob.glob(d + "/physical_node*"))]
        devs.append({"name": os.path.basename(d), "path": rd(d + "/path"), "hid": rd(d + "/hid"), "modalias": rd(d + "/modalias"),
                     "uid": rd(d + "/uid"), "adr": rd(d + "/adr"), "status": rd(d + "/status"), "physical": phys})
    REP["acpi"] = {"tables": tables, "skipped": skipped, "device_count": len(devs)}
    save("acpi/devices.json", devs)


def boot_option(b):
    if not b or len(b) < 8:
        return None
    attr, fplen = struct.unpack_from("<IH", b, 0)
    desc = b[6:]
    end = next((i for i in range(0, len(desc) - 1, 2) if desc[i:i + 2] == b"\x00\x00"), len(desc))
    return {"active": bool(attr & 1), "description": desc[:end].decode("utf-16-le", "replace")}


def st_efi():
    fw = REP["firmware"]
    e = {k: rd("/sys/firmware/efi/" + k) for k in ("fw_platform_size", "systab", "fw_vendor", "runtime", "config_table")}
    esrt = []
    for d in sorted(glob.glob("/sys/firmware/efi/esrt/entries/*")):
        esrt.append({k: rd(d + "/" + k) for k in ("fw_class", "fw_type", "fw_version", "lowest_supported_fw_version", "last_attempt_status")})
    e["esrt"] = esrt
    vdir = "/sys/firmware/efi/efivars"
    names = sorted(os.listdir(vdir)) if os.path.isdir(vdir) else []
    save("efi/efivar-names.txt", "\n".join(names) + "\n")  # names only; values are read from a whitelist below
    dec = {}
    for k in ("BootCurrent", "Timeout"):
        v = efivar(k)
        dec[k] = struct.unpack_from("<H", v)[0] if v and len(v) >= 2 else None
    v = efivar("BootOrder")
    dec["BootOrder"] = ["%04X" % x for x in struct.unpack("<%dH" % (len(v) // 2), v)] if v else None
    v = efivar("OsIndicationsSupported")
    dec["OsIndicationsSupported"] = hx(struct.unpack_from("<Q", v)[0], 1) if v and len(v) >= 8 else None
    v = efivar("PlatformLang")
    dec["PlatformLang"] = v.decode("latin-1").strip("\x00") if v else None
    dec["boot_options"] = {n[:8]: boot_option(efivar(n[:8])) for n in names if re.match(r"Boot[0-9A-F]{4}-" + EFI_GLOBAL, n)}
    v = efivar("opencore-version", OC_VENDOR)
    dec["opencore_version"] = v.decode("latin-1").strip("\x00") if v else None
    v = efivar("boot-args", APPLE_BOOT)
    dec["apple_boot_args"] = v.decode("latin-1").strip("\x00") if v else None
    e["vars"] = dec
    fw["efi"] = e
    rows = []
    for d in sorted(glob.glob("/sys/firmware/memmap/*"), key=lambda x: int(os.path.basename(x))):
        rows.append("%s\t%s\t%s" % (rd(d + "/start"), rd(d + "/end"), rd(d + "/type")))
    save("efi/memmap.tsv", "start\tend\ttype\n" + "\n".join(rows) + "\n")
    save("efi/iomem.txt", rd("/proc/iomem") or "")


def st_storage():
    sh("storage/lsblk.json", ["lsblk", "-J", "-b", "-o",
                              "NAME,KNAME,TYPE,SIZE,RM,RO,HOTPLUG,ROTA,TRAN,VENDOR,MODEL,REV,PHY-SEC,LOG-SEC"], 30)
    # no FSTYPE / PTTYPE / LABEL / UUID columns: without udev lsblk gets those by reading each drive's superblock
    nvme = []
    for c in sorted(glob.glob("/sys/class/nvme/nvme*")):
        dev = os.path.realpath(c + "/device")
        nvme.append({"name": os.path.basename(c), "model": rd(c + "/model"), "firmware": rd(c + "/firmware_rev"),
                     "transport": rd(c + "/transport"), "pci": os.path.basename(dev) if "/pci" in dev else None})
    disks = []
    for b in sorted(glob.glob("/sys/block/*")):
        n = os.path.basename(b)
        if n.startswith(("loop", "ram", "zram", "dm-")):
            continue
        real = os.path.realpath(b)
        disks.append({"name": n, "bytes": (ival(rd(b + "/size")) or 0) * 512, "removable": rd(b + "/removable"),
                      "rotational": rd(b + "/queue/rotational"), "vendor": rd(b + "/device/vendor"),
                      "model": rd(b + "/device/model"), "rev": rd(b + "/device/rev"),
                      "bus": "usb" if "/usb" in real else ("nvme" if "/nvme" in real else ("ata" if "/ata" in real else "other")),
                      "is_probe_stick": bool(STICK) and whole_disk(os.path.basename(STICK)) == n,
                      "locked": LOCKED.get(n, "not locked (the probe stick)" if STICK and whole_disk(os.path.basename(STICK)) == n else None)})
    REP["storage"] = {"nvme": nvme, "disks": disks}


def st_misc():
    # audio: HDA codecs need no firmware; the cmdline forces the legacy HDA driver so SOF laptops show theirs too
    codecs = []
    for f in sorted(glob.glob("/proc/asound/card*/codec#*")):
        txt = rd(f) or ""
        save("audio/" + f.split("/")[3] + "-" + os.path.basename(f).replace("#", "") + ".txt", txt)
        g = lambda k: (re.search(r"^%s: (.+)$" % k, txt, re.M) or [None, None])[1]
        card = f.split("/")[3]
        codecs.append({"card": card, "codec": g("Codec"), "vendor_id": g("Vendor Id"), "subsystem_id": g("Subsystem Id"),
                       "revision_id": g("Revision Id"), "address": g("Address"),
                       "controller": rdlink("/sys/class/sound/%s/device" % card)})
    save("audio/cards.txt", (rd("/proc/asound/cards") or "") + "\n\n" + (rd("/proc/asound/pcm") or ""))
    REP["audio"] = codecs
    save("input/devices.txt", rd("/proc/bus/input/devices") or "")
    i2c = [{"name": os.path.basename(d), "dev": rd(d + "/name"), "acpi_path": rd(d + "/firmware_node/path"),
            "hid": rd(d + "/firmware_node/hid"), "modalias": rd(d + "/modalias")} for d in sorted(glob.glob("/sys/bus/i2c/devices/*"))]
    hid = [os.path.basename(d) for d in sorted(glob.glob("/sys/bus/hid/devices/*"))]
    ps = [{"name": os.path.basename(d), "type": rd(d + "/type"), "technology": rd(d + "/technology"),
           "manufacturer": rd(d + "/manufacturer"), "model": rd(d + "/model_name")} for d in sorted(glob.glob("/sys/class/power_supply/*"))]
    bl = [{"name": os.path.basename(d), "type": rd(d + "/type"), "max": rd(d + "/max_brightness")} for d in sorted(glob.glob("/sys/class/backlight/*"))]
    tb = [{"name": os.path.basename(d), "vendor": rd(d + "/vendor_name"), "device": rd(d + "/device_name"),
           "generation": rd(d + "/generation")} for d in sorted(glob.glob("/sys/bus/thunderbolt/devices/*")) if os.path.exists(d + "/vendor_name")]
    tpm = [{"name": os.path.basename(d), "version": rd(d + "/tpm_version_major")} for d in sorted(glob.glob("/sys/class/tpm/tpm*"))]
    # SuperIO: OpenCore's SMCSuperIO needs the chip; the hwmon drivers read its ID and say so in the kernel log
    # safe mode skips it: these drivers write the SuperIO "enter config" key to ports 0x2e/0x4e to read the chip ID
    for m in (() if MODE == "safe" else ("it87", "nct6775", "nct6683", "f71882fg", "w83627ehf", "sch5627", "dme1737")):
        modprobe(m, timeout=20)
    hw = [{"name": rd(d + "/name"), "device": os.path.basename(os.path.realpath(d + "/device")) if os.path.exists(d + "/device") else None}
          for d in sorted(glob.glob("/sys/class/hwmon/hwmon*"))]
    sio = [l for l in sh(None, ["dmesg"], 20).splitlines() if re.search(r"(it87|nct67|f71882fg|w83627|sch5627|dme1737).*(Found|found|chip|ID)", l)]
    REP["misc"] = {"i2c": i2c, "hid": hid, "power_supply": ps, "backlight": bl, "thunderbolt": tb, "tpm": tpm,
                   "hwmon": hw, "superio_log": sio[:20]}


def rom_images(rom):
    """Walk the PCI expansion-ROM image chain. has_gop = a UEFI x64 driver is in the ROM, which is what
    draws the OpenCore picker on a discrete card."""
    imgs, off = [], 0
    if rom[:2] != b"\x55\xaa":  # NVIDIA may prefix an IFR/NVGI header; the PCI image starts at a 512-byte boundary
        off = next((o for o in range(512, min(len(rom), 1 << 20), 512) if rom[o:o + 2] == b"\x55\xaa"), -1)
        if off < 0:
            return {"images": [], "has_gop": False, "note": "no 55AA signature"}
    for _ in range(16):
        if off + 0x1A > len(rom) or rom[off:off + 2] != b"\x55\xaa":
            break
        pcir = off + struct.unpack_from("<H", rom, off + 0x18)[0]
        if pcir + 0x18 > len(rom) or rom[pcir:pcir + 4] not in (b"PCIR", b"NPDS"):
            break
        vid, did = struct.unpack_from("<HH", rom, pcir + 4)
        length = struct.unpack_from("<H", rom, pcir + 0x10)[0] * 512
        code, ind = rom[pcir + 0x14], rom[pcir + 0x15]
        img = {"offset": hx(off, 1), "length": length, "code_type": code, "vendor": hx(vid), "device": hx(did)}
        if code == 3 and off + 0x10 <= len(rom):
            sig, sub, mach, comp = struct.unpack_from("<IHHH", rom, off + 4)
            img.update(efi_signature=hx(sig, 1), efi_subsystem=sub, efi_machine=hx(mach), efi_compressed=comp == 1)
        imgs.append(img)
        if ind & 0x80 or length == 0:
            break
        off += length
    gop = any(i["code_type"] == 3 and i.get("efi_signature") == "0xef1" and i.get("efi_machine") == "0x8664" for i in imgs)
    return {"images": imgs, "has_gop": gop}


def read_rom(bdf):
    p = "/sys/bus/pci/devices/%s/rom" % bdf
    if not os.path.exists(p):
        return None, "no rom attribute"
    try:
        with open(p, "w") as f:
            f.write("1")
        with open(p, "rb") as f:
            data = f.read()
        return data, None
    except OSError as e:
        return None, str(e)
    finally:
        try:
            with open(p, "w") as f:
                f.write("0")
        except OSError:
            pass


def nv_boot_regs(bdf):
    """NV_PMC_BOOT_0 (0x0) and NV_PMC_BOOT_42 (0xA00): the chip's own architecture/implementation words,
    the key NullMoth's device table uses. Two 32-bit loads from BAR0; nothing is written."""
    p = "/sys/bus/pci/devices/" + bdf
    cfg = rd(p + "/config", binary=True, limit=64) or b""
    if len(cfg) < 6 or not (struct.unpack_from("<H", cfg, 4)[0] & 0x2):
        return {"skipped": "memory decode off"}
    if (rd(p + "/power_state") or "").startswith("D3"):
        return {"skipped": "device in D3"}
    fd = os.open(p + "/resource0", os.O_RDWR | os.O_SYNC)  # a RW mapping is what ctypes needs to take a 32-bit load
    try:
        m = mmap.mmap(fd, 4096, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=0)
        try:
            a = ctypes.c_uint32.from_buffer(m, 0x0)
            b = ctypes.c_uint32.from_buffer(m, 0xA00)
            boot0, boot42 = a.value, b.value
            del a, b
        finally:
            m.close()
    finally:
        os.close(fd)
    return {"pmc_boot_0": hx(boot0, 8), "pmc_boot_42": hx(boot42, 8)}


@contextlib.contextmanager
def memory_decode(bdf):
    """Laptop dGPUs come up with memory decode off (the firmware only enables the boot display). Then the ROM
    reads back as 0xFF, which the kernel answers with EIO, and BAR0 cannot be read: five of five laptops in the
    first uploads. Set the decode bit of the PCI command register for the length of the reads, then write the
    original command word back. Yields the original word when it had to switch decode on, else None."""
    p = "/sys/bus/pci/devices/%s/config" % bdf
    cfg = rd(p, binary=True, limit=64) or b""
    res = (rd("/sys/bus/pci/devices/%s/resource" % bdf) or "").split()
    if len(cfg) < 6 or struct.unpack_from("<H", cfg, 4)[0] & 0x2 or not res or int(res[0], 16) == 0:
        yield None  # already on, or BAR0 never got an address (then decode would claim nothing)
        return
    before = struct.unpack_from("<H", cfg, 4)[0]
    fd = os.open(p, os.O_RDWR)
    try:
        os.pwrite(fd, struct.pack("<H", before | 0x2), 4)
        yield before
    finally:
        os.pwrite(fd, struct.pack("<H", before), 4)
        os.close(fd)


def st_gpu_raw():
    gpus = []
    for d in REP.get("pci", []):
        if not (d.get("class") or "").startswith("0x03"):
            continue
        bdf = d["bdf"]
        g = {k: d.get(k) for k in ("bdf", "vendor", "device", "subsystem_vendor", "subsystem_device", "revision", "class",
                                   "vendor_name", "device_name", "sdevice_name", "boot_vga", "link", "acpi_path", "oc_path",
                                   "bars", "rebar", "iommu_group", "power_state")}
        with step("gpu-config:" + bdf) as go:
            cfg = rd("/sys/bus/pci/devices/%s/config" % bdf, binary=True, limit=4096) if go else None
            if cfg:
                save("gpu/%s/config.bin" % bdf, zero_pcie_dsn(cfg))  # the command word as the firmware left it
        with step("gpu-rom:" + bdf) as go:
            if not go:
                g["vbios"] = {"error": "skipped: reading this ROM froze a previous run"}
            else:
                with memory_decode(bdf) as was:
                    rom, err = read_rom(bdf)
                if was is not None:
                    g["decode_switched_on"] = hx(was)
                if rom:
                    save("gpu/%s/vbios.rom" % bdf, rom)
                    g["vbios"] = dict(size=len(rom), **rom_images(rom))
                else:
                    g["vbios"] = {"error": err}
        if d.get("vendor") == "0x10de":
            with step("gpu-regs:" + bdf) as go:
                if not go:
                    g["nv"] = {"skipped": "reading BOOT_0 froze a previous run"}
                else:
                    try:
                        with memory_decode(bdf):
                            g["nv"] = nv_boot_regs(bdf)
                    except Exception as e:  # noqa: BLE001 - the error goes into the report
                        g["nv"] = {"error": "%s: %s" % (type(e).__name__, e)}
        gpus.append(g)
    fb = {k: rd("/sys/class/graphics/fb0/" + k) for k in ("name", "virtual_size", "stride", "bits_per_pixel")}
    REP["gpus"] = gpus
    REP["boot_framebuffer"] = fb


def st_dmesg():
    sh("kernel/dmesg-1-before-gpu-drivers.txt", ["dmesg"], 20)
    sh("kernel/lsmod.txt", ["lsmod"], 20)
    save("kernel/cmdline.txt", CMDLINE)
    save("kernel/modules-in-image.txt", "\n".join(sorted(os.path.relpath(os.path.join(r, f), "/lib/modules")
                                                           for r, _, fs in os.walk("/lib/modules") for f in fs if ".ko" in f)))


DEBUGFS_KEEP = ("name", "amdgpu_firmware_info", "amdgpu_vbios", "amdgpu_discovery", "i915_vbt", "i915_opregion",
                "i915_display_info", "i915_capabilities", "vbios.rom", "clients")


def st_gpu_drivers():
    """Last and optional: load the real GPU driver to learn connectors, EDIDs, firmware and IP versions.
    This is the one step that can blank a screen, so it only runs after the safe report is on the stick."""
    os.makedirs("/sys/kernel/debug", exist_ok=True)
    sh(None, ["mount", "-t", "debugfs", "none", "/sys/kernel/debug"], 10)
    vendors = {g.get("vendor") for g in REP.get("gpus", [])}
    tried = []
    for vid, first, second in (("0x10de", "nouveau", None), ("0x1002", "amdgpu", "radeon"), ("0x8086", "i915", "xe")):
        if vid not in vendors:
            continue
        UI.say("    loading %s ..." % first)
        with step("driver:" + first) as go:
            tried.append(first if go else first + " (skipped: it froze a previous run)")
            if go:
                sh("kernel/modprobe-%s.txt" % first, ["modprobe", first], 150)
        unbound = [g["bdf"] for g in REP["gpus"] if g.get("vendor") == vid and not rdlink("/sys/bus/pci/devices/%s/driver" % g["bdf"])]
        if unbound and second:
            with step("driver:" + second) as go:
                tried.append(second if go else second + " (skipped: it froze a previous run)")
                if go:
                    sh("kernel/modprobe-%s.txt" % second, ["modprobe", second], 150)
    time.sleep(6)  # connectors are probed asynchronously after the driver binds
    sh("kernel/dmesg-2-after-gpu-drivers.txt", ["dmesg"], 20)
    conns = []
    for c in sorted(glob.glob("/sys/class/drm/card*-*")):
        n = os.path.basename(c)
        e = rd(c + "/edid", binary=True, limit=65536)
        if e:
            save("gpu/drm/%s.edid" % n, scrub_edid(e))
        conns.append({"connector": n, "status": rd(c + "/status"), "enabled": rd(c + "/enabled"),
                      "modes": (rd(c + "/modes") or "").splitlines()[:8], "edid": edid_info(e)})
    cards = []
    for c in sorted(glob.glob("/sys/class/drm/card[0-9]")):
        dev = c + "/device"
        info = {"card": os.path.basename(c), "pci": os.path.basename(os.path.realpath(dev)), "driver": rdlink(dev + "/driver")}
        for k in ("vbios_version", "mem_info_vram_total", "mem_info_vis_vram_total", "product_name", "product_number",
                  "current_link_speed", "current_link_width"):
            v = rd(dev + "/" + k)
            if v:
                info[k] = v
        ipd = dev + "/ip_discovery/die"
        if os.path.isdir(ipd):
            ips = {}
            for f in glob.glob(ipd + "/*/*/*/major"):
                parts = f.split("/")
                blk = parts[-3]
                ips.setdefault(blk, "%s.%s.%s" % (rd(f), rd(os.path.dirname(f) + "/minor"), rd(os.path.dirname(f) + "/revision")))
            info["ip_discovery"] = ips
        cards.append(info)
    errors = {}
    for d in debugfs_dirs(sorted(glob.glob("/sys/kernel/debug/dri/*"))):
        base = os.path.basename(d)
        try:
            listing = sorted(os.listdir(d))
        except OSError as e:
            listing, errors[base] = [], str(e)
        # every name, so a file we do not keep yet (or one a driver moved) still shows up in the report
        save("gpu/debugfs/dri%s-files.txt" % base, "\n".join(listing) + "\n")
        for f in DEBUGFS_KEEP:
            if f not in listing:
                continue
            try:
                with open(os.path.join(d, f), "rb") as fh:
                    data = fh.read(32 << 20)
            except OSError as e:
                errors["%s/%s" % (base, f)] = str(e)  # v1.0.0 dropped these silently; nouveau's vbios.rom was one
                continue
            if not data:
                errors["%s/%s" % (base, f)] = "empty"
                continue
            ext = ".bin" if f in ("amdgpu_discovery", "i915_vbt", "i915_opregion", "amdgpu_vbios") else (".rom" if f.endswith(".rom") else ".txt")
            save("gpu/debugfs/dri%s-%s%s" % (base, f.replace(".rom", ""), ext), data)
    try:
        top = sorted(os.listdir("/sys/kernel/debug"))
    except OSError:
        top = []
    for t in list(top):
        if t.startswith("nouveau"):  # GSP-RM logs live here on newer kernels; names only
            for r, ds, fs in os.walk("/sys/kernel/debug/" + t):
                top += [os.path.relpath(os.path.join(r, x), "/sys/kernel/debug") for x in ds + fs]
                if r.count("/") > 5:
                    ds[:] = []
    save("gpu/debugfs/top-files.txt", "\n".join(top) + "\n")
    REP["gpu_drivers"] = {"tried": tried, "cards": cards, "connectors": conns, "debugfs_errors": errors}


def debugfs_dirs(dirs):
    """This kernel shows each device three times: dri/<pci address>, dri/<N> and dri/<128+N>. Read it once, by
    address; the numbered ones are only used on a kernel that has no address directories."""
    named = [d for d in dirs if ":" in os.path.basename(d)]
    return named or [d for d in dirs if os.path.basename(d).isdigit() and int(os.path.basename(d)) < 128]


# --------------------------------------------------------------------------------------------- flow

def lsusb_names():
    out = {}
    for line in (rd(os.path.join(OUT, "usb/lsusb.txt")) or "").splitlines():
        m = re.match(r"Bus \d+ Device \d+: ID ([0-9a-f]{4}):([0-9a-f]{4}) ?(.*)$", line)
        if m:
            out["%s:%s" % (m.group(1), m.group(2))] = m.group(3)
    return out


def summary():
    d, c = REP.get("dmi", {}), REP.get("cpu", {})
    L = ["1401 Probe %s - %s" % (VERSION, NAME), ""]
    L.append("Computer  : %s %s  (chassis type %s)" % (d.get("sys_vendor"), d.get("product_name"), d.get("chassis_type")))
    L.append("Board     : %s %s   BIOS %s (%s)" % (d.get("board_vendor"), d.get("board_name"), d.get("bios_version"), d.get("bios_date")))
    ct = c.get("core_types") or {}
    split = " (%s)" % " + ".join("%d%s" % (v, k) for k, v in sorted(ct.items())) if len(ct) > 1 else ""
    L.append("CPU       : %s, %s cores%s / %s threads   [family %s model %s stepping %s]" % (
        c.get("brand"), c.get("cores"), split, c.get("threads"), hx(c.get("family"), 2), hx(c.get("model"), 2), c.get("stepping")))
    for g in REP.get("gpus", []):
        v = g.get("vbios") or {}
        nv = g.get("nv") or {}
        L.append("GPU       : %s %s [%s:%s]%s  VBIOS %s  UEFI GOP %s%s" % (
            g.get("vendor_name"), g.get("device_name"), (g.get("vendor") or "")[2:], (g.get("device") or "")[2:],
            "  (boot display)" if g.get("boot_vga") == "1" else "",
            ("%d KB" % (v["size"] // 1024)) if v.get("size") else "not readable", "yes" if v.get("has_gop") else "no",
            ("  BOOT_0 %s" % nv["pmc_boot_0"]) if nv.get("pmc_boot_0") else ""))
    cls = lambda pref: [p for p in REP.get("pci", []) if (p.get("class") or "").startswith(pref)]
    for p in cls("0x0280"):
        L.append("Wi-Fi     : %s %s [%s:%s]" % (p["vendor_name"], p["device_name"], p["vendor"][2:], p["device"][2:]))
    names = lsusb_names()
    for u in REP.get("usb", {}).get("devices", []):
        if any(i["class"] == "e0" and i["subclass"] == "01" and i["protocol"] == "01" for i in u["interfaces"]):
            k = "%s:%s" % (u["vid"], u["pid"])
            L.append("Bluetooth : %s [%s] (USB)" % (names.get(k) or u.get("product") or "?", k))
    for p in cls("0x0200"):
        L.append("Ethernet  : %s %s [%s:%s]" % (p["vendor_name"], p["device_name"], p["vendor"][2:], p["device"][2:]))
    for a in REP.get("audio", []):
        L.append("Audio     : %s [%s]" % (a.get("codec"), a.get("vendor_id")))
    for n in REP.get("storage", {}).get("nvme", []):
        L.append("NVMe      : %s (fw %s)" % (n.get("model"), n.get("firmware")))
    for dk in REP.get("storage", {}).get("disks", []):
        if dk["bus"] != "nvme" and not dk["is_probe_stick"]:
            L.append("Disk      : %s %s %.0f GB (%s)" % (dk.get("vendor") or "", dk.get("model") or dk["name"], dk["bytes"] / 1e9, dk["bus"]))
    fw = REP.get("firmware", {})
    sb = {1: "ON", 0: "off"}.get(fw.get("SecureBoot"), "unknown")
    cfg = {True: "LOCKED", False: "unlocked"}.get(c.get("cfg_lock"), "n/a")
    L.append("Firmware  : %s %s-bit, Secure Boot %s, CFG Lock %s, VT-d/IOMMU table %s" % (
        fw.get("boot_mode"), fw.get("uefi_bitness"), sb, cfg,
        "present" if any(t["sig"] in ("DMAR", "IVRS") for t in REP.get("acpi", {}).get("tables", [])) else "absent"))
    net = REP.get("network", {})
    L.append("Network   : %s (interfaces: %s)" % ("OFFLINE" if net.get("offline") else "WARNING: NOT OFFLINE", ", ".join(net.get("interfaces", []))))
    bad = ["%s=%s" % (k, v["status"]) for k, v in REP.get("stages", {}).items() if v["status"] != "ok"]
    L.append("Stages    : %s" % ("all ok" if not bad else ", ".join(bad)))
    if REP["meta"].get("previous_run_froze_at"):
        L.append("Last run  : froze at %s (skipped this time)" % ", ".join(REP["meta"]["previous_run_froze_at"]))
    if SKIPPED:
        L.append("Skipped   : %s" % ", ".join(SKIPPED))
    L.append("Saved as  : REPORTS/%s.zip" % NAME if STICK else "Saved as  : NOT SAVED (the USB stick was not found)")
    return "\n".join(L) + "\n"


def run_stage(i, n, title, key, fn, timeout):
    UI.say(" [%2d/%d] %-44s" % (i, n, title), end="")
    if "stage:" + key in SKIP:
        SKIPPED.append("stage:" + key)
        REP["stages"][key] = {"status": "skipped", "seconds": 0, "error": "this stage froze the previous run on this computer"}
        UI.say("SKIPPED (it froze last time)")
        checkpoint()
        return
    lock_disks()
    t0 = time.monotonic()
    box = {}

    def body():
        try:
            fn()
            box["ok"] = True
        except Exception as e:  # noqa: BLE001 - the error goes into report.json
            box["err"] = "%s: %s" % (type(e).__name__, e)
            box["tb"] = traceback.format_exc()

    th = threading.Thread(target=body, daemon=True)
    with step("stage:" + key):  # a hung thread times out below; a hung machine leaves this name on the stick
        th.start()
        th.join(timeout)
    dt = round(time.monotonic() - t0, 1)
    if th.is_alive():
        st = {"status": "timeout", "seconds": dt, "error": "still running after %ds" % timeout}
    elif "ok" in box:
        st = {"status": "ok", "seconds": dt}
    else:
        st = {"status": "error", "seconds": dt, "error": box.get("err"), "traceback": box.get("tb")}
    REP["stages"][key] = st
    UI.say({"ok": "ok  (%.0fs)" % dt, "timeout": "TIMED OUT (kept going)", "error": "ERROR (kept going)"}[st["status"]])
    checkpoint()


def make_name():
    board = rd("/sys/class/dmi/id/board_name") or rd("/sys/class/dmi/id/product_name") or "board"
    vend = (rd("/sys/class/dmi/id/board_vendor") or "").split()[0:1]
    cpu = (re.search(r"^model name\s*:\s*(.+)$", rd("/proc/cpuinfo") or "", re.M) or [None, "cpu"])[1]
    cpu = re.sub(r"(?i)\b(intel|amd|core|processor|cpu|with.*|\d+-core)\b", " ", cpu)
    gpu = "nogpu"
    names = parse_lspci_vmm(sh(None, ["lspci", "-vmm", "-nn", "-D"], 30))
    for n in names.values():
        if re.search(r"\[03[0-9a-f]{2}\]$", n.get("Class", "")):
            dn = strip_id(n.get("Device"))
            m = re.search(r"\[([^\]]+)\]", dn)
            gpu = m.group(1) if m else dn
            if not n.get("Vendor", "").endswith("[8086]"):
                break  # prefer a discrete card over the iGPU in the name
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime())
    return "_".join([clean_name(" ".join(vend + [board]), 36), clean_name(cpu, 24), clean_name(gpu, 28), stamp,
                     "%04x" % random.getrandbits(16)])


def main():
    global CMDLINE, MODE, OUT, NAME, STICK, UI, STICK_DONE, MACHINE
    CMDLINE = rd("/proc/cmdline") or ""
    m = re.search(r"p1401\.mode=(\w+)", CMDLINE)
    MODE = m.group(1) if m else "full"
    UI = Screen()
    REP.update({"schema": SCHEMA, "meta": {"probe_version": VERSION, "mode": MODE,
                                           "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                "firmware": {}, "stages": {}, "redaction": {"hits": {}}})
    UI.say("\033[2J\033[H")
    UI.say("  1401 PROBE %s - hardware scan" % VERSION)
    UI.say("  Offline: this stick has no network drivers. Nothing leaves this computer.")
    UI.say("  Read-only: your drives are locked read-only and never read. Serial numbers are removed.")
    UI.say("")
    SECRETS.gather()
    UI.say(" [ 0] %-49s" % "Finding the 1401 USB stick", end="")
    STICK = find_stick()
    STICK_DONE = True
    lock_disks()
    UI.say("ok" if STICK else "NOT FOUND - the report will only be shown on screen")
    MACHINE = machine_id()
    if STICK:
        froze = load_steps()
        if froze:
            UI.say("")
            UI.say("  The last scan on this computer stopped at: %s" % ", ".join(froze))
            UI.say("  That step is skipped this time, so the scan can finish.")
            UI.say("")
    NAME = make_name()
    OUT = os.path.join(WORK, NAME)
    os.makedirs(OUT, exist_ok=True)
    REP["meta"]["name"] = NAME
    REP["meta"]["stick"] = bool(STICK)
    stages = [("Firmware and boot mode", "meta", st_meta, 20), ("Computer and motherboard", "dmi", st_dmi, 40),
              ("Processor (CPUID, MSRs)", "cpu", st_cpu, 180), ("PCI devices", "pci", st_pci, 180),
              ("USB controllers and ports", "usb", st_usb, 90), ("ACPI tables", "acpi", st_acpi, 90),
              ("UEFI firmware", "efi", st_efi, 60), ("Drives (identity only)", "storage", st_storage, 60),
              ("Audio, input, sensors", "misc", st_misc, 150), ("Graphics cards (VBIOS)", "gpu_raw", st_gpu_raw, 120),
              ("Kernel log", "dmesg", st_dmesg, 30)]
    total = len(stages) + (1 if MODE == "full" else 0)
    for i, (title, key, fn, t) in enumerate(stages, 1):
        run_stage(i, total, title, key, fn, t)
    s = summary()
    save("SUMMARY.txt", s)
    checkpoint()
    UI.say("")
    for line in s.splitlines():
        UI.say("  " + line)
    UI.say("")
    known = {g.get("vendor") for g in REP.get("gpus", [])} & {"0x10de", "0x1002", "0x8086"}
    if MODE == "full" and known:
        UI.say("  Your report is saved. Last step: a graphics-driver test.")
        UI.say("  The screen may flicker or go black for up to 3 minutes. The computer turns itself off when done.")
        UI.say("  If nothing happens for 5 minutes, hold the power button. Your report is already saved.")
        UI.say("  Then start the stick again: it skips the step that froze.")
        # No skip key: the graphics-driver stage is the data NullMoth needs most. A freeze here is
        #   remembered on the stick, so the next run skips only the driver that froze (step "driver:<name>").
        UI.countdown(20, "Starting in (any key starts now)")
        run_stage(total, total, "Graphics drivers (connectors, firmware)", "gpu_drivers", st_gpu_drivers, 420)
    elif MODE == "full":
        REP["stages"]["gpu_drivers"] = {"status": "skipped", "seconds": 0, "error": "no NVIDIA/AMD/Intel display device"}
    elif MODE == "safe":
        REP["stages"]["gpu_drivers"] = {"status": "skipped", "seconds": 0,
                                        "error": "safe scan chosen at the boot menu (nomodeset): the normal scan froze?"}
    REP["network_at_end"] = net_state()
    REP["meta"]["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save("SUMMARY.txt", summary())
    ok = checkpoint()
    if STICK:
        subprocess.run(["umount", MNT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.sync()
    UI.say("")
    UI.say("  DONE. %s" % ("Report saved to the USB stick: REPORTS/%s.zip" % NAME if ok else "WARNING: The report could NOT be saved."))
    UI.say("  Next: 1. Turn Secure Boot back ON in your BIOS setup (Del or F2 as the computer starts).")
    UI.say("        2. Start Windows, open 1401 Probe, click \"I already scanned - find my report\".")
    UI.say("        3. Drag BOTH files (the .zip and the .txt) onto nullmothsystems.com.")
    if MODE != "test":
        UI.countdown(90, "Turning off in (press any key to turn off now)")
    return 0 if ok else 1


# --------------------------------------------------------------------------------------------- selftest

def selftest():
    fails = []

    def check(name, cond, detail=""):
        print(("  ok   " if cond else "  FAIL ") + name + (("   [" + detail + "]") if detail else ""))
        if not cond:
            fails.append(name)

    # EDID: the serial leaves, the checksum stays valid, the monitor's identity survives
    e = bytearray(128)
    e[0:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
    e[8:10] = bytes([0x10, 0xAC])  # "DEL"
    e[12:16] = b"\x12\x34\x56\x78"
    e[72:90] = b"\x00\x00\x00\xff\x00" + b"CN0SECRET77\n "
    e[90:108] = b"\x00\x00\x00\xfc\x00" + b"DELL U2720Q\n "
    e[127] = (-sum(e[:127])) & 0xFF
    s = scrub_edid(bytes(e))
    check("an EDID's serial number (header and 0xFF descriptor) leaves; its checksum stays valid",
        b"SECRET77" not in s and s[12:16] == b"\0\0\0\0" and sum(s) & 0xFF == 0, "sum=%d" % (sum(s) & 0xFF))
    info = edid_info(s)
    check("the monitor's vendor and name survive the scrub", info["vendor"] == "DEL" and info.get("name") == "DELL U2720Q", str(info))
    check("a non-EDID blob passes through untouched", scrub_edid(b"hello") == b"hello")

    # ROM chain: legacy image + UEFI x64 GOP image, last-image bit on the second
    def img(code, last, efi=False, size=1024):
        b = bytearray(size)
        b[0:2] = b"\x55\xaa"
        if efi:
            struct.pack_into("<IHHH", b, 4, 0x0EF1, 11, 0x8664, 1)
        struct.pack_into("<H", b, 0x18, 0x40)
        b[0x40:0x44] = b"PCIR"
        struct.pack_into("<HH", b, 0x44, 0x10DE, 0x2D05)
        struct.pack_into("<H", b, 0x50, size // 512)
        b[0x54] = code
        b[0x55] = 0x80 if last else 0
        return bytes(b)
    r = rom_images(img(0, False) + img(3, True, efi=True))
    check("a ROM with a UEFI x64 image reads has_gop", r["has_gop"] and len(r["images"]) == 2, str([i["code_type"] for i in r["images"]]))
    r2 = rom_images(img(0, True))
    check("a legacy-only ROM reads no gop", not r2["has_gop"])
    r3 = rom_images(b"NVGI" + b"\0" * 508 + img(0, False) + img(3, True, efi=True))
    check("a ROM behind a 512-byte vendor header is still walked", r3["has_gop"], str(r3.get("note")))

    # CPUID decode: known signatures
    def rows_for(eax1):
        return [(0, 0, (0x16, 0x756E6547, 0x6C65746E, 0x49656E69)), (1, 0, (eax1, 0, 0, 0))]
    x = cpu_decode(rows_for(0x000906EC))
    check("CPUID 0x906EC decodes as family 6 model 0x9E stepping 12 (i9-9900K)", (x["family"], x["model"], x["stepping"]) == (6, 0x9E, 12),
        "%s %s %s" % (x["family"], hex(x["model"]), x["stepping"]))
    check("vendor string is reassembled in EBX-EDX-ECX order", x["vendor"] == "GenuineIntel", x["vendor"])
    y = cpu_decode([(0, 0, (0x10, 0x68747541, 0x444D4163, 0x69746E65)), (1, 0, (0x00A20F12, 0, 0, 0))])
    check("CPUID 0xA20F12 decodes as family 0x19 model 0x21 (Zen 3 Vermeer)", (y["family"], y["model"]) == (0x19, 0x21), "%s %s" % (hex(y["family"]), hex(y["model"])))

    # subleaf walk terminates and follows the cache rule
    def q(l, s):
        if l == 0:
            return (4, 0, 0, 0)
        if l == 4:
            return (0x21 if s < 3 else 0, 0, 0, 0)
        if l == 0x80000000:
            return (0x80000001, 0, 0, 0)
        return (0, 0, 0, 0)
    rows = cpuid_walk(q)
    check("leaf 4 is walked until its cache-type field reads 0", sum(1 for l, s, _ in rows if l == 4) == 3, str([(hex(l), s) for l, s, _ in rows]))

    # OpenCore device path
    check("a GPU behind a root port maps to PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)",
        oc_path("/sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0") == "PciRoot(0x0)/Pci(0x1,0x0)/Pci(0x0,0x0)",
        str(oc_path("/sys/devices/pci0000:00/0000:00:01.0/0000:01:00.0")))
    check("a function number > 0 is kept", oc_path("/sys/devices/pci0000:00/0000:00:1f.3") == "PciRoot(0x0)/Pci(0x1f,0x3)")

    # PCIe DSN zeroing
    cfg = bytearray(4096)
    struct.pack_into("<I", cfg, 0x100, 0x0003 | (1 << 16) | (0x140 << 20))
    cfg[0x104:0x10C] = b"\xde\xad\xbe\xef\x01\x02\x03\x04"
    struct.pack_into("<I", cfg, 0x140, 0x0015 | (1 << 16))  # ReBAR cap after it must survive
    z = zero_pcie_dsn(bytes(cfg))
    check("the PCIe Device Serial Number in extended config space is zeroed", z[0x104:0x10C] == b"\0" * 8)
    check("the capability after it (ReBAR) is untouched", z[0x140:0x144] == bytes(cfg[0x140:0x144]))

    # redaction: values anywhere, placeholders never, patterns for the rest
    global OUT
    import tempfile
    OUT = tempfile.mkdtemp()
    REP.clear()
    REP.update({"redaction": {"hits": {}}})
    SECRETS.values.clear()
    SECRETS.add("SECRETSYS789", "dmi")
    SECRETS.add("5ec2e7aa-1111-2222-3333-444455556666", "dmi")
    SECRETS.add("Default string", "dmi")
    SECRETS.add("0000000000", "dmi")
    SECRETS.add("abc", "dmi")
    save("dmi/dmidecode.txt", "Serial Number: SECRETSYS789\nUUID: 5EC2E7AA-1111-2222-3333-444455556666\nProduct Name: Default string\n")
    save("pci/lspci-vvv.txt", "Capabilities: [140] Device Serial Number 00-11-22-ff-ff-33-44-55\n\t[SN] Serial number: PF1234XYZ\n")
    save("x/mac.txt", "link/ether a4:bb:6d:01:02:03 brd\n00: 86 80 d3 10\n")
    save("x/blob.rom", b"\x55\xaaSECRETSYS789\x00")
    redact_tree()
    t = open(os.path.join(OUT, "dmi/dmidecode.txt"), "rb").read()
    check("a known serial is redacted wherever it appears", b"SECRETSYS789" not in t, t.decode())
    check("the SMBIOS UUID is redacted though dmidecode prints it in upper case", b"5EC2E7AA" not in t)
    check("a placeholder ('Default string') is not treated as a secret", b"Default string" in t and "Default string" not in SECRETS.values)
    check("a 3-character or all-zero value is never learned as a secret", "abc" not in SECRETS.values and "0000000000" not in SECRETS.values)
    SECRETS.add("0001A2", "usb")
    check("a short all-hex serial is counted, not replaced (it would shred hex dumps)",
        "0001A2" not in SECRETS.values and "0001A2" in SECRETS.short)
    p = open(os.path.join(OUT, "pci/lspci-vvv.txt"), "rb").read()
    check("a PCIe DSN and a VPD serial are redacted by pattern", b"00-11-22-ff" not in p and b"PF1234XYZ" not in p, p.decode())
    mtxt = open(os.path.join(OUT, "x/mac.txt"), "rb").read()
    check("a MAC address is redacted; a hex dump row is not mistaken for one", b"a4:bb" not in mtxt and b"00: 86 80 d3 10" in mtxt, mtxt.decode())
    b = open(os.path.join(OUT, "x/blob.rom"), "rb").read()
    check("a secret inside a binary is overwritten same-length (offsets survive)", b"SECRET" not in b and len(b) == 15, repr(b))
    check("redaction counts are recorded by source", REP["redaction"]["hits"].get("dmi", 0) >= 3, str(REP["redaction"]["hits"]))
    check("no USB candidate means no blkid at all (a bare blkid reads every internal disk)", label_argv([]) is None)
    a = label_argv(["/dev/sdb", "/dev/sdb1"])
    check("   ... and with candidates blkid is limited to exactly them", a is not None and a[-2:] == ["/dev/sdb", "/dev/sdb1"], str(a))

    # seal: a report sealed here verifies with the key; one changed byte or one added file does not
    global NAME, SEAL_KEY, MNT, STICK, MACHINE
    NAME = "SELFTEST"
    kf = os.path.join(tempfile.mkdtemp(), "seal.key")
    key = bytes(range(32))
    with open(kf, "w") as f:
        f.write(key.hex() + "\n")
    SEAL_KEY = kf
    seal()
    sj = json.load(open(os.path.join(OUT, "seal.json")))
    check("a sealed report verifies against its own files and the build key",
        sj.get("mac") == seal_mac(key, NAME, seal_manifest(OUT)) and "seal.json" not in sj["files"], str(sorted(sj)))
    with open(os.path.join(OUT, "x/mac.txt"), "ab") as f:
        f.write(b"!")
    check("   ... one byte changed after sealing breaks it", sj["mac"] != seal_mac(key, NAME, seal_manifest(OUT)))
    seal()
    sj = json.load(open(os.path.join(OUT, "seal.json")))
    save("x/extra.txt", "added later\n")
    check("   ... one file added after sealing breaks it", sj["mac"] != seal_mac(key, NAME, seal_manifest(OUT)))
    check("   ... and a renamed report breaks it", sj["mac"] != seal_mac(key, "OTHER", sj["files"]))
    SEAL_KEY = os.path.join(os.path.dirname(kf), "missing.key")
    seal()
    sj = json.load(open(os.path.join(OUT, "seal.json")))
    check("a build without a key says sealed: false and carries no mac", sj.get("sealed") is False and "mac" not in sj, str(sorted(sj)))

    # freeze memory, on a fake stick in a temp dir (never a real one)
    MNT = tempfile.mkdtemp()
    os.makedirs(os.path.join(MNT, "REPORTS"))
    STICK = "/dev/selftest"
    MACHINE = "aaaaaaaaaaaa"
    REP["meta"] = {}
    with step("stage:pci"):
        with step("pci-sysfs:0000:01:00.0"):
            left = open(os.path.join(MNT, "REPORTS", STEP_FILE)).read()
    check("a running step is on the stick while it runs", left == "aaaaaaaaaaaa stage:pci\naaaaaaaaaaaa pci-sysfs:0000:01:00.0\n", repr(left))
    check("   ... and gone when it returns", open(os.path.join(MNT, "REPORTS", STEP_FILE)).read() == "")
    with open(os.path.join(MNT, "REPORTS", STEP_FILE), "w") as f:
        f.write(left + "bbbbbbbbbbbb gpu-rom:0000:02:00.0\n")
    froze = load_steps()
    check("after a freeze the innermost step is blamed, not its stage", froze == ["pci-sysfs:0000:01:00.0"], str(froze))
    check("   ... another computer's leftover step is ignored", "gpu-rom:0000:02:00.0" not in SKIP, str(sorted(SKIP)))
    ran = []
    with step("pci-sysfs:0000:01:00.0") as go:
        ran.append(go)
    with step("pci-sysfs:0000:00:02.0") as go:
        ran.append(go)
    check("   ... that step is skipped on the next run, its neighbours still run", ran == [False, True], str(ran))
    check("   ... and the skip is kept on the stick for the run after", "aaaaaaaaaaaa pci-sysfs:0000:01:00.0" in open(os.path.join(MNT, "REPORTS", SKIP_FILE)).read())
    SKIP.clear()
    with open(os.path.join(MNT, "REPORTS", STEP_FILE), "w") as f:
        f.write("aaaaaaaaaaaa stage:usb\n")
    froze = load_steps()
    check("a freeze with only a stage running blames the stage", froze == ["stage:usb"] and "stage:usb" in SKIP, str(froze))
    SKIP.clear()
    del SKIPPED[:]
    STICK = None

    # debugfs: one directory per device
    check("debugfs is read once per device, by PCI address",
        debugfs_dirs(["/d/dri/0", "/d/dri/0000:01:00.0", "/d/dri/128"]) == ["/d/dri/0000:01:00.0"])
    check("   ... and by number (below 128) on a kernel without address directories",
        debugfs_dirs(["/d/dri/0", "/d/dri/1", "/d/dri/128", "/d/dri/129"]) == ["/d/dri/0", "/d/dri/1"])

    print("\n%d failed" % len(fails) if fails else "\nall checks passed")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    if "--lock-disks" in sys.argv:  # init's debug shell: lock every drive before a prompt exists
        STICK_DONE = True
        lock_disks()
        for k, v in sorted(LOCKED.items()):
            print("  %-12s %s" % (k, v))
        sys.exit(0 if all(v == "read-only" for v in LOCKED.values()) else 1)
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - PID 1's child: print the reason on screen, then let init power off
        traceback.print_exc()
        try:
            save("CRASH.txt", traceback.format_exc())
            checkpoint()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(60)
        sys.exit(2)
