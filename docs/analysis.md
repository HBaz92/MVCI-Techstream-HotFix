# Root-cause analysis

All offsets are RVAs (module base + offset). Confirmed on `MVCI32.dll`
file version `1.0.0.1`, from a full-memory crash dump of `Techstream.exe`.

## The crash

```
Exception c0000005 (Access Violation, write) at MVCI32+0x1397
  mov byte ptr [edx+eax-1], cl     ds:...:15f73000=??   (unmapped)
CONTEXT: eax=0000008f  edx=15f72f72  esi=00040f3a  ebx=15f32038
```

`edx = ebx + esi` = accumulation buffer + `total`, with `total = 0x40F3A =
266,042`. The write lands one page past the heap allocation. `AV.Type: Write`,
`INVALID_POINTER_WRITE`. A second, different crash in the same session reported
`c00000fd` (stack overflow) — the same corruption surfacing elsewhere.

## The vulnerable routine (`0x1300`)

Reassembles one framed message from the cable:

```
0x1300  sub esp,28h; push ebx/ebp/esi/edi
0x1309  push 1388h ; call alloc  -> ebx      ; accumulation buffer (5000)
0x131e  push 1388h ; call alloc  -> [esp+1Ch] ; chunk buffer (5000)
0x1329  xor esi,esi                           ; total = 0
loop (0x1352):
0x1361  push 157Ch ; read up to 5500 bytes into the 5000-byte chunk buffer
0x136b  call 0x10d0                           ; the device read (see below)
0x1382  test ecx,ecx ...                      ; ecx = bytes read
0x138b  lea edx,[esi+ebx]                     ; dest = total + buffer
0x138e  COPY LOOP: dest[i] = chunk[i]  for i in 0..bytes_read   <-- no bound
0x13ad  esi += bytes_read ; store total
0x13b5  call edi                              ; refresh "last data" timer
0x13b9  if total<2 -> read more
0x13e1  if declared_len != total -> read more ; never matches on desync
0x14ce  exit conditions:
0x14e2    no data AND elapsed >= timeout -> exit 0x14FF
0x14ec    complete frame               -> exit 0x14FF
0x14f4    (t - last_data) < 10000 ms   -> read more   ; refreshed every chunk!
0x14ff  EPILOGUE: free both buffers; ret 10h
```

Two independent defects combine:

1. **Unbounded append.** The copy loop at `0x138E` writes `bytes_read` bytes to
   `buffer + total` and never checks `total` against the 5000-byte size.
2. **No runaway timeout.** `0x13B5` updates the "last data" timestamp on every
   chunk, so the only loop-exit that could stop a garbage stream
   (`0x14F4`, 10 s of silence) never triggers while garbage keeps arriving.

On a desynced stream the declared length (`buffer[0..1]`) is garbage, so
`declared_len == total` (`0x13E1`) is never satisfied, the loop reads forever,
`total` grows past 5000, and the append overruns the heap → crash. Before the
overflow faults, it silently corrupts adjacent heap, which is why a second crash
can appear as a stack overflow elsewhere.

## Why one patch covers the whole program

- `0x10D0` (the device read) has **exactly one caller**: `0x1300`
  (call site `0x136B`).
- `0x1300` has **20 callers** (`0x2278, 0x24AF, 0x2670, 0x2881, 0x2A3D, 0x2D29,
  0x2FDA, 0x3220, 0x34D4, 0x3700, 0x387F, 0x39CA, 0x3D7D, 0x3EC8, 0x3F2A,
  0x3FE6, 0x409A, 0x416D, 0x4237, 0x4277`) — Data List, Health Check, Active
  Test, DTC reads, etc.
- Scanning every backward byte-copy loop in the 40 KB code section, the **only**
  loop whose destination index accumulates across reads is `0x138E`. The others
  (`0x140A, 0x147A, 0x148D, 0x2AAB, 0x39E0, 0x40D3`) copy a **validated frame
  length** into a caller buffer; with `0x1300` capped at ≤ 5000, they are all
  bounded.

Therefore every cable read for every operation funnels through `0x1300`, and
fixing `0x1300` removes the overflow program-wide.

## The patch

- `0x1361`: `push 157Ch` → `push 1388h` (cap each read at 5000).
- `0x1382` (12 bytes) → `jmp` to a trampoline that:
  - caps `bytes_read` at 5000;
  - if `total + bytes_read > 5000` → `jmp 0x14FF` (the routine's own clean
    timeout exit: frees both buffers, returns, Techstream re-requests);
  - else restores the displaced `lea edx,[esi+ebx]` and resumes at `0x138E`.

The trampoline runs at the function's baseline `esp`, so jumping to the `0x14FF`
epilogue is stack-safe and byte-for-byte the behaviour of a normal timeout.

## Second-order fix: the downstream caller crash (`0x2AAB`)

After `0x1300` was guarded, a crash moved **one level up** to a caller, e.g.
the read at **`0x2AAB`** (`mov dl,[esi+eax+9]`), `c0000005`.

Each of the 20 callers passes `0x1300` a pointer to an output-length slot
(`arg2`, baseline `[esp+40h]`) and reads that length back afterwards
(`0x2A42 mov eax,[esp+34h]; cmp eax,9`). `0x1300` writes the length **only on a
complete frame**. On the desync **abort** path it did not, so the caller used a
**stale/garbage length** and copied far past its buffer → crash.

Fix: the abort branch now sets the caller's output length to **0** before
jumping to `0x14FF`. Every caller then sees "no data" and skips its copy
(`cmp eax,9; jb`). Verified in the emulator: on abort the length slot is zeroed;
on the normal path it is untouched.
