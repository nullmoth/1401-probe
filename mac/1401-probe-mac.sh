#!/bin/bash
# 1401 Probe for macOS: make the scanner stick, and find the report afterwards.
#
#   bash 1401-probe-mac.sh            make the stick (asks before it erases anything)
#   bash 1401-probe-mac.sh --report   after the scan: copy the report off the stick, open the upload page
#   bash 1401-probe-mac.sh --scan     scan THIS Mac (or Nullintosh): CPU, graphics, Wi-Fi, Bluetooth, every device
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

# Text from this Mac loses what identifies it or its owner: serial numbers, UUIDs, network and Bluetooth
# addresses, the Wi-Fi networks it knows and their passwords, the names of paired devices, and the Mac's and the
# user's names.
# $1 = file. Lists under the headings in the awk below are dropped whole (they name networks and other people's devices).
# itlwm and AirportItlwm (Intel Wi-Fi on a Mothintosh) can carry the network name and password in their Info.plist as
# "WiFiConfig", which ioreg prints on the Wi-Fi card's PCI entry; the registry root's "IOConsoleUsers" names the
# logged-in user (short and full name). Both values go whole, as do password and network-name keys anywhere else.
scrub_file() {
	local f=$1 t="$1.t" n q u
	LC_ALL=C awk '
		/^[[:space:]]*(Current Network Information|Other Local Wi-Fi Networks|Preferred Networks|Connected|Not Connected|Paired Devices|Devices \(Paired, Configured, etc\.\)|Recent Devices):[[:space:]]*$/ {
			match($0, /^[[:space:]]*/); skip = RLENGTH; print substr($0, 1, skip) "(list removed)"; next }
		skip >= 0 { match($0, /^[[:space:]]*/); if (RLENGTH > skip || $0 ~ /^[[:space:]]*$/) next; skip = -1 }
		{ print }' skip=-1 "$f" > "$t" || { rm -f "$t"; return 1; }
	LC_ALL=C sed -E \
		-e 's/("IOPlatform(UUID|SerialNumber)" = )"[^"]*"/\1"(removed)"/' \
		-e 's/("[A-Za-z -]*(Serial|UUID|UDID)[A-Za-z ]*" ?= ?)("[^"]*"|<[0-9A-Fa-f]*>|[0-9]+)/\1"(removed)"/g' \
		-e 's/("(serial-number|board-id-serial|IOPlatformSerialNumber|kUSBSerialNumberString|USB Serial Number|Serial Number|SerialNumber|IOMACAddress|BD_ADDR|BluetoothDeviceAddress|LocalAddress|IOBluetoothHostControllerAddress)" = ).*/\1(removed)/' \
		-e 's/("(WiFiConfig|IOConsoleUsers)" = ).*/\1(removed)/' \
		-e 's/("(password|Password|passwd|passphrase|psk|PSK|ssid|SSID|bssid|BSSID)" ?= ?).*/\1(removed)/' \
		-e 's/("kCG[A-Za-z]*UserName[A-Za-z]*" ?= ?).*/\1(removed)/' \
		-e 's/^([[:space:]]*[^:]*(Serial|UUID|UDID)[^:]*:).*/\1 (removed)/' \
		-e 's/^([[:space:]]*(UID|Address|MAC Address|Ethernet Address|Wi-Fi Address|Bluetooth Address|SSID|BSSID|Network Name)[^:]*:).*/\1 (removed)/' \
		-e 's/(^|[^0-9A-Fa-f:])([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}([^0-9A-Fa-f:]|$)/\1XX:XX:XX:XX:XX:XX\3/g' \
		"$t" > "$f" || { rm -f "$t"; return 1; }
	rm -f "$t"
	for n in $SCRUB_NAMES; do
		q=$(printf '%s' "$n" | LC_ALL=C sed 's/[][\\.*^$/]/\\&/g')
		LC_ALL=C sed "s/$q/(removed)/g" "$f" > "$t" && mv "$t" "$f" || { rm -f "$t"; return 1; }
	done
	# a user name can be an ordinary word, so it is only replaced inside a home path
	for u in $SCRUB_USERS; do
		LC_ALL=C sed "s#/Users/$u\([/ \"]\)#/Users/(removed)\1#g; s#/Users/$u\$#/Users/(removed)#" "$f" > "$t" && mv "$t" "$f" || { rm -f "$t"; return 1; }
	done
}

