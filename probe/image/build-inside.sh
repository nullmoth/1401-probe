#!/bin/sh
# 1401 Probe image build. Runs inside `docker run --platform linux/amd64 alpine:3.24` (see ../../build.sh).
# Output: out/usb/ (the files that go on the stick) and out/1401-probe.img (a raw disk image of the same stick).
# Nothing is installed on the host; every tool comes from Alpine's own repositories at build time.
set -eu
cd /work
OUT=/work/out
R=/tmp/root       # the root filesystem that becomes the initramfs
K=/tmp/k          # unpacked kernel package
F=/tmp/f          # unpacked firmware packages
P=/tmp/pkgs
LABEL=PROBE1401
# only the image's own outputs: out/ also holds the NVIDIA packs and their downloads (build.sh nvpack)
rm -rf "$OUT/usb" "$OUT/1401-probe.img" "$R" "$K" "$F" "$P"
mkdir -p "$OUT/usb/EFI/BOOT" "$OUT/usb/boot/grub" "$OUT/usb/REPORTS" "$R" "$K" "$F" "$P"

say() { printf '\n== %s\n' "$*"; }

say "build tools"
apk update -q
apk add -q kmod cpio zstd xz grub grub-efi mtools dosfstools python3 coreutils sfdisk
cat /etc/alpine-release

say "kernel + GPU firmware packages (fetched, not installed: linux-firmware is 1 GB+ we do not want)"
cd "$P"
apk fetch -q linux-lts linux-firmware-nvidia linux-firmware-amdgpu linux-firmware-i915 linux-firmware-xe linux-firmware-radeon
# radio firmware: only for the opt-in radio pack under drivers/, never the image (trim.py radios picks by name)
apk fetch -q linux-firmware-intel linux-firmware-rtw88 linux-firmware-rtw89 linux-firmware-rtlwifi linux-firmware-rtl_bt \
	linux-firmware-rtl_nic linux-firmware-ath10k linux-firmware-ath11k linux-firmware-ath12k linux-firmware-qca \
	linux-firmware-mediatek linux-firmware-brcm linux-firmware-cypress
ls -1 *.apk
tar -xzf linux-lts-*.apk -C "$K" 2>/dev/null
for p in linux-firmware-*.apk; do tar -xzf "$p" -C "$F" 2>/dev/null; done
cd /work
V=$(ls "$K/lib/modules")
echo "kernel $V"
[ -f "$K/boot/vmlinuz-lts" ] || { echo "error: no vmlinuz-lts in the kernel package"; exit 1; }
# the image's offline claim rests on these being modules (=m) that we then delete, never built in (=y)
BUILTIN=$(grep -E '^CONFIG_(NET_VENDOR_INTEL|E1000|E1000E|IGB|IGC|R8169|CFG80211|MAC80211|BT|IWLWIFI|ATH9K|ATH10K|ATH11K|ATH12K|RTW88|RTW89|MT76_CORE|BRCMFMAC|USB_USBNET|VIRTIO_NET|THUNDERBOLT_NET)=y' "$K/boot/config-$V" | grep -v NET_VENDOR || true)
if [ -n "$BUILTIN" ]; then echo "error: network drivers built into the kernel: $BUILTIN"; exit 1; fi
echo "network/radio drivers built into the kernel: none (all modular)"

say "root filesystem"
apk --root "$R" --initdb --arch x86_64 --keys-dir /etc/apk/keys --repositories-file /etc/apk/repositories \
    --no-cache --no-scripts -q add alpine-baselayout busybox busybox-binsh musl kmod python3 pciutils hwdata-pci \
    hwdata-usb usbutils dmidecode lsblk lscpu blkid kbd font-terminus
# --no-scripts: under Rosetta (colima on Apple silicon) no x86 binary can run inside a chroot without /proc,
# so apk's install scripts died with signal 5. The only one the image needs is busybox's applet links: made here
# from the build container's own busybox, which is the same package version.
BBH=$(apk list --installed busybox 2>/dev/null | cut -d' ' -f1); BBR=$(apk --root "$R" list --installed busybox 2>/dev/null | cut -d' ' -f1)
[ -n "$BBH" ] && [ "$BBH" = "$BBR" ] || { echo "error: busybox differs (container '$BBH', image '$BBR'): the applet list would be wrong"; exit 1; }
for a in $(/bin/busybox --list-full); do
	[ -e "$R/$a" ] || [ -L "$R/$a" ] || { mkdir -p "$R/$(dirname "$a")"; ln -s /bin/busybox "$R/$a"; }
