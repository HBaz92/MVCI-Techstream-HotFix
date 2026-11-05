#!/usr/bin/env python3
"""
mvci_patch.py - In-memory hotfix for the heap overflow / hang in MVCI32.dll
(Mini-VCI J2534 driver, file version 1.0.0.1, used by Toyota Techstream).

The bug (read routine at RVA 0x1300, confirmed from a crash dump):
  * Two 5000-byte (0x1388) heap buffers are allocated: an accumulation
    buffer (ebx) and a chunk buffer ([esp+1Ch]).
  * Each read asks for up to 5500 (0x157C) bytes into the 5000-byte chunk buffer.
  * Every chunk is appended to the accumulation buffer at offset `total`
    with NO bounds check (RVA 0x138E..0x13A1).
  * The read loop only exits after 10 s with NO data, or on a complete frame.
    But RVA 0x13B5 refreshes the "last data" timer on EVERY chunk, garbage
    included. So when the cable desyncs and streams garbage non-stop, `total`
    grows without bound (0x40F3A = 266,042 bytes in the dump) and the driver
    overruns the heap until Techstream crashes.

The fix (applied after the packed DLL has unpacked itself in memory):
  1. RVA 0x1361: request at most 5000 bytes per read (push 157Ch -> push 1388h).
  2. RVA 0x1382: jump to a small stub that
       - caps the chunk length at 5000 bytes, and
       - when total + chunk would exceed the 5000-byte buffer (i.e. the stream
         has desynced), jumps to the routine's own clean exit at RVA 0x14FF -
         the same epilogue it already uses on a normal timeout: it frees both
         buffers and returns, handing control back to Techstream, which simply
         re-issues the request. No crash, no hang; at worst one reading is
         skipped and the next cycle recovers.
     Otherwise it resumes the original copy loop at RVA 0x138E unchanged.
  3. RVA 0x39CF (inside PassThruReadVersion): the version routine calls the
     0x1300 read, then copies `length` bytes from the accumulation buffer into
     a small (~0x32-byte) version buffer with the loop at RVA 0x39E0, using the
     returned length as the bound WITHOUT clamping it. On a desync the read
     returns a garbage length (0x10290 seen in the crash dump) and the loop
     runs off the end of the source buffer -> access violation at MVCI32+0x39E0.
     The call itself requested at most 0x32 bytes (push 32h), so clamp the
     stored length to 0x32 before the loop. Legitimate versions (<= 0x32) are
     unaffected; a garbage length is capped and the read is skipped, not crashed.

Nothing on disk is modified. Run as Administrator, leave it running, then
start Techstream and connect to the vehicle.

Usage:
  python mvci_patch.py                 watch and patch every Techstream session
  python mvci_patch.py --check         only report what it finds, write nothing
  python mvci_patch.py --once          scan one time and exit
  python mvci_patch.py --dump FILE     save the unpacked MVCI32.dll image to FILE
                                       (Techstream must be running with the cable
                                        connected so the driver is loaded)
"""

import argparse
import ctypes
import struct
import sys
import time
from datetime import datetime

# ---------------------------------------------------------------------------
# Patch definition (offsets are relative to the MVCI32.dll load address)
# ---------------------------------------------------------------------------

BUF_SIZE = 0x1388  # 5000 bytes, the size of both driver buffers

READ_SIZE_RVA = 0x1361
READ_SIZE_ORIG = bytes.fromhex("687c150000")  # push 157Ch
READ_SIZE_NEW = bytes.fromhex("6888130000")   # push 1388h

HOOK_RVA = 0x1382
HOOK_ORIG = bytes.fromhex("85c9" "c644241201" "7622" "8d141e")  # 12 bytes
HOOK_LEN = len(HOOK_ORIG)

RESUME_RVA = 0x138E   # back into the original copy loop
ABORT_RVA = 0x14FF    # the routine's own clean exit (frees buffers, returns)
OUTLEN_ARG_OFF = 0x40  # [esp+40h] at baseline = arg2 = pointer to the output length

