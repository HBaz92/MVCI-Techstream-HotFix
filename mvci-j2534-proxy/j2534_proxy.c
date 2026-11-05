/*
 * j2534_proxy.c  -  J2534 pass-through shim for the clone Mini-VCI / MVCI32
 * driver used by Toyota Techstream.
 *
 *     Techstream  ->  j2534_proxy.dll  ->  (real) MVCI32.dll  ->  cable
 *
 * It forwards every J2534 (04.04) call to the real driver and adds, on top:
 *   - a diagnostic log (empty-read stalls, recovery actions);
 *   - Recovery=1 : on a RUN of empty reads (got=0), flush RX/TX so the stream
 *                  re-aligns (handles the common desync);
 *   - DeviceReset=1 (opt-in, OFF by default) : if empties persist past
 *                  ReconnectAfter even with flushes, do a full device reset
 *                  (Close+Open+reconnect+replay filters) transparently - the
 *                  shim virtualises the device / channel handles so Techstream
 *                  keeps using the same IDs across the reset.
 *
 * The patch (mvci_patch) must run alongside: it stops the crash *inside* the
 * real driver; this shim cannot catch a crash in code it has to call.
 *
 * Build (32-bit):
 *   i686-w64-mingw32-gcc -O2 -Wall -shared -o j2534_proxy.dll j2534_proxy.c \
 *       -Wl,--kill-at -lkernel32
 */

#include <windows.h>
#include <stdio.h>
#include <string.h>

/* ---- J2534 v04.04 types ------------------------------------------------- */

typedef struct {
    unsigned long ProtocolID;
    unsigned long RxStatus;
    unsigned long TxFlags;
    unsigned long Timestamp;
    unsigned long DataSize;
    unsigned long ExtraDataIndex;
    unsigned char Data[4128];
} PASSTHRU_MSG;

#define CLEAR_TX_BUFFER 0x07
#define CLEAR_RX_BUFFER 0x08
#define STATUS_NOERROR  0x00

/* Fixed, always-writable, easy-to-find log location (Public needs no admin). */
#define FIXED_LOG "C:\\Users\\Public\\mvci-proxy.log"

/* ---- Real-driver function pointer types (all __stdcall) ----------------- */

typedef long (__stdcall *pfOpen)(void *, unsigned long *);
typedef long (__stdcall *pfClose)(unsigned long);
typedef long (__stdcall *pfConnect)(unsigned long, unsigned long, unsigned long, unsigned long, unsigned long *);
typedef long (__stdcall *pfDisconnect)(unsigned long);
typedef long (__stdcall *pfReadMsgs)(unsigned long, PASSTHRU_MSG *, unsigned long *, unsigned long);
typedef long (__stdcall *pfWriteMsgs)(unsigned long, PASSTHRU_MSG *, unsigned long *, unsigned long);
typedef long (__stdcall *pfStartPeriodic)(unsigned long, PASSTHRU_MSG *, unsigned long *, unsigned long);
typedef long (__stdcall *pfStopPeriodic)(unsigned long, unsigned long);
typedef long (__stdcall *pfStartFilter)(unsigned long, unsigned long, PASSTHRU_MSG *, PASSTHRU_MSG *, PASSTHRU_MSG *, unsigned long *);
typedef long (__stdcall *pfStopFilter)(unsigned long, unsigned long);
typedef long (__stdcall *pfSetVoltage)(unsigned long, unsigned long, unsigned long);
typedef long (__stdcall *pfReadVersion)(unsigned long, char *, char *, char *);
typedef long (__stdcall *pfGetLastError)(char *);
typedef long (__stdcall *pfIoctl)(unsigned long, unsigned long, void *, void *);

static struct {
    pfOpen Open; pfClose Close; pfConnect Connect; pfDisconnect Disconnect;
    pfReadMsgs ReadMsgs; pfWriteMsgs WriteMsgs;
    pfStartPeriodic StartPeriodic; pfStopPeriodic StopPeriodic;
    pfStartFilter StartFilter; pfStopFilter StopFilter;
    pfSetVoltage SetVoltage; pfReadVersion ReadVersion;
    pfGetLastError GetLastError_; pfIoctl Ioctl;
} R;

/* ---- State -------------------------------------------------------------- */

