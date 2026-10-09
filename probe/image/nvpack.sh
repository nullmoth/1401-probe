#!/bin/sh
# 1401 Probe: NVIDIA driver packs, built inside the same Alpine container as the image (build.sh nvpack).
#
# The trace step loads NVIDIA's own driver for the card in the machine and records what it does to the hardware,
# so every NVIDIA generation that has run macOS needs a driver that builds against the stick's kernel:
#   open    610.57.04   Turing and newer (GSP).  The same release the macOS RM is built from.
#   closed  580.178.04  Maxwell, Pascal, Volta   (580 is the last branch that supports them).
#   closed  470.256.02  Kepler                   (470 is the last branch that supports it).
#   Fermi and Tesla: nouveau only (390/340 do not build on a current kernel; nouveau drives both fully).
#
# Licence: NVIDIA driver licence 1.1(d) allows distribution with an OSI-licensed kernel when the binary files are
# not modified and the licence travels with them. Only the kernel glue is compiled here; nv-kernel.o_binary is
# linked as shipped, never stripped or edited, and each pack carries NVIDIA's LICENSE file.
#
# Packs are NOT in the initramfs (the stick runs from RAM; these are ~200 MB). They sit on the stick under
# drivers/ and the probe unpacks only the one the card needs, only for the trace step.
set -eu
say() { printf '\n== %s\n' "$*"; }
cd /work
OUT=out/nvpack
mkdir -p "$OUT" /tmp/nv
apk add -q gcc make musl-dev linux-lts-dev binutils xz coreutils bash perl findutils kmod gcompat >/dev/null
V=$(ls /usr/src | sed -n 's/^linux-headers-//p' | head -1)
[ -n "$V" ] || { echo "error: no linux-lts headers"; exit 1; }
echo "kernel $V"
URL=https://download.nvidia.com/XFree86/Linux-x86_64

# version  kind    archs (PMC_BOOT_42 architecture bytes)            sha256 of the .run (NVIDIA's own .sha256sum)
PACKS="610.57.04 open 16,17,18,19,1a,1b b2e935c66b83bb00c0c857bc8e0ee0fd52de9286b40c9cc1eec29a7ce7eb116d
580.178.04 closed 11,12,13,14 5975a86ee45bffcb626f51ae33d1169b108186a2ea47ad651e72f13fa4b6d6f9
470.256.02 closed e,f,10 d6451862deb695bb0447f3b7cd6268f73e81168c10e2c10597ff3fa01349b1de"

