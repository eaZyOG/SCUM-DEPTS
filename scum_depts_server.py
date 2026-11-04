#!/usr/bin/env python3
"""
SCUM-DEPTS - SERVER (nur der Host)

Start:  py scum_depts_server.py
Startet auch deinen eigenen Client (Overlay + Makro bei dir) - abschaltbar mit START_CLIENT = False.
Router: TCP-Port 7780 an diesen PC weiterleiten (und in der Windows-Firewall erlauben).

Zahlungsplan: Frist 1 nach "Tage bis 1. Zahlung" Spieltagen (1 Spieltag = 8 Std echt). Wird die
Rate früher erreicht, zählt sofort die nächste Frist (Frist + "Danach +Tage", Rate + "Danach +Rate").
Verpasste Frist: Strafe, dann startet die Zeit neu bei "Danach +Tage" ab jetzt.
Dateien für die Freunde (Client, Setup, sounds/ images/ fonts/) verteilt der Server selbst über
denselben Port; dafür erzeugt er scum_depts_key.txt (Update-Schlüssel, geheim halten).
Ist der Server aus, stehen Timer und Spielzeit still und laufen beim nächsten Start weiter.
Die Spielzeit-Uhr muss einmal von dir gestellt werden und läuft danach von allein weiter.

Makro-Befehle für die Strafe (eine pro Zeile, # = Kommentar):
    key <taste> [ms]      Taste drücken, ms gehalten (Standard 50)
    wait <ms>             warten
    move <dx> <dy>        Maus relativ bewegen
    moveto p1|p2          Maus zu einem im Setup gesetzten Punkt (beim Spieler)
    click left|right      Mausklick
Tasten: a-z, 0-9, space, enter, tab, shift, ctrl, esc, up, down, left, right, backspace, f1-f12
"""
import base64, hashlib, hmac, json, math, secrets, socket, string, subprocess, sys, threading, time
import ctypes, os
import tkinter as tk
from tkinter import ttk, messagebox

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


_migrate("layout.json", "state.json", "key.txt")

PORT = 7780
START_CLIENT = True  # True: startet automatisch deinen eigenen Client (Overlay + Makro auch bei dir)
STATE_FILE = os.path.join(BASE, "scum_depts_state.json")
LAYOUT_FILE = os.path.join(BASE, "scum_depts_layout.json")
KEY_FILE = os.path.join(BASE, "scum_depts_key.txt")   # Update-Schlüssel (signiert die Dateien für die Freunde)

# ---- Datei-Verteilung an die Installer der Freunde (Auto-Update über denselben Port)
DIST_FILES = ("scum_depts_client.py", "scum_depts_setup.py")
DIST_DIRS = ("sounds", "images", "fonts")
ASSET_EXT = (".wav", ".png", ".gif", ".ttf", ".otf")
MAX_FILE = 25 * 1024 * 1024
CHUNK = 24576

ALLOWED_KEYS = (set(string.ascii_lowercase + string.digits) |
                {"space", "enter", "tab", "shift", "ctrl", "esc", "up", "down", "left",
                 "right", "backspace"} | {f"f{i}" for i in range(1, 13)})
# bewusst NICHT erlaubt: alt, win/cmd (kein Alt+Tab, Alt+F4, Windows-Tasten)

OLD_DEFAULT_MACROS = ("key space 100\nwait 400",
                      "key esc 100\nwait 500\nkey 1 100\nwait 500\nkey 2 100")
# ESC öffnet das Menü, dann Klick auf Punkt 1 und Punkt 2 (im Setup auf die Menü-Buttons legen)
DEFAULT_MACRO = ("key esc 100\nwait 800\nmoveto p1\nwait 300\nclick left\nwait 500\n"
                 "moveto p2\nwait 300\nclick left\n")



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


