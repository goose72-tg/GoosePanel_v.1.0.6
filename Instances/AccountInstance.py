import ctypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import base64
import hashlib, hmac
import winreg
from datetime import datetime, timedelta
from ctypes import wintypes

import wmi


import pyautogui
import pyperclip
import json
from pathlib import Path
import psutil
import pygetwindow as gw
import win32con
import win32gui
import win32process
from pywinauto import Application, findwindows

from Helpers.MouseController import MouseHelper
from Helpers.WinregHelper import WinregHelper
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager


def bytes_to_int(bytes):
    result = 0
    for b in bytes:
        result = result * 256 + int(b)
    return result

def GetMainWindowByPID(pid: int) -> int:
    """
    Возвращает hwnd главного окна процесса по PID.
    Если окно не найдено, возвращает 0.
    """
    hwnds = []

    def enum_windows_callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.IsWindowEnabled(hwnd):
            return True
        if win32gui.GetParent(hwnd) != 0:
            return True
        _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        if window_pid == pid:
            hwnds.append(hwnd)
            return False  # нашли, можно остановить
        return True

    win32gui.EnumWindows(enum_windows_callback, None)
    return hwnds[0] if hwnds else 0

def update_video_cfg(src_path, dst_path, updates: dict):
    """
    Копирует cfg-файл и обновляет указанные параметры.

    :param src_path: путь к исходному файлу
    :param dst_path: путь для сохранения нового файла
    :param updates: словарь параметров для изменения
    """
    # Создаем директорию назначения, если её нет
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)

    # Если файл уже есть — можно просто пересоздать
    shutil.copy(src_path, dst_path)

    # Читаем содержимое копии
    with open(dst_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Обновляем параметры
    with open(dst_path, "w", encoding="utf-8") as f:
        for line in lines:
            for key, value in updates.items():
                if f'"{key}"' in line:
                    prefix = line[:line.find('"'+key+'"')]
                    line = f'{prefix}"{key}"\t\t"{value}"\n'
                    break
            f.write(line)

user32 = ctypes.WinDLL('user32', use_last_error=True)

HWND = wintypes.HWND
RECT = wintypes.RECT
LPRECT = ctypes.POINTER(RECT)
BOOL = wintypes.BOOL
UINT = wintypes.UINT

# Функции Win32
SetProcessDPIAware = user32.SetProcessDPIAware
SetProcessDPIAware.restype = BOOL

GetWindowRect = user32.GetWindowRect
GetWindowRect.argtypes = [HWND, LPRECT]
GetWindowRect.restype = BOOL

GetClientRect = user32.GetClientRect
GetClientRect.argtypes = [HWND, LPRECT]
GetClientRect.restype = BOOL

SetWindowPos = user32.SetWindowPos
SetWindowPos.argtypes = [HWND, HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, UINT]
SetWindowPos.restype = BOOL

SetWindowText = user32.SetWindowTextW
SetWindowText.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
SetWindowText.restype = wintypes.BOOL
# Константы
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004

def fix_window(hwnd):
    if not hwnd:
        return

    SetProcessDPIAware()

    wr = RECT()
    cr = RECT()

    if not GetWindowRect(hwnd, ctypes.byref(wr)) or not GetClientRect(hwnd, ctypes.byref(cr)):
        return

    current_client_width = cr.right - cr.left
    current_client_height = cr.bottom - cr.top
    current_window_width = wr.right - wr.left
    current_window_height = wr.bottom - wr.top

    # Если размеры client area не совпадают с target
    if current_client_width != current_window_width or current_client_height != current_window_height:
        # Вычисляем рамки окна
        dx = (wr.right - wr.left) - current_client_width
        dy = (wr.bottom - wr.top) - current_client_height

        # Применяем новые размеры окна
        SetWindowPos(hwnd, None, wr.left, wr.top, current_client_width, current_client_height, SWP_NOZORDER | SWP_NOMOVE)

VENDOR_PRIORITY = {
    0x10DE: 3,  # NVIDIA
    0x1002: 2,  # AMD
    0x8086: 1   # Intel
}
# Создаём DXGIFactory
def get_best_gpu():
    c = wmi.WMI()
    gpus = []

    for gpu in c.Win32_VideoController():
        try:
            raw_memory = gpu.AdapterRAM
            if not raw_memory:
                continue

            mem_bytes = int(raw_memory)
            if mem_bytes <= 0:
                try:
                    mem_bytes = get_gpu_memory_alternative(gpu)
                except:
                    continue

            mem_mb = mem_bytes // (1024 * 1024)

        except (ValueError, AttributeError):
            continue

        vendor_id = "0"
        device_id = "0"
        if gpu.PNPDeviceID:
            ven_match = re.search(r'VEN_([0-9A-Fa-f]{4})', gpu.PNPDeviceID)
            dev_match = re.search(r'DEV_([0-9A-Fa-f]{4})', gpu.PNPDeviceID)
            if ven_match:
                vendor_id = int(ven_match.group(1), 16)
            if dev_match:
                device_id = int(dev_match.group(1), 16)

        gpus.append({
            "VendorID": vendor_id,
            "DeviceID": device_id,
            "MemoryMB": mem_mb,
            "Priority": VENDOR_PRIORITY.get(vendor_id, 0)  # Если неизвестный вендор, приоритет 0
        })

    if not gpus:
        return {"VendorID": "0", "DeviceID": "0"}

    # Сортируем сначала по приоритету производителя, потом по памяти
    gpu_best = max(gpus, key=lambda x: (x["Priority"], x["MemoryMB"]))
    return {"VendorID": gpu_best["VendorID"], "DeviceID": gpu_best["DeviceID"]}


def get_gpu_memory_alternative(gpu):
    """Альтернативный метод получения памяти GPU через реестр"""
    import winreg

    try:
        # Получаем ID устройства из PNPDeviceID
        pnp_id = gpu.PNPDeviceID
        if not pnp_id:
            return 0

        # Формируем путь в реестре
        part = pnp_id.split("\\")[1]
        key_path = f"SYSTEM\\CurrentControlSet\\Control\\Class\\{part}"


        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            # Пытаемся прочитать значение памяти
            value, _ = winreg.QueryValueEx(key, "HardwareInformation.qwMemorySize")
            return int(value)
    except:
        return 0
def get_base_path():
    """Определяем базовый путь относительно запуска программы."""
    if getattr(sys, 'frozen', False):
        # Если программа собрана в .exe
        return os.path.dirname(sys.executable)
    else:
        # Если запущено через python main.py
        return os.path.dirname(os.path.abspath(sys.argv[0]))

import os

class ApplicationException(Exception):
    pass

def _find_handle_exe() -> str | None:
    base_path = Path(get_base_path())
    candidates = [
        base_path / "handle.exe",
        base_path.parent / "handle.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None

def _run_handle_process(args: str) -> str:
    handle_path = _find_handle_exe()
    if not handle_path:
        raise FileNotFoundError("handle.exe не найден рядом с main.py.")
    result = subprocess.run(
        [handle_path] + shlex.split(args),
        capture_output=True,
        text=True,
        creationflags=0x08000000,
        check=False,
    )
    return (result.stdout + result.stderr).strip()

def _parse_handle_values(output: str, name_filter: str, type_filter: str) -> list[str]:
    handles = []
    lines = [line for line in output.splitlines() if line.strip()]
    for line in lines:
        if name_filter.lower() in line.lower() and type_filter.lower() in line.lower():
            parts = re.split(r"[ \t]+", line.strip())
            for part in parts:
                if part.endswith(":") and len(part) > 1:
                    hex_value = part[:-1]
                    if re.fullmatch(r"[0-9A-Fa-f]+", hex_value):
                        handles.append(hex_value.upper())
                        break
    return handles

def _close_cs2_singleton_mutex(pid: int) -> bool:
    """
    Закрытие Хэндла у мьютекса тем самым давая возможность запустить второй CS2.
    Возвращает True если успешно закрыл хэндл.
    """
    if not pid:
        return False

    try:
        # Ищем мьютекс через handle.exe для конкретного процесса.
        search_variants = [
            f"-accepteula -nobanner -a -p {pid} csgo_singleton_mutex",
            f"-accepteula -a -p {pid} csgo_singleton_mutex",
            f"-accepteula -p {pid} -a csgo_singleton_mutex",
        ]

        handles: list[str] = []
        for args in search_variants:
            search_output = _run_handle_process(args)
            handles = _parse_handle_values(search_output, "csgo_singleton_mutex", "Mutant")
            if handles:
                break

        if not handles:
            return False

        closed_any = False
        for handle_id in handles:
            result = _run_handle_process(f"-accepteula -nobanner -c {handle_id} -p {pid} -y")
            low_result = result.lower()
            if (
                not result.strip()
                or "closed" in low_result
                or "handle closed" in low_result
                or "заверш" in low_result
            ):
                closed_any = True
        return closed_any
    except Exception as exc:
        raise ApplicationException(f"{exc} Возможно включен антивирус") from exc


def _close_all_cs2_singleton_mutexes(primary_pid: int | None = None) -> bool:
    """
    Аналог CloseAllMutexes из cs2ch.exe:
    проходит по всем запущенным cs2.exe и закрывает csgo_singleton_mutex.
    """
    pids: list[int] = []
    if primary_pid:
        pids.append(int(primary_pid))

    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = (proc.info.get('name') or '').lower()
            proc_pid = int(proc.info.get('pid') or 0)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError, TypeError):
            continue

        if name == 'cs2.exe' and proc_pid > 0 and proc_pid not in pids:
            pids.append(proc_pid)

    closed_any = False
    for cs2_pid in pids:
        try:
            if _close_cs2_singleton_mutex(cs2_pid):
                closed_any = True
        except ApplicationException:
            continue

    return closed_any

def launch_isolated_steam(account_name: str, steam_path: str, extra_args: list[str] | None = None) -> subprocess.Popen:
    """
    Запускает Steam в изолированном окружении PanelData для конкретного аккаунта.
    CS2 наследует переменные окружения Steam и не видит мутексы других копий.
    """
    base_profile = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "PanelData" / account_name
    local_path = base_profile / "AppData" / "Local"
    locallow_path = base_profile / "AppData" / "LocalLow"

    if local_path.exists():
        shutil.rmtree(local_path, ignore_errors=True)
    local_path.mkdir(parents=True, exist_ok=True)
    locallow_path.mkdir(parents=True, exist_ok=True)

    # Символьная ссылка на NVIDIA (требуются права администратора)
    original_local = Path(os.environ.get("LOCALAPPDATA", ""))
    nvidia_src = original_local / "NVIDIA"
    nvidia_dest = local_path / "NVIDIA"
    if nvidia_dest.exists() or nvidia_dest.is_symlink():
        if nvidia_dest.is_dir():
            shutil.rmtree(nvidia_dest, ignore_errors=True)
        else:
            nvidia_dest.unlink(missing_ok=True)
    if nvidia_src.exists():
        subprocess.run(
            ["cmd", "/c", "mklink", "/D", str(nvidia_dest), str(nvidia_src)],
            creationflags=0x08000000,
            check=False,
        )

    env = os.environ.copy()
    env["USERPROFILE"] = str(base_profile)
    env["LOCALAPPDATA"] = str(local_path)

    args = [
        steam_path,
        "-master_ipc_name_override",
        account_name,
        "-nosingleinstance",
        "-silent",
    ]
    if extra_args:
        args.extend(extra_args)

    return subprocess.Popen(args, env=env, creationflags=0x08000000)

def find_latest_file(filename: str) -> str | None:
    settings = SettingsManager()
    latest_file_path = None
    latest_mtime = 0

    cs2_path = settings.get(
        "CS2Path",
        "C:/Program Files (x86)/Steam/steamapps/common/Counter-Strike Global Offensive",
    )
    search_roots = [
        Path(cs2_path),
        Path(cs2_path) / "game" / "csgo",
        Path.cwd(),
    ]

    for root_path in search_roots:
        if not root_path.exists():
            continue
        for root, dirs, files in os.walk(root_path):
            if filename in files:
                file_path = os.path.join(root, filename)
                mtime = os.path.getmtime(file_path)
                if mtime > latest_mtime:
                    latest_mtime = mtime
                    latest_file_path = file_path

    return latest_file_path

def to_base62(num: int) -> str:
    alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    base = len(alphabet)
    result = []
    while num:
        num, rem = divmod(num, base)
        result.append(alphabet[rem])
    return ''.join(reversed(result)) or '0'

class Account:
    def __init__(self, login, password, shared_secret=None, steam_id = 0, identity_secret=None):
        self.login = login
        self.password = password
        self.shared_secret = shared_secret
        self.identity_secret = identity_secret
        self.steam_id = steam_id
        self.steamProcess = None
        self.CS2Process = None
        self.last_match_id = None

        self._settingsManager = SettingsManager()
        self._logManager = LogManager()

        self._color = "#DCE4EE"
        self._color_callback = None  # callback на смену цвета
        self._stop_monitoring = False  # флаг для остановки мониторинга
        runtime_path = Path("runtime.json")
        if runtime_path.exists():
            try:
                with open(runtime_path, "r", encoding="utf-8") as f:
                    entries = json.load(f)
                entry = next((e for e in entries if e.get("login") == self.login), None)
                if entry:
                    steam_pid = entry.get("SteamPid")
                    cs2_pid = entry.get("CS2Pid")
                    if psutil.pid_exists(steam_pid) and psutil.pid_exists(cs2_pid):
                        steam_proc = psutil.Process(steam_pid)
                        cs2_proc = psutil.Process(cs2_pid)
                        if cs2_proc.name().lower() == "cs2.exe" and cs2_proc.ppid() == steam_proc.pid:
                            self.steamProcess = steam_proc
                            self.CS2Process = cs2_proc
                            self.setColor("green")
                            self.MonitorCS2(interval=5)  # запускаем мониторинг CS2
                            self.start_log_watcher(f"{self.login}.log")
                            csWindow = self.FindCSWindow()
                            fix_window(csWindow)
                            SetWindowText(csWindow, f"[FREE] {self.login}")
            except Exception as e:
                print(f"Ошибка при чтении runtime.json: {e}")

    def start_log_watcher(self, filename: str):
        # Запускаем поток, который будет искать файл и потом его читать
        t = threading.Thread(target=self._watch_log_file, args=(filename,), daemon=True)
        t.start()

    def _watch_log_file(self, filename: str):
        timeout = 5 * 60  # 5 минут
        start_time = time.time()

        while time.time() - start_time < timeout:
            path = find_latest_file(filename)
            if path:
                try:
                    # Пробуем открыть файл, если доступ есть — переходим к чтению
                    with open(path, 'r', encoding='utf-8', errors='ignore'):
                        self.tail_log_file(path)
                        return  # выходим из функции, поток теперь читает файл
                except PermissionError:
                    # Файл найден, но недоступен — продолжаем поиск
                    pass
            time.sleep(1)

        return
    def tail_log_file(self, file_path: str):
        with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    self.process_log_line(line)
                else:
                    time.sleep(0.1)


    def process_log_line(self, line: str):
        if "Scratch RT Allocations:" in line:
            fix_window(self.FindCSWindow())
            return
        match = re.search(r"match_id=(\d+)", line)
        if match:
            match_id_str = match.group(1)
            match_id_int = int(match_id_str)
            match_id_compact = to_base62(match_id_int)
            self.last_match_id = match_id_compact


    def isCSValid(self):
        if self.CS2Process is None or self.steamProcess is None:
            return False

        try:
            if psutil.pid_exists(self.steamProcess.pid) and psutil.pid_exists(self.CS2Process.pid):
                steam_proc = psutil.Process(self.steamProcess.pid)
                cs2_proc = psutil.Process(self.CS2Process.pid)
                if cs2_proc.name().lower() == "cs2.exe" and cs2_proc.ppid() == steam_proc.pid:
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            return False
        return False

        return False

    def isSteamValid(self):
        if self.steamProcess is None:
            return False

        try:
            if not psutil.pid_exists(self.steamProcess.pid):
                return False
            steam_proc = psutil.Process(self.steamProcess.pid)
            return (steam_proc.name() or "").lower() == "steam.exe"
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            return False

    def setColorCallback(self, callback):
        """Регистрируем callback, который будет вызываться при смене цвета"""
        self._color_callback = callback

    def setColor(self, color):
        """Меняем цвет и вызываем callback, если он есть"""
        self._color = color
        if self._color_callback:
            self._color_callback(color)

    def getWindowSize(self):
        hwnd = self.FindCSWindow()
        rect = win32gui.GetWindowRect(hwnd)
        win_width = rect[2] - rect[0]
        win_height = rect[3] - rect[1]
        return win_width, win_height

    def MoveWindow(self, x, y):
        ctypes.windll.user32.SetProcessDPIAware()
        hwnd = self.FindCSWindow()
        if hwnd is None: return
        rect = win32gui.GetWindowRect(hwnd)
        win_width = rect[2] - rect[0]
        win_height = rect[3] - rect[1]
        win32gui.MoveWindow(hwnd, x, y, win_width, win_height, True)
        SetWindowText(hwnd, f"[FREE] {self.login}")

    def FindCSWindow(self) -> int:
        if self.CS2Process and self.isCSValid():
            return GetMainWindowByPID(self.CS2Process.pid)
        return 0
    def get_auth_code(self):
        t = int(time.time() / 30)
        t = t.to_bytes(8, 'big')
        key = base64.b64decode(self.shared_secret)
        h = hmac.new(key, t, hashlib.sha1)
        signature = list(h.digest())
        start = signature[19] & 0xf
        fc32 = bytes_to_int(signature[start:start + 4])
        fc32 &= 2147483647
        fullcode = list('23456789BCDFGHJKMNPQRTVWXY')
        code = ''
        for i in range(5):
            code += fullcode[fc32 % 26]
            fc32 //= 26
        return code

    def get_fresh_auth_code(self, min_validity_seconds: int = 18):
        """
        Возвращает «свежий» 2FA код и не отдаёт код, который скоро истечёт.
        Это снижает шанс, что Steam получит уже просроченный токен.
        """
        min_validity_seconds = max(5, min(25, int(min_validity_seconds)))

        while True:
            now = time.time()
            seconds_left = 30 - (int(now) % 30)

            # Если код вот-вот сменится, дожидаемся следующего 30-сек окна.
            if seconds_left <= min_validity_seconds:
                time.sleep(seconds_left + 0.15)
                continue

            code = self.get_auth_code()
            # Небольшая защита от гонки на границе секунды.
            if code == self.get_auth_code():
                return code
            time.sleep(0.1)

    def MoveMouse(self, x: int, y: int):
        """
        Перемещает курсор мыши тносительно окна CS2.
        """
        hwnd = self.FindCSWindow()
        if hwnd:
            MouseHelper.MoveMouse(hwnd, x, y)

    def ClickMouse(self, x: int, y: int, button: str = 'left'):
        """
        Кликает мышью относительно окна CS2.
        """
        hwnd = self.FindCSWindow()
        if hwnd:
            MouseHelper.ClickMouse(hwnd, x, y, button)

    def ProcessWindowsBeforeCS(self, steamPid):
        """Обрабатывает все окна Steam и выводит тексты TextBox"""

        parent = psutil.Process(steamPid)
        children = parent.children(recursive=True)  # рекурсивно

        all_pids = [steamPid] + [child.pid for child in children]

        for pid in all_pids:
            try:
                exclude_titles = {"Steam", "Friends List", "Special Offers"}
                windows = [hwnd for hwnd in findwindows.find_windows(process=pid) if
                           win32gui.GetWindowText(hwnd) not in exclude_titles]
                if not windows:
                    continue
                app = Application(backend="uia").connect(process=pid)
                for win in app.windows():
                    win.set_focus()
                    all_descendants = win.descendants()
                    edits = [c for c in all_descendants if c.friendly_class_name() == "Edit"]
                    buttons = [c for c in all_descendants if c.friendly_class_name() == "Button"]
                    statics = [c for c in all_descendants if c.friendly_class_name() == "Static"]
                    if len(edits) == 2 and any(btn.window_text().strip() == "Sign in" for btn in buttons):
                        edits[0].set_text(self.login)
                        edits[1].set_text(self.password)
                        sign_in_button = next((btn for btn in buttons if btn.window_text().strip() == "Sign in"), None)
                        sign_in_button.click()
                        time.sleep(2)
                    if any(txt.window_text().strip() == "Enter a code instead" for txt in statics):
                        target = next((s for s in statics if s.window_text().strip() == "Enter a code instead"), None)
                        target.click_input()
                    if any(btn.window_text().strip() == "Play anyway" for btn in buttons):
                        target = next((btn for btn in buttons if btn.window_text().strip() == "Play anyway"), None)
                        if target:
                            target.click()
                    if any(btn.window_text().strip().lower() == "no thanks".lower() for btn in buttons):
                        target = next(
                            (btn for btn in buttons if btn.window_text().strip().lower() == "no thanks".lower()), None)
                        if target:
                            target.click()
                    if any(txt.window_text().strip() == "Enter the code from your Steam Mobile App" for txt in statics) \
                            and self.shared_secret is not None:
                        code = self.get_fresh_auth_code()
                        twofa_edit = next((e for e in edits if e.is_enabled() and e.is_visible()), None)

                        if twofa_edit is not None:
                            twofa_edit.set_text(code)
                        else:
                            win.set_focus()
                            pyperclip.copy(code)
                            time.sleep(0.1)
                            MouseHelper.PasteText()

            except Exception as e:
                print(f"Не удалось подключиться к PID {pid}: {e}")

    def _sync_cfg_files_before_start(self, cs2_path, steam_path):
        settings_path = Path(get_base_path()) / "settings"

        game_cfg_dir = Path(cs2_path) / "game" / "csgo" / "cfg"
        game_cfg_dir.mkdir(parents=True, exist_ok=True)

        # Глобальные cfg (перезаписываем перед каждым стартом аккаунта)
        for filename in ("fsn.cfg", "gamestate_integration_fsn.cfg"):
            src = settings_path / filename
            if src.exists():
                shutil.copy2(src, game_cfg_dir / filename)

        if self.steam_id == 0:
            return

        userdata_cfg_dir = Path(os.path.dirname(steam_path)) / "userdata" / str(self.steam_id - 76561197960265728) / "730" / "local" / "cfg"
        userdata_cfg_dir.mkdir(parents=True, exist_ok=True)

        vendorID = self._settingsManager.get("VendorID", 0)
        deviceID = self._settingsManager.get("DeviceID", 0)

        if vendorID == 0 or deviceID == 0:
            best_gpu = get_best_gpu()
            vendorID = best_gpu["VendorID"]
            deviceID = best_gpu["DeviceID"]
            self._settingsManager.set("VendorID", vendorID)
            self._settingsManager.set("DeviceID", deviceID)
            self._logManager.add_log(f"Detected VendorID: {vendorID}, DeviceID: {deviceID}")

        # Всегда обновляем cs2_video.txt и cs2_video.txt.bak перед каждым аккаунтом
        for video_name in ("cs2_video.txt", "cs2_video.txt.bak"):
            src_video = settings_path / video_name
            dst_video = userdata_cfg_dir / video_name
            if src_video.exists():
                update_video_cfg(str(src_video), str(dst_video), {
                    "VendorID": str(vendorID),
                    "DeviceID": str(deviceID),
                })

        # Всегда перезаписываем эти cfg в userdata\...\cfg
        for filename in ("cs2_machine_convars.vcfg", "gamestate_integration_fsn.cfg"):
            src = settings_path / filename
            if src.exists():
                shutil.copy2(src, userdata_cfg_dir / filename)

    def StartGame(self):
        time.sleep(5)
        print("Запуск Steam...")
        steam_path = self._settingsManager.get("SteamPath", r"C:\Program Files (x86)\Steam\steam.exe")
        cs2_path = self._settingsManager.get(
            "CS2Path",
            "C:/Program Files (x86)/Steam/steamapps/common/Counter-Strike Global Offensive"
        )

        # Удаление фона
        if self._settingsManager.get("RemoveBackground", False):
            maps_path = Path(cs2_path) / "game" / "csgo" / "maps"
            if maps_path.exists() and maps_path.is_dir():
                for file in maps_path.iterdir():
                    if file.is_file() and file.name.endswith("_vanity.vpk"):
                        print(f"Delete file: {file}")
                        file.unlink()

            panorama_path = Path(cs2_path) / "game" / "csgo" / "panorama" / "videos"
            if panorama_path.exists() and panorama_path.is_dir():
                print(f"Delete folder: {panorama_path}")
                shutil.rmtree(panorama_path)

       

        self._sync_cfg_files_before_start(cs2_path, steam_path)

        # Запуск Steam
        try:
            WinregHelper.set_value(
                r"Software\Valve\Steam",
                "AutoLoginUser",
                self.login,
                winreg.REG_SZ
            )

            args = (
                f'{self._settingsManager.get("SteamArg", "-nofriendsui -vgui -noreactlogin")}'
                f' -applaunch 730 '
                f'-con_logfile {self.login}.log '
                f'{self._settingsManager.get("CS2Arg", "")}'
            )

            final = shlex.split(args)
            self.steamProcess = launch_isolated_steam(self.login, steam_path, final)

            # 🔥 ВАЖНО: перезапускаем Steam при ошибках в течение 60 секунд


        except Exception as e:
            print(f"Ошибка запуска Steam: {e}")
            return


        # Логин + ожидание CS2
        while True:
            self.ProcessWindowsBeforeCS(self.steamProcess.pid)

            cs2_found = False
            for proc in psutil.process_iter(['pid', 'name', 'ppid']):
                if proc.info['name'] and proc.info['name'].lower() == 'cs2.exe':
                    try:
                        parent = psutil.Process(proc.info['ppid'])
                        if parent.pid == self.steamProcess.pid:
                            self.CS2Process = proc
                            cs2_found = True
                            self._kill_cs2_mutex(proc.pid)
                            
                            # 🔥 ПЕРЕИМЕНОВАНИЕ ОКНА СРАЗУ ПОСЛЕ НАХОЖДЕНИЯ PID!
                            csWindow = self.FindCSWindow()
                            if csWindow:
                                fix_window(csWindow)
                                SetWindowText(csWindow, f"[FREE] {self.login}")
                                print(f"✅ [{self.login}] Окно переименовано!")
                            
                            break
                    except psutil.NoSuchProcess:
                        continue

            if cs2_found:
                break

            time.sleep(0.5)

        self.ProcessWindowsAfterCS(self.steamProcess.pid)

        time.sleep(5)

        # runtime.json
        runtime_path = Path("runtime.json")
        try:
            data = []
            if runtime_path.exists():
                with open(runtime_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

            data = [d for d in data if d.get("login") != self.login]
            data.append({
                "login": self.login,
                "SteamPid": self.steamProcess.pid,
                "CS2Pid": self.CS2Process.pid if self.CS2Process else None
            })

            with open(runtime_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            self.start_log_watcher(f"{self.login}.log")

        except Exception as e:
            print(f"Ошибка записи runtime.json: {e}")

    def StartSteamOnly(self, login_timeout=25):
        print(f"Запуск только Steam для [{self.login}]...")
        steam_path = self._settingsManager.get("SteamPath", r"C:\Program Files (x86)\Steam\steam.exe")

        if not os.path.isfile(steam_path) or not steam_path.lower().endswith(".exe"):
            raise FileNotFoundError(f"Некорректный SteamPath: {steam_path}")

        if self.isSteamValid() and not self.isCSValid():
            print(f"ℹ️ Steam [{self.login}] уже запущен, повторный старт не требуется")
            return

        try:
            WinregHelper.set_value(
                r"Software\Valve\Steam",
                "AutoLoginUser",
                self.login,
                winreg.REG_SZ
            )

            launch_args = shlex.split(
                self._settingsManager.get("SteamArg", "-nofriendsui -vgui -noreactlogin")
            )
            self.steamProcess = launch_isolated_steam(self.login, steam_path, launch_args)
            self.CS2Process = None

            deadline = time.time() + max(5, int(login_timeout))
            while time.time() < deadline:
                if not self.isSteamValid():
                    raise RuntimeError("Steam завершился во время авторизации")

                self.ProcessWindowsBeforeCS(self.steamProcess.pid)
                time.sleep(0.75)

            self.setColor("#5dade2")
            print(f"✅ Steam-only запуск завершён для [{self.login}] (PID {self.steamProcess.pid})")
        except Exception:
            self.steamProcess = None
            self.CS2Process = None
            raise


    def restart_steam_on_error(self, steam_pid, timeout=60):
        """🔄 Перезапускает Steam при ошибках в течение timeout секунд"""
        print(f"🔄 Мониторим Steam [{self.login}] на ошибки ({timeout}с)...")
        
        start_time = time.time()
        max_restarts = 3  # Максимум 3 перезапуска
        
        while time.time() - start_time < timeout and max_restarts > 0:
            time.sleep(2)  # Проверяем каждые 2 секунды
            
            # Проверяем, жив ли Steam процесс
            try:
                steam_proc = psutil.Process(steam_pid)
            except psutil.NoSuchProcess:
                print(f"⚠️ Steam [{self.login}] завершился, перезапускаем...")
                self._restart_steam()
                return
            
            # Проверяем наличие окон Steam Service Error или зависший Steam
            found_error = False
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    info = proc.info
                    if info['name'] and info['name'].lower() in ['steam.exe', 'steamwebhelper.exe']:
                        cmdline = ' '.join(info['cmdline'] or [])
                        if any(error_str in cmdline.lower() for error_str in [
                            'serviceerror', 'updateandrestart', 'error'
                        ]) or steam_proc.status() in ['zombie', 'dead']:
                            found_error = True
                            break
                except:
                    continue
            
            if found_error:
                print(f"🔄 Обнаружена ошибка Steam [{self.login}], перезапускаем... (осталось: {max_restarts})")
                self._restart_steam()
                max_restarts -= 1
                start_time = time.time()  # Сбрасываем таймер после перезапуска
        
        print(f"✅ Мониторинг Steam [{self.login}] завершен")

    def _kill_cs2_mutex(self, pid: int) -> None:
        try:
            # cs2ch.exe закрывал mutex не у одного PID, а у всех cs2.exe.
            # Повторяем это поведение и делаем несколько попыток на старте.
            for _ in range(6):
                if _close_all_cs2_singleton_mutexes(pid):
                    return
                time.sleep(0.4)
        except ApplicationException as exc:
            print(f"Ошибка очистки mutex: {exc}")

    def _restart_steam(self):
        """🔄 Полностью ерезапускает Steam для текущего аккаунта"""
        print(f"🔄 Полный перезапуск Steam [{self.login}]...")
        
        # 1. Убиваем все процессы аккаунта
        self.KillAccountProcesses()
        time.sleep(2)
        
        # 2. Очищаем реестр AutoLoginUser
        try:
            WinregHelper.delete_value(r"Software\Valve\Steam", "AutoLoginUser")
        except:
            pass
        
        # 3. Перезапускаем Steam заново
        steam_path = self._settingsManager.get("SteamPath", r"C:\Program Files (x86)\Steam\steam.exe")
        
        WinregHelper.set_value(
            r"Software\Valve\Steam",
            "AutoLoginUser",
            self.login,
            winreg.REG_SZ
        )
        
        args = (
            f'{self._settingsManager.get("SteamArg", "-nofriendsui -vgui -noreactlogin")}'
            f' -applaunch 730 '
            f'-con_logfile {self.login}.log '
            f'{self._settingsManager.get("CS2Arg", "")}'
        )

        final = shlex.split(args)
        self.steamProcess = launch_isolated_steam(self.login, steam_path, final)
        print(f"✅ Steam [{self.login}] перезапущен (PID: {self.steamProcess.pid})")


    def get_level_xp(self):
        """✅ Возвращает текущие level/xp"""
        return self.level, self.xp

    def _get_weekly_window_start_iso(self):
        """Возвращает ISO-дату начала текущего недельного окна (ср, 03:00)."""
        current_time = datetime.now()
        reset_anchor = current_time.replace(hour=3, minute=0, second=0, microsecond=0)
        days_since_reset = (current_time.weekday() - 2) % 7
        week_start = reset_anchor - timedelta(days=days_since_reset)
        if current_time < week_start:
            week_start -= timedelta(days=7)
        return week_start.isoformat()

    def _load_level_from_json(self):
        """✅ Загружаем Level/XP из level.json"""
        from pathlib import Path
        import json
        
        level_file = Path("level.json")
        if level_file.exists():
            try:
                with open(level_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if self.login in data:
                    info = data[self.login]
                    self.level = info.get("level", 0)
                    self.xp = info.get("xp", 0)
                    print(f"✅ [{self.login}] Загружен из level.json: lvl: {self.level} xp: {self.xp}")
            except Exception as e:
                print(f"⚠️ [{self.login}] Ошибка level.json: {e}")

    def update_level_xp(self, level, xp):
        """✅ ОБНОВЛЯЕМ Level/XP + СОХРАНЯЕМ в level.json"""
        self.level = level
        self.xp = xp
        
        # ✅ Сохраняем в level.json
        from pathlib import Path
        import json
        
        level_file = Path("level.json")
        data = {}
        if level_file.exists():
            try:
                with open(level_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except:
                pass
        
        existing = data.get(self.login, {})
        if not isinstance(existing, dict):
            existing = {}

        existing["level"] = level
        existing["xp"] = xp

        # Для нового аккаунта (или если baseline был пустой) фиксируем первый
        # распарсенный уровень как weekly baseline.
        if not isinstance(existing.get("weekly_baseline_level"), int):
            existing["weekly_baseline_level"] = level

        if not existing.get("weekly_baseline_start"):
            existing["weekly_baseline_start"] = self._get_weekly_window_start_iso()

        data[self.login] = existing
        
        try:
            with open(level_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            print(f"✅ [{self.login}] Сохранено в level.json: Lv{level} XP{xp}")
        except Exception as e:
            print(f"⚠️ [{self.login}] Ошибка сохранения level.json: {e}")

    def parse_current_level(self):
        """🆕 РЕАЛЬНЫЙ ПАРСИНГ уровня (как try_get_level из ui/accounts_tab.py)"""
        try:
            from Helpers.LoginExecutor import SteamLoginSession
            
            print(f"🔍 [{self.login}] Парсим реальный уровень...")
            steam = SteamLoginSession(self.login, self.password, self.shared_secret)
            
            # Тот же код что и в try_get_level()
            html = self._fetch_steam_html(steam, "gcpd/730")
            if not html:
                print(f"⚠️ [{self.login}] Нет HTML")
                return False

            print(f"⏳ [{self.login}] Ждем JS...")
            time.sleep(1)

            level, xp = self._extract_level_xp_from_html(html)
            if level <= 0:
                print(f"⚠️ [{self.login}] Не удалось вытащить уровень из первой попытки. Повторяем логин...")
                html = self._fetch_steam_html(steam, "gcpd/730")
                if html:
                    level, xp = self._extract_level_xp_from_html(html)

            # 🔁 Последняя попытка: другая вкладка gcpd, иногда там есть нужные поля
            if level <= 0:
                html = self._fetch_steam_html(steam, "gcpd/730/?tab=matchmaking")
                if html:
                    level, xp = self._extract_level_xp_from_html(html)

            if level > 0:
                self.update_level_xp(level, xp)
                print(f"✅ [{self.login}] Уровень успешно обновлён")
                return True
            else:
                print(f"❌ [{self.login}] Уровень не найден")
                return False
                
        except Exception as e:
            print(f"❌ [{self.login}] Ошибка парсинга: {e}")
            return False

    def _extract_level_xp_from_html(self, html):
        """Пытается вытащить lvl/xp из разных форматов страницы Steam."""
        if not html:
            return 0, 0

        level, xp = 0, 0

        # Формат из текста страницы
        rank_match = re.search(r'CS:GO Profile Rank:\s*([\d,]+)', html, re.IGNORECASE)
        if rank_match:
            level = int(rank_match.group(1).replace(',', ''))
            xp_match = re.search(r'Experience points earned towards next rank:\s*([\d,]+)', html, re.IGNORECASE)
            xp = int(xp_match.group(1).replace(',', '')) if xp_match else 0
            return level, xp

        # Формат JSON внутри страницы
        rank_match = re.search(r'"profile_rank"[:\s]*(\d+)', html, re.IGNORECASE)
        if rank_match:
            level = int(rank_match.group(1))
            xp_match = re.search(r'"(?:current_)?xp"[:\s]*(\d+)', html, re.IGNORECASE)
            xp = int(xp_match.group(1)) if xp_match else 0
            return level, xp

        # Доп. парсинг под проблемные аккаунты (в т.ч. shadowcrypt94):
        # Steam иногда возвращает level в альтернативном поле
        rank_match = re.search(r'"player_level"[:\s]*(\d+)', html, re.IGNORECASE)
        if rank_match:
            level = int(rank_match.group(1))
            xp_match = re.search(r'"experience_points"[:\s]*(\d+)', html, re.IGNORECASE)
            xp = int(xp_match.group(1)) if xp_match else 0

        return level, xp

    def _fetch_steam_html(self, steam, url_suffix="gcpd/730/?tab=matchmaking"):
        """Вспомогательный метод для Steam HTML без sessions.json."""
        try:
            steam.login()
            resp = steam.session.get(f'https://steamcommunity.com/profiles/{steam.steamid}/{url_suffix}', timeout=10)
            if resp.status_code == 200:
                return resp.text
        except:
            pass
        return None


    def set_ui_callback(self, callback):
        """✅ Регистрируем callback для AccountsListFrame"""
        self._ui_callback = callback

    def notify_ui_level_update(self):
        """✅ Уведомляем UI об изменении уровня"""
        if self._ui_callback:
            self._ui_callback(self.login, self.level, self.xp)
        
    def close_steam_service_error(self, steam_pid: int, timeout: int = 60):
        """
        В течение timeout секунд ищет окно 'Steam Service Error'
        и закрывает ТОЛЬКО его, не завершая Steam
        """
        start_time = time.time()

        def worker():
            while time.time() - start_time < timeout:
                try:
                    windows = findwindows.find_windows(process=steam_pid)
                    for hwnd in windows:
                        title = win32gui.GetWindowText(hwnd)
                        if title and "Steam Service Error" in title:
                            print("⚠️ Найдено окно Steam Service Error — закрываем")
                            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                            return
                except Exception:
                    pass

                time.sleep(0.5)

        threading.Thread(target=worker, daemon=True).start()
        
    def MonitorCS2(self, interval: float = 2.0):
        """
        ПАССИВНЫЙ мониторинг CS2. Только отслеживает состояние, НИЧЕГО НЕ ЗАКРЫВАЕТ.
        Меняет цвет на серый при пропаже процесса.
        """
        self._stop_monitoring = False

        def monitor():
            while not self._stop_monitoring:
                # Если CS2Process не задан — ждём
                if not getattr(self, 'CS2Process', None):
                    time.sleep(interval)
                    continue

                # Проверяем жив ли процесс
                if psutil.pid_exists(self.CS2Process.pid):
                    # Живой CS2 = зелёный
                    if self._color != "green":
                        self.setColor("green")
                    time.sleep(interval)
                    continue

                # CS2 пропал — меняем цвет на серый (БЕЗ перезапусков/закрытий)
                print(f"⚪ [{self.login}] CS2.exe пропал (PID {self.CS2Process.pid})")
                self.CS2Process = None
                self.setColor("#DCE4EE")  # серый — CS2 закрыт
                
                # Ждём новый CS2 (пассивно)
                time.sleep(interval * 5)

        thread = threading.Thread(target=monitor, daemon=True)
        thread.start()


    def KillSteamAndCS(self):
        """
        Ручное завершение — ТОЛЬКО Steam, CS2 НЕ ТРОГАЕМ.
        """
        try:
            if self.steamProcess and psutil.pid_exists(self.steamProcess.pid):
                print(f"🛑 [{self.login}] Убиваем Steam (PID {self.steamProcess.pid})")
                self.steamProcess.kill()
                self.steamProcess = None
        except Exception as e:
            print(f"Ошибка Steam kill: {e}")

        # CS2 НЕ УБИВАЕМ — остаётся работать
        self.setColor("#DCE4EE")
        self._stop_monitoring = True

    def ProcessWindowsAfterCS(self, steamPid):
        """
        Закрывает все дополнительные окна Steam после авторизации.
        Окна CS2 всегда защищены и не трогаются.
        """
        try:
            parent = psutil.Process(steamPid)
            children = parent.children(recursive=True)
            all_pids = [steamPid] + [child.pid for child in children]

            for pid in all_pids:
                try:
                    windows = findwindows.find_windows(process=pid)
                    for hwnd in windows:
                        window_title = win32gui.GetWindowText(hwnd)
                        normalized_title = window_title.strip().lower()
                        
                        # 🔥 МАКСИМАЛЬНАЯ ЗАЩИТА CS2:
                        if ("counter-strike 2" in normalized_title or 
                            "cs2.exe" in normalized_title or 
                            self.login.lower() in normalized_title or
                            "[FREE]" in window_title):
                            print(f"🛡️ CS2 окно защищено: {window_title[:50]}...")
                            continue

                        # После логина закрываем любое доп. окно Steam-процесса,
                        # включая диалоги вроде "Change Password".
                        if win32gui.IsWindowVisible(hwnd) and win32gui.IsWindowEnabled(hwnd):
                            win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                            print(f"🪟 Закрыто доп. окно: {window_title[:50]}...")
                        else:
                            print(f"ℹ️ Окно неактивно/невидимо, пропущено: {window_title[:30]}...")
                                
                except Exception as e:
                    print(f"Ошибка PID {pid}: {e}")
        except Exception as e:
            print(f"ProcessWindowsAfterCS ошибка: {e}")
