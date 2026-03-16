import threading
import logging
import os
import pyautogui
import time
import json
import re
import psutil
import win32gui
import win32process
import win32con
import win32api
import win32com.client
import pydirectinput
from enum import Enum
from flask import Flask, request
import random
import keyboard
from Managers.LobbyManager import LobbyManager
from datetime import datetime, timedelta

from Managers.AccountsManager import AccountManager
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager


# =========================
# STATE MACHINE
# =========================
class RoundState(Enum):
    IDLE = 0
    LIVE = 1
    OVER = 2


class MatchState(Enum):
    WAITING = 0
    LIVE = 1
    GAMEOVER = 2


T_ACTIONS_LONG = T_ACTIONS_SECOND = [
    ("W+D", 4.5), ("S", 0.05), ("A", 1.1), ("S", 3.3),
    ("W", 0.1), ("A", 1.1),("S", 2.5),("D", 2.3),("D+S", 3.2),("Shift+W+D", 0.15), ("E", 0),
    ("D+S", 1.6), ("S", 3.2), ("D", 3),("D+S", 0.1), ("2", 0), ("1", 0)
    
]

RANDOM_PRE_LONG_KEYS = ["z", "x", "c", "v",  "n",  "ins", "home"]


# =========================
# GSI MANAGER
# =========================
class GSIManager:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        logging.getLogger("werkzeug").setLevel(logging.ERROR)
        
        self.round_over_events = {i: threading.Event() for i in range(1, 17)}
        self._freeze_ctrl_active = False

        self.app = Flask("CS2-GSI")
        self.app.logger.disabled = True
        self._thread = None

        self.logManager = LogManager._instance if LogManager._instance else LogManager()
        self.accountManager = AccountManager()
        self.settingsManager = SettingsManager()
        self.accounts_list_frame = None
        self._freeze_ctrl_event = threading.Event()  # 🆕
        self._gameover_lock = threading.Lock()
        self._last_gameover_trigger_ts = 0.0
        self._post_game_flow_running = False
        
        self.t_actions_done_rounds = set()
        self._spam_stop_event = threading.Event()
        self._spam_lock = threading.Lock()

        # 🆕 Блокировка для раунда 1
        
        # =========================
        # FSM STATE
        # =========================
        self.round_state = RoundState.IDLE
        self.match_state = MatchState.WAITING

        self.current_round = None
        self.round_players = {}
        self.printed_rounds = set()

        # блокировки
        self.parsing_in_progress = False

        # runtime.json
        self.login_to_pid = self._load_runtime_data()

        # mafiles
        self.mafiles_dir = "mafiles"
        self.steamid_login_cache = {}

        self._register_routes()
        self._initialized = True

    # =========================
    # UI
    # =========================
    def set_accounts_list_frame(self, frame):
        self.accounts_list_frame = frame
        print("✅ GSIManager подключен к UI")

    # =========================
    # runtime.json
    # =========================
    def _get_runtime_path(self):
        for path in [
            "runtime.json",
            os.path.join("..", "runtime.json"),
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime.json")
        ]:
            if os.path.exists(path):
                return path
        return None

    def _load_runtime_data(self):
        path = self._get_runtime_path()
        if not path:
            return {}

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

            mapping = {}
            for item in data:
                login = item.get("login")
                pid = item.get("CS2Pid")
                try:
                    pid = int(pid)
                except (TypeError, ValueError):
                    pid = None

                if login and pid:
                    mapping[login.lower()] = (login, pid)

            print(f"✅ runtime.json загружен: {len(mapping)} аккаунтов")
            return mapping
        except Exception as e:
            print(f"❌ runtime.json ошибка: {e}")
            return {}

    # =========================
    # CS2 WINDOWS
    # =========================
    def _extract_login(self, title: str):
        m = re.match(r"\[FREE\]\s*(.+)", title)
        return m.group(1).strip() if m else None

    def _get_cs2_windows(self):
        active_logins = set()

        def cb(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if "[FREE]" in title:
                    login = self._extract_login(title)
                    if login:
                        try:
                            _, pid = win32process.GetWindowThreadProcessId(hwnd)
                            active_logins.add((login, pid))
                            print(f"🪟 НАЙДЕНО окно: {login} (PID:{pid}) | '{title}'")
                        except Exception as e:
                            print(f"❌ Ошибка PID для '{title}': {e}")
            return True

        win32gui.EnumWindows(cb, None)
        print(f"✅ CS2 окна найдено: {len(active_logins)}")
        return active_logins

    def _sync_login_pid_from_windows(self):
        """Обновляет mapping login->(login, pid) по заголовкам окон."""
        for login, pid in self._get_cs2_windows():
            if login:
                self.login_to_pid[login.lower()] = (login, pid)

    def _reload_runtime_data(self):
        """Обновляет runtime.json mapping, если файл доступен."""
        runtime_mapping = self._load_runtime_data()
        if runtime_mapping:
            self.login_to_pid.update(runtime_mapping)

    def _find_hwnd_for_login(self, login, pid=None, retries=5, delay=0.5):
        """Ищет HWND по PID и/или заголовку окна, с повторами."""
        for attempt in range(1, retries + 1):
            hwnds = []
            if pid:
                hwnds = self._get_hwnds_by_pid(pid, login)
            if not hwnds:
                def cb(hwnd, _):
                    if not win32gui.IsWindowVisible(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd)
                    if "[FREE]" in title:
                        if login.lower() in title.lower():
                            hwnds.append(hwnd)
                            return False
                    return True
                win32gui.EnumWindows(cb, None)

            if hwnds:
                return hwnds[0]

            print(f"⏳ HWND не найден ({login}) попытка {attempt}/{retries}")
            time.sleep(delay)
        return None

    def _get_active_from_runtime(self):
        active_logins = set()
        for lower_login, (login, pid) in self.login_to_pid.items():
            try:
                proc = psutil.Process(pid)
                if proc.is_running() and "cs2" in proc.name().lower():
                    active_logins.add(login)
                    print(f"⚙️ Runtime НАЙДЕН: {login} (PID:{pid})")
            except:
                pass
        print(f"✅ Runtime процессы: {len(active_logins)}")
        return active_logins

    # =========================
    # MAFILE
    # =========================
    def _login_from_mafile(self, steamid):
        if steamid in self.steamid_login_cache:
            return self.steamid_login_cache[steamid]

        path = os.path.join(self.mafiles_dir, f"{steamid}.mafile")
        if not os.path.exists(path):
            return None

        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            login = data.get("account_name")
            if login:
                self.steamid_login_cache[steamid] = login
            return login
        except:
            return None

    # =========================
    # ROUND LOGS
    # =========================
    def _login_with_pid(self, login):
        entry = self.login_to_pid.get(login.lower())
        if entry:
            return f"{entry[0]} (PID:{entry[1]})"
        return login

    def _round_start(self, rnd, ct, t):
        players = self.round_players.get(rnd, {})
        ct_team = []
        t_team = []
        
        for login, team in players.items():
            entry = self.login_to_pid.get(login.lower())
            if entry:
                login_display = f"{entry[0]} (PID:{entry[1]})"
            else:
                login_display = login
                
            if team == "CT":
                ct_team.append(login_display)
            else:
                t_team.append(login_display)

        print(f"\n🎮 НАЧАЛО РАУНДА {rnd} | CT:{ct} T:{t}")
        print("🔵 CT:")
        for p in ct_team:
            print(f"  • {p}")
        print("🔴 T:")
        for p in t_team:
            print(f"  • {p}")
        print("═" * 70)


    def _round_end(self, rnd, ct, t, winner):
        print(f"\n🏁 КОНЕЦ РАУНДА {rnd} | CT:{ct} T:{t} | {winner}")
        print("═" * 70)
    def _get_hwnds_by_pid(self, target_pid, login=None):
        """Ищет top-level HWND процесса и приоритизирует «правильное» окно CS2."""
        try:
            target_pid = int(target_pid)
        except (TypeError, ValueError):
            return []

        hwnds = []

        def callback(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd) or not win32gui.IsWindowEnabled(hwnd):
                return True

            # Берем только top-level окна процесса.
            if win32gui.GetParent(hwnd) != 0:
                return True

            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid == target_pid:
                    title = win32gui.GetWindowText(hwnd)
                    title_lower = title.lower()

                    score = 0
                    # Самый приоритетный кейс: окно явно переименовано под логин.
                    if login and login.lower() in title_lower:
                        score += 100
                    if "[fsn free]" in title_lower:
                        score += 40
                    # Фолбэк для ручного старта без переименования окна.
                    if "counter-strike" in title_lower or "cs2" in title_lower:
                        score += 20
                    if title:
                        score += 5

                    hwnds.append((score, hwnd, title))
            except:
                pass
            return True

        win32gui.EnumWindows(callback, None)

        hwnds.sort(key=lambda item: item[0], reverse=True)

        result = []
        for score, hwnd, title in hwnds:
            result.append(hwnd)
            print(f"🎯 НАЙДЕН HWND: {hwnd} для PID:{target_pid} | score:{score} | '{title}'")
        return result



    def _activate_window(self, hwnd):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            return True
        except Exception as e:
            print(f"❌ Не удалось активировать окно {hwnd}: {e}")
            return False
    def _reset_keys(self):
        for k in ["w", "a", "s", "d", "e", "2"]:
            pydirectinput.keyUp(k)

    def _sleep_with_stop(self, duration, stop_event=None, step=0.05):
        if not stop_event:
            time.sleep(duration)
            return False
        elapsed = 0.0
        while elapsed < duration:
            if stop_event.is_set():
                return True
            time.sleep(step)
            elapsed += step
        return False

    def _perform_actions(self, hwnd, actions, stop_event=None):
        self._reset_keys()  # 👈 ВАЖНО: сначала отпускаем всё

        if not self._activate_window(hwnd):
            return

        if self._sleep_with_stop(0.25, stop_event=stop_event):
            return  # 👈 рекомендую, CS2 любит паузу

        for key, duration in actions:
            if stop_event and stop_event.is_set():
                return
            if "+" in key:
                keys = key.split("+")
                for k in keys:
                    pydirectinput.keyDown(k.lower())
                if self._sleep_with_stop(duration, stop_event=stop_event):
                    for k in keys:
                        pydirectinput.keyUp(k.lower())
                    return
                for k in keys:
                    pydirectinput.keyUp(k.lower())
            else:
                pydirectinput.keyDown(key.lower())
                if self._sleep_with_stop(duration, stop_event=stop_event):
                    pydirectinput.keyUp(key.lower())
                    return
                pydirectinput.keyUp(key.lower())

            if self._sleep_with_stop(0.05, stop_event=stop_event):
                return

    def _press_random_pre_long_key(self, hwnd, stop_event=None):
        """Перед длинным маршрутом: активируем окно и жмём случайную кнопку."""
        if stop_event and stop_event.is_set():
            return

        if not self._activate_window(hwnd):
            return

        if self._sleep_with_stop(0.15, stop_event=stop_event):
            return

        key = random.choice(RANDOM_PRE_LONG_KEYS)
        print(f"🎲 PRE-LONG: нажимаем '{key}'")

        try:
            pydirectinput.press(key)
        except Exception as e:
            print(f"⚠️ Ошибка нажатия '{key}': {e}")

        self._sleep_with_stop(0.1, stop_event=stop_event)


    def _perform_t_actions_for_round(self, round_number):
        if round_number in self.t_actions_done_rounds:
            return

        stop_event = self.round_over_events.get(round_number)
        print(f"🔥 ROUND {round_number}: старт сценария движений")

        # 1) Небольшая пауза после старта раунда
        print("⏱️ Шаг 1/7: пауза 0.4s после старта раунда")
        if self._sleep_with_stop(0.4, stop_event=stop_event):
            print(f"🛑 ROUND {round_number}: сценарий остановлен во время стартовой паузы")
            return

        players = self.round_players.get(round_number, {})
        ct_players = [login for login, team in players.items() if team == "CT"]
        t_players = [login for login, team in players.items() if team == "T"]

        print(f"📋 ROUND {round_number}: состав CT={ct_players} | T={t_players}")

        if not ct_players and not t_players:
            print(f"❌ ROUND {round_number}: игроки CT/T не обнаружены")
            return

        # 2) Пауза для стабилизации окон
        print("⏱️ Шаг 2/7: пауза 0.4s перед поиском HWND")
        if self._sleep_with_stop(0.4, stop_event=stop_event):
            print(f"🛑 ROUND {round_number}: сценарий остановлен до поиска HWND")
            return

        self._reload_runtime_data()
        self._sync_login_pid_from_windows()

        def resolve_hwnd(login: str):
            entry = self.login_to_pid.get(login.lower())
            pid = entry[1] if entry else None
            hwnd = self._find_hwnd_for_login(login, pid=pid, retries=6, delay=0.5)
            if hwnd:
                print(f"🎯 HWND найден: {login} | PID:{pid} | HWND:{hwnd}")
            else:
                print(f"❌ HWND не найден: {login} | PID:{pid}")
            return hwnd

        ct_hwnd_map = {login: resolve_hwnd(login) for login in ct_players}
        t_hwnd_map = {login: resolve_hwnd(login) for login in t_players}

        ct_hwnd_map = {login: hwnd for login, hwnd in ct_hwnd_map.items() if hwnd}
        t_hwnd_map = {login: hwnd for login, hwnd in t_hwnd_map.items() if hwnd}

        print(
            f"📊 ROUND {round_number}: доступно окон -> "
            f"CT {len(ct_hwnd_map)}/{len(ct_players)} | T {len(t_hwnd_map)}/{len(t_players)}"
        )

        ct_long_hwnd = None
        ct_long_login = None

        # 3) CT блок:
        # - если найдено >=2 окна CT: рандомный CT жмёт D 0.5s, второй делает длинный маршрут
        # - если найдено ровно 1 окно CT: только длинный маршрут
        ct_logins = list(ct_hwnd_map.keys())
        random.shuffle(ct_logins)

        if len(ct_logins) >= 2:
            ct_d_login = ct_logins[0]
            ct_long_login = ct_logins[1]
            ct_d_hwnd = ct_hwnd_map[ct_d_login]
            ct_long_hwnd = ct_hwnd_map[ct_long_login]

            print(f"🧭 Шаг 3/7: CT-D 0.5s -> {ct_d_login}")
            self._perform_actions(ct_d_hwnd, [("D", 0.5)], stop_event=stop_event)
            if self._sleep_with_stop(0.2, stop_event=stop_event):
                print(f"🛑 ROUND {round_number}: остановлено после CT-D")
                return

            print(f"🧭 Шаг 4/7: CT-LONG -> {ct_long_login}")
            self._press_random_pre_long_key(ct_long_hwnd, stop_event=stop_event)
            if stop_event and stop_event.is_set():
                print(f"🛑 ROUND {round_number}: остановлено перед CT-LONG (pre-key)")
                return
            self._perform_actions(ct_long_hwnd, T_ACTIONS_LONG, stop_event=stop_event)
            if stop_event and stop_event.is_set():
                print(f"🛑 ROUND {round_number}: остановлено во время CT-LONG")
                return

        elif len(ct_logins) == 1:
            ct_long_login = ct_logins[0]
            ct_long_hwnd = ct_hwnd_map[ct_long_login]
            print(f"🧭 Шаг 3/7: найден 1 CT ({ct_long_login}) -> только CT-LONG")
            self._press_random_pre_long_key(ct_long_hwnd, stop_event=stop_event)
            if stop_event and stop_event.is_set():
                print(f"🛑 ROUND {round_number}: остановлено перед CT-LONG (single CT pre-key)")
                return
            self._perform_actions(ct_long_hwnd, T_ACTIONS_LONG, stop_event=stop_event)
            if stop_event and stop_event.is_set():
                print(f"🛑 ROUND {round_number}: остановлено во время CT-LONG (single CT)")
                return
        else:
            print(f"⚠️ ROUND {round_number}: CT окна не найдены, CT-блок пропущен")

        # 4) T блок:
        # - если найдено >=2 окна T: T1 -> D 0.5, T2 -> маршрут, T1 -> маршрут
        # - если найдено ровно 1 окно T: только маршрут A/W/W+D
        t_logins = list(t_hwnd_map.keys())
        random.shuffle(t_logins)
        short_route = [("A", 0.5), ("W", 1.8), ("W+D", 1.3)]

        if len(t_logins) >= 2:
            t_d_login = t_logins[0]
            t_route_login = t_logins[1]
            t_d_hwnd = t_hwnd_map[t_d_login]
            t_route_hwnd = t_hwnd_map[t_route_login]

            print(f"🧭 Шаг 5/7: T-D 0.5s -> {t_d_login}")
            self._perform_actions(t_d_hwnd, [("D", 0.4)], stop_event=stop_event)
            if self._sleep_with_stop(0.2, stop_event=stop_event):
                print(f"🛑 ROUND {round_number}: остановлено после первого T-D")
                return

            print(f"🧭 Шаг 6/7: T-ROUTE A/W/WD -> {t_route_login}")
            self._perform_actions(t_route_hwnd, short_route, stop_event=stop_event)
            if self._sleep_with_stop(0.2, stop_event=stop_event):
                print(f"🛑 ROUND {round_number}: остановлено после маршрута второго T")
                return

            print(f"🧭 Шаг 7/7: T-ROUTE A/W/WD -> {t_d_login} (после D)")
            self._perform_actions(t_d_hwnd, short_route, stop_event=stop_event)
            if stop_event and stop_event.is_set():
                print(f"🛑 ROUND {round_number}: остановлено после маршрута первого T")
                return

        elif len(t_logins) == 1:
            t_route_login = t_logins[0]
            t_route_hwnd = t_hwnd_map[t_route_login]
            print(f"🧭 Шаг 5/7: найден 1 T ({t_route_login}) -> только маршрут A/W/WD")
            self._perform_actions(t_route_hwnd, short_route, stop_event=stop_event)
            if stop_event and stop_event.is_set():
                print(f"🛑 ROUND {round_number}: остановлено во время маршрута single T")
                return
        else:
            print(f"⚠️ ROUND {round_number}: T окна не найдены, T-блок пропущен")

        # 5) Флуд K запускается только после завершения CT/T сценария
        if ct_long_hwnd:
            print(
                f"🧭 ROUND {round_number}: перед K-flood CT long ({ct_long_login}) делает W+D 0.5s"
            )
            self._perform_actions(ct_long_hwnd, [("W+D", 0.5)], stop_event=stop_event)
            if stop_event and stop_event.is_set():
                print(f"🛑 ROUND {round_number}: остановлено перед запуском K-flood")
                return

            with self._spam_lock:
                self._spam_stop_event.set()
                self._spam_stop_event = threading.Event()
                spam_stop_event = self._spam_stop_event

            hold_ctrl = random.random() < 0.7
            mode = "CTRL+K" if hold_ctrl else "K"
            print(
                f"🎲 ROUND {round_number}: запуск K-flood на CT long ({ct_long_login}) | "
                f"режим={mode} | шанс CTRL=70%"
            )

            threading.Thread(
                target=self._spam_k_until_round_over,
                args=(ct_long_hwnd, round_number, spam_stop_event, hold_ctrl),
                daemon=True
            ).start()
        else:
            print(f"⚠️ ROUND {round_number}: K-flood не запущен (нет CT long HWND)")

        self.t_actions_done_rounds.add(round_number)
        print(f"✅ ROUND {round_number}: сценарий завершен")





    def _stop_spam_keys(self):
        with self._spam_lock:
            self._spam_stop_event.set()


    def _perform_ct_actions_for_round(self, round_number):
        players = self.round_players.get(round_number, {})
        ct_players = sorted([login for login, team in players.items() if team == "T"])

        for login in ct_players:
            entry = self.login_to_pid.get(login.lower())
            if not entry:
                continue

            _, pid = entry
            hwnds = self._get_hwnds_by_pid(pid, login)
            if not hwnds:
                continue

            hwnd = hwnds[0]
            print(f"🛡️ T ACTIONS → {login} | ROUND {round_number}")

            self._perform_actions(hwnd, [
                ("A+S", 1.8),
                ("A+W", 1.8),
                ("S+A", 1.8),
            ])



    def _spam_k_until_round_over(self, hwnd, round_number, spam_stop_event, hold_ctrl):
        mode = "CTRL+K" if hold_ctrl else "K"
        print(f"⌨️ {mode} до конца раунда/матча {round_number}")

        self._reset_keys()
        if not self._activate_window(hwnd):
            return

        if hold_ctrl:
            pydirectinput.keyDown("ctrl")
            time.sleep(0.05)

        try:
            while True:
                round_event = self.round_over_events.get(round_number)

                # Останов: принудительная остановка спама
                if spam_stop_event.is_set():
                    break

                # Останов: завершение раунда
                if round_event and round_event.is_set():
                    break

                # Останов: начался следующий раунд
                if self.current_round is not None and self.current_round != round_number:
                    break

                # Останов: завершение матча в любом раунде
                if self.match_state == MatchState.GAMEOVER:
                    break

                pydirectinput.press("k")
                time.sleep(0.05)
        finally:
            # Отпускаем все, даже если матч завершился/ошибка
            if hold_ctrl:
                pydirectinput.keyUp("ctrl")
            pydirectinput.keyUp("k")
            self._reset_keys()
            print(f"🛑 Раунд {round_number} завершён, {mode} остановлен")




    # =========================
    # LEVEL PARSING
    # =========================
    def _parse_levels_after_match(self):
        if self.parsing_in_progress:
            print("⚠️ 🔒 Парсинг уже идет")
            return

        print("🚀 ПАРСИНГ УРОВНЕЙ ПОСЛЕ МАТЧА")
        self.parsing_in_progress = True

        window_logins = self._get_cs2_windows()
        print(f"🪟 CS2 окна: {len(window_logins)}")
        
        runtime_logins = self._get_active_from_runtime()
        print(f"⚙️ Runtime: {len(runtime_logins)}")
        
        all_active = set()
        for login, _ in window_logins:
            all_active.add(login)
        all_active.update(runtime_logins)
        
        print(f"🔍 АКТИВНЫХ ({len(all_active)}): {sorted(all_active)}")
        
        if not all_active:
            print("❌ НЕТ АКТИВНЫХ АККАУНТОВ!")
            self.parsing_in_progress = False
            return

        parsed = 0
        for login in sorted(all_active):
            print(f"\n🔍 '{login}'")
            for i, acc in enumerate(self.accountManager.accounts):
                print(f"  [{i}] '{acc.login}'")
                if acc.login.lower() == login.lower():
                    print(f"  ✅ НАЙДЕН: {login}")
                    if hasattr(acc, 'parse_current_level'):
                        try:
                            level_parsed = False
                            for attempt in range(2):
                                if acc.parse_current_level():
                                    level_parsed = True
                                    break

                                if attempt == 0:
                                    msg = f"({login} error, return)"
                                else:
                                    msg = f"({login} error parsing lvl)"

                                print(msg)
                                self.logManager.add_log(msg)

                            if level_parsed:
                                parsed += 1
                                level = getattr(acc, 'level', 0)
                                xp = getattr(acc, 'xp', 0)
                                xp_pretty = f"{xp:,}".replace(",", " ")
                                print(f"[{login}] lvl: {level} | xp: {xp_pretty}")
                                self.logManager.add_log(f"[{login}] lvl: {level} | xp: {xp_pretty}")
                                if self.accounts_list_frame:
                                    self.accounts_list_frame.update_account_level(login, level, xp)
                        except Exception as e:
                            print(f"❌ [{login}] Ошибка: {e}")
                    break
            else:
                print(f"❌ [{login}] НЕ НАЙДЕН")

        print(f"🎉 ПАРСИНГ: {parsed}/{len(all_active)}")
        self.logManager.add_log(f"🎉 Обновлено {parsed} уровней")
        self.parsing_in_progress = False

    # =========================
    # GSI ROUTE
    # =========================

    def _register_routes(self):
        @self.app.route("/", methods=["POST"])
        def gsi():
            data = request.json
            if not data:
                return "ok"

            player = data.get("player")
            round_info = data.get("round", {})
            map_info = data.get("map", {})

            round_phase = round_info.get("phase")
            map_phase = map_info.get("phase")

            ct = map_info.get("team_ct", {}).get("score", 0)
            t = map_info.get("team_t", {}).get("score", 0)

            round_start_num = ct + t + 1
            round_end_num = ct + t

            # сбор игроков
            if player:
                login = self._login_from_mafile(str(player.get("steamid")))
                if login:
                    self.round_players.setdefault(round_start_num, {})[login] = player.get("team")

            # ===== ROUND FSM =====
            if round_phase == "live":
                self._freeze_ctrl_event.set()   # 🛑 стоп Ctrl-логик (если есть)

                # Не останавливаем spam в пределах того же раунда: GSI может
                # присылать множество "live" апдейтов, и это преждевременно
                # прерывает удержание/флуд до конца раунда.
                if self.current_round != round_start_num:
                    self._stop_spam_keys()
                self._freeze_ctrl_active = False
                self.round_state = RoundState.LIVE

                self.current_round = round_start_num
                if self.current_round not in self.printed_rounds:
                    self.printed_rounds.add(self.current_round)
                    self._round_start(self.current_round, ct, t)
                    threading.Thread(
                        target=self._perform_t_actions_for_round,
                        args=(self.current_round,),
                        daemon=True
                    ).start()

            elif round_phase == "over" and self.round_state == RoundState.LIVE:
                self._round_end(round_end_num, ct, t, round_info.get("win_team", "?"))
                if round_end_num in self.round_over_events:
                    self.round_over_events[round_end_num].set()
                self._stop_spam_keys()
                self.round_state = RoundState.OVER
            else:
                self.round_state = RoundState.IDLE

            # ===== MATCH FSM =====
            if map_phase == "gameover" and self.match_state != MatchState.GAMEOVER:
                self._freeze_ctrl_event.set()
                self._stop_spam_keys()
                self.match_state = MatchState.GAMEOVER

                msg = f"🏆 КОНЕЦ МАТЧА | CT:{ct} T:{t}"
                print(f"\n{msg}")
                self.logManager.add_log(msg)

                threading.Thread(target=self._parse_levels_after_match, daemon=True).start()
                self._start_post_game_flow_once()

            elif map_phase in ["warmup", "waiting", "live"] and self.match_state == MatchState.GAMEOVER:
                # матч снова пошёл -> сброс
                self.match_state = MatchState.LIVE
                self.round_players.clear()
                self.printed_rounds.clear()
                self.current_round = None
                self.t_actions_done_rounds.clear()
                for ev in self.round_over_events.values():
                    ev.clear()
                self._freeze_ctrl_event.clear()

            return "ok"


    # =========================
    # POST-GAME FLOW (по ТЗ)
    # =========================
    @staticmethod
    def _is_cancelled_ctrl_q():
        try:
            return keyboard.is_pressed("ctrl+q")
        except Exception:
            return False

    def _ui_log(self, text: str):
        # консоль
        try:
            print(text)
        except Exception:
            pass

        # logManager
        try:
            self.logManager.add_log(text)
        except Exception:
            pass

        # UI (если есть метод)
        if self.accounts_list_frame and hasattr(self.accounts_list_frame, "set_status_text"):
            try:
                self.accounts_list_frame.set_status_text(text)
            except Exception:
                pass

    def _sleep_with_cancel_ctrl_q(self, seconds: float, step: float = 0.1) -> bool:
        """True если отменили Ctrl+Q, False если досидели."""
        end_t = time.time() + seconds
        while time.time() < end_t:
            if self._is_cancelled_ctrl_q():
                time.sleep(0.15)
                if self._is_cancelled_ctrl_q():
                    return True
            time.sleep(max(0.0, min(step, end_t - time.time())))
        return False
    def _safe_activate_hwnd(self, hwnd) -> bool:
        """Стабильнее активирует окно (ShowWindow + AttachThreadInput)."""
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

            if fg_tid and hwnd_tid and fg_tid != hwnd_tid:
                try:
                    win32process.AttachThreadInput(fg_tid, hwnd_tid, True)
                    attached = True
                except Exception:
                    attached = False

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

            time.sleep(0.12)
            return True
        except Exception as e:
            print(f"❌ activate hwnd failed {hwnd}: {e}")
            return False
        finally:
            if attached and fg_tid and hwnd_tid and fg_tid != hwnd_tid:
                try:
                    win32process.AttachThreadInput(fg_tid, hwnd_tid, False)
                except Exception:
                    pass

    def _send_esc(self, hwnd):
        try:
            win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, win32con.VK_ESCAPE, 0)
            win32api.PostMessage(hwnd, win32con.WM_KEYUP, win32con.VK_ESCAPE, 0)
        except Exception:
            pass

    def _click_in_window(self, hwnd, x, y, hover_delay=0.3):
 
        try:
            rect = win32gui.GetWindowRect(hwnd)  # (left, top, right, bottom)
            abs_x = rect[0] + x
            abs_y = rect[1] + y

            # наведение
            win32api.SetCursorPos((abs_x, abs_y))
            time.sleep(hover_delay)

            # клик
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        except Exception:
            pass


    def post_game_restart_flow(self):
        try:
            self._ui_log("⏳ Жду 60 секунд после матча (Ctrl+Q чтобы отменить)...")

            cancelled = self._sleep_with_cancel_ctrl_q(60.0, step=0.2)
            if cancelled:
                self._ui_log("🛑 Отменено Ctrl+Q")
                return

            if self._try_auto_switch_accounts_after_drop():
                return

            hwnds = self._get_all_cs2_hwnds()  # список (hwnd, pid)
            if not hwnds:
                self._ui_log("❌ Не найдено CS2 окон после матча")
                return

            for hwnd, pid in hwnds:
                if self._is_cancelled_ctrl_q():
                    self._ui_log("🛑 Отменено Ctrl+Q")
                    return

                if not win32gui.IsWindow(hwnd):
                    continue

                # активируем окно
                self._safe_activate_hwnd(hwnd)

                # ESC


                # hover -> click x2
                self._click_in_window(hwnd, 374, 8, hover_delay=0.4)
                time.sleep(0.4)
                self._send_esc(hwnd)
                time.sleep(0.4)
                self._click_in_window(hwnd, 374, 8, hover_delay=0.4)
                time.sleep(0.4)
                self._send_esc(hwnd)
                time.sleep(0.4)                
                self._send_esc(hwnd)
                time.sleep(0.4)             
            try:
                LobbyManager().MakeLobbiesAndSearchGame()
            except Exception as e:
                self._ui_log(f"❌ Ошибка запуска MakeLobbiesAndSearchGame: {e}")

        finally:
            with self._gameover_lock:
                self._post_game_flow_running = False

    def _start_post_game_flow_once(self):
        now = time.time()
        with self._gameover_lock:
            if self._post_game_flow_running:
                self._ui_log("⚠️ Пост-матч уже выполняется — повтор пропущен")
                return

            # Защита от дублирующего gameover-события (окно 5 секунд)
            if (now - self._last_gameover_trigger_ts) < 5:
                self._ui_log("⚠️ Дублирующий gameover за 5 секунд — повтор пропущен")
                return

            self._last_gameover_trigger_ts = now
            self._post_game_flow_running = True

        threading.Thread(target=self.post_game_restart_flow, daemon=True).start()

    def _get_weekly_window_start(self, now=None):
        current_time = now or datetime.now()
        reset_anchor = current_time.replace(hour=3, minute=0, second=0, microsecond=0)
        days_since_reset = (current_time.weekday() - 2) % 7
        week_start = reset_anchor - timedelta(days=days_since_reset)
        if current_time < week_start:
            week_start -= timedelta(days=7)
        return week_start

    def _is_take_drop_for_login(self, login):
        if not self.accounts_list_frame:
            return False

        levels_cache = getattr(self.accounts_list_frame, "levels_cache", {}) or {}
        account_data = levels_cache.get(login, levels_cache.get(login.lower(), {}))
        if not isinstance(account_data, dict):
            return False

        week_start_iso = self._get_weekly_window_start().isoformat()
        if account_data.get("weekly_baseline_start") != week_start_iso:
            return False

        current_level = account_data.get("level")
        baseline_level = account_data.get("weekly_baseline_level")
        return isinstance(current_level, int) and isinstance(baseline_level, int) and current_level >= baseline_level + 1

    def _collect_active_match_accounts(self):
        active = []
        for acc in self.accountManager.accounts:
            try:
                if acc.isCSValid():
                    active.append(acc)
            except Exception:
                continue
        return active

    def _mark_accounts_as_drop_ready(self, accounts):
        if not self.accounts_list_frame:
            return

        for acc in accounts:
            login = acc.login
            try:
                self.accounts_list_frame.set_drop_ready(login, value=True)
                acc.setColor("#a855f7")
            except Exception:
                pass

    def _try_auto_switch_accounts_after_drop(self):
        auto_switch = bool(self.settingsManager.get("AutomaticAccountSwitchingEnabled", True))
        if not auto_switch:
            self._ui_log("ℹ️ Automatic account switching: OFF — продолжаем фарм текущих аккаунтов")
            return False

        if self.parsing_in_progress:
            wait_start = time.time()
            while self.parsing_in_progress and (time.time() - wait_start) < 30:
                time.sleep(0.3)

        active_accounts = self._collect_active_match_accounts()
        if len(active_accounts) != 4:
            self._ui_log(f"ℹ️ Автосмена не активирована: активных аккаунтов {len(active_accounts)}, требуется 4")
            return False

        if not all(self._is_take_drop_for_login(acc.login) for acc in active_accounts):
            return False

        self._ui_log("✅ 4/4 получили уровень и статус Take drop — ставлю фиолетовый и запускаю автосмену")
        self._mark_accounts_as_drop_ready(active_accounts)

        try:
            for acc in self.accountManager.accounts:
                if hasattr(acc, "steamProcess"):
                    acc.steamProcess = None
                if hasattr(acc, "CS2Process"):
                    acc.CS2Process = None
        except Exception:
            pass

        killed = 0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
                if "cs2" in name or "steam" in name or "csgo" in name:
                    proc.kill()
                    killed += 1
            except Exception:
                continue
        self._ui_log(f"💀 Автосмена: остановлено процессов CS/Steam: {killed}")

        remaining = []
        if self.accounts_list_frame:
            remaining = [acc for acc in self.accountManager.accounts if not self.accounts_list_frame.is_reserved_from_rotation(acc)]

        if len(remaining) < 4:
            self._ui_log(f"ℹ️ Недостаточно аккаунтов для следующей пачки: {len(remaining)}. Все аккаунты остановлены.")
            return True

        self.accountManager.selected_accounts.clear()
        self.accountManager.selected_accounts.extend(remaining[:4])
        self._ui_log("🚀 Запускаю следующие 4 аккаунта по списку")

        try:
            app = self.accounts_list_frame.winfo_toplevel() if self.accounts_list_frame else None
            if app and hasattr(app, "accounts_control"):
                app.after(0, app.accounts_control.start_selected)
                return True
        except Exception as e:
            self._ui_log(f"❌ Не удалось запустить следующую пачку: {e}")

        self._ui_log("❌ Не удалось запустить следующую пачку: accounts_control недоступен")
        return True




    def _get_all_cs2_hwnds(self):
        """Ищет все top-level HWND для активных CS2 PID (процессы + runtime)."""
        cs2_hwnds = []
        seen_hwnds = set()

        # Подтягиваем свежий runtime (на случай запуска не из панели).
        self._reload_runtime_data()

        # 1️⃣ PID из реально запущенных процессов.
        cs2_pids = set()
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                name = (proc.info.get('name') or '').lower()
                if 'cs2' in name:
                    pid = int(proc.info['pid'])
                    cs2_pids.add(pid)
                    print(f"🎮 CS2.exe PID: {pid}")
            except Exception:
                continue

        # 2️⃣ PID из runtime.json (только если процесс реально жив).
        runtime_pids = set()
        for _, (_, pid) in self.login_to_pid.items():
            try:
                pid = int(pid)
                proc = psutil.Process(pid)
                if proc.is_running() and 'cs2' in proc.name().lower():
                    runtime_pids.add(pid)
            except Exception:
                continue

        if runtime_pids:
            print(f"⚙️ Runtime активные CS2 PID: {sorted(runtime_pids)}")

        all_pids = sorted(cs2_pids | runtime_pids)
        if not all_pids:
            print("❌ Не найдено активных cs2 PID")
            return []

        # 3️⃣ Для каждого PID ищем HWND общим PID-поиском.
        for pid in all_pids:
            hwnds = self._get_hwnds_by_pid(pid)
            for hwnd in hwnds:
                if hwnd in seen_hwnds:
                    continue
                seen_hwnds.add(hwnd)
                cs2_hwnds.append((hwnd, pid))

        print(f"✅ Найдено CS2 окон: {len(cs2_hwnds)}")
        return cs2_hwnds



    def _spam_ctrl_freeze_time(self, hwnds):
        """Цикл: 1сек Ctrl → следующее окно → повтор до live"""
        print(f"🕐 FREEZE TIME: {len(hwnds)} окон по 1сек Ctrl")
        
        while self._freeze_ctrl_active and not self._freeze_ctrl_event.is_set():
            for hwnd, pid in hwnds:
                if not self._freeze_ctrl_active or self._freeze_ctrl_event.is_set():
                    break  # 🛑 live/gameover
                
                self._single_window_ctrl_spam(hwnd, pid)
                time.sleep(0.2)  # пауза между окнами
            
            print("🔄 Freeze time: полный цикл окон завершён")
        
        self._freeze_ctrl_active = False
        print("🛑 Freeze time Ctrl остановлен")



    def _single_window_ctrl_spam(self, hwnd, pid):
        """Удерживает Ctrl 1 сек, потом следующее окно"""
        print(f"🎮 Ctrl 1сек → HWND:{hwnd}")
        
        if self._activate_window(hwnd):
            # УДЕРЖИВАЕМ Ctrl ровно 1 секунду
            pydirectinput.keyDown("ctrl")
            time.sleep(1.0)  # 👈 1 СЕКУНДА
            pydirectinput.keyUp("ctrl")
            print(f"✅ Ctrl 1сек выполнен HWND:{hwnd}")
        else:
            print(f"❌ Не удалось активировать HWND:{hwnd}")


    # =========================
    # SERVER
    # =========================
    def start(self):
        if self._thread:
            return

        def run():
            print("🟢 GSI сервер запущен: http://127.0.0.1:6969")
            self.app.run(
                host="127.0.0.1",
                port=6969,
                debug=False,
                use_reloader=False,
            )

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()