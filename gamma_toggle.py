"""
Парящие круглые кнопки: по одной на каждый подключённый монитор + кнопки «Фото», «Прицел» и «Сценарий» (клавиши, паузы, клики).
Кнопка поднимает гамму СВОЕГО монитора до x2, повторное нажатие возвращает обычную.

Спрятано всё, кроме маленького полукруга-язычка у края экрана. Наведи на него мышь,
и из него веером (полукругом) вылетят кнопки без фона.
Язычок можно перетащить мышью на любой монитор: он прилипнет к ближайшему краю
(в том числе к общему краю между мониторами; пока тащишь, пунктирный полукруг
показывает, куда он встанет) и запомнит место.
Правая кнопка мыши по язычку: меню «Выход».
Только Windows. Сторонние библиотеки не нужны (tkinter + ctypes).
Запуск: python gamma_toggle.py

Способ 1: настоящая гамма-таблица конкретного монитора (SetDeviceGammaRamp).
Способ 2 (если драйвер/HDR не дают менять гамму): светлая полупрозрачная плёнка
          поверх этого монитора, через которую проходят клики мыши.
"""
import base64
import ctypes
import io
import json
import math
import os
import re
import sys
import tempfile
import random
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont
from ctypes import wintypes

# Чтобы размеры и координаты мониторов считались в реальных пикселях
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)

gdi32.CreateDCW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p]
gdi32.CreateDCW.restype = wintypes.HDC
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL
gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL
gdi32.GetDeviceCaps.argtypes = [wintypes.HDC, ctypes.c_int]
gdi32.GetDeviceCaps.restype = ctypes.c_int
winmm = ctypes.WinDLL("winmm")

user32.GetAsyncKeyState.restype = ctypes.c_short
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = ctypes.c_long
user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.SetWindowLongW.restype = ctypes.c_long
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.SetWindowPos.restype = wintypes.BOOL
HWND_TOPMOST = wintypes.HWND(-1)
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


def raise_top(hwnd):
    """Поднимает окно наверх только если над ним стоит чужое окно. Если выше только наши же окна
    (панель, прицел, плёнка), ничего не делаем: иначе они бы по кругу перекрывали друг друга и мигали."""
    try:
        prev = user32.GetWindow(hwnd, 3)      # GW_HWNDPREV: окно прямо над нашим
        if not prev:
            return                            # мы уже выше всех
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(prev, ctypes.byref(pid))
        if pid.value == os.getpid():
            return                            # выше нас только наше же окно
        user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    except Exception:
        pass

Ramp = (ctypes.c_ushort * 256) * 3  # R, G, B по 256 значений

# Нужная гамма (1.0 - стандартная). Если Windows её не примет, пробуем чуть слабее.
GAMMA_TARGET = 2.0
GAMMA_CANDIDATES = [GAMMA_TARGET, 1.8, 1.5]

# Запасной способ: насколько белая плёнка (0.0 - прозрачная, 1.0 - сплошной белый).
OVERLAY_ALPHA = 0.25

# Файл, который открывает третья кнопка
PHOTO_PATH = r"C:\Users\Vitalya\photo_2026-01-20_19-31-28.jpg"
# Вшитое фото (программа сама пишет его сюда, когда выбираешь новое правой кнопкой мыши).
# Если оно есть, путь выше не нужен.
PHOTO_B64 = ""
PHOTO_FILE = "gamma_panel_photo.bin"   # в exe выбранное фото хранится рядом с программой
MAX_FRAMES = 100                       # сколько кадров гифки показывать на кнопке
MAX_GIF_BYTES = 2 * 1024 * 1024        # гифка больше этого размера уменьшается перед вшиванием

# Стили прицела: (название, форма, цвет). ПКМ по кнопке переключает по кругу.
CROSSHAIRS = [("Крест", "cross", "#3CFF5A"),
              ("Точка", "dot", "#FF3B3B"),
              ("Круг", "ring", "#2EE6FF"),
              ("Крест + точка", "crossdot", "#FFFFFF")]

# Кнопка «Клавиши»: сочетания хранятся в этом файле рядом с программой
KEYS_FILE = "gamma_panel_keys.json"

# Масштаб интерфейса под настройки Windows (100% = 1.0, 150% = 1.5 ...)
try:
    S = max(1.0, ctypes.windll.user32.GetDpiForSystem() / 96.0)
except Exception:
    S = 1.0


def u(v):
    """Размер в пикселях с учётом масштаба экрана."""
    return int(round(v * S))


def geo(w, h, x, y):
    """Строка геометрии для Tk. Знак «+» перед КАЖДЫМ числом обязателен: запись «-1500»
    Tk понимает как отступ от ПРАВОГО/нижнего края экрана, а не как отрицательную координату
    (так выглядят мониторы слева от основного или над ним). «+-1500» - верная запись."""
    return f"{int(w)}x{int(h)}+{int(x)}+{int(y)}"


user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL


def cursor_pos():
    """Положение курсора в тех же координатах, что и список мониторов."""
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


# ---------- нажатие клавиш ----------
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
user32.keybd_event.restype = None
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.VkKeyScanW.argtypes = [wintypes.WCHAR]
user32.VkKeyScanW.restype = ctypes.c_short

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUT_UNION)]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def send_key(vk, up=False):
    """Одно событие клавиши через SendInput. Если у клавиши есть скан-код, шлём именно его
    (так клавиши видят игры, читающие клавиатуру напрямую), иначе обычный код клавиши."""
    scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
    flags = (0x0002 if up else 0) | (0x0001 if vk in EXTENDED_KEYS else 0)
    if scan:
        flags |= 0x0008                      # KEYEVENTF_SCANCODE
    inp = INPUT(type=1)
    inp.u.ki = KEYBDINPUT(0 if scan else vk, scan, flags, 0, 0)
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


def send_mouse(flag, dx=0, dy=0):
    inp = INPUT(type=0)
    inp.u.mi = MOUSEINPUT(dx, dy, 0, flag, 0, 0)
    return user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) == 1


user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]


def foreground_info():
    """Какое окно сейчас активно: туда уйдут нажатия (для журнала)."""
    try:
        hwnd = user32.GetForegroundWindow()
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return f"окно «{buf.value}»" + (" (это сама панель!)" if pid.value == os.getpid() else "")
    except Exception:
        return "окно неизвестно"


MOD_KEYS = {"ctrl": 0x11, "control": 0x11, "alt": 0x12, "shift": 0x10,
            "win": 0x5B, "windows": 0x5B, "super": 0x5B, "cmd": 0x5B}
NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09, "space": 0x20,
    "backspace": 0x08, "bksp": 0x08, "del": 0x2E, "delete": 0x2E, "ins": 0x2D, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pgup": 0x21, "pageup": 0x21, "pgdn": 0x22, "pagedown": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "printscreen": 0x2C, "prtsc": 0x2C, "capslock": 0x14, "numlock": 0x90,
    "minus": 0xBD, "equals": 0xBB, "comma": 0xBC, "period": 0xBE, "slash": 0xBF,
    "backslash": 0xDC, "semicolon": 0xBA, "quote": 0xDE, "backtick": 0xC0,
    "lbracket": 0xDB, "rbracket": 0xDD,
    "volumeup": 0xAF, "volumedown": 0xAE, "volumemute": 0xAD,
    "playpause": 0xB3, "nexttrack": 0xB0, "prevtrack": 0xB1,
}
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B,
                 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB3}


def resolve_key(name):
    """Название клавиши -> код клавиши Windows (или None, если не поняли)."""
    if name in MOD_KEYS:
        return MOD_KEYS[name]
    if name in NAMED_KEYS:
        return NAMED_KEYS[name]
    m = re.fullmatch(r"f(\d{1,2})", name)
    if m and 1 <= int(m.group(1)) <= 24:
        return 0x6F + int(m.group(1))
    m = re.fullmatch(r"num(?:pad)?(\d)", name)
    if m:
        return 0x60 + int(m.group(1))
    if len(name) == 1:
        if name.isascii() and name.isalnum():
            return ord(name.upper())
        vk = user32.VkKeyScanW(name) & 0xFF
        return None if vk == 0xFF else vk
    return None


def parse_combo(text):
    """'ctrl+alt+1' -> [0x11, 0x12, 0x31] (порядок нажатия) или None, если сочетание непонятно."""
    parts = [p.strip().lower() for p in text.split("+")]
    if not parts or any(not p for p in parts):
        return None
    vks = []
    for p in parts[:-1]:
        if p not in MOD_KEYS:
            return None                      # до последней клавиши могут стоять только ctrl/alt/shift/win
        vks.append(MOD_KEYS[p])
    last = resolve_key(parts[-1])
    if last is None:
        return None
    vks.append(last)
    return vks


def tap_keys(vks, hold=0.08):
    """Зажимает клавиши по очереди, держит их hold секунд, затем отпускает в обратном порядке.
    Удержание важно: игры опрашивают клавиатуру раз в кадр, и мгновенное нажатие они пропускают.
    Возвращает True, если Windows приняла все события."""
    ok = True
    for vk in vks:
        ok &= send_key(vk)
        time.sleep(0.01)
    time.sleep(hold)
    for vk in reversed(vks):
        ok &= send_key(vk, up=True)
        time.sleep(0.01)
    return ok


# ---------- мышь и шаги сценария ----------
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_size_t]
user32.mouse_event.restype = None
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL

MOUSE_FLAGS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
WAIT_WORDS = {"wait", "sleep", "pause", "delay", "ждать", "жди", "пауза"}
CLICK_WORDS = {"click": ("left", 1), "lclick": ("left", 1), "leftclick": ("left", 1), "клик": ("left", 1),
               "rclick": ("right", 1), "rightclick": ("right", 1),
               "mclick": ("middle", 1), "middleclick": ("middle", 1),
               "dblclick": ("left", 2), "doubleclick": ("left", 2)}