done
[ -x "$R/bin/busybox" ] || { echo "error: no busybox in the root filesystem"; exit 1; }
# the package list for THIRD-PARTY-NOTICES, taken now: the slimming below deletes the image's apk database
apk --root "$R" list --installed 2>/dev/null | sed -E 's/ x86_64 / /; s/ \[installed\]//' | sort > /tmp/pkgs.txt
[ "$(wc -l < /tmp/pkgs.txt)" -ge 10 ] || { echo "error: could not list the image's packages"; exit 1; }
# kmod's tools, not busybox's applets, must answer to these names (kmod reads modprobe.d blacklists and -b)
KM=$(cd "$R" && for k in usr/bin/kmod bin/kmod sbin/kmod; do [ -f "$k" ] && [ ! -L "$k" ] && echo "/$k" && break; done)
[ -n "$KM" ] || { echo "error: kmod binary not found in the root filesystem"; exit 1; }
for t in modprobe insmod rmmod lsmod modinfo depmod; do
	rm -f "$R/sbin/$t" "$R/usr/sbin/$t" "$R/bin/$t" "$R/usr/bin/$t"
	ln -s "$KM" "$R/sbin/$t"
done
# kbd's setfont is a real file at /usr/sbin/setfont (links above never overwrite one); drop only a busybox link named
# setfont elsewhere on PATH. An earlier build deleted kbd's own binary here with a plain rm -f.
for f in sbin/setfont bin/setfont usr/bin/setfont; do if [ -L "$R/$f" ]; then rm -f "$R/$f"; fi; done
[ -f "$R/usr/sbin/setfont" ] && [ ! -L "$R/usr/sbin/setfont" ] || { echo "error: kbd setfont missing at /usr/sbin/setfont"; exit 1; }
# strip every busybox applet that is a network client or server; with no NIC drivers they are dead anyway
for a in wget nc netcat telnet telnetd udhcpc udhcpc6 udhcpd dhcprelay ftpget ftpput ftpd tftp tftpd httpd ntpd \
         ping ping6 traceroute traceroute6 ssl_client arping nslookup whois sendmail popmaildir fakeidentd inetd \
         dnsd nameif ifenslave tunctl vconfig zcip brctl ether-wake pscan route ifconfig ifup ifdown; do
	for d in bin sbin usr/bin usr/sbin; do rm -f "$R/$d/$a"; done
done
# strip every applet that can write a drive (format, partition, raw copy, discard, swap, undo a read-only lock):
#   the probe reads hardware and writes one file to its own stick; nothing here may ever touch a tester's disk.
for a in dd mkfs.ext2 mkfs.minix mkfs.reiser mkfs.vfat mke2fs mkdosfs mkswap fdisk fsck fsck.minix e2fsck tune2fs \
         hdparm blkdiscard blockdev shred swapon swapoff fdformat fatattr freeramdisk flashcp flash_eraseall \
         flash_lock flash_unlock nandwrite nanddump ubiattach ubidetach ubimkvol ubirmvol ubirsvol ubiupdatevol losetup; do
	for d in bin sbin usr/bin usr/sbin; do rm -f "$R/$d/$a"; done
done
W=$(cd "$R" && find bin sbin usr/bin usr/sbin \( -name 'dd' -o -name 'mkfs*' -o -name 'mke2fs' -o -name 'mkswap' \
      -o -name '*fdisk' -o -name 'parted' -o -name 'hdparm' -o -name 'blkdiscard' -o -name 'blockdev' -o -name 'wipefs' \
      -o -name 'nvme' -o -name 'smartctl' -o -name 'sg_*' -o -name 'shred' \) 2>/dev/null | tr '\n' ' ')
