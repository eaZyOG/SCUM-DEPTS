#!/usr/bin/env python3
"""
SCUM-DEPTS - CLIENT (für die Freunde)

Vorher einmal:  py -m pip install pynput   und   py scum_depts_setup.py
Start:          py scum_depts_client.py
Beim ersten Start fragt das Skript nach Host-Adresse und Name. Nach der
ersten erfolgreichen Verbindung wird alles in scum_depts_client.json gespeichert und
beim nächsten Start automatisch benutzt. Ändern: py scum_depts_client.py --neu
Spiel im Fenster- oder Rahmenlos-Modus laufen lassen (Overlay geht nicht im exklusiven Vollbild).

Optional im selben Ordner (nichts davon ist Pflicht):
  sounds/   eigene .wav-Dateien: notify, paid, warn, tick, alarm, penalty (ersetzen die Standardtöne)
  fonts/    eigene Schrift (.ttf/.otf) - wird automatisch fürs Overlay benutzt
  images/icon.png   kleines Symbol (max. ~48 px) im Kopf der Schulden-Anzeige
"""
import argparse, collections, json, math, socket, string, struct, sys, threading, time, ctypes, os
import traceback, types, wave
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import winsound
except ImportError:      # nicht Windows
    winsound = None

BASE = os.path.dirname(os.path.abspath(__file__))

def _migrate(*names):
    """Übernimmt Dateien aus der alten Version (Schuldenwache) unter dem neuen Namen."""
    for n in names:
        old, new = os.path.join(BASE, "schuldenwache_" + n), os.path.join(BASE, "scum_depts_" + n)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                os.replace(old, new)
            except OSError:
                pass


_migrate("layout.json", "client.json")


PORT = 7780
GAME = "scum"      # Prozess-/Fenstername des Spiels (SCUM.exe)
CONFIG_FILE = os.path.join(BASE, "scum_depts_client.json")

LAYOUT_FILE = os.path.join(BASE, "scum_depts_layout.json")
ABORT_HOTKEY = "<ctrl>+<shift>+<f12>"          # Notbremse
MAX_SECS = 180                                  # Makro bricht spätestens nach 3 Min ab
PIX_TOL = 45                                    # erlaubte Farbabweichung pro Kanal (0-255)

# ---- Overlay-Design
ALPHA = 0.93                       # Gesamt-Transparenz des Overlays
COUNTDOWN_FROM = 3600              # Countdown-Feld ab 1 Stunde (echte Zeit)
REMIND_EVERY = 1800                # zusätzliche Erinnerung alle 30 Min (echte Zeit)
THRESHOLDS = (86400, 57600, 28800, 14400, 7200, 3600, 1800, 600, 300, 120, 60)   # Meldungen (echte Sek.)
SLIDE = 0.35                       # Ein-/Ausblend-Dauer der Meldungen
FONT_PREFERENCE = ["Bahnschrift", "Agency FB", "Segoe UI Semibold", "Segoe UI"]
SOUND_DIR = os.path.join(BASE, "sounds")      # eigene .wav hier ablegen ersetzen die Standardtöne
FONT_DIR = os.path.join(BASE, "fonts")        # optional: eigene .ttf/.otf
ICON_FILE = os.path.join(BASE, "images", "icon.png")   # optional: kleines Symbol (max ~48 px)
LEVEL_COL = {"info": "#4aa8ff", "good": "#3ddc84", "warn": "#ffb020", "alarm": "#ff3b30"}
SOUND_FOR = {"info": "notify", "good": "paid", "warn": "warn", "alarm": "alarm"}

ALLOWED_KEYS = (set(string.ascii_lowercase + string.digits) |
                {"space", "enter", "tab", "shift", "ctrl", "esc", "up", "down", "left",
                 "right", "backspace"} | {f"f{i}" for i in range(1, 13)})
# bewusst NICHT erlaubt: alt, win/cmd (kein Alt+Tab, Alt+F4, Windows-Tasten)

DEFAULT_MACRO = "key space 100\nwait 400\n"


def send(sock, obj):
    sock.sendall((json.dumps(obj) + "\n").encode())