static HMODULE          g_real = NULL;
static int              g_loaded = 0;
static int              g_recovery = 0;          /* flush RX/TX on empty-read runs */
static int              g_zerostreak = 3;        /* empties between flushes */
static int              g_devreset = 0;          /* opt-in device reset escalation */
static int              g_reconnect_after = 15;  /* empties before a device reset */
static char             g_logpath[MAX_PATH] = "";
static char             g_realpath[MAX_PATH] = "";
static char             g_inipath[MAX_PATH] = "";
static int              g_ini_found = 0;
static CRITICAL_SECTION g_lock;
static int              g_lock_ready = 0;

/* ---- Handle virtualisation tables --------------------------------------- */
/* Techstream keeps the FIRST id we hand it (pub); the real id can change on a
 * device reset. We translate pub<->real on every call. With DeviceReset=0 no
 * reset ever happens, so pub==real throughout and translation is a no-op. */

#define MAX_DEV  4
#define MAX_CH   4
#define MAX_FILT 10

typedef struct {
    int used; unsigned long type;
    int has_mask, has_pat, has_flow;
    PASSTHRU_MSG mask, pat, flow;
} FILT;

typedef struct {
    int used;
    unsigned long pub, real;      /* channel ids */
    unsigned long pub_dev;        /* owning device (pub id) */
    unsigned long proto, flags, baud;
    unsigned long zero_run;       /* consecutive empty reads */
    FILT filt[MAX_FILT];
} CHAN;

typedef struct { int used; unsigned long pub, real; } DEV;

static DEV  g_dev[MAX_DEV];
static CHAN g_chan[MAX_CH];

static void dev_add(unsigned long real) {
    int i; for (i = 0; i < MAX_DEV; i++) if (!g_dev[i].used) {
        g_dev[i].used = 1; g_dev[i].pub = real; g_dev[i].real = real; return; }
}
static int dev_idx(unsigned long pub) {
    int i; for (i = 0; i < MAX_DEV; i++) if (g_dev[i].used && g_dev[i].pub == pub) return i;
    return -1;
}
static unsigned long real_dev(unsigned long pub) { int i = dev_idx(pub); return i >= 0 ? g_dev[i].real : pub; }

static void chan_add(unsigned long real, unsigned long pub_dev,
                     unsigned long proto, unsigned long flags, unsigned long baud) {
    int i; for (i = 0; i < MAX_CH; i++) if (!g_chan[i].used) {
        memset(&g_chan[i], 0, sizeof(g_chan[i]));
        g_chan[i].used = 1; g_chan[i].pub = real; g_chan[i].real = real;
        g_chan[i].pub_dev = pub_dev; g_chan[i].proto = proto;
        g_chan[i].flags = flags; g_chan[i].baud = baud; return; }
}
static int chan_idx(unsigned long pub) {
    int i; for (i = 0; i < MAX_CH; i++) if (g_chan[i].used && g_chan[i].pub == pub) return i;
    return -1;
}
static unsigned long real_ch(unsigned long pub) { int i = chan_idx(pub); return i >= 0 ? g_chan[i].real : pub; }

static void plog(const char *fmt, ...);

/* Full device reset: Close+Open the device, reconnect every channel on it and
 * replay its stored filters. Caller holds g_lock. Returns 1 on success. */
static int device_reset_locked(int ci)
{
    CHAN *c = &g_chan[ci];
    int di = dev_idx(c->pub_dev);
    if (di < 0) return 0;
    DEV *d = &g_dev[di];
    unsigned long old = d->real, ndev = 0;
    long r = STATUS_NOERROR;
    int attempt;

    R.Close(old);
    Sleep(300);                       /* let the USB device re-enumerate */
    for (attempt = 0; attempt < 3; attempt++) {
        ndev = 0;
        r = R.Open(NULL, &ndev);
        if (r == STATUS_NOERROR && ndev != 0) break;
        plog("device_reset: re-Open attempt %d failed (r=%ld)", attempt + 1, r);
        Sleep(400);
    }
    if (r != STATUS_NOERROR || ndev == 0) {
        plog("device_reset: re-Open gave up - device stays closed until Techstream reconnects");
        return 0;
    }
    d->real = ndev;

    int i, f;
    for (i = 0; i < MAX_CH; i++) {
        CHAN *cc = &g_chan[i];
        if (!cc->used || cc->pub_dev != d->pub) continue;
        unsigned long nch = 0;
        r = R.Connect(ndev, cc->proto, cc->flags, cc->baud, &nch);
        if (r == STATUS_NOERROR && nch) {
            cc->real = nch;
            for (f = 0; f < MAX_FILT; f++) {
                if (!cc->filt[f].used) continue;
                unsigned long fid = 0;
                R.StartFilter(nch, cc->filt[f].type,
                              cc->filt[f].has_mask ? &cc->filt[f].mask : NULL,
                              cc->filt[f].has_pat  ? &cc->filt[f].pat  : NULL,
                              cc->filt[f].has_flow ? &cc->filt[f].flow : NULL, &fid);
            }
            plog("device_reset: dev %lu->%lu, ch pub=%lu real->%lu, filters replayed",
                 old, ndev, cc->pub, nch);
        } else {
            plog("device_reset: reconnect ch pub=%lu failed (r=%ld)", cc->pub, r);
        }
    }
    return 1;
}

