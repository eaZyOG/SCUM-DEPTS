#!/usr/bin/env python3
"""
SCUM-DEPTS - SETUP (jeder Spieler und der Host einmalig an seinem PC)

Start:  py scum_depts_setup.py
Zieh die farbigen Fenster an die gewünschte Stelle, Check-Farben aufnehmen, speichern.
Die Datei scum_depts_layout.json landet im selben Ordner und wird von
scum_depts_client.py bzw. scum_depts_server.py benutzt.
Spiel dafür im Fenster- oder Rahmenlos-Modus laufen lassen (nicht exklusives Vollbild).
"""
import json, time, ctypes, os
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


_migrate("layout.json", "client.json")

LAYOUT_FILE = os.path.join(BASE, "scum_depts_layout.json")
CONFIG_FILE = os.path.join(BASE, "scum_depts_client.json")   # Host-Adresse + Name (vom Client gelesen)
PIX_TOL = 45        # erlaubte Farbabweichung pro Kanal (0-255)


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


class Snapshot:
    """Bildschirmkopie (alle Monitore) im Speicher, damit die Lupe Pixel lesen kann, ohne dass die
    Setup-Fenster darüber liegen."""
    def __init__(self, vx, vy, vw, vh):
        self.u = ctypes.WinDLL("user32")
        self.g = ctypes.WinDLL("gdi32")
        u, g, vp = self.u, self.g, ctypes.c_void_p
        u.GetDC.argtypes, u.GetDC.restype = [vp], vp
        u.ReleaseDC.argtypes = [vp, vp]
        g.CreateCompatibleDC.argtypes, g.CreateCompatibleDC.restype = [vp], vp
        g.CreateCompatibleBitmap.argtypes, g.CreateCompatibleBitmap.restype = [vp, ctypes.c_int, ctypes.c_int], vp
        g.SelectObject.argtypes, g.SelectObject.restype = [vp, vp], vp
        g.BitBlt.argtypes = [vp, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             vp, ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
        g.GetPixel.argtypes, g.GetPixel.restype = [vp, ctypes.c_int, ctypes.c_int], ctypes.c_uint32
        g.DeleteObject.argtypes = [vp]
        g.DeleteDC.argtypes = [vp]
        self.vx, self.vy, self.vw, self.vh = vx, vy, vw, vh
        sdc = u.GetDC(None)
        self.dc = g.CreateCompatibleDC(sdc)
        self.bmp = g.CreateCompatibleBitmap(sdc, vw, vh)
        g.SelectObject(self.dc, self.bmp)
        g.BitBlt(self.dc, 0, 0, vw, vh, sdc, vx, vy, 0x40CC0020)     # SRCCOPY | CAPTUREBLT
        u.ReleaseDC(None, sdc)

    def pixel(self, x, y):
        x, y = x - self.vx, y - self.vy
        if not (0 <= x < self.vw and 0 <= y < self.vh):
            return None
        c = self.g.GetPixel(self.dc, x, y)
        if c == 0xFFFFFFFF:
            return None
        return (c & 255, (c >> 8) & 255, (c >> 16) & 255)

    def close(self):
        try:
            self.g.DeleteObject(self.bmp)
            self.g.DeleteDC(self.dc)
        except Exception:
            pass


LOUPE_N, LOUPE_Z = 11, 14                       # 11x11 Pixel, je 14x vergrößert
LOUPE_KEYS = ("p1", "p2", "check1", "check2", "check3", "dead1", "dead2", "dead3")


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


def save_layout_key(key, value):
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d[key] = value
    with open(LAYOUT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


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


def setup_ui():
    root = tk.Tk()
    root.title("SCUM-DEPTS - Setup")
    set_icon(root)
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    lay = load_layout(sw, sh)
    wins = {}

    # ---- Lupe: beim Ziehen von Punkten/Checks wird der Bereich um den Zielpixel vergrößert gezeigt
    loupe = {"top": None, "cv": None, "cells": [], "info": None, "snap": None}

    def loupe_end():
        if loupe["top"] is not None:
            loupe["top"].destroy()
        if loupe["snap"] is not None:
            loupe["snap"].close()
        loupe.update(top=None, cv=None, cells=[], info=None, snap=None)

    def loupe_begin():
        loupe_end()
        everything = [root] + list(wins.values())
        for w in everything:                       # kurz unsichtbar (bleiben aber klickbar), dann Bildschirmkopie
            w.attributes("-alpha", 0.0)
        root.update()
        time.sleep(0.15)
        try:
            vx, vy, vw, vh = virtual_screen(sw, sh)
            loupe["snap"] = Snapshot(vx, vy, vw, vh)
        except Exception:
            loupe["snap"] = None
        for w in wins.values():
            w.attributes("-alpha", 0.85)
        root.attributes("-alpha", 1.0)
        if loupe["snap"] is None:
            return
        n, z = LOUPE_N, LOUPE_Z
        t = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        c = tk.Canvas(t, width=n * z, height=n * z + 22, bg="#000000", highlightthickness=1,
                      highlightbackground="#ffffff")
        c.pack()
        loupe["cells"] = [c.create_rectangle(i * z, j * z, (i + 1) * z, (j + 1) * z,
                                             fill="#202020", outline="#303030")
                          for j in range(n) for i in range(n)]
        m = n // 2
        c.create_rectangle(m * z, m * z, (m + 1) * z, (m + 1) * z, outline="#ff0000", width=2)
        loupe["info"] = c.create_text(4, n * z + 11, anchor="w", fill="#ffffff", font=("Consolas", 9), text="")
        loupe["top"], loupe["cv"] = t, c

    def loupe_update(px, py, ex, ey):
        snap, c = loupe["snap"], loupe["cv"]
        if snap is None or c is None:
            return
        n, z = LOUPE_N, LOUPE_Z
        r = n // 2
        for j in range(n):
            for i in range(n):
                p = snap.pixel(px - r + i, py - r + j)
                c.itemconfig(loupe["cells"][j * n + i], fill="#%02x%02x%02x" % p if p else "#400040")
        mid = snap.pixel(px, py)
        c.itemconfig(loupe["info"], text=f"x={px} y={py}  RGB {mid[0]},{mid[1]},{mid[2]}" if mid
                     else f"x={px} y={py}  (nicht lesbar)")
        vx, vy, vw, vh = virtual_screen(sw, sh)
        lw, lh = n * z + 2, n * z + 24
        lx = ex + 40 if ex + 40 + lw < vx + vw else ex - 40 - lw      # Lupe neben dem Mauszeiger, nie darunter
        ly = ey + 40 if ey + 40 + lh < vy + vh else ey - 40 - lh
        loupe["top"].geometry(f"+{lx}+{ly}")

    def draggable(win, key, *widgets):
        use_loupe = key in LOUPE_KEYS

        def center(x, y):
            return x + MARKERS[key][1] // 2, y + MARKERS[key][2] // 2

        def start(e):
            win._dx, win._dy = e.x_root - win.winfo_x(), e.y_root - win.winfo_y()
            if use_loupe:
                loupe_begin()
                loupe_update(*center(win.winfo_x(), win.winfo_y()), e.x_root, e.y_root)

        def move(e):
            nx, ny = e.x_root - win._dx, e.y_root - win._dy
            win.geometry(f"+{nx}+{ny}")
            if use_loupe:
                loupe_update(*center(nx, ny), e.x_root, e.y_root)

        def end(e):
            if use_loupe:
                loupe_end()
        for w in (win,) + widgets:
            w.bind("<Button-1>", start)
            w.bind("<B1-Motion>", move)
            w.bind("<ButtonRelease-1>", end)

    colors = {"stats": "#806000", "countdown": "#802020", "banner": "#205080",
              "payment": "#205020", "check1": "#703070", "check2": "#703070",
              "check3": "#703070", "dead1": "#902020", "dead2": "#902020", "dead3": "#902020"}
    for k, (label, w, h) in MARKERS.items():
        t = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-alpha", 0.85)
        t.geometry(f"{w}x{h}+{lay[k][0]}+{lay[k][1]}")
        if k in ("p1", "p2", "check1", "check2", "check3", "dead1", "dead2", "dead3"):
            # Fadenkreuz: der rote Punkt in der Mitte ist der Pixel, der gemessen / angefahren wird
            lb = tk.Canvas(t, width=w, height=h, bg=colors.get(k, "#505050"),
                           highlightthickness=0, bd=0)
            lb.pack()
            cx, cy = w // 2, h // 2
            for x1, y1, x2, y2 in ((0, cy, cx - 4, cy), (cx + 5, cy, w, cy),
                                   (cx, 0, cx, cy - 4), (cx, cy + 5, cx, h)):
                lb.create_line(x1, y1, x2, y2, fill="#ffffff")
            lb.create_rectangle(cx, cy, cx + 1, cy + 1, outline="#ff0000", fill="#ff0000")
            lb.create_rectangle(cx - 3, cy - 3, cx + 4, cy + 4, outline="#ff0000")
            lb.create_text(3, 2, anchor="nw", text=label, fill="white", font=("Segoe UI", 7, "bold"))
        else:
            lb = tk.Label(t, text=f"{label}\n(ziehen)" if h > 50 else label,
                          bg=colors.get(k, "#505050"), fg="white", relief="ridge", bd=3,
                          font=("Segoe UI", 11, "bold"))
            lb.pack(fill="both", expand=True)
        draggable(t, k, lb)
        wins[k] = t

    ttk.Label(root, justify="left", padding=10, text=(
        "1. IP Adresse und Namen eintragen.\n"
        "2. SANDBOX betreten.\n"
        "3. Meldungen auf eine gewünschte Position verschieben, dass zeigt dir aktuelle Mitteilungen an. (Empfehlung oben am Rand)\n"
        "4. Countdown auf eine gewünschte Position verschieben, dass zeigt dir den Countdown an wenn die Zeit knapp wird. (Empfehlung oben am Rand unter Meldungen)\n"
        "5. Schulden-Anzeige auf eine gewünschte Position verschieben, dass zeigt dir den Countdown an wenn die Zeit knapp wird. (Empfehlung rechts oben etwas versetzt am Rand wegen Airdrop.)\n"
        "6. Zahlungsfenster auf den zweiten Bildschirm Positionieren, damit zahlst du die Schulden.\n"
        "7. Check 1-3 auf das Mikrofon-, Augen- und auf das HANDS-symbol legen (Weiß).\n"
        "8. Auf Farben aufnehmen drücken und danach einmal testen, es sollte 3/3 zeigen, im Menü oder Inventar sollte 0/3 erscheinen.\n"
        "9. Drücke ESC und lege Punkt 1 auf \"Commit Suicide\" und Punkt 2 auf Yes beim betätigen.\n"
        "10. Begehe Selbstmord und lege Tod 1-3 auf das Rote \"Prisoner Died\" links, rechts und mittig.\n"
        "11. Gehe auf Tod-Farben aufnehmen und teste es mit Tod testen, nach dem Respawn sollte es 0/3 anzeigen.")).pack()

    CHECKS = ("check1", "check2", "check3")
    DEADS = ("dead1", "dead2", "dead3")

    def hidden_sample(points):
        """Alle Setup-Fenster kurz verstecken und die Pixel darunter lesen."""
        root.withdraw()
        for t in wins.values():
            t.withdraw()
        root.update()
        time.sleep(0.5)
        res = {k: get_pixel(x, y) for k, (x, y) in points.items()}
        root.deiconify()
        for t in wins.values():
            t.deiconify()
            t.attributes("-topmost", True)
        return res

    def capture(keys, ckey, title, prompt):
        if not messagebox.askokcancel(title, prompt + "\nNach OK wird in 3 Sekunden gemessen."):
            return
        pts = {k: (wins[k].winfo_x() + MARKERS[k][1] // 2, wins[k].winfo_y() + MARKERS[k][2] // 2)
               for k in keys}

        def go():
            res = hidden_sample(pts)
            if any(v is None for v in res.values()):
                messagebox.showerror("Fehler", "Pixel nicht lesbar (außerhalb des Bildschirms?).")
                return
            for k in keys:
                save_layout_key(k, [wins[k].winfo_x(), wins[k].winfo_y()])
            save_layout_key(ckey, {k: list(v) for k, v in res.items()})
            msg = "Gespeichert: " + ", ".join(f"{k}={v}" for k, v in res.items())
            if all(v == (0, 0, 0) for v in res.values()):
                msg += "\n\nAlles schwarz: Das Spiel ist wohl im exklusiven Vollbild und nicht lesbar. " \
                       "Nutze Rahmenlos/Fenster."
            messagebox.showinfo(title, msg)
        root.after(3000, go)

    def test(ckey, prompt, yes, no):
        lay2 = load_layout(sw, sh)
        cols = lay2.get(ckey) or {}
        if not cols:
            messagebox.showinfo("Test", "Noch keine Farben aufgenommen.")
            return
        if not messagebox.askokcancel("Test", prompt + "\nNach OK wird in 3 Sekunden geprüft."):
            return
        pts = {k: (lay2[k][0] + MARKERS[k][1] // 2, lay2[k][1] + MARKERS[k][2] // 2) for k in cols}

        def go():
            res = hidden_sample(pts)
            ok = [k for k in cols if pixel_match(res[k], cols[k])]
            hit = len(ok) == len(cols)          # alle Pixel müssen passen
            messagebox.showinfo("Test", f"{len(ok)}/{len(cols)} Punkte passen -> {yes if hit else no}")
        root.after(3000, go)

    def clear_colors(ckey, text):
        save_layout_key(ckey, {})
        messagebox.showinfo("OK", text)

    def cap_checks():
        capture(CHECKS, "check_colors", "Check-Farben",
                "Bring das Spiel in den NORMALEN Spielzustand (kein Menü).")

    def cap_dead():
        capture(DEADS, "dead_colors", "Tod-Farben",
                "Bring den 'You are dead'-Bildschirm auf den Schirm\n"
                "(Tod 1-3 vorher auf rote Bildelemente darauf gelegt).")

    # ---- Verbindungsdaten (der Client hat kein Konsolenfenster mehr und kann nicht nachfragen)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    cf = ttk.LabelFrame(root, text="Verbindung zum Host", padding=8)
    cf.pack(fill="x", padx=10, pady=(0, 6))
    host_var = tk.StringVar(value=cfg.get("host", ""))
    name_var = tk.StringVar(value=cfg.get("name", ""))
    ttk.Label(cf, text="Adresse des Hosts (IP/DynDNS):").grid(row=0, column=0, sticky="w")
    ttk.Entry(cf, textvariable=host_var, width=30).grid(row=0, column=1, padx=6, pady=2)
    ttk.Label(cf, text="Dein Name:").grid(row=1, column=0, sticky="w")
    ttk.Entry(cf, textvariable=name_var, width=30).grid(row=1, column=1, padx=6, pady=2)

    cb = ttk.Frame(root)
    cb.pack()
    ttk.Button(cb, text="Farben aufnehmen", command=cap_checks).pack(side="left", padx=3)
    ttk.Button(cb, text="Testen", command=lambda: test(
        "check_colors", "Geh ins Spiel (oder öffne ein Menü).", "IM SPIEL", "NICHT im Spiel / Menü offen")
    ).pack(side="left", padx=3)
    ttk.Button(cb, text="Checks löschen", command=lambda: clear_colors(
        "check_colors", "Check-Farben gelöscht (Bereichs-Prüfung aus).")).pack(side="left", padx=3)
    cd = ttk.Frame(root)
    cd.pack(pady=(4, 0))
    ttk.Button(cd, text="Tod-Farben aufnehmen", command=cap_dead).pack(side="left", padx=3)
    ttk.Button(cd, text="Tod testen", command=lambda: test(
        "dead_colors", "Bring den Tod-Bildschirm (oder das normale Spiel) auf den Schirm.",
        "TOT erkannt", "nicht tot")).pack(side="left", padx=3)
    ttk.Button(cd, text="Tod löschen", command=lambda: clear_colors(
        "dead_colors", "Tod-Farben gelöscht (Tod-Erkennung aus).")).pack(side="left", padx=3)

    def save():
        host, name = host_var.get().strip(), name_var.get().strip()
        if not host or not name:
            messagebox.showwarning("Verbindung", "Bitte Adresse des Hosts und deinen Namen eintragen.")
            return
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(dict(host=host, name=name), f, indent=1)
        except Exception as e:
            messagebox.showerror("Fehler", f"Verbindungsdaten nicht speicherbar: {e}")
            return
        for k, t in wins.items():
            if k not in CHECKS + DEADS:  # Check-/Tod-Positionen werden nur beim Aufnehmen gespeichert
                save_layout_key(k, [t.winfo_x(), t.winfo_y()])
        messagebox.showinfo("Gespeichert", f"Positionen in {LAYOUT_FILE} gespeichert.")
        root.destroy()
    ttk.Button(root, text="Speichern & Beenden", command=save).pack(pady=8)
    root.mainloop()


def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # echte Pixel-Koordinaten
    except Exception:
        pass
    setup_ui()


if __name__ == "__main__":
    main()