[ -z "$W" ] || { echo "error: disk-writing tools left in the image: $W"; exit 1; }
echo "disk-writing tools in the image: none"
# python: drop the network stack and the TLS library; the probe uses none of it.
# libcrypto stays: kmod links it (module signatures). Without it modprobe can load nothing.
PY=$(ls -d "$R"/usr/lib/python3.*)
rm -rf "$PY"/test "$PY"/idlelib "$PY"/tkinter "$PY"/turtledemo "$PY"/ensurepip "$PY"/venv "$PY"/pydoc_data \
       "$PY"/http "$PY"/urllib "$PY"/email "$PY"/xmlrpc "$PY"/wsgiref "$PY"/asyncio "$PY"/sqlite3 "$PY"/dbm \
       "$PY"/ssl.py "$PY"/ftplib.py "$PY"/smtplib.py "$PY"/poplib.py "$PY"/imaplib.py "$PY"/socketserver.py \
       "$PY"/lib-dynload/_ssl.* "$PY"/lib-dynload/_hashlib.* "$PY"/lib-dynload/_sqlite3.* "$PY"/lib-dynload/_tkinter.* \
       "$PY"/lib-dynload/_asyncio.* "$PY"/config-* "$R"/usr/lib/libssl.so* \
       "$R"/usr/share/man "$R"/usr/share/doc "$R"/usr/include "$R"/var/cache/apk/* "$R"/lib/apk "$R"/etc/apk
find "$R" -name __pycache__ -type d -prune -exec rm -rf {} +
find "$R/usr/share/consolefonts" -type f ! -name 'ter-v24n.psf.gz' ! -name 'ter-v32n.psf.gz' ! -name 'default8x16*' -delete 2>/dev/null || true
find "$R/usr/share/keymaps" "$R/usr/share/kbd" -mindepth 1 -maxdepth 1 ! -name consolefonts -exec rm -rf {} + 2>/dev/null || true

say "kernel modules (every network, Wi-Fi and Bluetooth path removed)"
mkdir -p "$R/lib/modules/$V"
python3 probe/image/trim.py modules "$K/lib/modules/$V" "$R/lib/modules/$V"
depmod -b "$R" "$V"

say "GPU firmware (only what nouveau, amdgpu, radeon, i915 and xe ask for)"
python3 probe/image/trim.py firmware "$V" "$R" "$F/lib/firmware" "$R/lib/firmware"

say "overlay"
cp -a probe/overlay/. "$R/"
chmod 0755 "$R/init" "$R/usr/lib/1401-probe/probe.py"
if [ -n "${NM_SEAL_KEY:-}" ]; then
	echo "$NM_SEAL_KEY" | grep -Eqx '[0-9a-f]{64}' || { echo "error: NM_SEAL_KEY is not 64 hex digits"; exit 1; }
	echo "$NM_SEAL_KEY" > "$R/usr/lib/1401-probe/seal.key"
	echo "  seal key: in (not printed)"
else
	echo "  seal key: NONE - reports from this image say sealed: false"
fi
mkdir -p "$R/dev" "$R/proc" "$R/sys" "$R/run" "$R/tmp" "$R/mnt/usb" "$R/root"
mknod -m 600 "$R/dev/console" c 5 1
mknod -m 666 "$R/dev/null" c 1 3
echo "1401-probe $(date -u +%Y-%m-%dT%H:%MZ) alpine $(cat /etc/alpine-release) kernel $V" > "$R/etc/1401-probe-build"

say "proof inside the image itself (its own python, its own tools)"
# no chroot (see --no-scripts above): the image's own binaries run against the image's own libraries,
# and python runs the image's pruned stdlib, so an import the pruning broke fails the build here, not on a tester's PC
IMG() { LD_LIBRARY_PATH="$R/usr/lib:$R/lib" PYTHONHOME="$R/usr" PYTHONDONTWRITEBYTECODE=1 "$@"; }
PYBIN=$(find "$R/usr/bin" -name 'python3.*' -type f | head -1)
IMG "$PYBIN" -c 'import sys; assert sys.prefix.startswith("'"$R"'"), sys.prefix; print("  python", sys.version.split()[0], "stdlib", sys.prefix)'
IMG "$PYBIN" "$R/usr/lib/1401-probe/probe.py" --selftest
IMG "$PYBIN" -c 'import importlib.util as u; bad=[m for m in ("ssl","http","urllib","_hashlib","email") if u.find_spec(m)]; assert not bad, bad; print("  python network modules present: none")'
for t in lspci:--version lsusb:--version dmidecode:--version blkid:--version lsblk:--version lscpu:--version \
         kmod:--version setfont:-V; do
	b=$(find "$R/bin" "$R/sbin" "$R/usr/bin" "$R/usr/sbin" -name "${t%%:*}" -type f 2>/dev/null | head -1)
	[ -n "$b" ] || { echo "error: ${t%%:*} is not in the image"; exit 1; }
	IMG "$b" "${t#*:}" >/dev/null 2>&1 && echo "  runs: ${b#$R}" || { echo "error: does not run: ${b#$R}"; IMG "$b" "${t#*:}"; exit 1; }
done
python3 probe/image/trim.py linkage "$R" libcrypto.so.3 kmod
rm -rf "$R"/tmp/* "$R"/root/.cache
# nothing that names a person may ship on the stick: scan every file in the image root, text and binary
# -D skip: grep -r opens device nodes and FIFOs under $R/dev and can block forever on one
# the names come from $NM_PRIVATE_RE (build.sh reads them from outside the repo); empty only when build.sh allowed it
if [ -n "${NM_PRIVATE_RE:-}" ]; then
	N=$(grep -r -a -i -l -D skip --exclude-dir=dev --exclude-dir=proc --exclude-dir=sys -E "$NM_PRIVATE_RE" "$R" 2>/dev/null || true)
	[ -z "$N" ] || { echo "error: identifying strings in the image: $N"; exit 1; }
	echo "identifying strings in the image: none (private patterns applied)"
else
	echo "identifying strings in the image: NOT SCANNED (no private patterns; build.sh allowed it)"
fi
N=$(find "$R/lib/modules" -path '*/kernel/net/*' -o -path '*/kernel/drivers/net/*' -o -path '*/drivers/bluetooth/*' | wc -l)
[ "$N" -eq 0 ] || { echo "error: $N network module files in the image"; exit 1; }
echo "  network module files in the image: 0"

say "initramfs"
(cd "$R" && find . | sort | cpio -o -H newc --quiet) | zstd -q -16 -T0 > "$OUT/usb/boot/initramfs.zst"
cp "$K/boot/vmlinuz-lts" "$OUT/usb/boot/vmlinuz"

say "bootloader (GRUB, standalone, UEFI x64)"
cp probe/image/grub.cfg "$OUT/usb/boot/grub/grub.cfg"
grub-mkstandalone -O x86_64-efi -o "$OUT/usb/EFI/BOOT/BOOTX64.EFI" \
    --modules="part_msdos part_gpt fat search search_fs_file normal linux echo all_video efi_gop configfile test sleep halt reboot" \
    --locales="" --themes="" --fonts="" "boot/grub/grub.cfg=probe/image/embedded.cfg"
cp probe/stick/README.txt "$OUT/usb/README.txt"
cp probe/stick/1401-probe.marker "$OUT/usb/1401-probe.marker"
cp probe/stick/REPORTS-README.txt "$OUT/usb/REPORTS/README.txt"
cp docs/LICENSE.txt "$OUT/usb/LICENSE.txt"
# GPL-3 s.4/s.6: every copy of the program carries the license text, not just its name
cp LICENSE "$OUT/usb/GPL-3.0.txt"
# the firmware licences require their text to travel with the files (LICENCE.nvidia: "must reproduce the above
# copyright notice"). Alpine's linux-firmware packages carry none, so the texts are vendored from the same upstream tag.
mkdir -p "$OUT/usb/licenses/linux-firmware"
cp docs/firmware-licenses/* "$OUT/usb/licenses/linux-firmware/"
[ "$(ls "$OUT/usb/licenses/linux-firmware" | wc -l)" -eq 6 ] || { echo "error: firmware licence texts missing"; exit 1; }
say "radio pack (opt-in Wi-Fi/Bluetooth/Ethernet trace; never in the image)"
RS=/tmp/radios
rm -rf "$RS" && mkdir -p "$RS"
python3 probe/image/trim.py radios "$K" "$V" "$F/lib/firmware" "$RS"
mkdir -p "$OUT/usb/drivers"
(cd "$RS" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > /tmp/sums && mv /tmp/sums SHA256SUMS)
tar -C "$RS" -cf - . | xz -6 -T0 > "$OUT/usb/drivers/radios-$V.tar.xz"
rm -rf "$RS"
echo "  radios-$V.tar.xz ($(du -h "$OUT/usb/drivers/radios-$V.tar.xz" | cut -f1))"

say "NVIDIA driver packs for the trace step (build.sh nvpack)"
# not in the initramfs: the stick runs from RAM and these are ~200 MB; the probe unpacks only the one a card needs
mkdir -p "$OUT/usb/drivers"
NP=$(ls out/nvpack/nvidia-*.tar.xz 2>/dev/null | wc -l)
if [ "$NP" -eq 0 ] && [ "${NM_ALLOW_NO_NVPACK:-}" != 1 ]; then
	echo "error: no NVIDIA driver packs in out/nvpack (./build.sh nvpack, or NM_ALLOW_NO_NVPACK=1)"; exit 1
fi
for pk in out/nvpack/nvidia-*.tar.xz; do
	[ -f "$pk" ] || continue
	PV=$(tar -xJOf "$pk" ./pack.json | sed -n 's/.*"kernel": "\([^"]*\)".*/\1/p')
	# a pack built for another kernel would refuse to load on every tester's PC: stop here instead
	[ "$PV" = "$V" ] || { echo "error: $pk is built for kernel '$PV', the image is $V: rebuild with ./build.sh nvpack"; exit 1; }
	cp "$pk" "$OUT/usb/drivers/"
	echo "  $(basename "$pk") ($(du -h "$pk" | cut -f1))"
