"""Chest Counter - counts the chests you open in Fortnite.

It watches one small strip of the screen for the purple "+XP  CHEST" popup that
appears when you open a chest, and keeps two counts: this game and all time.
A small overlay under the minimap shows both while you play.

It only reads pixels from the screen (like a recording app does). It never
touches the game's memory, files or network traffic, and never presses keys.

Kept light on purpose:
  * only a 280x140 strip is captured, every 0.6 s, and only while Fortnite is
    the active window
  * runs at below-normal priority, no GPU use

Run:   python chest_counter.py
Tests: python chest_counter.py --test  shot1.png shot2.png ...   (match scores)
       python chest_counter.py --count <fps> frame1.png ...       (counts a frame sequence)
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

import cv2
import mss
import numpy as np

cv2.setNumThreads(1)      # the images are tiny; extra threads just burn CPU spinning up

APP_NAME = "Chest Counter"
VERSION = "1.0.0"
HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(getattr(sys, "_MEIPASS", HERE), "assets")       # bundled into the .exe
DATA_DIR = os.path.join(os.environ.get("APPDATA", HERE), "ChestCounter")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
SNAP_DIR = os.path.join(DATA_DIR, "snapshots")

# HUD coordinates are in 1920x1080 space. Other resolutions are scaled by
# height and kept centred, so 1440p, 4K and ultrawide all line up.
BASE_W, BASE_H = 1920, 1080
WORK_SCALE = 0.5          # banners are matched at half resolution
WHITE_LEVEL = 200
PURPLE_LO, PURPLE_HI = (125, 70, 140), (165, 255, 255)   # OpenCV HSV, XP-popup outline
FORTNITE_EXE = "fortniteclient-win64-shipping.exe"

SCAN_INTERVAL = 0.6       # seconds between checks while Fortnite is active
IDLE_INTERVAL = 1.0       # seconds between "is Fortnite active?" checks otherwise

# ---- chests: the purple label under "+600 XP" (all in the same strip -> one capture)
_CHEST_STRIP = [1240, 280, 1520, 420]
CHEST_TEMPLATES = [
    {"file": "chest_live.png", "region": _CHEST_STRIP, "mode": "purple"},               # CHEST
    {"file": "chest_x2_live.png", "region": _CHEST_STRIP, "mode": "purple", "count": 2},  # CHEST X2
    {"file": "chest_sprite_live.png", "region": _CHEST_STRIP, "mode": "purple"},        # SPRITE CHEST
    {"file": "chest_xp.png", "region": _CHEST_STRIP, "mode": "purple"},                 # CHEST (soft)
    {"file": "chest_rare.png", "region": _CHEST_STRIP, "mode": "purple"},               # RARE CHEST
    {"file": "chest_sprite.png", "region": _CHEST_STRIP, "mode": "purple"},             # SPRITE CHEST (soft)
]
CHEST_THRESHOLD = 0.70    # chest popups score 0.81+, other XP popups (KNOCKED, SHIELD CRACKED...) up to 0.57
CHEST_GAP = 1.0           # popup must be gone this long before the next one counts as new
SNAP_MIN = 0.55           # with snapshots on, near misses down to this score are saved too

# ---- Battle Bus: seeing it starts a new game (resets "This game")
BUS_TEMPLATES = [
    {"file": "bus_banner.png", "region": [600, 100, 1320, 230]},        # LEAVE 'EM A TIP FOR SMOOTH SAILING
    {"file": "captain_banner.png", "region": [600, 100, 1320, 230]},    # CONTROL THE GOING MERRY...
    {"file": "jump_prompt.png", "region": [1110, 565, 1270, 635], "mode": "white"},   # "TO JUMP"
]
BUS_THRESHOLD = 0.75
BUS_HITS = 2              # bus seen this many checks in a row -> on the bus
NEW_GAME_GAP = 90.0       # bus must have been gone this long before seeing it again means a new game

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
# Handles are 64-bit; without these, ctypes would cut them down to 32-bit ints.
kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.SetPriorityClass.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CreateMutexW.restype = wt.HANDLE
user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.FindWindowW.restype = wt.HWND
user32.GetParent.restype, user32.GetParent.argtypes = wt.HWND, [wt.HWND]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)   # real pixel coordinates
except Exception:
    user32.SetProcessDPIAware()
try:
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
except Exception:
    pass


# ---------------------------------------------------------------- config ----
def load_config():
    cfg = {"enabled": True, "overlay": True, "snapshots": False, "game": 0, "total": 0}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        os.replace(tmp, CONFIG_PATH)
    except OSError:
        pass


# --------------------------------------------------------------- windows ----
def fortnite_rect():
    """Screen rect (left, top, w, h) of Fortnite's game area if it's the active window."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wt.DWORD(260)
        if not kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return None
        if os.path.basename(buf.value).lower() != FORTNITE_EXE:
            return None
    finally:
        kernel32.CloseHandle(h)
    rc = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(rc))
    pt = wt.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    if rc.right < 200 or rc.bottom < 200:
        return None
    return pt.x, pt.y, rc.right, rc.bottom