# Copy-loop body; never modified, used to confirm the DLL version.
LOOP_ORIG = bytes.fromhex("8b4c241c" "40" "8a4c08ff" "884c02ff")

CHECK_START = READ_SIZE_RVA
CHECK_END = RESUME_RVA + len(LOOP_ORIG)

# --- Fix 2: PassThruReadVersion copy-loop overflow (function at RVA 0x38D0) ---
# After calling 0x1300, the version routine runs a byte copy loop at RVA 0x39E0
# bounded by the returned length at [esp+10h], with no clamp. A desynced read
# returns a garbage length, so the loop reads past the source buffer and faults
# at MVCI32+0x39E0 (confirmed: eax=0x10290, esi=src, [eax+esi] unmapped). The
# call requested at most 0x32 bytes, so clamp the stored length to 0x32 first.
VER_HOOK_RVA = 0x39CF
VER_HOOK_ORIG = bytes.fromhex("8b4c2410" "8b542450")  # mov ecx,[esp+10h]; mov edx,[esp+50h]
VER_HOOK_LEN = len(VER_HOOK_ORIG)                      # 8 bytes overwritten
VER_RESUME_RVA = 0x39D7                                # mov eax,1 (first byte after the hook)
VER_MAXLEN = 0x32                                      # requested max (arg4 = push 32h)
VER_LEN_STK_OFF = 0x10                                 # [esp+10h] = returned-length slot


def build_stub(stub_addr: int, resume_addr: int, abort_addr: int) -> bytes:
    """x86 code executed instead of RVA 0x1382..0x138D.

    On entry: eax = 0, ecx = bytes read (> 0), esi = total, ebx = buffer,
    [esp+14h] = total, [esp+18h] = bytes read, [esp+12h] = 'got data' flag,
    [esp+40h] = pointer to the caller's output-length slot (arg2).
    esp is at the routine's baseline, so jumping to the 0x14FF epilogue is safe.

    Normal chunk  -> cap at 5000, restore the displaced `lea edx,[esi+ebx]`,
                     resume the copy loop at 0x138E.
    Desynced chunk (total+chunk would overrun the 5000-byte buffer)
                  -> set the caller's output length to 0, then jump to the
                     routine's clean exit (0x14FF). Zeroing the length is what
                     stops callers of 0x1300 from copying a stale/garbage length
                     out of the (unfilled) buffer and crashing downstream
                     (e.g. the read at RVA 0x2AAB).
    """
    size = struct.pack("<I", BUF_SIZE)
    outoff = struct.pack("<B", OUTLEN_ARG_OFF)
    code = bytearray()
    code += bytes.fromhex("c644241201")    # mov  byte ptr [esp+12h], 1   (original)
    code += bytes.fromhex("81f9") + size   # cmp  ecx, 5000
    code += bytes.fromhex("7609")          # jbe  +9  (skip the cap)
    code += b"\xb9" + size                 # mov  ecx, 5000
    code += bytes.fromhex("894c2418")      # mov  [esp+18h], ecx          (cap chunk)
    code += bytes.fromhex("8d140e")        # lea  edx, [esi+ecx]          (total + chunk)
    code += bytes.fromhex("81fa") + size   # cmp  edx, 5000

    ja_at = len(code)
    code += b"\x00" * 6                     # placeholder: ja abort_local

    code += bytes.fromhex("8d141e")        # lea  edx, [esi+ebx]          (original dest)
    jmp_resume_at = len(code)
    code += b"\x00" * 5                     # placeholder: jmp resume

    abort_local = len(code)
    code += b"\x8b\x44\x24" + outoff       # mov  eax, [esp+40h]          (outlen ptr)
    code += bytes.fromhex("85c0")          # test eax, eax
    code += bytes.fromhex("7406")          # jz   +6  (skip the store if null)
    code += bytes.fromhex("c70000000000")  # mov  dword ptr [eax], 0      (*outlen = 0)
    jmp_abort_at = len(code)
    code += b"\x00" * 5                     # placeholder: jmp 0x14FF

    code[ja_at:ja_at + 2] = b"\x0f\x87"    # ja  (above / unsigned >)
    code[ja_at + 2:ja_at + 6] = struct.pack(
        "<I", (stub_addr + abort_local - (stub_addr + ja_at + 6)) & 0xFFFFFFFF)
    code[jmp_resume_at] = 0xE9
    code[jmp_resume_at + 1:jmp_resume_at + 5] = struct.pack(
        "<I", (resume_addr - (stub_addr + jmp_resume_at + 5)) & 0xFFFFFFFF)
    code[jmp_abort_at] = 0xE9
    code[jmp_abort_at + 1:jmp_abort_at + 5] = struct.pack(
        "<I", (abort_addr - (stub_addr + jmp_abort_at + 5)) & 0xFFFFFFFF)
    return bytes(code)


