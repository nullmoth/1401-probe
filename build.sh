#!/bin/sh
# 1401 Probe: build everything in containers. Nothing is installed on this Mac.
#   ./build.sh image     the offline Linux stick   -> out/usb/  and  out/1401-probe.img
#   ./build.sh windows   the Windows installer     -> out/windows/1401-Probe-Setup.exe  (needs out/usb first)
#   ./build.sh all       both
set -eu
cd "$(dirname "$0")"
what=${1:-all}
# images are pinned by digest, not by tag: a tag is a pointer the registry can move, and whatever it points at
# builds the installer testers run as admin. To take an update, pull, read the new digest, and change it here.
ALPINE=alpine:3.24@sha256:294b683cb724975bec92580e1e685676bd4b50bda910ddb8c51d4cabeaec77e6
DOTNET=mcr.microsoft.com/dotnet/sdk:8.0@sha256:78235e09001f52b6592c458ac010775ebac6725422e80cd0c1650590f67b2743
# the names the leak scans hunt for live outside this repo: a pattern that spells the owner's name, committed to a
# public tree, is the leak. One extended regex on one line, kept at $NM_PRIVATE_PATTERNS (default below).
# No pattern file means no build, unless NM_ALLOW_NO_PRIVATE=1 (a CI runner, which never
# sees the owner's machine) says so out loud.
PF=${NM_PRIVATE_PATTERNS:-$HOME/.config/nullmoth/private-patterns}
NM_PRIVATE_RE=$(head -n 1 "$PF" 2>/dev/null || true)
if [ -z "$NM_PRIVATE_RE" ] && [ "${NM_ALLOW_NO_PRIVATE:-}" != 1 ]; then
	echo "error: no private name patterns at $PF: the leak scans cannot run (NM_ALLOW_NO_PRIVATE=1 only on CI)"; exit 1
fi
export NM_PRIVATE_RE
if [ "$what" = image ] || [ "$what" = all ]; then
	docker run --rm --platform linux/amd64 -v "$PWD":/work -w /work -e NM_PRIVATE_RE "$ALPINE" sh probe/image/build-inside.sh
fi
if [ "$what" = windows ] || [ "$what" = all ]; then
	[ -f out/usb/boot/initramfs.zst ] || { echo "error: build the image first: ./build.sh image"; exit 1; }
	docker run --rm --platform linux/amd64 -v "$PWD":/work -w /work -e NM_PRIVATE_RE "$DOTNET" bash windows/build-inside.sh
fi