/* ---- Logging ------------------------------------------------------------ */

static void plog(const char *fmt, ...)
{
    if (!g_logpath[0]) return;
    FILE *f = fopen(g_logpath, "a");
    if (!f) return;
    SYSTEMTIME t; GetLocalTime(&t);
    fprintf(f, "%02d:%02d:%02d.%03d [%lu] ",
            t.wHour, t.wMinute, t.wSecond, t.wMilliseconds,
            (unsigned long)GetCurrentThreadId());
    va_list ap; va_start(ap, fmt); vfprintf(f, fmt, ap); va_end(ap);
    fputc('\n', f); fclose(f);
}

/* ---- Config + loading --------------------------------------------------- */

static void dll_dir(char *out, const char *leaf)
{
    char path[MAX_PATH]; HMODULE self = NULL;
    GetModuleHandleExA(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS |
                       GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCSTR)&dll_dir, &self);
    GetModuleFileNameA(self, path, MAX_PATH);
    char *slash = strrchr(path, '\\');
    if (slash) *(slash + 1) = 0; else path[0] = 0;
    _snprintf(out, MAX_PATH, "%s%s", path, leaf); out[MAX_PATH - 1] = 0;
}

static int try_open(const char *p) { FILE *f = fopen(p, "a"); if (f) { fclose(f); return 1; } return 0; }

static void read_config(void)
{
    dll_dir(g_inipath, "mvci-proxy.ini");
    g_ini_found = (GetFileAttributesA(g_inipath) != INVALID_FILE_ATTRIBUTES);

    GetPrivateProfileStringA("proxy", "RealDriver", "", g_realpath, MAX_PATH, g_inipath);
    GetPrivateProfileStringA("proxy", "LogFile", "", g_logpath, MAX_PATH, g_inipath);
    g_recovery        = (int)GetPrivateProfileIntA("proxy", "Recovery", 0, g_inipath);
    g_zerostreak      = (int)GetPrivateProfileIntA("proxy", "ZeroStreak", 3, g_inipath);
    g_devreset        = (int)GetPrivateProfileIntA("proxy", "DeviceReset", 0, g_inipath);
    g_reconnect_after = (int)GetPrivateProfileIntA("proxy", "ReconnectAfter", 15, g_inipath);
    if (g_zerostreak < 1) g_zerostreak = 1;
    if (g_zerostreak > 100) g_zerostreak = 100;
    if (g_reconnect_after < g_zerostreak) g_reconnect_after = g_zerostreak;
    if (g_reconnect_after > 1000) g_reconnect_after = 1000;

    if (!(g_logpath[0] && try_open(g_logpath))) {
        char cand[MAX_PATH];
        if (try_open(FIXED_LOG)) { strncpy(g_logpath, FIXED_LOG, MAX_PATH); g_logpath[MAX_PATH-1]=0; }
        else {
            dll_dir(cand, "mvci-proxy.log");
            if (try_open(cand)) { strncpy(g_logpath, cand, MAX_PATH); g_logpath[MAX_PATH-1]=0; }
            else {
                char tmp[MAX_PATH];
                if (GetTempPathA(MAX_PATH, tmp)) {
                    _snprintf(cand, MAX_PATH, "%smvci-proxy.log", tmp); cand[MAX_PATH-1]=0;
                    if (try_open(cand)) { strncpy(g_logpath, cand, MAX_PATH); g_logpath[MAX_PATH-1]=0; }
                    else g_logpath[0]=0;
                } else g_logpath[0]=0;
            }
        }
    }
}