def base_to_screen(frame, region):
    """Convert a 1920x1080 region into a screen rect inside `frame`."""
    left, top, w, h = frame
    s = h / BASE_H
    x1, y1, x2, y2 = region
    sx1 = max(0, int(w / 2 + (x1 - BASE_W / 2) * s))
    sx2 = min(w, int(w / 2 + (x2 - BASE_W / 2) * s))
    sy1 = max(0, int(y1 * s))
    sy2 = min(h, int(y2 * s))
    return {"left": left + sx1, "top": top + sy1, "width": sx2 - sx1, "height": sy2 - sy1}, s


# ------------------------------------------------------------- detection ----
class Detector:
    """Template matcher. Modes:
       None     - grayscale, half scale (banners on a solid plate)
       "white"  - only bright pixels, full scale (white text over the game world)
       "purple" - only purple outline pixels, full scale (XP popup text)"""

    def __init__(self, templates, threshold):
        self.threshold = threshold
        self.items = []
        self.counts = {t["file"]: t.get("count", 1) for t in templates}
        self.full = any(c > 1 for c in self.counts.values())   # need every score to read X2
        self.last_x = None        # right edge of the best match in 1080p space
        self.last_scores = {}
        self.last_grabs = {}
        for t in templates:
            img = cv2.imread(os.path.join(ASSETS, t["file"]), cv2.IMREAD_COLOR)
            if img is None:
                continue
            mode = t.get("mode")
            img = self._prep(img, 1.0, mode)
            ink = float((img > 127).mean()) if mode else 0.0
            self.items.append((t["file"], img, t["region"], mode, ink))

    @staticmethod
    def _prep(bgr, s, mode):
        """1080p-space crop (BGR/BGRA) at native scale `s` -> matching image."""
        if mode == "purple":
            if bgr.shape[2] == 4:
                bgr = cv2.cvtColor(bgr, cv2.COLOR_BGRA2BGR)
            m = cv2.inRange(cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV), PURPLE_LO, PURPLE_HI)
        else:
            gray = cv2.cvtColor(bgr, cv2.COLOR_BGRA2GRAY if bgr.shape[2] == 4 else cv2.COLOR_BGR2GRAY)
            if not mode:
                f = WORK_SCALE / s
                return cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
            m = np.where(gray > WHITE_LEVEL, 255, 0).astype(np.uint8)
        if s != 1.0:
            m = cv2.resize(m, None, fx=1 / s, fy=1 / s, interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(m, (3, 3), 0)   # tolerate 1px wobble

    @staticmethod
    def _match(roi, tpl, mode, ink):
        """-> (score, x of best match in roi pixels)"""
        if roi.shape[0] < tpl.shape[0] or roi.shape[1] < tpl.shape[1]:
            return 0.0, 0
        if mode:
            fill = (roi > 127).mean()
            if fill * roi.size < 0.5 * ink * tpl.size:
                return 0.0, 0   # not enough text-coloured pixels anywhere - skip
            if mode == "purple" and fill > 0.4:
                return 0.0, 0   # a big purple area (storm, sky) - not text
        res = np.nan_to_num(cv2.matchTemplate(roi, tpl, cv2.TM_CCOEFF_NORMED), nan=0.0, posinf=0.0, neginf=0.0)
        _, best, _, loc = cv2.minMaxLoc(res)
        return float(best), loc[0]

    def _record(self, name, sc, x, region, mode, tpl_w):
        self.last_scores[name] = sc
        if sc > self.best:
            self.best = sc
            self.last_x = region[0] + (x + tpl_w) / (1.0 if mode else WORK_SCALE)

    def scan(self, sct, frame):
        self.best, self.last_x, self.last_scores = 0.0, None, {}
        grabs, rois = {}, {}      # templates sharing a strip share one grab
        self.last_grabs = grabs
        for name, tpl, region, mode, ink in self.items:
            mon, s = base_to_screen(frame, region)
            if mon["width"] < 8 or mon["height"] < 8:
                continue
            key = tuple(region)
            if key not in grabs:
                grabs[key] = np.asarray(sct.grab(mon))      # BGRA, just the strip
            if (key, mode) not in rois:
                rois[key, mode] = self._prep(grabs[key], s, mode)
            sc, x = self._match(rois[key, mode], tpl, mode, ink)
            self._record(name, sc, x, region, mode, tpl.shape[1])
            if self.best >= self.threshold and not self.full:
                break
        return self.best

    def score_image(self, bgr):
        """Score a full screenshot (used by the tests)."""
        h, w = bgr.shape[:2]
        self.best, self.last_x, self.last_scores = 0.0, None, {}
        for name, tpl, region, mode, ink in self.items:
            mon, s = base_to_screen((0, 0, w, h), region)
            roi = bgr[mon["top"]:mon["top"] + mon["height"], mon["left"]:mon["left"] + mon["width"]]
            sc, x = self._match(self._prep(roi, s, mode), tpl, mode, ink)
            self._record(name, sc, x, region, mode, tpl.shape[1])
        return self.best

    def multiplier(self):
        """How many chests the popup says: 2 for "CHEST X2", else 1.
        X2 wins only if it matches about as well as the best plain label
        (the plain "CHEST" template also fits inside "CHEST X2")."""
        best_plain = max((v for k, v in self.last_scores.items() if self.counts.get(k, 1) == 1), default=0.0)
        mult = 1
        for k, v in self.last_scores.items():
            c = self.counts.get(k, 1)
            if c > mult and v >= self.threshold and v >= best_plain - 0.03:
                mult = c
        return mult


class ChestTally:
    """Turns popup sightings into chest counts.
    - a popup that appears after being gone CHEST_GAP seconds -> new
    - a popup that restarts its slide-in (jumps 20+ px right) -> new
    - "CHEST" turning into "CHEST X2" while showing -> one more chest
    update() returns how many chests to add (0, 1 or 2)."""

    def __init__(self):
        self.last_seen = None
        self.prev_x = None
        self.mult = 0

    def update(self, score, x, mult=1, now=None):
        now = time.monotonic() if now is None else now
        if score < CHEST_THRESHOLD:
            return 0
        add = 0
        fresh = self.last_seen is None or now - self.last_seen >= CHEST_GAP
        # A fresh popup slides in from ~30px right of where it settles. Its own
        # settle-bounce is only ~12px, so 20+ px to the right means a new chest.
        restart = (not fresh and self.prev_x is not None and x is not None and x - self.prev_x >= 20)
        if fresh or restart:
            add, self.mult = mult, mult
        elif mult > self.mult:
            add, self.mult = mult - self.mult, mult
        self.last_seen = now
        self.prev_x = x
        return add


# --------------------------------------------------------------- scanner ----
class Scanner(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.enabled = threading.Event()
        self.status = "Off"
        self.chest_score = 0.0
        self.frame = None           # Fortnite's screen rect while it's the active window
        self.on_bus = False

    def run(self):
        chest_det = Detector(CHEST_TEMPLATES, CHEST_THRESHOLD)
        bus_det = Detector(BUS_TEMPLATES, BUS_THRESHOLD)
        tally = ChestTally()
        bus_hits, bus_last_seen, last_snap = 0, 0.0, 0.0
        with mss.mss() as sct:
            while True:
                if not self.enabled.is_set():
                    self.status, self.chest_score, self.frame, self.on_bus = "Off", 0.0, None, False
                    self.enabled.wait()          # sleeps with zero CPU until turned on
                frame = fortnite_rect()
                self.frame = frame
                if frame is None:
                    self.status = "Waiting for Fortnite to be the active window"
                    time.sleep(IDLE_INTERVAL)
                    continue
                now = time.monotonic()
                try:
                    bus = bus_det.scan(sct, frame) >= BUS_THRESHOLD
                except Exception:
                    bus = False
                bus_hits = bus_hits + 1 if bus else 0
                if bus_hits >= BUS_HITS:
                    if not self.on_bus and now - bus_last_seen > NEW_GAME_GAP:
                        self.app.new_game()
                    self.on_bus = True
                    bus_last_seen = now
                elif not bus and self.on_bus and now - bus_last_seen > 3:
                    self.on_bus = False

                # chests can't be opened on the bus
                if not self.on_bus:
                    try:
                        self.chest_score = chest_det.scan(sct, frame)
                    except Exception:
                        self.chest_score = 0.0
                    n = tally.update(self.chest_score, chest_det.last_x, chest_det.multiplier())
                    if n:
                        self.app.add_chests(n)
                    if self.app.cfg["snapshots"] and (n or (SNAP_MIN <= self.chest_score < CHEST_THRESHOLD
                                                            and time.time() - last_snap > 4)):
                        last_snap = time.time()
                        self._snapshot(chest_det, "counted" if n else "near_miss")
                self.status = "On the Battle Bus" if self.on_bus else "Watching for chests"
                time.sleep(SCAN_INTERVAL)

    def _snapshot(self, det, kind):
        """Save what the chest check just saw (newest 30 kept) - for troubleshooting."""
        try:
            raw = next(iter(det.last_grabs.values()))
            out = os.path.join(SNAP_DIR, kind)
            os.makedirs(out, exist_ok=True)
            name = time.strftime("%Y%m%d-%H%M%S") + f"_{self.chest_score:.2f}.png"
            cv2.imencode(".png", raw[:, :, :3])[1].tofile(os.path.join(out, name))
            for f in sorted(os.listdir(out))[:-30]:
                os.remove(os.path.join(out, f))
        except Exception:
            pass


# ---------------------------------------------------------------- hotkey ----
def start_hotkey(callback):
    """Ctrl+Alt+C toggles on/off from anywhere (even in game). Uses no CPU while waiting."""
    def loop():
        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x1, 0x2, 0x4000, 0x0312
        if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("C")):
            return
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                callback()
    threading.Thread(target=loop, daemon=True).start()


# --------------------------------------------------------------- overlay ----
class Overlay:
    """Counter drawn over the game: "This game" and "All time" rows.
    Numbers scroll up when they go up (down when they go down).
    Click-through and never takes focus. Animation only runs for ~1/4 s on a change."""
    GWL_EXSTYLE = -20
    EX = 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000   # layered, transparent, toolwindow, noactivate
    BG, LABEL, NUM, FLASH = "#14161c", "#cfd3dc", "#ffd34d", "#ffffff"
    STEPS, STEP_MS = 12, 18

    def __init__(self, root):
        from tkinter import font as tkfont
        self.root = root
        self.win = tk.Toplevel(root, bg=self.BG)
        self.win.title(APP_NAME + " overlay")
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.85)
        self.font = tkfont.Font(family="Segoe UI", size=12, weight="bold")
        self.h = self.font.metrics("linespace") + 2
        self.w = self.font.measure("This game:") + self.font.measure("00000") + 14
        self.rows = {}
        for i, (key, label) in enumerate((("game", "This game:"), ("total", "All time:"))):
            c = tk.Canvas(self.win, width=self.w, height=self.h, bg=self.BG, highlightthickness=0, bd=0)
            c.pack(padx=10, pady=(5, 0) if i == 0 else (0, 5))    # each row clips its own scroll
            c.create_text(0, self.h / 2, text=label, anchor="w", fill=self.LABEL, font=self.font)
            item = c.create_text(self.w, self.h / 2, text="", anchor="e", fill=self.NUM, font=self.font)
            self.rows[key] = {"c": c, "item": item, "value": None, "anim": None}
        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id())
        get = user32.GetWindowLongPtrW
        get.restype, get.argtypes = ctypes.c_ssize_t, [wt.HWND, ctypes.c_int]
        setl = user32.SetWindowLongPtrW
        setl.restype, setl.argtypes = ctypes.c_ssize_t, [wt.HWND, ctypes.c_int, ctypes.c_ssize_t]
        setl(self.hwnd, self.GWL_EXSTYLE, get(self.hwnd, self.GWL_EXSTYLE) | self.EX)
        user32.ShowWindow(self.hwnd, 0)
        self.shown = False

    def _finish(self, r):
        """Snap any running scroll to its end."""
        if r["anim"]:
            after_id, old, new = r["anim"]
            self.root.after_cancel(after_id)
            r["c"].delete(old)
            r["c"].coords(new, self.w, self.h / 2)
            r["c"].itemconfigure(new, fill=self.NUM)
            r["item"], r["anim"] = new, None

    def set_value(self, key, value):
        r = self.rows[key]
        if r["value"] == value:
            return
        if r["value"] is None or not self.shown:      # first draw / hidden: no animation
            self._finish(r)
            r["c"].itemconfigure(r["item"], text=str(value))
            r["value"] = value
            return
        self._finish(r)
        c, mid = r["c"], self.h / 2
        d = 1 if value > r["value"] else -1           # +1 scrolls up, -1 scrolls down
        old = r["item"]
        new = c.create_text(self.w, mid + d * self.h, text=str(value), anchor="e",
                            fill=self.FLASH, font=self.font)
        r["value"] = value

        def step(i=1):
            t = i / self.STEPS
            e = 1 - (1 - t) ** 3                       # ease out
            c.coords(old, self.w, mid - d * self.h * e)
            c.coords(new, self.w, mid + d * self.h * (1 - e))
            if i < self.STEPS:
                r["anim"] = (self.root.after(self.STEP_MS, step, i + 1), old, new)
            else:
                c.delete(old)
                r["item"], r["anim"] = new, None
                self.root.after(250, lambda: c.itemconfigure(new, fill=self.NUM)
                                if new in c.find_all() else None)
        r["anim"] = (self.root.after(self.STEP_MS, step), old, new)

    def show(self, frame, game, total):
        left, top, w, h = frame
        s = h / BASE_H
        x = left + w - int(24 * s) - self.win.winfo_reqwidth()    # right edge, just under the minimap
        y = top + int(240 * s)
        # HWND_TOPMOST, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        user32.SetWindowPos(self.hwnd, wt.HWND(-1), x, y, 0, 0, 0x0001 | 0x0010 | 0x0040)
        self.shown = True
        self.set_value("game", game)
        self.set_value("total", total)

    def hide(self):
        if self.shown:
            user32.ShowWindow(self.hwnd, 0)
            self.shown = False


