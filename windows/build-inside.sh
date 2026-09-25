#!/bin/bash
# 1401 Probe: Windows side. Runs inside mcr.microsoft.com/dotnet/sdk:8.0 (see ../build.sh); installs nothing on the Mac.
# Builds the .NET Framework 4.8 wizard (every Windows 10/11 already has 4.8, so no runtime to install), then wraps it,
# the stick payload (out/usb) and probe.json into one NSIS installer: out/windows/1401-Probe-Setup.exe
set -euo pipefail
cd /work
VERSION=$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' probe/overlay/usr/lib/1401-probe/probe.py)
[ -n "$VERSION" ] || { echo "error: could not read VERSION from probe.py"; exit 1; }

echo "== app ($VERSION)"
rm -rf out/windows && mkdir -p out/windows
dotnet build windows/Probe/Probe.csproj -c Release -nologo -v q -o out/windows/app

echo "== nsis"
apt-get update -qq >/dev/null && apt-get install -y -qq nsis binutils >/dev/null
makensis -VERSION; echo

stage=out/windows/stage
mkdir -p "$stage/payload"
cp out/windows/app/1401Probe.exe "$stage/"
[ -f out/windows/app/1401Probe.exe.config ] && cp out/windows/app/1401Probe.exe.config "$stage/"
cp windows/probe.json "$stage/"
cp windows/Probe/app.ico "$stage/"
# the license, the instructions, and the open-source notices the image build wrote (they also ride on the stick)
mkdir -p "$stage/docs"
cp docs/LICENSE.txt docs/instructions.html out/usb/THIRD-PARTY-NOTICES.txt out/usb/GPL-3.0.txt "$stage/docs/"
cp -r out/usb/. "$stage/payload/"
# the app refuses a stick whose files do not match SHA256SUMS, so the payload must match it before it ships
( cd "$stage/payload" && sha256sum -c --quiet SHA256SUMS ) || { echo "error: payload does not match its SHA256SUMS"; exit 1; }
echo "payload: $(du -sh "$stage/payload" | cut -f1), $(find "$stage/payload" -type f | wc -l) files"

# nothing that names a person or the build machine may ship. Build-machine paths are hunted here; names come from
# $NM_PRIVATE_RE (outside the repo, see ../build.sh). The scan has two jobs, and they are different:
#  1. Every file that goes into the installer, before packing: a hit there is a real leak, and the build stops.
# 2. The raw installer: its solid-lzma stream is noise, and noise spells short name fragments by accident (one
#     build had 1 five-byte hit, the next 2, while all 28 unpacked files had 0). Not a leak, but `strings | grep` on the
#     download would show one, so it is repacked with the next nonce until the raw exe is clean as well.
scan() {  # prints the count of identifying strings in $1, never the strings: a hit is the name
	# a scan that cannot read its file must stop the build, never count 0 (under set -e, c=$(scan ...) exits here)
	local s; s=$(strings -a -e l "$1" && strings -a "$1") || { echo "error: strings could not read $1" >&2; exit 1; }
	# the one upstream path allowed through, stripped as an exact literal so the rest of its line is still scanned:
	# Alpine builds every package as user "buildozer", and its GRUB (payload/EFI/BOOT/BOOTX64.EFI) carries 335
	# "/home/buildozer/aports/main/grub/src/..." source paths. That is Alpine's builder, not ours. Any other
	# /home/ or /Users/ path still counts.
	printf '%s\n' "$s" | sed 's#/home/buildozer/##g' \
	  | grep -c -i -E "/Users/|/home/|\.pdb${NM_PRIVATE_RE:+|$NM_PRIVATE_RE}" || true
}
n=0
while IFS= read -r -d '' f; do
	c=$(scan "$f"); [ "$c" -eq 0 ] || { echo "error: $c identifying string(s) in ${f#"$stage"/}"; n=$((n + c)); }
done < <(find out/windows/app/1401Probe.exe "$stage" -type f -print0)
[ "$n" -eq 0 ] || exit 1
nonce_file="$PWD/out/windows/pack-nonce.txt"
for nonce in $(seq 1 40); do
	printf '1401 Probe %s packing nonce %s\n' "$VERSION" "$nonce" > "$nonce_file"
	makensis -V2 -DVERSION="$VERSION" -DSTAGE="$PWD/$stage" -DNONCE="$nonce_file" -DOUT="$PWD/out/windows/1401-Probe-Setup.exe" windows/installer.nsi
	n=$(scan out/windows/1401-Probe-Setup.exe)
	[ "$n" -eq 0 ] && break
	echo "packing nonce $nonce: $n chance fragment(s) in the compressed stream, repacking"
done
rm -rf "$stage" "$nonce_file"
# 40 packings at ~1.5 fragments each would all carry one with odds of e^-60: that is not noise, stop
[ "$n" -eq 0 ] || { echo "error: every packing carried an identifying string: look at the inputs"; exit 1; }
# never echo $NM_PRIVATE_RE itself: it is the names. Report only whether it was applied.
if [ -n "${NM_PRIVATE_RE:-}" ]; then pp=applied; else pp="NOT applied"; fi
echo "identifying strings in the exe and installer: none (private patterns $pp)"
ls -l out/windows/1401-Probe-Setup.exe
sha256sum out/windows/1401-Probe-Setup.exe