def lines(sock):
    buf = b""
    while True:
        data = sock.recv(4096)
        if not data:
            return
        buf += data
        if len(buf) > 65536:
            return
        while b"\n" in buf:
            l, buf = buf.split(b"\n", 1)
            if l.strip():
                yield json.loads(l)


def get_pixel(x, y):
    """Farbe (r,g,b) eines Bildschirmpixels (Windows/GDI) oder None."""
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    hdc = u.GetDC(0)
    c = g.GetPixel(hdc, int(x), int(y)) & 0xFFFFFFFF
    u.ReleaseDC(0, hdc)
    if c == 0xFFFFFFFF:
        return None
    return (c & 255, (c >> 8) & 255, (c >> 16) & 255)


def pixel_match(px, ref):
    return px is not None and all(abs(px[i] - ref[i]) <= PIX_TOL for i in range(3))


def parse_macro(text):
    """Prüft das Makro gegen die Whitelist. Wirft ValueError bei Fehlern."""
    steps = []
    for raw in text.splitlines():
        line = raw.split("#")[0].strip().lower()
        if not line:
            continue
        p = line.split()
        try:
            if p[0] == "key" and p[1] in ALLOWED_KEYS:
                steps.append(("key", p[1], min(10000, max(0, int(p[2]))) if len(p) > 2 else 50))
            elif p[0] == "wait":
                steps.append(("wait", min(10000, max(0, int(p[1])))))
            elif p[0] == "move":
                steps.append(("move", max(-3000, min(3000, int(p[1]))),
                              max(-3000, min(3000, int(p[2])))))
            elif p[0] == "moveto" and p[1] in ("p1", "p2", "p3"):
                steps.append(("moveto", p[1]))
            elif p[0] == "click" and p[1] in ("left", "right"):
                steps.append(("click", p[1]))
            else:
                raise ValueError
        except (ValueError, IndexError):
            raise ValueError(f"Ungültige Zeile: {raw.strip()}")
    return steps


# ------------------------------------------------------------ Layout / Setup
MARKERS = {   # key: (Beschriftung, Breite, Höhe)
    "stats": ("Schulden-Anzeige", 400, 200),
    "countdown": ("Countdown", 260, 260),
    "banner": ("Meldungen", 700, 64),
    "payment": ("Zahlungsfenster", 300, 240),
    "p1": ("Punkt 1", 110, 36),
    "p2": ("Punkt 2", 110, 36),
    "p3": ("Punkt 3", 110, 36),
    "check1": ("Check 1", 40, 40),
    "check2": ("Check 2", 40, 40),
    "check3": ("Check 3", 40, 40),
}


def virtual_screen(sw, sh):
    """(x, y, breite, höhe) des gesamten Desktops über alle Monitore (kann negativ beginnen)."""
    try:
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    except Exception:
        return 0, 0, sw, sh