KEY=$(cat probe/image/nvpack.sh | sha256sum | cut -c1-12)
echo "$PACKS" | while read -r VER KIND ARCHS SUM; do
	NAME="nvidia-$KIND-$VER"
	# NVPACK_ONLY=<version> builds one pack (several containers in parallel); a lock keeps two off one build dir
	[ -z "${NVPACK_ONLY:-}" ] || [ "$NVPACK_ONLY" = "$VER" ] || continue
	PK="$OUT/$NAME.tar.xz"
	STAMP="$OUT/$NAME.stamp"
	if [ -f "$PK" ] && [ "$(cat "$STAMP" 2>/dev/null)" = "$V $KEY" ]; then
		echo "$NAME: cached for $V"; continue
	fi
	mkdir -p /work/out/nvbuild
	if ! mkdir "/work/out/nvbuild/$VER.lock" 2>/dev/null; then echo "$NAME: another run is building it, skipped here"; continue; fi
	trap 'rmdir /work/out/nvbuild/*.lock 2>/dev/null' EXIT
	rm -f "$PK" "$STAMP" "$OUT/$NAME.failed"
	say "$NAME"
	RUN="/work/out/nvdl/NVIDIA-Linux-x86_64-$VER.run"
	mkdir -p /work/out/nvdl
	if ! echo "$SUM  $RUN" | sha256sum -c --status 2>/dev/null; then
		wget -q -O "$RUN.part" "$URL/$VER/NVIDIA-Linux-x86_64-$VER.run"
		mv "$RUN.part" "$RUN"
	fi
	echo "$SUM  $RUN" | sha256sum -c --status || { echo "error: $RUN does not match NVIDIA's published sha256"; exit 1; }
	# kept in out/ until the pack is made: make is incremental, so a rerun after a packing error does not recompile
	X=/work/out/nvbuild/$VER
	[ -f "$X/.extracted" ] || rm -rf "$X"
	# the .run unpacks itself with a glibc helper (gcompat runs it on musl) and refuses a /tmp it thinks is noexec
	mkdir -p /root/nvtmp
	if [ ! -f "$X/.extracted" ]; then
		TMPDIR=/root/nvtmp bash "$RUN" -x --target "$X" > "$OUT/$NAME.extract.log" 2>&1 || { echo "error: could not unpack $RUN"; tail -5 "$OUT/$NAME.extract.log"; exit 1; }
		touch "$X/.extracted"
	fi
	if [ "$KIND" = open ]; then SRC="$X/kernel-open"; else SRC="$X/kernel"; fi
	J=$(nproc)
	# a closed branch that no longer builds on this kernel is recorded, not fatal: those cards fall back to nouveau
	if ! make -C "$SRC" -j"$J" SYSSRC=/usr/src/linux-headers-$V modules >"$OUT/$NAME.build.log" 2>&1; then
		echo "  $NAME does NOT build on kernel $V (log: $OUT/$NAME.build.log); its cards use nouveau"
		tail -5 "$OUT/$NAME.build.log"
		echo "$V $KEY" > "$OUT/$NAME.failed"
		rmdir "/work/out/nvbuild/$VER.lock"
		continue
	fi
	S=/tmp/nv/stage-$VER
	rm -rf "$S"
	mkdir -p "$S/modules" "$S/firmware/nvidia/$VER" "$S/licenses"
	for m in nvidia nvidia-modeset nvidia-drm nvidia-uvm; do
		[ -f "$SRC/$m.ko" ] || { echo "error: $NAME built no $m.ko"; exit 1; }
		cp "$SRC/$m.ko" "$S/modules/"
	done
	# open modules are MIT/GPL: debug info stripped. The closed ones are linked from NVIDIA's binary and stay as built.
	[ "$KIND" = open ] && strip --strip-debug "$S"/modules/*.ko
	if [ "$KIND" = open ]; then
		ls "$X"/firmware/*.bin >/dev/null 2>&1 || { echo "error: no GSP firmware in $VER"; exit 1; }
		cp "$X"/firmware/*.bin "$S/firmware/nvidia/$VER/"
		# the open modules ship no COPYING file: their licence is the MIT text in every source header
		# (MODULE_LICENSE "Dual MIT/GPL"), so that header travels as the licence text
		sed -n '1,/\*\//p' "$SRC/nvidia/nv.c" > "$S/licenses/LICENSE.kernel-open"
		grep -q "Permission is hereby granted" "$S/licenses/LICENSE.kernel-open" || { echo "error: no MIT text in nv.c's header"; exit 1; }
	fi
	cp "$X/LICENSE" "$S/licenses/LICENSE.nvidia"
	for m in "$S"/modules/*.ko; do
		modinfo -F vermagic "$m" | grep -q "^$V " || { echo "error: $m vermagic is not $V"; exit 1; }
	done
	cat > "$S/pack.json" <<EOF
{"name": "$NAME", "version": "$VER", "kind": "$KIND", "kernel": "$V", "archs": "$ARCHS",
 "load": ["nvidia", "nvidia-modeset", "nvidia-drm"], "run_sha256": "$SUM"}
EOF
	(cd "$S" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > /tmp/nvsums && mv /tmp/nvsums SHA256SUMS)
	tar -C "$S" -cf - . | xz -6 -T0 > "$PK.part"
	mv "$PK.part" "$PK"
	echo "$V $KEY" > "$STAMP"
	rmdir "/work/out/nvbuild/$VER.lock"
	ls -la "$PK"
	rm -rf "$X" "$S"
done
say "packs"
ls -la "$OUT"