MOVE_WORDS = {"move", "moveto", "mouse", "двинуть"}
# Сценарий по умолчанию. Последние шаги: после ESC двигаем мышь на кнопку «Возродить»
# (34,4% ширины вправо и 33,9% высоты вниз от центра экрана) и нажимаем её.
DEFAULT_STEPS = ["v", "1", "f11", "wait 8.5", "click", "wait 2", "esc", "wait 1",
                 "move c 34.4% 33.9%", "click", "enter", "wait 17"]
OLD_DEFAULT_STEPS = [["1", "f11", "wait 6", "click"],      # прежние варианты по умолчанию
                     ["1", "f11", "wait 10", "click"],
                     ["1", "f11", "wait 8.5", "click"],
                     ["1", "f11", "wait 8.5", "click", "wait 2", "esc"],
                     ["1", "f11", "wait 8.5", "click", "wait 2", "esc", "wait 1",
                      "move c 34.4% 33.9%", "click"]]


def click_mouse(button, count, pos, hold=0.08):
    """Клик мышью в текущем месте курсора или в точке экрана pos=(x, y).
    Перед кликом курсор сдвигается на пиксель и возвращается: некоторые игры принимают клик,
    только если до него было движение мыши. Возвращает True, если Windows приняла все события."""
    if pos:
        user32.SetCursorPos(int(pos[0]), int(pos[1]))
        time.sleep(0.05)
    down, up = MOUSE_FLAGS[button]
    ok = send_mouse(0x0001, 1, 0)           # MOUSEEVENTF_MOVE на 1 пиксель
    time.sleep(0.015)
    ok &= send_mouse(0x0001, -1, 0)
    time.sleep(0.03)
    for _ in range(count):
        ok &= send_mouse(down)
        time.sleep(hold)
        ok &= send_mouse(up)
        time.sleep(0.05)
    return ok


JITTER_MS = 300     # все ожидания сценария плавают на случайную величину до ±300 мс


def jit(ms):
    """Случайно отклоняет время ожидания не более чем на ±JITTER_MS (результат не меньше 0)."""
    return max(0, int(ms + random.uniform(-JITTER_MS, JITTER_MS)))


def jit_up(ms):
    """Только в большую сторону (от 0 до +JITTER_MS): для пауз между шагами, которые нельзя
    делать короче заданной, иначе игра не успевает обработать предыдущее нажатие."""
    return int(ms + random.uniform(0, JITTER_MS))


def move_mouse(pos):
    """Двигает курсор в точку экрана и «встряхивает» его на пиксель, чтобы игра и меню
    заметили движение (наведение на кнопку). Возвращает True, если Windows приняла события."""
    user32.SetCursorPos(int(pos[0]), int(pos[1]))
    time.sleep(0.05)
    ok = send_mouse(0x0001, 1, 0)
    time.sleep(0.02)
    ok &= send_mouse(0x0001, -1, 0)
    return ok


def parse_pos(rest):
    """Координаты в шаге: 'X Y' (пиксели экрана) или 'c DX DY' (смещение от центра основного
    монитора; можно в процентах: 'c 34.4% 33.9%'). False, если не поняли."""
    if len(rest) == 3 and rest[0] in ("c", "center", "центр"):
        vals = []
        for t in rest[1:]:
            if not re.fullmatch(r"[+-]?\d+(?:[.,]\d+)?%?", t):
                return False
            vals.append((float(t.rstrip("%").replace(",", ".")), t.endswith("%")))
        return ("center", vals[0], vals[1])
    if len(rest) == 2 and all(re.fullmatch(r"-?\d+", t) for t in rest):
        return ("abs", int(rest[0]), int(rest[1]))
    return False


def resolve_pos(spec):
    """Превращает координаты из шага в пиксели экрана. Проценты считаются от ширины/высоты
    основного монитора, поэтому сценарий работает при любом разрешении."""
    if spec[0] == "abs":
        return spec[1], spec[2]
    m = list_monitors()[0]                      # основной монитор всегда первый
    def off(val, size):
        v, pct = val
        return v * size / 100.0 if pct else v
    return (int(round(m["x"] + m["w"] / 2.0 + off(spec[1], m["w"]))),
            int(round(m["y"] + m["h"] / 2.0 + off(spec[2], m["h"]))))


def parse_step(text):
    """Строка сценария -> ("keys", [коды]) | ("wait", секунды) | ("click", кнопка, раз, точка) | None."""
    parts = text.strip().lower().split()
    if not parts:
        return None
    word = parts[0]
    if word in WAIT_WORDS:                      # wait 6, wait 6s, wait 500ms, wait 0.5
        m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(ms|мс|s|sec|с)?", "".join(parts[1:]))
        if not m:
            return None
        sec = float(m.group(1).replace(",", "."))
        if m.group(2) in ("ms", "мс"):
            sec /= 1000.0
        return ("wait", min(sec, 3600.0))
    if word in MOVE_WORDS:                      # move 1200 600, move c 34% 34%
        spec = parse_pos(parts[1:])
        return ("move", spec) if spec else None
    if word in CLICK_WORDS:                     # click, rclick, dblclick, click 1200 600, click c 10 20
        button, count = CLICK_WORDS[word]
        rest = parts[1:]
        if not rest:
            return ("click", button, count, None)
        spec = parse_pos(rest)
        return None if spec is False else ("click", button, count, spec)
    vks = parse_combo(text)
    return None if vks is None else ("keys", vks)


# ---------- список мониторов ----------
class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


MONITORENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                       MONITORENUMPROC, wintypes.LPARAM]
user32.EnumDisplayMonitors.restype = wintypes.BOOL
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL


def list_monitors():
    """Возвращает список словарей: device, x, y, w, h, primary, num."""
    found = []

    def cb(hmon, hdc, rect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            m = re.search(r"(\d+)$", info.szDevice)
            found.append({
                "device": info.szDevice,
                "x": r.left, "y": r.top,
                "w": r.right - r.left, "h": r.bottom - r.top,
                "primary": bool(info.dwFlags & 1),
                "num": int(m.group(1)) if m else len(found) + 1,
                "work": (info.rcWork.left, info.rcWork.top, info.rcWork.right, info.rcWork.bottom),
            })
        return True

    user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(cb), 0)
    found.sort(key=lambda m: (not m["primary"], m["num"]))
    return found


def refresh_rate(device):
    """Частота обновления монитора в Гц (если узнать не удалось - 60)."""
    try:
        hdc = gdi32.CreateDCW("DISPLAY", device, None, None)
        if not hdc:
            return 60
        try:
            hz = gdi32.GetDeviceCaps(hdc, 116)  # VREFRESH
        finally:
            gdi32.DeleteDC(hdc)
        return min(hz, 360) if hz > 30 else 60
    except Exception:
        return 60


def make_ramp(gamma):
    ramp = Ramp()
    for i in range(256):
        v = int(((i / 255.0) ** (1.0 / gamma)) * 65535 + 0.5)
        for c in range(3):
            ramp[c][i] = min(65535, v)
    return ramp


# ---------- оформление ----------
KEY = "#010203"        # цвет, который окно делает прозрачным (для скруглённых углов)
PANEL = "#1A202C"      # фон панели
BORDER = "#2C3547"
TILE_OFF = "#262E3D"   # плитка выключена
TILE_HOV = "#303A4D"   # плитка под мышью
ICON_OFF = "#344056"   # круг с иконкой, когда выключено
ICON_ON = "#DFA630"    # круг с иконкой, когда включено
GLYPH_OFF = "#B4BED2"
ACCENT = "#F0BE4D"     # плитка включена
INK = "#1B1608"        # тёмный текст на янтарном фоне
INK_SOFT = "#5A4514"
TEXT = "#E9EDF5"
MUTED = "#8B96AB"
FAINT = "#5F6A80"
ERR = "#E88A7F"
FONT = "Segoe UI"

D = u(52)                       # диаметр круглой кнопки
FAN_STEP = 50                   # угол между соседними кнопками в полукруге, градусы
FAN_HALF = 60                   # максимальное отклонение крайней кнопки от центра веера, градусы
HR = u(24)                      # радиус полукруга-язычка
HANDLE_Y = u(60)                # положение язычка по умолчанию: отступ от верха экрана
TIP_H = u(40)                   # высота всплывающей подсказки
TIP_W = u(170)                  # максимальная ширина подсказки
STAGGER = 0.18                  # «веер»: насколько кнопки вылетают по очереди
SHADOW = "#0A0D13"              # тень под кнопками
TAU_TILE = 0.07                 # скорость анимации кнопок, секунды (не зависит от герцовки)
TAU_PANEL = 0.08                # скорость вылета кнопок, секунды
CONFIG_NAME = "gamma_panel.json"


def mix(a, b, t):
    """Смесь двух цветов #rrggbb: t=0 -> a, t=1 -> b."""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(ca, cb))


def rrect(c, x1, y1, x2, y2, r, **kw):
    """Скруглённый прямоугольник на Canvas."""
    pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y1 + r,
           x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2,
           x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1]
    return c.create_polygon(pts, smooth=True, **kw)


# ---------- плитки ----------
class Tile:
    """Общая часть плитки: название, подпись, анимация включения."""
    icon = "monitor"
    enabled = False

    def __init__(self, title, sub):
        self.title = title
        self.default_sub = sub
        self.t = 0.0        # 0 - выключено, 1 - включено (плавная анимация)
        self.target = 0.0
        self.error = False
        self.boosted = False

    @property
    def sub(self):
        return self.default_sub

    def click(self):
        pass

    def check(self):
        pass