def load_layout(sw, sh):
    d = {"stats": [sw - 420, 20], "countdown": [sw // 2 - 130, 150],
         "banner": [sw // 2 - 350, 60], "payment": [100, 100],
         "p1": [sw // 2 - 200, sh // 2], "p2": [sw // 2, sh // 2], "p3": [sw // 2 + 200, sh // 2],
         "check1": [sw // 2 - 100, sh - 150], "check2": [sw // 2, sh - 150],
         "check3": [sw // 2 + 100, sh - 150]}
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            d.update(json.load(f))
    except Exception:
        pass
    vx, vy, vw, vh = virtual_screen(sw, sh)   # alle Monitore, negative Koordinaten erlaubt
    for k in MARKERS:
        if k in d:
            d[k] = [max(vx, min(vx + vw - 40, int(d[k][0]))), max(vy, min(vy + vh - 40, int(d[k][1])))]
    return d


# ------------------------------------------------------------ CLIENT
def game_in_front(key):
    """True, wenn das Vordergrundfenster zum Spiel gehört (Prozessname, sonst Fenstertitel)."""
    key = key.lower()
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        h = u.GetForegroundWindow()
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
                                                 ctypes.POINTER(ctypes.c_ulong)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        pid = ctypes.c_ulong()
        u.GetWindowThreadProcessId(h, ctypes.byref(pid))
        hp = k.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if hp:
            buf = ctypes.create_unicode_buffer(520)
            size = ctypes.c_ulong(520)
            ok = k.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(size))
            k.CloseHandle(hp)
            if ok:
                return key in os.path.basename(buf.value).lower()
        n = u.GetWindowTextLengthW(h)                          # Fallback: Fenstertitel
        b = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, b, n + 1)
        return key in b.value.lower()
    except Exception:
        return False


def mix(c1, c2, t):
    """Farbmischung zwischen zwei #rrggbb-Farben (t = 0..1)."""
    t = max(0.0, min(1.0, t))
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def nfmt(x):
    return f"{int(x):,}".replace(",", ".")


def spiel_rest(rem, speed=3):
    """Restzeit in Spielzeit-Einheiten (Tage/Stunden/Minuten)."""
    s = int(max(0, rem) * speed)
    d, r = divmod(s, 86400)
    h, m = r // 3600, r % 3600 // 60
    return f"{d} Tg {h} Std" if d else f"{h} Std {m} Min"


def th_text(th, open_):
    if th >= 28800:
        n = th / 28800
        t = f"Noch {n:g} Spieltag{'e' if n != 1 else ''}"
    elif th >= 3600:
        t = f"Noch {th // 3600} Std echt" + (" - Countdown startet" if th == 3600 else "")
    else:
        t = f"Noch {th // 60} Min"
    return f"{t} - fällig: {nfmt(open_)}"


def make_sounds():
    """Erzeugt Standardtöne als .wav (nur wenn die Datei fehlt) - eigene Dateien einfach hineinlegen."""
    os.makedirs(SOUND_DIR, exist_ok=True)
    sr = 22050

    def wav(name, parts, vol=0.45):
        path = os.path.join(SOUND_DIR, name + ".wav")
        if os.path.exists(path):
            return
        frames = bytearray()
        for p in parts:
            freqs, dur = p[0], p[1]
            decay = p[2] if len(p) > 2 else 3.0
            n = int(sr * dur)
            for i in range(n):
                env = min(1.0, i / (sr * 0.006)) * math.exp(-decay * i / n)
                v = (sum(math.sin(2 * math.pi * f * i / sr) for f in freqs) / len(freqs)) if freqs[0] else 0.0
                frames += struct.pack("<h", int(32767 * vol * env * v))
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(bytes(frames))

    wav("notify", [((660,), .10), ((880,), .18)])
    wav("paid", [((523,), .09), ((659,), .09), ((784, 1047), .26)])
    wav("warn", [((880,), .12), ((0,), .05), ((880,), .12), ((0,), .05), ((880,), .16)])
    wav("tick", [((1800,), .035, 8.0)])
    wav("alarm", [((880,), .18, .5), ((620,), .18, .5)] * 3)
    wav("penalty", [((110, 165), .35, 1.0), ((82, 123), .6, 1.5)])


class Client:
    KEY = "#010101"

    def __init__(self, a):
        self.a = a
        self.state, self.recv_at = None, 0.0
        self.status = "Verbinde..."
        self.sock = None
        self.cur_banner, self.bq = None, collections.deque()
        self.last_rem, self.last_sec, self.last_remind = None, None, 0.0
        self.last_lbl, self.last_err = 0.0, None
        self.icon, self.fam = None, "Segoe UI"
        self.abort = threading.Event()
        self.busy = False
        self.saved = False

    def net_loop(self):
        while True:
            try:
                s = socket.create_connection((self.a.host, self.a.port), timeout=10)
                s.settimeout(None)
                self.sock = s
                send(s, {"t": "hello", "name": self.a.name})
                self.status = "Verbunden"
                for m in lines(s):
                    self.on_msg(m)
            except Exception:
                pass
            self.sock = None
            self.status = "Getrennt - neuer Versuch..."
            time.sleep(3)

    def on_msg(self, m):
        t = m.get("t")
        if t == "state":
            self.state, self.recv_at = m, time.monotonic()
            if not self.saved:
                self.saved = True
                self.a.save()
        elif t == "msg":
            low = m["text"].lower()
            if "nicht bezahlt" in low or "strafe" in low:
                lvl = "alarm"
            elif "zahlt" in low or "erfüllt" in low or "getilgt" in low or "erlassen" in low:
                lvl = "good"
            else:
                lvl = "info"
            self.banner(m["text"], 7, lvl)
        elif t == "error":
            self.banner(m["text"], 10, "warn")
        elif t == "penalty" and not self.busy:
            self.play("penalty")
            threading.Thread(target=self.run_macro, args=(m["macro"],),
                             daemon=True).start()

    def banner(self, text, secs=6, level="info", sound=True, now=False):
        """Meldung einblenden. now=True ersetzt die aktuelle Meldung sofort (z.B. Strafen-Countdown)."""
        item = dict(text=text, secs=secs, level=level, sound=SOUND_FOR.get(level) if sound else None)
        if now:
            keep = self.cur_banner is not None and self.cur_banner.get("now")
            item["now"] = True
            item["t0"] = time.monotonic() - (SLIDE if keep else 0)    # kein erneutes Einblenden
            if item["sound"] and not keep:
                self.play(item["sound"])
            self.cur_banner = item
        elif len(self.bq) < 6 and not (self.bq and self.bq[-1]["text"] == text):
            self.bq.append(item)

    def play(self, name):
        if winsound is None:
            return
        p = os.path.join(SOUND_DIR, name + ".wav")
        if os.path.exists(p):
            try:
                winsound.PlaySound(p, winsound.SND_FILENAME | winsound.SND_ASYNC)
            except Exception:
                pass

    def nap(self, sec):
        end = time.time() + sec
        while time.time() < end and not self.abort.is_set():
            time.sleep(min(0.05, max(0, end - time.time())))

    def point(self, key):
        x, y = self.lay[key]
        _, w, h = MARKERS[key]
        return x + w // 2, y + h // 2

    def in_play(self):
        """Pixel-Check: True wenn die Check-Punkte zum aufgenommenen Spielzustand passen."""
        cols = self.lay.get("check_colors") or {}
        if not cols:
            return True
        ok = 0
        for k, ref in cols.items():
            try:
                if pixel_match(get_pixel(*self.point(k)), ref):
                    ok += 1
            except Exception:
                pass
        return ok * 2 > len(cols)

    # ---- Strafe: Makro (einmalig; Whitelist, nur im Spiel, Notbremse)
    def run_macro(self, text):
        try:
            steps = parse_macro(text)
        except ValueError as e:
            self.banner(f"Makro ungültig: {e}", 8, "warn")
            return
        from pynput.keyboard import Controller as KC, Key, GlobalHotKeys
        from pynput.mouse import Controller as MC, Button
        self.busy = True
        self.abort.clear()
        hk = GlobalHotKeys({ABORT_HOTKEY: self.abort.set})
        hk.start()
        kb, ms = KC(), MC()
        K = lambda n: n if len(n) == 1 else getattr(Key, n)
        warned = [False]

        def in_game(deadline, check_pixels):
            while not (game_in_front(self.a.game) and (not check_pixels or self.in_play())):
                if not warned[0]:
                    warned[0] = True
                    self.banner("Strafe pausiert (nicht im Spiel/Menü offen)", 4, "warn", now=True)
                if self.abort.is_set() or time.time() > deadline:
                    return False
                time.sleep(0.3)
            warned[0] = False
            return not self.abort.is_set()
        try:
            for i in range(5, 0, -1):
                if self.abort.is_set():
                    return
                self.banner(f"STRAFE startet in {i}s  (Abbruch: Strg+Shift+F12)", 1.2, "alarm", sound=False, now=True)
                self.play("tick")
                time.sleep(1)
            deadline = time.time() + MAX_SECS
            for idx, st in enumerate(steps):
                # Pixel-Check nur vor dem 1. Schritt: danach ist z.B. nach ESC absichtlich das Menü offen
                if time.time() > deadline or not in_game(deadline, idx == 0):
                    return
                k = st[0]
                if k == "key":
                    kb.press(K(st[1]))
                    try: self.nap(st[2] / 1000)
                    finally: kb.release(K(st[1]))
                elif k == "wait":
                    self.nap(st[1] / 1000)
                elif k == "move":
                    ms.move(st[1], st[2])
                elif k == "moveto":
                    ms.position = self.point(st[1])
                elif k == "click":
                    ms.click(Button.left if st[1] == "left" else Button.right)
        finally:
            hk.stop()
            self.busy = False
            self.banner("Strafe beendet.", 4, "info")

    # ---- GUI
    def run(self):
        self.root = root = tk.Tk()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", self.KEY)
        root.attributes("-alpha", ALPHA)
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        self.lay = load_layout(sw, sh)
        root.geometry(f"{sw}x{sh}+0+0")
        self.cv = tk.Canvas(root, bg=self.KEY, highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        self.W, self.H = sw, sh
        self.setup_assets()
        root.update_idletasks()
        try:
            u = ctypes.windll.user32
            hwnd = u.GetParent(root.winfo_id()) or root.winfo_id()
            st = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, st | 0x80000 | 0x20 | 0x80)   # klickdurchlässig
        except Exception:
            pass

        win = tk.Toplevel(root)
        win.title(f"Schulden - {self.a.name}")
        px, py = self.lay["payment"]
        win.geometry(f"+{px}+{py}")
        self.lbl = ttk.Label(win, text="...", justify="left", font=("Segoe UI", 11))
        self.lbl.pack(padx=12, pady=8)
        ttk.Label(win, text="Betrag zahlen:").pack()
        self.amount = ttk.Entry(win, width=14)
        self.amount.pack(pady=4)
        ttk.Button(win, text="Zahlen", command=self.pay).pack(pady=6)
        self.stat = ttk.Label(win, text="")
        self.stat.pack(pady=4)
        self.macro_lbl = ttk.Label(win, text="", justify="left", foreground="#666")
        self.macro_lbl.pack(padx=8, pady=4)
        win.protocol("WM_DELETE_WINDOW", root.destroy)

        threading.Thread(target=self.net_loop, daemon=True).start()
        self.draw()
        root.mainloop()

    def pay(self):
        try:
            amt = int(self.amount.get())
            if amt <= 0 or not self.sock:
                raise ValueError
            send(self.sock, {"t": "pay", "amount": amt})
            self.stat.config(text=f"Zahlung {amt} gesendet.")
        except Exception:
            self.stat.config(text="Ungültiger Betrag oder keine Verbindung.")

    @staticmethod
    def fmt(sec):
        sec = int(max(0, sec))
        h, r = divmod(sec, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @staticmethod
    def clock_txt(sec):
        sec = int(sec)
        return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"

    def game_clock(self, st, extra=0.0):
        """Spielzeit (Sekunden des Tages), vom Server-Stand hochgerechnet; None = nicht gesetzt."""
        if st.get("clock") is None:
            return None
        return (st["clock"] + (time.monotonic() - self.recv_at + extra) * st["speed"]) % 86400

    # ---- Schrift, Töne, Symbol
    def setup_assets(self):
        from tkinter import font as tkfont
        before = set(tkfont.families(self.root))
        try:
            if os.path.isdir(FONT_DIR):
                for fn in os.listdir(FONT_DIR):
                    if fn.lower().endswith((".ttf", ".otf")):
                        ctypes.windll.gdi32.AddFontResourceExW(os.path.join(FONT_DIR, fn), 0x10, 0)
        except Exception:
            pass
        fams = set(tkfont.families(self.root))
        new = sorted(fams - before)            # eigene Schrift aus fonts/ hat Vorrang
        self.fam = new[0] if new else next((f for f in FONT_PREFERENCE if f in fams), "Segoe UI")
        try:
            make_sounds()
        except Exception:
            pass
        try:
            if os.path.exists(ICON_FILE):
                img = tk.PhotoImage(file=ICON_FILE)
                if img.width() > 48:
                    img = img.subsample(max(1, img.width() // 48))
                self.icon = img
        except Exception:
            self.icon = None

    def font(self, size, bold=True):
        return (self.fam, int(round(size)), "bold" if bold else "normal")

    def txt(self, x, y, text, size, fill, anchor="nw", bold=True, shadow=2, width=None):
        kw = dict(anchor=anchor, font=self.font(size, bold))
        if width:
            kw["width"] = width
        if shadow:
            self.cv.create_text(x + shadow, y + shadow, text=text, fill="#050505", **kw)
        self.cv.create_text(x, y, text=text, fill=fill, **kw)

    # ---- Zeichnen
    def draw(self):
        try:
            self._draw()
        except Exception:
            msg = traceback.format_exc()
            if msg != self.last_err:        # Fehler nur einmal ausgeben
                self.last_err = msg
                print(msg)
        self.root.after(33, self.draw)      # ~30 Bilder/s für die Animationen

    def _draw(self):
        now, st = time.monotonic(), self.state
        self.cv.delete("all")
        rem, open_ = None, 0
        if st and st["active"]:
            rem = max(0.0, st["remaining"] - (now - self.recv_at))
            open_ = max(0, st["installment"] - st["paid"])
            self.check_reminders(st, rem, open_, now)
        else:
            self.last_rem = None
        self.draw_stats(st, rem, open_, now)
        if rem is not None and open_ > 0 and rem <= COUNTDOWN_FROM:
            self.draw_countdown(rem, open_, now)
        self.draw_banner(now)
        self.update_window(st, now)

    def check_reminders(self, st, rem, open_, now):
        lr = self.last_rem
        if lr is None or rem > lr + 5:                  # erster Stand oder neue Runde
            self.last_remind = now
        elif open_ > 0:
            for th in THRESHOLDS:
                if lr > th >= rem:
                    lvl = "info" if th >= 14400 else "warn" if th >= 3600 else "alarm"
                    self.banner(th_text(th, open_), 7, lvl)
            if rem > COUNTDOWN_FROM and now - self.last_remind >= REMIND_EVERY:
                self.last_remind = now
                self.banner(f"Fällig: {nfmt(open_)} - noch {spiel_rest(rem, st['speed'])} (Spielzeit)", 6, "info")
        self.last_rem = rem

    def draw_stats(self, st, rem, open_, now):
        c = self.cv
        sx, sy = self.lay["stats"]
        W, H = MARKERS["stats"][1], MARKERS["stats"][2]
        pulse = 0.5 + 0.5 * math.sin(now * 3)
        if rem is None:
            col = "#8a93a3"
        elif rem <= 3600:
            col = mix("#ff6b35", "#ff2d2d", pulse)
        elif rem <= 28800:
            col = "#ff9f1a"
        else:
            col = "#ffcf5a"
        c.create_rectangle(sx + 3, sy + 3, sx + W + 3, sy + H + 3, fill="#050505", outline="")
        c.create_rectangle(sx, sy, sx + W, sy + H, fill="#0f1217", outline="#2b313c")
        c.create_rectangle(sx, sy, sx + W, sy + 4, fill=col, outline="")
        tx = sx + 14
        if self.icon:
            c.create_image(sx + 12, sy + 12, image=self.icon, anchor="nw")
            tx = sx + 12 + self.icon.width() + 8
        self.txt(tx, sy + 14, "SCHULDEN", 11, "#8a93a3", shadow=0)
        gc = self.game_clock(st) if st else None
        if gc is None:
            clock = "--:--"
        else:
            clock = f"{int(gc // 3600):02d}{':' if int(now) % 2 == 0 else ' '}{int(gc % 3600 // 60):02d}"
        self.txt(sx + W - 14, sy + 12, "UHRZEIT", 8, "#8a93a3", anchor="ne", shadow=0)
        self.txt(sx + W - 14, sy + 24, clock, 22, "#ffffff", anchor="ne", shadow=0)
        if st and st["active"]:
            self.txt(sx + 14, sy + 48, "FÄLLIG", 9, "#8a93a3", shadow=0)
            self.txt(sx + 14, sy + 60, nfmt(open_), 30, col, shadow=0)
            self.txt(sx + 14, sy + 108, "REST (SPIELZEIT)", 9, "#8a93a3", shadow=0)
            self.txt(sx + 14, sy + 120, spiel_rest(rem, st["speed"]), 18, "#ffffff", shadow=0)
            dc = self.game_clock(st, rem)
            small = f"{self.fmt(rem)} echt" + (f"  ·  Frist {self.clock_txt(dc)} Uhr" if dc is not None else "")
            self.txt(sx + 14, sy + 148, small, 9, "#8a93a3", bold=False, shadow=0)
        elif st:
            done = (st.get("total") or 0) > 0
            self.txt(sx + 14, sy + 70, "Alle Schulden getilgt" if done else "Keine offenen Schulden",
                     16, "#3ddc84" if done else "#8a93a3", shadow=0)
        else:
            self.txt(sx + 14, sy + 70, self.status, 14, "#8a93a3", shadow=0)
        if st:
            total = st.get("total") or st["debt"]
            repaid = max(0, total - st["debt"])
            ratio = repaid / total if total else 0
            c.create_rectangle(sx + 14, sy + H - 30, sx + W - 14, sy + H - 24, fill="#232832", outline="")
            if ratio > 0:
                c.create_rectangle(sx + 14, sy + H - 30, sx + 14 + (W - 28) * ratio, sy + H - 24,
                                   fill="#3ddc84", outline="")
            self.txt(sx + 14, sy + H - 20, f"getilgt {nfmt(repaid)} / {nfmt(total)}", 9, "#8a93a3",
                     bold=False, shadow=0)
            if st["pending"]:
                self.txt(sx + W - 14, sy + H - 20, f"{nfmt(st['pending'])} warten auf Bestätigung", 9,
                         "#ffcf5a", anchor="ne", shadow=0)

    def draw_countdown(self, rem, open_, now):
        c = self.cv
        x0, y0 = self.lay["countdown"]
        W, H = MARKERS["countdown"][1], MARKERS["countdown"][2]
        cx, cy, R = x0 + W // 2, y0 + H // 2, 100
        frac = max(0.0, min(1.0, rem / COUNTDOWN_FROM))
        urgent = rem <= 60
        speed = 6 if rem <= 10 else 3 if urgent else 1.5
        pulse = 0.5 + 0.5 * math.sin(now * speed)
        col = mix("#ffb020", "#ff2d2d", 1 - frac)
        if rem <= 10:                                          # roter Rahmen um den ganzen Bildschirm
            c.create_rectangle(4, 4, self.W - 4, self.H - 4, width=8,
                               outline=mix("#400000", "#ff2020", pulse))
        sec = int(rem)
        if sec != self.last_sec:
            self.last_sec = sec
            if sec <= 10:
                self.play("tick")
        c.create_oval(cx - R - 16, cy - R - 16, cx + R + 16, cy + R + 16, fill="#0f1217", outline="#2b313c")
        c.create_arc(cx - R, cy - R, cx + R, cy + R, start=0, extent=359.9, style="arc",
                     outline="#232832", width=10)
        if frac > 0:
            c.create_arc(cx - R, cy - R, cx + R, cy + R, start=90, extent=max(1.0, 360 * frac), style="arc",
                         outline=col, width=10 + (5 * pulse if urgent else 0))
        size = 40
        if rem <= 10:                                          # Herzschlag-Effekt pro Sekunde
            size = 40 * (1 + 0.25 * (rem % 1) ** 3)
        dcol = mix("#ffffff", "#ff3b30", pulse) if urgent else "#ffffff"
        self.txt(cx, cy - 56, "BIS ZUR RATE", 9, "#8a93a3", anchor="center", shadow=0)
        self.txt(cx, cy - 4, f"{sec // 60:02d}:{sec % 60:02d}", size, dcol, anchor="center", shadow=0)
        self.txt(cx, cy + 46, f"FÄLLIG {nfmt(open_)}", 12, col, anchor="center", shadow=0)

    def draw_banner(self, now):
        cur = self.cur_banner
        if cur is not None and now - cur["t0"] > cur["secs"] + 2 * SLIDE:
            cur = self.cur_banner = None
        if cur is None and self.bq:
            cur = self.cur_banner = self.bq.popleft()
            cur["t0"] = now
            if cur["sound"]:
                self.play(cur["sound"])
        if cur is None:
            return
        t, total = now - cur["t0"], cur["secs"] + 2 * SLIDE
        if t < SLIDE:
            p = 1 - (1 - t / SLIDE) ** 3
        elif t > total - SLIDE:
            p = 1 - ((t - (total - SLIDE)) / SLIDE) ** 3
        else:
            p = 1.0
        bx, by = self.lay["banner"]
        w, h = MARKERS["banner"][1], MARKERS["banner"][2]
        y = by - (1 - p) * (by + h + 20)                       # gleitet von oben herein
        col = LEVEL_COL.get(cur["level"], "#4aa8ff")
        if cur["level"] == "alarm":
            col = mix(col, "#ffd0cc", 0.5 + 0.5 * math.sin(now * 10))
        c = self.cv
        c.create_rectangle(bx + 4, y + 4, bx + w + 4, y + h + 4, fill="#050505", outline="")
        c.create_rectangle(bx, y, bx + w, y + h, fill="#12151b", outline="#2b313c")
        c.create_rectangle(bx, y, bx + 8, y + h, fill=col, outline="")
        left = max(0.0, 1 - max(0.0, t - SLIDE) / max(0.1, cur["secs"]))
        c.create_rectangle(bx + 8, y + h - 3, bx + 8 + (w - 8) * left, y + h, fill=col, outline="")
        self.txt(bx + 26, y + h / 2 - 1, cur["text"], 16, "#ffffff", anchor="w", shadow=0, width=w - 44)

    def update_window(self, st, now):
        if not st or now - self.last_lbl < 0.5:
            return
        self.last_lbl = now
        gc = self.game_clock(st)
        clock = f"Spielzeit: {self.clock_txt(gc) if gc is not None else '--:--'}"
        open_ = max(0, st["installment"] - st["paid"])
        self.lbl.config(text=f"Schulden (alle): {nfmt(st['debt'])}\n"
                             f"Fällig: {nfmt(open_)}  ({nfmt(st['paid'])}/{nfmt(st['installment'])}, "
                             f"danach +{nfmt(st['step_amount'])})\n"
                             f"Von dir gezahlt: {nfmt(st['mine'])}\nVerpasst: {st['missed']}\n"
                             f"{clock}\nVerbindung: {self.status}")
        self.macro_lbl.config(text=f"Strafe (einmalig):\n{st['macro'].strip()}")


# ------------------------------------------------------------ MAIN
def load_cfg():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def ask(prompt):
    """Konsole, falls vorhanden - sonst ein kleines Eingabefenster (Start ohne cmd-Fenster)."""
    try:
        if sys.stdin is None:
            raise RuntimeError("keine Konsole")
        return input(prompt + ": ").strip()
    except Exception:
        from tkinter import simpledialog
        r = tk.Tk()
        r.withdraw()
        v = simpledialog.askstring("SCUM-DEPTS", prompt + ":", parent=r)
        r.destroy()
        return (v or "").strip()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # echte Pixel-Koordinaten
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", help="Adresse des Hosts (wird gespeichert)")
    ap.add_argument("--neu", action="store_true", help="gespeicherte Einstellungen verwerfen")
    args = ap.parse_args()

    cfg = {} if args.neu else load_cfg()
    if args.host:
        cfg["host"] = args.host
    host = cfg.get("host") or ask("Adresse des Hosts (IP/DynDNS)")
    name = cfg.get("name") or ask("Dein Name")
    if not (host and name):
        try:
            messagebox.showerror("SCUM-DEPTS", "Host-Adresse und Name werden benötigt.\n"
                                               "Trage sie im Setup ein (scum_depts_setup.py).")
        except Exception:
            print("Host und Name werden benötigt.")
        return
    print(f"Verbinde als {name} mit {host}:{PORT}   (Änderungen: --neu)")
    print("Die Strafe läuft nur, wenn SCUM im Vordergrund ist. Notbremse: Strg+Shift+F12")

    a = types.SimpleNamespace(host=host, port=PORT, name=name, game=GAME)

    def save():   # nach der ersten erfolgreichen Verbindung
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(dict(host=host, name=name), f, indent=1)
        except Exception:
            pass
    a.save = save
    Client(a).run()


if __name__ == "__main__":
    main()