"""
Pulse Connect - links a business's Tally to its Business Pulse dashboard.

Runs on the computer where Tally (or the Tally cloud/VCC client) is used. It:
  * pairs itself to the business (setup code from the file name, the clipboard, the setup web page, or typed),
  * finds Tally - any generation: Tally.ERP 9, every TallyPrime release, Tally on cloud / AWS via VCC,
  * switches on Tally's data link when it is off (with backup, check and automatic undo), or shows simple steps,
  * reads the books READ-ONLY and uploads the numbers to the private dashboard,
  * starts with Windows and re-syncs whenever something changes in Tally.

It never writes to Tally's data. The only Tally file it may touch is tally.ini (to switch the data link on),
and only after the user asks it to, with a backup that is restored if Tally does not come back healthy.
"""
import argparse, datetime as dt, hashlib, http.server, io, json, os, re, shutil, socket, socketserver, subprocess
import sys, threading, time, traceback, urllib.error, urllib.request, webbrowser
from pathlib import Path
from types import SimpleNamespace

VERSION = "1.1.2"
APP = "Pulse Connect"
UI_PORT = 47819
SITE = "https://tms-pulse-blush.vercel.app"
SB_URL = os.environ.get("PULSE_SB_URL") or "https://lktkhgjnldmgniesfcar.supabase.co"
SB_KEY = "sb_publishable_x63FxbxZkudEW5HHPx3j-g_eVGYIMAf"
CODE_ALPHA = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_RE = re.compile(r"(?<![A-Z0-9])([%s]{8})(?![A-Z0-9])" % CODE_ALPHA)
WIN = os.name == "nt"
COMPILED = "__compiled__" in globals()          # built as a real Windows program and installed by PulseConnectSetup.exe
FROZEN = getattr(sys, "frozen", False) or COMPILED
RES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
HOME = Path(os.environ.get("PULSE_HOME") or (Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PulseConnect"))
HOME.mkdir(parents=True, exist_ok=True)
CFG = HOME / "config.json"
LOG = HOME / "log.txt"
INSTALLED_EXE = Path(sys.executable).resolve() if COMPILED else HOME / "PulseConnect.exe"
CHECK_EVERY = int(os.environ.get("PULSE_CHECK") or 120)         # seconds between cheap "did anything change in Tally?" checks
FULL_EVERY = 60 * 60       # full refresh at least this often even if nothing changed (keeps "today" figures right)
SKIP_PORTS = {9999}        # Tally license server - never talk to it

sys.path.insert(0, str(RES))
import engine  # the proven read-only Tally reader + dashboard builder

class _LocalNoProxy(urllib.request.ProxyHandler):
    """Use the computer's proxy for the internet, but never for Tally on this computer."""
    def proxy_open(self, req, proxy, type):
        if (req.host or "").split(":")[0].strip("[]") in ("127.0.0.1", "localhost", "::1"): return None
        return super().proxy_open(req, proxy, type)
urllib.request.install_opener(urllib.request.build_opener(_LocalNoProxy()))

# ------------------------------------------------------------------ small helpers
_log_lock = threading.Lock()
def log(*a):
    line = "[{}] {}".format(dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), " ".join(str(x) for x in a))
    with _log_lock:
        try:
            if LOG.exists() and LOG.stat().st_size > 1_000_000:
                LOG.replace(HOME / "log.old.txt")
            with open(LOG, "a", encoding="utf-8") as f: f.write(line + "\n")
        except Exception: pass
    if not FROZEN:
        try: sys.__stdout__.write(line + "\n")
        except Exception: pass

def load_cfg():
    try: return json.loads(CFG.read_text(encoding="utf-8"))
    except Exception: return {}

def save_cfg(c):
    tmp = CFG.with_suffix(".tmp"); tmp.write_text(json.dumps(c, indent=1), encoding="utf-8"); tmp.replace(CFG)

def rpc(fn, body, timeout=60):
    req = urllib.request.Request(SB_URL + "/rest/v1/rpc/" + fn, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 method="POST", headers={"apikey": SB_KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None

def machine_name():
    return (os.environ.get("COMPUTERNAME") or socket.gethostname() or "Computer")[:60]

def ago(ts):
    if not ts: return ""
    s = int(time.time() - ts)
    if s < 60: return "just now"
    if s < 3600: return "{} min ago".format(s // 60)
    if s < 86400: return "{} hr ago".format(s // 3600)
    return "{} days ago".format(s // 86400)

def no_window():
    # windowed .exe has no console handles: give child processes empty ones and no window
    d = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if WIN: d["creationflags"] = 0x08000000   # CREATE_NO_WINDOW
    return d

def launch(args, cwd=None):
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if WIN: kw["creationflags"] = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(args, cwd=cwd, **kw)

# ------------------------------------------------------------------ Windows bits (all guarded)
def win_processes():
    """[(image_name_lower, pid)] of running processes."""
    if not WIN: return []
    try:
        kw = no_window(); kw.pop("stdout"); kw.pop("stderr")
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20, **kw).stdout
    except Exception: return []
    res = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].strip('"').isdigit():
            res.append((parts[0].lower(), int(parts[1].strip('"'))))
    return res

def exe_path_of(pid):
    if not WIN: return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h: return None
        try:
            buf = ctypes.create_unicode_buffer(1024); n = wintypes.DWORD(1024)
            k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)): return buf.value
        finally:
            k32.CloseHandle(h)
    except Exception: pass
    return None

def read_clipboard():
    if not WIN: return os.environ.get("PULSE_FAKE_CLIPBOARD", "")
    try:
        import ctypes
        from ctypes import wintypes
        u32 = ctypes.WinDLL("user32"); k32 = ctypes.WinDLL("kernel32")
        u32.GetClipboardData.restype = wintypes.HANDLE; u32.GetClipboardData.argtypes = [wintypes.UINT]
        k32.GlobalLock.restype = ctypes.c_void_p; k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        u32.OpenClipboard.argtypes = [wintypes.HWND]
        if not u32.OpenClipboard(None): return ""
        try:
            h = u32.GetClipboardData(13)   # CF_UNICODETEXT
            if not h: return ""
            p = k32.GlobalLock(h)
            if not p: return ""
            try: return ctypes.wstring_at(p)[:2000]
            finally: k32.GlobalUnlock(h)
        finally:
            u32.CloseClipboard()
    except Exception: return ""

def set_startup(enable=True):
    if not WIN: return
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE)
        if enable: winreg.SetValueEx(k, "PulseConnect", 0, winreg.REG_SZ, '"{}" --background'.format(INSTALLED_EXE))
        else:
            try: winreg.DeleteValue(k, "PulseConnect")
            except FileNotFoundError: pass
        winreg.CloseKey(k)
    except Exception as e: log("startup entry failed:", e)

def run_elevated(args):
    """Run this program again as administrator (Windows shows one 'Yes/No' prompt). Returns True if launched."""
    if not WIN: return False
    import ctypes
    exe = sys.executable
    params = " ".join('"{}"'.format(a) for a in ([] if FROZEN else [str(Path(__file__).resolve())]) + args)
    r = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
    return r > 32

def registry_tally_dirs():
    dirs = []
    if not WIN: return dirs
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for base in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try: k = winreg.OpenKey(root, base)
                except OSError: continue
                for i in range(4000):
                    try: sub = winreg.EnumKey(k, i)
                    except OSError: break
                    try:
                        sk = winreg.OpenKey(k, sub)
                        name = str(winreg.QueryValueEx(sk, "DisplayName")[0])
                        if "tally" in name.lower():
                            for v in ("InstallLocation", "DisplayIcon"):
                                try:
                                    val = str(winreg.QueryValueEx(sk, v)[0]).strip('"').split(",")[0]
                                    if val: dirs.append(Path(val) if Path(val).suffix == "" else Path(val).parent)
                                except OSError: pass
                    except OSError: pass
    except Exception: pass
    return dirs

_INSTALLS = {"t": 0, "v": []}
def find_tally_installs():
    """Every tally.exe we can find on this computer (newest first). Cached for 10 minutes."""
    if time.time() - _INSTALLS["t"] < 600: return _INSTALLS["v"]
    _INSTALLS["v"] = _find_tally_installs(); _INSTALLS["t"] = time.time()
    return _INSTALLS["v"]

def _find_tally_installs():
    cands = registry_tally_dirs()
    pf = [os.environ.get(k) for k in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    for d in [p for p in pf if p] + ["C:\\", "D:\\", "E:\\"]:
        for n in ("TallyPrime", "TallyPrimeEditLog", "TallyPrime Server", "Tally.ERP9", "Tally.ERP 9", "Tally", "TallyERP9", "Tally9"):
            cands.append(Path(d) / n)
        try:
            for sub in Path(d).iterdir():
                if "tally" in sub.name.lower() and sub.is_dir(): cands.append(sub)
        except Exception: pass
    seen, found = set(), []
    for c in cands:
        exe = c / "tally.exe"
        try:
            key = str(exe).lower()
            if key in seen or not exe.exists(): continue
            seen.add(key); found.append(exe)
        except Exception: pass
    found.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
    return found

def product_from_path(p):
    s = str(p or "").lower()
    if "erp" in s or "tally9" in s: return "Tally.ERP 9"
    if "prime" in s: return "TallyPrime"
    return None

VCC_DIRS = [Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Virtual Computer Connection",
            Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Virtual Computer Connection"]
def vcc_info():
    if not WIN: return None
    for d in VCC_DIRS:
        if d.exists():
            lock = d / "connection.lock"
            running = any(n.startswith(("tallygo", "virtualcomputer", "vcc")) for n, _ in win_processes())
            return {"dir": str(d), "lock": lock.exists(), "lock_path": str(lock), "running": running}
    return None

# ------------------------------------------------------------------ Tally detection
def probe_port(port, host="127.0.0.1", timeout=2.5):
    """Returns 'TallyPrime Server is Running'-style banner if Tally answers on this port, else None."""
    if port in SKIP_PORTS: return None
    try:
        with socket.create_connection((host, port), timeout=1.0): pass
    except OSError:
        return None
    try:
        with urllib.request.urlopen("http://{}:{}/".format(host, port), timeout=timeout) as r:
            body = r.read(400).decode("utf-8", "ignore")
        if "tally" in body.lower(): return body.strip()[:80] or "Tally"
        return None
    except urllib.error.HTTPError:
        return None
    except Exception:
        # Some builds answer GET slowly / oddly: confirm with a tiny XML request instead.
        # (a cloud/VCC tunnel can accept the connection and then never answer - keep this short)
        try:
            req = urllib.request.Request("http://{}:{}".format(host, port), method="POST", headers={"Content-Type": "text/xml"},
                data=b"<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Function</TYPE><ID>$$SysInfo:Version</ID></HEADER><BODY><DESC></DESC></BODY></ENVELOPE>")
            with urllib.request.urlopen(req, timeout=5) as r:
                body = r.read(2000).decode("utf-8", "ignore")
            return "Tally" if "<ENVELOPE" in body.upper() or "RESPONSE" in body.upper() else None
        except Exception:
            return None

def listening_ports(pids):
    """Ports that Tally's own process is listening on (exact - no guessing)."""
    if not (WIN and pids): return []
    try:
        kw = no_window(); kw.pop("stdout"); kw.pop("stderr")
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20, **kw).stdout
    except Exception: return []
    ports = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4].isdigit() and int(parts[4]) in pids:
            try:
                p = int(parts[1].rsplit(":", 1)[1])
                if p not in ports and p not in SKIP_PORTS: ports.append(p)
            except ValueError: pass
    return ports

VERSION_REQ = ("<ENVELOPE><HEADER><VERSION>1</VERSION><TALLYREQUEST>Export</TALLYREQUEST><TYPE>Data</TYPE><ID>PulseInfo</ID></HEADER><BODY><DESC>"
    "<STATICVARIABLES><SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT></STATICVARIABLES><TDL><TDLMESSAGE>"
    "<REPORT NAME='PulseInfo'><FORMS>PulseInfo</FORMS></REPORT><FORM NAME='PulseInfo'><TOPPARTS>PulseInfo</TOPPARTS></FORM>"
    "<PART NAME='PulseInfo'><TOPLINES>PulseInfo</TOPLINES><SCROLLED>Vertical</SCROLLED></PART>"
    "<LINE NAME='PulseInfo'><LEFTFIELDS>PulseRel,PulsePath</LEFTFIELDS></LINE>"
    "<FIELD NAME='PulseRel'><USE>Name Field</USE><SET>$$ProdInfo:ProdReleaseAsStr</SET><XMLTAG>PULSEREL</XMLTAG></FIELD>"
    "<FIELD NAME='PulsePath'><USE>Name Field</USE><SET>$$SysInfo:ApplicationPath</SET><XMLTAG>PULSEPATH</XMLTAG></FIELD>"
    "</TDLMESSAGE></TDL></DESC></BODY></ENVELOPE>")
def tally_release(host, port):
    """Exact Tally release (e.g. 'TallyPrime 5.1' / 'Tally.ERP 9 6.6.3') and where it runs from. Best effort."""
    try:
        req = urllib.request.Request("http://{}:{}".format(host, port), data=VERSION_REQ.encode("utf-8"), method="POST",
                                     headers={"Content-Type": "text/xml; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=8) as r:
            t = engine._clean_xml(engine._decode(r.read()))
        rel = re.search(r"<PULSEREL>(.*?)</PULSEREL>", t, re.S); path = re.search(r"<PULSEPATH>(.*?)</PULSEPATH>", t, re.S)
        return ((rel.group(1).strip() if rel else None) or None, (path.group(1).strip() if path else None) or None)
    except Exception:
        return (None, None)

def ini_port(ini):
    try:
        for line in Path(ini).read_text(encoding="utf-8", errors="ignore").splitlines():
            k, _, v = line.partition("=")
            if k.replace(" ", "").lower() == "serverport" and v.strip().isdigit(): return int(v.strip())
    except Exception: pass
    return None

_REL = {}
def detect(cfg):
    """Look at the computer and say where Tally is and whether we can talk to it."""
    host = cfg.get("host") or "127.0.0.1"
    procs = win_processes()
    tally_pids = [pid for n, pid in procs if n in ("tally.exe", "tallyprime.exe")]
    exe = None
    for pid in tally_pids:
        exe = exe_path_of(pid)
        if exe: break
    installs = find_tally_installs() if WIN else []
    ini = None
    for d in ([Path(exe).parent] if exe else []) + [p.parent for p in installs]:
        if (d / "tally.ini").exists(): ini = d / "tally.ini"; break
    ports = listening_ports(set(tally_pids))
    for p in (cfg.get("port"), ini_port(ini) if ini else None, 9000, 9001, 9002, 9003, 9004, 9005, 9008, 9010, 9050, 9090, 9100):
        if p and p not in ports and p not in SKIP_PORTS: ports.append(p)
    extra = os.environ.get("PULSE_TALLY_PORTS")   # testing / unusual setups
    if extra: ports = [int(x) for x in extra.split(",")] + ports
    found = None
    for p in ports:
        b = probe_port(p, host)
        if b: found = (p, b); break
    product = None
    if found:
        product = "Tally.ERP 9" if "erp" in found[1].lower() else ("TallyPrime" if "prime" in found[1].lower() else None)
    product = product or product_from_path(exe) or (product_from_path(installs[0]) if installs else None)
    release = None
    if found:
        key = (host, found[0])
        if _REL.get("key") != key or time.time() - _REL.get("t", 0) > 3600:
            _REL.update({"key": key, "t": time.time(), "v": tally_release(host, found[0])})
        release, rpath = _REL["v"]
        if release:
            product = product or ("Tally.ERP 9" if "erp" in release.lower() else None)
    return {"release": release, "running": bool(tally_pids) or bool(found), "pids": tally_pids, "exe": exe, "installs": [str(i) for i in installs[:5]],
            "ini": str(ini) if ini else None, "port": found[0] if found else None, "banner": found[1] if found else None,
            "product": product, "vcc": vcc_info(), "host": host}

# ------------------------------------------------------------------ switching the data link on (tally.ini)
INI_WANT = [("Client Server", "Both"), ("ServerPort", None)]
def ini_enable(ini, port):
    """Set Tally to act as Both (client+server) on `port`. Keeps a backup. Returns backup path."""
    ini = Path(ini)
    bak = ini.with_name("tally.ini.pulse-backup")
    if not bak.exists(): shutil.copy2(ini, bak)
    raw = ini.read_bytes()
    enc = "utf-16" if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else "utf-8"
    text = raw.decode(enc, errors="ignore")
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    want = {"clientserver": ("Client Server", "Both"), "serverport": ("ServerPort", str(port))}
    done = set()
    for i, line in enumerate(lines):
        k, sep, v = line.partition("=")
        nk = k.replace(" ", "").lower()
        if sep and nk in want:
            lines[i] = "{}={}".format(k.rstrip(), want[nk][1]); done.add(nk)
    # insert missing keys right after the [Tally] section header (or at top)
    at = next((i + 1 for i, l in enumerate(lines) if l.strip().lower() == "[tally]"), 0)
    for nk, (k, v) in want.items():
        if nk not in done: lines.insert(at, "{}={}".format(k, v))
    ini.write_bytes(nl.join(lines).encode(enc))
    return str(bak)

def ini_restore(ini):
    ini = Path(ini); bak = ini.with_name("tally.ini.pulse-backup")
    if bak.exists(): shutil.copy2(bak, ini); return True
    return False

def run_with_deadline(fn, seconds):
    """Run fn in a helper thread. Returns (finished, result, error, thread)."""
    box = {}
    def go():
        try: box["r"] = fn()
        except Exception as e: box["e"] = e; box["tb"] = traceback.format_exc()[-1500:]
    t = threading.Thread(target=go, daemon=True); t.start(); t.join(seconds)
    return (not t.is_alive(), box.get("r"), box.get("e"), t)

# ------------------------------------------------------------------ the worker
class Connector:
    def __init__(self):
        self.cfg = load_cfg()
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.state = {"step": "starting", "title": "Starting…", "detail": "", "progress": [], "version": VERSION}
        self.tally = {}
        self.companies = []
        self.last_sync = self.cfg.get("last_sync_ts")
        self.last_alt = self.cfg.get("last_alt")
        self.last_status_push = 0
        self.syncing = False
        self.fix = None            # current tally.ini fix job
        self.sync_error = None
        self.ui_opened = False

    # -- state for the UI
    def set(self, step, title, detail="", **kw):
        with self.lock:
            self.state.update({"step": step, "title": title, "detail": detail}); self.state.update(kw)

    def snapshot(self):
        with self.lock:
            s = dict(self.state)
            s.update({"business": self.cfg.get("business"), "paired": bool(self.cfg.get("push_key")), "company": self.cfg.get("company"),
                      "companies": [c["name"] for c in self.companies], "tally": self.tally, "machine": machine_name(),
                      "last_sync": self.last_sync, "last_sync_ago": ago(self.last_sync), "dashboard": SITE,
                      "syncing": self.syncing, "fix": self.fix, "version": VERSION})
            return s

    # -- pairing
    def find_code(self):
        """A setup code from: our file name, the clipboard (the setup page copies it), or the command line."""
        cands = []
        for a in sys.argv[1:]:
            if a.startswith("--code="): cands.append(a.split("=", 1)[1])
        cands.append(Path(sys.executable if FROZEN else sys.argv[0]).stem.upper())
        clip = read_clipboard()
        m = re.search(r"PULSE[-:\s]*([A-Z0-9]{8})", clip.upper()) or re.search(r"[?&]code=([A-Za-z0-9]{8})", clip)
        if m: cands.append(m.group(1))
        for c in cands:
            m = CODE_RE.search((c or "").upper())
            if m: return m.group(1)
        return None

    def pair(self, code):
        code = (code or "").strip().upper().replace(" ", "").replace("-", "")
        code = code[-8:] if code.startswith("PULSE") else code
        if not re.fullmatch("[%s]{8}" % CODE_ALPHA, code):
            return {"ok": False, "error": "That code doesn't look right. It has 8 letters/numbers, like K7M2QX9P."}
        try:
            st = rpc("setup_status", {"p_code": code})
        except Exception:
            return {"ok": False, "error": "No internet right now. Check the connection and try again."}
        if not st:
            return {"ok": False, "error": "We couldn't find that code. Please check it on the setup page."}
        try:
            res = rpc("claim_setup", {"p_code": code, "p_machine": machine_name()})
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"ok": False, "error": "This setup code was already used on another computer. For safety each code works once. "
                                              "Ask the owner for a new setup link (on their Pulse page: \"Set up on another computer\")."}
            return {"ok": False, "error": "Couldn't connect just now. Try again in a minute."}
        except Exception:
            return {"ok": False, "error": "Couldn't connect just now. Check the internet and try again."}
        with self.lock:
            if self.cfg.get("code") != code:      # a different business: start fresh (first full upload)
                for k in ("last_sync_ts", "last_alt", "settings"): self.cfg.pop(k, None)
                self.last_sync = None; self.last_alt = None
                try: (HOME / "month_sig.json").unlink()
                except Exception: pass
            self.cfg.update({"code": code, "push_key": res["push_key"], "business": res.get("name") or st.get("name"), "paired_at": time.time()})
            save_cfg(self.cfg)
        self.write_cloud_cfg()
        log("paired to", self.cfg.get("business"))
        self.push_status("paired", "Connected to Pulse. Looking for Tally…", force=True)
        self.wake.set()
        return {"ok": True, "business": self.cfg.get("business")}

    def write_cloud_cfg(self):
        if not self.cfg.get("push_key"): return
        (HOME / "cloud_config.txt").write_text("supabase_url={}\nsupabase_key={}\npush_key={}\n".format(SB_URL, SB_KEY, self.cfg["push_key"]), encoding="utf-8")

    def unpair(self):
        with self.lock:
            for k in ("code", "push_key", "business", "company", "last_alt", "last_sync_ts"): self.cfg.pop(k, None)
            save_cfg(self.cfg)
        try: (HOME / "cloud_config.txt").unlink()
        except Exception: pass
        self.last_sync = None; self.last_alt = None
        self.wake.set()

    # -- status heartbeat to the owner's setup page
    def push_status(self, state, msg, force=False, **extra):
        if not self.cfg.get("push_key"): return
        if not force and time.time() - self.last_status_push < 240 and state == self.state.get("_pushed"): return
        body = {"state": state, "msg": msg, "company": self.cfg.get("company"), "product": self.tally.get("product"), "release": self.tally.get("release"),
                "machine": machine_name(), "app": VERSION, "last_sync": self.last_sync}
        body.update(extra)
        try:
            rpc("push_status", {"push_key": self.cfg["push_key"], "status": body}, timeout=20)
            self.last_status_push = time.time(); self.state["_pushed"] = state
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):   # this computer was replaced by another one with the same code
                log("push key rejected - unpairing"); self.unpair()
        except Exception as e:
            log("status push failed:", e)

    # -- per-business settings kept in the cloud (brand, how to describe the business to the assistant, govt buyers)
    GENERIC_GOVT = ("GOVT", "GOVERNMENT", "DIRECTORATE", "DEPARTMENT OF", "MUNICIPAL", "NAGAR NIGAM", "MINISTRY", "COMMISSIONER",
                    "DISTRICT HOSPITAL", "MEDICAL COLLEGE", "ARMY", "DEFENCE", "CRPF", "BSF", "RAILWAY", "COLLECTORATE", "PANCHAYAT")
    def load_settings(self):
        try:
            r = rpc("get_settings", {"push_key": self.cfg["push_key"]}, timeout=20) or {}
            st = r.get("settings") or {}
            with self.lock:
                self.cfg["settings"] = st
                if r.get("name"): self.cfg["business"] = r["name"]
                save_cfg(self.cfg)
        except Exception as e:
            log("settings fetch failed (using saved):", e)
        st = self.cfg.get("settings") or {}
        engine.GOVT_WORDS = tuple(w.upper() for w in st.get("govt_words") or []) or self.GENERIC_GOVT
        try: engine.DEFAULT_CREDIT_DAYS = int(st.get("credit_days") or 30)
        except (TypeError, ValueError): engine.DEFAULT_CREDIT_DAYS = 30
        try: engine.GOVT_CREDIT_DAYS = int(st["govt_credit_days"]) if st.get("govt_credit_days") not in (None, "") else None
        except (TypeError, ValueError): engine.GOVT_CREDIT_DAYS = None
        return st

    # -- precise change tracking: which months were edited, added to or deleted from since last time
    def month_signatures(self):
        """{'YYYY-MM': 'count:maxAlterId'} for the 13 months we show - one light request (dates + change ids only)."""
        today = dt.date.today()
        start = dt.date(today.year - 1, today.month, 1)
        T = engine.Tally(self.tally.get("host", "127.0.0.1"), self.tally["port"], self.cfg["company"])
        root = T.collection("Voucher", "DATE, ALTERID", start, today, filt=True)
        sig = {}
        for v in root.iter("VOUCHER"):
            d = engine.tdate(engine.txt(v, "DATE"))
            if not d: continue
            k = "{}-{:02d}".format(d.year, d.month)
            n, mx = sig.get(k, (0, 0))
            try: a = int(engine.txt(v, "ALTERID") or 0)
            except ValueError: a = 0
            sig[k] = (n + 1, max(mx, a))
        return {k: "{}:{}".format(*v) for k, v in sig.items()}

    def invalidate_changed(self):
        """Drop cached months that changed in Tally (edits, new or deleted vouchers), so old months stay correct."""
        f = HOME / "month_sig.json"
        try: old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
        except Exception: old = None
        try:
            new = self.month_signatures()
        except Exception as e:
            log("month check failed:", e); return
        if old and old.get("company") == self.cfg.get("company"):
            changed = sorted(k for k in set(old["sig"]) | set(new) if old["sig"].get(k) != new.get(k))
            if changed:
                cache = engine.CACHE
                first = changed[0].replace("-", "")
                for p in cache.glob("*.json") if cache.exists() else []:
                    n = p.stem
                    if (n.startswith("vch_") and n[4:].replace("-", "") in [c.replace("-", "") for c in changed]) or \
                       (n.startswith("led_") and n[4:].isdigit() and n[4:] >= first) or n.startswith("stock_"):
                        try: p.unlink()
                        except Exception: pass
                log("changed months:", ", ".join(changed))
        f.write_text(json.dumps({"company": self.cfg.get("company"), "sig": new}), encoding="utf-8")

    # -- syncing
    def do_sync(self, reason):
        cfg = self.cfg
        self.syncing = True; self.sync_error = None
        first = not self.last_sync
        self.set("syncing", "Reading your books…" if first else "Updating…",
                 "First time takes a few minutes. Keep Tally open." if first else "Something changed in Tally.", progress=[])
        self.push_status("syncing", "Reading the books from Tally…", force=True)
        args = SimpleNamespace(host=self.tally.get("host", "127.0.0.1"), port=self.tally["port"], company=cfg["company"],
                               od_limit=float(cfg.get("od_limit") or 0), every=60, open=False)
        buf = _Progress(self)
        t0 = time.time()
        log("sync start ({})".format(reason))
        done, _, _, th = run_with_deadline(self.invalidate_changed, 150)
        if not done:
            log("change check is taking too long - Tally not answering"); self.stuck_thread = th
            self.syncing = False; self.sync_error = "Tally isn't answering"; return False
        settings = self.load_settings()
        try:
            old = sys.stdout; sys.stdout = buf
            try:
                done, data, err, th = run_with_deadline(lambda: engine.sync(args), 1200 if first else 480)
            finally:
                if done: sys.stdout = old
            if not done:
                self.stuck_thread = th
                raise TimeoutError("Tally isn't answering")
            if err: raise err
            data["meta"]["product"] = " ".join(x for x in (self.tally.get("product"), self.tally.get("release")) if x) or "Tally"
            data["meta"]["connector"] = VERSION
            for k in ("brand", "about", "ask_label"):
                if settings.get(k): data["meta"][k] = settings[k]
            self.set("syncing", "Uploading to your dashboard…", "")
            engine.cloud_push(data)
            self.last_sync = time.time()
            with self.lock:
                self.cfg["last_sync_ts"] = self.last_sync; self.cfg["last_alt"] = self.last_alt; save_cfg(self.cfg)
            log("sync ok in {:.0f}s ({})".format(time.time() - t0, reason))
            self.push_status("live", "Live", force=True, sync_seconds=round(time.time() - t0))
            return True
        except Exception as e:
            self.sync_error = str(e)[:300]
            log("sync failed:", e, traceback.format_exc()[-1500:])
            return False
        finally:
            self.syncing = False

    def company_alt(self):
        for c in self.companies:
            if c["name"] == self.cfg.get("company"): return "{}|{}".format(c.get("alt_vch"), c.get("alt_mst"))
        return None

    # -- the main loop: figure out what is needed next, do it, sleep
    def tick(self):
        cfg = self.cfg
        th = getattr(self, "stuck_thread", None)
        if th is not None:
            if th.is_alive():   # an earlier read is still waiting on Tally - don't pile up more requests
                self.set("error", "Tally isn't answering", "Tally may be showing a message box or busy with a report. Close any popup in Tally (press Esc) and it will continue by itself.")
                self.push_status("error", "Tally isn't answering - a popup may be open in Tally")
                return 20
            self.stuck_thread = None; sys.stdout = sys.__stdout__
        if getattr(self, "stuck", False):
            self.stuck = False
        if not cfg.get("push_key"):
            code = self.find_code() if not cfg.get("asked_code") else None
            if code:
                self.set("pairing", "Connecting…", "Found your setup code " + code)
                r = self.pair(code)
                if r.get("ok"): return 2
                cfg["asked_code"] = True
            self.set("pair", "Enter your setup code", "It's on the setup page your owner sent you (8 letters, like K7M2QX9P).")
            return 5

        if self.fix and self.fix.get("phase") not in (None, "done", "failed"):
            return self.run_fix()

        self.tally = detect(cfg)
        t = self.tally
        if not t["port"]:
            self.companies = []
            v = t.get("vcc")
            if v and not t["pids"]:
                # Tally lives on a cloud computer reached through Virtual Computer Connection (Tally on AWS etc.)
                self.set("remote", "Connect to your Tally cloud", "", vcc=v)
                self.push_status("remote", "Waiting for the Tally cloud (VCC) connection on {}".format(machine_name()))
                return 15
            if not t["running"]:
                self.set("open_tally", "Open Tally", "Open Tally and your company. We'll take it from there.")
                self.push_status("tally_closed", "Tally is closed on {}".format(machine_name()))
                return 10
            self.set("port_off", "One setting to switch on in Tally", "")
            self.push_status("port_off", "Tally is open but its data link is switched off")
            return 8

        # Tally answers. Which company?
        try:
            self.companies = engine.list_companies(t["host"], t["port"])
        except Exception as e:
            log("company list failed:", e); self.companies = []
        names = [c["name"] for c in self.companies]
        if not names:
            self.set("no_company", "Open your company in Tally", "Tally is running, but no company is open. Press F3 (or Alt+F3 in Tally.ERP 9) and select it.")
            self.push_status("no_company", "Tally is open but no company is loaded")
            return 8
        if cfg.get("company") not in names:
            if cfg.get("company") and not cfg.get("company_confirmed_missing"):
                # The usual company is simply not open right now - wait instead of switching to another one.
                self.set("company_closed", "Open " + cfg["company"].title(), "Tally is open, but this company is not. Open it and we continue.")
                self.push_status("no_company", "{} is not open in Tally".format(cfg["company"]))
                return 10
            if len(names) == 1:
                with self.lock: cfg["company"] = names[0]; save_cfg(cfg)
            else:
                self.set("company", "Which company should we show?", "")
                return 3
        cfg.pop("company_confirmed_missing", None)

        # Up to date?
        alt = self.company_alt()
        stale = (not self.last_sync or time.time() - self.last_sync > FULL_EVERY or
                 dt.date.fromtimestamp(self.last_sync) != dt.date.today())
        if alt != self.last_alt or stale or self.state.get("force_sync"):
            self.state["force_sync"] = False
            reason = "changed" if alt != self.last_alt else "scheduled"
            self.last_alt = alt
            ok = self.do_sync(reason)
            if not ok:
                self.set("error", "Couldn't read the books", self.friendly_error(self.sync_error))
                self.push_status("error", "Sync problem: " + (self.sync_error or "")[:120], force=True)
                self.last_alt = None
                return 60
        self.set("live", "You're connected", "")
        self.push_status("live", "Live")
        return CHECK_EVERY

    def friendly_error(self, e):
        e = e or ""
        if "isn't answering" in e: return "Tally isn't answering. If a message box is open in Tally, close it (press Esc). We'll keep trying."
        if "timed out" in e.lower(): return "Tally took too long to answer. If Tally is busy (printing, reports), we'll try again in a minute."
        if "connection" in e.lower() or "refused" in e.lower(): return "Tally stopped answering. Is it still open? We'll try again in a minute."
        if "tally error" in e.lower(): return "Tally said: " + e.split(":", 1)[-1].strip()[:160]
        return "We'll try again in a minute. (" + e[:140] + ")"

    # -- switching the data link on, step by step (waits for the user to close Tally)
    def start_fix(self):
        t = self.tally
        ini = t.get("ini")
        exe = t.get("exe") or (t.get("installs") or [None])[0]
        if not ini or not exe:
            self.fix = {"phase": "failed", "msg": "We couldn't find Tally's settings file. Please use the steps on the right — it takes a minute."}
            return
        self.fix = {"phase": "wait_close", "ini": ini, "exe": exe, "port": t.get("port") or ini_port(ini) or 9000, "t": time.time(),
                    "msg": "Save your work and close Tally (press Esc, then Yes). We'll switch the setting on and open Tally again."}
        self.wake.set()

    def run_fix(self):
        f = self.fix
        if f["phase"] == "wait_close":
            if any(n in ("tally.exe", "tallyprime.exe") for n, _ in win_processes()):
                if time.time() - f["t"] > 600: self.fix = {"phase": "failed", "msg": "Tally wasn't closed, so we stopped. Click the button again any time."}
                return 2
            try:
                ini_enable(f["ini"], f["port"]); f["how"] = "direct"
            except PermissionError:
                # Tally is in a protected folder: ask Windows for permission once.
                if not run_elevated(["--fix-ini", f["ini"], str(f["port"])]):
                    self.fix = {"phase": "failed", "msg": "Windows didn't allow the change. Please use the steps on the right."}; return 3
                time.sleep(6); f["how"] = "elevated"
            except Exception as e:
                self.fix = {"phase": "failed", "msg": "Couldn't change the setting ({}). Please use the steps on the right.".format(e)}; return 3
            try:
                launch([f["exe"]], cwd=str(Path(f["exe"]).parent))
            except Exception as e:
                log("relaunch failed", e)
            f.update({"phase": "wait_port", "t": time.time(), "msg": "Opening Tally again… open your company when it asks."})
            return 3
        if f["phase"] == "wait_port":
            if probe_port(f["port"]):
                self.fix = {"phase": "done", "msg": "Done! Tally's data link is on."}
                self.cfg["port"] = f["port"]; save_cfg(self.cfg)
                return 1
            if time.time() - f["t"] > 150:
                if f.get("how") == "elevated": run_elevated(["--restore-ini", f["ini"]])
                else:
                    try: ini_restore(f["ini"])
                    except Exception: pass
                self.fix = {"phase": "failed", "msg": "That didn't work on this Tally, so we put the setting back as it was. Please use the steps on the right."}
                return 3
            return 3
        return 3

    def loop(self):
        while True:
            try:
                wait = self.tick()
            except Exception:
                log("tick error", traceback.format_exc()[-2000:]); wait = 30
            self.wake.wait(wait); self.wake.clear()


class _Progress(io.TextIOBase):
    """Turns the engine's progress prints into friendly lines for the UI."""
    WORDS = [("groups", "Account groups"), ("ledgers", "Ledgers & balances"), ("stock items", "Stock items"),
             ("bills receivable", "Bills to collect & pay"), ("vouchers", "Vouchers (13 months)"), ("party contacts", "Customer & supplier contacts"),
             ("post-dated", "Post-dated cheques"), ("dashboard written", "Building your dashboard")]
    def __init__(self, c): self.c = c
    def write(self, s):
        s = s.strip()
        if not s: return 0
        log("  " + s)
        for k, label in self.WORDS:
            if s.lower().startswith(k):
                with self.c.lock:
                    pr = self.c.state.setdefault("progress", [])
                    if label not in pr: pr.append(label)
        return len(s)


# ------------------------------------------------------------------ local web UI (127.0.0.1 only)
C = None
ALLOWED_ORIGINS = {SITE, "http://127.0.0.1:%d" % UI_PORT, "http://localhost:%d" % UI_PORT}

class UI(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _cors(self):
        o = self.headers.get("Origin")
        if o in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", o); self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Pulse")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Private-Network", "true")
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code); self._cors()
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(b)))
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers(); self.wfile.write(b)
    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0]
        return h in ("127.0.0.1", "localhost")      # blocks DNS-rebinding
    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()
    def do_GET(self):
        if not self._host_ok(): return self._send(403, {"error": "forbidden"})
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self._send(200, (RES / "ui.html").read_bytes(), "text/html")
        if p == "/api/state": return self._send(200, C.snapshot())
        if p == "/hello":   # the setup web page checks whether the app is running here
            return self._send(200, {"app": "pulse-connect", "version": VERSION, "paired": bool(C.cfg.get("push_key")), "business": C.cfg.get("business")})
        if p == "/focus":
            threading.Thread(target=open_ui, daemon=True).start(); return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})
    def do_POST(self):
        if not self._host_ok(): return self._send(403, {"error": "forbidden"})
        o = self.headers.get("Origin")
        if o and o not in ALLOWED_ORIGINS: return self._send(403, {"error": "forbidden"})
        if self.headers.get("X-Pulse") != "1": return self._send(403, {"error": "forbidden"})   # needs a CORS preflight => only our pages
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(n, 20000)) or b"{}")
        except Exception: body = {}
        p = self.path.split("?")[0]
        if p in ("/api/pair", "/pair"):
            if C.cfg.get("push_key") and p == "/pair" and body.get("code", "").upper() == C.cfg.get("code"):
                return self._send(200, {"ok": True, "business": C.cfg.get("business")})
            return self._send(200, C.pair(body.get("code")))
        if o == SITE: return self._send(403, {"error": "forbidden"})   # the web page may only pair
        if p == "/api/company":
            name = body.get("name")
            if name in [c["name"] for c in C.companies]:
                with C.lock:
                    if name != C.cfg.get("company"): C.last_sync = None; C.last_alt = None
                    C.cfg["company"] = name; save_cfg(C.cfg)
                C.wake.set(); return self._send(200, {"ok": True})
            return self._send(400, {"ok": False})
        if p == "/api/action":
            a = body.get("do")
            if a == "sync_now": C.state["force_sync"] = True
            elif a == "open_tally":
                exe = C.tally.get("exe") or (C.tally.get("installs") or [None])[0]
                if exe:
                    try: launch([exe], cwd=str(Path(exe).parent))
                    except Exception as e: return self._send(200, {"ok": False, "error": str(e)})
                else: return self._send(200, {"ok": False, "error": "We couldn't find Tally on this computer. Open it from the Start menu."})
            elif a == "fix_port": C.start_fix()
            elif a == "cancel_fix": C.fix = None
            elif a == "fix_vcc":
                v = C.tally.get("vcc") or {}
                if v.get("lock"):
                    ok = run_elevated(["--fix-vcc", v["lock_path"]])
                    return self._send(200, {"ok": ok})
            elif a == "change_company":
                with C.lock: C.cfg.pop("company", None); save_cfg(C.cfg)
            elif a == "unpair": C.unpair()
            elif a == "quit":
                self._send(200, {"ok": True}); log("quit requested (newer copy starting)")
                threading.Timer(0.5, lambda: os._exit(0)).start(); return
            elif a == "retry": C.cfg.pop("asked_code", None)
            C.wake.set(); return self._send(200, {"ok": True})
        return self._send(404, {"error": "not found"})

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = not WIN

