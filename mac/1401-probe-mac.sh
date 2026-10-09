#!/bin/bash
# 1401 Probe for macOS: make the scanner stick, and find the report afterwards.
#
#   bash 1401-probe-mac.sh            make the stick (asks before it erases anything)
#   bash 1401-probe-mac.sh --report   after the scan: copy the report off the stick, open the upload page
#   bash 1401-probe-mac.sh --selftest
#
# Step by step, and it says each step as it goes:
#   1. Finds 1401-probe-img.zip next to this file or in Downloads, or downloads it. The zip must match the
#      SHA-256 built into this file, or nothing is written.
#   2. Lists EXTERNAL physical disks only. Internal disks and the disk macOS runs from are never offered.
#   3. You type the disk's name (for example disk4) and then ERASE to confirm. That stick is erased.
#   4. Writes the image (it asks for your Mac password for that one step), reads it back and checks it.
#   5. Tells you how to boot the PC from it and how to get the report back.
# Uses only tools every Mac has (diskutil, dd, shasum, unzip, curl). Never uploads anything.
# Free software, GPL-3.0.
set -u
VERSION="@VERSION@"
IMG_ZIP_SHA="@IMG_ZIP_SHA@"
IMG_URL="https://nullmothsystems.com/download/1401-probe-img.zip"
UPLOAD_URL="https://nullmothsystems.com/#send"
LABEL="PROBE1401"
MIN=$((1 << 30)); MAX=$((512 << 30))

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }
step() { printf '\nStep %s. %s\n' "$1" "$2"; }

# one disk's facts from diskutil: "<internal> <bytes> <removable/external> <name>"
disk_ok() {  # $1 = diskN; prints a reason and returns 1 when it may NOT be erased
	local d=$1 info internal size boot
	info=$(diskutil info "$d" 2>/dev/null) || { echo "not a disk"; return 1; }
	internal=$(printf '%s\n' "$info" | awk -F: '/^ *Device Location:/{gsub(/ /,"",$2);print $2}')
	[ -z "$internal" ] && internal=$(printf '%s\n' "$info" | awk -F: '/^ *Internal:/{gsub(/ /,"",$2);print $2}')
	size=$(printf '%s\n' "$info" | awk -F'[()]' '/Disk Size:/{split($2,a," ");print a[1];exit}')
	boot=$(diskutil info / 2>/dev/null | awk -F: '/Part of Whole:/{gsub(/ /,"",$2);print $2}')
	case "$internal" in Internal|Yes) echo "internal disk"; return 1 ;; esac
	[ "$d" = "$boot" ] && { echo "macOS is running from it"; return 1; }
	[ -n "$size" ] || { echo "size unknown"; return 1; }
	[ "$size" -ge "$MIN" ] || { echo "smaller than 1 GB"; return 1; }
	[ "$size" -le "$MAX" ] || { echo "bigger than 512 GB: that is a drive, not a stick"; return 1; }
	return 0
}

get_zip() {
	local here p
	here=$(cd "$(dirname "$0")" && pwd)
	for p in "$here/1401-probe-img.zip" "$PWD/1401-probe-img.zip" "$HOME/Downloads/1401-probe-img.zip"; do
		[ -f "$p" ] && { ZIP=$p; break; }
	done
	if [ -z "${ZIP:-}" ]; then
		ZIP="${TMPDIR:-/tmp}/1401-probe-img-$VERSION.zip"
		say "  Not found next to this file or in Downloads. Downloading it (about 430 MB) ..."
		curl -fL --progress-bar -o "$ZIP.part" "$IMG_URL" || die "  Download failed. Get it from https://nullmothsystems.com/probe"
		mv "$ZIP.part" "$ZIP"
	else
		say "  Found $ZIP"
	fi
	say "  Checking its SHA-256 ..."
	local got; got=$(shasum -a 256 "$ZIP" | cut -d' ' -f1)
	[ "$got" = "$IMG_ZIP_SHA" ] || die "  STOP: $ZIP does not match this release. Nothing was written. Download it again from https://nullmothsystems.com/probe"
	say "  Matches this release."
}

