#!/usr/bin/env python3
"""
Emulate the MVCI32.dll read routine with Unicorn to prove the hotfix:
  * the ORIGINAL code overruns the 5000-byte buffer and faults (the crash);
  * the PATCHED code, on a desynced stream, zeroes the caller's output length
    and branches to the routine's clean exit (RVA 0x14FF) — no fault, no loop;
  * zeroing the output length is what prevents the downstream caller crash
    (the read at RVA 0x2AAB that uses the length);
  * normal in-bounds reads copy the exact same bytes and reach the loop tail.
"""

import sys
from unicorn import *
from unicorn.x86_const import *

import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from mvci_patch import (  # noqa: E402
    BUF_SIZE, build_stub, build_hook,
    HOOK_RVA, RESUME_RVA, ABORT_RVA, HOOK_ORIG, OUTLEN_ARG_OFF,
)

LOOP_FULL = bytes.fromhex(
    "8b4c241c40" "8a4c08ff" "884c02ff" "8b4c2418" "3bc1" "72eb"
)
TAIL = bytes.fromhex("8b742414" "909090909090" "03f1" "89742414")

BASE = 0x00400000
STUB = BASE + 0x8000
END = BASE + 0x13B3
ABORT = BASE + ABORT_RVA

HEAP = 0x10000000
HEAP_SZ = 0x2000
BUFA = HEAP + HEAP_SZ - BUF_SIZE
CHUNKBUF = 0x20000000
STACK = 0x30000000
STACK_SZ = 0x4000
ESP = STACK + 0x1000
OUTLEN_SLOT = ESP + 0x200       # the caller's output-length variable
SENTINEL = 0x41414141            # a large non-zero "stale length"


def layout(patched):
    code = bytearray(0x1600)
    code[HOOK_RVA:RESUME_RVA] = build_hook(BASE + HOOK_RVA, STUB) if patched else HOOK_ORIG
    code[RESUME_RVA:RESUME_RVA + len(LOOP_FULL)] = LOOP_FULL
    code[0x13A3:0x13A3 + len(TAIL)] = TAIL
    code[ABORT_RVA] = 0x90
    return bytes(code)


def new_engine(patched):
    mu = Uc(UC_ARCH_X86, UC_MODE_32)
    mu.mem_map(BASE, 0x10000)
    mu.mem_write(BASE, layout(patched))
    if patched:
        mu.mem_write(STUB, build_stub(STUB, BASE + RESUME_RVA, ABORT))
    mu.mem_map(HEAP, HEAP_SZ)
    mu.mem_map(CHUNKBUF, 0x2000)
    mu.mem_map(STACK, STACK_SZ)
    mu.mem_write(CHUNKBUF, bytes((i * 31 + 7) & 0xFF for i in range(0x2000)))

    def stop(uc, address, size, _):
        if address in (END, ABORT):
            uc.emu_stop()
    mu.hook_add(UC_HOOK_CODE, stop)
    return mu


def feed(mu, total, chunk):
    """Run one chunk. Returns (result, outlen) where result is new total,
    'ABORT' or 'FAULT', and outlen is the caller's length slot afterwards."""
    mu.reg_write(UC_X86_REG_EAX, 0)
    mu.reg_write(UC_X86_REG_ECX, chunk)
    mu.reg_write(UC_X86_REG_ESI, total)
    mu.reg_write(UC_X86_REG_EBX, BUFA)
    mu.reg_write(UC_X86_REG_EDI, 0)
    mu.reg_write(UC_X86_REG_ESP, ESP)
    mu.mem_write(ESP + 0x14, total.to_bytes(4, "little"))
    mu.mem_write(ESP + 0x18, chunk.to_bytes(4, "little"))
    mu.mem_write(ESP + 0x1C, CHUNKBUF.to_bytes(4, "little"))
    mu.mem_write(ESP + OUTLEN_ARG_OFF, OUTLEN_SLOT.to_bytes(4, "little"))   # arg2 ptr
    mu.mem_write(OUTLEN_SLOT, SENTINEL.to_bytes(4, "little"))               # stale length
    try:
        mu.emu_start(BASE + HOOK_RVA, 0, timeout=0, count=200000)
        res = "ABORT" if mu.reg_read(UC_X86_REG_EIP) == ABORT else \
            int.from_bytes(mu.mem_read(ESP + 0x14, 4), "little")
    except UcError:
        res = "FAULT"
    outlen = int.from_bytes(mu.mem_read(OUTLEN_SLOT, 4), "little")
    return res, outlen


def run():
    ok = True

    mu = new_engine(False)
    total, faulted_at = 0, None
    for n in range(1, 30):
        r, _ = feed(mu, total, 4096)
        if r == "FAULT":
            faulted_at = n
            break
        total = r
    print(f"[original] overran the buffer and faulted on chunk #{faulted_at} (the crash)")
    ok &= faulted_at is not None

    mu = new_engine(True)
    r1, o1 = feed(mu, 0, 4096)
    r2, o2 = feed(mu, r1, 4096)
    print(f"[patched]  chunk #1 -> total {r1} (outlen untouched={o1 == SENTINEL}); "
          f"chunk #2 overflow -> {r2}, outlen now {o2}")
    ok &= (r1 == 4096 and r2 == "ABORT" and o2 == 0)

    mu = new_engine(True)
    res = [feed(mu, 4000, 4000) for _ in range(500)]
    clean = all(r == "ABORT" and o == 0 for r, o in res)
    print(f"[patched]  500 desynced reads -> all ABORT with outlen zeroed: {clean}")
    ok &= clean

    mu = new_engine(True)
    r, o = feed(mu, 0, 5500)
    print(f"[patched]  5500-byte read on empty buffer -> total {r} (capped, normal exit)")
    ok &= (r == BUF_SIZE)

    mu = new_engine(True)
    src = mu.mem_read(CHUNKBUF, 0x2000)
    mirror = bytearray(BUF_SIZE)
    total = 0
    bad = False
    for chunk in [1000, 2000, 1000, 1000]:
        r, _ = feed(mu, total, chunk)
        mirror[total:total + chunk] = src[:chunk]
        if r != total + chunk or bytes(mu.mem_read(BUFA, BUF_SIZE)) != bytes(mirror):
            print(f"[patched]  mismatch at chunk {chunk}: {r}")
            bad = True
            break
        total = r
    if not bad:
        print(f"[patched]  filled to exactly {total} bytes, identical bytes to the original")
        ok &= (total == BUF_SIZE)
        r, o = feed(mu, total, 1)
        ok &= (r == "ABORT" and o == 0)
    ok &= not bad

    print("\nRESULT:", "all checks passed" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