class FileTile(Tile):
    """Кнопка с фото или гифкой: ЛКМ открывает файл, ПКМ позволяет выбрать другой вместо текущего."""
    icon = "photo"
    enabled = True

    def __init__(self, root, path, kick):
        super().__init__("Фото", "Открыть · ПКМ: сменить")
        self.root = root
        self.path = path
        self.kick = kick
        self.err_text = "Нет файла"
        self.reset()

    def reset(self):
        """Сбросить загруженные кадры (после выбора нового файла)."""
        self.frames = None    # квадратные заготовки кадров (None - ещё не грузили, [] - не вышло)
        self.durs = []        # длительность каждого кадра, мс
        self.fi = 0           # номер текущего кадра
        self.t_next = None    # когда переключить на следующий кадр
        self._cache = {}      # готовые круглые превью: (диаметр, номер кадра)

    @property
    def sub(self):
        return self.err_text if self.error else self.default_sub

    # --- откуда брать файл ---
    @staticmethod
    def source_bytes():
        """Байты файла: сохранённый рядом (для exe) или вшитый в код. Иначе None (берём по пути)."""
        if getattr(sys, "frozen", False):
            for name in (PHOTO_FILE, "gamma_panel_photo.jpg"):
                side = os.path.join(app_dir(), name)
                if os.path.exists(side):
                    with open(side, "rb") as f:
                        return f.read()
        if PHOTO_B64:
            return base64.b64decode(PHOTO_B64)
        return None

    # --- кадры и превью ---
    def load(self):
        if self.frames is not None:
            return
        self.frames, self.durs = [], []
        try:
            from PIL import Image, ImageOps, ImageSequence
            data = self.source_bytes()
            img = Image.open(io.BytesIO(data) if data else self.path)
            if getattr(img, "n_frames", 1) > 1:        # гифка: берём кадры по очереди
                for i, fr in enumerate(ImageSequence.Iterator(img)):
                    if i >= MAX_FRAMES:
                        break
                    self.frames.append(ImageOps.fit(fr.convert("RGB"), (192, 192), Image.LANCZOS))
                    self.durs.append(max(30, int(fr.info.get("duration", 100) or 100)))
            else:
                img = ImageOps.exif_transpose(img).convert("RGB")
                self.frames.append(ImageOps.fit(img, (256, 256), Image.LANCZOS))
                self.durs.append(1000)
        except Exception:
            self.frames, self.durs = [], []

    @property
    def animated(self):
        self.load()
        return len(self.frames) > 1

    def advance(self, now):
        """Переключает кадр гифки, когда подошло время."""
        if not self.animated:
            return
        if self.t_next is None:
            self.t_next = now + self.durs[self.fi] / 1000.0
        elif now >= self.t_next:
            self.fi = (self.fi + 1) % len(self.frames)
            self.t_next = now + self.durs[self.fi] / 1000.0

    def thumb(self, size):
        """Круглое превью текущего кадра нужного диаметра в пикселях или None, если показать нельзя.
        Нужна библиотека Pillow: pip install pillow"""
        if size < 8:
            return None
        self.load()
        if not self.frames:
            return None
        key = (size, self.fi)
        if key not in self._cache:
            try:
                from PIL import Image, ImageDraw, ImageTk
                mask = Image.new("L", (size * 4, size * 4), 0)
                ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
                mask = mask.resize((size, size), Image.LANCZOS)
                img = self.frames[self.fi].resize((size, size), Image.LANCZOS).convert("RGBA")
                img.putalpha(mask)
                if len(self._cache) > 800:
                    self._cache.clear()
                self._cache[key] = ImageTk.PhotoImage(img)
            except Exception:
                self.frames = []
                return None
        return self._cache[key]

    def fail(self, text):
        self.err_text = text
        self.error = True
        self.root.after(2200, self._clear_error)
        self.kick()

    def _clear_error(self):
        self.error = False
        self.kick()

    # --- ЛКМ: открыть ---
    def click(self):
        try:
            data = self.source_bytes()
            if data:   # вшитый файл: кладём во временную папку и открываем
                ext = ".gif" if data[:4] == b"GIF8" else ".jpg"
                tmp = os.path.join(tempfile.gettempdir(), "gamma_panel_photo" + ext)
                with open(tmp, "wb") as f:
                    f.write(data)
                os.startfile(tmp)
            else:
                os.startfile(self.path)
        except Exception:
            self.fail("Нет файла")

    # --- ПКМ: выбрать другое фото или гифку вместо текущего ---
    def right_click(self):
        global PHOTO_B64
        owner = tk.Toplevel(self.root)       # невидимое окно-владелец, чтобы диалог был поверх всех
        owner.overrideredirect(True)
        owner.attributes("-topmost", True)
        owner.geometry("1x1+0+0")
        owner.update()
        try:
            path = filedialog.askopenfilename(
                parent=owner, title="Выбери фото или гифку для кнопки",
                filetypes=[("Изображения и гифки", "*.jpg *.jpeg *.png *.bmp *.webp *.gif"),
                           ("Все файлы", "*.*")])
        finally:
            owner.destroy()
        if not path:
            return
        try:
            from PIL import Image, ImageOps, ImageSequence
        except ImportError:
            self.fail("Нужен Pillow")
            return
        try:
            img = Image.open(path)
            if img.format == "GIF" and getattr(img, "is_animated", False):
                if os.path.getsize(path) <= MAX_GIF_BYTES:
                    with open(path, "rb") as f:
                        data = f.read()               # маленькую гифку кладём как есть
                else:                                 # большую уменьшаем, чтобы код не раздувался
                    frames, durs = [], []
                    for i, fr in enumerate(ImageSequence.Iterator(img)):
                        if i >= MAX_FRAMES:
                            break
                        f = fr.convert("RGB")
                        f.thumbnail((256, 256), Image.LANCZOS)
                        frames.append(f.copy())
                        durs.append(max(30, int(fr.info.get("duration", 100) or 100)))
                    buf = io.BytesIO()
                    frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:],
                                   duration=durs, loop=0, optimize=True)
                    data = buf.getvalue()
            else:
                img = ImageOps.exif_transpose(img).convert("RGB")
                img.thumbnail((1600, 1600), Image.LANCZOS)    # чтобы код/файл не раздувался
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=85, optimize=True)
                data = buf.getvalue()
            if getattr(sys, "frozen", False):
                # exe нельзя переписать изнутри: храним рядом с программой
                with open(os.path.join(app_dir(), PHOTO_FILE), "wb") as f:
                    f.write(data)
                old = os.path.join(app_dir(), "gamma_panel_photo.jpg")
                if os.path.exists(old):
                    os.remove(old)
            else:
                # скрипт: вшиваем файл прямо в строку PHOTO_B64 этого файла
                b64 = base64.b64encode(data).decode("ascii")
                me = os.path.abspath(__file__)
                with open(me, encoding="utf-8") as f:
                    code = f.read()
                code, n = re.subn(r'(?m)^PHOTO_B64 = ".*"$', lambda m: 'PHOTO_B64 = "' + b64 + '"', code, count=1)
                if n != 1:
                    raise RuntimeError("нет строки PHOTO_B64")
                with open(me, "w", encoding="utf-8") as f:
                    f.write(code)
                PHOTO_B64 = b64
        except Exception:
            self.fail("Не вышло")
            return
        self.reset()             # перезагрузить кадры
        self.kick()


