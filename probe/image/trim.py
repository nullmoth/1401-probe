#!/usr/bin/env python3
"""Build-time trimmer for the 1401 Probe image (runs inside the Alpine build container).

  trim.py modules <src_moddir> <dst_moddir>     copy the kernel modules minus every network path
  trim.py firmware <kver> <moddir> <fw_src> <fw_dst>   copy exactly the firmware the GPU drivers ask for
  trim.py linkage <root> <control_lib> <control_bin>   every ELF's libraries resolve inside the image
  trim.py radios <kroot> <kver> <fw_src> <stage>       the OPT-IN radio pack: the deleted Wi-Fi/BT/Ethernet drivers
                                                       + their firmware, kept OUT of the image (drivers/ on the stick)

Offline by construction. The image cannot reach a network because no driver for one exists in it:
every Ethernet, Wi-Fi, Bluetooth, USB-net, Thunderbolt-net, FireWire-net, InfiniBand and modem driver
and the whole kernel/net tree is deleted here. The runtime check (only `lo` may exist) and the QEMU test
(an e1000e card is present and no interface appears) prove it from the other side.
"""
import json
import os
import re
import shutil
import struct
import subprocess
import sys

# every path that can bring up a network interface or radio
DENY = ("kernel/net/", "kernel/drivers/net/", "kernel/drivers/bluetooth/", "kernel/drivers/staging/",
        "kernel/drivers/infiniband/", "kernel/drivers/media/", "kernel/drivers/isdn/", "kernel/drivers/atm/",
        "kernel/drivers/vhost/", "kernel/drivers/nfc/", "kernel/drivers/usb/gadget/", "kernel/drivers/ntb/",
        "kernel/drivers/firewire/firewire-net", "kernel/drivers/hv/", "kernel/drivers/xen/",
        "kernel/drivers/tty/n_gsm", "kernel/drivers/s390/",
        # nova (the Rust NVIDIA driver) is experimental; nouveau is the one whose output we read
        "kernel/drivers/gpu/nova-core/", "kernel/drivers/gpu/drm/nova/")
# drm_display_helper needs cec for DP-CEC; without it nouveau/amdgpu/i915 cannot load at all.
# snd_usb_audio needs the media controller (mc); it names USB audio interfaces for the audio map.
# rfkill stays out: only laptop hotkey drivers (thinkpad_acpi, asus_wmi, ...) need it, and DMI/ACPI already carry their data.
ALLOW = ("kernel/drivers/media/cec/", "kernel/drivers/media/rc/rc-core", "kernel/drivers/media/mc/")
# configfs: xe (Intel Arc / Lunar Lake+) will not load without it; the CRITICAL list check catches that.
KEEP_FS = ("kernel/fs/fat/", "kernel/fs/nls/", "kernel/fs/efivarfs/", "kernel/fs/configfs/")
# the modules the scan depends on: if one of these is lost, the build fails
CRITICAL = ("nouveau", "amdgpu", "radeon", "i915", "xe", "snd_hda_intel", "snd_hda_codec_realtek", "usb_storage",
            "uas", "sd_mod", "nvme", "ahci", "vfat", "efivarfs", "cpuid", "msr", "xhci_pci", "ehci_pci",
            "it87", "nct6775", "k10temp", "coretemp", "nls_cp437", "nls_utf8")
GPU = ("nouveau", "amdgpu", "radeon", "i915", "xe")


def modname(rel):
    return re.sub(r"\.ko(\.[a-z]+)?$", "", os.path.basename(rel)).replace("-", "_")


def denied(rel):
    if rel.startswith(ALLOW):
        return False
    if rel.startswith("kernel/fs/") and not rel.startswith(KEEP_FS):
        return True
    return rel.startswith(DENY)


