# Mini-VCI / Techstream Hotfix

An in-memory hotfix for a heap-buffer-overflow bug in **`MVCI32.dll`** (the
J2534 driver used by Toyota **Techstream** with clone *Mini-VCI* cables, file
version `1.0.0.1`). The bug makes Techstream crash or freeze while reading
live data — Data List recording, Health Check, Active Test — on many clone
cables.

This tool does **not** modify any file on disk. It attaches to the running
Techstream process and corrects the faulty machine code in memory. Everything
disappears when Techstream closes.

> Educational / interoperability bug-fix for a closed, abandoned driver you run
> on your own machine. No ECU reprogramming, no network component. See
> [Disclaimer](#disclaimer).

---

## Symptoms

- Techstream closes with **Application Error, exception code `0xc0000005`**
  (Access Violation) in `MVCI32.dll`, typically a minute or two into a Data
  List recording or when opening a data list / running a Health Check.
- Sometimes a **hard freeze** instead of a crash: the operation stops updating
  and never recovers.
- Other cheap-cable symptoms (choppy readings, momentary stalls) come from the
  cable itself — see [Limitations](#limitations-what-this-does-not-fix).

## Root cause

Confirmed from a full crash dump (`!analyze -v`, `uf MVCI32+0x1300`):

- The driver's frame-reassembly routine at **RVA `0x1300`** allocates two
  **5000-byte (`0x1388`)** heap buffers: a *chunk* buffer and an *accumulation*
  buffer.
- It reads chunks from the cable and **appends each chunk to the accumulation
  buffer at offset `total`** with **no bounds check** (copy loop at
  `0x138E`–`0x13A1`).
- The loop only exits after 10 s with **no** data, or on a complete frame — but
  `0x13B5` refreshes the "last data" timer on **every** chunk, garbage included.
  So when the cable **desyncs and streams garbage non-stop**, `total` grows
  without bound (observed **`0x40F3A` = 266,042 bytes** in the dump) and the
  driver overruns the heap until Techstream crashes.

Why it is **one** bug covering the **whole** program:

- The low-level cable read (`0x10D0`) has **exactly one caller**: `0x1300`.
- `0x1300` has **20 callers** — every operation (Data List, Health Check,
  Active Test, DTC reads, …) reads through it.
- A scan of the entire 40 KB code section found **only one unbounded
  accumulation loop**: the one inside `0x1300`. Every other copy loop is bounded
  by a validated frame length (now capped ≤ 5000 by the fix).

So **every byte from the cable, for every operation, on every car, flows
through `0x1300`** — and patching `0x1300` fixes all of them.

See [`docs/analysis.md`](docs/analysis.md) for the full disassembly and reasoning.

## The fix

Applied in memory after the packed DLL has unpacked itself:

1. **`0x1361`** — request at most 5000 bytes per read (`push 157Ch` → `push 1388h`),
   so the 5000-byte chunk buffer can't be overrun.
2. **`0x1382`** — a short trampoline stub that:
   - caps the chunk length at 5000 bytes, and
   - when `total + chunk` would exceed the 5000-byte buffer (i.e. the stream has
     desynced), jumps to the routine's **own clean exit at `0x14FF`** — the same
     epilogue it already uses on a normal timeout. It frees both buffers and
     returns, handing control back to Techstream, which simply re-issues the
     request.
   - otherwise resumes the original copy loop unchanged.

Result: **no crash and no freeze, ever.** At worst one reading is skipped on a
desynced burst and the next cycle recovers.

The patcher watches for every Techstream session and re-applies the fix each
time the driver is (re)loaded, so it survives the per-ECU reloads Techstream does.

## Verification

The copy loop and the stub are executed in a **Unicorn** CPU emulator with the
accumulation buffer mapped so byte 5000 is unmapped — exactly like the heap
boundary in the real crash. [`tests/test_patch.py`](tests/test_patch.py) proves:

- the **original** code overruns the buffer and faults (reproduces the crash);
- the **patched** code, on a desynced stream, branches to the clean exit — never
  faults, never loops forever (500 back-to-back garbage reads all exit cleanly);
- an oversized 5500-byte read is capped, not written past the end;
- **normal** reads copy byte-for-byte identical data up to exactly 5000 bytes.

```
pip install unicorn
python tests/test_patch.py
```

## Usage

Requires **Python 3** on Windows. Techstream normally runs elevated, so run the
tool **as Administrator**.

**Easiest — double-click:**

1. Put `run_mvci_patch.bat` next to `mvci_patch.py`.
2. Double-click `run_mvci_patch.bat` (it self-elevates via UAC).
3. Keep its window open, then start Techstream and connect to the car.
   It prints `patched` once the driver loads.

**From a terminal:**

```powershell
py mvci_patch.py            # watch and patch every Techstream session
py mvci_patch.py --check    # report only, write nothing
py mvci_patch.py --dump FILE  # save the unpacked MVCI32.dll image (for analysis)
```

`build_mvci_exe.bat` builds a standalone `mvci_patch.exe` (via PyInstaller,
`--uac-admin`) if you prefer a single file. Note: because the tool writes to
another process's memory, antivirus may flag it — the same way it flags
`MVCI32.dll` itself.

## Limitations — what this does **not** fix

This hotfix eliminates the **crash and the freeze** completely, program-wide.
It does **not** fix the **cable desync** that triggers them — that is the clone
cable's hardware/firmware dropping and reordering bytes. You can **reduce** how
often it desyncs (so data is smoother); you cannot remove it in software:

- Set the FTDI **Latency Timer to `1`** (Device Manager → the cable's COM port →
  Port Settings → Advanced). Biggest single improvement.
- Use a **rear USB 2.0** port, no hub; High-performance power plan; disable USB
  selective suspend.
- In Techstream, use a **smaller Data List** and a **slower sample rate**.
- For zero desync, use a quality interface (genuine Tactrix OpenPort 2.0 /
  Mongoose-class).

Full settings list: [`docs/link-stability.md`](docs/link-stability.md).

With the hotfix running, even the worst desync only **skips a reading** — it
never crashes or freezes Techstream.

## Compatibility

Analyzed against `MVCI32.dll` **file version `1.0.0.1`** (the common
"Autokent MVCI MultiDriver" clone build). The patcher verifies the exact bytes
at the patch site before writing and refuses to touch anything else, so a
different driver build is reported as `waiting`/`unexpected` and left untouched.
If you have another build, open an issue with a `--dump` of it.

## Disclaimer

Provided as-is, for interoperability and reliability research on a driver you
run on your own computer. It touches only the memory of a process you started;
it writes nothing to disk and performs no ECU programming. Use at your own risk.
Not affiliated with Toyota or any cable vendor.

## License

MIT — see [LICENSE](LICENSE).