make_stick() {
	say "1401 Probe $VERSION for macOS: makes the scanner stick."
	step 1 "The stick image"; get_zip
	step 2 "Choose the USB stick (it will be erased)"
	local list d why n=0
	list=$(diskutil list external physical 2>/dev/null | awk '/^\/dev\/disk/{sub("/dev/","",$1);print $1}')
	for d in $list; do
		if why=$(disk_ok "$d"); then
			n=$((n + 1))
			say "  $d  $(diskutil info "$d" | awk -F: '/Disk Size:/{sub(/^ */,"",$2);split($2,a,"(");print a[1];exit}')  $(diskutil info "$d" | awk -F: '/Media Name:/{sub(/^ */,"",$2);print $2;exit}')"
		else
			say "  not offered: $d ($why)"
		fi
	done
	[ "$n" -gt 0 ] || die "  No USB stick found. Plug one in (1 GB or bigger) and run this again."
	printf '\n  Type the name of the stick to erase (for example disk4), or press Enter to stop: '
	read -r pick; pick=${pick#/dev/}
	[ -n "$pick" ] && printf '%s\n' "$list" | grep -qx "$pick" || die "  Stopped. Nothing was written."
	why=$(disk_ok "$pick") || die "  STOP: $pick cannot be used ($why). Nothing was written."
	say "  EVERYTHING on $pick will be erased."
	printf '  Type ERASE to continue: '; read -r ok
	[ "$ok" = ERASE ] || die "  Stopped. Nothing was written."
	step 3 "Writing the stick (your Mac password is needed for this one step)"
	diskutil unmountDisk "/dev/$pick" >/dev/null || die "  Could not unmount $pick. Close any window using it and try again."
	local want got size
	want=$(unzip -p "$ZIP" 1401-probe.img | shasum -a 256 | cut -d' ' -f1)
	size=$(unzip -Zl "$ZIP" 1401-probe.img | awk 'NR==1{print $4}')
	unzip -p "$ZIP" 1401-probe.img | sudo dd of="/dev/r$pick" bs=4m 2>/dev/null || die "  Writing failed."
	sync
	say "  Reading it back to check ..."
	got=$(sudo dd if="/dev/r$pick" bs=4m 2>/dev/null | head -c "$size" | shasum -a 256 | cut -d' ' -f1)
	[ "$got" = "$want" ] || die "  STOP: the stick does not read back what was written. Try another stick or port."
	say "  Verified: the stick holds exactly the image."
	diskutil eject "/dev/$pick" >/dev/null 2>&1
	step 4 "Start the PC from the stick"
	say "  1. Plug the stick into the PC you want to scan. Open its BIOS setup (usually Del or F2 while it starts)."
	say "  2. Turn Secure Boot OFF and boot the USB stick (boot menu: ASUS F8, MSI/ASRock F11, Gigabyte/Dell/Lenovo/Acer F12, HP F9)."
	say "  3. Pick \"1401 Probe - scan this computer\". It asks once whether to include Wi-Fi, Bluetooth and Ethernet"
	say "     (press Y within 30 seconds, or nothing for no; it never goes online). The scan takes about 5 to 20 minutes;"
	say "     near the end the screen may go black for up to 10 minutes. If nothing happens for 15 minutes, hold the"
	say "     power button and start the stick again: it skips the step that froze. It turns the PC off when done."
	say "  4. Turn Secure Boot back on if you use it."
	say "  5. Plug the stick back into this Mac and run:  bash $(basename "$0") --report"
}

find_report() {
	say "1401 Probe $VERSION for macOS: finds your report."
	local v="/Volumes/$LABEL" dst="$HOME/Desktop/1401-Probe-Reports" f n=0
	[ -d "$v" ] || die "  No $LABEL stick found. Plug in the stick you scanned with (it shows up as $LABEL)."
	[ -d "$v/REPORTS" ] || die "  The stick has no REPORTS folder. Start the PC from it first."
	mkdir -p "$dst"
	for f in "$v"/REPORTS/*.zip "$v"/REPORTS/*.txt; do
		[ -f "$f" ] || continue
		cp -p "$f" "$dst/" && say "  copied $(basename "$f")" && n=$((n + 1))
	done
	[ "$n" -gt 0 ] || die "  The stick has no report yet. Start the PC from it first."
	say ""; say "  Your report is in $dst"
	say "  Send BOTH files (the .zip and the .txt) at $UPLOAD_URL"
	open "$dst" 2>/dev/null; open "$UPLOAD_URL" 2>/dev/null
}

selftest() {
	local fails=0
	check() { if [ "$2" = 0 ]; then say "  ok   $1"; else say "  FAIL $1"; fails=$((fails + 1)); fi; }
	# disk_ok against a fake diskutil
	diskutil() {
		case "$*" in
			"info /") printf '   Part of Whole:            disk3\n' ;;
			"info disk3") printf '   Device Location:          Internal\n   Disk Size:                500.3 GB (500277792768 Bytes)\n' ;;
			"info disk4") printf '   Device Location:          External\n   Disk Size:                16.0 GB (16008609792 Bytes)\n' ;;
			"info disk5") printf '   Device Location:          External\n   Disk Size:                2.0 TB (2000398934016 Bytes)\n' ;;
			"info disk6") printf '   Device Location:          External\n   Disk Size:                268.4 MB (268435456 Bytes)\n' ;;
			"info disk7") printf '   Internal:                 Yes\n   Disk Size:                8.0 GB (8000000000 Bytes)\n' ;;
			*) return 1 ;;
		esac
	}
	disk_ok disk4 >/dev/null; check "an external 16 GB stick is offered" $?
	disk_ok disk3 >/dev/null; r=$?; check "the internal disk macOS runs from is refused" $((r == 0))
	disk_ok disk5 >/dev/null; r=$?; check "a 2 TB external drive is refused as a drive, not a stick" $((r == 0))
	disk_ok disk6 >/dev/null; r=$?; check "a stick under 1 GB is refused" $((r == 0))
	disk_ok disk7 >/dev/null; r=$?; check "a disk that says Internal: Yes is refused" $((r == 0))
	disk_ok disk9 >/dev/null; r=$?; check "something diskutil does not know is refused" $((r == 0))
	unset -f diskutil
	[ "$fails" = 0 ] && say "" && say "all checks passed" || say "$fails failed"
	return "$fails"
}

case "${1:-}" in
	--selftest) selftest ;;
	--report) find_report ;;
	"") make_stick ;;
	*) sed -n '2,8p' "$0"; exit 2 ;;
esac