def build_hook(hook_addr: int, stub_addr: int) -> bytes:
    rel = (stub_addr - (hook_addr + 5)) & 0xFFFFFFFF
    return b"\xe9" + struct.pack("<I", rel) + b"\x90" * (HOOK_LEN - 5)


def build_ver_stub(stub_addr: int, resume_addr: int) -> bytes:
    """x86 code executed instead of RVA 0x39CF..0x39D6.

    Clamp the version length at [esp+10h] to VER_MAXLEN, replay the two
    displaced instructions (mov ecx,[esp+10h]; mov edx,[esp+50h]), then jump
    back to RVA 0x39D7. Reached by a jmp (esp unchanged), so [esp+10h] and
    [esp+50h] are exactly the slots the original code reads. The loop at 0x39E0
    re-reads [esp+10h] every iteration, so clamping the stored value (not just a
    register) is what bounds the copy.
    """
    maxb = struct.pack("<B", VER_MAXLEN)
    maxd = struct.pack("<I", VER_MAXLEN)
    off = struct.pack("<B", VER_LEN_STK_OFF)
    code = bytearray()
    code += b"\x83\x7c\x24" + off + maxb   # cmp  dword ptr [esp+10h], 32h
    code += bytes.fromhex("7608")          # jbe  +8  (length already <= 32h)
    code += b"\xc7\x44\x24" + off + maxd   # mov  dword ptr [esp+10h], 32h
    code += bytes.fromhex("8b4c2410")      # mov  ecx, [esp+10h]   (displaced)
    code += bytes.fromhex("8b542450")      # mov  edx, [esp+50h]   (displaced)
    jmp_at = len(code)
    code += b"\x00" * 5                     # placeholder: jmp resume
    code[jmp_at] = 0xE9
    code[jmp_at + 1:jmp_at + 5] = struct.pack(
        "<I", (resume_addr - (stub_addr + jmp_at + 5)) & 0xFFFFFFFF)
    return bytes(code)


def build_ver_hook(hook_addr: int, stub_addr: int) -> bytes:
    rel = (stub_addr - (hook_addr + 5)) & 0xFFFFFFFF
    return b"\xe9" + struct.pack("<I", rel) + b"\x90" * (VER_HOOK_LEN - 5)


# ---------------------------------------------------------------------------
# Windows API
# ---------------------------------------------------------------------------

TARGET_PROCESSES = {"techstream.exe", "mainmenu.exe"}
TARGET_MODULE = "mvci32.dll"
POLL_SECONDS = 0.2

PROCESS_ACCESS = 0x0008 | 0x0010 | 0x0020 | 0x0400  # VM_OPERATION | VM_READ | VM_WRITE | QUERY_INFORMATION
TH32CS_SNAPPROCESS = 0x02
TH32CS_SNAPMODULE = 0x08
TH32CS_SNAPMODULE32 = 0x10
MEM_COMMIT = 0x1000
MEM_RESERVE = 0x2000
MEM_RELEASE = 0x8000
PAGE_EXECUTE_READWRITE = 0x40
ERROR_BAD_LENGTH = 24