def open_ui():
    webbrowser.open("http://127.0.0.1:%d/" % UI_PORT)

def _ver(v):
    try: return tuple(int(x) for x in str(v).split("."))
    except Exception: return (0,)

def already_running():
    """True if a copy is already running. An older copy is asked to step aside for this newer one."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/hello" % UI_PORT, timeout=2) as r:
            h = json.loads(r.read())
    except Exception:
        return False
    if h.get("app") != "pulse-connect": return False
    if _ver(h.get("version")) < _ver(VERSION):
        try:
            urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/api/action" % UI_PORT, data=b'{"do":"quit"}', method="POST",
                                   headers={"X-Pulse": "1", "Content-Type": "application/json"}), timeout=5).close()
        except Exception: pass
        for _ in range(20):
            time.sleep(0.5)
            try: urllib.request.urlopen("http://127.0.0.1:%d/hello" % UI_PORT, timeout=1).close()
            except Exception: return False
    return True

# ------------------------------------------------------------------ install / update
def install_self():
    """Copy the downloaded program into the user's app folder and start it from there (no admin needed)."""
    if not (WIN and FROZEN) or COMPILED: return False   # the installer already put us in place
    me = Path(sys.executable).resolve()
    if me == INSTALLED_EXE.resolve(): return False
    code = None
    m = CODE_RE.search(me.stem.upper())
    if m: code = m.group(1)
    inst = load_cfg().get("installed_version")
    if INSTALLED_EXE.exists() and inst and _ver(inst) > _ver(VERSION):
        # an older download was opened again: never downgrade - just start the newer installed copy
        launch([str(INSTALLED_EXE)] + (["--code=" + code] if code else []), cwd=str(HOME))
        return True
    for i in range(12):
        try:
            shutil.copy2(me, INSTALLED_EXE); break
        except Exception as e:
            if i == 11:
                log("install copy failed (running from download):", e); return False
            time.sleep(0.5)
    set_startup(True)
    args = [str(INSTALLED_EXE)] + (["--code=" + code] if code else [])
    launch(args, cwd=str(HOME))
    return True

