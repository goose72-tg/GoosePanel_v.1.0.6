import time
from datetime import datetime
from pathlib import Path

import pyautogui
import pyperclip
import win32gui
import win32con
import win32process
import keyboard
import psutil

from Helpers.MouseController import MouseHelper
from Managers.SettingsManager import SettingsManager

class LobbyInstance:
    def __init__(self, leader, bots):
        self.leader = leader
        self.bots = bots
        self.last_collect_error = None

    @staticmethod
    def _is_cancelled():
        try:
            return keyboard.is_pressed("ctrl+q")
        except Exception:
            return False

    @staticmethod
    def _focus_window(hwnd):
        try:
            if not hwnd or not win32gui.IsWindow(hwnd):
                return False

            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                pass

            if not win32gui.IsWindow(hwnd):
                return False

            try:
                win32gui.BringWindowToTop(hwnd)
            except Exception:
                pass

            if not win32gui.IsWindow(hwnd):
                return False

            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                return False

            return True
        except Exception:
            return False


    @staticmethod
    def _is_cs2_process(pid):
        if not pid:
            return False
        try:
            proc = psutil.Process(pid)
            return (proc.name() or "").lower() == "cs2.exe"
        except Exception:
            return False

    def _resolve_member_cs2_hwnd(self, member):
        hwnd = 0
        try:
            hwnd = member.FindCSWindow()
        except Exception:
            hwnd = 0

        if hwnd and win32gui.IsWindow(hwnd):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            if self._is_cs2_process(pid):
                return hwnd

        pid = 0
        try:
            if getattr(member, 'CS2Process', None):
                pid = member.CS2Process.pid
        except Exception:
            pid = 0

        if not self._is_cs2_process(pid):
            return 0

        candidates = []

        def enum_cb(enum_hwnd, _):
            try:
                if not win32gui.IsWindow(enum_hwnd):
                    return True
                if not win32gui.IsWindowVisible(enum_hwnd):
                    return True
                if win32gui.GetParent(enum_hwnd) != 0:
                    return True

                _, hwnd_pid = win32process.GetWindowThreadProcessId(enum_hwnd)
                if hwnd_pid != pid:
                    return True

                rect = win32gui.GetWindowRect(enum_hwnd)
                area = max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])
                if area <= 0:
                    return True

                candidates.append((area, rect[0], rect[1], enum_hwnd))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            return 0

        if not candidates:
            return 0

        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        return candidates[0][3]

    def _resolve_member_hwnd(self, member):
        """CS2-aware HWND with safe fallback to raw FindCSWindow when needed."""
        hwnd = self._resolve_member_cs2_hwnd(member)
        if hwnd and win32gui.IsWindow(hwnd):
            return hwnd

        try:
            fallback = member.FindCSWindow()
        except Exception:
            fallback = 0

        if fallback and win32gui.IsWindow(fallback):
            return fallback

        return 0

    def _focus_member(self, member, retries=3, delay=0.12):
        for _ in range(max(1, retries)):
            hwnd = self._resolve_member_hwnd(member)
            if hwnd and self._focus_window(hwnd):
                return hwnd
            time.sleep(delay)
        return 0

    @staticmethod
    def _find_member_log_path(login):
        normalized_login = str(login or "").strip()
        if not normalized_login:
            return None

        filename = f"{normalized_login}.log"
        settings = SettingsManager()
        cs2_path = settings.get(
            "CS2Path",
            "C:/Program Files (x86)/Steam/steamapps/common/Counter-Strike Global Offensive",
        )

        search_roots = [
            Path.cwd(),
            Path(__file__).resolve().parent.parent,
            Path(cs2_path),
            Path(cs2_path) / "game" / "csgo",
        ]

        latest_path = None
        latest_mtime = 0.0
        target_name = filename.lower()

        for root in search_roots:
            if not root.exists():
                continue

            direct_path = root / filename
            if direct_path.is_file():
                mtime = direct_path.stat().st_mtime
                if mtime >= latest_mtime:
                    latest_mtime = mtime
                    latest_path = direct_path

            for path in root.rglob("*.log"):
                if not path.is_file() or path.name.lower() != target_name:
                    continue
                mtime = path.stat().st_mtime
                if mtime >= latest_mtime:
                    latest_mtime = mtime
                    latest_path = path

        return latest_path

    def _get_log_cursor(self, member):
        login = getattr(member, "login", "")
        if not login:
            return None, None

        log_path = self._find_member_log_path(login)
        if not log_path:
            print(f"❌ Лог не найден для [{login}]")
            return None, None

        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as log_file:
                cursor = log_file.seek(0, 2)
        except Exception:
            cursor = 0

        return log_path, cursor

    def _wait_log_phrase(self, member, phrase="JsFriendLobbyLeaderName", timeout=30.0, poll=0.2, start_cursor=0):
        login = getattr(member, "login", "")
        if not login:
            return False

        log_path = self._find_member_log_path(login)
        if not log_path:
            print(f"❌ Лог не найден для [{login}]")
            return False

        start_time = time.time()
        read_pos = max(0, int(start_cursor or 0))

        while time.time() - start_time < timeout:
            if self._is_cancelled():
                return False

            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as log_file:
                    log_file.seek(read_pos)
                    chunk = log_file.read()
                    read_pos = log_file.tell()
            except Exception:
                chunk = ""

            if phrase in chunk:
                print(f"✅ [{login}] Найдена строка '{phrase}' в {log_path}")
                return True

            time.sleep(poll)

        print(f"❌ [{login}] Не дождались строки '{phrase}' в {log_path}")
        return False

    @staticmethod
    def _parse_log_timestamp(line):
        if not line:
            return None

        prefix = line[:14]
        if len(prefix) < 14:
            return None

        try:
            base_ts = datetime.strptime(prefix, "%m/%d %H:%M:%S")
        except ValueError:
            return None

        now = datetime.now()
        candidate = base_ts.replace(year=now.year)

        # Логи не содержат год, поэтому на стыке года корректируем его вручную.
        if (candidate - now).total_seconds() > 86400:
            candidate = candidate.replace(year=now.year - 1)
        elif (now - candidate).total_seconds() > 366 * 86400:
            candidate = candidate.replace(year=now.year + 1)

        return candidate

    def _wait_log_phrase_in_window(
        self,
        member,
        phrase="JsFriendLobbyLeaderName",
        timeout=30.0,
        poll=0.2,
        start_cursor=0,
        center_ts=None,
        half_window_sec=30,
    ):
        login = getattr(member, "login", "")
        if not login:
            return False

        log_path = self._find_member_log_path(login)
        if not log_path:
            print(f"❌ Лог не найден для [{login}]")
            return False

        start_time = time.time()
        read_pos = max(0, int(start_cursor or 0))
        line_tail = ""
        window_start = None
        window_end = None

        if center_ts is not None:
            window_start = datetime.fromtimestamp(center_ts - half_window_sec)
            window_end = datetime.fromtimestamp(center_ts + half_window_sec)
            print(
                f"ℹ️ [{login}] Ищем '{phrase}' в окне {window_start.strftime('%m/%d %H:%M:%S')} - {window_end.strftime('%m/%d %H:%M:%S')}"
            )

        while time.time() - start_time < timeout:
            if self._is_cancelled():
                return False

            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as log_file:
                    log_file.seek(read_pos)
                    chunk = log_file.read()
                    read_pos = log_file.tell()
            except Exception:
                chunk = ""

            if chunk:
                lines = (line_tail + chunk).splitlines(keepends=False)
                if chunk and not chunk.endswith(("\n", "\r")):
                    line_tail = lines.pop() if lines else (line_tail + chunk)
                else:
                    line_tail = ""

                for line in lines:
                    if phrase not in line:
                        continue

                    if window_start is None or window_end is None:
                        print(f"✅ [{login}] Найдена строка '{phrase}' в {log_path}")
                        return True

                    line_ts = self._parse_log_timestamp(line)
                    if line_ts is None:
                        continue
                    if window_start <= line_ts <= window_end:
                        print(
                            f"✅ [{login}] Найдена строка '{phrase}' с таймкодом {line_ts.strftime('%m/%d %H:%M:%S')} в {log_path}"
                        )
                        return True

            time.sleep(poll)

        print(f"❌ [{login}] Не дождались строки '{phrase}' в {log_path}")
        return False

    def Collect(self):
        self.last_collect_error = None
        leader_hwnd = self._focus_member(self.leader)
        if not leader_hwnd:
            return False

        final_click_ts_by_login = {}

        for bot in self.bots:
            if self._is_cancelled():
                return False

            bot_hwnd = self._focus_member(bot)
            if not bot_hwnd:
                return False

            time.sleep(0.1)
            bot.MoveMouse(380, 100)
            time.sleep(0.5)
            bot.ClickMouse(375, 8)
            time.sleep(0.5)
            bot.ClickMouse(375, 8)
            time.sleep(0.5)
            bot.ClickMouse(204, 157)
            time.sleep(0.5)
            bot.ClickMouse(237, 157)

            if self._is_cancelled():
                return False

            leader_hwnd = self._focus_member(self.leader)
            if not leader_hwnd:
                return False

            self.leader.MoveMouse(380, 100)
            time.sleep(0.6)
            self.leader.ClickMouse(375, 8)
            time.sleep(1)
            MouseHelper.PasteText()
            time.sleep(1)
            self.leader.ClickMouse(195, 140)
            time.sleep(1.5)
            for i in range(142, 221, 5):
                self.leader.ClickMouse(235, i)
                time.sleep(0.001)
            self.leader.ClickMouse(235, 165)
            final_click_ts_by_login[getattr(bot, "login", "")] = time.time()

        time.sleep(1.5)

        for bot in self.bots:
            if self._is_cancelled():
                return False
            bot_hwnd = self._focus_member(bot)
            if not bot_hwnd:
                return False
            bot.MoveMouse(380, 100)
            time.sleep(0.6)
            if not self._wait_log_phrase_in_window(
                bot,
                center_ts=final_click_ts_by_login.get(getattr(bot, "login", "")),
            ):
                self.last_collect_error = "missing_js_friend_lobby_leader_name"
                return False
            bot.ClickMouse(306, 37)

        return True

    def Disband(self):
        # По ТЗ disband должен работать строго с bot1/bot2.
        primary_bots = self.bots[:1]

        for bot in primary_bots:
            if self._is_cancelled():
                return False
            bot_hwnd = self._focus_member(bot)
            if not bot_hwnd:
                return False
            time.sleep(0.1)
            bot.MoveMouse(380, 100)
            time.sleep(0.5)
            bot.ClickMouse(375, 8)

        return True