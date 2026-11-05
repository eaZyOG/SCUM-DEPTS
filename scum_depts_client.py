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
  images/icon.png   Symbol: im Kopf der DEPT-Anzeige und als Fenster-Symbol
"""
import argparse, collections, json, math, socket, string, struct, sys, threading, time, ctypes, os
import traceback, types, wave
import tkinter as tk
from tkinter import ttk, messagebox
from tkinter import font as tkfont

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
MAX_SECS = 180                                  # Makro bricht spätestens nach 3 Min ab
PIX_TOL = 45                                    # erlaubte Farbabweichung pro Kanal (0-255)
DEATH_CONFIRM = 3.0                             # alle Tod-Pixel (3/3) müssen so lange (Sek.) durchgehend passen = Toleranz
INGAME_GAP = 0.3                                # Sek. ohne passende Check-Pixel, bevor das Overlay ausgeblendet wird
DEATH_GAP = 0.8                                 # so kurze Aussetzer (Sek.) unterbrechen die Erkennung nicht
DEATH_IGNORE = 180                              # Sek. nach dem Makro: der nächste Tod-Bildschirm gilt als Makro-Tod

# ---- Overlay-Design
ALPHA = 0.93                       # Gesamt-Transparenz des Overlays
PANEL_STIPPLE = "gray25"           # Hintergrund der Anzeigen: gray12 = fast unsichtbar ... gray50 = dichter
COUNTDOWN_GAME = 86400             # Countdown-Uhr ab 1 Spieltag Restzeit (Spielsekunden)
FINAL_GAME = 30                    # letzte halbe Spielminute wird sekundengenau abgezählt
HOUR_MARKS = (15, 9, 6, 3)         # Meldungen bei noch 15 / 9 / 6 / 3 Spielstunden (zusätzlich: jeder volle Tag)
SKIP_AFTER = 1.0                   # Meldung per ESC überspringen, wenn sie mind. so lange (Sek.) sichtbar war
SLIDE = 0.35                       # Ein-/Ausblend-Dauer der Meldungen
FONT_PREFERENCE = ["Bahnschrift", "Agency FB", "Segoe UI Semibold", "Segoe UI"]
SOUND_DIR = os.path.join(BASE, "sounds")      # eigene .wav hier ablegen ersetzen die Standardtöne
FONT_DIR = os.path.join(BASE, "fonts")        # optional: eigene .ttf/.otf
ICON_FILE = os.path.join(BASE, "images", "icon.png")   # optional: kleines Symbol (max ~48 px)
BG, PANEL, LINE, MUTED, TEXT = "#0f1217", "#171b22", "#2b313c", "#8a93a3", "#ffffff"
ACCENT, GOOD, WARN = "#ffb020", "#3ddc84", "#ff6b35"
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
            elif p[0] == "moveto" and p[1] in ("p1", "p2"):
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
    "check1": ("Check 1", 40, 40),
    "check2": ("Check 2", 40, 40),
    "check3": ("Check 3", 40, 40),
    "dead1": ("Tod 1", 40, 40),
    "dead2": ("Tod 2", 40, 40),
    "dead3": ("Tod 3", 40, 40),
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
         "p1": [sw // 2 - 100, sh // 2], "p2": [sw // 2 + 100, sh // 2],
         "check1": [sw // 2 - 100, sh - 150], "check2": [sw // 2, sh - 150],
         "check3": [sw // 2 + 100, sh - 150],
         "dead1": [sw // 2 - 100, sh - 250], "dead2": [sw // 2, sh - 250], "dead3": [sw // 2 + 100, sh - 250]}
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
    """Restzeit in Spielzeit-Einheiten (Tage/Stunden/Minuten/Sekunden)."""
    s = int(max(0, rem) * speed)
    d, r = divmod(s, 86400)
    h, m = r // 3600, r % 3600 // 60
    if d:
        return f"{d} Tg {h} Std"
    if h:
        return f"{h} Std {m} Min"
    return f"{m} Min {r % 60} Sek"


def day_text(n):
    return "Noch 1 Spieltag (Countdown startet)" if n == 1 else f"Noch {n} Spieltage"


def set_app_id():
    """Eigene Taskleisten-Gruppe, damit das Fenster-Symbol (icon.png) statt dem Python-Symbol erscheint."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("scum.depts")
    except Exception:
        pass


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
        self.last_rem, self.last_sec = None, None
        self.last_lbl, self.last_err = 0.0, None
        self.icon, self.win_icon, self.fam = None, None, "Segoe UI"
        self.cw = {}                                # Zellbreiten für feste Ziffern (pro Schriftgröße)
        self.visible, self.last_ok, self.last_chk, self.last_frame = True, 0.0, 0.0, None
        self.esc_flag = False
        self.busy = False
        self.saved = False
        # Tod-Erkennung
        self.dead_since, self.dead_last, self.dead_done, self.dead_skip = None, 0.0, False, False
        self.last_dead_chk = 0.0
        self.ignore_death, self.ignore_until = False, None      # Makro verursacht selbst einen Tod

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
            if low.startswith("neue schulden"):
                return
            if "gestorben" in low:
                lvl = "warn"
            elif "nicht bezahlt" in low or "strafe" in low:
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
        time.sleep(max(0.0, sec))

    def point(self, key):
        x, y = self.lay[key]
        _, w, h = MARKERS[key]
        return x + w // 2, y + h // 2

    def in_play(self):
        """Pixel-Check: True wenn ALLE Check-Punkte zum aufgenommenen Spielzustand passen."""
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
        return ok == len(cols)                         # alle Pixel müssen passen (3/3)

    # ---- Tod-Erkennung (Pixel-Check auf den "You are dead"-Bildschirm)
    def dead_match(self):
        """True, wenn ALLE Tod-Pixel passen; None = keine Tod-Farben im Setup aufgenommen."""
        cols = self.lay.get("dead_colors") or {}
        if not cols:
            return None
        ok = 0
        for k, ref in cols.items():
            try:
                if pixel_match(get_pixel(*self.point(k)), ref):
                    ok += 1
            except Exception:
                pass
        return ok == len(cols)                         # alle Pixel müssen passen (3/3)

    def ignoring_death(self, now):
        if self.busy:
            return True
        if self.ignore_death and self.ignore_until is not None and now > self.ignore_until:
            self.ignore_death = False                  # Makro hat nicht getötet: Schutz läuft ab
        return self.ignore_death

    def check_death(self, now):
        """Meldet einen Tod an den Server, wenn der Tod-Bildschirm DEATH_CONFIRM Sekunden durchgehend
        erkannt wird. Ein vom Makro verursachter Tod wird ignoriert (sonst doppelte Strafe)."""
        if now - self.last_dead_chk < 0.25:
            return
        self.last_dead_chk = now
        m = self.dead_match()
        if m is None:
            return
        if m and not game_in_front(self.a.game):
            m = False
        if m:
            self.dead_last = now
            if self.dead_since is None:                # neuer Tod-Bildschirm beginnt
                self.dead_since, self.dead_done = now, False
                self.dead_skip = self.ignoring_death(now)
            if not self.dead_done and now - self.dead_since >= DEATH_CONFIRM:
                self.dead_done = True
                if self.dead_skip or self.ignoring_death(now):
                    self.ignore_death = False          # Makro-Tod verbraucht, nicht melden
                    return
                try:
                    if self.sock:
                        send(self.sock, {"t": "death"})
                except Exception:
                    pass
        elif self.dead_since is not None and now - self.dead_last > DEATH_GAP:
            self.dead_since, self.dead_done = None, False   # Bildschirm weg (oder nur Fehlalarm)

    # ---- Strafe: Makro (einmalig; Whitelist, nur im Spiel, wird sofort ausgeführt)
    def run_macro(self, text):
        try:
            steps = parse_macro(text)
        except ValueError as e:
            self.banner(f"Makro ungültig: {e}", 8, "warn")
            return
        from pynput.keyboard import Controller as KC, Key
        from pynput.mouse import Controller as MC, Button
        self.busy = True
        self.ignore_death, self.ignore_until = True, None      # ein Tod während/nach dem Makro zählt nicht
        kb, ms = KC(), MC()
        K = lambda n: n if len(n) == 1 else getattr(Key, n)
        warned = [False]

        def in_game(deadline, check_pixels):
            while not (game_in_front(self.a.game) and (not check_pixels or self.in_play())):
                if not warned[0]:
                    warned[0] = True
                    self.banner("Strafe wartet (nicht im Spiel/Menü offen)", 4, "warn", now=True)
                if time.time() > deadline:
                    return False
                time.sleep(0.3)
            warned[0] = False
            return True
        try:
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
            self.busy = False
            self.ignore_until = time.monotonic() + DEATH_IGNORE
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

        self.build_window()
        self.start_keys()
        threading.Thread(target=self.net_loop, daemon=True).start()
        self.draw()
        root.mainloop()

    def start_keys(self):
        """Globaler ESC-Listener (das Overlay ist klickdurchlässig und hat nie den Fokus)."""
        try:
            from pynput.keyboard import Listener, Key
        except Exception:
            return

        def on_press(k):
            if k == Key.esc:
                self.esc_flag = True
        try:
            Listener(on_press=on_press).start()
        except Exception:
            pass

    def build_window(self):
        """Zahlungsfenster im Look der Anzeigen (dunkel, Akzentfarbe, ohne Makro-Anzeige)."""
        win = self.win = tk.Toplevel(self.root)
        win.title(f"DEPT - {self.a.name}")
        win.configure(bg=BG)
        win.resizable(False, False)
        px, py = self.lay["payment"]
        win.geometry(f"+{px}+{py}")
        if self.win_icon:
            try:
                win.iconphoto(True, self.win_icon)
            except Exception:
                pass
        F = lambda size, bold=False: (self.fam, size, "bold" if bold else "normal")
        pad = 16

        def sep():
            tk.Frame(win, height=1, bg=LINE).pack(fill="x", padx=pad, pady=8)

        head = tk.Frame(win, bg=BG)
        head.pack(fill="x", padx=pad, pady=(14, 0))
        if self.icon:
            tk.Label(head, image=self.icon, bg=BG).pack(side="left", padx=(0, 10))
        tb = tk.Frame(head, bg=BG)
        tb.pack(side="left")
        tk.Label(tb, text="DEPT", font=F(16, True), fg=TEXT, bg=BG).pack(anchor="w")
        tk.Label(tb, text=self.a.name, font=F(9), fg=MUTED, bg=BG).pack(anchor="w")
        self.pw_status = tk.Label(head, text="", font=F(9), fg=MUTED, bg=BG)
        self.pw_status.pack(side="right", anchor="n")
        sep()

        card = tk.Frame(win, bg=PANEL, highlightthickness=1, highlightbackground=LINE)
        card.pack(fill="x", padx=pad)
        tk.Label(card, text="FÄLLIG", font=F(8, True), fg=MUTED, bg=PANEL).pack(anchor="w", padx=12, pady=(10, 0))
        self.pw_due = tk.Label(card, text="-", font=F(26, True), fg=ACCENT, bg=PANEL)
        self.pw_due.pack(anchor="w", padx=12)
        self.pw_bar = tk.Canvas(card, height=8, width=300, bg="#232832", highlightthickness=0)
        self.pw_bar.pack(fill="x", padx=12, pady=(6, 3))
        self.pw_paid = tk.Label(card, text="", font=F(9), fg=MUTED, bg=PANEL)
        self.pw_paid.pack(anchor="w", padx=12)
        self.pw_frist = tk.Label(card, text="", font=F(10, True), fg=TEXT, bg=PANEL)
        self.pw_frist.pack(anchor="w", padx=12, pady=(2, 10))
        sep()

        rows = tk.Frame(win, bg=BG)
        rows.pack(fill="x", padx=pad)
        rows.columnconfigure(1, weight=1)
        self.pw = {}
        for i, (key, label) in enumerate((("debt", "Dept gesamt"), ("mine", "Von dir gezahlt"),
                                          ("missed", "Verpasst"), ("deaths", "Tode"), ("clock", "Spielzeit"))):
            tk.Label(rows, text=label, font=F(10), fg=MUTED, bg=BG).grid(row=i, column=0, sticky="w", pady=1)
            self.pw[key] = tk.Label(rows, text="-", font=F(10, True), fg=TEXT, bg=BG)
            self.pw[key].grid(row=i, column=1, sticky="e", pady=1)
        self.pw_pending = tk.Label(win, text="", font=F(9), fg="#ffcf5a", bg=BG)
        self.pw_pending.pack(anchor="w", padx=pad)
        sep()

        tk.Label(win, text="BETRAG ZAHLEN", font=F(8, True), fg=MUTED, bg=BG).pack(anchor="w", padx=pad)
        er = tk.Frame(win, bg=BG)
        er.pack(fill="x", padx=pad, pady=(4, 8))
        self.amount = tk.Entry(er, font=F(13, True), bg=PANEL, fg=TEXT, insertbackground=TEXT,
                               relief="flat", justify="right", highlightthickness=1,
                               highlightbackground=LINE, highlightcolor=ACCENT)
        self.amount.pack(side="left", fill="x", expand=True, ipady=6)
        self.amount.bind("<Return>", lambda e: self.pay())
        tk.Button(er, text="Fälliger Betrag", command=self.fill_due, font=F(9), bg=PANEL, fg=MUTED,
                  activebackground=LINE, activeforeground=TEXT, relief="flat", bd=0,
                  cursor="hand2", padx=8).pack(side="left", padx=(6, 0), fill="y")
        tk.Button(win, text="ZAHLEN", command=self.pay, font=F(11, True), bg=ACCENT, fg="#1a1205",
                  activebackground="#ffc654", activeforeground="#1a1205", relief="flat", bd=0,
                  cursor="hand2").pack(fill="x", padx=pad, ipady=7)
        self.stat = tk.Label(win, text="", font=F(9), fg=MUTED, bg=BG, wraplength=300, justify="left")
        self.stat.pack(anchor="w", padx=pad, pady=(6, 14))
        win.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def fill_due(self):
        st = self.state
        if st and st["active"]:
            self.amount.delete(0, "end")
            self.amount.insert(0, str(max(0, st["installment"] - st["paid"])))

    def pay(self):
        try:
            amt = int(self.amount.get().replace(".", "").replace(" ", ""))
            if amt <= 0 or not self.sock:
                raise ValueError
            send(self.sock, {"t": "pay", "amount": amt})
            self.stat.config(text=f"Zahlung {nfmt(amt)} gesendet.", fg=GOOD)
            self.amount.delete(0, "end")
        except Exception:
            self.stat.config(text="Ungültiger Betrag oder keine Verbindung.", fg=WARN)

    def rem_now(self, st, now):
        """Restzeit bis zur Frist in echten Sekunden; steht still, solange das Skript pausiert ist."""
        if st.get("paused"):
            return max(0.0, st["remaining"])
        return max(0.0, st["remaining"] - (now - self.recv_at))

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
        if st.get("clock_paused"):
            return st["clock"] % 86400
        return (st["clock"] + (time.monotonic() - self.recv_at + extra) * st["speed"]) % 86400

    # ---- Schrift, Töne, Symbol
    def setup_assets(self):
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
                self.win_icon = tk.PhotoImage(file=ICON_FILE)            # Fenster-Symbol (Titelleiste/Taskleiste)
                img = tk.PhotoImage(file=ICON_FILE)
                big = max(img.width(), img.height())
                if big > 32:
                    img = img.subsample(math.ceil(big / 32))               # Anzeige-Symbol max. ca. 32 px
                self.icon = img
        except Exception:
            self.icon, self.win_icon = None, None

    def font(self, size, bold=True):
        return (self.fam, int(round(size)), "bold" if bold else "normal")

    def txt(self, x, y, text, size, fill, anchor="nw", bold=True, shadow=1, width=None):
        kw = dict(anchor=anchor, font=self.font(size, bold))
        if width:
            kw["width"] = width
        if shadow:
            self.cv.create_text(x + shadow, y + shadow, text=text, fill="#050505", **kw)
        self.cv.create_text(x, y, text=text, fill=fill, **kw)

    def fixed_text(self, x, y, text, size, fill, anchor="nw", colon=True):
        """Zeichnet Ziffern in festen Zellen: nichts verschiebt sich, auch wenn der Doppelpunkt blinkt."""
        key = int(round(size))
        if key not in self.cw:
            f = tkfont.Font(root=self.root, font=self.font(key))
            self.cw[key] = (max(f.measure(d) for d in "0123456789"), f.measure(":"))
        dw, cw = self.cw[key]
        widths = [cw if ch == ":" else dw for ch in text]
        total = sum(widths)
        x0 = x - total if anchor in ("ne", "e", "se") else x - total / 2 if anchor in ("n", "center", "s") else x
        v = "n" if anchor in ("nw", "ne", "n") else "center"
        for ch, w in zip(text, widths):
            if ch != ":" or colon:
                self.txt(x0 + w / 2, y, ch, size, fill, anchor=v)
            x0 += w

    def panel(self, x1, y1, x2, y2):
        """Halbdurchsichtiger Hintergrund (Raster), damit die Sicht aufs Spiel frei bleibt."""
        self.cv.create_rectangle(x1, y1, x2, y2, fill=BG, stipple=PANEL_STIPPLE, outline=LINE)

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

    def update_visibility(self, now):
        """Pixel-Check alle 0.1 s: im Spiel -> Overlay sichtbar, sonst nach INGAME_GAP komplett ausgeblendet."""
        if now - self.last_chk < 0.1:
            return
        self.last_chk = now
        try:
            ok = self.in_play()
        except Exception:
            ok = True
        if ok:
            self.last_ok = now
        self.visible = ok or now - self.last_ok < INGAME_GAP     # kurze Aussetzer: nicht flackern

    def _draw(self):
        now, st = time.monotonic(), self.state
        dt = now - self.last_frame if self.last_frame else 0.0
        self.last_frame = now
        self.cv.delete("all")
        rem, open_ = None, 0
        if st and st["active"]:
            rem = self.rem_now(st, now)
            open_ = max(0, st["installment"] - st["paid"])
            self.check_reminders(st, rem, open_, now)
        else:
            self.last_rem = None
        self.update_visibility(now)
        self.check_death(now)
        if self.visible:
            self.draw_stats(st, rem, open_, now)
            if rem is not None and open_ > 0 and rem * st["speed"] <= COUNTDOWN_GAME:
                self.draw_countdown(rem, open_, now, st["speed"], bool(st.get("paused")))
            self.draw_banner(now)
        else:                                       # nicht im Spiel: nichts zeichnen, Meldungen warten
            self.esc_flag = False
            if self.cur_banner:
                self.cur_banner["t0"] += dt
        self.update_window(st, now)

    def check_reminders(self, st, rem, open_, now):
        """Meldungen in SPIELZEIT: jeder volle Tag, dann bei 15 / 9 / 6 / 3 Spielstunden."""
        lr, sp = self.last_rem, st["speed"]
        if lr is not None and rem <= lr + 5 and open_ > 0:      # sonst: erster Stand oder neue Runde
            g, lg = rem * sp, lr * sp
            for n in range(min(int(lg // 86400), 60), 0, -1):
                if lg > n * 86400 >= g:
                    self.banner(f"{day_text(n)} - fällig: {nfmt(open_)}", 7, "warn" if n == 1 else "info")
            for h in HOUR_MARKS:
                if lg > h * 3600 >= g:
                    self.banner(f"Noch {h} Spielstunden - fällig: {nfmt(open_)}", 7,
                                "alarm" if h <= 3 else "warn")
        self.last_rem = rem

    def frist_txt(self, st, rem):
        """Frist als Spielzeit: 'heute 20:00 Uhr', 'morgen 20:00 Uhr', 'in 3 Tagen, 20:00 Uhr'."""
        gc = self.game_clock(st)
        if gc is None:
            return None
        due = gc + rem * st["speed"]
        day, t = int(due // 86400), self.clock_txt(due % 86400)
        if day == 0:
            return f"heute {t} Uhr"
        return f"morgen {t} Uhr" if day == 1 else f"in {day} Tagen, {t} Uhr"

    def draw_stats(self, st, rem, open_, now):
        c = self.cv
        sx, sy = self.lay["stats"]
        W, H = MARKERS["stats"][1], MARKERS["stats"][2]
        pulse = 0.5 + 0.5 * math.sin(now * 3)
        gr = rem * st["speed"] if (st and rem is not None) else None     # Restzeit in Spielsekunden
        if gr is None:
            col = MUTED
        elif st.get("paused"):
            col = ACCENT
        elif gr <= 3 * 3600:
            col = mix("#ff6b35", "#ff2d2d", pulse)
        elif gr <= 86400:
            col = "#ff9f1a"
        else:
            col = "#ffcf5a"
        self.panel(sx, sy, sx + W, sy + H)
        c.create_rectangle(sx, sy, sx + W, sy + 4, fill=col, outline="")
        tx, ih = sx + 14, 0
        if self.icon:
            ih = self.icon.height()
            c.create_image(sx + 12, sy + 12, image=self.icon, anchor="nw")
            tx = sx + 12 + self.icon.width() + 8
        self.txt(tx, sy + 12 + (ih or 20) / 2, "DEPT", 13, "#c9d1de", anchor="w")
        top = 12 + max(ih, 32) + 8                  # Inhalt beginnt unterhalb des Symbols
        gc = self.game_clock(st) if st else None
        cpaused = bool(st and st.get("clock_paused"))
        self.txt(sx + W - 14, sy + 12, "UHRZEIT · PAUSE" if cpaused else "UHRZEIT", 8,
                 ACCENT if cpaused else MUTED, anchor="ne")
        if gc is None:
            self.txt(sx + W - 14, sy + 24, "--:--", 22, TEXT, anchor="ne")
        else:
            clock = f"{int(gc // 3600):02d}:{int(gc % 3600 // 60):02d}"
            self.fixed_text(sx + W - 14, sy + 24, clock, 22, TEXT, anchor="ne",
                            colon=cpaused or int(now) % 2 == 0)  # Doppelpunkt blinkt im Sekundentakt (Pause: aus)
        if st and st["active"]:
            self.txt(sx + 14, sy + top, "FÄLLIG", 9, MUTED)
            self.txt(sx + 14, sy + top + 12, nfmt(open_), 30, col)
            self.txt(sx + 14, sy + top + 54, "REST (SPIELZEIT)", 9, MUTED)
            self.txt(sx + 14, sy + top + 66, spiel_rest(rem, st["speed"]), 18, TEXT)
            fr = self.frist_txt(st, rem)
            if st.get("paused"):
                self.txt(sx + 14, sy + top + 94, "PAUSIERT - Countdown steht", 10, ACCENT)
            elif fr:
                self.txt(sx + 14, sy + top + 94, f"Frist: {fr}", 9, MUTED, bold=False)
        elif st:
            done = (st.get("total") or 0) > 0
            self.txt(sx + 14, sy + top + 10, "Alle Schulden getilgt" if done else "Keine offenen Schulden",
                     16, GOOD if done else MUTED)
        else:
            self.txt(sx + 14, sy + top + 10, self.status, 14, MUTED)
        if st:
            total = st.get("total") or st["debt"]
            repaid = max(0, total - st["debt"])
            ratio = repaid / total if total else 0
            c.create_rectangle(sx + 14, sy + H - 30, sx + W - 14, sy + H - 24, fill="#232832", outline="")
            if ratio > 0:
                c.create_rectangle(sx + 14, sy + H - 30, sx + 14 + (W - 28) * ratio, sy + H - 24,
                                   fill=GOOD, outline="")
            self.txt(sx + 14, sy + H - 20, f"getilgt {nfmt(repaid)} / {nfmt(total)}", 9, MUTED, bold=False)
            if st["pending"]:
                self.txt(sx + W - 14, sy + H - 20, f"{nfmt(st['pending'])} warten auf Bestätigung", 9,
                         "#ffcf5a", anchor="ne")

    def draw_countdown(self, rem, open_, now, speed, paused=False):
        """Countdown in Spielzeit: ab 1 Spieltag, die letzten 30 Spielsekunden werden einzeln abgezählt."""
        c = self.cv
        x0, y0 = self.lay["countdown"]
        W, H = MARKERS["countdown"][1], MARKERS["countdown"][2]
        cx, cy, R = x0 + W // 2, y0 + H // 2, 100
        gr = rem * speed
        frac = max(0.0, min(1.0, gr / COUNTDOWN_GAME))
        final = gr <= FINAL_GAME and not paused
        urgent = gr <= 180 and not paused                      # letzte 3 Spielminuten
        pulse = 0.5 + 0.5 * math.sin(now * (6 if final else 3 if urgent else 1.5))
        col = mix("#ffb020", "#ff2d2d", 1 - frac)
        if final:                                              # roter Rahmen um den ganzen Bildschirm
            c.create_rectangle(4, 4, self.W - 4, self.H - 4, width=8,
                               outline=mix("#400000", "#ff2020", pulse))
        sec = int(rem)                                         # Tick einmal pro echter Sekunde
        if sec != self.last_sec:
            self.last_sec = sec
            if final:
                self.play("tick")
        c.create_oval(cx - R - 16, cy - R - 16, cx + R + 16, cy + R + 16,
                      fill=BG, stipple=PANEL_STIPPLE, outline=LINE)
        c.create_arc(cx - R, cy - R, cx + R, cy + R, start=0, extent=359.9, style="arc",
                     outline="#232832", width=10)
        if frac > 0:
            c.create_arc(cx - R, cy - R, cx + R, cy + R, start=90, extent=max(1.0, 360 * frac), style="arc",
                         outline=col, width=10 + (5 * pulse if urgent else 0))
        dcol = mix("#ffffff", "#ff3b30", pulse) if urgent else TEXT
        if final:
            size = 54 * (1 + 0.2 * (rem % 1) ** 3)             # Herzschlag-Effekt pro echter Sekunde
            self.txt(cx, cy - 56, "SPIELSEKUNDEN", 9, MUTED, anchor="center")
            self.txt(cx, cy - 2, str(max(0, math.ceil(gr))), size, dcol, anchor="center")
        else:
            s = int(gr)
            self.txt(cx, cy - 56, "PAUSIERT" if paused else "BIS ZUR RATE (SPIELZEIT)", 8,
                     ACCENT if paused else MUTED, anchor="center")
            self.fixed_text(cx, cy - 4, f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}", 26, dcol,
                            anchor="center")
        self.txt(cx, cy + 46, f"FÄLLIG {nfmt(open_)}", 12, col, anchor="center")

    def draw_banner(self, now):
        cur = self.cur_banner
        if cur is not None and now - cur["t0"] > cur["secs"] + 2 * SLIDE:
            cur = self.cur_banner = None
        if cur is None and self.bq:
            cur = self.cur_banner = self.bq.popleft()
            cur["t0"] = now
            self.esc_flag = False
            if cur["sound"]:
                self.play(cur["sound"])
        if cur is None:
            self.esc_flag = False
            return
        t, total = now - cur["t0"], cur["secs"] + 2 * SLIDE
        skippable = SLIDE + SKIP_AFTER <= t < total - SLIDE        # mind. 1 s voll sichtbar, noch nicht am Ausblenden
        if self.esc_flag:
            self.esc_flag = False
            if skippable:                                          # ESC: sofort ausblenden
                cur["t0"] = now - (cur["secs"] + SLIDE)
                t, skippable = now - cur["t0"], False
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
        self.panel(bx, y, bx + w, y + h)
        c.create_rectangle(bx, y, bx + 8, y + h, fill=col, outline="")
        left = max(0.0, 1 - max(0.0, t - SLIDE) / max(0.1, cur["secs"]))
        c.create_rectangle(bx + 8, y + h - 3, bx + 8 + (w - 8) * left, y + h, fill=col, outline="")
        tw = w - 44
        if skippable:                                          # Skip-Hinweis: [ESC] >|
            tw = w - 130
            my, ix = y + h / 2, bx + w - 34
            c.create_rectangle(bx + w - 100, my - 10, bx + w - 56, my + 10, outline="#6b7385")
            self.txt(bx + w - 78, my, "ESC", 8, "#c9d1de", anchor="center")
            c.create_polygon(ix, my - 7, ix + 10, my, ix, my + 7, fill="#c9d1de", outline="")
            c.create_polygon(ix + 10, my - 7, ix + 20, my, ix + 10, my + 7, fill="#c9d1de", outline="")
            c.create_rectangle(ix + 21, my - 7, ix + 23, my + 7, fill="#c9d1de", outline="")
        self.txt(bx + 26, y + h / 2 - 1, cur["text"], 16, TEXT, anchor="w", width=tw)

    def update_window(self, st, now):
        if now - self.last_lbl < 0.5:
            return
        self.last_lbl = now
        ok = self.status == "Verbunden"
        self.pw_status.config(text=f"● {self.status}", fg=GOOD if ok else WARN)
        if not st:
            return
        gc = self.game_clock(st)
        self.pw["clock"].config(text=self.clock_txt(gc) if gc is not None else "--:--")
        self.pw["debt"].config(text=nfmt(st["debt"]))
        self.pw["mine"].config(text=nfmt(st["mine"]))
        self.pw["missed"].config(text=str(st["missed"]))
        self.pw["deaths"].config(text=str(st.get("deaths", 0)))
        self.pw_pending.config(text=f"{nfmt(st['pending'])} warten auf Bestätigung" if st["pending"] else "")
        ratio, w = 0.0, max(1, self.pw_bar.winfo_width())
        if st["active"]:
            rem = self.rem_now(st, now)
            open_ = max(0, st["installment"] - st["paid"])
            ratio = min(1.0, st["paid"] / st["installment"]) if st["installment"] else 0.0
            self.pw_due.config(text=nfmt(open_), fg=ACCENT)
            self.pw_paid.config(text=f"bezahlt {nfmt(st['paid'])} / {nfmt(st['installment'])}"
                                     f"   ·   danach +{nfmt(st['step_amount'])}")
            fr = self.frist_txt(st, rem)
            if st.get("paused"):
                self.pw_frist.config(text="PAUSIERT  ·  " + spiel_rest(rem, st["speed"]), fg=ACCENT)
            else:
                self.pw_frist.config(text=(f"Frist: {fr}  ·  " if fr else "Frist: ") + spiel_rest(rem, st["speed"]),
                                     fg=TEXT)
        else:
            done = (st.get("total") or 0) > 0
            self.pw_due.config(text="Getilgt" if done else "Keine Schulden", fg=GOOD if done else MUTED)
            self.pw_paid.config(text="")
            self.pw_frist.config(text="")
        self.pw_bar.delete("all")
        if ratio > 0:
            self.pw_bar.create_rectangle(0, 0, w * ratio, 8, fill=GOOD, outline="")


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
    set_app_id()
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
    print("Die Strafe läuft nur, wenn SCUM im Vordergrund ist.")

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