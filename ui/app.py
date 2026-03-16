import json
import os
import queue
import re
import subprocess
import uuid
import hashlib
import sys
import base64
import time
import webbrowser
from urllib.parse import quote
import rsa
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import customtkinter
import requests
from Managers.AccountsManager import AccountManager
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager
from Managers.TelegramBotManager import TelegramBotManager


from .accounts_tab import AccountsControl
from .config_tab import ConfigTab
from .control_frame import ControlFrame
from .main_menu import MainMenu


customtkinter.set_appearance_mode("Dark")
customtkinter.set_default_color_theme("blue")

BG_MAIN = "#0b1020"
BG_PANEL = "#121a30"
BG_CARD = "#151d34"
BG_CARD_ALT = "#10182d"
BG_BORDER = "#242d48"
TXT_MAIN = "#e9edf7"
TXT_MUTED = "#8f9bb8"
TXT_SOFT = "#b8c2df"
ACCENT_BLUE = "#2f6dff"
ACCENT_BLUE_DARK = "#214ebe"
ACCENT_GREEN = "#1f9d55"
ACCENT_RED = "#c83a4a"
ACCENT_PURPLE = "#252b4f"
ACCENT_ORANGE = "#ff9500"

LICENSE_SERVER_URL = ""
LICENSE_PUBLIC_KEY_PATH = Path("")
LICENSE_EMBEDDED_PUBLIC_KEY_PEM = ''' '''
LICENSE_CACHE_PATH = Path("")
LICENSE_TOKEN_TTL_GRACE_SECONDS = 300
LICENSE_RECHECK_INTERVAL_MS = 60000
LICENSE_REQUEST_TIMEOUT = (3, 8)
LICENSE_WATCHDOG_TIMEOUT_MS = 25000
MAX_TOKEN_TTL_SECONDS = 3600
LICENSE_CHALLENGE_TTL_SECONDS = 30

REGION_PING_TARGETS = {}
WEEKLY_RESET_WEEKDAY = 2  # Wednesday
WEEKLY_RESET_HOUR = 3
APP_ICON_CANDIDATES = ("Icon2.ico", "Icon1.ico")