DWORD = ctypes.c_uint32
HANDLE = ctypes.c_void_p
LPVOID = ctypes.c_void_p
SIZE_T = ctypes.c_size_t
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("cntUsage", DWORD),
        ("th32ProcessID", DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", DWORD),
        ("cntThreads", DWORD),
        ("th32ParentProcessID", DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("th32ModuleID", DWORD),
        ("th32ProcessID", DWORD),
        ("GlblcntUsage", DWORD),
        ("ProccntUsage", DWORD),
        ("modBaseAddr", ctypes.c_void_p),
        ("modBaseSize", DWORD),
        ("hModule", ctypes.c_void_p),
        ("szModule", ctypes.c_wchar * 256),
        ("szExePath", ctypes.c_wchar * 260),
    ]


kernel32 = None


def init_api():
    global kernel32
    if sys.platform != "win32":
        sys.exit("This tool only runs on Windows.")
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [DWORD, DWORD]
    k.CreateToolhelp32Snapshot.restype = HANDLE
    for name, entry_type in (
        ("Process32FirstW", PROCESSENTRY32W),
        ("Process32NextW", PROCESSENTRY32W),
        ("Module32FirstW", MODULEENTRY32W),
        ("Module32NextW", MODULEENTRY32W),
    ):
        fn = getattr(k, name)
        fn.argtypes = [HANDLE, ctypes.POINTER(entry_type)]
        fn.restype = ctypes.c_int
    k.OpenProcess.argtypes = [DWORD, ctypes.c_int, DWORD]
    k.OpenProcess.restype = HANDLE
    k.CloseHandle.argtypes = [HANDLE]
    k.CloseHandle.restype = ctypes.c_int
    k.ReadProcessMemory.argtypes = [HANDLE, LPVOID, LPVOID, SIZE_T, ctypes.POINTER(SIZE_T)]
    k.ReadProcessMemory.restype = ctypes.c_int
    k.WriteProcessMemory.argtypes = [HANDLE, LPVOID, LPVOID, SIZE_T, ctypes.POINTER(SIZE_T)]
    k.WriteProcessMemory.restype = ctypes.c_int
    k.VirtualAllocEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD, DWORD]
    k.VirtualAllocEx.restype = LPVOID
    k.VirtualFreeEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD]
    k.VirtualFreeEx.restype = ctypes.c_int
    k.VirtualProtectEx.argtypes = [HANDLE, LPVOID, SIZE_T, DWORD, ctypes.POINTER(DWORD)]
    k.VirtualProtectEx.restype = ctypes.c_int
    k.FlushInstructionCache.argtypes = [HANDLE, LPVOID, SIZE_T]
    k.FlushInstructionCache.restype = ctypes.c_int
    kernel32 = k


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def iter_target_processes():
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap in (None, INVALID_HANDLE_VALUE):
        return []
    found = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in TARGET_PROCESSES:
                found.append((entry.th32ProcessID, entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return found


def find_module(pid: int):
    for _ in range(5):
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snap not in (None, INVALID_HANDLE_VALUE):
            break
        if ctypes.get_last_error() != ERROR_BAD_LENGTH:
            return None
        time.sleep(0.05)
    else:
        return None
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szModule.lower() == TARGET_MODULE:
                return entry.modBaseAddr, entry.modBaseSize, entry.szExePath
            ok = kernel32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return None


# Other modules worth reporting (e.g. the J2534 proxy shim), so one run shows
# whether Techstream actually loaded them, live in this window.
WATCH_MODULES = ("j2534_proxy.dll",)


def enumerate_modules(pid: int):
    """Return {lowercased module name: (base, size, path)} for a process."""
    for _ in range(5):
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
        if snap not in (None, INVALID_HANDLE_VALUE):
            break
        if ctypes.get_last_error() != ERROR_BAD_LENGTH:
            return {}
        time.sleep(0.05)
    else:
        return {}
    out = {}
    try:
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = kernel32.Module32FirstW(snap, ctypes.byref(entry))
        while ok:
            out[entry.szModule.lower()] = (entry.modBaseAddr, entry.modBaseSize, entry.szExePath)
            ok = kernel32.Module32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def report_watched(states: dict, pid: int, exe: str, mods: dict) -> None:
    """Log, once per change, whether each watched module is loaded in a process."""
    for name in WATCH_MODULES:
        key = ("watch", pid, name)
        m = mods.get(name)
        if m:
            base, _size, path = m
            if states.get(key) != "loaded":
                log(f"{name}: LOADED in {exe} (PID {pid}) @ 0x{base:08X} from {path}")
                states[key] = "loaded"
        elif states.get(key) == "loaded":
            log(f"{name}: unloaded from {exe} (PID {pid})")
            states[key] = "absent"
        else:
            states[key] = "absent"


def dump_module(handle, base: int, size: int, path: str):
    """Read the whole in-memory (unpacked) module image to a file.

    Unreadable pages are written as zeros so every byte keeps its RVA, which
    lets the image be disassembled offline with file offset == RVA.
    """
    page = 0x1000
    data = bytearray()
    readable = 0
    for off in range(0, size, page):
        want = min(page, size - off)
        chunk = read_mem(handle, base + off, want)
        if chunk is None:
            data += b"\x00" * want
        else:
            data += chunk
            readable += want
    with open(path, "wb") as fh:
        fh.write(data)
    return len(data), readable


def read_mem(handle, addr: int, size: int):
    buf = ctypes.create_string_buffer(size)
    done = SIZE_T()
    if not kernel32.ReadProcessMemory(handle, addr, buf, size, ctypes.byref(done)) or done.value != size:
        return None
    return buf.raw


def write_code(handle, addr: int, data: bytes) -> None:
    old = DWORD()
    if not kernel32.VirtualProtectEx(handle, addr, len(data), PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buf = ctypes.create_string_buffer(data, len(data))
        done = SIZE_T()
        if not kernel32.WriteProcessMemory(handle, addr, buf, len(data), ctypes.byref(done)) or done.value != len(data):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        restored = DWORD()
        kernel32.VirtualProtectEx(handle, addr, len(data), old.value, ctypes.byref(restored))
    kernel32.FlushInstructionCache(handle, addr, len(data))


def alloc_stub(handle, size: int = 0x1000) -> int:
    flags = MEM_COMMIT | MEM_RESERVE
    addr = kernel32.VirtualAllocEx(handle, None, size, flags, PAGE_EXECUTE_READWRITE)
    if addr and addr < 0x1_0000_0000:
        return addr
    if addr:
        kernel32.VirtualFreeEx(handle, addr, 0, MEM_RELEASE)
    # The target is a 32-bit process, so the stub must live below 4 GB.
    for hint in range(0x1000_0000, 0x7FFF_0000, 0x1_0000):
        addr = kernel32.VirtualAllocEx(handle, hint, size, flags, PAGE_EXECUTE_READWRITE)
        if addr:
            return addr
    raise RuntimeError("could not allocate stub memory below 4 GB")


# ---------------------------------------------------------------------------
# Patch logic
# ---------------------------------------------------------------------------

STATE_ORIGINAL = "original"
STATE_PATCHED = "patched"
STATE_WAITING = "waiting"
STATE_UNKNOWN = "unknown"
STATE_ERROR = "error"
STATE_NO_ACCESS = "no-access"


def classify(region: bytes) -> str:
    read_size = region[0:len(READ_SIZE_ORIG)]
    hook_off = HOOK_RVA - CHECK_START
    hook = region[hook_off:hook_off + HOOK_LEN]
    loop = region[RESUME_RVA - CHECK_START:]
    if loop != LOOP_ORIG:
        return STATE_WAITING
    if read_size == READ_SIZE_ORIG and hook == HOOK_ORIG:
        return STATE_ORIGINAL
    if read_size == READ_SIZE_NEW and hook[0] == 0xE9:
        return STATE_PATCHED
    return STATE_UNKNOWN


def classify_ver(region: bytes) -> str:
    """State of the PassThruReadVersion hook site (8 bytes at RVA 0x39CF)."""
    if region == VER_HOOK_ORIG:
        return STATE_ORIGINAL
    if region and region[0] == 0xE9:
        return STATE_PATCHED
    return STATE_UNKNOWN


def apply_patch(handle, base: int, stubs_for_key: dict) -> int:
    resume_addr = base + RESUME_RVA
    abort_addr = base + ABORT_RVA
    hook_addr = base + HOOK_RVA
    stub_addr = stubs_for_key.get("main")
    if stub_addr is not None:
        expected = build_stub(stub_addr, resume_addr, abort_addr)
        if read_mem(handle, stub_addr, len(expected)) != expected:
            stub_addr = None
    if stub_addr is None:
        stub_addr = alloc_stub(handle)
        write_code(handle, stub_addr, build_stub(stub_addr, resume_addr, abort_addr))
        stubs_for_key["main"] = stub_addr
    write_code(handle, base + READ_SIZE_RVA, READ_SIZE_NEW)
    write_code(handle, hook_addr, build_hook(hook_addr, stub_addr))
    return stub_addr


def apply_ver_patch(handle, base: int, stubs_for_key: dict) -> int:
    resume_addr = base + VER_RESUME_RVA
    hook_addr = base + VER_HOOK_RVA
    stub_addr = stubs_for_key.get("ver")
    if stub_addr is not None:
        expected = build_ver_stub(stub_addr, resume_addr)
        if read_mem(handle, stub_addr, len(expected)) != expected:
            stub_addr = None
    if stub_addr is None:
        stub_addr = alloc_stub(handle)
        write_code(handle, stub_addr, build_ver_stub(stub_addr, resume_addr))
        stubs_for_key["ver"] = stub_addr
    write_code(handle, hook_addr, build_ver_hook(hook_addr, stub_addr))
    return stub_addr


def report(states: dict, key, state: str, text: str) -> None:
    if states.get(key) != state:
        log(text)
        states[key] = state


def process_once(states: dict, stubs: dict, check_only: bool) -> int:
    seen = 0
    for pid, exe in iter_target_processes():
        mods = enumerate_modules(pid)
        report_watched(states, pid, exe, mods)
        module = mods.get(TARGET_MODULE)
        if not module:
            continue
        base, _size, _path = module
        seen += 1
        key = (pid, base)
        vkey = (pid, base, "ver")
        stubs_for_key = stubs.setdefault(key, {})
        tag = f"{exe} (PID {pid}) MVCI32.dll @ 0x{base:08X}"
        if states.get(key) == STATE_ERROR:
            continue
        handle = kernel32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not handle:
            report(states, key, STATE_NO_ACCESS,
                   f"{tag}: cannot open process (error {ctypes.get_last_error()}). Run as Administrator.")
            continue
        try:
            region = read_mem(handle, base + CHECK_START, CHECK_END - CHECK_START)
            state = classify(region) if region else STATE_WAITING
            # Fix 1: Data List overflow (read routine at RVA 0x1300).
            if state == STATE_ORIGINAL and not check_only:
                stub = apply_patch(handle, base, stubs_for_key)
                states[key] = STATE_PATCHED
                log(f"{tag}: Data List protected (stub @ 0x{stub:08X}).")
            elif state == STATE_ORIGINAL:
                report(states, key, state, f"{tag}: original code found, patch can be applied (check mode).")
            elif state == STATE_PATCHED:
                report(states, key, state, f"{tag}: Data List already patched.")
            elif state == STATE_WAITING:
                report(states, key, state,
                       f"{tag}: loaded, waiting for the driver to unpack "
                       f"(if this never changes, the DLL version differs from the analysed one).")
            else:
                report(states, key, state, f"{tag}: unexpected bytes at the patch site, left untouched.")
            # Fix 2: PassThruReadVersion copy overflow (RVA 0x39CF) - only once unpacked.
            if state in (STATE_ORIGINAL, STATE_PATCHED):
                vregion = read_mem(handle, base + VER_HOOK_RVA, VER_HOOK_LEN)
                vstate = classify_ver(vregion) if vregion else STATE_WAITING
                if vstate == STATE_ORIGINAL and not check_only:
                    vstub = apply_ver_patch(handle, base, stubs_for_key)
                    report(states, vkey, STATE_PATCHED, f"{tag}: ReadVersion protected (stub @ 0x{vstub:08X}).")
                elif vstate == STATE_ORIGINAL:
                    report(states, vkey, STATE_ORIGINAL, f"{tag}: ReadVersion site found, can be patched (check mode).")
                elif vstate == STATE_PATCHED:
                    report(states, vkey, STATE_PATCHED, f"{tag}: ReadVersion already patched.")
                else:
                    report(states, vkey, STATE_UNKNOWN, f"{tag}: unexpected bytes at the ReadVersion site, left untouched.")
        except Exception as exc:
            states[key] = STATE_ERROR
            log(f"{tag}: patch failed: {exc}")
        finally:
            kernel32.CloseHandle(handle)
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(
        description="In-memory hotfix for the MVCI32.dll overflow/hang (Techstream + Mini-VCI).")
    parser.add_argument("--check", action="store_true", help="only report the driver state, write nothing")
    parser.add_argument("--once", action="store_true", help="scan one time and exit instead of watching")
    parser.add_argument("--dump", metavar="FILE", help="save the unpacked MVCI32.dll image to FILE and exit")
    args = parser.parse_args()

    init_api()

    if args.dump:
        log("Dumping the unpacked MVCI32.dll image...")
        if not is_admin():
            log("WARNING: not running as Administrator. Techstream usually runs elevated, so access may fail.")
        for pid, exe in iter_target_processes():
            module = find_module(pid)
            if not module:
                continue
            base, size, _path = module
            handle = kernel32.OpenProcess(PROCESS_ACCESS, False, pid)
            if not handle:
                log(f"{exe} (PID {pid}): cannot open process (error {ctypes.get_last_error()}). Run as Administrator.")
                continue
            try:
                total, readable = dump_module(handle, base, size, args.dump)
                log(f"{exe} (PID {pid}): wrote {total} bytes from MVCI32.dll @ 0x{base:08X} "
                    f"to {args.dump} ({readable} readable). Send me this file.")
            finally:
                kernel32.CloseHandle(handle)
            return
        log("No Techstream process with MVCI32.dll loaded was found. "
            "Open Techstream and connect to the vehicle first, then run this again.")
        return

    mode = "check mode, nothing will be written" if args.check else "patch mode"
    log(f"MVCI32 hotfix started ({mode}). Press Ctrl+C to stop.")
    if not is_admin():
        log("WARNING: not running as Administrator. Techstream usually runs elevated, so access may fail.")
    if not args.once:
        log("Waiting for Techstream to load MVCI32.dll (connect to the vehicle)...")

    states, stubs = {}, {}
    try:
        while True:
            seen = process_once(states, stubs, args.check)
            if args.once:
                if not seen:
                    log("No Techstream process with MVCI32.dll loaded was found.")
                break
            time.sleep(POLL_SECONDS)
    except KeyboardInterrupt:
        log("Stopped.")


if __name__ == "__main__":
    main()