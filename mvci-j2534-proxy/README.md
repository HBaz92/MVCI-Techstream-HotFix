# MVCI J2534 Proxy — Stage 1 (transparent + logging)

A thin J2534 (04.04) pass-through shim that sits between Toyota Techstream and
the real clone Mini-VCI driver:

```
Techstream  ->  j2534_proxy.dll  ->  (real) MVCI32.dll  ->  cable
```

**This works together with the in-memory MVCI32 patch, it does not replace it.**
The patch stops the crash/hang *inside* the real driver (the proxy has to call
that driver, so it cannot catch a crash in it). The proxy adds buffer flushing
and retry *above* the driver. Keep `run_mvci_patch.bat` running.

## What Stage 1 does

- Forwards every J2534 call to the real driver unchanged — Techstream behaves
  exactly as before.
- Logs every error (and the connect/version calls) to a file, so we can see
  **exactly** what happens at the J2534 level when the cable desyncs: which call
  fails, the error code, the sequence. Successful reads are not logged, so the
  hot path stays fast.
- Optional `Recovery=1`: on a failed read, flush RX/TX and retry (a first,
  conservative recovery to experiment with). Default is `0` (safe).

Stage 2 will add the precise recovery/reconnect logic, designed from the log.

## Install

1. **Keep the real driver installed and the patch running** (`run_mvci_patch.bat`).
2. Make a folder `C:\mvci-proxy\` and put these in it:
   - `j2534_proxy.dll`
   - `mvci-proxy.ini`
3. Edit `mvci-proxy.ini` → set `RealDriver=` to your actual driver DLL path
   (Autokent or XHorse — examples are in the file).
4. If you put the folder somewhere other than `C:\mvci-proxy\`, edit
   `mvci-proxy.reg` → `FunctionLibrary` to match. Then **double-click
   `mvci-proxy.reg`** and approve the prompt.
5. In **Techstream → Setup → VIM Select**, choose **“MVCI Proxy (Techstream Fix)”**.
6. Connect to the car and use Techstream normally.

To undo: in VIM Select pick your original driver again.

## After a test run

Send me **`C:\mvci-proxy.log`** — especially after a session where it desynced.
That log is what lets me build Stage 2's recovery correctly instead of guessing.

To try the experimental recovery now: set `Recovery=1` in `mvci-proxy.ini`,
save, and restart Techstream.

## Recovery levels

The proxy has two levels. The first is the one you are already running.

**1. Flush (`Recovery=1`) — the working setup.**
On a run of empty reads (a desync), it flushes the cable's RX/TX buffers so the
stream re-aligns. This is what cut your stalls from ~10s down to ~1s. Leave it
on. `ZeroStreak` = how many empty reads in a row count as a stall (default 3).

**2. Device reset (`DeviceReset=1`) — opt-in, OFF by default, experimental.**
For the *stubborn* stalls the flush can't clear. If empty reads keep coming even
after several flushes (past `ReconnectAfter`, default 15), the proxy does a full
device reset behind Techstream's back: `Close` + `Open` the cable, reconnect the
channel, and replay the filters — all while Techstream keeps using the same
handles, so it never notices. 

- It is **off by default**. With `DeviceReset=0` the proxy behaves *exactly*
  like your current working setup — nothing changes.
- To test it: set `DeviceReset=1`, save, restart Techstream. If it helps the
  stubborn stalls, keep it. If anything behaves worse, set it back to `0` and
  you are instantly back on the known-good flush setup.
- This is a last resort. Honest expectation: it may not help much, because the
  stubborn stalls come from the old cable firmware (MINIV1.03) and Techstream
  already tries its own reconnect. We are only finding out whether a *faster,
  flush-combined* reset does better. The log will show exactly what happened:
  look for `escalate:` and `device_reset:` lines.

## Notes

- The DLL is 32-bit (Techstream and MVCI32 are 32-bit) and depends only on
  standard Windows DLLs (kernel32, msvcrt).
- It contains no Toyota or vendor code — it only forwards the standard J2534
  API and loads whichever real driver you point it at.
- If Techstream fails to connect through the proxy, switch VIM back to the real
  driver and send me the log.

## Build (reproducible)

Built with the 32-bit MinGW-w64 cross-compiler:

```
i686-w64-mingw32-gcc -O2 -Wall -shared -o j2534_proxy.dll j2534_proxy.c \
    -Wl,--kill-at -lkernel32
```

`-Wl,--kill-at` exports the `__stdcall` functions under their plain J2534 names
(`PassThruOpen`, not `_PassThruOpen@8`), as the J2534 spec requires.
