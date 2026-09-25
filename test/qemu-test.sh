#!/bin/bash
# 1401 Probe: boot the real stick image (out/1401-probe.img) on an emulated UEFI PC and judge the report it writes.
#   ./test/qemu-test.sh          (after ./build.sh image; ~5-10 min under TCG on Apple silicon)
# The machine is loaded with things that must not come out the other side:
#     planted serials (SMBIOS system + board, USB stick, NVMe), a fixed SMBIOS UUID, and a real NIC (Intel e1000e).
#   It fails if any planted secret survives anywhere in the zip, or if any interface other than lo exists.
#   Sanity checks that must pass: the e1000e is on the PCI bus (so "no interface" means no driver, not no NIC),
#   and the redactor reports hits (so "no secret found" means it was removed, not that it was never collected).
# The tester's SSD: the NVMe drive carries a decoy, a FAT volume with the stick's own label and marker, the worst
#   case for a stick finder. It must come out byte-identical (sha256), locked read-only, never probed, never mounted.
#   Sanity check: the USB stick's hash does change (writes happen where allowed, and the hash can see them).
set -euo pipefail
cd "$(dirname "$0")/.."
T=out/test
CODE=/opt/homebrew/share/qemu/edk2-x86_64-code.fd
VARS=/opt/homebrew/share/qemu/edk2-i386-vars.fd
LIMIT=${LIMIT:-1200}   # seconds QEMU may run before it is killed and the test fails
[ -f out/1401-probe.img ] || { echo "error: build the image first: ./build.sh image"; exit 1; }
for f in "$CODE" "$VARS"; do [ -f "$f" ] || { echo "error: missing OVMF file $f"; exit 1; }; done
rm -rf "$T"; mkdir -p "$T/reports"
cp out/1401-probe.img "$T/stick.img"
cp "$VARS" "$T/vars.fd"
python3 -c "open('$T/nvme.img','wb').truncate(256 << 20)"

# the test boots straight into p1401.mode=test (no GPU-driver stage, no power-off countdown) with a serial mirror
cat > "$T/grub.cfg" <<'EOF'
set timeout=0
menuentry "1401 Probe - QEMU test" {
	linux /boot/vmlinuz rdinit=/init loglevel=4 efi=debug console=tty0 console=ttyS0,115200 consoleblank=0 snd_intel_dspcfg.dsp_driver=1 p1401.mode=test p1401.serial=1
	initrd /boot/initramfs.zst
}
EOF
MT() { docker run --rm --platform linux/amd64 -v "$PWD/$T":/t alpine:3.24 sh -c "apk add -q mtools >/dev/null && $1"; }
MT 'mcopy -o -i /t/stick.img@@1M /t/grub.cfg ::/boot/grub/grub.cfg && mdir -b -i /t/stick.img@@1M ::/boot/grub'
MT 'mformat -i /t/nvme.img -v PROBE1401 -F :: && echo decoy > /t/1401-probe.marker && mcopy -i /t/nvme.img /t/1401-probe.marker :: && mdir -i /t/nvme.img ::'
NVME0=$(shasum -a 256 "$T/nvme.img" | cut -d' ' -f1); STICK0=$(shasum -a 256 "$T/stick.img" | cut -d' ' -f1)

echo "== booting (limit ${LIMIT}s)"
qemu-system-x86_64 -machine q35 -cpu max -smp 2 -m 4096 \
	-drive if=pflash,format=raw,readonly=on,file="$CODE" -drive if=pflash,format=raw,file="$T/vars.fd" \
	-device qemu-xhci,id=xhci \
	-drive if=none,id=stick,format=raw,file="$T/stick.img" \
	-device usb-storage,bus=xhci.0,drive=stick,serial=SECRETSTICK123,removable=on \
	-drive if=none,id=nv0,format=raw,file="$T/nvme.img" -device nvme,drive=nv0,serial=SECRETNVME456 \
	-netdev user,id=n0,restrict=on -device e1000e,netdev=n0,mac=52:54:00:12:34:56 \
	-device intel-hda -device hda-duplex \
	-smbios type=1,manufacturer=ProbeTest,product=QEMU-1401,serial=SECRETSYS789,uuid=5ec2e7aa-1111-2222-3333-444455556666 \
	-smbios type=2,manufacturer=ProbeBoards,product=TEST-B860,serial=SECRETBOARD000 \
	-display none -monitor none -serial file:"$T/serial.log" -no-reboot &
QP=$!
t0=$(date +%s)
while kill -0 $QP 2>/dev/null; do
	if [ $(( $(date +%s) - t0 )) -ge "$LIMIT" ]; then
		kill $QP; echo "error: QEMU still running after ${LIMIT}s, killed. Last console lines:"; tail -30 "$T/serial.log"; exit 1
	fi
	sleep 2
done
echo "QEMU exited after $(( $(date +%s) - t0 ))s"
NVME1=$(shasum -a 256 "$T/nvme.img" | cut -d' ' -f1); STICK1=$(shasum -a 256 "$T/stick.img" | cut -d' ' -f1)

MT 'mcopy -n -i /t/stick.img@@1M "::/REPORTS/*" /t/reports/ ; ls -la /t/reports'

T="$T" NVME0="$NVME0" NVME1="$NVME1" STICK0="$STICK0" STICK1="$STICK1" python3 - <<'EOF'
import glob, json, os, sys, zipfile
T = os.environ["T"]
fails = []
def check(name, cond, detail=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("   [%s]" % detail) if detail != "" else ""))
    if not cond:
        fails.append(name)