# -------------------------------------------------------------------- UI ----
BG, PANEL, FG, DIM = "#14161c", "#1e2129", "#eef0f5", "#8b90a0"
GREEN, RED, ACCENT, GOLD = "#2fbf71", "#d9534f", "#7c3aed", "#ffd34d"


class App:
    def __init__(self):
        self.cfg = load_config()
        self.scanner = Scanner(self)
        self.hotkey_pressed = False
        self.dirty = False
        # Opened while in a game? Hand focus straight back to Fortnite and start minimised.
        game_hwnd = user32.GetForegroundWindow() if fortnite_rect() else None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        try:
            self.root.iconbitmap(os.path.join(ASSETS, "icon.ico"))
        except tk.TclError:
            pass
        self.root.configure(bg=BG)
        self.root.resizable(False, False)
        self._build()
        self.overlay = Overlay(self.root)
        self._refresh()
        missing = [t["file"] for t in CHEST_TEMPLATES + BUS_TEMPLATES
                   if not os.path.exists(os.path.join(ASSETS, t["file"]))]
        if missing:
            messagebox.showerror(APP_NAME, "Some files are missing, so chests can't be detected:\n"
                                 + "\n".join(missing) + "\n\nRe-download Chest Counter.")

        self.scanner.start()
        self.set_enabled(self.cfg["enabled"])
        start_hotkey(lambda: setattr(self, "hotkey_pressed", True))
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(300, self._tick)
        if game_hwnd:
            def give_back():
                self.root.iconify()
                user32.SetForegroundWindow(game_hwnd)
            self.root.after(150, give_back)

    # ---- layout
    def _label(self, parent, text, size=10, color=FG, bold=False, **kw):
        return tk.Label(parent, text=text, bg=parent["bg"], fg=color,
                        font=("Segoe UI", size, "bold" if bold else "normal"), **kw)

    def _button(self, parent, text, cmd, color="#2a2e39"):
        return tk.Button(parent, text=text, command=cmd, bg=color, fg=FG, activebackground=ACCENT,
                         activeforeground=FG, relief="flat", bd=0, padx=12, pady=6,
                         font=("Segoe UI", 10), cursor="hand2")

    def _check(self, parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var, command=self._on_options, bg=parent["bg"],
                              fg=FG, selectcolor=PANEL if parent["bg"] == BG else BG,
                              activebackground=parent["bg"], activeforeground=FG,
                              font=("Segoe UI", 10), anchor="w")

    def _build(self):
        pad = {"padx": 18}
        self._label(self.root, APP_NAME, 18, bold=True).pack(anchor="w", pady=(16, 0), **pad)
        self._label(self.root, "Counts the chests you open in Fortnite", 10, DIM).pack(anchor="w", **pad)

        self.toggle_btn = tk.Button(self.root, command=self.toggle, relief="flat", bd=0, fg="white",
                                    font=("Segoe UI", 15, "bold"), height=2, cursor="hand2",
                                    activeforeground="white")
        self.toggle_btn.pack(fill="x", pady=(14, 6), **pad)
        self.status_lbl = self._label(self.root, "", 10, DIM)
        self.status_lbl.pack(anchor="w", **pad)

        box = tk.Frame(self.root, bg=PANEL, padx=14, pady=12)
        box.pack(fill="x", pady=(14, 0), **pad)
        row = tk.Frame(box, bg=PANEL)
        row.pack(fill="x")
        left = tk.Frame(row, bg=PANEL)
        left.pack(side="left", expand=True, fill="x")
        self._label(left, "THIS GAME", 8, DIM, bold=True).pack(anchor="w")
        self.game_lbl = self._label(left, "0", 30, GOLD, bold=True)
        self.game_lbl.pack(anchor="w")
        right = tk.Frame(row, bg=PANEL)
        right.pack(side="left", expand=True, fill="x")
        self._label(right, "ALL TIME", 8, DIM, bold=True).pack(anchor="w")
        self.total_lbl = self._label(right, "0", 30, FG, bold=True)
        self.total_lbl.pack(anchor="w")
        btns = tk.Frame(box, bg=PANEL)
        btns.pack(fill="x", pady=(10, 0))
        self._button(btns, "−1", lambda: self.adjust(-1)).pack(side="left")
        self._button(btns, "+1", lambda: self.adjust(1)).pack(side="left", padx=(8, 0))
        self._button(btns, "New game", self.new_game_clicked).pack(side="left", padx=(8, 0))
        self._button(btns, "Reset all", self.reset_all).pack(side="left", padx=(8, 0))

        opts = tk.Frame(self.root, bg=BG)
        opts.pack(fill="x", pady=(12, 0), **pad)
        self.overlay_var = tk.BooleanVar(value=self.cfg["overlay"])
        self.snap_var = tk.BooleanVar(value=self.cfg["snapshots"])
        self._check(opts, "Show the counter in game (under the minimap)", self.overlay_var).pack(fill="x")
        self._check(opts, "Save snapshots of what it sees (for troubleshooting)", self.snap_var).pack(fill="x")
        self.score_lbl = self._label(self.root, "", 9, DIM)
        self.score_lbl.pack(anchor="w", pady=(8, 0), **pad)
        foot = tk.Frame(self.root, bg=BG)
        foot.pack(fill="x", pady=(6, 16), **pad)
        self._label(foot, "Ctrl + Alt + C turns it on/off (works in game)", 9, DIM).pack(side="left")
        link = self._label(foot, "snapshots", 9, ACCENT, cursor="hand2")
        link.pack(side="right")
        link.bind("<Button-1>", lambda e: self.open_snapshots())
        self._label(foot, f"v{VERSION}", 9, DIM).pack(side="right", padx=(0, 10))

    # ---- state
    def set_enabled(self, on):
        (self.scanner.enabled.set if on else self.scanner.enabled.clear)()
        self.cfg["enabled"] = on
        self.dirty = True
        self.toggle_btn.configure(text="ON" if on else "OFF  —  click to turn on",
                                  bg=GREEN if on else RED, activebackground=GREEN if on else RED)

    def toggle(self):
        self.set_enabled(not self.scanner.enabled.is_set())

    def _on_options(self):
        self.cfg["overlay"] = self.overlay_var.get()
        self.cfg["snapshots"] = self.snap_var.get()
        self.dirty = True

    # called from the scanner thread - only touch numbers here, the UI picks them up in _tick
    def add_chests(self, n):
        self.cfg["game"] += n
        self.cfg["total"] += n
        self.dirty = True

    def new_game(self):
        self.cfg["game"] = 0
        self.dirty = True

    def new_game_clicked(self):
        self.new_game()
        self._refresh()

    def adjust(self, d):
        if self.cfg["game"] + d < 0:
            return
        self.cfg["game"] += d
        self.cfg["total"] = max(0, self.cfg["total"] + d)
        self.dirty = True
        self._refresh()

    def reset_all(self):
        if messagebox.askyesno(APP_NAME, "Reset this game AND all time to 0?"):
            self.cfg["game"] = self.cfg["total"] = 0
            self.dirty = True
            self._refresh()

    def open_snapshots(self):
        os.makedirs(SNAP_DIR, exist_ok=True)
        os.startfile(SNAP_DIR)

    def _refresh(self):
        self.game_lbl.configure(text=str(self.cfg["game"]))
        self.total_lbl.configure(text=str(self.cfg["total"]))
        if self.dirty:
            self.dirty = False
            save_config(self.cfg)

    def _tick(self):
        if self.hotkey_pressed:
            self.hotkey_pressed = False
            self.toggle()
            try:
                import winsound
                winsound.MessageBeep(0x40 if self.scanner.enabled.is_set() else 0x10)
            except Exception:
                pass
        on = self.scanner.enabled.is_set()
        self.status_lbl.configure(text=self.scanner.status if on else "Off - not watching",
                                  fg=GREEN if on and self.scanner.frame else DIM)
        self.score_lbl.configure(
            text=f"Chest popup match: {self.scanner.chest_score:.2f}  (needs {CHEST_THRESHOLD:.2f})"
            if on and self.scanner.frame else "")
        self._refresh()
        frame = self.scanner.frame
        if on and self.cfg["overlay"] and frame:
            self.overlay.show(frame, self.cfg["game"], self.cfg["total"])
        else:
            self.overlay.hide()
        self.root.after(400 if on else 1000, self._tick)

    def quit(self):
        save_config(self.cfg)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