def modules(src, dst):
    deps = {}
    with open(os.path.join(src, "modules.dep")) as f:
        for line in f:
            k, _, v = line.partition(":")
            deps[k.strip()] = v.split()
    drop = {m for m in deps if denied(m)}
    # a kept module that needs a dropped one could never load: drop it too, and say so
    cascade = []
    changed = True
    while changed:
        changed = False
        for m, ds in deps.items():
            if m not in drop and any(d in drop for d in ds):
                drop.add(m)
                cascade.append("%s (needs %s)" % (modname(m), ",".join(modname(d) for d in ds if d in drop)))
                changed = True
    lost = [c for c in CRITICAL if not any(modname(m) == c for m in deps if m not in drop)]
    present = {modname(m) for m in deps}
    missing_crit = [c for c in lost if c in present]
    if missing_crit:
        sys.exit("error: trim would remove critical modules: %s\ncascade: %s" % (missing_crit, cascade))
    for m in deps:
        if m in drop:
            continue
        s, d = os.path.join(src, m), os.path.join(dst, m)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(s, d)
    for f in os.listdir(src):
        if f.startswith("modules.") and f.endswith((".order", ".builtin", ".builtin.modinfo", ".builtin.ranges")):
            shutil.copy2(os.path.join(src, f), os.path.join(dst, f))
    netleft = [m for m in deps if m not in drop and re.search(r"/(net|wireless|bluetooth)/", m)]
    print("modules: %d kept, %d dropped (%d by cascade)" % (len(deps) - len(drop), len(drop), len(cascade)))
    for c in cascade[:40]:
        print("  cascade-dropped:", c)
    print("  critical absent from this kernel build (not an error):", [c for c in lost if c not in present])
    if netleft:
        sys.exit("error: network-looking modules survived: %s" % netleft[:20])
    print("  network-looking modules left: 0")


def materialize(src, dst, rel, depth=0):
    """Copy firmware `rel`, recreating every symlink on the way (nvidia/gb206 -> gb202 is a directory link),
    so the image holds each blob once. Returns the bytes copied, or None if the file does not exist."""
    if depth > 16:
        return None
    parts = rel.split("/")
    cur = ""
    for i, part in enumerate(parts):
        cur = cur + "/" + part if cur else part
        s = os.path.join(src, cur)
        if os.path.islink(s):
            tgt = os.readlink(s)
            d = os.path.join(dst, cur)
            if not os.path.lexists(d):
                os.makedirs(os.path.dirname(d), exist_ok=True)
                os.symlink(tgt, d)
            rest = "/".join(parts[i + 1:])
            nxt = os.path.normpath(os.path.join(os.path.dirname(cur), tgt))
            return materialize(src, dst, nxt + ("/" + rest if rest else ""), depth + 1)
    s, d = os.path.join(src, cur), os.path.join(dst, cur)
    if not os.path.isfile(s):
        return None
    if os.path.exists(d):
        return 0
    os.makedirs(os.path.dirname(d), exist_ok=True)
    shutil.copy2(s, d)
    return os.path.getsize(d)


def gsp_version(name):
    m = re.search(r"/gsp/.*-(\d+\.\d+(?:\.\d+)*)\.bin$", name)
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def firmware(kver, moddir_root, src, dst):
    total, report = 0, []
    for mod in GPU:
        try:
            names = subprocess.run(["modinfo", "-b", moddir_root, "-k", kver, "-F", "firmware", mod],
                                   capture_output=True, text=True, check=True).stdout.split()
        except subprocess.CalledProcessError as e:
            sys.exit("error: modinfo %s failed: %s" % (mod, e.stderr))
        if mod == "nouveau":
            # nouveau lists every GSP-RM release it can speak (1.3 GB dereferenced). Keep only the newest
            # release present per chip; the driver walks its version list and loads the first it finds.
            best = {}
            for n in names:
                v = gsp_version(n)
                if v:
                    chip = n.split("/")[1]
                    best[chip] = max(best.get(chip, v), v)
            names = [n for n in names if not gsp_version(n) or gsp_version(n) == best[n.split("/")[1]]]
            report.append("nouveau GSP kept: " + ", ".join("%s=%s" % (c, ".".join(map(str, v))) for c, v in sorted(best.items())))
        got = miss = size = 0
        for n in names:
            for cand in (n + ".zst", n, n + ".xz"):
                b = materialize(src, dst, cand)
                if b is not None:
                    got += 1
                    size += b
                    break
            else:
                miss += 1
        total += size
        report.append("%-8s %4d files  %6.1f MiB new  (%d named but absent from linux-firmware)" % (mod, got, size / 1048576, miss))
    for r in report:
        print("  " + r)
    print("firmware total: %.1f MiB" % (total / 1048576))