class CrosshairTile(Tile):
    """Прицел по центру основного монитора: ЛКМ включает/выключает, ПКМ меняет стиль.
    Окно прицела прозрачное для кликов: игра и другие программы работают как обычно."""
    icon = "crosshair"
    enabled = True

    def __init__(self, root, mon, kick):
        super().__init__("Прицел", "Выкл")
        self.root = root
        self.mon = mon
        self.kick = kick
        self.win = None
        self.cv = None
        self.style = 0

    @property
    def sub(self):
        if self.win is not None:
            return "Вкл · " + CROSSHAIRS[self.style][0]
        return "Выкл · ПКМ: стиль"

    def click(self):
        if self.win is None:
            if not self.show():
                self.error = True
                self.root.after(2200, self._clear_error)
                self.kick()
                return
            self.boosted = True
            self.target = 1.0
        else:
            self.hide()
            self.boosted = False
            self.target = 0.0
        self.kick()

    def right_click(self):
        self.style = (self.style + 1) % len(CROSSHAIRS)
        if self.win is not None:
            self.paint()
        self.kick()

    def _clear_error(self):
        self.error = False
        self.kick()

    def show(self):
        try:
            half = u(22)
            cx = self.mon["x"] + self.mon["w"] // 2
            cy = self.mon["y"] + self.mon["h"] // 2
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.attributes("-topmost", True)
            win.configure(bg=KEY)
            win.attributes("-transparentcolor", KEY)
            win.geometry(geo(2 * half, 2 * half, cx - half, cy - half))
            cv = tk.Canvas(win, bg=KEY, highlightthickness=0, width=2 * half, height=2 * half)
            cv.pack()
            win.update_idletasks()
            App.make_noactivate(win, click_through=True)   # клики проходят сквозь прицел
            self.win, self.cv, self.half = win, cv, half
            self.paint()
            return True
        except Exception:
            self.hide()
            return False

    def hide(self):
        if self.win is not None:
            try:
                self.win.destroy()
            except Exception:
                pass
        self.win = self.cv = None

    def render(self):
        """Рисует прицел с настоящим сглаживанием (Pillow, 4-кратное разрешение) и возвращает картинку.
        Линии креста стоят ровно на границах пикселей, поэтому они чёткие, а круги без «лесенки»."""
        from PIL import Image, ImageChops, ImageDraw
        _, shape, col = CROSSHAIRS[self.style]
        h = self.half
        n, K = 2 * h, 4
        w = max(2, u(2))
        w += w % 2                      # чётная толщина: линия ложится ровно на сетку пикселей
        arm, gap = u(14), u(4)
        color = tuple(int(col[i:i + 2], 16) for i in (1, 3, 5))
        black = (16, 16, 16)            # чуть светлее фона-ключа, чтобы не стать прозрачным
        big = Image.new("RGBA", (n * K, n * K), (0, 0, 0, 0))
        d = ImageDraw.Draw(big)

        def rect(x1, y1, x2, y2, fill):
            d.rectangle([round(x1 * K), round(y1 * K), round(x2 * K) - 1, round(y2 * K) - 1], fill=fill)

        def disc(r, fill):
            d.ellipse([round((h - r) * K), round((h - r) * K), round((h + r) * K) - 1, round((h + r) * K) - 1],
                      fill=fill)

        def layer(grow, fill):          # grow=1: контур (на 1 пиксель шире), grow=0: сама фигура
            if shape in ("cross", "crossdot"):
                for x1, y1, x2, y2 in ((h - w / 2, h - gap - arm, h + w / 2, h - gap),
                                       (h - w / 2, h + gap, h + w / 2, h + gap + arm),
                                       (h - gap - arm, h - w / 2, h - gap, h + w / 2),
                                       (h + gap, h - w / 2, h + gap + arm, h + w / 2)):
                    rect(x1 - grow, y1 - grow, x2 + grow, y2 + grow, fill)
            if shape in ("dot", "crossdot"):
                disc((u(2) if shape == "crossdot" else u(3)) + grow, fill)
            if shape == "ring":
                r = u(10)
                ro = r + w / 2 + grow
                d.ellipse([round((h - ro) * K), round((h - ro) * K), round((h + ro) * K) - 1,
                           round((h + ro) * K) - 1], outline=fill, width=round((w + 2 * grow) * K))
                disc(max(2, u(1.5)) + grow, fill)

        layer(1, black + (255,))
        layer(0, color + (255,))
        small = big.resize((n, n), Image.LANCZOS)
        key = tuple(int(KEY[i:i + 2], 16) for i in (1, 3, 5))
        bg = Image.new("RGB", (n, n), key)
        bg.paste(small, mask=small.split()[3])
        # слабые краевые пиксели возвращаем в прозрачный цвет, чтобы не было серой каймы
        near = ImageChops.difference(bg, Image.new("RGB", (n, n), key)).convert("L").point(
            lambda v: 255 if v <= 8 else 0)
        bg.paste(key, mask=near)
        return bg

    def paint(self):
        try:
            from PIL import ImageTk
            self.img = ImageTk.PhotoImage(self.render())     # ссылка нужна, иначе Tk удалит картинку
            self.cv.delete("all")
            self.cv.create_image(0, 0, image=self.img, anchor="nw")
        except Exception:
            self.paint_tk()                                  # без Pillow рисуем средствами Tk

    def paint_tk(self):
        c, h = self.cv, self.half
        _, shape, col = CROSSHAIRS[self.style]
        w = max(2, u(2))
        arm, gap = u(14), u(4)
        c.delete("all")

        def line(x1, y1, x2, y2):          # чёрная подложка + цветная линия: видно на любом фоне
            c.create_line(x1, y1, x2, y2, fill="#000000", width=w + 2)
            c.create_line(x1, y1, x2, y2, fill=col, width=w)

        def dot(r):
            c.create_oval(h - r - 1, h - r - 1, h + r + 1, h + r + 1, fill="#000000", outline="")
            c.create_oval(h - r, h - r, h + r, h + r, fill=col, outline="")

        if shape in ("cross", "crossdot"):
            line(h, h - gap - arm, h, h - gap)
            line(h, h + gap, h, h + gap + arm)
            line(h - gap - arm, h, h - gap, h)
            line(h + gap, h, h + gap + arm, h)
        if shape in ("dot", "crossdot"):
            dot(u(2) if shape == "crossdot" else u(3))
        if shape == "ring":
            r = u(10)
            c.create_oval(h - r, h - r, h + r, h + r, outline="#000000", width=w + 2)
            c.create_oval(h - r, h - r, h + r, h + r, outline=col, width=w)
            dot(u(1.5))

    def check(self):
        """Прицел всегда поверх остальных окон."""
        if self.win is not None:
            try:
                hwnd = user32.GetAncestor(self.win.winfo_id(), 2) or self.win.winfo_id()
                raise_top(hwnd)
            except Exception:
                pass


