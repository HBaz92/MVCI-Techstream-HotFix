# Cable link-stability settings (reduce the stuttering/desync)

The hotfix stops the **crash and freeze** completely. The remaining **choppy
readings / stalls** come from the clone cable's USB link dropping and reordering
bytes. These settings **reduce** how often that happens; a quality interface
removes it entirely. Apply them in order — the first one matters most.

## 1. FTDI Latency Timer → 1 ms  (biggest single win)

The Mini-VCI uses an FTDI serial chip. Its default latency timer is **16 ms**,
which batches bytes and causes desync.

1. Device Manager → **Ports (COM & LPT)**.
2. Double-click the cable's **USB Serial Port (COMx)**.
3. **Port Settings** tab → **Advanced…**
4. Set **Latency Timer (msec)** to **`1`**.
5. Set **Minimum Read Timeout (msec)** and **Minimum Write Timeout (msec)** to **`0`**.
6. Untick **Serial Enumerator**.
7. OK, and replug the cable.

To confirm the value afterwards (PowerShell):

```powershell
Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Enum\FTDIBUS\*\0000\Device Parameters" -Name LatencyTimer -ErrorAction SilentlyContinue |
  Select-Object PSPath, LatencyTimer
```

## 2. Stop Windows from powering the port down

Run **as Administrator**:

```powershell
# Disable USB selective suspend on the active power plan
powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
powercfg /setactive SCHEME_CURRENT
```

Then, per device:

- Device Manager → **USB Serial Converter** → **Power Management** tab →
  untick **Allow the computer to turn off this device to save power**.
- Do the same for the **USB Root Hub(s)** under *Universal Serial Bus controllers*.

## 3. Use a good port

- Plug the cable into a **rear motherboard USB 2.0** port, **directly** — no hub,
  no extension, and avoid front-panel and USB 3.x ports (clone FT232R chips are
  flakier on some USB 3 controllers).
- Keep the cable away from ignition/HV harnesses in the car.

## 4. High-performance power plan

```powershell
powercfg /setactive 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c   # High performance
```

(If that GUID isn't present, pick High performance in Control Panel → Power
Options, or use the Ultimate Performance plan.)

## 5. Lighten the Techstream workload

- In **Data List**, select only the **10–15 parameters** you actually need
  instead of the full list.
- Set the **sample rate** to **500 ms** (or 1 s) instead of 100 ms.
- Fewer PIDs at a slower rate = far less data over the link = far less desync.

## 6. The real root: a quality cable

The clone chip/firmware is the ultimate cause of desync. For stutter-free
capture, use a genuine **Tactrix OpenPort 2.0** or a **Mongoose-class** MFC
interface. With any cable, the hotfix guarantees Techstream will not crash or
freeze — worst case is a skipped reading.