class SteamRouteManager:
    """Manages Windows Firewall rules for Steam SDR regional routing."""

    PREFIX = "FSN_Route_"

    def __init__(self):
        pass

    def _run_netsh(self, cmd_args):
        try:
            subprocess.run(
                ["netsh", "advfirewall", "firewall"] + cmd_args,
                capture_output=True,
                check=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except Exception:
            return False

    def add_block_rule(self, region_name, ips):
        if ips:
            packed_ips = ",".join(ips)
            return self._run_netsh(
                ["add", "rule", f"name={self.PREFIX}{region_name}", "dir=out", "action=block", f"remoteip={packed_ips}"]
            )
        return False

    def remove_rule(self, region_name):
        return self._run_netsh(["delete", "rule", f"name={self.PREFIX}{region_name}"])

    def add_block_rules_bulk(self, rules_map, max_workers=24):
        if not rules_map:
            return {}

        results = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.add_block_rule, rule_name, ips): rule_name
                for rule_name, ips in rules_map.items()
            }
            for future, rule_name in futures.items():
                try:
                    results[rule_name] = bool(future.result())
                except Exception:
                    results[rule_name] = False

        return results

    def remove_rules_bulk(self, rule_names, max_workers=24):
        if not rule_names:
            return {}

        results = {}
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self.remove_rule, rule_name): rule_name
                for rule_name in rule_names
            }
            for future, rule_name in futures.items():
                try:
                    results[rule_name] = bool(future.result())
                except Exception:
                    results[rule_name] = False

        return results

    def full_cleanup(self):
        try:
            cmd = f'Remove-NetFirewallRule -Name "{self.PREFIX}*" -ErrorAction SilentlyContinue'
            subprocess.run(["powershell", "-Command", cmd], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass

    def get_blocked_regions(self):
        try:
            cmd = (
                f'Get-NetFirewallRule -DisplayName "{self.PREFIX}*" -ErrorAction SilentlyContinue '
                "| Select-Object -ExpandProperty DisplayName"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                capture_output=True,
                text=True,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            if result.returncode != 0:
                cmd = (
                    f'Get-NetFirewallRule -Name "{self.PREFIX}*" -ErrorAction SilentlyContinue '
                    "| Select-Object -ExpandProperty Name"
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd],
                    capture_output=True,
                    text=True,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )

            if result.returncode != 0:
                return set()

            blocked_regions = set()
            for line in (result.stdout or "").splitlines():
                rule_name = line.strip()
                if not rule_name.startswith(self.PREFIX):
                    continue
                blocked_regions.add(rule_name[len(self.PREFIX):])
            return blocked_regions
        except Exception:
            return set()


class App(customtkinter.CTk):
    def __init__(self, gsi_manager=None, startup_gpu_info=None, startup_services_initializer=None):
        super().__init__()
        self.title("Goose Panel | v.1.0.6")
        self._set_window_icon(self)
        self.gsi_manager = gsi_manager
        self.window_position_file = Path("window_position.txt")
        self.executor = ThreadPoolExecutor(max_workers=8)
        self.runtime_poll_in_flight = False
        self.ping_refresh_in_flight = False
        self._ui_actions_queue = queue.SimpleQueue()
        self._pending_section = None
        self._section_switch_job = None
        self._active_section = None
        self._accounts_scroll_fix_job = None
        self.splash_window = None
        self.splash_status = None
        self.splash_progress = None
        self._startup_services_initializer = startup_services_initializer
        
        self.geometry("1100x600")
        self.minsize(1100, 600)
        self.maxsize(1100, 600)
        self.configure(fg_color=BG_MAIN)
        self.withdraw()

        self._startup_steps = []
        self._startup_step_index = 0

        self.after(80, lambda: self._finish_initialization(startup_gpu_info))
    def _resolve_app_icon_path(self, base_dir=None):
        root = Path(base_dir) if base_dir else Path(__file__).resolve().parent.parent
        for icon_name in APP_ICON_CANDIDATES:
            icon_path = root / icon_name
            if icon_path.exists():
                return icon_path
        return None

    def _set_window_icon(self, window, base_dir=None):
        icon_path = self._resolve_app_icon_path(base_dir=base_dir)

        if icon_path:
            try:
                window.iconbitmap(str(icon_path))
            except Exception:
                try:
                    window.iconbitmap(str(APP_ICON_PATH))
                except Exception:
                    pass
    def _animate_window_alpha(self, window, start_alpha, end_alpha, duration_ms=180, steps=12, on_complete=None):
        if not window or not window.winfo_exists():
            if on_complete:
                on_complete()
            return

        if steps <= 0:
            steps = 1

        interval_ms = max(10, duration_ms // steps)
        delta = (end_alpha - start_alpha) / steps

        def tick(step=0):
            if not window.winfo_exists():
                if on_complete:
                    on_complete()
                return

            current = start_alpha + (delta * step)
            current = max(0.0, min(1.0, current))
            try:
                window.attributes("-alpha", current)
            except Exception:
                if on_complete:
                    on_complete()
                return

            if step >= steps:
                if on_complete:
                    on_complete()
                return

            self.after(interval_ms, lambda: tick(step + 1))

        tick()

    def _create_splash_screen(self):
        self.splash_window = customtkinter.CTkToplevel(self)
        self.splash_window.title("Goose Panel")
        self._set_window_icon(self.splash_window)
        self.splash_window.geometry("520x320")
        self.splash_window.resizable(False, False)
        self.splash_window.attributes("-topmost", True)
        self.splash_window.configure(fg_color=BG_MAIN)
        self.splash_window.protocol("WM_DELETE_WINDOW", lambda: None)

        splash_frame = customtkinter.CTkFrame(
            self.splash_window,
            fg_color=BG_PANEL,
            corner_radius=16,
            border_width=1,
            border_color=BG_BORDER,
        )
        self.splash_window.grid_rowconfigure(0, weight=1)
        self.splash_window.grid_columnconfigure(0, weight=1)
        splash_frame.grid(row=0, column=0, sticky="nsew", padx=24, pady=24)

        splash_title = customtkinter.CTkLabel(
            splash_frame,
            text="Goose Panel",
            font=customtkinter.CTkFont(size=34, weight="bold"),
            text_color=TXT_MAIN,
        )
        splash_title.pack(pady=(42, 10))

        self.splash_status = customtkinter.CTkLabel(
            splash_frame,
            text="Запуск...",
            font=customtkinter.CTkFont(size=14),
            text_color=TXT_SOFT,
        )
        self.splash_status.pack(pady=(0, 16))

        self.splash_progress = customtkinter.CTkProgressBar(
            splash_frame,
            mode="indeterminate",
            width=320,
            progress_color=ACCENT_BLUE,
            fg_color=ACCENT_PURPLE,
        )
        self.splash_progress.pack(pady=(0, 18))
        self.splash_progress.start()

        self.splash_window.update_idletasks()
        x = (self.winfo_screenwidth() // 2) - (520 // 2)
        y = (self.winfo_screenheight() // 2) - (320 // 2)
        self.splash_window.geometry(f"520x320+{x}+{y}")

    def _set_loading_status(self, message):
        if self.splash_status and self.splash_status.winfo_exists():
            self.splash_status.configure(text=message)
        if self.splash_window and self.splash_window.winfo_exists():
            self.splash_window.update_idletasks()

    def _close_splash_screen(self):
        if self.splash_progress and self.splash_progress.winfo_exists():
            self.splash_progress.stop()
        if self.splash_window and self.splash_window.winfo_exists():
            self.splash_window.destroy()
        self.splash_window = None
        self.splash_status = None
        self.splash_progress = None

    def _schedule_startup_step(self, status, action):
        self._startup_steps.append((status, action))

    def _run_next_startup_step(self):
        if self._startup_step_index >= len(self._startup_steps):
            self._startup_steps = []
            self._startup_step_index = 0
            return
            

        status, action = self._startup_steps[self._startup_step_index]
        self._startup_step_index += 1

        self._set_loading_status(status)
        def run_action():
            action()
            if self._startup_step_index < len(self._startup_steps):
                self.after(10, self._run_next_startup_step)

        self.after(20, run_action)

    def _run_startup_services(self):
        if not self._startup_services_initializer:
            return None
        try:
            return self._startup_services_initializer()
        except Exception as exc:
            try:
                print(f"⚠️ Ошибка фоновой инициализации сервисов: {exc}")
            except Exception:
                pass
            return None            
    def _finish_initialization(self, startup_gpu_info=None):
        self._startup_steps = []
        self._startup_step_index = 0
        self._startup_gpu_info = startup_gpu_info
        
        def prepare_window():
            self.is_unlocked = True
            self.license_token = None
            self.license_exp = 0
            self.license_nonce = None
            self.license_challenge_id = None
            self.license_challenge_exp = 0
            self._license_check_in_flight = False
            self._background_license_check_in_flight = False
            self.http_session = requests.Session()
            self.http_session.trust_env = False  # игнорировать системные прокси/ENV
            self.http_session.verify = True  # для http:// не влияет
            self._load_window_position()

        def load_resources():
            base_path = Path(sys._MEIPASS) if hasattr(sys, "_MEIPASS") else Path(__file__).parent.parent
            self._set_window_icon(self, base_dir=base_path)

        def init_managers():
            self.account_manager = AccountManager()
            self.log_manager = LogManager()
            self.settings_manager = SettingsManager()
            self.account_row_items = []
            self.account_badges = {}
            self.sdr_regions = {}
            self.sdr_region_servers = {}
            self._level_file_mtime = None
            self.lobby_buttons = {}
            self.telegram_bot_manager = None
            self.telegram_bot_status_label = None
            self.telegram_bot_create_button = None
            self.telegram_bot_remove_button = None
            self.telegram_bot_set_proxies_button = None
            self.control_frame = None
            self.farmed_file = Path("settings/accs_list.txt")
            self.farmed_file.parent.mkdir(exist_ok=True)

        def load_accounts_data():
            self.levels_cache = self._load_levels_from_json()
            self.farmed_accounts = self._load_farmed_accounts()


        def prepare_interface_state():
            self._build_srt_state()
        def load_regions():
            self._load_region_json_if_exists()
            # ВАЖНО: сначала создаём legacy контроллеры (accounts_list нужен для functional UI)
        def create_legacy_controllers():
            self._create_hidden_legacy_controllers()
        def build_interface_layout():
            self._build_layout()

        def connect_services():
            if self._startup_gpu_info is None:
                self._startup_gpu_info = self._run_startup_services()
            self._connect_gsi_to_ui()

            self._log_startup_gpu_info(self._startup_gpu_info)
            self._try_start_telegram_bot_from_settings()

        def finalize_startup():
            self.protocol("WM_DELETE_WINDOW", self.on_closing)
            self.show_section("functional")
            self._start_ui_actions_pump()
            self._start_runtime_status_tracking()
    


            self.deiconify()
            self.lift()


        self._schedule_startup_step("Подготавливаю окно...", prepare_window)
        self._schedule_startup_step("Загружаю ресурсы...", load_resources)
        self._schedule_startup_step("Инициализирую менеджеры...", init_managers)
        self._schedule_startup_step("Читаю данные аккаунтов...", load_accounts_data)
        self._schedule_startup_step("Формирую интерфейс...", prepare_interface_state)
        self._schedule_startup_step("Формирую интерфейс...", load_regions)
        self._schedule_startup_step("Формирую интерфейс...", create_legacy_controllers)
        self._schedule_startup_step("Формирую интерфейс...", build_interface_layout)
        self._schedule_startup_step("Подключаю сервисы...", connect_services)
        self._schedule_startup_step("Завершаю запуск...", finalize_startup)

        self._run_next_startup_step()
        
    # ---------------- UI queue / async ----------------
    def _start_ui_actions_pump(self):
        def pump():
            try:
                while True:
                    action = self._ui_actions_queue.get_nowait()
                    action()
            except queue.Empty:
                pass
            except Exception:
                pass
            finally:
                if self.winfo_exists():
                    self.after(50, pump)

        self.after(50, pump)

    def _queue_ui_action(self, action):
        try:
            self._ui_actions_queue.put(action)
        except Exception:
            pass

    def _run_action_async(self, fn, done_callback=None):
        try:
            future = self.executor.submit(fn)
        except Exception as exc:
            if self.winfo_exists():
                self.after(0, lambda: self.log_manager.add_log(f"❌ executor.submit failed: {exc}"))
            raise

        def on_done(done_future):
            if not done_callback:
                return
            try:
                self._queue_ui_action(lambda: done_callback(done_future))
            except Exception as exc:
                try:
                    self._queue_ui_action(lambda: self.log_manager.add_log(f"❌ done_callback scheduling failed: {exc}"))
                except Exception:
                    pass

        future.add_done_callback(on_done)
        return future

    def _safe_ui_refresh(self):
        if not self.winfo_exists():
            return
        self._sync_switches_with_selection()
        self._update_accounts_info()

    # ---------------- Accounts data logic ----------------
    def set_control_frame(self, control_frame):
        self.control_frame = control_frame

    def _load_levels_from_json(self):
        level_file = Path("level.json")
        if not level_file.exists():
            return {}
        try:
            with open(level_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception as e:
            print(f"⚠️ Ошибка level.json: {e}")
            return {}

    def _save_levels_to_json(self):
        try:
            with open("level.json", "w", encoding="utf-8") as f:
                json.dump(self.levels_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ Сохранение level.json: {e}")

    def _load_farmed_accounts(self):
        if not self.farmed_file.exists():
            return set()
        try:
            with open(self.farmed_file, "r", encoding="utf-8") as f:
                logins = [line.strip() for line in f.readlines() if line.strip()]
            return set(logins)
        except Exception as e:
            print(f"⚠️ Ошибка загрузки farmed_accounts: {e}")
            return set()

    def _save_farmed_accounts(self):
        try:
            with open(self.farmed_file, "w", encoding="utf-8") as f:
                for login in sorted(self.farmed_accounts):
                    f.write(f"{login}\n")
        except Exception as e:
            print(f"⚠️ Ошибка сохранения farmed_accounts: {e}")

    def _get_weekly_window_start(self, now=None):
        current_time = now or datetime.now()
        reset_anchor = current_time.replace(hour=WEEKLY_RESET_HOUR, minute=0, second=0, microsecond=0)
        days_since_reset = (current_time.weekday() - WEEKLY_RESET_WEEKDAY) % 7
        week_start = reset_anchor - timedelta(days=days_since_reset)
        if current_time < week_start:
            week_start -= timedelta(days=7)
        return week_start

    def is_drop_ready_login(self, login):
        account_data = self.levels_cache.get(login, self.levels_cache.get(str(login).lower(), {}))
        if not isinstance(account_data, dict):
            return False
        return account_data.get("drop_ready_week_start") == self._get_weekly_window_start().isoformat()

    def is_drop_ready_account(self, account):
        return self.is_drop_ready_login(account.login)

    def set_drop_ready(self, login, value=True):
        account_data = self.levels_cache.get(login, self.levels_cache.get(str(login).lower(), {}))
        if not isinstance(account_data, dict):
            account_data = {}

        if value:
            account_data["drop_ready_week_start"] = self._get_weekly_window_start().isoformat()
        else:
            account_data.pop("drop_ready_week_start", None)

        self.levels_cache[login] = account_data
        self._save_levels_to_json()

    def is_farmed_account(self, account):
        return account.login in self.farmed_accounts

    def is_reserved_from_rotation(self, account):
        return self.is_farmed_account(account) or self.is_drop_ready_account(account)

    def mark_farmed_accounts(self):
        selected_accounts = self.account_manager.selected_accounts.copy()
        for account in selected_accounts:
            login = account.login
            account.setColor("#ff9500")
            self.farmed_accounts.add(login)
            self.set_drop_ready(login, value=False)

        self._save_farmed_accounts()
        self.account_manager.selected_accounts.clear()
        self.update_label()

    def update_account_level(self, login, level, xp):
        existing = self.levels_cache.get(login, self.levels_cache.get(login.lower(), {}))
        current_data = existing if isinstance(existing, dict) else {}
        current_data.update({"level": level, "xp": xp})

        week_start_iso = self._get_weekly_window_start().isoformat()
        baseline_level = current_data.get("weekly_baseline_level")
        baseline_start = current_data.get("weekly_baseline_start")
        has_take_drop = (
            baseline_start == week_start_iso
            and isinstance(level, int)
            and isinstance(baseline_level, int)
            and level >= baseline_level + 1
        )

        if has_take_drop:
            current_data["drop_ready_week_start"] = week_start_iso

        self.levels_cache[login] = current_data
        self._save_levels_to_json()

        account = next((acc for acc in self.account_manager.accounts if acc.login == login), None)
        if has_take_drop and account and login not in self.farmed_accounts:
            account.setColor("#a855f7")

        self._queue_ui_action(self._refresh_level_labels)
        self._queue_ui_action(self.update_label)

    def select_first_non_farmed(self, n=4):
        available_accounts = [acc for acc in self.account_manager.accounts if not self.is_reserved_from_rotation(acc)]
        count = min(n, len(available_accounts))
        self.account_manager.selected_accounts.clear()
        self.account_manager.selected_accounts.extend(available_accounts[:count])
        self.update_label()

    def set_green_for_launched_cs2(self, launched_pids):
        processed_accounts = set()
        for account in self.account_manager.accounts:
            login = account.login
            if login in processed_accounts:
                continue

            cs2_pid = self._get_account_cs2_pid(login)
            if cs2_pid and cs2_pid in launched_pids:
                account.setColor("green")
            else:
                if login in self.farmed_accounts:
                    account.setColor("#ff9500")
                elif self.is_drop_ready_account(account):
                    account.setColor("#a855f7")
                else:
                    account.setColor("#DCE4EE")
            processed_accounts.add(login)

        self.update_label()

    def _get_account_cs2_pid(self, login):
        try:
            runtime_path = Path("runtime.json")
            if runtime_path.exists():
                with open(runtime_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data:
                    if item.get("login") == login:
                        return int(item.get("CS2Pid", 0))
        except Exception as e:
            print(f"⚠️ Ошибка поиска CS2Pid {login}: {e}")
        return None
    def _create_hidden_legacy_controllers(self):
        self.legacy_host = customtkinter.CTkFrame(self, fg_color="transparent")

        self.accounts_control = AccountsControl(self.legacy_host, self.update_label, self)
        self.control_frame = ControlFrame(self.legacy_host)
        self.main_menu = MainMenu(self.legacy_host)
        self.config_tab = ConfigTab(self.legacy_host)

        for widget in [self.accounts_control, self.control_frame, self.main_menu, self.config_tab]:
            widget.grid_remove()

        self.control_frame.set_accounts_list_frame(self)
        self.set_control_frame(self.control_frame)

    # ---------------- Layout ----------------
    def _build_layout(self):
        if not hasattr(self, "accounts_control"):
            self._create_hidden_legacy_controllers()

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = customtkinter.CTkFrame(
            self,
            width=200,
            corner_radius=1,
            fg_color=BG_PANEL,
            border_width=1,
            border_color=BG_BORDER,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(1, 1), pady=1)
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(7, weight=1)

        customtkinter.CTkLabel(
            self.sidebar,
            text="    Goose Panel  ",
            font=customtkinter.CTkFont(size=20, weight="bold"),
            text_color=TXT_MAIN,
        ).grid(row=0, column=0, padx=10, pady=(10, 4), sticky="w")

        self.nav_buttons = {}
        nav_items = [("functional", "Functionals"), ("config", "Configurations"), ("stats", "Accs Statistics")]
        for idx, (key, text) in enumerate(nav_items, start=1):
            btn = customtkinter.CTkButton(
                self.sidebar,
                text=text,
                width=150,
                height=34,
                corner_radius=9,
                font=customtkinter.CTkFont(size=12, weight="bold"),
                fg_color=BG_CARD_ALT,
                hover_color=BG_CARD,
                text_color=TXT_MAIN,
                border_width=1,
                border_color=ACCENT_RED,
                command=lambda k=key: self.show_section(k),
            )
            btn.grid(row=idx, column=0, padx=24, pady=4)
            self.nav_buttons[key] = btn

        logs_wrap = customtkinter.CTkFrame(
            self.sidebar,
            width=197,
            fg_color=BG_CARD_ALT,
            corner_radius=1,
            border_width=1,
            border_color=BG_BORDER,
        )
        logs_wrap.grid(row=7, column=0, padx=2, pady=(2, 2), sticky="nsew")
        logs_wrap.grid_propagate(False)
        logs_wrap.grid_columnconfigure(0, weight=1)
        logs_wrap.grid_rowconfigure(1, weight=1)

        customtkinter.CTkLabel(
            logs_wrap,
            text="• Logs",
            text_color=TXT_MAIN,
            font=customtkinter.CTkFont(size=15, weight="bold"),
        ).grid(row=0, column=0, padx=8, pady=(6, 2), sticky="w")

        self.logs_box = customtkinter.CTkTextbox(
            logs_wrap,
            width=250,
            fg_color="#0e1428",
            text_color="#98a7cf",
            border_width=0,
            corner_radius=8,
            wrap="word",
            font=customtkinter.CTkFont(size=11),
        )
        self.logs_box.grid(row=1, column=0, padx=2, pady=(0, 2), sticky="nsew")
        self.log_manager.textbox = self.logs_box

        self.content = customtkinter.CTkFrame(
            self,
            fg_color=BG_PANEL,
            corner_radius=12,
            border_width=1,
            border_color=BG_BORDER,
        )
        self.content.grid(row=0, column=1, padx=(6, 10), pady=10, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)

        self.sections = {
            "functional": self._build_functional_section(self.content),
            "config": self._build_config_section(self.content),

            "stats": self._build_stats_section(self.content),
        }
        for frame in self.sections.values():
            frame.grid(row=0, column=0, sticky="nsew")
    def _run_hidden_cmd(self, cmd, check=False):
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=check,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    # ---------------- Reset proxy/firewall ----------------
    def _reset_windows_proxy(self):
        if not sys.platform.startswith("win"):
            self.log_manager.add_log("⚠️ Reset доступен только на Windows")
            return

        self.log_manager.add_log("🔄 Reset: сброс proxy...")

        commands = [
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                'Remove-NetFirewallRule -Name "FSN_Route_*" -ErrorAction SilentlyContinue; '
                'Get-NetFirewallRule -DisplayName "FSN_Route_*" -ErrorAction SilentlyContinue | '
                "Remove-NetFirewallRule -ErrorAction SilentlyContinue",
            ],
            ["netsh", "advfirewall", "firewall", "delete", "rule", "name=FSN_Route_*"],
            ["netsh", "winhttp", "reset", "proxy"],
            ["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyEnable", "/t", "REG_DWORD", "/d", "0", "/f"],
            ["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyServer", "/t", "REG_SZ", "/d", "", "/f"],
            ["reg", "delete", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "AutoConfigURL", "/f"],
            ["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "AutoDetect", "/t", "REG_DWORD", "/d", "1", "/f"],
            ["reg", "add", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyEnable", "/t", "REG_DWORD", "/d", "0", "/f"],
            ["reg", "add", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "ProxyServer", "/t", "REG_SZ", "/d", "", "/f"],
            ["reg", "delete", r"HKLM\Software\Microsoft\Windows\CurrentVersion\Internet Settings", "/v", "AutoConfigURL", "/f"],
            ["rundll32.exe", "inetcpl.cpl,ClearMyTracksByProcess", "8"],
            ["ipconfig", "/flushdns"],
        ]

        command_errors = []
        for cmd in commands:
            try:
                result = self._run_hidden_cmd(cmd, check=False)
                if result.returncode != 0:
                    command_errors.append(" ".join(cmd[:3]))
            except Exception:
                command_errors.append(" ".join(cmd[:3]))

        try:
            verify = self._run_hidden_cmd(["netsh", "winhttp", "show", "proxy"], check=False)
            verify_text = ((verify.stdout or "") + "\n" + (verify.stderr or "")).lower()
        except Exception:
            verify_text = ""

        direct_markers = ("direct access", "прямой доступ", "without proxy", "без прокси", "no proxy server", "нет прокси")
        has_proxy_markers = ("proxy server", "прокси-сервер", "proxy-server")

        is_direct = any(marker in verify_text for marker in direct_markers)
        if not is_direct and verify_text:
            is_direct = not any(marker in verify_text for marker in has_proxy_markers)

        if is_direct:
            self.log_manager.add_log("✅ Reset завершен: proxy очищен")
        elif command_errors:
            self.log_manager.add_log("⚠️ Reset частично выполнен: запустите от администратора для полного сброса")
        else:
            self.log_manager.add_log("⚠️ Reset выполнен, но WinHTTP не подтвердил direct mode")

    # ---------------- Functional section ----------------
    def _build_functional_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)
        frame.grid_rowconfigure(2, weight=1)

        top = customtkinter.CTkFrame(frame, fg_color="transparent")
        top.grid(row=0, column=0, padx=10, pady=(8, 6), sticky="ew")

        title_frame = customtkinter.CTkFrame(top, fg_color="transparent")
        title_frame.grid(row=0, column=0, sticky="w")

        customtkinter.CTkLabel(
            title_frame,
            text="Accounts",
            text_color=TXT_MAIN,
            font=customtkinter.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, padx=(0, 10))

        self.accounts_info = customtkinter.CTkLabel(
            title_frame,
            text="0 accounts • 0 selected • 0 launched",
            text_color=TXT_MUTED,
            font=customtkinter.CTkFont(size=12),
        )
        self.accounts_info.grid(row=0, column=1)

        search_wrap = customtkinter.CTkFrame(title_frame, fg_color="transparent")
        search_wrap.grid(row=0, column=2, padx=(14, 0), sticky="w")
        self.search_var = customtkinter.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_account_filter())

        customtkinter.CTkEntry(
            search_wrap,
            textvariable=self.search_var,
            placeholder_text="Search",
            width=220,
            height=32,
            fg_color=BG_CARD,
            border_color=BG_BORDER,
            text_color=TXT_MAIN,
        ).grid(row=0, column=0)

        actions = customtkinter.CTkFrame(frame, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        actions.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")
        for i in range(5):
            actions.grid_columnconfigure(i, weight=1)

        common_btn = {"height": 34, "font": customtkinter.CTkFont(size=12, weight="bold")}
        customtkinter.CTkButton(actions, text="Launch Selected", command=self._action_start_selected, fg_color=ACCENT_BLUE, hover_color=ACCENT_BLUE_DARK, **common_btn).grid(row=0, column=0, padx=6, pady=8, sticky="ew")
        customtkinter.CTkButton(actions, text="Select 4 accs", command=self._action_select_first_4, fg_color=ACCENT_PURPLE, hover_color="#313866", **common_btn).grid(row=0, column=1, padx=6, pady=8, sticky="ew")
        customtkinter.CTkButton(actions, text="Select all accs", command=self._action_select_all_toggle, fg_color=BG_CARD_ALT, hover_color=BG_BORDER, **common_btn).grid(row=0, column=2, padx=6, pady=8, sticky="ew")
        customtkinter.CTkButton(actions, text="Kill selected", command=self._action_kill_selected, fg_color=BG_CARD_ALT, hover_color=BG_BORDER, **common_btn).grid(row=0, column=3, padx=6, pady=8, sticky="ew")

        main = customtkinter.CTkFrame(frame, fg_color="transparent")
        main.grid(row=2, column=0, padx=10, pady=(0, 8), sticky="nsew")
        main.grid_columnconfigure(0, weight=2)
        main.grid_columnconfigure(1, weight=1)
        main.grid_columnconfigure(2, weight=1)
        main.grid_rowconfigure(0, weight=1)
        main.grid_rowconfigure(1, weight=0)

        accounts_block = customtkinter.CTkFrame(main, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        accounts_block.grid(row=0, column=0, rowspan=2, padx=(0, 6), pady=0, sticky="nsew")
        accounts_block.grid_rowconfigure(1, weight=1)
        accounts_block.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(accounts_block, text="Accounts", font=customtkinter.CTkFont(size=20, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, padx=10, pady=8, sticky="w")

       
        self.accounts_scroll = customtkinter.CTkScrollableFrame(accounts_block, fg_color=BG_CARD_ALT)
        self.accounts_scroll.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.accounts_scroll.grid_columnconfigure(0, weight=1)
        self._create_account_rows()

        self.srt_placeholder = customtkinter.CTkFrame(main, width=260, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        self.srt_placeholder.grid(row=0, column=1, padx=6, pady=0, sticky="nsew")
        self.srt_placeholder.grid_propagate(False)
        self.srt_placeholder.grid_rowconfigure(2, weight=1)
        self.srt_placeholder.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(self.srt_placeholder, text="Steam Route Tool", text_color="#2ee66f", font=customtkinter.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=8, pady=(8, 3), sticky="w")

        actions_bar = customtkinter.CTkFrame(self.srt_placeholder, fg_color="transparent")
        actions_bar.grid(row=1, column=0, padx=8, pady=(0, 4), sticky="ew")
        actions_bar.grid_columnconfigure((0, 1), weight=1)

        customtkinter.CTkButton(actions_bar, text="Block all", fg_color=ACCENT_RED, hover_color="#962c38", height=28, command=self._srt_block_all, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=0, column=0, padx=(0, 4), sticky="ew")
        customtkinter.CTkButton(actions_bar, text="Reset", fg_color=BG_CARD_ALT, hover_color=BG_BORDER, height=28, command=self._srt_reset, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=0, column=1, padx=(4, 0), sticky="ew")

        self.srt_scroll = customtkinter.CTkScrollableFrame(self.srt_placeholder, fg_color=BG_CARD_ALT, corner_radius=8)
        self.srt_scroll.grid(row=2, column=0, padx=8, pady=(0, 8), sticky="nsew")
        self.srt_scroll.grid_columnconfigure(0, weight=1)
        self._build_srt_rows()

        tools = customtkinter.CTkFrame(main, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        tools.grid(row=0, column=2, padx=(6, 0), pady=0, sticky="nsew")
        tools.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(tools, text="Extra Tools", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=16, weight="bold")).grid(row=0, column=0, padx=8, pady=(8, 6), sticky="w")
        extra_buttons = [
            ("Move all CS windows", self._action_move_all_cs_windows, BG_CARD_ALT),
            ("Kill ALL CS & Steam", self._action_kill_all_cs_and_steam, ACCENT_PURPLE),
            ("Send trade", self._action_send_trade_selected, ACCENT_GREEN),
            ("Settings trade", self._action_open_looter_settings, ACCENT_RED),
            ("Marked farmed", self._action_marked_farmer, ACCENT_ORANGE),

        ]
        for idx, (text, cmd, color) in enumerate(extra_buttons, start=1):
            customtkinter.CTkButton(tools, text=text, command=cmd, fg_color=color, hover_color=BG_BORDER, height=34, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=idx, column=0, padx=8, pady=4, sticky="ew")

        lobby = customtkinter.CTkFrame(main, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        lobby.grid(row=1, column=1, columnspan=2, padx=(6, 0), pady=(0, 0), sticky="ew")
        customtkinter.CTkLabel(lobby, text="Lobby Management", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=13, weight="bold")).grid(row=0, column=0, columnspan=2, padx=8, pady=(8, 4), sticky="w")
        for i in range(2):
            lobby.grid_columnconfigure(i, weight=1)

        lobby_buttons = [
            ("Make Lobbies", self._action_make_lobbies, BG_CARD_ALT),
            ("Make Lobbies & Search Game", self._action_make_lobbies_and_search, ACCENT_BLUE),
            ("Disband lobbies", self._action_disband_lobbies, BG_CARD_ALT),
            ("Get level", self._action_try_get_level, BG_CARD_ALT),
            ("Shuffle Lobbies", self._action_shuffle_lobbies, BG_CARD_ALT),
            ("Get wingman rank", self._action_try_get_wingman_rank, BG_CARD_ALT),
        ]
        for idx, (text, cmd, color) in enumerate(lobby_buttons):
            r, c = divmod(idx, 2)
            btn = customtkinter.CTkButton(
                lobby,
                text=text,
                command=cmd,
                fg_color=color,
                hover_color=BG_BORDER,
                height=32,
                font=customtkinter.CTkFont(size=11, weight="bold"),
            )
            btn.grid(row=r + 1, column=c, padx=6, pady=4, sticky="ew")
            self.lobby_buttons[text] = btn

        self._update_accounts_info()

        return frame

    def _create_account_rows(self):
        self.account_row_items.clear()
        self.account_badges.clear()

        levels_cache = self.levels_cache or {}
        levels_cache_lower = {str(k).lower(): v for k, v in levels_cache.items()}

        for idx, account in enumerate(self.account_manager.accounts):
            row = customtkinter.CTkFrame(
                self.accounts_scroll,
                fg_color=BG_CARD,
                corner_radius=8,
                border_width=1,
                border_color=BG_BORDER,
                height=64,
            )
            row.grid(row=idx, column=0, padx=4, pady=3, sticky="ew")
            row.grid_columnconfigure(0, weight=0)
            row.grid_columnconfigure(1, weight=1)
            row.grid_columnconfigure(2, weight=0)

            sw = customtkinter.CTkSwitch(
                row,
                text="",
                width=24,
                command=lambda a=account: self._toggle_account(a),
                fg_color="#2d3b60",
                progress_color=ACCENT_BLUE,
            )
            sw.grid(row=0, column=0, rowspan=2, padx=(8, 6), pady=10, sticky="w")

            if account in self.account_manager.selected_accounts:
                sw.select()

            lvl_data = levels_cache.get(account.login, levels_cache_lower.get(account.login.lower(), {}))
            level_text = lvl_data.get("level", "--")
            xp_text = lvl_data.get("xp", "--")

            text_wrap = customtkinter.CTkFrame(row, fg_color="transparent")
            text_wrap.grid(row=0, column=1, rowspan=2, padx=(2, 6), pady=6, sticky="nsew")
            text_wrap.grid_columnconfigure(0, weight=1)

            login_label = customtkinter.CTkLabel(
                text_wrap,
                text=account.login,
                anchor="w",
                text_color=TXT_MAIN,
                font=customtkinter.CTkFont(size=12, weight="bold"),
            )
            login_label.grid(row=0, column=0, sticky="ew")

            level_label = customtkinter.CTkLabel(
                text_wrap,
                text=f"lvl: {level_text} | xp: {xp_text}",
                anchor="w",
                text_color=TXT_MUTED,
                font=customtkinter.CTkFont(size=11),
            )
            level_label.grid(row=1, column=0, pady=(2, 0), sticky="ew")

            badge = customtkinter.CTkLabel(
                row,
                text="Idle week",
                text_color="#dbe8ff",
                font=customtkinter.CTkFont(size=10),
                fg_color=ACCENT_BLUE,
                corner_radius=8,
                width=84,
                height=24,
            )
            badge.grid(row=0, column=2, rowspan=2, padx=(4, 8), pady=10, sticky="e")

            account.setColorCallback(lambda color, a=account: self._handle_account_color_change(a, color))
            self.account_badges[account.login] = badge

            self.account_row_items.append(
                {
                    "row": row,
                    "account": account,
                    "login_lower": account.login.lower(),
                    "switch": sw,
                    "login_label": login_label,
                    "level_label": level_label,
                    "badge": badge,
                    "ui_state": {
                        "login_color": None,
                        "level_text": None,
                        "badge_text": None,
                        "badge_color": None,
                        "visible": True,
                        "selected": account in self.account_manager.selected_accounts,
                    }
                }
            )

            self._refresh_account_badge(account)

        


                
    def _refresh_level_labels(self):
        try:
            self.levels_cache = self._load_levels_from_json()

            levels_cache = self.levels_cache or {}
            levels_cache_lower = {str(k).lower(): v for k, v in levels_cache.items()}

            changed = False

            for item in self.account_row_items:
                login = item["account"].login
                lvl_data = levels_cache.get(login, levels_cache_lower.get(login.lower(), {}))
                level_text = lvl_data.get("level", "--")
                xp_text = lvl_data.get("xp", "--")
                new_text = f"lvl: {level_text} | xp: {xp_text}"

                if item["ui_state"]["level_text"] != new_text:
                    item["level_label"].configure(text=new_text)
                    item["ui_state"]["level_text"] = new_text
                    changed = True

            for item in self.account_row_items:
                if self._refresh_account_badge(item["account"]):
                    changed = True

            if changed:
                self.after_idle(self._refresh_accounts_scroll_layout)

        except Exception:
            pass

    def _refresh_level_labels_if_changed(self):
        try:
            level_path = Path("level.json")
            mtime = level_path.stat().st_mtime if level_path.exists() else None
            if mtime != self._level_file_mtime:
                self._level_file_mtime = mtime
                self._refresh_level_labels()
        except Exception:
            pass

    def _normalize_account_color(self, color):
        color_map = {"green": ACCENT_GREEN, "yellow": "#f5c542", "white": "#DCE4EE"}
        return color_map.get(str(color).lower(), color)

    def _handle_account_color_change(self, account, color):
        normalized = self._normalize_account_color(color)
        if normalized == "#DCE4EE":
            if self.is_farmed_account(account):
                normalized = "#ff9500"
            elif self.is_drop_ready_account(account):
                normalized = "#a855f7"
        def apply_change():
            for item in self.account_row_items:
                if item["account"] is account:
                    item["login_label"].configure(text_color=normalized)
                    break
            self._refresh_account_badge(account)
            self._update_accounts_info()

        self._queue_ui_action(apply_change)

    def _refresh_account_badge(self, account, is_running=None):
        for item in self.account_row_items:
            if item["account"] is not account:
                continue

            badge_text, badge_color = self._get_weekly_badge_status(account)
            state = item["ui_state"]

            if state["badge_text"] != badge_text or state["badge_color"] != badge_color:
                item["badge"].configure(text=badge_text, fg_color=badge_color)
                state["badge_text"] = badge_text
                state["badge_color"] = badge_color
                return True

            return False

        return False



    def _get_weekly_badge_status(self, account):
        levels_cache = self.levels_cache or {}
        account_data = levels_cache.get(account.login, {})
        if not isinstance(account_data, dict):
            account_data = {}

        now = datetime.now()
        week_start = self._get_weekly_window_start(now)
        week_start_iso = week_start.isoformat()
        should_persist = False

        if account_data.get("weekly_baseline_start") != week_start_iso:
            account_data["weekly_baseline_start"] = week_start_iso
            level_value = account_data.get("level")
            account_data["weekly_baseline_level"] = level_value if isinstance(level_value, int) else None
            account_data.pop("trade_sent_week_start", None)
            should_persist = True

        if should_persist:
            levels_cache[account.login] = account_data
            self.levels_cache = levels_cache
            self._save_levels_to_json()

        if account_data.get("trade_sent_week_start") == week_start_iso:
            return "Sent trade", ACCENT_ORANGE

        current_level = account_data.get("level")
        baseline_level = account_data.get("weekly_baseline_level")
        if isinstance(current_level, int) and isinstance(baseline_level, int) and current_level >= baseline_level + 1:
            return "Take drop", ACCENT_GREEN

        return "Idle week", ACCENT_BLUE

    def _refresh_all_runtime_states(self):
        changed = False

        for item in self.account_row_items:
            account = item["account"]
            current_color = self._normalize_account_color(getattr(account, "_color", TXT_MAIN))

            if current_color == "#DCE4EE":
                if self.is_farmed_account(account):
                    current_color = "#ff9500"
                elif self.is_drop_ready_account(account):
                    current_color = "#a855f7"

            if item["ui_state"]["login_color"] != current_color:
                item["login_label"].configure(text_color=current_color)
                item["ui_state"]["login_color"] = current_color
                changed = True

        self._sync_switches_with_selection()
        self._update_accounts_info()

        if changed:
            self.after_idle(self._refresh_accounts_scroll_layout)

    def _start_runtime_status_tracking(self):
        def poll():
            try:
                self._refresh_all_runtime_states()
                self._refresh_level_labels_if_changed()
            except Exception:
                pass
            finally:
                if self.winfo_exists():
                    self.after(1500, poll)

        self.after(500, poll)

    def _apply_account_filter(self):
        filter_text = self.search_var.get().strip().lower() if hasattr(self, "search_var") else ""
        render_idx = 0
        changed = False

        for item in self.account_row_items:
            should_show = not filter_text or filter_text in item["login_lower"]
            was_visible = item["ui_state"]["visible"]

            if should_show:
                current_grid = item["row"].grid_info()
                current_row = current_grid.get("row")

                if (not was_visible) or str(current_row) != str(render_idx):
                    item["row"].grid(row=render_idx, column=0, padx=4, pady=3, sticky="ew")
                    changed = True

                render_idx += 1
            else:
                if was_visible:
                    item["row"].grid_remove()
                    changed = True

            item["ui_state"]["visible"] = should_show

        if changed:
            self._schedule_accounts_scroll_refresh()
    def _refresh_accounts_scroll_layout(self):
        try:
            if hasattr(self, "accounts_scroll") and self.accounts_scroll.winfo_exists():
                self.accounts_scroll.update_idletasks()
        except Exception:
            pass  


    
    def _schedule_accounts_scroll_refresh(self):
        if self._accounts_scroll_fix_job is not None:
            return

        def run():
            self._accounts_scroll_fix_job = None
            self._refresh_accounts_scroll_layout()

        self._accounts_scroll_fix_job = self.after(30, run)
    def _toggle_account(self, account):
        if account in self.account_manager.selected_accounts:
            self.account_manager.selected_accounts.remove(account)
        else:
            self.account_manager.selected_accounts.append(account)
        self._safe_ui_refresh()

    def _sync_switches_with_selection(self):
        selected = set(self.account_manager.selected_accounts)

        for item in self.account_row_items:
            should_be_selected = item["account"] in selected
            if item["ui_state"]["selected"] == should_be_selected:
                continue

            if should_be_selected:
                item["switch"].select()
            else:
                item["switch"].deselect()

            item["ui_state"]["selected"] = should_be_selected

    def _update_accounts_info(self):
        total = len(self.account_manager.accounts)
        selected = len(self.account_manager.selected_accounts)
        launched = self.account_manager.count_launched_accounts()
        if hasattr(self, "accounts_info"):
            self.accounts_info.configure(text=f"{total} accounts • {selected} selected • {launched} launched")

    # ---------------- Config section ----------------
    def _build_config_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color="transparent")
        frame.grid_columnconfigure(0, weight=1)

        header = customtkinter.CTkFrame(frame, fg_color="transparent")
        header.grid(row=0, column=0, padx=12, pady=(10, 4), sticky="ew")
        customtkinter.CTkLabel(header, text="Configurations", font=customtkinter.CTkFont(size=24, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, sticky="w")
        customtkinter.CTkLabel(header, text="Настройте автологику и пути Steam/CS2 в одном месте", font=customtkinter.CTkFont(size=11), text_color=TXT_MUTED).grid(row=1, column=0, pady=(4, 0), sticky="w")

        card = customtkinter.CTkFrame(frame, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        card.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="nsew")
        card.grid_columnconfigure(0, weight=1, minsize=150)
        card.grid_columnconfigure(1, weight=2, minsize=150)

        switches_card = customtkinter.CTkFrame(card, fg_color=BG_CARD_ALT, corner_radius=10, border_width=1, border_color=BG_BORDER)
        switches_card.grid(row=0, column=0, padx=(8, 4), pady=8, sticky="new")
        switches_card.grid_columnconfigure(0, weight=1)
        customtkinter.CTkLabel(switches_card, text="Automation", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=10, pady=(8, 2), sticky="w")

        self.config_toggle_auto_accept = self._create_labeled_switch(
            switches_card,
            row=1,
            title="Auto accept game",
            description="Автоматически принимает матч.",
            setting_key="AutoAcceptEnabled",
            on_toggle=self._on_auto_accept_toggle,
            default=True,
        )
        self.config_toggle_auto_match = self._create_labeled_switch(
            switches_card,
            row=2,
            title="Auto match in start",
            description="После 4 запуска CS2 ждёт 25с и начинает игру.",
            setting_key="AutoMatchInStartEnabled",
            default=True,
        )
        self.config_toggle_auto_account_switching = self._create_labeled_switch(
            switches_card,
            row=3,
            title="Automatic account switching",
            description="Автоматическая смена аккаунтов после отфарма",
            setting_key="AutomaticAccountSwitchingEnabled",
            default=True,
        )


        paths_card = customtkinter.CTkFrame(card, fg_color=BG_CARD_ALT, corner_radius=10, border_width=1, border_color=BG_BORDER)
        paths_card.grid(row=0, column=1, padx=(4, 8), pady=8, sticky="nsew")
        paths_card.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(paths_card, text="Steam / CS2 paths", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=15, weight="bold")).grid(row=0, column=0, padx=10, pady=(8, 2), sticky="w")

        self.path_status = {}
        self.path_entries = {}
        self._create_path_input(
            paths_card,
            row=1,
            label="Steam path",
            key="SteamPath",
            placeholder="C:/Program Files (x86)/Steam/steam.exe",
            validator=lambda value: Path(value).is_file() and value.lower().endswith(".exe"),
        )
        self._create_path_input(
            paths_card,
            row=2,
            label="CS2 path",
            key="CS2Path",
            placeholder="C:/Program Files (x86)/Steam/steamapps/common/Counter-Strike Global Offensive",
            validator=lambda value: (Path(value) / "game" / "bin" / "win64" / "cs2.exe").is_file(),
        )
        telegram_block = customtkinter.CTkFrame(paths_card, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BG_BORDER)
        telegram_block.grid(row=3, column=0, padx=8, pady=5, sticky="ew")
        telegram_block.grid_columnconfigure(0, weight=0)
        telegram_block.grid_columnconfigure(1, weight=0)
        telegram_block.grid_columnconfigure(2, weight=0)
        customtkinter.CTkLabel(telegram_block, text="Telegram bot", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=12, weight="bold")).grid(row=0, column=0, columnspan=3, padx=10, pady=(7, 2), sticky="w")
        customtkinter.CTkLabel(telegram_block, text="Управление panel через Telegram", text_color=TXT_SOFT, font=customtkinter.CTkFont(size=11)).grid(row=1, column=0, columnspan=3, padx=10, pady=(2, 6), sticky="w")

        self.telegram_bot_create_button = customtkinter.CTkButton(
            telegram_block,
            text="Create telegram bot",
            width=160,
            height=34,
            fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_DARK,
            font=customtkinter.CTkFont(size=11, weight="bold"),
            command=lambda: self._open_create_telegram_bot_dialog(),
        )
        self.telegram_bot_create_button.grid(row=2, column=0, padx=(10, 6), pady=(0, 8), sticky="w")

        self.telegram_bot_status_label = customtkinter.CTkLabel(
            telegram_block,
            text="",
            text_color=TXT_SOFT,
            font=customtkinter.CTkFont(size=11),
        )
        self.telegram_bot_status_label.grid(row=3, column=0, columnspan=3, padx=10, pady=(0, 8), sticky="w")

        self.telegram_bot_remove_button = customtkinter.CTkButton(
            telegram_block,
            text="Remove bot",
            width=120,
            height=32,
            fg_color=ACCENT_RED,
            hover_color="#9f2f3c",
            font=customtkinter.CTkFont(size=11, weight="bold"),
            command=self._remove_telegram_bot,
        )
        self.telegram_bot_set_proxies_button = customtkinter.CTkButton(
            telegram_block,
            text="Set proxies",
            width=120,
            height=32,
            fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_DARK,
            font=customtkinter.CTkFont(size=11, weight="bold"),
            command=self._open_telegram_proxies_dialog,
        )

        self.telegram_bot_remove_button.grid(row=2, column=1, padx=(0, 10), pady=(0, 8), sticky="w")
        self.telegram_bot_set_proxies_button.grid(row=2, column=2, padx=(0, 10), pady=(0, 8), sticky="w")
        self._refresh_telegram_bot_block()
        
        self.config_status_label = customtkinter.CTkLabel(frame, text="", text_color=TXT_MUTED, font=customtkinter.CTkFont(size=11, weight="bold"))
        self.config_status_label.grid(row=2, column=0, padx=14, pady=(0, 2), sticky="e")

        frame.grid_rowconfigure(1, weight=1)
        return frame

    def _create_labeled_switch(self, parent, row, title, description, setting_key, default=False, on_toggle=None):
        row_wrap = customtkinter.CTkFrame(parent, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BG_BORDER)
        row_wrap.grid(row=row, column=0, padx=8, pady=5, sticky="ew")
        row_wrap.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(row_wrap, text=title, text_color=TXT_MAIN, font=customtkinter.CTkFont(size=12, weight="bold")).grid(row=0, column=0, padx=10, pady=(7, 0), sticky="w")
        customtkinter.CTkLabel(row_wrap, text=description, text_color=TXT_SOFT, font=customtkinter.CTkFont(size=10)).grid(row=1, column=0, padx=10, pady=(2, 6), sticky="w")

        switch_wrap = customtkinter.CTkFrame(row_wrap, fg_color="transparent")
        switch_wrap.grid(row=2, column=0, padx=10, pady=(0, 6), sticky="w")
        customtkinter.CTkLabel(switch_wrap, text="OFF", text_color=TXT_MUTED, font=customtkinter.CTkFont(size=10, weight="bold")).grid(row=0, column=0, padx=(0, 4))

        switch = customtkinter.CTkSwitch(switch_wrap, text="", width=44)
        switch.grid(row=0, column=1)
        customtkinter.CTkLabel(switch_wrap, text="ON", text_color=TXT_MAIN, font=customtkinter.CTkFont(size=10, weight="bold")).grid(row=0, column=2, padx=(4, 0))

        current_value = bool(self.settings_manager.get(setting_key, default))
        if current_value:
            switch.select()
        else:
            switch.deselect()

        def handle_toggle():
            value = bool(switch.get())
            self.settings_manager.set(setting_key, value)
            if on_toggle:
                on_toggle(value)

        switch.configure(command=handle_toggle)
        return switch

    def _create_path_input(self, parent, row, label, key, placeholder, validator):
        wrap = customtkinter.CTkFrame(parent, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BG_BORDER)
        wrap.grid(row=row, column=0, padx=8, pady=5, sticky="ew")
        wrap.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(wrap, text=label, text_color=TXT_MAIN, font=customtkinter.CTkFont(size=12, weight="bold")).grid(row=0, column=0, padx=10, pady=(7, 2), sticky="w")
        entry = customtkinter.CTkEntry(wrap, placeholder_text=placeholder, fg_color=BG_CARD_ALT, border_color=BG_BORDER, text_color=TXT_MAIN)
        entry.grid(row=1, column=0, padx=10, pady=(0, 6), sticky="ew")

        saved_path = self.settings_manager.get(key, "") or ""
        entry.insert(0, saved_path)

        status = customtkinter.CTkLabel(wrap, text="", font=customtkinter.CTkFont(size=20, weight="bold"), text_color=ACCENT_GREEN)
        status.grid(row=1, column=1, padx=(0, 8), pady=(0, 6), sticky="e")

        def save_path():
            value = entry.get().strip()
            if not value:
                status.configure(text="✖", text_color=ACCENT_RED)
                self.config_status_label.configure(text=f"{label}: путь пустой", text_color=ACCENT_RED)
                return

            self.settings_manager.set(key, value)
            if validator(value):
                status.configure(text="✔", text_color=ACCENT_GREEN)
                self.config_status_label.configure(text=f"{label}: сохранено", text_color=ACCENT_GREEN)
            else:
                status.configure(text="✖", text_color=ACCENT_RED)
                self.config_status_label.configure(text=f"{label}: путь невалидный", text_color=ACCENT_RED)

        customtkinter.CTkButton(wrap, text="Save", width=60, height=24, fg_color=ACCENT_BLUE, hover_color=ACCENT_BLUE_DARK, command=save_path).grid(row=2, column=0, padx=10, pady=(0, 7), sticky="w")

        self.path_status[key] = status
        self.path_entries[key] = entry
        if saved_path and validator(saved_path):
            status.configure(text="✔", text_color=ACCENT_GREEN)

    def _on_auto_accept_toggle(self, enabled):
        try:
            self.main_menu._lobbyManager.auto_accept = enabled
        except Exception:
            pass

        try:
            module = self.main_menu.auto_accept_module
            if enabled and not module._running:
                module.start()
            elif (not enabled) and module._running:
                module.stop()
        except Exception:
            pass



    # ---------------- License logic ----------------
    def get_hwid(self):
        mac = uuid.getnode()
        return hashlib.sha256(str(mac).encode("utf-8")).hexdigest()[:20].upper()

    def _urlsafe_b64decode(self, value: str) -> bytes:
        padding = "=" * ((4 - len(value) % 4) % 4)
        return base64.urlsafe_b64decode((value + padding).encode("utf-8"))

    def _load_public_key(self):
        try:
            if LICENSE_EMBEDDED_PUBLIC_KEY_PEM.strip():
                return rsa.PublicKey.load_pkcs1_openssl_pem(LICENSE_EMBEDDED_PUBLIC_KEY_PEM.encode("utf-8"))
            if LICENSE_PUBLIC_KEY_PATH.exists():
                return rsa.PublicKey.load_pkcs1_openssl_pem(LICENSE_PUBLIC_KEY_PATH.read_bytes())
            return None
        except Exception:
            return None

    def _save_license_cache(self, signed_token, hwid, exp):
        try:
            LICENSE_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            LICENSE_CACHE_PATH.write_text(
                json.dumps({"signed_token": signed_token, "hwid": hwid, "exp": int(exp), "saved_at": int(time.time())}, ensure_ascii=False),
                encoding="utf-8",
            )
            return True
        except Exception:
            return False

    def _refresh_license_cache(self):
        """Гарантированно обновляет локальный кэш после успешного подтверждения лицензии."""
        try:
            token = getattr(self, "license_token", None)
            exp = int(getattr(self, "license_exp", 0) or 0)
            if not token or exp <= int(time.time()):
                return

            hwid = self.get_hwid()
            if not self._save_license_cache(token, hwid, exp):
                self.log_manager.add_log("⚠️ Не удалось обновить локальный кэш лицензии.")
        except Exception:
            pass

    def _clear_license_cache(self):
        try:
            if LICENSE_CACHE_PATH.exists():
                LICENSE_CACHE_PATH.unlink()
        except Exception:
            pass

    def _restore_cached_license(self, hwid):
        try:
            if not LICENSE_CACHE_PATH.exists():
                return False
            cached = json.loads(LICENSE_CACHE_PATH.read_text(encoding="utf-8"))
            token = cached.get("signed_token")
            cached_hwid = cached.get("hwid")
            cached_exp = int(cached.get("exp", 0) or 0)
            if not token:
                return False
            if cached_hwid and cached_hwid != hwid:
                self.log_manager.add_log("⚠️ Кэш лицензии отклонён: HWID не совпадает.")
                self._clear_license_cache()
                return False
            if cached_exp and cached_exp <= int(time.time()):
                self.log_manager.add_log("⚠️ Кэш лицензии просрочен.")
                self._clear_license_cache()
                return False
            payload = self._verify_signed_token(token, hwid, expected_nonce=None)
            self._apply_license_result(True, f"Офлайн кэш до {payload.get('expires_at', 'n/a')}")
            self.log_manager.add_log("ℹ️ Использован локальный кэш лицензии.")
            return True
        except Exception:
            self._clear_license_cache()
            return False

    def _request_license_challenge(self, hwid):
        # ВАЖНО: на сервере роут без trailing slash: /api/challenge
        url = f"{LICENSE_SERVER_URL}/api/challenge"

        response = self.http_session.get(
            url,
            params={"hwid": hwid},
            timeout=LICENSE_REQUEST_TIMEOUT,
        )
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Некорректный challenge от сервера")

        nonce = data.get("nonce")
        challenge_id = data.get("challenge_id")
        expires_in = int(data.get("expires_in", LICENSE_CHALLENGE_TTL_SECONDS))

        if not nonce or not challenge_id:
            raise ValueError(f"Сервер не вернул nonce/challenge_id. Ответ: {data}")

        self.license_nonce = nonce
        self.license_challenge_id = challenge_id
        self.license_challenge_exp = int(time.time()) + min(expires_in, LICENSE_CHALLENGE_TTL_SECONDS)

        return {"nonce": nonce, "challenge_id": challenge_id}


    def _request_license_state(self, hwid):
        # 1) получить challenge
        challenge = self._request_license_challenge(hwid)

        # 2) POST /api/check с JSON body (как в серверном CheckRequest)
        url = f"{LICENSE_SERVER_URL}/api/check"
        body = {
            "hwid": hwid,
            "challenge_id": challenge["challenge_id"],
            "nonce": challenge["nonce"],
            "ts": int(time.time()),
        }

        response = self.http_session.post(url, json=body, timeout=LICENSE_REQUEST_TIMEOUT)
        response.raise_for_status()

        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Некорректный ответ сервера лицензий")

        signed_token = data.get("signed_token")
        if not signed_token:
            raise ValueError(data.get("detail") or data.get("message") or f"Сервер не вернул signed_token. Ответ: {data}")

        # Сервер всегда возвращает подписанный токен (signed_token) в этом коде:
        return self._verify_signed_token(signed_token, hwid, expected_nonce=challenge["nonce"])

    def _verify_signed_token(self, signed_token, expected_hwid, expected_nonce=None):
        if not signed_token or "." not in signed_token:
            raise ValueError("Подпись лицензии отсутствует")

        payload_b64, signature_b64 = signed_token.split(".", 1)
        payload_raw = self._urlsafe_b64decode(payload_b64)
        signature = self._urlsafe_b64decode(signature_b64)

        public_key = self._load_public_key()
        if public_key is None:
            raise ValueError("Отсутствует settings/license_public_key.pem для проверки подписи")

        try:
            rsa.verify(payload_raw, signature, public_key)
        except Exception as exc:
            raise ValueError(f"Подпись сервера не прошла проверку: {exc}") from exc

        payload = json.loads(payload_raw.decode("utf-8"))
        now_ts = int(time.time())
        iat = int(payload.get("iat", 0))
        exp = int(payload.get("exp", 0))

        # Некоторые серверы лицензий возвращают unix-время в миллисекундах.
        # Нормализуем к секундам, чтобы не отклонять валидные токены.
        if iat > 10**12:
            iat //= 1000
        if exp > 10**12:
            exp //= 1000

        if payload.get("hwid") != expected_hwid:
            raise ValueError("HWID в токене не совпадает с устройством")
        if expected_nonce and payload.get("nonce") != expected_nonce:
            raise ValueError("Nonce в токене не совпадает с запросом")
        if iat > now_ts + LICENSE_TOKEN_TTL_GRACE_SECONDS:
            raise ValueError("Токен имеет некорректный iat")
        if exp <= now_ts:
            raise ValueError("Токен лицензии истёк")
        if exp - iat > MAX_TOKEN_TTL_SECONDS:
            raise ValueError("TTL токена превышает допустимый лимит")
        if payload.get("status") != "active":
            raise ValueError(payload.get("message") or "Лицензия не активна")

        self.license_token = signed_token
        self.license_exp = exp
        self.license_nonce = payload.get("nonce")
        self._save_license_cache(signed_token, expected_hwid, exp)
        return payload

    def _validate_current_token(self):
        return int(time.time()) < int(self.license_exp) - LICENSE_TOKEN_TTL_GRACE_SECONDS

    def check_license_async(self, hwid):
        # если уже идёт проверка — НЕ молчим
        if self._license_check_in_flight:
            self.log_manager.add_log("⏳ Проверка лицензии уже выполняется...")
            try:
                self.license_status.configure(text="Статус: Проверка уже идёт...", text_color=ACCENT_ORANGE)
            except Exception:
                pass
            return

        self._license_check_in_flight = True
        request_id = getattr(self, "_license_check_request_id", 0) + 1
        self._license_check_request_id = request_id
        self.log_manager.add_log(f"🔄 Проверка лицензии: {hwid}...")
        try:
            self.license_status.configure(text="Статус: Проверка...", text_color=ACCENT_ORANGE)
        except Exception:
            pass

        # watchdog: если что-то пошло не так и future не вернулось — сбросим флаг
        watchdog_id = None

        def watchdog():
            # если спустя 15с всё ещё "in flight" — сбрасываем и логируем
            if (
                self._license_check_in_flight
                and getattr(self, "_license_check_request_id", 0) == request_id
                and self.winfo_exists()
            ):
                self._license_check_in_flight = False
                if not self.is_unlocked:
                    self.log_manager.add_log("⚠️ Проверка лицензии зависла/не вернулась. Флаг сброшен, попробуйте ещё раз.")
                    try:
                        self.license_status.configure(text="Статус: Таймаут проверки", text_color=ACCENT_RED)
                    except Exception:
                        pass

        if self.winfo_exists():
            watchdog_id = self.after(LICENSE_WATCHDOG_TIMEOUT_MS, watchdog)

        def task():
            return self._request_license_state(hwid)

        def done(fut):
            nonlocal watchdog_id
            try:
                # блокируем watchdog до изменения UI, чтобы не перетёр корректный статус
                self._license_check_in_flight = False
                if watchdog_id is not None and self.winfo_exists():
                    try:
                        self.after_cancel(watchdog_id)
                    except Exception:
                        pass

                payload = fut.result()
                msg = f"Активна до {payload.get('expires_at', 'n/a')}"
                self._apply_license_result(True, msg)
                self._refresh_license_cache()

            except Exception as exc:
                self._apply_license_result(False, f"Проверка не пройдена: {exc}")

       

        # важно: если executor.submit упал — тоже сбросить флаг
        try:
            self._run_action_async(task, done)
        except Exception as exc:
            if watchdog_id is not None and self.winfo_exists():
                try:
                    self.after_cancel(watchdog_id)
                except Exception:
                    pass
            self._license_check_in_flight = False
            self.log_manager.add_log(f"❌ Не удалось запустить проверку в фоне: {exc}")
            try:
                self.license_status.configure(text="Статус: Ошибка запуска проверки", text_color=ACCENT_RED)
            except Exception:
                pass

    def _start_background_check(self):
        my_hwid = self.get_hwid()

        if self._license_check_in_flight or self._background_license_check_in_flight:
            if self.winfo_exists():
                self.after(LICENSE_RECHECK_INTERVAL_MS, self._start_background_check)
            return

        self._background_license_check_in_flight = True
        self._run_action_async(lambda: self._request_license_state(my_hwid), self._on_silent_check_done)

        if self.winfo_exists():
            self.after(LICENSE_RECHECK_INTERVAL_MS, self._start_background_check)

    def _on_silent_check_done(self, future):
        self._background_license_check_in_flight = False

        if future.exception():
            self.log_manager.add_log(f"⚠️ Автопроверка: ошибка проверки лицензии: {future.exception()}")
            self._apply_license_result(False, "Проверьте лицензию: запись не найдена или недоступна в БД")
            self.log_manager.add_log("⚠️ Автопроверка: лицензия не подтверждена в БД. Доступ ограничен до раздела License.")
            return
        payload = future.result()
        expires_at = payload.get("expires_at", "n/a")
        if self.is_unlocked:

            return

        self._apply_license_result(True, f"Активна до {expires_at}")
            
    def _ensure_license(self):
        return True

    def _apply_license_result(self, is_valid, message):
        self.is_unlocked = is_valid

        if is_valid:
            self._refresh_license_cache()
            status_text = "Статус: Лицензия подтверждена"
            if message:
                status_text = f"{status_text} ({message})"
            try:
                self.license_status.configure(text=status_text, text_color=ACCENT_GREEN)
            except Exception:
                pass
            self.log_manager.add_log(f"✅ Лицензия подтверждена сервером! {message}")
            self.show_section(self._pending_section or "license")
        else:
            try:
                self.license_status.configure(text="Статус: Лицензия не подтверждена. Нажмите «Проверить»", text_color=ACCENT_RED)
            except Exception:
                pass
            self.log_manager.add_log(f"❌ Лицензия отклонена: {message}")
            self.show_section(self._pending_section or "functional")

    # ---------------- License section UI ----------------
    def _build_license_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        frame.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(frame, text="License", font=customtkinter.CTkFont(size=30, weight="bold"), text_color=TXT_MAIN).grid(row=0, column=0, padx=16, pady=(20, 8), sticky="w")

        self.license_status = customtkinter.CTkLabel(frame, text="Статус: Ожидание...", text_color=ACCENT_ORANGE, font=customtkinter.CTkFont(size=14, weight="bold"))
        self.license_status.grid(row=1, column=0, padx=16, pady=(0, 14), sticky="w")

        block = customtkinter.CTkFrame(frame, fg_color=BG_CARD_ALT, corner_radius=8, border_width=1, border_color=BG_BORDER)
        block.grid(row=2, column=0, padx=16, pady=8, sticky="ew")
        block.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(block, text="Ваш HWID:", text_color=TXT_SOFT).grid(row=0, column=0, padx=10, pady=(8, 2), sticky="w")

        hwid_entry = customtkinter.CTkEntry(block, height=34)
        hwid_entry.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")

        my_hwid = self.get_hwid()
        hwid_entry.insert(0, my_hwid)
        hwid_entry.configure(state="readonly")

        customtkinter.CTkButton(
            block,
            text="Копировать",
            width=100,
            height=34,
            fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_DARK,
            command=lambda: [self.clipboard_clear(), self.clipboard_append(my_hwid), self.log_manager.add_log("📋 HWID скопирован")],
        ).grid(row=1, column=1, padx=(0, 10), pady=(0, 8))

        customtkinter.CTkButton(
            block,
            text="Проверить",
            width=100,
            height=34,
            fg_color=ACCENT_GREEN,
            hover_color="#177a42",
            command=lambda: self.check_license_async(my_hwid),
        ).grid(row=1, column=2, padx=(0, 10), pady=(0, 8))


        self._apply_license_result(True, "Public build")
        return frame


    # ---------------- Stats section ----------------
    def _build_stats_section(self, parent):
        frame = customtkinter.CTkFrame(parent, fg_color=BG_CARD, corner_radius=10, border_width=1, border_color=BG_BORDER)
        frame.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(
            frame,
            text="Accs Statistics",
            font=customtkinter.CTkFont(size=30, weight="bold"),
            text_color=TXT_MAIN,
        ).grid(row=0, column=0, padx=16, pady=(20, 8), sticky="w")

        notice_card = customtkinter.CTkFrame(
            frame,
            fg_color=BG_CARD_ALT,
            corner_radius=14,
            border_width=1,
            border_color=BG_BORDER,
        )
        notice_card.grid(row=1, column=0, padx=16, pady=(8, 16), sticky="ew")
        notice_card.grid_columnconfigure(0, weight=1)

        customtkinter.CTkLabel(
            notice_card,
            text="🚧 В разработке",
            font=customtkinter.CTkFont(size=24, weight="bold"),
            text_color=TXT_MAIN,
        ).grid(row=0, column=0, padx=20, pady=(20, 8))

        customtkinter.CTkLabel(
            notice_card,
            text="Скоро здесь появится расширенная статистика аккаунтов\nс красивыми графиками и детальной аналитикой.",
            font=customtkinter.CTkFont(size=13),
            text_color=TXT_SOFT,
            justify="center",
        ).grid(row=1, column=0, padx=20, pady=(0, 14))

        buttons_row = customtkinter.CTkFrame(notice_card, fg_color="transparent")
        buttons_row.grid(row=2, column=0, padx=20, pady=(0, 20))
        customtkinter.CTkButton(
            buttons_row,
            text="📨 Telegram channel",
            width=180,
            height=38,
            fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_DARK,
            font=customtkinter.CTkFont(size=13, weight="bold"),
            command=lambda: webbrowser.open("https://t.me/fermagoose"),
        ).grid(row=0, column=0, padx=6)

        customtkinter.CTkButton(
            buttons_row,
            text="Support Developer",
            width=180,
            height=38,
            fg_color=ACCENT_GREEN,
            hover_color="#177a42",
            font=customtkinter.CTkFont(size=13, weight="bold"),
            command=lambda: webbrowser.open(
                "https://steamcommunity.com/tradeoffer/new/?partner=1820312068&token=IfT_ec3_"
            ),
        ).grid(row=0, column=1, padx=6)

        customtkinter.CTkButton(
            buttons_row,
            text="Support",
            width=180,
            height=38,
            fg_color=ACCENT_RED,
            hover_color="#8f2329",
            font=customtkinter.CTkFont(size=13, weight="bold"),
            command=lambda: webbrowser.open("https://t.me/fbdanu"),
        ).grid(row=0, column=2, padx=6)
        return frame

    # ---------------- Actions (locked) ----------------
    def _action_start_selected(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.accounts_control.start_selected)

    def _action_select_first_4(self):
        if not self._ensure_license():
            return
        non_farmed = [acc for acc in self.account_manager.accounts if not self.is_reserved_from_rotation(acc)]
        target = non_farmed[:4]
        current = self.account_manager.selected_accounts
        if len(current) == len(target) and all(a in current for a in target):
            self.account_manager.selected_accounts.clear()
        else:
            self.account_manager.selected_accounts.clear()
            self.account_manager.selected_accounts.extend(target)
        self._safe_ui_refresh()

    def _action_select_all_toggle(self):
        if not self._ensure_license():
            return
        if len(self.account_manager.selected_accounts) == len(self.account_manager.accounts):
            self.account_manager.selected_accounts.clear()
        else:
            self.account_manager.selected_accounts.clear()
            self.account_manager.selected_accounts.extend(self.account_manager.accounts)
        self._safe_ui_refresh()

    def _action_reload_accounts(self):
        if not self._ensure_license():
            return

        self.account_manager.reload_accounts_from_disk()
        self._create_account_rows()
        self._safe_ui_refresh()
        
    def _action_kill_selected(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.accounts_control.kill_selected)

    def _action_try_get_level(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.accounts_control.try_get_level, lambda _: self.after(300, self._refresh_level_labels))

    def _action_kill_all_cs_and_steam(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.control_frame.kill_all_cs_and_steam)

    def _action_move_all_cs_windows(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.control_frame.move_all_cs_windows)

    def _action_launch_bes(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.control_frame.launch_bes)

    def _action_try_get_wingman_rank(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.accounts_control.try_get_wingmanRank)

    def _action_send_trade_selected(self):
        if not self._ensure_license():
            return
        self.config_tab.send_trade_selected(on_trade_sent=self._on_trade_sent_success)

    def _on_trade_sent_success(self, login):
        def mark_sent():
            levels_cache = self.levels_cache or {}
            account_data = levels_cache.get(login, {})
            if not isinstance(account_data, dict):
                account_data = {}

            week_start_iso = self._get_weekly_window_start().isoformat()
            account_data["trade_sent_week_start"] = week_start_iso
            levels_cache[login] = account_data
            self.levels_cache = levels_cache

            self._save_levels_to_json()

            for item in self.account_row_items:
                if item["account"].login == login:
                    self._refresh_account_badge(item["account"])
                    break

        self._queue_ui_action(mark_sent)

    def _action_open_looter_settings(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.config_tab.open_looter_settings)

    def _action_marked_farmer(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.accounts_control.mark_farmed, lambda _: self._safe_ui_refresh())

    def _action_make_lobbies_and_search(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.main_menu.make_lobbies_and_search_game)

    def trigger_make_lobbies_and_search_button(self):
        button = self.lobby_buttons.get("Make lobbies & search game")
        if button is None:
            for text, candidate in self.lobby_buttons.items():
                if text.strip().lower() == "make lobbies & search game":
                    button = candidate
                    break
        if button is None:
            self.log_manager.add_log("❌ UI button 'Make lobbies & search game' not found in app.py")
            return False

        def invoke_button():
            try:
                button.invoke()
                self.log_manager.add_log("✅ AUTO: invoke() on app.py button 'Make lobbies & search game'")
            except Exception as error:
                self.log_manager.add_log(f"❌ Failed to invoke app.py button: {error}")

        self._queue_ui_action(invoke_button)
        return True

    def _action_make_lobbies(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.main_menu.make_lobbies)

    def _action_shuffle_lobbies(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.main_menu.shuffle_lobbies)

    def _action_disband_lobbies(self):
        if not self._ensure_license():
            return
        self._run_action_async(self.main_menu.disband_lobbies)

    # ---------------- Regions / SRT ----------------
    def _load_region_json_if_exists(self):
        region_path = Path("region.json")
        if not region_path.exists():
            return
        try:
            raw_data = json.loads(region_path.read_text(encoding="utf-8-sig"))
            if isinstance(raw_data, dict):
                pops = raw_data.get("pops", {})
            elif isinstance(raw_data, list):
                pops = {str(index): item for index, item in enumerate(raw_data) if isinstance(item, dict)}
            else:
                pops = {}
            parsed_regions = {}
            parsed_region_servers = {}
            parsed_ping_targets = {}

            for pop_key, pop_data in pops.items():
                relays = pop_data.get("relays", [])
                if not relays:
                    continue

                desc = pop_data.get("desc") or pop_key
                relay_ips = []
                ping_targets = []
                for relay in relays:
                    ip = relay.get("ipv4")
                    if not ip:
                        continue
                    relay_ips.append(ip)

                    port_range = relay.get("port_range") or []
                    if isinstance(port_range, (list, tuple)) and len(port_range) >= 2:
                        try:
                            start_port = int(port_range[0])
                            end_port = int(port_range[1])
                        except Exception:
                            start_port, end_port = 27015, 27060
                    else:
                        start_port, end_port = 27015, 27060

                    ping_targets.append((ip, start_port, end_port))

                if not relay_ips:
                    continue

                parsed_regions[desc] = sorted(set(relay_ips))
                parsed_region_servers[desc] = sorted(set(relay_ips))
                parsed_ping_targets[desc] = sorted(set(ping_targets))

            if parsed_regions:
                self.sdr_regions = parsed_regions
                self.sdr_region_servers = parsed_region_servers
                REGION_PING_TARGETS.clear()
                REGION_PING_TARGETS.update(parsed_ping_targets)
        except Exception:
            pass

    def _build_srt_state(self):
        self.route_manager = SteamRouteManager() if sys.platform.startswith("win") else None
        self.blocked_servers = set()
        self.region_expand_state = {}
        self.srt_rows = {}
        self.region_ping_cache = {}
    def _server_rule_name(self, region, server_ip):
        return f"{region}::{server_ip}"

    def _get_region_servers(self, region):
        return self.sdr_region_servers.get(region, self.sdr_regions.get(region, []))

    def _is_region_fully_blocked(self, region):
        servers = self._get_region_servers(region)
        if not servers:
            return False
        return all(self._server_rule_name(region, ip) in self.blocked_servers for ip in servers)
    def _build_srt_rows(self):
        if not self.sdr_regions:
            customtkinter.CTkLabel(self.srt_scroll, text="region.json не найден или пуст", text_color=TXT_MUTED, font=customtkinter.CTkFont(size=11)).grid(row=0, column=0, padx=6, pady=8, sticky="w")
            return

        for idx, region in enumerate(self.sdr_regions.keys()):
            self.region_expand_state[region] = False
            region_card = customtkinter.CTkFrame(self.srt_scroll, fg_color=BG_CARD, corner_radius=8, border_width=1, border_color=BG_BORDER)
            region_card.grid(row=idx, column=0, padx=2, pady=2, sticky="ew")
            region_card.grid_columnconfigure(0, weight=1)

            row = customtkinter.CTkFrame(region_card, fg_color="transparent")
            row.grid(row=0, column=0, padx=0, pady=0, sticky="ew")
            row.grid_columnconfigure(1, weight=1)

            expand_btn = customtkinter.CTkButton(
                row,
                text="↓",
                width=24,
                height=24,
                fg_color="transparent",
                text_color=ACCENT_RED,
                hover_color=BG_BORDER,
                font=customtkinter.CTkFont(size=12, weight="bold"),
                command=lambda r=region: self._toggle_region_expand(r),
            )
            expand_btn.grid(row=0, column=0, padx=(4, 0), pady=3)

            customtkinter.CTkLabel(row, text=region, text_color=TXT_MAIN, font=customtkinter.CTkFont(size=11, weight="bold")).grid(row=0, column=1, padx=(4, 2), pady=4, sticky="w")
            
            ping_label = customtkinter.CTkLabel(row, text="-- ms", text_color=TXT_MUTED, font=customtkinter.CTkFont(size=10))
            ping_label.grid(row=0, column=2, padx=2, pady=4)

            block_btn = customtkinter.CTkButton(
                row,
                text="✕",
                width=26,
                height=24,
                fg_color=BG_CARD_ALT,
                hover_color=ACCENT_RED,
                font=customtkinter.CTkFont(size=12, weight="bold"),
                command=lambda r=region: self._toggle_region_block(r),
            )
            block_btn.grid(row=0, column=3, padx=(2, 6), pady=3)

            servers_frame = customtkinter.CTkFrame(region_card, fg_color=BG_CARD_ALT, corner_radius=6)
            servers_frame.grid(row=1, column=0, padx=6, pady=(0, 6), sticky="ew")
            servers_frame.grid_columnconfigure(0, weight=1)
            servers_frame.grid_remove()

            server_rows = {}
            for sidx, server_ip in enumerate(self._get_region_servers(region)):
                server_row = customtkinter.CTkFrame(servers_frame, fg_color="transparent")
                server_row.grid(row=sidx, column=0, padx=2, pady=1, sticky="ew")
                server_row.grid_columnconfigure(0, weight=1)

                customtkinter.CTkLabel(server_row, text=server_ip, text_color=TXT_SOFT, font=customtkinter.CTkFont(size=10)).grid(row=0, column=0, padx=(8, 2), pady=2, sticky="w")

                server_btn = customtkinter.CTkButton(
                    server_row,
                    text="✕",
                    width=26,
                    height=22,
                    fg_color=BG_CARD,
                    hover_color=ACCENT_RED,
                    font=customtkinter.CTkFont(size=11, weight="bold"),
                    command=lambda r=region, ip=server_ip: self._toggle_server_block(r, ip),
                )
                server_btn.grid(row=0, column=1, padx=(2, 4), pady=2)
                server_rows[server_ip] = server_btn

            self.srt_rows[region] = {
                "ping": ping_label,
                "button": block_btn,
                "expand": expand_btn,
                "servers_frame": servers_frame,
                "server_buttons": server_rows,
            }

        self._restore_blocked_regions_state_async()
        self._schedule_ping_refresh()

    def _restore_blocked_regions_state_async(self):
        if self.route_manager is None:
            return

        def on_done(future):
            try:
                blocked_items = future.result()
            except Exception:
                blocked_items = set()
            self._apply_blocked_regions_state(blocked_items)

        self._run_action_async(self.route_manager.get_blocked_regions, on_done)

    def _apply_blocked_regions_state(self, blocked_items):
        if not blocked_items:
            return
        valid_rules = set()
        for region in self.sdr_regions.keys():
            for ip in self._get_region_servers(region):
                valid_rules.add(self._server_rule_name(region, ip))
        self.blocked_servers = {rule for rule in blocked_items if rule in valid_rules}
        for region in self.sdr_regions.keys():
            self._set_region_visual(region)
    def _set_server_visual(self, region, server_ip):
        row = self.srt_rows.get(region)
        if not row:
            return
        server_btn = row.get("server_buttons", {}).get(server_ip)
        if not server_btn:
            return
        is_blocked = self._server_rule_name(region, server_ip) in self.blocked_servers
        server_btn.configure(
            fg_color=ACCENT_RED if is_blocked else BG_CARD,
            text="✓" if is_blocked else "✕",
            hover_color="#962c38" if is_blocked else ACCENT_RED,
        )
    def _set_region_visual(self, region):
        row = self.srt_rows.get(region)
        if not row:
            return
        is_blocked = self._is_region_fully_blocked(region)
        row["button"].configure(
            fg_color=ACCENT_RED if is_blocked else BG_CARD_ALT,
            text="✓" if is_blocked else "✕",
            hover_color="#962c38" if is_blocked else ACCENT_RED,
        )
        for server_ip in row.get("server_buttons", {}).keys():
            self._set_server_visual(region, server_ip)

    def _toggle_region_expand(self, region):
        row = self.srt_rows.get(region)
        if not row:
            return
        expanded = not self.region_expand_state.get(region, False)
        self.region_expand_state[region] = expanded
        if expanded:
            row["servers_frame"].grid()
            row["expand"].configure(text="▾")
        else:
            row["servers_frame"].grid_remove()
            row["expand"].configure(text="▸")

    def _toggle_server_block(self, region, server_ip):
        rule_name = self._server_rule_name(region, server_ip)

        def op():

            if rule_name in self.blocked_servers:
                ok = True if self.route_manager is None else self.route_manager.remove_rule(rule_name)
                if ok:
                    self.blocked_servers.discard(rule_name)
            else:
                ok = True if self.route_manager is None else self.route_manager.add_block_rule(rule_name, [server_ip])
                if ok:
                    self.blocked_servers.add(rule_name)
            return True

        self._run_action_async(op, lambda _: self._set_region_visual(region))

    def _toggle_region_block(self, region):
        def op():
            if self.route_manager is None:
                for region in self.sdr_regions.keys():
                    for server_ip in self._get_region_servers(region):
                        self.blocked_servers.add(self._server_rule_name(region, server_ip))
                return True

            rules_to_add = {}
            target_block_state = not self._is_region_fully_blocked(region)
            for server_ip in self._get_region_servers(region):
                rule_name = self._server_rule_name(region, server_ip)
                if target_block_state:
                    if rule_name in self.blocked_servers:
                        continue
                    rules_to_add[rule_name] = [server_ip]

            if not rules_to_add:
                return True

            results = self.route_manager.add_block_rules_bulk(rules_to_add)

            for rule_name, ok in results.items():
                if ok:
                    self.blocked_servers.add(rule_name)
                else:
                    ok = True if self.route_manager is None else self.route_manager.remove_rule(rule_name)
                    if ok:
                        self.blocked_servers.discard(rule_name)

            return True

        self._run_action_async(op, lambda _: self._set_region_visual(region))

    def _srt_block_all(self):
        def op():
            if self.route_manager is None:
                for region in self.sdr_regions.keys():
                    for server_ip in self._get_region_servers(region):
                        self.blocked_servers.add(self._server_rule_name(region, server_ip))
                return

            rules_to_add = {}
            for region in self.sdr_regions.keys():
                for server_ip in self._get_region_servers(region):
                    rule_name = self._server_rule_name(region, server_ip)

                    if rule_name in self.blocked_servers:
                        continue
                    rules_to_add[rule_name] = [server_ip]

            results = self.route_manager.add_block_rules_bulk(rules_to_add)
            for rule_name, ok in results.items():
                if ok:
                    self.blocked_servers.add(rule_name)

        def done(_):
            for region in self.sdr_regions.keys():
                self._set_region_visual(region)

        self._run_action_async(op, done)

    def _srt_reset(self):
        def op():
            self._reset_windows_proxy()

            self.blocked_servers.clear()

        def done(_):
            for region in self.sdr_regions.keys():
                self._set_region_visual(region)

        self._run_action_async(op, done)

    def _measure_host_latency_ms(self, host, tcp_ports=None):
        try:
            if sys.platform.startswith("win"):
                cmd = ["cmd", "/c", "ping", "-n", "1", "-w", "1000", host]
            else:
                cmd = ["ping", "-c", "1", "-W", "1", host]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=False,
                timeout=4,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            raw_output = (result.stdout or b"") + b"\n" + (result.stderr or b"")
            decoded_parts = []
            for enc in ("utf-8", "cp866", "cp1251", "latin-1"):
                try:
                    decoded_parts.append(raw_output.decode(enc, errors="ignore"))
                except Exception:
                    pass
            out = "\n".join(decoded_parts).lower()

            samples = []
            for m in re.finditer(r"(?:time|время)\s*[=<]?\s*([0-9]+(?:[\.,][0-9]+)?)\s*(?:ms|мс|мсек)?", out):
                try:
                    samples.append(float(m.group(1).replace(",", ".")))
                except Exception:
                    pass

            for m in re.finditer(r"(?<!\d)([0-9]+(?:[\.,][0-9]+)?)\s*(?:ms|мс|мсек)", out):
                try:
                    samples.append(float(m.group(1).replace(",", ".")))
                except Exception:
                    pass

            avg_match = re.search(r"(?:average|avg|среднее)\s*[=:]\s*([0-9]+(?:[\.,][0-9]+)?)", out)
            if avg_match:
                try:
                    samples.append(float(avg_match.group(1).replace(",", ".")))
                except Exception:
                    pass

            rtt_match = re.search(r"(?:rtt|round-trip)[^=]*=\s*[0-9]+(?:[\.,][0-9]+)?/([0-9]+(?:[\.,][0-9]+)?)/", out)
            if rtt_match:
                try:
                    samples.append(float(rtt_match.group(1).replace(",", ".")))
                except Exception:
                    pass

            if samples:
                return samples[0]

            if sys.platform.startswith("win"):
                ps_cmd = (
                    f'$r=Test-Connection -Count 1 -TimeoutSeconds 1 -TargetName "{host}" -ErrorAction SilentlyContinue; '
                    "if ($r) { [double]$r.Latency }"
                )
                ps_result = subprocess.run(
                    ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                ps_text = (ps_result.stdout or "").strip().replace(",", ".")
                if ps_text:
                    try:
                        return float(ps_text)
                    except Exception:
                        pass

            return None
        except Exception:
            return None

    def _get_ping_ms(self, target_hosts):
        try:
            if not target_hosts:
                return "-- ms"

            hosts = target_hosts if isinstance(target_hosts, (list, tuple, set)) else [target_hosts]
            best_latency = None
            for target in hosts:
                if isinstance(target, (list, tuple)) and len(target) >= 3:
                    host = target[0]
                    try:
                        start_port = int(target[1])
                        end_port = int(target[2])
                    except Exception:
                        start_port, end_port = 27015, 27060

                    if end_port < start_port:
                        start_port, end_port = end_port, start_port

                    span = max(0, end_port - start_port)
                    step = max(1, span // 3) if span else 1
                    _ = sorted({start_port, start_port + step, start_port + step * 2, end_port})
                else:
                    host = target

                latency = self._measure_host_latency_ms(host)
                if latency is None:
                    continue

                if best_latency is None or latency < best_latency:
                    best_latency = latency

            if best_latency is None:
                return "-- ms"

            return f"{int(round(best_latency))} ms"
        except Exception:
            return "-- ms"

    def _collect_region_pings(self):
        ping_map = {}
        for region in self.srt_rows.keys():
            targets = REGION_PING_TARGETS.get(region) or self.sdr_regions.get(region, [])
            current_ping = self._get_ping_ms(targets)
            if current_ping != "-- ms":
                self.region_ping_cache[region] = current_ping
            ping_map[region] = self.region_ping_cache.get(region, current_ping)
        return ping_map

    def _schedule_ping_refresh(self):
        def refresh_once():
            try:
                if self.ping_refresh_in_flight:
                    return
                self.ping_refresh_in_flight = True

                def done_callback(future):
                    self.ping_refresh_in_flight = False
                    try:
                        ping_map = future.result()
                        for region, row in self.srt_rows.items():
                            row["ping"].configure(text=ping_map.get(region, "-- ms"))
                    except Exception:
                        pass

                self._run_action_async(self._collect_region_pings, done_callback)
            except Exception:
                self.ping_refresh_in_flight = False

        self.after(500, refresh_once)

    # ---------------- Navigation ----------------
    def _apply_section_switch(self, section_key):
        target_section = section_key if section_key in self.sections else "functional"
        if self._active_section == target_section:
            self._pending_section = None
            self._section_switch_job = None
            return

        target_frame = self.sections[target_section]
        target_frame.tkraise()
        self._active_section = target_section

        for key, button in self.nav_buttons.items():
            is_target = (key == target_section)
            button.configure(fg_color=BG_CARD if is_target else BG_CARD_ALT, border_color=ACCENT_GREEN if is_target else ACCENT_RED)
        self._pending_section = None
        self._section_switch_job = None

        
    def show_section(self, section_key):
        if section_key not in self.sections:
            section_key = "functional"


        for k, button in self.nav_buttons.items():
            is_selected = (k == section_key)


            button.configure(
                state="normal",
                fg_color=BG_CARD if is_selected else BG_CARD_ALT,
                border_color=ACCENT_GREEN if is_selected else ACCENT_RED,
            )

        if self._active_section == section_key:
            self._pending_section = None
            return

        self._pending_section = section_key
        
        if self._section_switch_job is not None:
            try:
                self.after_cancel(self._section_switch_job)
            except Exception:
                pass

        self._section_switch_job = self.after(16, lambda: self._apply_section_switch(self._pending_section))

    # ---------------- Misc ----------------
    def _log_startup_gpu_info(self, startup_gpu_info):
        if not startup_gpu_info:
            return
        vendor_id, device_id, source = startup_gpu_info
        source_label = "detected" if source == "detected" else "settings fallback"
        try:
            self.log_manager.add_log(f"🎮 GPU ({source_label}): VendorID={vendor_id}, DeviceID={device_id}")
        except Exception:
            pass

    def _connect_gsi_to_ui(self):
        try:
            if self.gsi_manager:
                self.gsi_manager.set_accounts_list_frame(self)
                print("✅ 🎮 GSIManager подключен к App data API!")
            else:
                print("⚠️ GSIManager недоступен")
        except Exception as exc:
            print(f"❌ Ошибка подключения GSIManager: {exc}")

    # ---------------- Telegram bot ----------------
    def _try_start_telegram_bot_from_settings(self):
        token = (self.settings_manager.get("TelegramBotToken", "") or "").strip()
        self.telegram_bot_create_button.grid(row=2, column=0, padx=(10, 6), pady=(0, 8), sticky="w")
        self.telegram_bot_remove_button.grid(row=2, column=1, padx=(0, 10), pady=(0, 8), sticky="w")
        if self.telegram_bot_set_proxies_button:
            self.telegram_bot_set_proxies_button.grid(row=2, column=2, padx=(0, 10), pady=(0, 8), sticky="w")
        if not token:
            return
        self._start_telegram_bot(token)
        self._refresh_telegram_bot_block()
    def _get_telegram_proxy_pool(self):
        raw = self.settings_manager.get("TelegramBotProxies", [])
        if isinstance(raw, str):
            return self._parse_proxy_pool(raw)
        if isinstance(raw, (list, tuple)):
            parsed = []
            for value in raw:
                normalized = self._normalize_proxy_url(value)
                if normalized:
                    parsed.append(normalized)
            return parsed
        return []

    @staticmethod
    def _split_proxy_scheme(proxy_value):
        value = (proxy_value or "").strip()
        if not value:
            return "", ""
        if "://" in value:
            scheme, body = value.split("://", 1)
            return (scheme or "http").strip().lower(), body.strip()
        return "http", value

    @staticmethod
    def _parse_host_port(host_port):
        value = (host_port or "").strip()
        if not value or ":" not in value:
            return None
        host, port_raw = value.rsplit(":", 1)
        host = host.strip()
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1].strip()
        if not host:
            return None
        if not port_raw.isdigit():
            return None
        port = int(port_raw)
        if port < 1 or port > 65535:
            return None
        return host, str(port)

    @staticmethod
    def _parse_user_pass(user_pass):
        value = (user_pass or "").strip()
        if not value or ":" not in value:
            return None
        user, password = value.split(":", 1)
        user = user.strip()
        password = password.strip()
        if not user or not password:
            return None
        return user, password

    def _normalize_proxy_url(self, proxy_value):
        scheme, body = self._split_proxy_scheme(proxy_value)
        if not body:
            return ""

        host = ""
        port = ""
        user = ""
        password = ""

        if "@" in body:
            left, right = body.rsplit("@", 1)
            right_host_port = self._parse_host_port(right)
            left_host_port = self._parse_host_port(left)
            if right_host_port:
                creds = self._parse_user_pass(left)
                if not creds:
                    return ""
                host, port = right_host_port
                user, password = creds
            elif left_host_port:
                creds = self._parse_user_pass(right)
                if not creds:
                    return ""
                host, port = left_host_port
                user, password = creds
            else:
                return ""
        else:
            parts = body.split(":")
            if len(parts) == 2:
                host_port = self._parse_host_port(body)
                if not host_port:
                    return ""
                host, port = host_port
            elif len(parts) >= 4:
                host = parts[0].strip()
                port_raw = parts[1].strip()
                user = parts[2].strip()
                password = ":".join(parts[3:]).strip()
                if not host or not port_raw.isdigit() or not user or not password:
                    return ""
                port_int = int(port_raw)
                if port_int < 1 or port_int > 65535:
                    return ""
                port = str(port_int)
            else:
                return ""

        if not host or not port:
            return ""
        if user and password:
            return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
        return f"{scheme}://{host}:{port}"

    def _parse_proxy_pool(self, raw_text):
        values = []
        for raw_item in re.split(r"[\n,;]+", raw_text or ""):
            normalized = self._normalize_proxy_url(raw_item)
            if normalized:
                values.append(normalized)
        return values
    @staticmethod
    def _mask_telegram_token(token):
        return (token or "")[:10]

    def _fetch_telegram_bot_name(self, token):
        safe_token = (token or "").strip()
        if not safe_token:
            return ""

        try:
            response = requests.get(
                f"https://api.telegram.org/bot{safe_token}/getMe",
                timeout=6,
            )
            response.raise_for_status()
            payload = response.json()
            if not payload.get("ok"):
                return ""
            result = payload.get("result") or {}
            return (result.get("username") or result.get("first_name") or "").strip()
        except Exception:
            return ""

    def _refresh_telegram_bot_block(self):
        if not self.telegram_bot_status_label or not self.telegram_bot_create_button or not self.telegram_bot_remove_button:
            return

        token = (self.settings_manager.get("TelegramBotToken", "") or "").strip()
        if not token:
            self.telegram_bot_status_label.configure(text="Telegram bot: not configured | Proxies: off")
            self.telegram_bot_remove_button.configure(state="disabled")
            if self.telegram_bot_set_proxies_button:
                self.telegram_bot_set_proxies_button.configure(state="normal")
            return

        bot_name = self._fetch_telegram_bot_name(token)
        if bot_name:
            status_text = f"Name bot - {bot_name}"
        else:
            status_text = f"Bot API - {self._mask_telegram_token(token)}"

        proxies_count = len(self._get_telegram_proxy_pool())
        proxy_text = f" | Proxies: {proxies_count}" if proxies_count else " | Proxies: off"
        self.telegram_bot_status_label.configure(text=f"{status_text}{proxy_text}")
        self.telegram_bot_remove_button.configure(state="normal")
        if self.telegram_bot_set_proxies_button:
            self.telegram_bot_set_proxies_button.configure(state="normal")

    def _open_telegram_proxies_dialog(self):
        popup = customtkinter.CTkToplevel(self)
        popup.title("Set proxies")
        popup.geometry("640x390")
        popup.resizable(False, False)
        popup.transient(self)
        popup.grab_set()

        customtkinter.CTkLabel(
            popup,
            text="Прокси для Telegram бота (по одному в строке):",
            anchor="w",
            justify="left",
            font=customtkinter.CTkFont(size=13, weight="bold"),
        ).pack(fill="x", padx=14, pady=(14, 8))
        customtkinter.CTkLabel(
            popup,
            text=(
                "Поддерживаются: host:port, http://host:port, socks5://host:port,\n"
                "user:pass@host:port, host:port@user:pass, host:port:user:pass"
            ),
            anchor="w",
            justify="left",
            text_color=TXT_SOFT,
            font=customtkinter.CTkFont(size=11),
        ).pack(fill="x", padx=14, pady=(0, 8))

        textbox = customtkinter.CTkTextbox(popup, corner_radius=8, width=610, height=220)
        textbox.pack(fill="both", expand=True, padx=14, pady=(0, 10))

        current_proxy_pool = self._get_telegram_proxy_pool()
        if current_proxy_pool:
            textbox.insert("1.0", "\n".join(current_proxy_pool))

        buttons = customtkinter.CTkFrame(popup, fg_color="transparent")
        buttons.pack(fill="x", padx=14, pady=(0, 14))

        def handle_save():
            if getattr(popup, "_save_in_progress", False):
                return

            popup._save_in_progress = True
            save_button.configure(state="disabled", text="Saving...")

            raw_value = (textbox.get("1.0", "end") or "").strip()
            def save_worker():
                parsed_pool = self._parse_proxy_pool(raw_value)
                self.settings_manager.set("TelegramBotProxies", parsed_pool)

                if self.telegram_bot_manager:
                    try:
                        self.telegram_bot_manager.update_proxy_pool(parsed_pool)
                    except Exception as exc:
                        self._queue_ui_action(lambda: self.log_manager.add_log(f"⚠️ Не удалось применить прокси без перезапуска: {exc}"))

                def finish_ui():
                    self.log_manager.add_log(f"🌐 Telegram proxies updated: {len(parsed_pool)} шт.")
                    self._refresh_telegram_bot_block()
                    if popup.winfo_exists():
                        popup.destroy()

                self._queue_ui_action(finish_ui)

            self._run_action_async(save_worker)

        customtkinter.CTkButton(buttons, text="Cancel", fg_color=BG_CARD_ALT, hover_color=BG_BORDER, width=110, command=popup.destroy).pack(side="right")
        save_button = customtkinter.CTkButton(
            buttons,
            text="Save",
            fg_color=ACCENT_BLUE,
            hover_color=ACCENT_BLUE_DARK,
            width=110,
            command=handle_save,
        )
        save_button.pack(side="right", padx=(0, 8))

    def _remove_telegram_bot(self):
        try:
            if self.telegram_bot_manager:
                self.telegram_bot_manager.stop()
                self.telegram_bot_manager = None
        except Exception:
            pass

        self.settings_manager.set("TelegramBotToken", "")
        self.log_manager.add_log("🗑 Telegram bot удален из панели")
        self._refresh_telegram_bot_block()

    def _open_create_telegram_bot_dialog(self):
        dialog = customtkinter.CTkInputDialog(
            text="Введите токен Telegram-бота (BotFather):",
            title="Create telegram bot",
        )
        token = (dialog.get_input() or "").strip()
        if not token:
            self.log_manager.add_log("⚠️ Telegram token не введен")
            return

        self.settings_manager.set("TelegramBotToken", token)
        self._start_telegram_bot(token)

    def _start_telegram_bot(self, token):
        handlers = {
            "get_accounts": self._telegram_get_accounts,
            "toggle_account": self._telegram_toggle_account,
            "launch_selected": self._action_start_selected,
            "select4": self._action_select_first_4,
            "killall": self._action_kill_all_cs_and_steam,
            "make_lobbies_search": self.trigger_make_lobbies_and_search_button,
            "get_launched_levels": self._telegram_get_launched_levels,
            "get_config": self._telegram_get_config,
            "set_config": self._telegram_set_config,
            "get_proxy_status": self._telegram_get_proxy_status,
        }

        try:
            if self.telegram_bot_manager:
                self.telegram_bot_manager.stop()

            proxy_pool = self._get_telegram_proxy_pool()
            self.telegram_bot_manager = TelegramBotManager(
                token=token,
                handlers=handlers,
                log_callback=self.log_manager.add_log,
                proxy_pool=proxy_pool,
            )
            if not self.telegram_bot_manager.start():
                self.log_manager.add_log("❌ Не удалось запустить Telegram bot")
        except Exception as exc:
            self.log_manager.add_log(f"❌ Ошибка запуска Telegram bot: {exc}")
        finally:
            self._refresh_telegram_bot_block()
    def _telegram_get_accounts(self):
        levels = self.levels_cache or {}
        levels_lower = {str(k).lower(): v for k, v in levels.items()}

        result = []
        for index, account in enumerate(self.account_manager.accounts):
            lvl_data = levels.get(account.login, levels_lower.get(account.login.lower(), {}))
            level_text = lvl_data.get("level", "-")
            xp_text = lvl_data.get("xp", "-")
            is_farmed = self.is_farmed_account(account)
            farm_status = "Farmed" if is_farmed else "Unfarmed"
            result.append(
                {
                    "index": index,
                    "login": account.login,
                    "selected": account in self.account_manager.selected_accounts,
                    "status": "🟢" if account.isCSValid() else "⚪",
                    "state": farm_status,
                    "lvlxp": f"lvl: {level_text} | xp: {xp_text}",
                }
            )
        return result

    def _telegram_toggle_account(self, account_index):
        if account_index < 0 or account_index >= len(self.account_manager.accounts):
            return
        account = self.account_manager.accounts[account_index]
        self._toggle_account(account)
        self._queue_ui_action(self._safe_ui_refresh)

    def _telegram_get_launched_levels(self):
        launched = [acc for acc in self.account_manager.accounts if acc.isCSValid()]
        if not launched:
            return "No launched accounts"

        levels = self.levels_cache or {}
        levels_lower = {str(k).lower(): v for k, v in levels.items()}

        lines = []
        for account in launched:
            lvl_data = levels.get(account.login, levels_lower.get(account.login.lower(), {}))
            level_text = lvl_data.get("level", "-")
            xp_text = lvl_data.get("xp", "-")
            lines.append(f"{account.login}: lvl {level_text} | xp {xp_text}")
        return "\n".join(lines)

    def _telegram_get_config(self):
        keys = ["AutoAcceptEnabled", "AutoMatchInStartEnabled", "AutomaticAccountSwitchingEnabled"]
        return {key: bool(self.settings_manager.get(key, False)) for key in keys}

    def _telegram_set_config(self, key, enabled):
        if key not in {"AutoAcceptEnabled", "AutoMatchInStartEnabled", "AutomaticAccountSwitchingEnabled"}:
            return
        self.settings_manager.set(key, bool(enabled))
        if key == "AutoAcceptEnabled":
            self._on_auto_accept_toggle(bool(enabled))

        def sync_switch():
            mapping = {
                "AutoAcceptEnabled": getattr(self, "config_toggle_auto_accept", None),
                "AutoMatchInStartEnabled": getattr(self, "config_toggle_auto_match", None),
                "AutomaticAccountSwitchingEnabled": getattr(self, "config_toggle_auto_account_switching", None),
            }
            switch = mapping.get(key)
            if not switch:
                return
            if enabled:
                switch.select()
            else:
                switch.deselect()

        self._queue_ui_action(sync_switch)
        self.log_manager.add_log(f"🤖 Telegram config updated: {key}={bool(enabled)}")

    def _telegram_get_proxy_status(self):
        proxies = self._get_telegram_proxy_pool()
        if not proxies:
            return "off"
        preview = ", ".join(proxies[:2])
        if len(proxies) > 2:
            preview = f"{preview}, ..."
        return f"{len(proxies)} | {preview}"
    def _load_window_position(self):
        try:
            if not self.window_position_file.exists():
                return
            raw = self.window_position_file.read_text(encoding="utf-8").strip()
            if not raw:
                return
            parts = raw.split(",")
            if len(parts) != 2:
                return
            x = int(parts[0].strip())
            y = int(parts[1].strip())
            self.geometry(f"1100x600+{x}+{y}")
        except Exception:
            pass

    def _save_window_position(self):
        try:
            x = self.winfo_x()
            y = self.winfo_y()
            self.window_position_file.write_text(f"{x},{y}", encoding="utf-8")
        except Exception:
            pass

    def on_closing(self):
        if self._section_switch_job is not None:
            try:
                self.after_cancel(self._section_switch_job)
            except Exception:
                pass
        try:
            self._save_window_position()
        except Exception:
            pass
        try:
            if self.telegram_bot_manager:
                self.telegram_bot_manager.stop()
        except Exception:
            pass
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        try:
            self.quit()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        os._exit(0)

    def update_label(self):
        self._update_accounts_info()
        self._sync_switches_with_selection()
        self._apply_account_filter()


if __name__ == "__main__":
    app = App()
    app.mainloop()
    