class KeysTile(Tile):
    """Кнопка выполняет сценарий: нажатия клавиш, паузы и клики мыши (все шаги по очереди
    или по одному за нажатие). ПКМ открывает окно, где сценарий редактируется."""
    icon = "keys"
    enabled = True

    def __init__(self, root, mon, kick):
        super().__init__("Клавиши", "Не задано")
        self.root = root
        self.mon = mon
        self.kick = kick
        self.err_text = "Ошибка"
        self.running = False
        self.prog = ""             # что сейчас выполняется (для подсказки)
        self.token = 0             # номер запуска: позволяет остановить сценарий
        self.dlg = None
        self.i = 0                 # какой шаг выполнится следующим (режим «по одному»)
        self.combos, self.step, self.delay = list(DEFAULT_STEPS), False, 1200
        self.looping = False
        self.run_lines = []        # строки сценария, которые выполняются сейчас (для журнала)
        self.start_delay = 5.0     # пауза перед самым первым действием, секунды
        self.loop = True           # зацикливать сценарий (стоп: пробел или повторный клик)
        self.hold = 80             # сколько мс удерживать клавишу/кнопку мыши (для игр)
        self.load()

    @property
    def sub(self):
        if self.error:
            return self.err_text
        if self.running:
            return self.prog or "Выполняю…"
        n = len(self.combos)
        if n == 0:
            return "Не задано · ПКМ"
        if self.step:
            return f"Далее {self.i % n + 1} из {n} · ПКМ"
        mode = "цикл" if self.loop else "1 раз"
        return f"{n} шаг., {mode} · СКМ: один раз"

    # --- хранение настроек ---
    def _path(self):
        return os.path.join(app_dir(), KEYS_FILE)

    def load(self):
        try:
            with open(self._path(), encoding="utf-8") as f:
                d = json.load(f)
            self.combos = [str(c) for c in d.get("combos", [])]
            old_default = self.combos in OLD_DEFAULT_STEPS
            if old_default:
                self.combos = list(DEFAULT_STEPS)    # прежний сценарий по умолчанию обновляем
            self.step = bool(d.get("step", False))
            self.delay = int(d.get("delay", 200))
            add_second = int(d.get("v", 1)) < 2
            if add_second:
                self.delay += 1000       # однократно добавляем секунду к паузе между шагами
            self.delay = max(0, min(10000, self.delay))
            self.start_delay = max(0.0, min(600.0, float(d.get("start", 5))))
            self.hold = max(10, min(1000, int(d.get("hold", 80))))
            self.loop = bool(d.get("loop", True))
            if add_second or old_default:
                self.save()              # записываем сразу, чтобы изменения не повторялись
        except Exception:
            pass

    def save(self):
        try:
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump({"v": 2, "combos": self.combos, "step": self.step, "delay": self.delay,
                           "start": self.start_delay, "hold": self.hold, "loop": self.loop}, f,
                          ensure_ascii=False)
        except Exception:
            pass

    def fail(self, text):
        self.err_text = text
        self.error = True
        self.running = False
        self.target = 0.0
        self.root.after(2200, self._clear_error)
        self.kick()

    def _clear_error(self):
        self.error = False
        self.kick()

    # --- ЛКМ: выполнить сценарий (повторное нажатие во время выполнения останавливает его) ---
    def click_once(self):
        """Одиночный запуск: весь сценарий один раз, даже если включено зацикливание."""
        if self.running:
            self.click()                 # во время выполнения — просто остановить
            return
        self.click(once=True)

    def click(self, once=False):
        if self.running:
            self.token += 1
            self.running = False
            self.target = 0.0
            self.kick()
            return
        if not self.combos:
            self.right_click()           # ещё ничего не задано: сразу открываем настройку
            return
        n = len(self.combos)
        if self.step:
            idx = self.i % n
            todo = [self.combos[idx]]
            self.i = (idx + 1) % n
        else:
            todo = list(self.combos)
        steps = [parse_step(c) for c in todo]
        self.run_lines = todo
        if any(v is None for v in steps):
            self.fail("Ошибка в шаге")
            return
        self.token += 1
        self.running = True
        self.target = 1.0
        self.kick()
        self.looping = self.loop and not self.step and not once
        self._watch(self.token)
        if self.start_delay > 0:
            self._start(steps, self.token, time.monotonic() + jit(self.start_delay * 1000) / 1000.0)
        else:
            self._run(steps, 0, self.token)

    def stop(self, why=""):
        """Принудительная остановка сценария."""
        self.token += 1
        self.running = False
        self.target = 0.0
        if why:
            self.log(why)
        self.kick()

    def _watch(self, token):
        """Следит за пробелом: нажатие в любой программе останавливает сценарий."""
        if token != self.token or not self.running:
            return
        if user32.GetAsyncKeyState(0x20) < 0:
            self.stop("остановлено пробелом")
            return
        self.root.after(30, lambda: self._watch(token))

    def _start(self, steps, token, end_t):
        """Ждёт заданное время перед первым действием и показывает обратный отсчёт."""
        if token != self.token:
            return                       # запуск отменили
        left = end_t - time.monotonic()
        if left <= 0:
            self._run(steps, 0, token)
            return
        self.prog = f"Старт через {math.ceil(left)} с · пробел — стоп"
        self.kick()
        self.root.after(min(200, int(left * 1000) + 1), lambda: self._start(steps, token, end_t))

    def combos_text(self, k):
        return self.run_lines[k] if k < len(self.run_lines) else "?"

    def log(self, text):
        """Журнал шагов сценария: gamma_keys_log.txt рядом с программой (помогает понять, почему
        игра не реагирует: куда ушли нажатия и приняла ли их Windows)."""
        try:
            with open(os.path.join(app_dir(), "gamma_keys_log.txt"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {text}\n")
        except Exception:
            pass

    def _run(self, steps, k, token):
        """Выполняет шаги по одному; паузы идут через after, поэтому интерфейс не зависает."""
        if token != self.token:
            return                       # сценарий остановили
        if k >= len(steps) and self.looping:
            self.log("--- новый круг ---")
            self.root.after(jit_up(self.delay), lambda: self._run(steps, 0, token))
            return
        if k >= len(steps):
            self.running = False
            self.target = 0.0
            self.kick()
            return
        st = steps[k]
        wait_ms = None
        try:
            where = foreground_info()
            if st[0] == "keys":
                ok = tap_keys(st[1], self.hold / 1000.0)
                self.prog = f"Шаг {k + 1} из {len(steps)}"
                self.log(f"шаг {k + 1}: клавиши {self.combos_text(k)}, Windows приняла: {'да' if ok else 'НЕТ'}, {where}")
            elif st[0] == "click":
                pos = resolve_pos(st[3]) if st[3] else None
                ok = click_mouse(st[1], st[2], pos, self.hold / 1000.0)
                self.prog = f"Шаг {k + 1} из {len(steps)}"
                self.log(f"шаг {k + 1}: клик мыши ({st[1]}) в {pos or 'текущем месте'}, Windows приняла: {'да' if ok else 'НЕТ'}, {where}")
            elif st[0] == "move":
                pos = resolve_pos(st[1])
                ok = move_mouse(pos)
                self.prog = f"Шаг {k + 1} из {len(steps)}"
                self.log(f"шаг {k + 1}: мышь в точку {pos}, Windows приняла: {'да' if ok else 'НЕТ'}")
            else:
                wait_ms = jit(st[1] * 1000)
                self.prog = f"Жду {wait_ms / 1000:.2f} с · пробел — стоп"
                self.log(f"шаг {k + 1}: пауза {wait_ms / 1000:.2f} с (по сценарию {st[1]:g} с)")
        except Exception as e:
            self.log(f"шаг {k + 1}: ошибка {e!r}")
            self.fail("Не вышло")
            return
        self.kick()
        if wait_ms is not None:
            delay = wait_ms                      # сама пауза и есть задержка перед следующим шагом
        else:
            delay = jit_up(self.delay) if k + 1 < len(steps) else 180
        self.root.after(delay, lambda: self._run(steps, k + 1, token))

    # --- ПКМ: окно настройки ---
    def right_click(self):
        if self.dlg is not None:
            try:
                self.dlg.lift()
                self.dlg.focus_force()
                return
            except Exception:
                self.dlg = None
        dlg = tk.Toplevel(self.root)
        self.dlg = dlg
        dlg.title("Сценарий кнопки")
        dlg.configure(bg=PANEL)
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        def label(text, fg=TEXT, font=(FONT, 9), **kw):
            lb = tk.Label(dlg, text=text, bg=PANEL, fg=fg, font=font, justify="left", anchor="w", **kw)
            lb.pack(fill="x", padx=14, pady=(0, 4))
            return lb

        tk.Frame(dlg, bg=PANEL, height=10).pack()
        label("Сценарий кнопки", font=(FONT, 11, "bold"))
        label("По одному шагу на строку. Шаги выполняются сверху вниз.", fg=MUTED, wraplength=360)
        text = tk.Text(dlg, width=40, height=9, bg=TILE_OFF, fg=TEXT, insertbackground=TEXT,
                       relief="flat", font=("Consolas", 11), padx=8, pady=6)
        text.pack(padx=14, pady=(0, 8))
        text.insert("1.0", "\n".join(self.combos))

        var_step = tk.BooleanVar(value=self.step)
        tk.Checkbutton(dlg, text="Один шаг за нажатие (по очереди)", variable=var_step,
                       bg=PANEL, fg=TEXT, activebackground=PANEL, activeforeground=TEXT,
                       selectcolor=TILE_OFF, font=(FONT, 9)).pack(anchor="w", padx=10)

        var_loop = tk.BooleanVar(value=self.loop)
        tk.Checkbutton(dlg, text="Зациклить сценарий (стоп — пробел)", variable=var_loop,
                       bg=PANEL, fg=TEXT, activebackground=PANEL, activeforeground=TEXT,
                       selectcolor=TILE_OFF, font=(FONT, 9)).pack(anchor="w", padx=10)

        row0 = tk.Frame(dlg, bg=PANEL)
        row0.pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(row0, text="Задержка перед первым действием, сек:", bg=PANEL, fg=TEXT,
                 font=(FONT, 9)).pack(side="left")
        var_start = tk.StringVar(value=f"{self.start_delay:g}")
        tk.Spinbox(row0, from_=0, to=600, increment=1, width=6, textvariable=var_start,
                   bg=TILE_OFF, fg=TEXT, buttonbackground=TILE_OFF, relief="flat").pack(side="left", padx=6)

        row = tk.Frame(dlg, bg=PANEL)
        row.pack(fill="x", padx=14, pady=(4, 6))
        tk.Label(row, text="Пауза между шагами, мс:", bg=PANEL, fg=TEXT,
                 font=(FONT, 9)).pack(side="left")
        var_delay = tk.StringVar(value=str(self.delay))
        tk.Spinbox(row, from_=0, to=10000, increment=50, width=6, textvariable=var_delay,
                   bg=TILE_OFF, fg=TEXT, buttonbackground=TILE_OFF, relief="flat").pack(side="left", padx=6)

        row2 = tk.Frame(dlg, bg=PANEL)
        row2.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(row2, text="Удержание клавиши/клика, мс (для игр):", bg=PANEL, fg=TEXT,
                 font=(FONT, 9)).pack(side="left")
        var_hold = tk.StringVar(value=str(self.hold))
        tk.Spinbox(row2, from_=10, to=1000, increment=10, width=6, textvariable=var_hold,
                   bg=TILE_OFF, fg=TEXT, buttonbackground=TILE_OFF, relief="flat").pack(side="left", padx=6)

        label("Шаги: клавиши (1, f11, ctrl+alt+2, win+d, enter ...), пауза (wait 6 = 6 секунд, "
              "wait 500ms), клик мыши (click = левая кнопка в текущем месте курсора, rclick, mclick, "
              "dblclick), мышь по координатам: move 1200 600 (пиксели экрана) или move c 34.4% 33.9% "
              "(от центра основного монитора, можно в пикселях: move c 300 -150); "
              "click тоже принимает координаты: click c 34.4% 33.9%.",
              fg=FAINT, font=(FONT, 8), wraplength=360)
        err = label("", fg=ERR)

        def close():
            self.dlg = None
            dlg.destroy()

        def save():
            lines = [ln.strip() for ln in text.get("1.0", "end").splitlines() if ln.strip()]
            for ln in lines:
                if parse_step(ln) is None:
                    err.config(text=f"Не понял: {ln}")
                    return
            try:
                delay = max(0, min(10000, int(var_delay.get())))
            except ValueError:
                err.config(text="Пауза должна быть числом")
                return
            try:
                start = max(0.0, min(600.0, float(var_start.get().replace(",", "."))))
            except ValueError:
                err.config(text="Задержка перед запуском должна быть числом")
                return
            try:
                hold = max(10, min(1000, int(var_hold.get())))
            except ValueError:
                err.config(text="Удержание должно быть числом")
                return
            self.combos, self.step, self.delay, self.i = lines, var_step.get(), delay, 0
            self.start_delay, self.hold, self.loop = start, hold, var_loop.get()
            self.save()
            close()
            self.kick()

        btns = tk.Frame(dlg, bg=PANEL)
        btns.pack(fill="x", padx=14, pady=(2, 12))
        tk.Button(btns, text="Сохранить", command=save, bg=ACCENT, fg=INK, relief="flat",
                  font=(FONT, 9, "bold"), padx=14, pady=3).pack(side="right")
        tk.Button(btns, text="Отмена", command=close, bg=TILE_OFF, fg=TEXT, relief="flat",
                  font=(FONT, 9), padx=12, pady=3).pack(side="right", padx=8)
        dlg.protocol("WM_DELETE_WINDOW", close)

        dlg.update_idletasks()
        w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
        m = self.mon
        dlg.geometry(geo(w, h, m["x"] + (m["w"] - w) // 2, m["y"] + (m["h"] - h) // 2))
        dlg.focus_force()
        text.focus_set()


class MonitorTile(Tile):
    icon = "monitor"
    enabled = True

    def __init__(self, root, mon, kick):
        super().__init__(f"Монитор {mon['num']}", "Основной" if mon["primary"] else "Доп.")
        self.root = root
        self.mon = mon
        self.kick = kick      # просит панель перерисоваться
        self.original = None
        self.mode = None      # "gamma" или "overlay"
        self.boosted = False
        self.label = ""
        self.overlay = None
        self.applied = None   # последняя применённая гамма-таблица
        self.applied_rb = None   # что вернул драйвер сразу после установки

    @property
    def sub(self):
        if self.error:
            return "Ошибка"
        return self.label if self.boosted else self.default_sub

    def dc(self):
        return gdi32.CreateDCW("DISPLAY", self.mon["device"], None, None) or None

    # --- способ 1: гамма. Возвращает подпись или None ---
    def gamma_on(self):
        hdc = self.dc()
        if not hdc:
            return None
        try:
            if self.original is None:
                buf = Ramp()
                if gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(buf)):
                    self.original = buf
                else:
                    self.original = make_ramp(1.0)  # линейная = стандартная
            for g in GAMMA_CANDIDATES:
                ramp = make_ramp(g)
                if gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(ramp)):
                    self.applied = ramp
                    rb = Ramp()
                    self.applied_rb = rb if gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(rb)) else None
                    return f"Гамма x{g:g}"
            return None
        finally:
            gdi32.DeleteDC(hdc)

    def gamma_off(self):
        hdc = self.dc()
        if hdc:
            gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(self.original))
            gdi32.DeleteDC(hdc)

    # --- способ 2: светлая плёнка. Возвращает подпись или None ---
    def overlay_on(self):
        m = self.mon
        try:
            win = tk.Toplevel(self.root)
            win.overrideredirect(True)
            win.configure(bg="white")
            win.geometry(geo(m["w"], m["h"], m["x"], m["y"]))
            win.attributes("-topmost", True)
            win.attributes("-alpha", OVERLAY_ALPHA)
            win.update()
            # пропускаем клики сквозь плёнку
            hwnd = user32.GetAncestor(win.winfo_id(), 2) or win.winfo_id()
            style = user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            style |= 0x00080000 | 0x00000020 | 0x00000080 | 0x08000000  # LAYERED|TRANSPARENT|TOOLWINDOW|NOACTIVATE
            user32.SetWindowLongW(hwnd, -20, style)
            self.overlay = win
            self.root.attributes("-topmost", True)
            self.root.lift()
            return "Запасной"
        except Exception:
            self.overlay_off()
            return None

    def overlay_off(self):
        if self.overlay is not None:
            try:
                self.overlay.destroy()
            except Exception:
                pass
            self.overlay = None

    # --- нажатие на плитку ---
    def click(self):
        if not self.boosted:
            label = self.gamma_on()
            if label:
                self.mode = "gamma"
            else:
                label = self.overlay_on()
                if label:
                    self.mode = "overlay"
            if not label:
                self.error = True
                self.root.after(2200, self._clear_error)
                self.kick()
                return
            self.boosted = True
            self.label = label
            self.target = 1.0
        else:
            self.restore()
            self.target = 0.0
        self.kick()

    def _clear_error(self):
        self.error = False
        self.kick()

    def restore(self):
        if self.mode == "gamma":
            self.gamma_off()
        elif self.mode == "overlay":
            self.overlay_off()
        self.boosted = False

    def log(self, text):
        """Пишет события в gamma_log.txt рядом с программой (помогает найти причину морганий)."""
        try:
            base = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                                   else os.path.abspath(__file__))
            with open(os.path.join(base, "gamma_log.txt"), "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  монитор {self.mon['num']}: {text}\n")
        except Exception:
            pass

    def check(self):
        """Раз в пару секунд: если гамму сбросили или плёнку перекрыли, возвращаем как было."""
        if not self.boosted:
            return
        if self.mode == "gamma" and self.applied is not None:
            hdc = self.dc()
            if not hdc:
                return
            try:
                cur = Ramp()
                # если гамму прочитать нельзя, ничего не трогаем (чтобы не мигать самим)
                if gdi32.GetDeviceGammaRamp(hdc, ctypes.byref(cur)):
                    # драйвер может чуть округлять таблицу, поэтому сравниваем с допуском и с тем,
                    # что вернул сам драйвер сразу после установки (иначе гамма переставлялась бы
                    # каждые 2 секунды, и монитор мигал)
                    base = self.applied_rb if self.applied_rb is not None else self.applied
                    diff = max(abs(cur[c][i] - base[c][i]) for c in range(3) for i in range(0, 256, 8))
                    if diff > 3000:
                        gdi32.SetDeviceGammaRamp(hdc, ctypes.byref(self.applied))
                        self.log(f"гамма была сброшена (отличие {diff}), применена снова")
            finally:
                gdi32.DeleteDC(hdc)
        elif self.mode == "overlay" and self.overlay is not None:
            try:
                flags = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE
                hwnd = user32.GetAncestor(self.overlay.winfo_id(), 2) or self.overlay.winfo_id()
                raise_top(hwnd)
                root_hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
                raise_top(root_hwnd)
            except Exception:
                pass