#define BIND(name, field, type) \
    R.field = (type)GetProcAddress(g_real, name); \
    if (!R.field) { plog("MISSING export: %s", name); ok = 0; }

static int do_load(void)
{
    int ok = 1;
    read_config();
    plog("==== proxy init: pid=%lu ini='%s' (found=%d) log='%s' real='%s' recovery=%d devreset=%d ====",
         (unsigned long)GetCurrentProcessId(), g_inipath, g_ini_found, g_logpath,
         g_realpath, g_recovery, g_devreset);
    if (!g_realpath[0]) { plog("ERROR: RealDriver not set in mvci-proxy.ini"); return 0; }
    g_real = LoadLibraryA(g_realpath);
    if (!g_real) { plog("ERROR: LoadLibrary('%s') failed, err=%lu", g_realpath, GetLastError()); return 0; }

    BIND("PassThruOpen", Open, pfOpen);
    BIND("PassThruClose", Close, pfClose);
    BIND("PassThruConnect", Connect, pfConnect);
    BIND("PassThruDisconnect", Disconnect, pfDisconnect);
    BIND("PassThruReadMsgs", ReadMsgs, pfReadMsgs);
    BIND("PassThruWriteMsgs", WriteMsgs, pfWriteMsgs);
    BIND("PassThruStartPeriodicMsg", StartPeriodic, pfStartPeriodic);
    BIND("PassThruStopPeriodicMsg", StopPeriodic, pfStopPeriodic);
    BIND("PassThruStartMsgFilter", StartFilter, pfStartFilter);
    BIND("PassThruStopMsgFilter", StopFilter, pfStopFilter);
    BIND("PassThruSetProgrammingVoltage", SetVoltage, pfSetVoltage);
    BIND("PassThruReadVersion", ReadVersion, pfReadVersion);
    BIND("PassThruGetLastError", GetLastError_, pfGetLastError);
    BIND("PassThruIoctl", Ioctl, pfIoctl);

    plog("loaded real driver '%s' (recovery=%d zerostreak=%d devreset=%d reconnectafter=%d)",
         g_realpath, g_recovery, g_zerostreak, g_devreset, g_reconnect_after);
    return ok;
}

static int ensure_loaded(void)
{
    if (g_loaded) return (g_real != NULL);
    if (!g_lock_ready) return 0;
    EnterCriticalSection(&g_lock);
    if (!g_loaded) { do_load(); g_loaded = 1; }
    LeaveCriticalSection(&g_lock);
    return (g_real != NULL);
}

#define GUARD(fn) if (!ensure_loaded() || !R.fn) return 0x00000001L

/* ---- Exported J2534 functions ------------------------------------------- */

__declspec(dllexport) long __stdcall PassThruOpen(void *pName, unsigned long *pDeviceID)
{
    GUARD(Open);
    long r = R.Open(pName, pDeviceID);
    if (r == STATUS_NOERROR && pDeviceID) {
        EnterCriticalSection(&g_lock); dev_add(*pDeviceID); LeaveCriticalSection(&g_lock);
    }
    plog("Open -> %ld (dev=%lu)", r, pDeviceID ? *pDeviceID : 0);
    return r;
}

__declspec(dllexport) long __stdcall PassThruClose(unsigned long DeviceID)
{
    GUARD(Close);
    unsigned long rdev = real_dev(DeviceID);
    long r = R.Close(rdev);
    EnterCriticalSection(&g_lock);
    int i; for (i = 0; i < MAX_CH; i++) if (g_chan[i].used && g_chan[i].pub_dev == DeviceID) g_chan[i].used = 0;
    i = dev_idx(DeviceID); if (i >= 0) g_dev[i].used = 0;
    LeaveCriticalSection(&g_lock);
    plog("Close(dev=%lu) -> %ld", DeviceID, r);
    return r;
}

