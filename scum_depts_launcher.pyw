"""
SCUM-DEPTS - LAUNCHER (wird von der Desktop-Verknüpfung gestartet, ohne Konsolenfenster)
1. still nach Updates suchen (offline = ignorieren)
2. noch kein Layout vorhanden -> Setup starten und warten, bis es beendet ist
3. Client starten
"""
import os, sys, json, hashlib, shutil, subprocess, urllib.request, urllib.parse

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


_migrate("layout.json", "config.json")

LAYOUT = os.path.join(BASE, "scum_depts_layout.json")
NOWIN = 0x08000000   # CREATE_NO_WINDOW


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "SCUM-DEPTSLauncher"})
    return urllib.request.urlopen(req, timeout=10)


def update():
    with open(os.path.join(BASE, "scum_depts_config.json"), encoding="utf-8") as f:
        base_url = json.load(f)["base_url"]
    with fetch(base_url + "manifest.json") as r:
        files = json.loads(r.read().decode("utf-8"))["files"]
    for fe in files:
        rel = fe["path"]
        dst = os.path.join(BASE, *rel.split("/"))
        if os.path.exists(dst) and sha256(dst) == fe["sha256"]:
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tmp = dst + ".part"
        with fetch(base_url + urllib.parse.quote(rel)) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        if sha256(tmp) == fe["sha256"]:
            os.replace(tmp, dst)
        else:
            os.remove(tmp)


def ensure_pynput():
    """Falls pynput fehlt (z. B. PC gewechselt): unsichtbar nachinstallieren."""
    import importlib.util
    if importlib.util.find_spec("pynput") is not None:
        return
    exe = sys.executable
    py = exe[:-len("pythonw.exe")] + "python.exe" if exe.lower().endswith("pythonw.exe") else exe
    try:
        subprocess.run([py, "-m", "pip", "install", "--user", "--disable-pip-version-check", "pynput"],
                       creationflags=NOWIN, capture_output=True, timeout=300)
    except Exception:
        pass


def run(script, wait):
    cmd = [sys.executable, os.path.join(BASE, script)]
    if wait:
        subprocess.run(cmd, cwd=BASE, creationflags=NOWIN)
    else:
        subprocess.Popen(cmd, cwd=BASE, creationflags=NOWIN)


def main():
    try:
        update()
    except Exception:
        pass   # offline oder Server nicht erreichbar -> mit vorhandenen Dateien weiter
    ensure_pynput()
    if not os.path.exists(LAYOUT):
        run("scum_depts_setup.py", wait=True)
    run("scum_depts_client.py", wait=False)


if __name__ == "__main__":
    main()