# ---------- панель ----------
def app_dir():
    return os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                           else os.path.abspath(__file__))


class App:
    A_VEC = {"left": (1, 0), "right": (-1, 0), "top": (0, 1), "bottom": (0, -1)}

    def __init__(self, root):
        self.root = root
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=KEY)
        root.attributes("-transparentcolor", KEY)
        try:
            winmm.timeBeginPeriod(1)   # точные таймеры, иначе кадры не чаще ~64 Гц
        except Exception:
            pass
        self.last_tick = None
        self.f_bold = tkfont.Font(root=root, family=FONT, size=9, weight="bold")
        self.f_norm = tkfont.Font(root=root, family=FONT, size=8)
        self.f_num = tkfont.Font(root=root, family=FONT, size=7, weight="bold")

        self.mons = list_monitors()
        if not self.mons:  # запасной вариант, если список мониторов получить не удалось
            sw, sh = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            self.mons = [{"device": None, "x": 0, "y": 0, "w": sw, "h": sh, "primary": True,
                          "num": 1, "work": (0, 0, sw, sh)}]
        self.monitor_tiles = [MonitorTile(root, m, self.kick) for m in self.mons]
        prim_mon = next((m for m in self.mons if m["primary"]), self.mons[0])
        self.tiles = self.monitor_tiles + [FileTile(root, PHOTO_PATH, self.kick),
                                           CrosshairTile(root, prim_mon, self.kick),
                                           KeysTile(root, prim_mon, self.kick)]
        self.n = len(self.tiles)
        self.compute_fan()

        self.e = 0.0               # 0 - кнопки спрятаны, 1 - кнопки раскрыты
        self.e_target = 0.0
        self.last_inside = 0.0
        self.enter_t = None
        self.hover_tile = None
        self.job = None
        self.mode = "docked"       # "docked" - у края, "drag" - тащим, "snap" - прилипает к краю
        self.expanded = False
        self.press = None
        self.snap = None
        self.ghost = None
        self.ghost_key = None

        # где стоит язычок: из сохранённых настроек или слева сверху
        prim = next((m for m in self.mons if m["primary"]), self.mons[0])
        cfg = self.load_cfg()
        mon = next((m for m in self.mons if cfg.get("device") and m["device"] == cfg["device"]), prim)
        edge = cfg.get("edge") if cfg.get("edge") in self.A_VEC else "left"
        lo, hi = self.edge_range(edge, mon)
        try:
            pos = lo + float(cfg["ratio"]) * (hi - lo)
        except Exception:
            edge, mon = "left", prim
            pos = prim["work"][1] + HANDLE_Y + HR
        self.dock(edge, pos, mon)

        self.canvas = tk.Canvas(root, bg=KEY, highlightthickness=0, width=HR, height=2 * HR)
        self.canvas.pack(fill="both", expand=True)
        self.apply_geometry()
        root.update_idletasks()
        self.make_noactivate(root)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="Выход", command=self.on_close)

        c = self.canvas
        c.bind("<Motion>", self.on_motion)
        c.bind("<Leave>", self.on_leave)
        c.bind("<Button-1>", self.on_press)
        c.bind("<B1-Motion>", self.on_drag)
        c.bind("<ButtonRelease-1>", self.on_release)
        c.bind("<Button-3>", self.on_right)
        c.bind("<Button-2>", self.on_middle)

        self.draw()
        root.after(40, self.poll)
        root.after(2000, self.watch)

    # ---------- настройки ----------
    def load_cfg(self):
        try:
            with open(os.path.join(app_dir(), CONFIG_NAME), encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_cfg(self):
        try:
            lo, hi = self.edge_range(self.edge, self.mon)
            data = {"edge": self.edge, "device": self.mon["device"],
                    "ratio": (self.pos - lo) / max(1, hi - lo)}
            with open(os.path.join(app_dir(), CONFIG_NAME), "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    @staticmethod
    def make_noactivate(win, click_through=False):
        """Окно не забирает фокус у других программ и не видно в Alt+Tab."""
        try:
            hwnd = user32.GetAncestor(win.winfo_id(), 2) or win.winfo_id()
            style = user32.GetWindowLongW(hwnd, -20)
            style |= 0x00000080 | 0x08000000  # TOOLWINDOW | NOACTIVATE
            if click_through:
                style |= 0x00080000 | 0x00000020  # LAYERED | TRANSPARENT
            user32.SetWindowLongW(hwnd, -20, style)
        except Exception:
            pass

    # ---------- веер кнопок ----------
    def compute_fan(self):
        """Кнопки стоят полукругом вокруг язычка: считаем радиус и углы."""
        n = self.n
        if n > 1:
            step = math.radians(min(FAN_STEP, 2 * FAN_HALF / (n - 1)))
            r_need = (D + u(10)) / (2 * math.sin(step / 2))
        else:
            step, r_need = 0.0, 0.0
        self.R = max(HR + D / 2 + u(16), r_need)
        self.phis = [(i - (n - 1) / 2) * step for i in range(n)]
        # насколько веер выступает вдоль края (чтобы не упираться в угол экрана)
        self.btn_b = max(abs(self.R * math.sin(p)) for p in self.phis) + D / 2 + u(6)

    def fan_extents(self, row):
        """Размер окна под веер вместе с подсказками (в осях: a - вглубь экрана, b - вдоль края)."""
        pa = TIP_W if row else TIP_H
        pb = TIP_H if row else TIP_W
        a_hi = HR + u(6)
        b_ext = HR + u(6)
        for phi in self.phis:
            da, db = self.R * math.cos(phi), self.R * math.sin(phi)
            a_hi = max(a_hi, da + D / 2 + u(6))
            b_ext = max(b_ext, abs(db) + D / 2 + u(6))
            ua, ub = math.cos(phi), math.sin(phi)
            off = D / 2 + u(8) + abs(ua) * pa / 2 + abs(ub) * pb / 2
            a_hi = max(a_hi, da + ua * off + pa / 2 + u(4))
            b_ext = max(b_ext, abs(db + ub * off) + pb / 2 + u(4))
        return a_hi, b_ext

    # ---------- геометрия ----------
    @staticmethod
    def edge_range(edge, mon):
        l, t, r, b = mon["work"]
        return (t, b) if edge in ("left", "right") else (l, r)

    def clamp_pos(self, edge, pos, mon):
        lo, hi = self.edge_range(edge, mon)
        m = max(HR + u(6), self.btn_b + u(2))     # чтобы веер не вылезал за угол экрана
        return max(lo + m, min(hi - m, pos))

    @staticmethod
    def edge_point(edge, pos, mon):
        l, t, r, b = mon["work"]
        if edge == "left":
            return l, pos
        if edge == "right":
            return r, pos
        if edge == "top":
            return pos, t
        return pos, b

    def dock(self, edge, pos, mon):
        """Прикрепляет язычок к краю рабочей области монитора (не заходит под панель задач)."""
        self.edge, self.mon = edge, mon
        self.a = self.A_VEC[edge]                          # направление «внутрь» экрана
        self.b = (abs(self.a[1]), abs(self.a[0]))          # направление вдоль края
        self.row = edge in ("left", "right")
        self.pos = self.clamp_pos(edge, pos, mon)
        self.hx, self.hy = self.edge_point(edge, self.pos, mon)
        self.a_ext, self.b_ext = self.fan_extents(self.row)
        self.hz = refresh_rate(mon["device"])
        self.frame_ms = max(1, int(round(1000 / self.hz)))

    def rect(self, a0, a1, b0, b1):
        """Прямоугольник на экране: a - вглубь от края, b - вдоль края (от точки крепления)."""
        ax, ay = self.a
        bx, by = self.b
        p = (self.hx + ax * a0 + bx * b0, self.hy + ay * a0 + by * b0)
        q = (self.hx + ax * a1 + bx * b1, self.hy + ay * a1 + by * b1)
        return min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1])

    def apply_geometry(self):
        if self.expanded:
            x1, y1, x2, y2 = self.rect(0, self.a_ext, -self.b_ext, self.b_ext)
        else:
            x1, y1, x2, y2 = self.rect(0, HR, -HR, HR)
        self.wx, self.wy = int(round(x1)), int(round(y1))
        w, h = int(round(x2 - x1)), int(round(y2 - y1))
        self.root.geometry(geo(w, h, self.wx, self.wy))

    def set_circle(self, cx, cy):
        """Окно-круг вокруг точки (когда язычок тащат или он прилипает)."""
        self.root.geometry(geo(2 * HR, 2 * HR, cx - HR, cy - HR))

    def hc(self):
        """Центр язычка в координатах окна."""
        return self.hx - self.wx, self.hy - self.wy

    def button_dir(self, i):
        phi = self.phis[i]
        ca, sa = math.cos(phi), math.sin(phi)
        return (self.a[0] * ca + self.b[0] * sa, self.a[1] * ca + self.b[1] * sa)

    def button_center(self, i):
        hx, hy = self.hc()
        dx, dy = self.button_dir(i)
        return hx + dx * self.R, hy + dy * self.R

    # ---------- анимация (по реальному времени: одинаковая скорость на 60, 144, 240 Гц) ----------
    def kick(self):
        if self.job is None:
            self.tick()

    def tick(self):
        now = time.perf_counter()
        dt = (now - self.last_tick) if self.last_tick is not None else 1.0 / self.hz
        dt = max(0.0, min(dt, 0.05))
        self.last_tick = now
        k_tile = 1 - math.exp(-dt / TAU_TILE)
        k_panel = 1 - math.exp(-dt / TAU_PANEL)

        moving = False
        for t in self.tiles:
            d = t.target - t.t
            if abs(d) > 0.004:
                t.t += d * k_tile
                moving = True
            else:
                t.t = t.target

        if self.mode == "docked":
            d = self.e_target - self.e
            if abs(d) > 0.004:
                self.e += d * k_panel
                moving = True
            else:
                self.e = self.e_target
            want = self.e > 0.001
            if want != self.expanded:
                self.expanded = want
                self.apply_geometry()
        elif self.mode == "snap":
            sn = self.snap
            sn["p"] = min(1.0, sn["p"] + dt / 0.18)
            q = 1 - (1 - sn["p"]) ** 3
            self.set_circle(sn["x0"] + (sn["x1"] - sn["x0"]) * q, sn["y0"] + (sn["y1"] - sn["y0"]) * q)
            moving = True
            if sn["p"] >= 1.0:
                self.mode = "docked"
                self.snap = None
                self.e = self.e_target = 0.0
                self.expanded = False
                self.apply_geometry()

        anim = False        # крутится ли гифка на раскрытой кнопке
        if self.mode == "docked" and self.expanded and self.e > 0.3:
            for t in self.tiles:
                if getattr(t, "animated", False):
                    t.advance(now)
                    anim = True

        self.draw()
        if moving:
            self.job = self.root.after(self.frame_ms, self.tick)
        elif anim:
            self.job = self.root.after(33, self.tick)    # гифке хватает ~30 кадров/с
        else:
            self.job = None
            self.last_tick = None

    # ---------- отрисовка ----------
    def draw(self):
        c = self.canvas
        c.delete("all")
        active = 1.0 if any(t.boosted for t in self.monitor_tiles) else 0.0

        if self.mode in ("drag", "snap"):
            # язычок целиком (круг) - пока его тащат или он прилипает к краю
            c.create_oval(1, 1, 2 * HR - 1, 2 * HR - 1, fill=PANEL, outline=ACCENT, width=2)
            for dy in (-u(6), 0, u(6)):
                c.create_oval(HR - u(2), HR + dy - u(2), HR + u(2), HR + dy + u(2), fill=ACCENT, outline="")
            return

        hx, hy = self.hc()
        if self.expanded and self.e > 0.001:
            self.draw_buttons(c, hx, hy)

        # язычок: половина круга у края (вторая половина обрезается краем окна)
        c.create_oval(hx - HR, hy - HR, hx + HR, hy + HR, fill=PANEL,
                      outline=mix(BORDER, ACCENT, active), width=2)
        ax, ay = self.a
        bx, by = self.b
        k = u(5)
        ccx, ccy = hx + ax * HR * 0.5, hy + ay * HR * 0.5   # стрелка смотрит вглубь экрана
        pts = [ccx - ax * k / 2 - bx * k, ccy - ay * k / 2 - by * k,
               ccx + ax * k / 2, ccy + ay * k / 2,
               ccx - ax * k / 2 + bx * k, ccy - ay * k / 2 + by * k]
        c.create_line(*pts, fill=mix(MUTED, ACCENT, active), width=max(2, u(2)),
                      capstyle="round", joinstyle="round")

    def draw_buttons(self, c, hx, hy):
        n = self.n
        for i, t in enumerate(self.tiles):
            fx, fy = self.button_center(i)
            p = max(0.0, min(1.0, self.e * (1 + STAGGER * (n - 1)) - i * STAGGER))
            if p <= 0.02:
                continue
            q = 1 - (1 - p) ** 3                       # плавное «вылетание» из язычка
            cx, cy = hx + (fx - hx) * q, hy + (fy - hy) * q
            self.draw_button(c, t, cx, cy, D / 2 * (0.4 + 0.6 * q))

        if self.hover_tile is not None and self.e > 0.8:
            i = self.tiles.index(self.hover_tile)
            self.draw_tip(c, self.hover_tile, i)

    def draw_button(self, c, t, cx, cy, r):
        """Кнопка: тень, тёмная окантовка, объёмный градиент, блик и иконка монитора с номером."""
        sc = r / (D / 2)
        g = lambda v: u(v) * sc
        hov = t is self.hover_tile and t.enabled
        base = mix(TILE_HOV if hov else TILE_OFF, ACCENT, t.t)
        ring = ERR if t.error else mix(mix(BORDER, "#56627D", 1.0 if hov else 0.0), ACCENT, t.t)

        # тень и окантовка (тёмное кольцо скрывает неровный край на любых обоях)
        rb = r + g(3)
        c.create_oval(cx - rb, cy - rb + g(3), cx + rb, cy + rb + g(3), fill=SHADOW, outline="")
        c.create_oval(cx - rb, cy - rb, cx + rb, cy + rb, fill=PANEL, outline=ring,
                      width=max(1, int(g(2))))

        # кнопка с превью файла: круглая картинка вместо заливки и иконки
        if t.icon == "photo":
            img = t.thumb(int(2 * (r - g(1))))
            if img is not None:
                c.create_image(cx, cy, image=img)
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill="", outline=ring,
                              width=max(1, int(g(2))))
                return

        # объёмная заливка: светлое пятно смещается к верхнему левому краю
        dark, light = mix(base, "#000000", 0.30), mix(base, "#FFFFFF", 0.24)
        steps = 8
        for k in range(steps):
            f = k / (steps - 1)
            rr = r * (1 - 0.8 * f)
            ox, oy = -r * 0.22 * f, -r * 0.28 * f
            c.create_oval(cx + ox - rr, cy + oy - rr, cx + ox + rr, cy + oy + rr,
                          fill=mix(dark, light, f), outline="")
        # блик по верхней дуге
        q = r * 0.8
        c.create_arc(cx - q, cy - q, cx + q, cy + q, start=45, extent=90, style="arc",
                     outline=mix(base, "#FFFFFF", 0.42), width=max(1, int(g(2))))

        body = mix(GLYPH_OFF if t.enabled else FAINT, INK, t.t)
        if t.icon == "photo":
            # рамка фото: небо, солнце и горы
            rrect(c, cx - g(13), cy - g(11), cx + g(13), cy + g(11), g(3), fill=body, outline="")
            c.create_rectangle(cx - g(10), cy - g(8), cx + g(10), cy + g(8), fill="#151A25", outline="")
            c.create_oval(cx + g(2), cy - g(6), cx + g(7), cy - g(1), fill="#F0BE4D", outline="")
            c.create_polygon(cx - g(10), cy + g(8), cx - g(4), cy - g(1), cx + g(1), cy + g(5),
                             cx + g(5), cy + g(1), cx + g(10), cy + g(8), fill="#7C8AA6", outline="")
            return
        if t.icon == "keys":
            rrect(c, cx - g(14), cy - g(9), cx + g(14), cy + g(9), g(3), fill=body, outline="")
            cap = mix("#151A25", "#FFF4CC", t.t)
            for row, y in enumerate((-6, -1.5)):
                for k in range(5):
                    x = -11 + k * 4.6
                    c.create_rectangle(cx + g(x), cy + g(y), cx + g(x + 3.4), cy + g(y + 3), fill=cap, outline="")
            c.create_rectangle(cx - g(7), cy + g(3.5), cx + g(7), cy + g(6.5), fill=cap, outline="")
            return
        if t.icon == "crosshair":
            r1 = g(9)
            w = max(2, int(g(2)))
            c.create_oval(cx - r1, cy - r1, cx + r1, cy + r1, outline=body, width=w)
            for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
                c.create_line(cx + dx * g(5), cy + dy * g(5), cx + dx * g(13), cy + dy * g(13),
                              fill=body, width=w, capstyle="round")
            c.create_oval(cx - g(2), cy - g(2), cx + g(2), cy + g(2), fill=body, outline="")
            return
        # иконка монитора: корпус, экран (светится при включении), подставка
        screen = mix("#151A25", "#FFF4CC", t.t)
        rrect(c, cx - g(13), cy - g(12), cx + g(13), cy + g(6), g(3), fill=body, outline="")
        rrect(c, cx - g(10), cy - g(9), cx + g(10), cy + g(3), g(2), fill=screen, outline="")
        c.create_rectangle(cx - g(2), cy + g(6), cx + g(2), cy + g(10), fill=body, outline="")
        rrect(c, cx - g(7), cy + g(9), cx + g(7), cy + g(12), g(1.5), fill=body, outline="")
        num = getattr(t, "mon", {}).get("num")
        if num is not None and sc > 0.9:
            c.create_text(cx, cy - g(3), text=str(num), font=self.f_num,
                          fill=mix(MUTED, "#8A5A00", t.t))

    def draw_tip(self, c, t, i):
        """Подсказка с названием и состоянием: за кнопкой, дальше от язычка."""
        title, sub = t.title, t.sub
        tw = min(TIP_W, max(self.f_bold.measure(title), self.f_norm.measure(sub)) + u(22))
        th = TIP_H
        fx, fy = self.button_center(i)
        dx, dy = self.button_dir(i)
        off = D / 2 + u(8) + abs(dx) * tw / 2 + abs(dy) * th / 2
        tcx, tcy = fx + dx * off, fy + dy * off
        rrect(c, tcx - tw / 2, tcy - th / 2, tcx + tw / 2, tcy + th / 2, u(12),
              fill=PANEL, outline=ERR if t.error else BORDER)
        c.create_text(tcx, tcy - u(7), text=title, fill=TEXT, font=self.f_bold)
        sub_col = ERR if t.error else (ACCENT if t.boosted else MUTED)
        c.create_text(tcx, tcy + u(8), text=sub, fill=sub_col, font=self.f_norm)

    # ---------- мышь ----------
    def on_handle(self, mx, my):
        hx, hy = self.hc()
        return (mx - hx) ** 2 + (my - hy) ** 2 <= HR * HR

    def hit(self, mx, my):
        if self.mode != "docked" or not self.expanded or self.e < 0.8:
            return None
        for i, t in enumerate(self.tiles):
            cx, cy = self.button_center(i)
            if (mx - cx) ** 2 + (my - cy) ** 2 <= (D / 2 + u(6)) ** 2:
                return t
        return None

    def on_motion(self, e):
        if self.mode != "docked":
            return
        tile = self.hit(e.x, e.y)
        self.canvas.config(cursor="hand2" if tile is not None or self.on_handle(e.x, e.y) else "")
        if tile is not self.hover_tile:
            self.hover_tile = tile
            self.kick()
            if self.job is None:
                self.draw()

    def on_leave(self, e):
        if self.mode != "docked":
            return
        self.hover_tile = None
        self.canvas.config(cursor="")
        self.draw()

    def on_press(self, e):
        self.press = None
        if self.mode != "docked":
            return
        if self.on_handle(e.x, e.y):
            self.press = cursor_pos()   # возможно, начало перетаскивания
            return
        tile = self.hit(e.x, e.y)
        if tile is not None:
            tile.click()

    def on_drag(self, e):
        if self.mode == "docked" and self.press is not None:
            cx, cy = cursor_pos()
            if abs(cx - self.press[0]) + abs(cy - self.press[1]) < 6:
                return
            self.begin_drag()
        if self.mode == "drag":
            x, y = cursor_pos()
            self.set_circle(x, y)
            self.update_ghost(self.snap_target(x, y))

    def on_release(self, e):
        if self.mode == "drag":
            self.end_drag(*cursor_pos())
        self.press = None

    def on_middle(self, e):
        """СКМ по кнопке сценария: выполнить его один раз, без цикла."""
        if self.mode != "docked":
            return
        tile = self.hit(e.x, e.y)
        if tile is not None and hasattr(tile, "click_once"):
            tile.click_once()
            self.kick()

    def on_right(self, e):
        if self.mode != "docked":
            return
        tile = self.hit(e.x, e.y)
        if tile is not None and hasattr(tile, "right_click"):
            tile.right_click()
            return
        if self.on_handle(e.x, e.y):
            try:
                self.menu.tk_popup(e.x_root, e.y_root)
            finally:
                self.menu.grab_release()

    # ---------- перетаскивание между экранами и прилипание к краю ----------
    def snap_target(self, x, y):
        """Куда прилипнет язычок, если отпустить его в точке (x, y): монитор, край, положение вдоль края."""
        def dist(m):
            l, t, r, b = m["work"]
            dx = max(l - x, 0, x - r)
            dy = max(t - y, 0, y - b)
            return dx * dx + dy * dy
        mon = min(self.mons, key=dist)
        l, t, r, b = mon["work"]
        sides = {"left": abs(x - l), "right": abs(r - x), "top": abs(y - t), "bottom": abs(b - y)}
        edge = min(sides, key=sides.get)   # любой край, в том числе общий между мониторами
        pos = y if edge in ("left", "right") else x
        return edge, self.clamp_pos(edge, pos, mon), mon

    def begin_drag(self):
        self.mode = "drag"
        self.e = self.e_target = 0.0
        self.expanded = False
        self.hover_tile = None
        self.make_ghost()
        self.draw()

    # пунктирный полукруг, показывающий, куда прилипнет язычок
    def make_ghost(self):
        g = tk.Toplevel(self.root)
        g.overrideredirect(True)
        g.attributes("-topmost", True)
        g.configure(bg=KEY)
        g.attributes("-transparentcolor", KEY)
        cv = tk.Canvas(g, bg=KEY, highlightthickness=0)
        cv.pack(fill="both", expand=True)
        g.geometry("1x1+0+0")
        g.update_idletasks()
        self.make_noactivate(g, click_through=True)
        self.ghost, self.ghost_cv, self.ghost_key = g, cv, None

    def update_ghost(self, target):
        if self.ghost is None:
            return
        edge, pos, mon = target
        key = (edge, int(pos // 4), mon["device"])
        if key == self.ghost_key:
            return
        self.ghost_key = key
        hx, hy = self.edge_point(edge, pos, mon)
        if edge == "left":
            x1, y1, x2, y2 = hx, hy - HR, hx + HR, hy + HR
        elif edge == "right":
            x1, y1, x2, y2 = hx - HR, hy - HR, hx, hy + HR
        elif edge == "top":
            x1, y1, x2, y2 = hx - HR, hy, hx + HR, hy + HR
        else:
            x1, y1, x2, y2 = hx - HR, hy - HR, hx + HR, hy
        self.ghost.geometry(geo(x2 - x1, y2 - y1, x1, y1))
        cv = self.ghost_cv
        cv.delete("all")
        cx, cy = hx - x1, hy - y1
        cv.create_oval(cx - HR, cy - HR, cx + HR, cy + HR, fill="", outline=ACCENT,
                       width=2, dash=(4, 3))

    def end_drag(self, x, y):
        if self.ghost is not None:
            self.ghost.destroy()
            self.ghost = None
        edge, pos, mon = self.snap_target(x, y)
        self.dock(edge, pos, mon)
        self.save_cfg()
        self.snap = {"p": 0.0, "x0": x, "y0": y, "x1": self.hx, "y1": self.hy}
        self.mode = "snap"
        self.kick()

    # ---------- вылет по наведению ----------
    def poll(self):
        if self.mode == "docked":
            mx, my = cursor_pos()
            now = time.monotonic()
            in_handle = self.rect(0, HR + u(2), -(HR + u(10)), HR + u(10))
            in_handle = in_handle[0] <= mx <= in_handle[2] and in_handle[1] <= my <= in_handle[3]
            # когда кнопки вылетели (или улетают), держим их, пока мышь в полукруге вокруг язычка
            dxs, dys = mx - self.hx, my - self.hy
            da = dxs * self.a[0] + dys * self.a[1]
            in_fan = (self.e > 0.02 and da >= -u(10)
                      and dxs * dxs + dys * dys <= (self.R + D / 2 + u(18)) ** 2)
            near = in_handle or in_fan

            if near:
                self.last_inside = now
                if self.e_target == 1.0:
                    want = 1.0
                else:
                    if self.enter_t is None:      # небольшая задержка, чтобы не вылетало случайно
                        self.enter_t = now
                    want = 1.0 if now - self.enter_t >= 0.12 else 0.0
            else:
                self.enter_t = None
                want = self.e_target if now - self.last_inside < 0.35 else 0.0

            if want != self.e_target:
                self.e_target = want
                self.kick()
        self.root.after(40, self.poll)

    def watch(self):
        for t in self.tiles:
            t.check()
        try:  # кнопки всегда поверх остальных окон
            hwnd = user32.GetAncestor(self.root.winfo_id(), 2) or self.root.winfo_id()
            raise_top(hwnd)
        except Exception:
            pass
        self.root.after(2000, self.watch)

    def on_close(self):
        # при выходе всегда возвращаем всё как было
        for t in self.monitor_tiles:
            if t.boosted:
                t.restore()
        try:
            winmm.timeEndPeriod(1)
        except Exception:
            pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
