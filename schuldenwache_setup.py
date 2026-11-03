#!/usr/bin/env python3
"""
Schuldenwache - SETUP (jeder Spieler und der Host einmalig an seinem PC)

Start:  py schuldenwache_setup.py
Zieh die farbigen Fenster an die gewuenschte Stelle, Check-Farben aufnehmen, speichern.
Die Datei schuldenwache_layout.json landet im selben Ordner und wird von
schuldenwache_client.py bzw. schuldenwache_server.py benutzt.
Spiel dafuer im Fenster- oder Rahmenlos-Modus laufen lassen (nicht exklusives Vollbild).
"""
import json, time, ctypes, os
import tkinter as tk
from tkinter import ttk, messagebox

BASE = os.path.dirname(os.path.abspath(__file__))
LAYOUT_FILE = os.path.join(BASE, "schuldenwache_layout.json")
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
MARKERS = {   # key: (Beschriftung, Breite, Hoehe)
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
    """(x, y, breite, hoehe) des gesamten Desktops ueber alle Monitore (kann negativ beginnen)."""
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


def save_layout_key(key, value):
    try:
        with open(LAYOUT_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d[key] = value
    with open(LAYOUT_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)


def setup_ui():
    root = tk.Tk()
    root.title("Schuldenwache - Setup")
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    lay = load_layout(sw, sh)
    wins = {}

    def draggable(win, *widgets):
        def start(e):
            win._dx, win._dy = e.x_root - win.winfo_x(), e.y_root - win.winfo_y()

        def move(e):
            win.geometry(f"+{e.x_root - win._dx}+{e.y_root - win._dy}")
        for w in (win,) + widgets:
            w.bind("<Button-1>", start)
            w.bind("<B1-Motion>", move)

    colors = {"stats": "#806000", "countdown": "#802020", "banner": "#205080",
              "payment": "#205020", "check1": "#703070", "check2": "#703070",
              "check3": "#703070"}
    for k, (label, w, h) in MARKERS.items():
        t = tk.Toplevel(root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.attributes("-alpha", 0.85)
        t.geometry(f"{w}x{h}+{lay[k][0]}+{lay[k][1]}")
        if k in ("p1", "p2", "p3", "check1", "check2", "check3"):
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
        draggable(t, lb)
        wins[k] = t

    ttk.Label(root, justify="left", padding=10, text=(
        "Ziehe die farbigen Fenster an die gewuenschte Stelle\n"
        "(Spiel dafuer im gleichen Modus wie spaeter laufen lassen).\n"
        "Punkt 1-3: Mittelpunkt = Zielpunkt fuer 'moveto p1' im Makro.\n"
        "Bei Punkten und Checks zeigt der ROTE PUNKT im Fadenkreuz den genauen Pixel.\n"
        "Check 1-3 (lila): ueber Bildelemente legen, die NUR im normalen Spiel zu sehen\n"
        "sind (z.B. Lebensanzeige/Hotbar), nicht im Menue - dann 'Farben aufnehmen'.\n"
        "Nicht unter die Anzeige-Fenster legen!")).pack()

    CHECKS = ("check1", "check2", "check3")

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

    def capture():
        if not messagebox.askokcancel(
                "Farben aufnehmen",
                "Bring das Spiel in den NORMALEN Spielzustand (kein Menue).\n"
                "Nach OK wird in 3 Sekunden gemessen."):
            return
        pts = {k: (wins[k].winfo_x() + MARKERS[k][1] // 2, wins[k].winfo_y() + MARKERS[k][2] // 2)
               for k in CHECKS}

        def go():
            res = hidden_sample(pts)
            if any(v is None for v in res.values()):
                messagebox.showerror("Fehler", "Pixel nicht lesbar (ausserhalb des Bildschirms?).")
                return
            for k in CHECKS:
                save_layout_key(k, [wins[k].winfo_x(), wins[k].winfo_y()])
            save_layout_key("check_colors", {k: list(v) for k, v in res.items()})
            msg = "Gespeichert: " + ", ".join(f"{k}={v}" for k, v in res.items())
            if all(v == (0, 0, 0) for v in res.values()):
                msg += "\n\nAlles schwarz: Das Spiel ist wohl im exklusiven Vollbild und nicht lesbar. " \
                       "Nutze Rahmenlos/Fenster."
            messagebox.showinfo("Check-Farben", msg)
        root.after(3000, go)

    def test():
        lay2 = load_layout(sw, sh)
        cols = lay2.get("check_colors") or {}
        if not cols:
            messagebox.showinfo("Test", "Noch keine Farben aufgenommen.")
            return
        if not messagebox.askokcancel("Test", "Geh ins Spiel (oder oeffne ein Menue).\n"
                                              "Nach OK wird in 3 Sekunden geprueft."):
            return
        pts = {k: (lay2[k][0] + MARKERS[k][1] // 2, lay2[k][1] + MARKERS[k][2] // 2) for k in cols}

        def go():
            res = hidden_sample(pts)
            ok = [k for k in cols if pixel_match(res[k], cols[k])]
            inplay = len(ok) * 2 > len(cols)
            messagebox.showinfo("Test", f"{len(ok)}/{len(cols)} Punkte passen -> "
                                        f"{'IM SPIEL' if inplay else 'NICHT im Spiel / Menue offen'}")
        root.after(3000, go)

    def clear_checks():
        save_layout_key("check_colors", {})
        messagebox.showinfo("OK", "Check-Farben geloescht (Bereichs-Pruefung aus).")

    cb = ttk.Frame(root)
    cb.pack()
    ttk.Button(cb, text="Farben aufnehmen", command=capture).pack(side="left", padx=3)
    ttk.Button(cb, text="Testen", command=test).pack(side="left", padx=3)
    ttk.Button(cb, text="Checks loeschen", command=clear_checks).pack(side="left", padx=3)

    def save():
        for k, t in wins.items():
            if k not in CHECKS:          # Check-Positionen werden nur beim Aufnehmen gespeichert
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