scan_mac() {
	[ "$(uname -s)" = Darwin ] || die "  --scan scans a Mac. On a PC, use the 1401 stick."
	local stamp tmpd out zip f
	stamp=$(date -u +%Y%m%dT%H%M%SZ)
	tmpd=$(mktemp -d /tmp/1401-scan.XXXXXX) || die "  could not make a temporary folder"
	out=$tmpd/1401-mac-scan-$stamp
	mkdir -p "$out" || die "  could not make a temporary folder"
	SCRUB_NAMES=$(printf '%s\n' "$(scutil --get ComputerName 2>/dev/null)" "$(scutil --get LocalHostName 2>/dev/null)" \
		"$(hostname -s 2>/dev/null)" | awk 'length($0) >= 3 && $0 !~ /[[:space:]]/' | sort -u)
	SCRUB_USERS=$(printf '%s\n' "$(id -un)" "$(stat -f %Su /dev/console 2>/dev/null)" | awk 'length($0) >= 2 && $0 != "root"' | sort -u)
	say ""
	say "  1401 Probe $VERSION: scanning this Mac. It only reads, changes nothing and sends nothing."
	say "  Left out: serial numbers, UUIDs, network and Bluetooth addresses, Wi-Fi network names,"
	say "  the names of paired devices, and this Mac's and your user's names."
	say ""
	c() { f=$1; shift; say "    $f"; "$@" > "$out/$f" 2>&1; echo "rc=$?" >> "$out/$f"; }
	c sw_vers.txt sw_vers
	c uname.txt uname -a
	c sysctl_cpu.txt sysctl machdep.cpu machdep.tsc machdep.xcpm hw.optional hw.cpufrequency hw.busfrequency \
		hw.tbfrequency hw.memsize hw.model hw.ncpu hw.physicalcpu hw.logicalcpu hw.cachesize hw.cachelinesize
	c sysctl_kern.txt sysctl kern.version kern.osversion kern.bootargs
	c ioreg_pci.txt ioreg -lw0 -r -c IOPCIDevice
	c ioreg_gfx_tree.txt sh -c 'for n in GFX0 IGPU PEGP GPU0 VID PEG0 PEG1 PEG2 IGFX; do ioreg -lw0 -p IODeviceTree -r -n $n; done'
	c ioreg_framebuffer.txt ioreg -lw0 -r -c IOFramebuffer
	c ioreg_accelerator.txt ioreg -lw0 -r -c IOAccelerator
	c ioreg_display.txt ioreg -lw0 -r -c IODisplayConnect
	c ioreg_backlight.txt ioreg -lw0 -r -c AppleBacklightDisplay
	c ioreg_cpu_pm.txt ioreg -lw0 -r -c X86PlatformPlugin
	c ioreg_acpi_tables.plist ioreg -a -r -c AppleACPIPlatformExpert -k "ACPI Tables"
	c ioreg_audio.txt ioreg -lw0 -r -c IOHDACodecDevice
	c ioreg_usb.txt ioreg -lw0 -p IOUSB
	c ioreg_wifi.txt sh -c 'ioreg -lw0 -r -c IO80211Controller; ioreg -lw0 -r -c IO80211Interface'
	c ioreg_bluetooth.txt ioreg -lw0 -r -c IOBluetoothHostController
	c ioreg_battery.txt ioreg -lw0 -r -c AppleSmartBattery
	c ioreg_input.txt sh -c 'ioreg -lw0 -r -c IOHIDInterface | grep -E "\"(Product|Manufacturer|VendorID|ProductID|Transport|PrimaryUsage|PrimaryUsagePage|LocationID)\" = |\+-o "'
	c ioreg_storage.txt sh -c 'ioreg -lw0 -r -c IONVMeController; ioreg -lw0 -r -c IOAHCIBlockStorageDevice'
	c kexts.txt kmutil showloaded
	c hardware.txt system_profiler -detailLevel mini SPDisplaysDataType SPPCIDataType SPAirPortDataType SPBluetoothDataType \
		SPAudioDataType SPUSBDataType SPThunderboltDataType SPNVMeDataType SPSerialATADataType SPCameraDataType \
		SPCardReaderDataType SPPowerDataType SPMemoryDataType
	c nvram.txt sh -c 'nvram boot-args; nvram csr-active-config'
	c pmset.txt sh -c 'pmset -g; pmset -g cap; pmset -g ps'
	c log_hardware.txt sh -c "log show --last boot --style compact --predicate 'sender CONTAINS[c] \"Intel\" OR sender CONTAINS[c] \"AMD\" OR sender CONTAINS[c] \"NV\" OR sender CONTAINS[c] \"Graphics\" OR sender CONTAINS[c] \"framebuffer\" OR sender CONTAINS[c] \"Backlight\" OR sender CONTAINS[c] \"Metal\" OR sender CONTAINS[c] \"80211\" OR sender CONTAINS[c] \"Bluetooth\" OR sender CONTAINS[c] \"Brcm\" OR sender CONTAINS[c] \"HDA\"' 2>/dev/null | tail -n 20000"
	say ""
	say "  Removing serial numbers, addresses and names..."
	for f in "$out"/*; do
		scrub_file "$f" || { say "    could not clean $(basename "$f"): left out"; rm -f "$f" "$f.t"; }
	done
	zip="$HOME/Desktop/1401-mac-scan-$stamp.zip"
	(cd "$tmpd" && COPYFILE_DISABLE=1 /usr/bin/zip -qrX "$zip" "$(basename "$out")") || die "  could not write $zip"
	rm -rf "$tmpd"
	say ""
	say "  Done. Your scan is on the Desktop: $(basename "$zip") ($(($(wc -c < "$zip") / 1024)) KB)"
	say "  Send it at $UPLOAD_URL"
	open -R "$zip" 2>/dev/null; open "$UPLOAD_URL" 2>/dev/null
	return 0
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
	# the scan's cleaner, on text shaped like what system_profiler and ioreg print
	local d; d=$(mktemp -d)
	SCRUB_NAMES="Jane-MacBook"; SCRUB_USERS="jane"; local h=/Users
	printf '%s\n' "Wi-Fi:" "    Card Type: Wi-Fi  (0x14E4, 0x7BF)" "    Firmware Version: wl0: Oct 2 2024 version 9.30.514.0.32.5.92" \
		"    MAC Address: a4:83:e7:12:34:56" "    Current Network Information:" "        HomeNetwork5G:" "          PHY Mode: 802.11ac" \
		"    Other Local Wi-Fi Networks:" "        Neighbours:" "Bluetooth Controller:" "    Chipset: BCM_4364B3" "    Address: A4:83:E7:12:34:57" \
		"    Connected:" "        Jane's AirPods:" "          Address: 11:22:33:44:55:66" "    Not Connected:" "        Jane's Keyboard:" \
		"Serial Number (system): C02XYZ12345" '    "IOPlatformSerialNumber" = "C02XYZ12345"' '    "IOMACAddress" = <a483e7123456>' '      "VolGroupUUID" = "ECE575C2-96B8-422D-8655-3DFAA72AB0E0"' '  |   "Device Characteristics" = {"Serial Number"="204370802915","Product Name"="WDC PC SN530"}' \
		"Computer Name: Jane-MacBook" "          Display Serial Number: 2IV0171002681" "$h/jane/Library/x" "Hardware UUID: 01234567-89AB-CDEF-0123-456789ABCDEF" \
		'    |   "IOModel" = "Intel Wireless Card"' '    |   "WiFiConfig" = {"WiFi_1"={"password"="hunter2-at-home","ssid"="JaneHome@5G"}}' \
		'  |   "IOConsoleUsers" = ({"kCGSSessionUserNameKey"="jane","kCGSessionLongUserNameKey"="Jane Appleseed","kCGSSessionIDKey"=257})' \
		'      "Networks" = ({"ssid"="CafeGuest","psk"="letmein99"})' > "$d/t.txt"
	scrub_file "$d/t.txt"
	if grep -qiE "a4:83:e7|C02XYZ|2IV0171|ECE575C2|204370802915|a483e7|HomeNetwork|Neighbours|AirPods|Keyboard|11:22:33|Jane-MacBook|$h/jane|01234567-89AB" "$d/t.txt"; then r=1; else r=0; fi
	check "the scan drops serials, addresses, Wi-Fi networks, paired devices and names" "$r"
	if grep -qE 'hunter2|JaneHome|CafeGuest|letmein99|Appleseed|UserNameKey"="jane' "$d/t.txt"; then r=1; else r=0; fi
	check "the scan drops Wi-Fi passwords kept by a Wi-Fi kext and the logged-in user's names" "$r"
	if grep -q 'Firmware Version: wl0' "$d/t.txt" && grep -q 'Chipset: BCM_4364B3' "$d/t.txt" && grep -q 'Product Name"="WDC PC SN530' "$d/t.txt" && grep -q 'Card Type: Wi-Fi  (0x14E4, 0x7BF)' "$d/t.txt" && grep -q '"IOModel" = "Intel Wireless Card"' "$d/t.txt"; then r=0; else r=1; fi
	check "the scan keeps the Wi-Fi card, its firmware and the Bluetooth chipset" "$r"
	if grep -q '^Bluetooth Controller:' "$d/t.txt" && grep -q '(list removed)' "$d/t.txt"; then r=0; else r=1; fi
	check "a dropped list says so, and the next section survives it" "$r"
	rm -rf "$d"
	[ "$fails" = 0 ] && say "" && say "all checks passed" || say "$fails failed"
	return "$fails"
}

case "${1:-}" in
	--selftest) selftest ;;
	--report) find_report ;;
	"") make_stick ;;
	--scan) scan_mac ;;
	*) sed -n '2,9p' "$0"; exit 2 ;;
esac
