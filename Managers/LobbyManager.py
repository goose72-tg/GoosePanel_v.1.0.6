import ctypes
import time
import random
import json
import psutil
import win32gui
import win32api
import win32con
import win32process
import keyboard
from pathlib import Path

from Instances.LobbyInstance import LobbyInstance
from Managers.AccountsManager import AccountManager
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager


class LobbyManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(LobbyManager, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._accountManager = AccountManager()
        self._logManager = LogManager()
        self._settingManager = SettingsManager()

        self.team1 = None
        self.team2 = None
        self._last_window_order_logins = []

        self._maps_scrolled_once = False
        self._screen_grab_warning_logged = False
        self._collect_restart_reason = None
        self._initialized = True

    # -----------------------------
    # Validation / lifecycle
    # -----------------------------
    def isValid(self):
        if self.team1 is None or self.team2 is None:
            return False

        if not self.team1.leader.isCSValid():
            return False
        if any(not bot.isCSValid() for bot in self.team1.bots):
            return False

        if not self.team2.leader.isCSValid():
            return False
        if any(not bot.isCSValid() for bot in self.team2.bots):
            return False

        return True

    def CollectLobby(self):
        self._collect_restart_reason = None

        if self._is_cancelled():
            return False

        # Жесткий анализ: фиксируем ровно 4 слота окон 1..4 и собираем лобби строго из них.
        top4 = self._get_strict_4_accounts_by_window_order()
        if not top4:
            return False
        self._build_strict_lobbies_from_4(top4)

        # Перед действиями всегда выравниваем окна в линию 1-2-3-4
        if not self.MoveWindows(ordered_logins=self._last_window_order_logins):
            return False

        if not self._has_strict_pair_windows():
            self._logManager.add_log("❌ Strict collect failed: нужны полные пары окон 1/2 и 3/4")
            return False

        if self._is_cancelled():
            return False

        if self.team1 and self.team1.Collect() is False:
            if getattr(self.team1, "last_collect_error", None) == "missing_js_friend_lobby_leader_name":
                self._collect_restart_reason = "missing_js_friend_lobby_leader_name"
            return False
        if self.team2 and self.team2.Collect() is False:
            if getattr(self.team2, "last_collect_error", None) == "missing_js_friend_lobby_leader_name":
                self._collect_restart_reason = "missing_js_friend_lobby_leader_name"
            return False

        return True

    def DisbandLobbies(self):
        if self._is_cancelled():
            return False

        # Для disband используем именно текущих bot1/bot2 из активных команд.
        # Если команды ещё не собраны — тогда делаем анализ по окнам.
        if not self._ensure_lobbies_for_disband():
            return False

        # ВАЖНО: не переставляем окна перед disband, чтобы кликать по реальным bot1/bot2,
        # а не по "2-му/4-му" окну после принудительного MoveWindows.
        if self.team1 is not None:
            if self.team1.Disband() is False:
                return False
            self.team1 = None
        if self.team2 is not None:
            if self.team2.Disband() is False:
                return False
            self.team2 = None

        return True

    def _ensure_lobbies_for_disband(self):
        if self.team1 and self.team2 and self._has_primary_bots(self.team1, self.team2):
            return True
        return self._auto_create_lobbies()

    @staticmethod
    def _has_primary_bots(team1, team2):
        return bool(getattr(team1, 'bots', None)) and bool(getattr(team2, 'bots', None))

    @staticmethod
    def _is_cs2_process(pid):
        if not pid:
            return False
        try:
            proc = psutil.Process(pid)
            return (proc.name() or "").lower() == "cs2.exe"
        except Exception:
            return False

    def _resolve_account_cs2_hwnd(self, account):
        hwnd = 0
        try:
            hwnd = account.FindCSWindow()
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
            if getattr(account, 'CS2Process', None):
                pid = account.CS2Process.pid
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
                width = max(0, rect[2] - rect[0])
                height = max(0, rect[3] - rect[1])
                area = width * height
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

    def _has_strict_pair_windows(self):
        if not self.team1 or not self.team2:
            return False
        if not self._has_primary_bots(self.team1, self.team2):
            return False

        members = [self.team1.leader, self.team1.bots[0], self.team2.leader, self.team2.bots[0]]
        positions = []
        seen_hwnds = set()

        for member in members:
            hwnd = self._resolve_account_cs2_hwnd(member)
            if not hwnd or not win32gui.IsWindow(hwnd) or hwnd in seen_hwnds:
                return False
            try:
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                return False

            seen_hwnds.add(hwnd)
            positions.append((rect[0], rect[1], member.login))

        expected_order = [member.login for member in members]
        actual_order = [item[2] for item in sorted(positions, key=lambda item: (item[0], item[1]))]
        return actual_order == expected_order

    def MoveWindows(self, ordered_logins=None):
        if not self.team1 or not self.team2:
            return False

        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        ordered_members = []
        all_members = [self.team1.leader] + self.team1.bots + [self.team2.leader] + self.team2.bots
        member_by_login = {m.login: m for m in all_members if hasattr(m, 'login')}

        # По умолчанию: строго по списку аккаунтов.
        # Для Shuffle можно передать random ordered_logins.
        if ordered_logins:
            order_source = ordered_logins
        elif self._last_window_order_logins:
            order_source = self._last_window_order_logins
        else:
            order_source = [acc.login for acc in self._accountManager.accounts]

        for login in order_source:
            member = member_by_login.get(login)
            if member:
                ordered_members.append(member)

        if not ordered_members:
            ordered_members = all_members

        target_width = 383
        target_height = 280
        y = 0
        placed = 0

        for member in ordered_members:
            if self._is_cancelled():
                return False

            try:
                hwnd = self._resolve_account_cs2_hwnd(member)
                if not hwnd or not win32gui.IsWindow(hwnd):
                    continue

                x = placed * target_width
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                win32gui.MoveWindow(hwnd, x, y, target_width, target_height, True)
                win32gui.SetWindowText(hwnd, f"[FSN FREE] {member.login}")
                placed += 1
            except Exception:
                continue

        return placed > 0

    def Shuffle(self):
        if self._is_cancelled():
            return False

        valid_accounts = [acc for acc in self._accountManager.accounts if acc.isCSValid()]
        if len(valid_accounts) < 4:
            self._logManager.add_log("❌ Недостаточно активных CS аккаунтов для Shuffle")
            return False

        random.shuffle(valid_accounts)
        random_order_logins = [acc.login for acc in valid_accounts]
        mid = len(valid_accounts) // 2

        self.team1 = LobbyInstance(valid_accounts[0], valid_accounts[1:mid])
        self.team2 = LobbyInstance(valid_accounts[mid], valid_accounts[mid + 1:])
        self._last_window_order_logins = random_order_logins

        moved = self.MoveWindows(ordered_logins=random_order_logins)
        if moved:
            self._logManager.add_log("🔀 Shuffle выполнен")
        return moved

    def _auto_create_lobbies(self):
        ordered_accounts = self._get_accounts_sorted_by_window_position()
        total = len(ordered_accounts)
        if total < 4:
            self._logManager.add_log("❌ Нужно минимум 4 валидных CS2 окна для сборки лобби")
            return False

        leader1 = ordered_accounts[0]
        bot1 = ordered_accounts[1]
        leader2 = ordered_accounts[2]
        bot2 = ordered_accounts[3]

        bots1 = [bot1]
        bots2 = [bot2]

        # Если аккаунтов больше 4 — дальше строго чередуем ботов между командами
        for index, account in enumerate(ordered_accounts[4:], start=4):
            if index % 2 == 0:
                bots1.append(account)
            else:
                bots2.append(account)

        self.team1 = LobbyInstance(leader1, bots1)
        self.team2 = LobbyInstance(leader2, bots2)
        self._last_window_order_logins = [acc.login for acc in ordered_accounts]

        return True

    def _get_strict_4_accounts_by_window_order(self):
        """Возвращает строго 4 аккаунта в порядке окон слева-направо (слоты 1..4)."""
        ordered_accounts = self._get_accounts_sorted_by_window_position()
        if len(ordered_accounts) < 4:
            self._logManager.add_log("❌ Нужно минимум 4 валидных CS2 окна")
            return None

        top4 = ordered_accounts[:4]
        seen_hwnds = set()

        for acc in top4:
            hwnd = self._resolve_account_cs2_hwnd(acc)
            if not hwnd or not win32gui.IsWindow(hwnd):
                self._logManager.add_log(f"❌ Не найдено окно CS2 для {getattr(acc, 'login', 'unknown')}")
                return None
            if hwnd in seen_hwnds:
                self._logManager.add_log("❌ Дубли hwnd среди 4 слотов — порядок окон нестабилен")
                return None
            seen_hwnds.add(hwnd)

        return top4

    def _build_strict_lobbies_from_4(self, top4_accounts):
        """Жёсткая сборка: slot1=leader1, slot2=bot1, slot3=leader2, slot4=bot2."""
        leader1, bot1, leader2, bot2 = top4_accounts
        self.team1 = LobbyInstance(leader1, [bot1])
        self.team2 = LobbyInstance(leader2, [bot2])
        self._last_window_order_logins = [acc.login for acc in top4_accounts]

        return True

    def _prepare_strict_4_windows_flow(self):
        """Подготовка без дополнительных пауз: move all -> align -> strict check."""
        self.lift_all_cs2_windows()

        top4 = self._get_strict_4_accounts_by_window_order()
        if not top4:
            return False
        self._build_strict_lobbies_from_4(top4)

        if not self.MoveWindows(ordered_logins=self._last_window_order_logins):
            self._logManager.add_log("❌ MoveWindows failed during strict pre-start")
            return False

        if not self._has_strict_pair_windows():
            self._logManager.add_log("❌ Strict check failed after MoveWindows: нужны пары 1/2 и 3/4")
            return False

        return True

    def _get_accounts_sorted_by_window_position(self):
        valid_accounts = [acc for acc in self._accountManager.accounts if acc.isCSValid()]
        if not valid_accounts:
            return []

        ordered = []
        missing_windows = []

        for order_index, account in enumerate(valid_accounts):
            hwnd = self._resolve_account_cs2_hwnd(account)
            if not hwnd:
                missing_windows.append(account.login)
                continue

            try:
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                missing_windows.append(account.login)
                continue

            ordered.append((rect[0], rect[1], order_index, account, hwnd))

        if missing_windows:
            self._logManager.add_log(f"⚠️ Пропущены аккаунты без окна CS2: {', '.join(missing_windows)}")

        # Строго: только слева направо. При равном X сохраняем исходный порядок аккаунтов.
        ordered.sort(key=lambda item: (item[0], item[1], item[2]))

        return [item[3] for item in ordered]

    def _get_rect_for_account_window(self, account):
        pid = 0
        try:
            if account.CS2Process:
                pid = account.CS2Process.pid
        except Exception:
            pid = 0

        if not pid:
            return None

        best = None

        def enum_cb(hwnd, _):
            nonlocal best
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if win32gui.GetParent(hwnd) != 0:
                    return True

                _, hwnd_pid = win32process.GetWindowThreadProcessId(hwnd)
                if hwnd_pid != pid:
                    return True

                title = win32gui.GetWindowText(hwnd)
                if not title:
                    return True

                rect = win32gui.GetWindowRect(hwnd)
                if not best:
                    best = rect
                    return True

                # Берем самое левое окно процесса; если X равен — самое верхнее.
                if rect[0] < best[0] or (rect[0] == best[0] and rect[1] < best[1]):
                    best = rect
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            return None

        return best

    # -----------------------------
    # Win32 helpers (shared)
    # -----------------------------
    @staticmethod
    def _is_cancelled():
        try:
            return keyboard.is_pressed("ctrl+q")
        except Exception:
            return False

    @staticmethod
    def _sleep_with_cancel(duration, step=0.05):
        if duration <= 0:
            return False

        end_time = time.time() + duration
        while True:
            if LobbyManager._is_cancelled():
                return True

            remaining = end_time - time.time()
            if remaining <= 0:
                return False

            time.sleep(max(0.0, min(step, remaining)))

    def _grab_avg_color_2x2(self, x, y, rect, image_grab):
        left = rect[0] + x
        top = rect[1] + y
        right = left + 2
        bottom = top + 2

        if right <= left or bottom <= top:
            return None

        try:
            img = image_grab.grab(bbox=(left, top, right, bottom))
        except Exception as e:
            if not self._screen_grab_warning_logged:
                self._logManager.add_log(f"⚠️ Pixel sampling unavailable for some windows: {e}")
                self._screen_grab_warning_logged = True
            return None

        r_sum = g_sum = b_sum = 0
        count = 0
        for px in range(img.size[0]):
            for py in range(img.size[1]):
                r, g, b = img.getpixel((px, py))[:3]
                r_sum += r
                g_sum += g
                b_sum += b
                count += 1

        if count == 0:
            return None

        return (r_sum // count, g_sum // count, b_sum // count)

    def _safe_activate_hwnd(self, hwnd):
        """
        Более простой и стабильный foreground-activation по HWND.
        """
        if not hwnd:
            return False

        attached = False
        fg_tid = 0
        hwnd_tid = 0

        try:
            if not win32gui.IsWindow(hwnd):
                return False

            try:
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            except Exception:
                pass

            fg = 0
            try:
                fg = win32gui.GetForegroundWindow()
            except Exception:
                fg = 0

            try:
                fg_tid, _ = win32process.GetWindowThreadProcessId(fg)
            except Exception:
                fg_tid = 0

            try:
                hwnd_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                hwnd_tid = 0

            user32 = ctypes.windll.user32

            if fg_tid and hwnd_tid and fg_tid != hwnd_tid:
                try:
                    user32.AttachThreadInput(fg_tid, hwnd_tid, True)
                    attached = True
                except Exception:
                    attached = False

            try:
                win32gui.BringWindowToTop(hwnd)
            except Exception:
                pass

            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                return False

            time.sleep(0.12)
            return True

        except Exception:
            return False

        finally:
            if attached and fg_tid and hwnd_tid and fg_tid != hwnd_tid:
                try:
                    ctypes.windll.user32.AttachThreadInput(fg_tid, hwnd_tid, False)
                except Exception:
                    pass

    def _safe_set_foreground(self, hwnd):
        return self._safe_activate_hwnd(hwnd)

    def _load_runtime_cs2_pids(self):
        pids = []
        try:
            with open("runtime.json", "r", encoding="utf-8") as runtime_file:
                data = json.load(runtime_file)
        except Exception:
            return pids

        if not isinstance(data, list):
            return pids

        for item in data:
            if not isinstance(item, dict):
                continue
            cs2_pid = item.get("CS2Pid")
            if cs2_pid is None:
                continue
            try:
                pid = int(cs2_pid)
            except (TypeError, ValueError):
                continue
            pids.append(pid)

        return pids

    def _find_cs2_hwnd_by_pid(self, pid):
        if not pid:
            return 0

        candidates = []

        def enum_cb(hwnd, _):
            try:
                if not win32gui.IsWindow(hwnd):
                    return True
                if not win32gui.IsWindowVisible(hwnd) or win32gui.GetParent(hwnd) != 0:
                    return True

                _, hwnd_pid = win32process.GetWindowThreadProcessId(hwnd)
                if hwnd_pid != pid:
                    return True

                rect = win32gui.GetWindowRect(hwnd)
                width = max(0, rect[2] - rect[0])
                height = max(0, rect[3] - rect[1])
                area = width * height
                if area <= 0:
                    return True

                candidates.append((area, rect[0], rect[1], hwnd))
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

    def _activate_hwnd_for_input(self, hwnd):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False

        if not self._safe_activate_hwnd(hwnd):
            return False

        try:
            rect = win32gui.GetWindowRect(hwnd)
            x = rect[0] + 120
            y = rect[1] + 80
            win32api.SetCursorPos((x, y))
            time.sleep(0.05)
        except Exception:
            pass

        return True

    def _send_esc(self, hwnd):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False

        try:
            scan_code = win32api.MapVirtualKey(win32con.VK_ESCAPE, 0)
            win32api.keybd_event(win32con.VK_ESCAPE, scan_code, 0, 0)
            time.sleep(0.03)
            win32api.keybd_event(win32con.VK_ESCAPE, scan_code, win32con.KEYEVENTF_KEYUP, 0)
            return True
        except Exception:
            try:
                win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
                win32api.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
                return True
            except Exception:
                return False

    def _click_in_window(self, hwnd, x, y, hover_delay=0.3):
        try:
            rect = win32gui.GetWindowRect(hwnd)
            abs_x = rect[0] + int(x)
            abs_y = rect[1] + int(y)
            win32api.SetCursorPos((abs_x, abs_y))
            time.sleep(max(0.0, float(hover_delay)))
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True
        except Exception:
            return False

    def _get_cs2_hwnds(self):
        cs2_pids = set()
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if (proc.info.get('name') or '').lower() == 'cs2.exe':
                    cs2_pids.add(int(proc.info['pid']))
            except Exception:
                continue

        if not cs2_pids:
            return []

        hwnd_list = []

        def enum_cb(hwnd, _):
            try:
                if not win32gui.IsWindow(hwnd):
                    return True
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if win32gui.GetParent(hwnd) != 0:
                    return True

                _, hwnd_pid = win32process.GetWindowThreadProcessId(hwnd)
                if hwnd_pid not in cs2_pids:
                    return True

                rect = win32gui.GetWindowRect(hwnd)
                width = max(0, rect[2] - rect[0])
                height = max(0, rect[3] - rect[1])
                if width * height <= 0:
                    return True

                hwnd_list.append((rect[0], rect[1], hwnd_pid, hwnd))
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(enum_cb, None)
        except Exception:
            return []

        hwnd_list.sort(key=lambda item: (item[0], item[1], item[2]))
        return [item[3] for item in hwnd_list]

    def _reset_search_in_all_cs2_windows(self):
        hwnds = self._get_cs2_hwnds()
        if not hwnds:
            return False

        processed = 0

        for hwnd in hwnds:
            if self._is_cancelled():
                return False
            if not win32gui.IsWindow(hwnd):
                continue

            if not self._run_esc_click_esc_sequence(hwnd):
                return False

            processed += 1

        return processed > 0

    def _run_esc_click_esc_sequence(self, hwnd, x=374, y=8, delay=0.4):
        self._safe_activate_hwnd(hwnd)
        self._send_esc(hwnd)
        if self._sleep_with_cancel(delay):
            return False

        self._click_in_window(hwnd, x, y, hover_delay=delay)
        if self._sleep_with_cancel(delay):
            return False

        self._send_esc(hwnd)
        if self._sleep_with_cancel(delay):
            return False

        return True

    def _build_log_watchers(self):
        cs2_path = self._settingManager.get(
            "CS2Path",
            "C:/Program Files (x86)/Steam/steamapps/common/Counter-Strike Global Offensive",
        )

        root_candidates = [
            Path(cs2_path),
            Path(cs2_path) / "game" / "csgo",
            Path(cs2_path) / "csgo",
        ]

        logins = []
        for team in (self.team1, self.team2):
            if not team:
                continue
            members = [team.leader] + list(getattr(team, "bots", []) or [])
            for member in members:
                login = str(getattr(member, "login", "") or "").strip()
                if login:
                    logins.append(login)

        watchers = {}
        for login in sorted(set(logins)):
            filename = f"{login}.log".lower()
            found_path = None
            latest_mtime = -1.0

            for root in root_candidates:
                if not root.exists():
                    continue

                direct = root / f"{login}.log"
                if direct.is_file():
                    try:
                        mtime = direct.stat().st_mtime
                    except Exception:
                        mtime = -1.0
                    if mtime >= latest_mtime:
                        latest_mtime = mtime
                        found_path = direct

                try:
                    candidates = root.rglob("*.log")
                except Exception:
                    candidates = []

                for path in candidates:
                    if not path.is_file() or path.name.lower() != filename:
                        continue
                    try:
                        mtime = path.stat().st_mtime
                    except Exception:
                        mtime = -1.0
                    if mtime >= latest_mtime:
                        latest_mtime = mtime
                        found_path = path

            if not found_path:
                continue

            try:
                with open(found_path, "r", encoding="utf-8", errors="ignore") as log_file:
                    cursor = log_file.seek(0, 2)
            except Exception:
                cursor = 0

            watchers[login] = {"path": found_path, "cursor": cursor}

        return watchers

    def _has_datacenter_ping_error(self, watchers, phrase="No official datacenters pingable"):
        for login, state in watchers.items():
            log_path = state.get("path")
            if not log_path:
                continue

            read_pos = max(0, int(state.get("cursor") or 0))

            try:
                with open(log_path, "r", encoding="utf-8", errors="ignore") as log_file:
                    log_file.seek(read_pos)
                    chunk = log_file.read()
                    state["cursor"] = log_file.tell()
            except Exception:
                continue

            if phrase in chunk:
                self._logManager.add_log(
                    f"⚠️ [{login}] Найдено '{phrase}'. Перезапускаем Make lobbies & search game"
                )
                return True

        return False

    def _click_window_relative(self, hwnd, x, y):
        if not hwnd or not win32gui.IsWindow(hwnd):
            return False

        try:
            screen_x, screen_y = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
            win32api.SetCursorPos((screen_x, screen_y))
            time.sleep(0.03)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True
        except Exception:
            try:
                client_x = int(x)
                client_y = int(y)
                lparam = win32api.MAKELONG(client_x, client_y)
                win32api.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lparam)
                win32api.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
                win32api.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)
                return True
            except Exception:
                return False

    def lift_all_cs2_windows(self):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

        cs2_pids = []
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                name = (proc.info.get('name') or "").lower()
                if name == "cs2.exe":
                    cs2_pids.append(proc.info['pid'])
            except Exception:
                continue

        if not cs2_pids:
            return 0

        processed = set()
        lifted = 0

        def enum_cb(hwnd, _):
            nonlocal lifted
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                if win32gui.GetParent(hwnd) != 0:
                    return True

                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid not in cs2_pids or pid in processed:
                    return True

                title = win32gui.GetWindowText(hwnd)
                if not title:
                    return True

                processed.add(pid)
                self._safe_activate_hwnd(hwnd)
                lifted += 1
                time.sleep(0.05)
            except Exception:
                pass
            return True

        win32gui.EnumWindows(enum_cb, None)
        return lifted

    def press_esc_all_cs2_windows(self):
        cs2_pids = set()
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                if (proc.info.get('name') or '').lower() == 'cs2.exe':
                    cs2_pids.add(int(proc.info['pid']))
            except Exception:
                continue

        if not cs2_pids:
            self._logManager.add_log("⚠️ cs2.exe процессы не найдены")
            return 0

        runtime_ordered_pids = []
        seen_runtime = set()
        for pid in self._load_runtime_cs2_pids():
            if pid in cs2_pids and pid not in seen_runtime:
                runtime_ordered_pids.append(pid)
                seen_runtime.add(pid)

        remaining_pids = sorted(pid for pid in cs2_pids if pid not in seen_runtime)
        ordered_pids = runtime_ordered_pids + remaining_pids

        processed = 0

        for pid in ordered_pids:
            if self._is_cancelled():
                return processed

            hwnd = self._find_cs2_hwnd_by_pid(pid)
            if not hwnd:
                continue

            if not win32gui.IsWindow(hwnd):
                continue

            if not self._activate_hwnd_for_input(hwnd):
                # Клик и ESC отправляем всё равно по HWND, даже если foreground не взяли.
                pass

            if self._sleep_with_cancel(0.15):
                return processed

            if not self._click_window_relative(hwnd, 375, 8):
                continue

            if self._sleep_with_cancel(0.15):
                return processed

            if not self._send_esc(hwnd):
                continue

            if self._sleep_with_cancel(0.2):
                return processed

            if not self._click_window_relative(hwnd, 375, 8):
                continue

            if self._sleep_with_cancel(0.15):
                return processed

            if not self._send_esc(hwnd):
                continue

            if self._sleep_with_cancel(0.2):
                return processed

            processed += 1
        

        return processed

    def _press_red_buttons_everywhere(self, final_click_pos, enforce_green=False, max_wait=12.0, leaders_only=False):
        from PIL import ImageGrab

        def get_avg_color_2x2(x, y, rect):
            return self._grab_avg_color_2x2(x, y, rect, ImageGrab)

        def button_state(x, y, rect):
            avg = get_avg_color_2x2(x, y, rect)
            if avg is None:
                return None
            r, g, b = avg
            if r > g + 20 and r > b + 20:
                return "red"
            if g > r + 20 and g > b + 20:
                return "green"
            return "red"

        def click_rel(x, y, rect, hwnd):
            if self._is_cancelled():
                return False
            self._safe_activate_hwnd(hwnd)
            abs_x = rect[0] + x
            abs_y = rect[1] + y
            win32api.SetCursorPos((abs_x, abs_y))
            if self._sleep_with_cancel(0.03):
                return False
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            if self._sleep_with_cancel(0.03):
                return False
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True

        members = []
        if leaders_only:
            if self.team1 and getattr(self.team1, 'leader', None):
                members.append(self.team1.leader)
            if self.team2 and getattr(self.team2, 'leader', None):
                members.append(self.team2.leader)

            if not members:
                ordered = self._get_accounts_sorted_by_window_position()
                if len(ordered) >= 3:
                    members = [ordered[0], ordered[2]]
        else:
            if self.team1:
                members.extend([self.team1.leader] + self.team1.bots)
            if self.team2:
                members.extend([self.team2.leader] + self.team2.bots)

        if not members:
            members = [acc for acc in self._accountManager.accounts if acc.isCSValid()]

        if not members:
            return True

        deadline = time.time() + max_wait
        warned_unknown = False

        while True:
            any_red = False
            all_green = True

            for acc in members:
                hwnd = self._resolve_account_cs2_hwnd(acc)
                if not hwnd:
                    continue
                try:
                    rect = win32gui.GetWindowRect(hwnd)
                except Exception:
                    continue

                state = button_state(final_click_pos[0], final_click_pos[1], rect)
                if state is None:
                    all_green = False
                    if not warned_unknown:
                 
                        warned_unknown = True
                    continue

                if state == "red":
                    any_red = True
                    all_green = False
                    if not click_rel(final_click_pos[0], final_click_pos[1], rect, hwnd):
                        return False
                    if self._sleep_with_cancel(0.1):
                        return False

            if not enforce_green:
                return True

            if all_green:
                return True

            if time.time() >= deadline:
      
                return False

            if not any_red and self._sleep_with_cancel(0.15):
                return False

    def _recover_after_match_timeout(self, final_click_pos):
        self._logManager.add_log("⏱ 600s timeout without accepted match. Running recovery flow.")
        self._logManager.add_log("🔴→🟢 Timeout reached: forcing red buttons to green on leader windows (1 & 3)")

        if not self._press_red_buttons_everywhere(final_click_pos, enforce_green=True, max_wait=20.0, leaders_only=True):
            return False

        self.press_esc_all_cs2_windows()
        if self._is_cancelled():
            return False

        if not self.DisbandLobbies():
            self._logManager.add_log("⚠️ DisbandLobbies failed")
        if self._is_cancelled():
            return False

        if not self.Shuffle():
            self._logManager.add_log("⚠️ Shuffle failed")
            return False
        if self._is_cancelled():
            return False

        return True

    # -----------------------------
    # Main flow (по ТЗ)
    # -----------------------------
    def MakeLobbiesAndSearchGame(self):
        from PIL import ImageGrab
        from Modules.AutoAcceptModule import AutoAcceptModule

        AutoAcceptModule.reset_final_clicks_state()

        self.press_esc_all_cs2_windows()

        if self._is_cancelled():
            return False

        if self._sleep_with_cancel(0.4):
            return False

        if not self._prepare_strict_4_windows_flow():
            return False

        if self._is_cancelled():
            return False

        FINAL_CLICK = (289, 271)
        OPEN_SEQ = [(206, 8), (154, 23), (142, 33)]
        BOT_TAB_CLICK = (231, 8)
        BOT_STATUS_PIXEL = (324, 103)
        max_cycles = 3

        def click_rel(x, y, rect, hwnd):
            if self._is_cancelled():
                return False
            self._safe_activate_hwnd(hwnd)
            abs_x = rect[0] + x
            abs_y = rect[1] + y
            win32api.SetCursorPos((abs_x, abs_y))
            if self._sleep_with_cancel(0.03):
                return False
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            if self._sleep_with_cancel(0.03):
                return False
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return True

        def get_team_info(team):
            if not team or not team.leader:
                return None
            hwnd = self._resolve_account_cs2_hwnd(team.leader)
            if not hwnd:
                return None
            try:
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                return None
            return {"hwnd": hwnd, "rect": rect}

        def get_account_info(account):
            if not account:
                return None
            hwnd = self._resolve_account_cs2_hwnd(account)
            if not hwnd:
                return None
            try:
                rect = win32gui.GetWindowRect(hwnd)
            except Exception:
                return None
            return {"hwnd": hwnd, "rect": rect}

        def get_button_state(info):
            if not info:
                return None
            avg = self._grab_avg_color_2x2(FINAL_CLICK[0], FINAL_CLICK[1], info["rect"], ImageGrab)
            if avg is None:
                return None
            r, g, b = avg
            if r == 0 and g == 0 and b == 0:
                return "black"
            if r > g + 20 and r > b + 20:
                return "red"
            if g > r + 20 and g > b + 20:
                return "green"
            return "red"

        def click_final(info):
            return click_rel(FINAL_CLICK[0], FINAL_CLICK[1], info["rect"], info["hwnd"])

        def _apply_pair_final_click_logic(info_a, info_b):
            if not info_a or not info_b:
                return None

            s_a = get_button_state(info_a)
            s_b = get_button_state(info_b)
            if s_a is None or s_b is None:
                return None
            if s_a == "black" or s_b == "black":
                return "black"

            if s_a == "red" and s_b == "green":
                if not click_final(info_a):
                    return False

                if self._sleep_with_cancel(0.15):
                    return False

                s_a_new = get_button_state(info_a)
                s_b_new = get_button_state(info_b)
                if s_a_new == "green" and s_b_new == "green":
                    if not click_final(info_a):
                        return False
                    if not click_final(info_b):
                        return False

            elif s_a == "green" and s_b == "red":
                if not click_final(info_b):
                    return False

                if self._sleep_with_cancel(0.15):
                    return False

                s_a_new = get_button_state(info_a)
                s_b_new = get_button_state(info_b)
                if s_a_new == "green" and s_b_new == "green":
                    if not click_final(info_a):
                        return False
                    if not click_final(info_b):
                        return False

            elif s_a == "green" and s_b == "green":
                if not click_final(info_a):
                    return False
                if not click_final(info_b):
                    return False

            # Если обе красные — ничего не делаем по ТЗ.
            return True

        def _run_all_final_click_pairs(include_bots=False):
            leader1_info = get_team_info(self.team1)
            leader2_info = get_team_info(self.team2)
            if not leader1_info or not leader2_info:
                self._logManager.add_log("❌ Не удалось получить окна лидеров перед финальными кликами")
                return False

            leaders_pair_result = _apply_pair_final_click_logic(leader1_info, leader2_info)
            if leaders_pair_result is False:
                return False
            if leaders_pair_result == "black":
                return "black"

            if not include_bots:
                return True

            bot1 = self.team1.bots[0] if self.team1 and getattr(self.team1, "bots", None) else None
            bot2 = self.team2.bots[0] if self.team2 and getattr(self.team2, "bots", None) else None
            if not bot1 or not bot2:
                return True

            bot1_info = get_account_info(bot1)
            bot2_info = get_account_info(bot2)
            if not bot1_info or not bot2_info:
                self._logManager.add_log("⚠️ Не удалось получить окна bot1/bot2 для финальных кликов")
                return False

            bots_pair_result = _apply_pair_final_click_logic(bot1_info, bot2_info)
            if bots_pair_result is False:
                return False
            if bots_pair_result == "black":
                return "black"

            return True

        def _is_black_color(avg):
            if avg is None:
                return False
            r, g, b = avg
            # Для проверки bot1/bot2 считаем "не в команде" только реальный чёрный пиксель.
            return r == 0 and g == 0 and b == 0

        def _click_other_leader_if_red_when_black():
            leader1_info = get_team_info(self.team1)
            leader2_info = get_team_info(self.team2)
            if not leader1_info or not leader2_info:
                return False

            state1 = get_button_state(leader1_info)
            state2 = get_button_state(leader2_info)
            if state1 is None or state2 is None:
                return False

            if state1 == "black" and state2 == "red":
                self._logManager.add_log("⚫ leader1=black, кликаем красную кнопку leader2 перед рестартом цикла")
                return click_final(leader2_info)

            if state2 == "black" and state1 == "red":
                self._logManager.add_log("⚫ leader2=black, кликаем красную кнопку leader1 перед рестартом цикла")
                return click_final(leader1_info)

            return True

        def _reset_windows_after_black_bot_status():
            members = []
            if self.team1:
                members.extend([self.team1.leader] + self.team1.bots)
            if self.team2:
                members.extend([self.team2.leader] + self.team2.bots)

            if not members:
                members = [acc for acc in self._accountManager.accounts if acc.isCSValid()]

            # Уберём дубликаты по логину, чтобы не кликать одно окно несколько раз.
            unique_members = []
            seen_logins = set()
            for acc in members:
                login = getattr(acc, "login", None)
                key = login or id(acc)
                if key in seen_logins:
                    continue
                seen_logins.add(key)
                unique_members.append(acc)

            if not unique_members:
                return False

            for acc in unique_members:
                if self._is_cancelled():
                    return False

                hwnd = self._resolve_account_cs2_hwnd(acc)
                if not hwnd:
                    continue

                self._safe_activate_hwnd(hwnd)
                if self._sleep_with_cancel(0.1):
                    return False

                if not self._click_window_relative(hwnd, 375, 8):
                    continue

                if self._sleep_with_cancel(0.1):
                    return False

                self._send_esc(hwnd)
                if self._sleep_with_cancel(0.1):
                    return False

                if not self._click_window_relative(hwnd, 375, 8):
                    continue

                if self._sleep_with_cancel(0.1):
                    return False

                self._send_esc(hwnd)

            return True

        def verify_primary_bots_joined():
            bots = []
            if self.team1 and getattr(self.team1, "bots", None):
                bots.append(("bot1", self.team1.bots[0]))
            if self.team2 and getattr(self.team2, "bots", None):
                bots.append(("bot2", self.team2.bots[0]))

            if not bots:
                return False

            all_joined = True
            for bot_name, bot_account in bots:
                info = get_account_info(bot_account)
                if not info:
                    return False

                self._safe_activate_hwnd(info["hwnd"])
                if self._sleep_with_cancel(0.2):
                    return False

                if not click_rel(BOT_TAB_CLICK[0], BOT_TAB_CLICK[1], info["rect"], info["hwnd"]):
                    return False

                if self._sleep_with_cancel(0.2):
                    return False

                avg = self._grab_avg_color_2x2(
                    BOT_STATUS_PIXEL[0],
                    BOT_STATUS_PIXEL[1],
                    info["rect"],
                    ImageGrab
                )

                if _is_black_color(avg):
                    all_joined = False

            return all_joined

        def rebuild_strict_slots_or_fail():
            top4_accounts = self._get_strict_4_accounts_by_window_order()
            if not top4_accounts:
                return False
            self._build_strict_lobbies_from_4(top4_accounts)
            return self._has_strict_pair_windows()

        cycle = 1
        while cycle <= max_cycles:
            if AutoAcceptModule.final_clicks_disabled():
                return True

            self._logManager.add_log(f"🚀 Start cycle {cycle}/{max_cycles}")

            self.press_esc_all_cs2_windows()

            if self._is_cancelled():
                return False

            self._maps_scrolled_once = False

            # На каждом цикле заново фиксируем 1..4 строго по реальным CS2 окнам.
            if not rebuild_strict_slots_or_fail():
                self._logManager.add_log("❌ Abort search: не удалось строго зафиксировать окна 1/2/3/4")
                return False

            if self.CollectLobby() is False:
                if self._collect_restart_reason == "missing_js_friend_lobby_leader_name":
                    self._logManager.add_log("⚠️ Не найден 'JsFriendLobbyLeaderName' за 30с. Перезапускаем цикл поиска.")
                    if not self._reset_search_in_all_cs2_windows():
                        return False
                    continue
                return False

            if not self._has_strict_pair_windows():
                self._logManager.add_log("❌ Abort search: собраны нестрого. Нужны пары 1/2 и 3/4")
                return False

            if AutoAcceptModule.final_clicks_disabled():
                self._logManager.add_log("✅ Match detected during lobby collect. Stopping search flow.")
                return True

            if not self.MoveWindows(ordered_logins=self._last_window_order_logins):
                self._logManager.add_log("❌ Abort search: MoveWindows failed before start clicks")
                return False

            if self._sleep_with_cancel(1.5):
                return False

            if not self._has_strict_pair_windows():
                self._logManager.add_log("❌ Abort start clicks: окна 1/2/3/4 потеряли строгий порядок")
                return False

            # Открывающие клики только по лидерам (слоты 1 и 3)
            for team in (self.team1, self.team2):
                if self._is_cancelled():
                    return False
                if AutoAcceptModule.final_clicks_disabled():
                    self._logManager.add_log("✅ Match detected. Skipping remaining start-search actions.")
                    return True

                info = get_team_info(team)
                if not info:
                    self._logManager.add_log("❌ Не удалось получить окно лидера для стартовых кликов")
                    return False

                self._safe_activate_hwnd(info["hwnd"])
                if self._sleep_with_cancel(0.25):
                    return False

                for x, y in OPEN_SEQ:
                    if not click_rel(x, y, info["rect"], info["hwnd"]):
                        return False
                    if self._sleep_with_cancel(0.25):
                        return False

            if self._sleep_with_cancel(0.6):
                return False

            # Финальные клики и анализ цветов только для лидеров (слоты 1 и 3).
            initial_final_click_result = _run_all_final_click_pairs(include_bots=False)
            if initial_final_click_result is False:
                return False
            if initial_final_click_result == "black":
                self._logManager.add_log("♻️ Обнаружен чёрный цвет на финальной кнопке лидера. Перезапускаем цикл поиска.")
                if not _click_other_leader_if_red_when_black():
                    return False
                if not _reset_windows_after_black_bot_status():
                    return False
                if not self._press_red_buttons_everywhere(FINAL_CLICK, enforce_green=True, max_wait=20.0, leaders_only=True):
                    return False
                self.press_esc_all_cs2_windows()
                if self._is_cancelled():
                    return False
                cycle += 1
                continue

            if not verify_primary_bots_joined():
                self._logManager.add_log("♻️ Bot1/Bot2 не в команде.")
                if not _reset_windows_after_black_bot_status():
                    return False
                if not self._press_red_buttons_everywhere(FINAL_CLICK, enforce_green=True, max_wait=20.0, leaders_only=True):
                    return False
                self.press_esc_all_cs2_windows()
                if self._is_cancelled():
                    return False
                cycle += 1
                continue

            log_watchers = self._build_log_watchers()
            if log_watchers:
                self._logManager.add_log(f"ℹ️Lobby created succesfully")

            timed_out = True
            should_restart_cycle = False
            start_time = time.time()
            while time.time() - start_time < 600:
                if self._is_cancelled():
                    return False

                if AutoAcceptModule.final_clicks_disabled():
                    timed_out = False
                    break
                # В первую минуту после старта поиска следим за ошибкой datacenter в {login}.log.
                if (time.time() - start_time) <= 60 and log_watchers and self._has_datacenter_ping_error(log_watchers):
                    if not self._reset_search_in_all_cs2_windows():
                        return False
                    should_restart_cycle = True
                    timed_out = False
                    break

                leaders_info_ok = bool(get_team_info(self.team1) and get_team_info(self.team2))
                if not leaders_info_ok:
                    self._logManager.add_log("⚠️ Лидерское окно потеряно во время поиска")
                    timed_out = False
                    break

                # Проверяем цвета на финальных координатах лидеров каждые 0.5 секунды.
                final_click_result = _run_all_final_click_pairs(include_bots=False)
                if final_click_result is False:
                    return False
                if final_click_result == "black":
                    self._logManager.add_log("♻️ Обнаружен чёрный цвет на финальной кнопке лидера. Перезапускаем цикл поиска.")
                    if not _click_other_leader_if_red_when_black():
                        return False
                    should_restart_cycle = True
                    timed_out = False
                    break

                # Если не смогли прочитать пиксели в одном из окон — просто ждём и повторяем.
                leader1_info = get_team_info(self.team1)
                leader2_info = get_team_info(self.team2)
                if not leader1_info or not leader2_info:
                    timed_out = False
                    break
                if get_button_state(leader1_info) is None or get_button_state(leader2_info) is None:
                    if self._sleep_with_cancel(0.25):
                        return False
                    continue

                if self._sleep_with_cancel(0.5):
                    return False

            if should_restart_cycle:
                self._logManager.add_log("♻️ Перезапускаем цикл Make lobbies & search game")
                continue

            if not timed_out or AutoAcceptModule.final_clicks_disabled():
                return True

            if not self._recover_after_match_timeout(FINAL_CLICK):
                return False

            cycle += 1

        self._logManager.add_log("❌ Match was not found after 3 recovery cycles")
        return False