__declspec(dllexport) long __stdcall PassThruConnect(unsigned long DeviceID, unsigned long ProtocolID,
                                                     unsigned long Flags, unsigned long BaudRate,
                                                     unsigned long *pChannelID)
{
    GUARD(Connect);
    unsigned long rdev = real_dev(DeviceID);
    long r = R.Connect(rdev, ProtocolID, Flags, BaudRate, pChannelID);
    if (r == STATUS_NOERROR && pChannelID) {
        EnterCriticalSection(&g_lock);
        chan_add(*pChannelID, DeviceID, ProtocolID, Flags, BaudRate);
        LeaveCriticalSection(&g_lock);
    }
    plog("Connect(dev=%lu proto=%lu flags=0x%lx baud=%lu) -> %ld (ch=%lu)",
         DeviceID, ProtocolID, Flags, BaudRate, r, pChannelID ? *pChannelID : 0);
    return r;
}

__declspec(dllexport) long __stdcall PassThruDisconnect(unsigned long ChannelID)
{
    GUARD(Disconnect);
    unsigned long rch = real_ch(ChannelID);
    long r = R.Disconnect(rch);
    EnterCriticalSection(&g_lock);
    int i = chan_idx(ChannelID); if (i >= 0) g_chan[i].used = 0;
    LeaveCriticalSection(&g_lock);
    plog("Disconnect(ch=%lu) -> %ld", ChannelID, r);
    return r;
}

__declspec(dllexport) long __stdcall PassThruReadMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                                      unsigned long *pNumMsgs, unsigned long Timeout)
{
    GUARD(ReadMsgs);
    int ci = chan_idx(ChannelID);
    unsigned long rch = ci >= 0 ? g_chan[ci].real : ChannelID;
    unsigned long want = pNumMsgs ? *pNumMsgs : 0;
    long r = R.ReadMsgs(rch, pMsg, pNumMsgs, Timeout);
    unsigned long got = pNumMsgs ? *pNumMsgs : 0;

    if (ci < 0) return r;                 /* unknown channel: pure passthrough */
    CHAN *c = &g_chan[ci];

    if (got == 0) {
        c->zero_run++;
        plog("ReadMsgs(ch=%lu want=%lu to=%lu) -> %ld  got=0  (empty #%lu)",
             ChannelID, want, Timeout, r, c->zero_run);

        if (g_recovery && R.Ioctl && (c->zero_run % (unsigned long)g_zerostreak) == 0) {
            R.Ioctl(c->real, CLEAR_RX_BUFFER, NULL, NULL);
            R.Ioctl(c->real, CLEAR_TX_BUFFER, NULL, NULL);
            plog("  recover: %lu empty reads -> flushed RX+TX on ch=%lu", c->zero_run, ChannelID);
        }

        if (g_devreset && c->zero_run >= (unsigned long)g_reconnect_after) {
            plog("  escalate: %lu empty reads -> full device reset", c->zero_run);
            EnterCriticalSection(&g_lock);
            int ok = device_reset_locked(ci);
            unsigned long newreal = c->real;
            LeaveCriticalSection(&g_lock);
            if (ok) {
                if (pNumMsgs) *pNumMsgs = want ? want : 1;
                r = R.ReadMsgs(newreal, pMsg, pNumMsgs, Timeout);
                got = pNumMsgs ? *pNumMsgs : 0;
                plog("  after device reset: read -> %ld got=%lu", r, got);
            }
            c->zero_run = 0;
        }
    } else if (c->zero_run) {
        plog("ReadMsgs: recovered (got=%lu) after %lu empty read(s)", got, c->zero_run);
        c->zero_run = 0;
    }
    return r;
}

__declspec(dllexport) long __stdcall PassThruWriteMsgs(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                                       unsigned long *pNumMsgs, unsigned long Timeout)
{
    GUARD(WriteMsgs);
    long r = R.WriteMsgs(real_ch(ChannelID), pMsg, pNumMsgs, Timeout);
    if (r != STATUS_NOERROR) plog("WriteMsgs(ch=%lu) -> %ld", ChannelID, r);
    return r;
}

__declspec(dllexport) long __stdcall PassThruStartPeriodicMsg(unsigned long ChannelID, PASSTHRU_MSG *pMsg,
                                                              unsigned long *pMsgID, unsigned long TimeInterval)
{
    GUARD(StartPeriodic);
    return R.StartPeriodic(real_ch(ChannelID), pMsg, pMsgID, TimeInterval);
}

__declspec(dllexport) long __stdcall PassThruStopPeriodicMsg(unsigned long ChannelID, unsigned long MsgID)
{
    GUARD(StopPeriodic);
    return R.StopPeriodic(real_ch(ChannelID), MsgID);
}