def check_update(c):
    """Once a day: if a newer Pulse Connect is published, download it, check it, and swap it in."""
    if not (WIN and FROZEN): return
    try:
        with urllib.request.urlopen(SITE + "/connect/version.json", timeout=20) as r:
            info = json.loads(r.read())
        if tuple(int(x) for x in info["version"].split(".")) <= tuple(int(x) for x in VERSION.split(".")): return
        if c.syncing: return
        url = info.get("setup_url") if COMPILED else info["url"]
        want = (info.get("setup_sha256") if COMPILED else info.get("sha256")) or ""
        if not url or not want: return
        new = HOME / ("PulseConnectSetup.new.exe" if COMPILED else "PulseConnect.new.exe")
        with urllib.request.urlopen(url, timeout=300) as r, open(new, "wb") as f: shutil.copyfileobj(r, f)
        if hashlib.sha256(new.read_bytes()).hexdigest() != want.lower():
            new.unlink(); log("update checksum mismatch - ignored"); return
        log("updating to", info["version"])
        if COMPILED:   # the installer closes us, replaces the files and starts the new version
            subprocess.Popen([str(new), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"], close_fds=True, **no_window())
            return
        cmd = 'ping -n 4 127.0.0.1 >nul & move /y "{n}" "{o}" & start "" "{o}" --background'.format(n=new, o=INSTALLED_EXE)
        subprocess.Popen(["cmd", "/c", cmd], close_fds=True, **no_window())
        os._exit(0)
    except Exception as e:
        log("update check:", e)

# ------------------------------------------------------------------ entry
def main():
    global C
    ap = argparse.ArgumentParser()
    ap.add_argument("--background", action="store_true")
    ap.add_argument("--code")
    ap.add_argument("--fix-vcc"); ap.add_argument("--fix-ini", nargs=2); ap.add_argument("--restore-ini")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    a, _ = ap.parse_known_args()

    # one-shot admin helpers (run elevated by the main app)
    if a.fix_vcc:
        try: Path(a.fix_vcc).unlink()
        except Exception: pass
        return
    if a.fix_ini:
        ini_enable(a.fix_ini[0], int(a.fix_ini[1])); return
    if a.restore_ini:
        ini_restore(a.restore_ini); return
    if a.uninstall:
        try:   # stop the running copy first
            urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/api/action" % UI_PORT, data=b'{"do":"quit"}', method="POST",
                                   headers={"X-Pulse": "1", "Content-Type": "application/json"}), timeout=5).close()
        except Exception: pass
        set_startup(False)
        try: CFG.unlink()
        except Exception: pass
        return

    if already_running():
        if not a.background:
            if a.code:
                try:
                    urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:%d/api/pair" % UI_PORT, data=json.dumps({"code": a.code}).encode(),
                                           headers={"X-Pulse": "1", "Content-Type": "application/json"}, method="POST"), timeout=30).close()
                except Exception: pass
            urllib.request.urlopen("http://127.0.0.1:%d/focus" % UI_PORT, timeout=5).close()
        return
    if not a.background and install_self():
        return

    # engine works inside our private app folder
    engine.HERE = HOME; engine.CACHE = HOME / "cache"; engine.TEMPLATE = RES / "pulse_template.html"
    engine.OUTPUT = HOME / "dashboard.html"; engine.DATA_OUT = HOME / "data.json"; engine.CLOUD_CFG = HOME / "cloud_config.txt"
    engine.WA_SENT = engine.CACHE / "whatsapp_sent.txt"; engine.TOKEN_FILE = engine.CACHE / "phone_key.txt"
    engine.GOVT_WORDS = Connector.GENERIC_GOVT
    engine.TIMEOUT = 180

    C = Connector()
    if FROZEN and Path(sys.executable).resolve() == INSTALLED_EXE.resolve() and C.cfg.get("installed_version") != VERSION:
        C.cfg["installed_version"] = VERSION; save_cfg(C.cfg)
    if a.code and not C.cfg.get("push_key"): sys.argv.append("--code=" + a.code)
    C.write_cloud_cfg()
    log("Pulse Connect", VERSION, "starting", "(background)" if a.background else "")
    srv = Server(("127.0.0.1", UI_PORT), UI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    if WIN and FROZEN: set_startup(True)
    if not a.no_browser and (not a.background or (not C.cfg.get("push_key") and time.time() - C.cfg.get("ui_nag", 0) > 6 * 3600)):
        if a.background:
            C.cfg["ui_nag"] = time.time(); save_cfg(C.cfg)
        open_ui()
    def updater():
        while True:
            time.sleep(90); check_update(C); time.sleep(86400)
    threading.Thread(target=updater, daemon=True).start()
    C.loop()

if __name__ == "__main__":
    main()