serial = open(os.path.join(T, "serial.log"), "rb").read().decode("utf-8", "replace")
check("the probe reached DONE on the console", "DONE. Report saved" in serial)
zips = sorted(glob.glob(T + "/reports/*.zip"))
check("exactly one report zip on the stick", len(zips) == 1, [os.path.basename(z) for z in zips])
check("no half-written .zip.tmp left behind", not glob.glob(T + "/reports/*.tmp"))
if len(zips) != 1:
    print("\n".join(serial.splitlines()[-30:]))
    sys.exit(1)
check("the summary .txt sits next to the zip", os.path.exists(zips[0][:-4] + ".txt"))
z = zipfile.ZipFile(zips[0])
names = z.namelist()
root = names[0].split("/")[0]
rep = json.loads(z.read(root + "/report.json"))
st = rep.get("stages", {})
bad = {k: v for k, v in st.items() if v.get("status") != "ok"}
check("all 11 stages ran and read ok", len(st) == 11 and not bad, bad or len(st))

net0, net1 = rep.get("network", {}), rep.get("network_at_end", {})
check("the only network interface at the start is lo", net0.get("interfaces") == ["lo"], net0)
check("the only network interface at the end is lo", net1.get("interfaces") == ["lo"], net1)
nic = [d for d in rep.get("pci", []) if d.get("vendor") == "0x8086" and d.get("device") == "0x10d3"]
check("sanity: the e1000e NIC is on the PCI bus", len(nic) == 1)
check("   ... and no driver bound to it", bool(nic) and nic[0].get("driver") is None, nic and nic[0].get("driver"))

red = rep.get("redaction", {})
hits = sum(red.get("hits", {}).values())
check("sanity: the redactor learned the planted serials", red.get("known_values", 0) >= 4, red.get("known_values"))
check("   ... and replaced them", hits > 0, red.get("hits"))
blob = b"".join(z.read(n) for n in names) + open(zips[0][:-4] + ".txt", "rb").read()
low = blob.lower()
for s in ("SECRETSTICK123", "SECRETNVME456", "SECRETSYS789", "SECRETBOARD000",
          "5ec2e7aa-1111-2222-3333-444455556666", "52:54:00:12:34:56"):
    n = low.count(s.lower().encode())
    check("%s appears nowhere in the report" % s, n == 0, n)
dmi = z.read(root + "/dmi/dmidecode.txt").decode("utf-8", "replace")
check("dmidecode still names the board (only the serial left)", "TEST-B860" in dmi and "[REDACTED]" in dmi)

check("the DSDT was saved", any(n.endswith("/acpi/tables/DSDT.aml") for n in names),
    [n.split("/tables/")[1] for n in names if "/tables/" in n][:12])
cpuid = z.read(root + "/cpu/cpuid.txt").decode()
check("raw CPUID leaf 0 is in cpu/cpuid.txt", "0x00000000 0x00: eax=" in cpuid)
check("the CPU was decoded", bool(rep.get("cpu", {}).get("vendor")), rep.get("cpu", {}).get("brand"))
ocp = [d["oc_path"] for d in rep.get("pci", []) if (d.get("oc_path") or "").startswith("PciRoot(0x0)/Pci(")]
check("OpenCore device paths were computed", len(ocp) >= 5, ocp[:3])
check("the HDA codec was read", any(c.get("vendor_id") for c in rep.get("audio", [])), rep.get("audio"))
check("the NVMe drive was identified (model, not serial)", bool(rep.get("storage", {}).get("nvme")))
check("the USB stick is in the USB device list", any("0x46f4" in json.dumps(d) or "46f4" in json.dumps(d)
                                                   for d in rep.get("usb", {}).get("devices", [])))

sf = rep.get("safety", {})
check("the NVMe drive (decoy PROBE1401 volume) is byte-identical after the scan", os.environ["NVME0"] == os.environ["NVME1"],
    os.environ["NVME0"][:12] + " -> " + os.environ["NVME1"][:12])
check("sanity: the USB stick image did change (the hash sees writes)", os.environ["STICK0"] != os.environ["STICK1"])
lk = sf.get("locked_read_only", {})
check("the NVMe drive was locked read-only in the kernel", lk.get("nvme0n1") == "read-only", lk)
check("no drive failed to lock", not [k for k, v in lk.items() if v != "read-only"], lk)
check("blkid never read the NVMe drive", sf.get("label_probed") and not [d for d in sf["label_probed"] if "nvme" in d],
    sf.get("label_probed"))
bm = sf.get("block_mounts", [])
check("exactly one drive mounted: the stick, at /mnt/usb", len(bm) == 1 and bm[0][0] == sf.get("stick") and bm[0][1] == "/mnt/usb", bm)
kf = set(sf.get("kernel_filesystems", []))
check("the kernel cannot even mount a tester's filesystem (no ext4/ntfs/btrfs/xfs/hfs/exfat/f2fs)",
    not kf & {"ext2", "ext3", "ext4", "ntfs", "ntfs3", "btrfs", "xfs", "hfs", "hfsplus", "exfat", "f2fs", "apfs", "fuseblk"}
    and "vfat" in kf, sorted(kf))
nv = [d for d in rep.get("storage", {}).get("disks", []) if d.get("name") == "nvme0n1"]
check("the drive list marks nvme0n1 locked and not the stick", nv and nv[0].get("locked") == "read-only" and not nv[0].get("is_probe_stick"), nv)
gp = rep.get("gpus", [])
check("the QEMU display adapter went through the GPU stage", len(gp) >= 1, [(g.get("vendor"), g.get("device")) for g in gp])
print("\n%s  (%d files, %.1f MB zip)" % ("ALL PASSED" if not fails else "%d FAILED" % len(fails), len(names),
                                         os.path.getsize(zips[0]) / 1e6))
sys.exit(1 if fails else 0)
EOF