__declspec(dllexport) long __stdcall PassThruStartMsgFilter(unsigned long ChannelID, unsigned long FilterType,
                                                            PASSTHRU_MSG *pMask, PASSTHRU_MSG *pPattern,
                                                            PASSTHRU_MSG *pFlow, unsigned long *pFilterID)
{
    GUARD(StartFilter);
    int ci = chan_idx(ChannelID);
    unsigned long rch = ci >= 0 ? g_chan[ci].real : ChannelID;
    long r = R.StartFilter(rch, FilterType, pMask, pPattern, pFlow, pFilterID);
    if (r == STATUS_NOERROR && ci >= 0) {
        EnterCriticalSection(&g_lock);
        CHAN *c = &g_chan[ci]; int f;
        for (f = 0; f < MAX_FILT; f++) if (!c->filt[f].used) {
            c->filt[f].used = 1; c->filt[f].type = FilterType;
            if (pMask)    { c->filt[f].has_mask = 1; c->filt[f].mask = *pMask; }
            if (pPattern) { c->filt[f].has_pat  = 1; c->filt[f].pat  = *pPattern; }
            if (pFlow)    { c->filt[f].has_flow = 1; c->filt[f].flow = *pFlow; }
            break;
        }
        LeaveCriticalSection(&g_lock);
    }
    plog("StartMsgFilter(ch=%lu type=%lu) -> %ld", ChannelID, FilterType, r);
    return r;
}

__declspec(dllexport) long __stdcall PassThruStopMsgFilter(unsigned long ChannelID, unsigned long FilterID)
{
    GUARD(StopFilter);
    return R.StopFilter(real_ch(ChannelID), FilterID);
}

__declspec(dllexport) long __stdcall PassThruSetProgrammingVoltage(unsigned long DeviceID, unsigned long PinNumber,
                                                                   unsigned long Voltage)
{
    GUARD(SetVoltage);
    return R.SetVoltage(real_dev(DeviceID), PinNumber, Voltage);
}

__declspec(dllexport) long __stdcall PassThruReadVersion(unsigned long DeviceID, char *pFirmware,
                                                         char *pDll, char *pApi)
{
    GUARD(ReadVersion);
    long r = R.ReadVersion(real_dev(DeviceID), pFirmware, pDll, pApi);
    plog("ReadVersion -> %ld (fw='%s' dll='%s' api='%s')", r,
         pFirmware ? pFirmware : "", pDll ? pDll : "", pApi ? pApi : "");
    return r;
}

__declspec(dllexport) long __stdcall PassThruGetLastError(char *pErrorDescription)
{
    GUARD(GetLastError_);
    long r = R.GetLastError_(pErrorDescription);
    plog("GetLastError -> %ld ('%s')", r, pErrorDescription ? pErrorDescription : "");
    return r;
}

__declspec(dllexport) long __stdcall PassThruIoctl(unsigned long ChannelID, unsigned long IoctlID,
                                                   void *pInput, void *pOutput)
{
    GUARD(Ioctl);
    return R.Ioctl(real_ch(ChannelID), IoctlID, pInput, pOutput);
}

/* ---- DllMain ------------------------------------------------------------ */

static void attach_marker(HINSTANCE hinst)
{
    char self[MAX_PATH] = "?";
    GetModuleFileNameA(hinst, self, MAX_PATH);
    FILE *f = fopen(FIXED_LOG, "a");
    if (!f) return;
    SYSTEMTIME t; GetLocalTime(&t);
    fprintf(f, "%02d:%02d:%02d.%03d ATTACH pid=%lu loaded-from '%s'\n",
            t.wHour, t.wMinute, t.wSecond, t.wMilliseconds,
            (unsigned long)GetCurrentProcessId(), self);
    fclose(f);
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    switch (reason) {
    case DLL_PROCESS_ATTACH:
        InitializeCriticalSection(&g_lock);
        g_lock_ready = 1;
        DisableThreadLibraryCalls(hinst);
        attach_marker(hinst);
        break;
    case DLL_PROCESS_DETACH:
        if (g_lock_ready) { DeleteCriticalSection(&g_lock); g_lock_ready = 0; }
        if (g_real) { FreeLibrary(g_real); g_real = NULL; }
        break;
    }
    return TRUE;
}