def save_layout_key(key, value):
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d[key] = value
    with open(LAYOUT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


# ------------------------------------------------------------ SERVER
SPEED = 3                       # Spielzeit läuft 3x so schnell
DAY_REAL = 86400 / SPEED        # echte Sekunden pro Spieltag (= 8 Stunden)


def virtual_screen(sw, sh):
    """(x, y, breite, höhe) des gesamten Desktops über alle Monitore (kann negativ beginnen)."""
    try:
        u = ctypes.windll.user32
        return (u.GetSystemMetrics(76), u.GetSystemMetrics(77),
                u.GetSystemMetrics(78), u.GetSystemMetrics(79))
    except Exception:
        return 0, 0, sw, sh


def set_icon(win):
    """Fenster-Symbol aus images/icon.png (statt der Python-Feder); gilt auch für Dialoge."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("scum.depts")
    except Exception:
        pass
    p = os.path.join(BASE, "images", "icon.png")
    try:
        if os.path.exists(p):
            img = tk.PhotoImage(file=p)
            win.iconphoto(True, img)
            win._icon_ref = img
    except Exception:
        pass



def hms(sec):
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def clock_txt(sec):
    if sec is None:
        return "--:--"
    sec = int(sec)
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}"


class Server:
    def __init__(self, port):
        self.port = port
        self.lock = threading.RLock()
        # Zahlungsplan: Frist D1 = Start + erste_Tage; jede erfüllte/verpasste Runde:
        #   Frist += step_days, Rate += step_amount.  Alle Fristen in echter Zeit (1 Tag = 8 Std).
        self.g = dict(debt=0, required=0, paid=0, next_due=0.0, step_days=2.0, step_amount=2500,
                      missed=0, total=0, macro=DEFAULT_MACRO, clock_game=None, clock_real=0.0)
        self.players = {}        # name -> {total, queued}
        self.conns, self.pending = {}, []
        self.next_id = 1
        self.auto_confirm = False
        self.host_name = None      # Name des lokalen (Host-)Clients, für die Debug-Funktion
        try:
            with open(KEY_FILE, encoding="utf-8") as f:
                self.key = f.read().strip()
        except Exception:
            self.key = ""
        if not self.key:                                   # beim ersten Start automatisch erzeugen
            self.key = secrets.token_hex(16)
            try:
                with open(KEY_FILE, "w", encoding="utf-8") as f:
                    f.write(self.key)
            except Exception:
                pass
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            if "required" in d["g"]:
                self.g.update(d["g"])
            else:                                  # alter Stand aus Version 1: nur Makro behalten
                self.g["macro"] = d["g"].get("macro", DEFAULT_MACRO)
            self.players = d["players"]
        except Exception:
            pass
        for p in self.players.values():            # "all" = Summe über alle Schulden, "total" = nur aktuelle Schuld
            p.setdefault("all", p.get("total", 0))
        if self.g["macro"].strip() in OLD_DEFAULT_MACROS:
            self.g["macro"] = DEFAULT_MACRO
        # War der Server aus, stehen Zahlungs-Timer UND Spielzeit still und laufen jetzt weiter
        now = time.time()
        alive = self.g.get("alive")
        if alive:
            down = max(0.0, now - alive)
            if self.g["next_due"] > 0:
                self.g["next_due"] += down
            if self.g["clock_game"] is not None:
                self.g["clock_real"] += down
            if down > 30:
                print(f"Server war {down / 60:.0f} Min aus - Timer und Spielzeit waren pausiert.")
        if not self.g.get("total"):
            self.g["total"] = self.g["debt"]
        self.g["alive"] = now
        self.beat = 0

    def save(self):
        self.g["alive"] = time.time()          # Lebenszeichen für die Pause beim nächsten Start
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({"g": self.g, "players": self.players}, f, indent=1)
        except Exception:
            pass

    # ---- Spielzeit (läuft von allein weiter, auch wenn der Server aus war)
    def game_secs(self, now=None):
        g = self.g
        if g["clock_game"] is None:
            return None
        now = time.time() if now is None else now
        return (g["clock_game"] + (now - g["clock_real"]) * SPEED) % 86400

    def set_clock(self, hh, mm):
        with self.lock:
            self.g["clock_game"] = hh * 3600 + mm * 60
            self.g["clock_real"] = time.time()      # beeinflusst die Zahlungs-Fristen NICHT
            self.save()

    def start(self):
        threading.Thread(target=self.accept_loop, daemon=True).start()
        threading.Thread(target=self.tick_loop, daemon=True).start()

    def accept_loop(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(20)
        while True:
            c, _ = srv.accept()
            threading.Thread(target=self.handle, args=(c,), daemon=True).start()

    def handle(self, conn):
        name = None
        try:
            conn.settimeout(10)
            it = lines(conn)
            hello = next(it)
            conn.settimeout(None)
            if hello.get("t") == "manifest":               # Installer/Updater eines Freundes
                self.serve_files(conn, it)
                return
            name = str(hello.get("name", "")).strip()[:20]
            if not name:
                send(conn, {"t": "error", "text": "Name fehlt."})
                return
            try:
                local = conn.getpeername()[0] in ("127.0.0.1", "::1")
            except Exception:
                local = False
            with self.lock:
                p = self.players.setdefault(name, dict(total=0, all=0, queued=False))
                if local:
                    self.host_name = name
                old = self.conns.get(name)
                self.conns[name] = conn
                if old:
                    try: old.close()
                    except Exception: pass
                if p["queued"]:
                    p["queued"] = False
                    send(conn, self.penalty_msg())
            self.push_state(name)
            for m in it:
                if m.get("t") == "pay":
                    self.request_payment(name, int(m.get("amount", 0)))
        except Exception:
            pass
        finally:
            with self.lock:
                if name and self.conns.get(name) is conn:
                    del self.conns[name]
            try: conn.close()
            except Exception: pass

    def safe_send(self, name, obj):
        c = self.conns.get(name)
        if not c:
            return False
        try:
            send(c, obj)
            return True
        except Exception:
            return False

    # ---- Datei-Verteilung (nur Whitelist: Client, Setup, sounds/ images/ fonts/)
    def build_manifest(self):
        rels = list(DIST_FILES)
        for d in DIST_DIRS:
            top = os.path.join(BASE, d)
            if os.path.isdir(top):
                for root_, _, names in os.walk(top):
                    for n in names:
                        rels.append(os.path.relpath(os.path.join(root_, n), BASE).replace("\\", "/"))
        files = {}
        for rel in rels:
            p = os.path.join(BASE, *rel.split("/"))
            ok_ext = rel.endswith(".py") if rel in DIST_FILES else rel.lower().endswith(ASSET_EXT)
            if ok_ext and os.path.isfile(p) and os.path.getsize(p) <= MAX_FILE:
                with open(p, "rb") as f:
                    data = f.read()
                files[rel] = dict(sha=hashlib.sha256(data).hexdigest(), size=len(data))
        body = json.dumps(files, sort_keys=True)
        sig = hmac.new(self.key.encode(), body.encode(), "sha256").hexdigest()
        return files, body, sig

    def serve_files(self, conn, it):
        files, body, sig = self.build_manifest()
        send(conn, {"t": "manifest", "files": body, "sig": sig})
        for m in it:
            rel = m.get("path")
            if m.get("t") != "get" or rel not in files:
                send(conn, {"t": "error", "text": "unbekannte Datei"})
                return
            with open(os.path.join(BASE, *rel.split("/")), "rb") as f:
                data = f.read()
            if hashlib.sha256(data).hexdigest() != files[rel]["sha"]:   # Datei hat sich gerade geändert
                send(conn, {"t": "error", "text": "Datei geändert, bitte neu starten"})
                return
            n = max(1, math.ceil(len(data) / CHUNK))
            for i in range(n):
                send(conn, {"t": "chunk", "path": rel, "i": i, "last": i == n - 1,
                            "data": base64.b64encode(data[i * CHUNK:(i + 1) * CHUNK]).decode()})

    def penalty_msg(self):
        return {"t": "penalty", "macro": self.g["macro"]}

    def push_state(self, name):
        with self.lock:
            g = self.g
            active = g["debt"] > 0 and g["next_due"] > 0
            self.safe_send(name, dict(
                t="state", active=active, debt=g["debt"], total=g["total"] or g["debt"],
                installment=min(g["required"], g["debt"]), paid=g["paid"],
                step_amount=g["step_amount"], missed=g["missed"],
                remaining=max(0.0, g["next_due"] - time.time()) if active else 0,
                clock=self.game_secs(), speed=SPEED,
                mine=self.players.get(name, {}).get("total", 0),
                pending=sum(x["amount"] for x in self.pending if x["name"] == name),
                macro=g["macro"]))

    def broadcast(self, text):
        for n in list(self.conns):
            self.safe_send(n, {"t": "msg", "text": text})

    def fire_penalty_all(self):
        with self.lock:
            for n, p in self.players.items():
                if not self.safe_send(n, self.penalty_msg()):
                    p["queued"] = True

    def settle(self):
        """Ist die aktuelle Rate erreicht, beginnt sofort die nächste (Frist + step_days)."""
        g, n = self.g, 0
        while g["debt"] > 0 and g["required"] > 0 and g["paid"] >= g["required"]:
            g["paid"] -= g["required"]
            g["next_due"] += g["step_days"] * DAY_REAL
            g["required"] += g["step_amount"]
            n += 1
        return n

    def tick_loop(self):
        while True:
            time.sleep(1)
            with self.lock:
                g, now = self.g, time.time()
                fired = False
                if g["debt"] > 0 and g["next_due"] > 0 and now >= g["next_due"]:
                    g["missed"] += 1                       # Rate nicht erreicht -> Strafe (einmal)
                    fired = True
                    g["next_due"] = now + g["step_days"] * DAY_REAL   # Zeit startet neu bei "Danach +Tage"
                    g["required"] += g["step_amount"]
                    self.settle()                           # übrig gebliebenes Guthaben zählt weiter
                if fired:
                    self.broadcast("Rate nicht bezahlt! Strafe wird ausgelöst.")
                    self.fire_penalty_all()
                    days = (g["next_due"] - now) / DAY_REAL
                    self.broadcast(f"Nächste Rate: {min(g['required'], g['debt'])} "
                                   f"in {days:.1f} Spieltagen.")
                    self.save()
                for n in list(self.conns):
                    self.push_state(n)
                self.beat += 1
                if self.beat % 5 == 0:
                    self.save()                # Lebenszeichen alle 5 Sekunden

    # ---- Zahlungen
    def request_payment(self, name, amount):
        if amount <= 0:
            return
        with self.lock:
            if self.auto_confirm:
                self.apply_payment(name, amount)
            else:
                self.pending.append(dict(id=self.next_id, name=name, amount=amount))
                self.next_id += 1
                self.safe_send(name, {"t": "msg", "text": f"Zahlung {amount} wartet auf Bestätigung."})

    def apply_payment(self, name, amount):
        g = self.g
        amount = min(amount, g["debt"])
        if amount <= 0:
            return
        g["debt"] -= amount
        g["paid"] += amount
        pl = self.players.setdefault(name, dict(total=0, all=0, queued=False))
        pl["total"] += amount
        pl["all"] = pl.get("all", 0) + amount
        if g["debt"] <= 0:
            g.update(debt=0, required=0, paid=0, next_due=0.0)
            self.broadcast(f"{name} zahlt {amount}. ALLE SCHULDEN GETILGT!")
        else:
            n = self.settle()
            txt = f"{name} zahlt {amount}. Offen: {g['debt']}."
            if n:
                days = (g["next_due"] - time.time()) / DAY_REAL
                txt += f" Rate erfüllt! Nächste: {min(g['required'], g['debt'])} in {days:.1f} Spieltagen."
            self.broadcast(txt)
        self.save()

    def resolve(self, pid, accept):
        with self.lock:
            for x in list(self.pending):
                if x["id"] == pid:
                    self.pending.remove(x)
                    if accept:
                        self.apply_payment(x["name"], x["amount"])
                    else:
                        self.safe_send(x["name"], {"t": "msg", "text": f"Zahlung {x['amount']} abgelehnt."})

    # ---- Host-Aktionen
    def set_terms(self, debt, first_days, first_amount, step_days, step_amount):
        with self.lock:
            first_days, step_days = max(0.01, first_days), max(0.01, step_days)
            self.g.update(debt=debt, total=debt, required=max(1, first_amount), paid=0,
                          step_days=step_days, step_amount=max(0, step_amount),
                          next_due=time.time() + first_days * DAY_REAL if debt > 0 else 0.0)
            for p in self.players.values():        # "Von dir gezahlt" beginnt mit der neuen Schuld bei 0
                p["total"] = 0
            self.save()

    def set_macro(self, text):
        parse_macro(text)           # wirft ValueError bei Fehlern
        with self.lock:
            self.g["macro"] = text
            self.save()

    def shift_time(self, seconds):
        with self.lock:
            if self.g["next_due"] > 0:
                self.g["next_due"] += seconds

    # ---- Debug (nur Host)
    def fire_penalty_host(self):
        """Löst das Makro nur beim eigenen (lokalen) Client aus. False = Host-Client nicht verbunden."""
        with self.lock:
            return bool(self.host_name) and self.safe_send(self.host_name, self.penalty_msg())

    def set_due_in(self, real_seconds):
        """Frist auf X echte Sekunden ab jetzt setzen (zum Testen von Countdown und Strafe)."""
        with self.lock:
            if self.g["debt"] > 0:
                self.g["next_due"] = time.time() + real_seconds
                self.save()

    def forgive(self):
        with self.lock:
            self.g.update(debt=0, required=0, paid=0, next_due=0.0)
            self.broadcast("Schulden erlassen.")
            self.save()


def admin_gui(srv):
    root = tk.Tk()
    root.title("SCUM-DEPTS - Host")
    set_icon(root)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    vx, vy, vw, vh = virtual_screen(sw, sh)
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            pos = json.load(f).get("admin")
        if pos:
            root.geometry(f"+{max(vx, min(vx + vw - 100, pos[0]))}+{max(vy, min(vy + vh - 100, pos[1]))}")
    except Exception:
        pass

    def on_close():
        save_layout_key("admin", [root.winfo_x(), root.winfo_y()])
        srv.save()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    info = ttk.Label(root, text="", font=("Segoe UI", 11, "bold"))
    info.grid(row=0, column=0, columnspan=6, padx=8, pady=6)

    # ---- Spielzeit
    cf = ttk.LabelFrame(root, text="Spielzeit (läuft 3x so schnell, 1 Tag = 8 Std echt)")
    cf.grid(row=1, column=0, columnspan=6, padx=6, pady=2, sticky="ew")
    clock_lbl = ttk.Label(cf, text="", font=("Segoe UI", 12, "bold"))
    clock_lbl.pack(side="left", padx=8)
    ttk.Label(cf, text="aktuelle Uhrzeit im Spiel (HH:MM):").pack(side="left", padx=4)
    clock_e = ttk.Entry(cf, width=7)
    clock_e.pack(side="left")

    def set_clock():
        try:
            hh, mm = clock_e.get().strip().split(":")
            hh, mm = int(hh), int(mm)
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError
            srv.set_clock(hh, mm)
        except ValueError:
            messagebox.showerror("Fehler", "Bitte Uhrzeit als HH:MM eingeben, z.B. 14:30")
    ttk.Button(cf, text="Uhrzeit setzen", command=set_clock).pack(side="left", padx=6)

    # ---- Zahlungsplan
    form = ttk.LabelFrame(root, text="Schulden & Zahlungsplan (Zeiten in SPIELTAGEN)")
    form.grid(row=2, column=0, columnspan=6, padx=6, sticky="ew")
    ents = {}
    for i, (lbl, dflt) in enumerate([("Schulden", "250000"), ("Tage bis 1. Zahlung", "5"),
                                     ("1. Rate", "5000"), ("Danach +Tage", "2"),
                                     ("Danach +Rate", "2500")]):
        ttk.Label(form, text=lbl).grid(row=0, column=i)
        e = ttk.Entry(form, width=14)
        e.insert(0, dflt)
        e.grid(row=1, column=i, padx=3, pady=3)
        ents[lbl] = e

    def apply_terms():
        try:
            srv.set_terms(int(ents["Schulden"].get()), float(ents["Tage bis 1. Zahlung"].get()),
                          int(ents["1. Rate"].get()), float(ents["Danach +Tage"].get()),
                          int(ents["Danach +Rate"].get()))
        except ValueError:
            messagebox.showerror("Fehler", "Ungültige Zahl.")

    bar = ttk.Frame(root)
    bar.grid(row=3, column=0, columnspan=6, pady=4)
    for txt, fn in [("Setzen (startet Plan)", apply_terms),
                    ("+5 Min echt", lambda: srv.shift_time(300)), ("-5 Min echt", lambda: srv.shift_time(-300)),
                    ("+1 Std echt", lambda: srv.shift_time(3600)), ("-1 Std echt", lambda: srv.shift_time(-3600)),
                    ("Erlassen", srv.forgive)]:
        ttk.Button(bar, text=txt, command=fn).pack(side="left", padx=2)

    # ---- Debug
    df = ttk.LabelFrame(root, text="Debug (nur Host)")
    df.grid(row=7, column=0, columnspan=6, padx=6, pady=4, sticky="ew")

    def debug_host():
        if not srv.fire_penalty_host():
            messagebox.showwarning("Debug", "Dein eigener Client ist nicht verbunden.")
    ttk.Button(df, text="Makro nur bei mir auslösen", command=debug_host).pack(side="left", padx=4, pady=4)
    ttk.Button(df, text="Strafe an alle", command=srv.fire_penalty_all).pack(side="left", padx=4)
    ttk.Button(df, text="Frist in 20 Sek. echt (Countdown testen)",
               command=lambda: srv.set_due_in(20)).pack(side="left", padx=4)

    mf = ttk.LabelFrame(root, text="Strafe = Makro (wird einmal ausgeführt)")
    mf.grid(row=4, column=0, columnspan=6, padx=6, pady=4, sticky="ew")
    mt = tk.Text(mf, height=7, width=60)
    mt.insert("1.0", srv.g["macro"])
    mt.pack(side="left", padx=4, pady=4)

    def save_macro():
        try:
            srv.set_macro(mt.get("1.0", "end"))
            messagebox.showinfo("OK", "Makro gespeichert.")
        except ValueError as e:
            messagebox.showerror("Makro-Fehler", str(e))
    ttk.Button(mf, text="Makro prüfen\n& speichern", command=save_macro).pack(padx=6)

    tree = ttk.Treeview(root, columns=("n", "o", "t", "a"), show="headings", height=5)
    for c, h in zip(("n", "o", "t", "a"), ("Spieler", "Online", "Diese Schuld", "Insgesamt")):
        tree.heading(c, text=h)
        tree.column(c, width=90, anchor="center")
    tree.grid(row=5, column=0, columnspan=3, padx=6, pady=4)

    pf = ttk.LabelFrame(root, text="Offene Zahlungen")
    pf.grid(row=5, column=3, columnspan=3, padx=6, pady=4, sticky="n")
    lb = tk.Listbox(pf, height=4, width=28)
    lb.pack(side="left", padx=4, pady=4)
    pids = []

    def resolve(acc):
        s = lb.curselection()
        if s:
            srv.resolve(pids[s[0]], acc)
    side = ttk.Frame(pf)
    side.pack(side="left")
    ttk.Button(side, text="Bestätigen", command=lambda: resolve(True)).pack(pady=2)
    ttk.Button(side, text="Ablehnen", command=lambda: resolve(False)).pack(pady=2)
    auto = tk.BooleanVar()
    ttk.Checkbutton(root, text="Zahlungen automatisch bestätigen", variable=auto,
                    command=lambda: setattr(srv, "auto_confirm", auto.get())
                    ).grid(row=6, column=0, columnspan=6, pady=4)

    def refresh():
        with srv.lock:
            g = srv.g
            gs = srv.game_secs()
            clock_lbl.config(text=f"Spielzeit: {clock_txt(gs)}" if gs is not None
                             else "Spielzeit: NICHT GESETZT ->")
            if g["next_due"] > 0 and g["debt"] > 0:
                rem = max(0, g["next_due"] - time.time())
                due = f"{rem / DAY_REAL:.2f} Spieltagen ({hms(rem * SPEED)} Spielzeit)"
            else:
                due = "-"
            info.config(text=f"Schulden: {g['debt']} | Rate: {g['paid']}/{min(g['required'], g['debt'])} "
                             f"(danach +{g['step_amount']}) | Frist in {due} | Verpasst: {g['missed']}")
            tree.delete(*tree.get_children())
            for n, p in srv.players.items():
                tree.insert("", "end", values=(n, "ja" if n in srv.conns else "-", p["total"], p.get("all", p["total"])))
            lb.delete(0, "end")
            pids.clear()
            for x in srv.pending:
                lb.insert("end", f"{x['name']}: {x['amount']}")
                pids.append(x["id"])
        root.after(1000, refresh)
    refresh()
    root.mainloop()


# ------------------------------------------------------------ MAIN
def start_local_client():
    """Startet scum_depts_client.py (Overlay + Makro für dich als Spieler) in eigenem Fenster."""
    path = os.path.join(BASE, "scum_depts_client.py")
    if not os.path.exists(path):
        print("Hinweis: scum_depts_client.py liegt nicht im Ordner - kein Overlay/Makro für dich.")
        return
    try:
        subprocess.Popen([sys.executable, path, "--host", "127.0.0.1"], cwd=BASE,
                         creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    except Exception as e:
        print("Client konnte nicht gestartet werden:", e)


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    srv = Server(PORT)
    srv.start()
    print(f"Server läuft auf TCP-Port {PORT}. Host-Fenster wird geöffnet.")
    if START_CLIENT:
        start_local_client()
    admin_gui(srv)


if __name__ == "__main__":
    main()