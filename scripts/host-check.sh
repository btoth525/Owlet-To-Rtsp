#!/usr/bin/env bash
# STEP 0 gate — run this on the Unraid HOST before anything else.
# redroid needs Android binder/ashmem kernel support. If it's missing, redroid
# won't boot and nothing else matters.
#
#   bash scripts/host-check.sh
set -u

ok()   { printf '  \033[32mOK\033[0m   %s\n' "$1"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$1"; }
warn() { printf '  \033[33mWARN\033[0m %s\n' "$1"; }

echo "== Owlet-To-Rtsp host prerequisite check =="
echo

problems=0

echo "[1] binder support"
if [ -e /dev/binder ] || [ -e /dev/binderfs/binder ] || ls /dev/binder* >/dev/null 2>&1; then
  ok "binder device present"
elif grep -q binder /proc/filesystems 2>/dev/null; then
  warn "binderfs is available but no /dev/binder* yet (redroid can create it). Likely fine."
elif lsmod 2>/dev/null | grep -q binder; then
  ok "binder_linux module loaded"
else
  bad "no binder device, binderfs, or binder_linux module found"
  problems=$((problems+1))
fi

echo "[2] ashmem support"
if [ -e /dev/ashmem ]; then
  ok "/dev/ashmem present"
elif lsmod 2>/dev/null | grep -q ashmem; then
  ok "ashmem_linux module loaded"
else
  warn "no ashmem device/module. Modern kernels (5.x+, incl. recent Unraid) use memfd instead — usually fine on redroid 11+."
fi

echo "[3] /proc/filesystems"
grep -q binder /proc/filesystems 2>/dev/null && ok "binderfs listed" || warn "binderfs not listed in /proc/filesystems"

echo "[4] docker + privileged"
if command -v docker >/dev/null 2>&1; then
  ok "docker present ($(docker --version 2>/dev/null))"
else
  warn "docker CLI not found in PATH (fine if you only use the Unraid UI)"
fi

echo "[5] NVIDIA (for NVENC encoding)"
if command -v nvidia-smi >/dev/null 2>&1; then
  ok "nvidia-smi present: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)"
else
  warn "nvidia-smi not found. Install the Unraid 'Nvidia-Driver' plugin for NVENC, or set ENCODER=libx264."
fi

echo
if [ "$problems" -gt 0 ]; then
  cat <<'EOF'
RESULT: binder support looks MISSING.

Fix options (host level):
  * Add to /boot/config/go (Unraid), then reboot:
        modprobe binder_linux devices="binder,hwbinder,vndbinder"
        modprobe ashmem_linux         # only if your kernel has it
  * If the stock Unraid kernel lacks these entirely, you need a custom kernel
    (e.g. the community "Unraid-Kernel-Helper" container). This is THE one true
    blocker — sort it before deploying the stack.
EOF
  exit 1
else
  echo "RESULT: host looks ready for redroid. Proceed with deployment."
fi