# ------------------------------------------------------------------ tests ----
def _read(p):
    return cv2.imdecode(np.fromfile(p, np.uint8), cv2.IMREAD_COLOR)


def test(paths):
    det = Detector(CHEST_TEMPLATES, CHEST_THRESHOLD)
    for p in paths:
        img = _read(p)
        if img is None:
            print(f"{p}: can't open")
            continue
        sc = det.score_image(img)
        print(f"{sc:.3f}  {'CHEST x%d' % det.multiplier() if sc >= CHEST_THRESHOLD else '-------'}  {os.path.basename(p)}")


def count_test(paths, fps):
    """Run the tally over frames in order (frames are 1/fps seconds apart)."""
    det, tally, n = Detector(CHEST_TEMPLATES, CHEST_THRESHOLD), ChestTally(), 0
    for i, p in enumerate(paths):
        sc = det.score_image(_read(p))
        add = tally.update(sc, det.last_x, det.multiplier(), now=i / fps)
        n += add
        x = f"{det.last_x:.0f}" if det.last_x is not None else "-"
        print(f"{sc:.2f} x={x:>5} {f'+{add} CHEST' if add else '':8s} {os.path.basename(p)}")
    print(f"TOTAL CHESTS: {n}")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--test":
        test(sys.argv[2:])
    elif len(sys.argv) > 3 and sys.argv[1] == "--count":
        count_test(sys.argv[3:], float(sys.argv[2]))
    else:
        # Only ever one copy (two would double the counts).
        _mutex = kernel32.CreateMutexW(None, False, "Local\\ChestCounterSingleInstance")
        if kernel32.GetLastError() == 183:          # ERROR_ALREADY_EXISTS
            hwnd = user32.FindWindowW(None, APP_NAME)
            if hwnd:
                user32.ShowWindow(hwnd, 9)          # SW_RESTORE
                user32.SetForegroundWindow(hwnd)
            sys.exit(0)
        App().run()
