"""
Hush — Windows Noise Tray App
==============================

Setup:
  python -m venv venv && venv\\Scripts\\activate
  pip install pystray Pillow numpy sounddevice soundfile

Run:
  python hush.py

Package as .exe:
  pip install pyinstaller
  pyinstaller --noconsole --onefile --add-data "pink_noise.ogg;." --add-data "brown_noise.ogg;." --add-data "grey_noise.ogg;." hush.py
  # Output: dist\\hush.exe

Auto-start: Drop a shortcut into %APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup

Tray icon always visible:
  If the icon is hidden behind the "^" arrow:
  Right-click taskbar → Taskbar settings → Other system tray icons
  → Find "Hush" and toggle it ON.
  The icon will then always show next to the clock.

Audio files:
  pink_noise.ogg, brown_noise.ogg, and grey_noise.ogg must be in the same directory as this script.
  They loop seamlessly. Re-generate with any audio you like:
    ffmpeg -ss 120 -i source.webm -t 300 -ac 2 -ar 44100 -c:a libvorbis -q:a 4 output.ogg -y
  (5 min loop; crossfade is baked in at load time.)
"""

__version__ = '1.0.0'

import ctypes
import os
import sys
import threading
import tkinter as tk
import winreg
import numpy as np
import sounddevice as sd
import soundfile as sf
import pystray
from PIL import Image, ImageDraw

# ---------------------------------------------------------------------------
# Locate audio file (works both as script and as PyInstaller .exe)
# ---------------------------------------------------------------------------
if getattr(sys, "frozen", False):
    _BASE = sys._MEIPASS
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

AUDIO_FILES = {
    "pink":  os.path.join(_BASE, "pink_noise.ogg"),
    "brown": os.path.join(_BASE, "brown_noise.ogg"),
    "grey":  os.path.join(_BASE, "grey_noise.ogg"),
}

# ---------------------------------------------------------------------------
# Volume persistence (Windows registry)
# ---------------------------------------------------------------------------
_REG_KEY = r"Software\Hush"
_REG_VAL = "Volume"

def _load_volume() -> float:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            val, _ = winreg.QueryValueEx(k, _REG_VAL)
            return max(0.0, min(0.35, float(val)))
    except OSError:
        return 0.15

def _save_volume(v: float):
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _REG_KEY) as k:
            winreg.SetValueEx(k, _REG_VAL, 0, winreg.REG_SZ, str(v))
    except OSError:
        pass

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
playing = False
volume = _load_volume()   # restored from registry; default 15%
stream = None
_lock = threading.Lock()

_click_timer = None
_click_lock = threading.Lock()
DBLCLICK_DELAY = 0.3  # seconds

BLOCK_SIZE = 2048

FADE_SAMPLES = int(44100 * 0.5)  # 500 ms fade
_fade_gain   = 0.0               # 0.0 = silent, 1.0 = full volume
_fade_dir    = 0                 # +1 = fading in, -1 = fading out, 0 = steady

# ---------------------------------------------------------------------------
# Audio file looping
# ---------------------------------------------------------------------------
_audio_buffers = {"pink": None, "brown": None, "grey": None}
_audio_data    = None   # points to active buffer
_play_pos      = 0
current_noise  = "pink"


def _load_noise(noise_type: str):
    path = AUDIO_FILES[noise_type]
    if not os.path.exists(path):
        return
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    xfade = 10 * 44100
    t = np.linspace(0.0, np.pi / 2, xfade, dtype=np.float32)[:, np.newaxis]
    fade_in  = np.sin(t)
    fade_out = np.cos(t)
    data[:xfade] = data[:xfade] * fade_in + data[-xfade:] * fade_out
    _audio_buffers[noise_type] = data[:-xfade]


def _load_audio():
    global _audio_data
    _load_noise("pink")
    _audio_data = _audio_buffers["pink"]


def _switch_noise(noise_type: str):
    global current_noise, _audio_data, _play_pos
    if _audio_buffers[noise_type] is None:
        _load_noise(noise_type)
    with _lock:
        current_noise = noise_type
        _audio_data   = _audio_buffers[noise_type]
        _play_pos     = 0


def _audio_callback(outdata: np.ndarray, frames: int, time, status):
    global _play_pos, _fade_gain, _fade_dir

    # Fully silent and not fading in — skip audio fill entirely
    if _audio_data is None or (_fade_gain == 0.0 and _fade_dir <= 0):
        outdata.fill(0)
        return

    total = len(_audio_data)
    out_ch = outdata.shape[1]
    src_ch = _audio_data.shape[1]

    # Fill outdata with raw audio (no volume applied yet)
    remaining = frames
    write_pos = 0
    with _lock:
        pos = _play_pos
        while remaining > 0:
            available = total - pos
            chunk = min(remaining, available)
            src = _audio_data[pos : pos + chunk]
            if src_ch >= out_ch:
                outdata[write_pos : write_pos + chunk] = src[:, :out_ch]
            else:
                # mono source → duplicate to all output channels
                for c in range(out_ch):
                    outdata[write_pos : write_pos + chunk, c] = src[:, 0]
            pos = (pos + chunk) % total
            write_pos += chunk
            remaining -= chunk
        _play_pos = pos

    # Apply gain ramp (fade) × volume to the entire block
    if _fade_dir != 0:
        end_gain = float(np.clip(_fade_gain + (_fade_dir / FADE_SAMPLES) * frames, 0.0, 1.0))
        gains = np.linspace(_fade_gain, end_gain, frames, dtype=np.float32)
        _fade_gain = end_gain
        if _fade_gain <= 0.0 or _fade_gain >= 1.0:
            _fade_dir = 0
        outdata *= (gains * volume)[:, np.newaxis]
    else:
        outdata *= _fade_gain * volume