# the device drivers the opt-in radio trace may load; their cores (cfg80211, mac80211, bluetooth, libphy, ...)
# come in through modules.dep. None of this is in the image: the probe unpacks it only after the user says yes.
RADIO = ("kernel/drivers/net/wireless/", "kernel/drivers/net/ethernet/", "kernel/drivers/net/usb/",
         "kernel/drivers/bluetooth/")


def newest_ucode(names, present):
    """iwlwifi (and others) list every firmware API they speak, newest first in the driver's own search. Keep
    the newest present file per prefix: the driver loads exactly that one, the rest is dead weight on the stick."""
    best, out = {}, []
    for n in names:
        m = re.match(r"^(.*)-(\d+)\.ucode$", n)
        if m and present(n):
            k = m.group(1)
            if int(m.group(2)) > best.get(k, (-1, None))[0]:
                best[k] = (int(m.group(2)), n)
        elif not m:
            out.append(n)
    return out + [n for _, n in best.values()]


def radios(kroot, kver, fw_src, stage):
    src = os.path.join(kroot, "lib/modules", kver)
    deps = {}
    with open(os.path.join(src, "modules.dep")) as f:
        for line in f:
            k, _, v = line.partition(":")
            deps[k.strip()] = v.split()
    top = sorted(m for m in deps if m.startswith(RADIO))
    want, todo = set(), list(top)
    while todo:
        m = todo.pop()
        if m not in want:
            want.add(m)
            todo.extend(deps.get(m, []))
    # only what the image deleted: everything else these need is already in the image
    ship = sorted(m for m in want if denied(m))
    for m in ship:
        d = os.path.join(stage, "modules", m)
        os.makedirs(os.path.dirname(d), exist_ok=True)
        shutil.copy2(os.path.join(src, m), d)
    got = miss = size = 0
    fdst = os.path.join(stage, "firmware")

    def present(n):
        return any(os.path.lexists(os.path.join(fw_src, c)) for c in (n + ".zst", n, n + ".xz"))
    for m in top:
        names = subprocess.run(["modinfo", "-b", kroot, "-k", kver, "-F", "firmware", os.path.join(src, m)],
                               capture_output=True, text=True).stdout.split()
        for n in newest_ucode(names, present):
            for cand in (n + ".zst", n, n + ".xz"):
                b = materialize(fw_src, fdst, cand)
                if b is not None:
                    got += 1
                    size += b
                    break
            else:
                miss += 1
    with open(os.path.join(stage, "pack.json"), "w") as f:
        json.dump({"name": "radios-" + kver, "kind": "radios", "kernel": kver,
                   "drivers": [modname(m) for m in top], "modules": len(ship)}, f)
    print("radio pack: %d driver modules (+%d cores), firmware %d files %.1f MiB (%d named but absent)"
          % (len(top), len(ship) - len([m for m in ship if m in top]), got, size / 1048576, miss))
    if not any(modname(m) == "cfg80211" for m in ship) or not any(modname(m) == "btusb" for m in ship):
        sys.exit("error: radio pack has no cfg80211 or btusb: the Wi-Fi/Bluetooth trace could load nothing")


def elf_deps(path):
    """(interpreter, [DT_NEEDED], [RPATH/RUNPATH dirs]) of a 64-bit little-endian ELF executable or shared
    object; None for anything else (kernel modules are ET_REL and carry no dynamic section)."""
    with open(path, "rb") as f:
        h = f.read(64)
        if len(h) < 64 or h[:4] != b"\x7fELF" or h[4] != 2 or h[5] != 1:
            return None
        if struct.unpack_from("<H", h, 16)[0] not in (2, 3):  # ET_EXEC, ET_DYN
            return None
        phoff = struct.unpack_from("<Q", h, 32)[0]
        phentsize, phnum = struct.unpack_from("<HH", h, 54)
        f.seek(phoff)
        ph = f.read(phentsize * phnum)
        loads, dyn, interp = [], None, None
        for i in range(phnum):
            p_type, _, p_offset, p_vaddr, _, p_filesz, _, _ = struct.unpack_from("<IIQQQQQQ", ph, i * phentsize)
            if p_type == 1:
                loads.append((p_vaddr, p_filesz, p_offset))
            elif p_type == 2:
                dyn = (p_offset, p_filesz)
            elif p_type == 3:
                f.seek(p_offset)
                interp = f.read(p_filesz).rstrip(b"\0").decode()
        if dyn is None:
            return interp, [], []
        f.seek(dyn[0])
        d = f.read(dyn[1])
        ents = []
        for o in range(0, len(d) - 15, 16):
            t, v = struct.unpack_from("<qQ", d, o)
            if t == 0:
                break
            ents.append((t, v))
        strtab = next((v for t, v in ents if t == 5), None)
        base = next((off + strtab - va for va, sz, off in loads if strtab is not None and va <= strtab < va + sz), None)
        if base is None:
            raise ValueError("DT_STRTAB outside every PT_LOAD")

        def s(i):
            f.seek(base + i)
            return f.read(4096).split(b"\0", 1)[0].decode()
        return interp, [s(v) for t, v in ents if t == 1], [x for t, v in ents if t in (15, 29) for x in s(v).split(":") if x]