done
for f in out/nvpack/*.failed; do [ -f "$f" ] && echo "  NOT BUILT on $V: $(basename "$f" .failed) (its cards are traced with nouveau)"; done

# GPL/other licenses travel with the binaries: every package in the image, its version, license and source
{
	cat docs/THIRD-PARTY-NOTICES.head.txt
	echo; echo "PACKAGES IN THE SCANNER (Alpine Linux ${ALPINE:-3.24}, x86_64): name-version  {source package}  (license)"
	cat /tmp/pkgs.txt
	echo; echo "KERNEL: linux-lts $(ls "$R/lib/modules")  (GPL-2.0-only)"
	echo; echo "HARDWARE COLLECTOR: Hardware-Sniffer by lzhoang2801, /usr/lib/1401-probe/hsniffer (BSD-3-Clause, text in that folder)"
	for pk in "$OUT"/usb/drivers/nvidia-*.tar.xz; do
		[ -f "$pk" ] || continue
		echo; echo "NVIDIA DRIVER: drivers/$(basename "$pk")  NVIDIA Software License (licenses/LICENSE.nvidia inside the pack);"
		echo "  binaries as shipped by NVIDIA, only the kernel interface compiled for this kernel. Open kernel modules: MIT/GPL-2.0."
	done
} > "$OUT/usb/THIRD-PARTY-NOTICES.txt"
[ "$(grep -c '(GPL' "$OUT/usb/THIRD-PARTY-NOTICES.txt")" -ge 3 ] || { echo "error: THIRD-PARTY-NOTICES has no package licenses"; exit 1; }
(cd "$OUT/usb" && sha256sum EFI/BOOT/BOOTX64.EFI boot/vmlinuz boot/initramfs.zst boot/grub/grub.cfg 1401-probe.marker \
	drivers/*.tar.xz > SHA256SUMS)

say "raw disk image (for QEMU and for balenaEtcher/Rufus users)"
KB=$(du -sk "$OUT/usb" | cut -f1)
MB=$(( KB / 1024 + 96 ))
[ "$MB" -lt 128 ] && MB=128
truncate -s $(( MB + 1 ))M "$OUT/1401-probe.img"
printf 'label: dos\nstart=2048, type=c, bootable\n' | sfdisk -q "$OUT/1401-probe.img"
truncate -s ${MB}M /tmp/part.img
mkfs.vfat -F 32 -n "$LABEL" /tmp/part.img >/dev/null
export MTOOLS_SKIP_CHECK=1
mcopy -s -i /tmp/part.img "$OUT/usb/EFI" "$OUT/usb/boot" "$OUT/usb/REPORTS" "$OUT/usb/licenses" "$OUT/usb/README.txt" \
      "$OUT/usb/LICENSE.txt" "$OUT/usb/GPL-3.0.txt" "$OUT/usb/THIRD-PARTY-NOTICES.txt" \
      "$OUT/usb/1401-probe.marker" "$OUT/usb/SHA256SUMS" "$OUT/usb/drivers" ::
dd if=/tmp/part.img of="$OUT/1401-probe.img" bs=1M seek=1 conv=notrunc status=none
rm -f /tmp/part.img

say "sizes"
ls -la "$OUT/usb/boot" "$OUT/usb/EFI/BOOT" | grep -v '^total'
du -sh "$R/lib/modules" "$R/lib/firmware" "$R/usr/lib/python3."* "$R"
du -sh "$OUT/usb" "$OUT/1401-probe.img"