# ---------------------------------------------------------------------------
# Stream management
# ---------------------------------------------------------------------------
def _start_stream():
    global stream
    if stream is not None and stream.active:
        return
    if stream is not None:
        try:
            stream.close()
        except Exception:
            pass
        stream = None
    stream = sd.OutputStream(
        samplerate=44100,
        blocksize=BLOCK_SIZE,
        channels=2,
        dtype="float32",
        callback=_audio_callback,
    )
    stream.start()


def _stop_stream():
    global stream
    if stream is not None:
        stream.stop()
        stream.close()
        stream = None


# ---------------------------------------------------------------------------
# Tray icon image
# ---------------------------------------------------------------------------
_ICON_COLORS = {
    "pink":  {"active": (255, 20, 147), "inactive": (120, 40, 80)},
    "brown": {"active": (205, 133, 63), "inactive": (80, 45, 15)},
    "grey":  {"active": (160, 160, 160), "inactive": (70, 70, 70)},
}

def _make_icon(active: bool = False, noise_type: str = "pink") -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = _ICON_COLORS[noise_type]["active" if active else "inactive"]
    draw.ellipse([4, 4, 60, 60], fill=color)
    return img


# ---------------------------------------------------------------------------
# Menu actions
# ---------------------------------------------------------------------------
def _toggle_play(icon: pystray.Icon, item):
    global playing, _fade_dir
    playing = not playing
    _fade_dir = 1 if playing else -1
    if playing:
        _start_stream()
    icon.icon = _make_icon(active=playing, noise_type=current_noise)
    icon.update_menu()


def _show_volume_slider(icon, item):
    def _open():
        root = tk.Tk()
        root.overrideredirect(True)   # no title bar
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.92)
        root.configure(bg="#2b2b2b")

        # Position near bottom-right (near tray)
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w, h = 220, 60
        root.geometry(f"{w}x{h}+{sw - w - 16}+{sh - h - 60}")

        # Header bar with × button
        header = tk.Frame(root, bg="#3a3a3a", height=18)
        header.pack(fill="x")
        header.pack_propagate(False)

        close_btn = tk.Label(
            header, text="×", bg="#3a3a3a", fg="#888888",
            font=("Segoe UI", 10), cursor="hand2", padx=6,
        )
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: root.destroy())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg="#ffffff"))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg="#888888"))

        def on_change(val):
            global volume
            volume = float(val) / 100.0
            _save_volume(volume)

        slider = tk.Scale(
            root, from_=0, to=35, orient="horizontal",
            command=on_change, showvalue=False,
            bg="#2b2b2b", fg="#ffffff", highlightthickness=0,
            troughcolor="#555555", activebackground="#00b4a0",
            length=200, sliderlength=20, width=16,
        )

        slider.set(round(volume * 100))
        slider.pack(padx=10, pady=4)
        root.bind("<FocusOut>", lambda e: root.destroy())
        root.focus_force()
        root.mainloop()

    threading.Thread(target=_open, daemon=True).start()


def _on_tray_click(icon, item):
    global _click_timer
    with _click_lock:
        if _click_timer is not None:
            # Second click within window → double-click → open volume
            _click_timer.cancel()
            _click_timer = None
            _show_volume_slider(icon, item)
        else:
            # First click → start timer; fire play/pause if no second click
            def _fire_single():
                global _click_timer
                with _click_lock:
                    _click_timer = None
                _toggle_play(icon, item)
            _click_timer = threading.Timer(DBLCLICK_DELAY, _fire_single)
            _click_timer.start()


def _select_pink(icon: pystray.Icon, item):
    _switch_noise("pink")
    icon.icon = _make_icon(active=playing, noise_type="pink")
    icon.update_menu()


def _select_brown(icon: pystray.Icon, item):
    _switch_noise("brown")
    icon.icon = _make_icon(active=playing, noise_type="brown")
    icon.update_menu()


def _select_grey(icon: pystray.Icon, item):
    _switch_noise("grey")
    icon.icon = _make_icon(active=playing, noise_type="grey")
    icon.update_menu()


def _quit(icon: pystray.Icon, item):
    global playing
    playing = False
    _stop_stream()
    icon.stop()


# ---------------------------------------------------------------------------
# Build and run tray
# ---------------------------------------------------------------------------
def _build_menu() -> pystray.Menu:
    return pystray.Menu(
        pystray.MenuItem("Pink Noise",  _select_pink,  checked=lambda item: current_noise == "pink"),
        pystray.MenuItem("Brown Noise", _select_brown, checked=lambda item: current_noise == "brown"),
        pystray.MenuItem("Grey Noise",  _select_grey,  checked=lambda item: current_noise == "grey"),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(lambda item: "Stop" if playing else "Play", _on_tray_click, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Volume", _show_volume_slider),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", _quit),
    )


def main():
    _mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\HushTrayApp")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        sys.exit(0)

    _load_audio()
    icon = pystray.Icon(
        name="hush",
        icon=_make_icon(active=False, noise_type="pink"),
        title=f"Hush v{__version__}",
        menu=_build_menu(),
    )
    icon.run()


if __name__ == "__main__":
    main()