def in_root(root, path, hops=0):
    """Resolve an absolute image path inside `root`, following symlinks as the image would see them.
    Note: os.path.exists() on root+path follows an absolute link out into the build container and says yes."""
    parts = [p for p in path.split("/") if p]
    cur = root
    for i, part in enumerate(parts):
        nxt = os.path.join(cur, part)
        if os.path.islink(nxt):
            if hops > 40:
                return None
            t = os.readlink(nxt)
            here = "/" + os.path.relpath(cur, root) if cur != root else "/"
            target = os.path.normpath(os.path.join(t if t.startswith("/") else os.path.join(here, t), *parts[i + 1:]))
            return in_root(root, target, hops + 1)
        cur = nxt
    return cur if os.path.exists(cur) else None


def link_scan(root, pretend_missing=()):
    """Every ELF in the image whose interpreter or a DT_NEEDED library does not resolve inside the image."""
    skip = (os.path.join(root, "lib/modules"), os.path.join(root, "lib/firmware"))
    count, broken = 0, {}
    for top in ("bin", "sbin", "lib", "usr"):
        for dp, dns, fns in os.walk(os.path.join(root, top)):
            dns[:] = [x for x in dns if not os.path.join(dp, x).startswith(skip)]
            for fn in fns:
                p = os.path.join(dp, fn)
                if os.path.islink(p) or not os.path.isfile(p):
                    continue
                rel = "/" + os.path.relpath(p, root)
                try:
                    e = elf_deps(p)
                except (OSError, struct.error, ValueError, UnicodeDecodeError) as ex:
                    broken[rel] = ["unreadable ELF: %s" % ex]
                    continue
                if e is None:
                    continue
                count += 1
                interp, needed, rpath = e
                miss = ["interpreter " + interp] if interp and not in_root(root, interp) else []
                dirs = [x.replace("$ORIGIN", os.path.dirname(rel)) for x in rpath] + ["/lib", "/usr/local/lib", "/usr/lib"]
                miss += [n for n in needed if n in pretend_missing or not any(in_root(root, os.path.join(x, n)) for x in dirs)]
                if miss:
                    broken[rel] = miss
    return count, broken


def linkage(root, control_lib, control_bin):
    """The build's "does it run" lines cannot see a missing library: musl falls back to the build container's
    /usr/lib. Build 4 shipped a modprobe without libcrypto.so.3 and passed; on the PC nothing could load.
    Judged here from the ELF headers, inside the image root only, after checking that a known-broken case is caught."""
    _, broken = link_scan(root, {control_lib})
    fired = sorted(p for p in broken if os.path.basename(p) == control_bin)
    print("  linkage control: with %s pretended missing, %d file(s) are flagged, incl. %s" % (control_lib, len(broken), fired))
    if not fired:
        sys.exit("error: the linkage check did not flag %s without %s; the check is broken" % (control_bin, control_lib))
    n, broken = link_scan(root)
    for p, m in sorted(broken.items()):
        print("  %s needs %s" % (p, ", ".join(m)))
    if broken:
        sys.exit("error: %d of %d ELF files in the image need something the image does not have" % (len(broken), n))
    print("  linkage: all %d ELF files resolve inside the image" % n)


if __name__ == "__main__":
    if sys.argv[1] == "modules":
        modules(sys.argv[2], sys.argv[3])
    elif sys.argv[1] == "firmware":
        firmware(*sys.argv[2:6])
    elif sys.argv[1] == "linkage":
        linkage(*sys.argv[2:5])
    elif sys.argv[1] == "radios":
        radios(*sys.argv[2:6])
    else:
        sys.exit(__doc__)
