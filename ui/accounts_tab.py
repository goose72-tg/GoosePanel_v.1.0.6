import os
import re
import shutil
import threading
import subprocess
import customtkinter
import time
import keyboard
import webbrowser

from Helpers.LoginExecutor import SteamLoginSession
from Managers.AccountsManager import AccountManager
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager


class AccountsControl(customtkinter.CTkTabview):
    def __init__(self, parent, update_label, accounts_list):
        super().__init__(parent, width=250)
        self._active_stat_threads = 0
        self._stat_lock = threading.Lock()
        self._start_sequence_lock = threading.Lock()
        self._start_sequence_active = False
        self._ctrlq_hotkey_handle = None
        self._settingsManager = SettingsManager()
        self._logManager = LogManager()
        self.accountsManager = AccountManager()
        self.update_label = update_label
        self.accounts_list = accounts_list
        self.stat_buttons = []
        self.grid(row=1, column=2, padx=(20, 0), pady=(0, 0), sticky="nsew")

        # Вкладки
        self.add("Accounts Control")
        self.tab("Accounts Control").grid_columnconfigure(0, weight=1)

        self.add("Account Stats")
        self.tab("Account Stats").grid_columnconfigure(0, weight=1)

        self.create_control_buttons()
        self.create_stat_buttons()
        
        self.accounts_list.set_control_frame(self)
        self.booster_processes = {}

    def _restore_account_color(self, account):
        if self.accounts_list and self.accounts_list.is_farmed_account(account):
            account.setColor("#ff9500")
        elif self.accounts_list and self.accounts_list.is_drop_ready_account(account):
            account.setColor("#a855f7")
        else:
            account.setColor("#DCE4EE")

    def _get_account_booster_config(self, login):
        account_configs = self._settingsManager.get("ActivityBoosterAccounts", {}) or {}
        if not isinstance(account_configs, dict):
            account_configs = {}
        cfg = account_configs.get(login, {})
        return cfg if isinstance(cfg, dict) else {}

    def _resolve_booster_settings(self, account):
        account_cfg = self._get_account_booster_config(account.login)

        min_minutes = int(account_cfg.get("min_minutes", self._settingsManager.get("ActivityBoosterMinMinutes", 60)) or 60)
        max_minutes = int(account_cfg.get("max_minutes", self._settingsManager.get("ActivityBoosterMaxMinutes", 100)) or 100)
        if min_minutes <= 0:
            min_minutes = 60
        if max_minutes < min_minutes:
            max_minutes = min_minutes

        game_appids = account_cfg.get("game_appids")
        if game_appids is None:
            game_appids = self._settingsManager.get("ActivityBoosterGameAppIds", []) or []
        if not isinstance(game_appids, list):
            game_appids = []

        parsed_game_appids = []
        seen = set()
        for app_id in game_appids:
            app_text = str(app_id).strip()
            if not app_text.isdigit():
                continue
            app_id_int = int(app_text)
            if app_id_int <= 0 or app_id_int in seen:
                continue
            parsed_game_appids.append(app_id_int)
            seen.add(app_id_int)
            if len(parsed_game_appids) >= 5:
                break

        return min_minutes, max_minutes, parsed_game_appids

    # ----------------- Вкладка Accounts Control -----------------
    def create_control_buttons(self):
        buttons = [
            ("Start selected accounts", "darkgreen", self.start_selected),
            ("Kill selected accounts", "red", self.kill_selected),
            ("Select first 4 accounts", None, self.select_first_4),
            ("Select all accounts", None, self.select_unselect_all_accounts),
            ("Select dedicated farmed", "orange", self.mark_farmed),  # Toggle кнопка
        ]
        for i, (text, color, cmd) in enumerate(buttons):
            b = customtkinter.CTkButton(self.tab("Accounts Control"), text=text, fg_color=color, command=cmd)
            b.grid(row=i, column=0, padx=20, pady=10)

    def mark_farmed(self):
        """🟠 Toggle: отмечает/снимает отфармленные аккаунты"""
        if self.accounts_list:
            selected_accounts = self.accountsManager.selected_accounts.copy()
            if not selected_accounts:
                print("⚠️ Нет выделенных аккаунтов!")
                return
            


            for account in selected_accounts:
                login = account.login
                if self.accounts_list.is_farmed_account(account):
                    account.setColor("#DCE4EE")
                    self.accounts_list.farmed_accounts.discard(login)
                    self.accounts_list.set_drop_ready(login, value=False)
                    print(f"⚪ [{login}] Оранжевый -> белый")
                elif self.accounts_list.is_drop_ready_account(account):
                    account.setColor("#ff9500")
                    self.accounts_list.farmed_accounts.add(login)
                    self.accounts_list.set_drop_ready(login, value=False)
                    print(f"🟠 [{login}] Фиолетовый -> оранжевый")
                else:
                    account.setColor("#ff9500")
                    self.accounts_list.farmed_accounts.add(login)
                    print(f"🟠 [{login}] Белый -> оранжевый")

            self.accounts_list._save_farmed_accounts()
        self.accountsManager.selected_accounts.clear()
        self.update_label()

    def stop_booster_selected(self):
        selected_accounts = self.accountsManager.selected_accounts.copy()
        if not selected_accounts:
            self._logManager.add_log("⚠️ Нет выделенных аккаунтов для остановки booster")
            return

        stopped = 0
        for acc in selected_accounts:
            booster_proc = self.booster_processes.get(acc.login)
            if not booster_proc or booster_proc.poll() is not None:
                self.booster_processes.pop(acc.login, None)
                self._restore_account_color(acc)
                self._logManager.add_log(f"⚠️ [{acc.login}] booster не запущен")
                continue

            try:
                booster_proc.kill()
                stopped += 1
                self._restore_account_color(acc)
                self._logManager.add_log(f"🛑 [{acc.login}] Activity booster остановлен")
            except Exception as exc:
                self._logManager.add_log(f"❌ [{acc.login}] Ошибка остановки booster: {exc}")
            finally:
                self.booster_processes.pop(acc.login, None)

        if stopped > 0:
            self._logManager.add_log(f"🛑 Stop booster: остановлено {stopped} аккаунтов")

        self.accountsManager.selected_accounts.clear()
        self.update_label()

    def stop_all_boosters(self):
        if not self.booster_processes:
            return

        for login, booster_proc in list(self.booster_processes.items()):
            try:
                if booster_proc and booster_proc.poll() is None:
                    booster_proc.kill()
                    self._logManager.add_log(f"🛑 [{login}] Booster остановлен при закрытии панели")
            except Exception:
                pass
            finally:
                acc = self.accountsManager.get_account(login)
                if acc:
                    self._restore_account_color(acc)
                self.booster_processes.pop(login, None)
        else:
            print("⚠️ Нет ссылки на accounts_list")

    def _unmark_farmed_accounts(self, accounts):
        """🔄 Снимает отметку отфармленных аккаунтов"""
        print("🔄 Снимаем отметку отфармленных аккаунтов...")
        unmarked_count = 0
        
        for account in accounts:
            login = account.login
            if self.accounts_list.is_farmed_account(account):
                # 🟠 → ⚪ Оранжвый → белый
                account.setColor("#DCE4EE")
                # Удаляем из списка отфармленных
                self.accounts_list.farmed_accounts.discard(login)
                self.accounts_list._save_farmed_accounts()
                print(f" [{login}] Снято отфармлено (оранжевый → белый)")
                unmarked_count += 1
            else:
                print(f"⚪ [{login}] Уже не отфармленный")
        
        print(f" Снято отфармлено с {unmarked_count} аккаунтов")
        
        # Очищаем выделение
        self.accountsManager.selected_accounts.clear()
        self.update_label()

    def create_stat_buttons(self):
        buttons = [
            ("Get level", None, self.try_get_level),
            ("Get wingman Rank", None, self.try_get_wingmanRank),
            ("Get MM Ranks", None, self.try_get_mapStats),
            ("Get premier Rank", None, self.try_get_premierRank),
            ("Get all in html", None, self.save_stats_to_html),
        ]
        for i, (text, color, cmd) in enumerate(buttons):
            b = customtkinter.CTkButton(self.tab("Account Stats"), text=text, fg_color=color,
                                        command=lambda c=cmd: self._run_stat_with_lock(c))
            b.grid(row=i, column=0, padx=20, pady=10)
            self.stat_buttons.append(b)

    def _disable_stat_buttons(self):
        for b in self.stat_buttons:
            b.configure(state="disabled")

    def _enable_stat_buttons(self):
        for b in self.stat_buttons:
            b.configure(state="normal")

    def _run_stat_with_lock(self, func):
        def wrapper():
            with self._stat_lock:
                self._active_stat_threads += 1
                if self._active_stat_threads == 1:
                    self._disable_stat_buttons()
            try:
                func()
            finally:
                with self._stat_lock:
                    self._active_stat_threads -= 1
                    if self._active_stat_threads == 0:
                        self._enable_stat_buttons()

        self._run_in_thread(wrapper)

    def start_selected(self):
        with self._start_sequence_lock:
            if self._start_sequence_active:
                self._logManager.add_log("⚠️ Start уже выполняется, дождитесь завершения")
                return
            self._start_sequence_active = True

        steam_path = self._settingsManager.get(
            "SteamPath", r"C:\Program Files (x86)\Steam\steam.exe"
        )
        cs2_path = self._settingsManager.get(
            "CS2Path", r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive"
        )
        cs2_exe_path = os.path.join(cs2_path, r"game\bin\win64\cs2.exe")

        if not os.path.isfile(steam_path) or not steam_path.lower().endswith(".exe"):
            self._logManager.add_log(f"❌ Некорректный SteamPath: {steam_path}")
            self._finish_start_sequence()
            return

        if not os.path.isfile(cs2_exe_path):
            self._logManager.add_log(f"❌ Не найден CS2 exe: {cs2_exe_path}")
            self._finish_start_sequence()
            return

        if not self._sync_required_cfg_files_to_cs2(cs2_path):
            self._logManager.add_log("❌ Не удалось скопировать cfg-файлы в папку CS2")
            self._finish_start_sequence()
            return

        accounts_to_start = self.accountsManager.selected_accounts.copy()
        if not accounts_to_start:
            self._logManager.add_log("⚠️ Нет выделенных аккаунтов для запуска")
            self._finish_start_sequence()
            return

        self._logManager.add_log(f"🚀 Launch selected: {len(accounts_to_start)} аккаунтов")

        self.auto_cancelled = False
        self.auto_cancelled_by_user = False

        self.accountsManager.begin_start_selected_batch(len(accounts_to_start))
        queued_accounts = []

        for acc in accounts_to_start:
            if self.accountsManager.add_to_start_queue(acc):
                queued_accounts.append(acc)
                print("Starting:", acc.login)
            else:
                self.accountsManager.skip_batch_item()

        if not queued_accounts:
            self._finish_start_sequence()
            return

        self.accountsManager.selected_accounts.clear()
        self.update_label()

        threading.Thread(
            target=lambda: self._auto_get_level(queued_accounts),
            daemon=True
        ).start()

        self._register_ctrlq_hotkey()

        def check_cancellation_loop():
            while True:
                if self.auto_cancelled:
                    break
                if self.accountsManager.is_batch_start_finished():
                    break
                time.sleep(0.5)

            self._unregister_ctrlq_hotkey()
            self._finish_start_sequence()

        threading.Thread(target=check_cancellation_loop, daemon=True).start()

        if self.auto_cancelled:
            self._finish_start_sequence()
            return

        try:
            app = self.winfo_toplevel()

            if hasattr(app, "control_frame"):

                def on_move_complete():
                    if self.auto_cancelled:
                        self._finish_start_sequence()
                        return

                    auto_match_enabled = bool(
                        self._settingsManager.get("AutoMatchInStartEnabled", True)
                    )

                    if not auto_match_enabled:
                        self._finish_start_sequence()
                        return

                    def schedule_lobbies():
                        try:
                            current_app = self.winfo_toplevel()
                            if hasattr(current_app, "main_menu"):
                                current_app.main_menu.make_lobbies_and_search_game()
                        finally:
                            self._finish_start_sequence()

                    def delay_and_schedule():
                        delay_seconds = 10
                        step = 0.5
                        waited = 0.0

                        while waited < delay_seconds:
                            if self.auto_cancelled:
                                self._finish_start_sequence()
                                return
                            time.sleep(step)
                            waited += step

                        self.after(0, schedule_lobbies)

                    threading.Thread(
                        target=delay_and_schedule,
                        daemon=True
                    ).start()

                if not self.auto_cancelled:
                    started = app.control_frame.auto_move_after_4_cs2(
                        delay=25,
                        callback=on_move_complete,
                        cancel_check=lambda: self.auto_cancelled
                    )

                    if not started:
                        self._finish_start_sequence()
                else:
                    self._finish_start_sequence()

            else:
                self._finish_start_sequence()

        except Exception:
            self._finish_start_sequence()

    def _finish_start_sequence(self):
        with self._start_sequence_lock:
            self._start_sequence_active = False

    def _global_ctrlq_callback(self):
        """🔥 Глобальный Ctrl+Q обработчик"""
        self.auto_cancelled = True
        self.auto_cancelled_by_user = True

    def _register_ctrlq_hotkey(self):
        self._unregister_ctrlq_hotkey()
        self._ctrlq_hotkey_handle = keyboard.add_hotkey('ctrl+q', self._global_ctrlq_callback)

    def _unregister_ctrlq_hotkey(self):
        if self._ctrlq_hotkey_handle is None:
            return
        try:
            keyboard.remove_hotkey(self._ctrlq_hotkey_handle)
        except KeyError:
            pass
        finally:
            self._ctrlq_hotkey_handle = None

    def _auto_get_level(self, accounts):
        time.sleep(2)
        self._logManager.add_log("🔄 Авто Get Level для запущенных аккаунтов...")
        self.try_get_level_for_accounts(accounts)

    def launch_steam_selected(self):
        with self._start_sequence_lock:
            if self._start_sequence_active:
                self._logManager.add_log("⚠️ Другой запуск уже выполняется, дождитесь завершения")
                return
            self._start_sequence_active = True

        steam_path = self._settingsManager.get(
            "SteamPath", r"C:\Program Files (x86)\Steam\steam.exe"
        )
        if not os.path.isfile(steam_path) or not steam_path.lower().endswith(".exe"):
            self._logManager.add_log(f"❌ Некорректный SteamPath: {steam_path}")
            self._finish_start_sequence()
            return

        accounts_to_start = self.accountsManager.selected_accounts.copy()
        if not accounts_to_start:
            self._logManager.add_log("⚠️ Нет выделенных аккаунтов для Steam-only запуска")
            self._finish_start_sequence()
            return

        self._logManager.add_log(
            f"🚀 Launch Steam only: {len(accounts_to_start)} аккаунтов"
        )

        try:
            for acc in accounts_to_start:
                try:
                    acc.setColor("yellow")
                    acc.StartSteamOnly()
                    self._logManager.add_log(
                        f"✅ [{acc.login}] Steam запущен без запуска CS2"
                    )
                except Exception as error:
                    acc.setColor("#DCE4EE")
                    self._logManager.add_log(
                        f"❌ [{acc.login}] Steam-only запуск не удался: {error}"
                    )
                time.sleep(1.5)
        finally:
            self.accountsManager.selected_accounts.clear()
            self.update_label()
            self._finish_start_sequence()

    def open_steam_profile(self, login):
        account = self.accountsManager.get_account(login)
        if not account:
            self._logManager.add_log(f"⚠️ [{login}] Аккаунт не найден")
            return

        steam_id = str(getattr(account, "steam_id", "") or "").strip()
        if not steam_id or steam_id == "0":
            self._logManager.add_log(f"⚠️ [{login}] В mafile отсутствует SteamID")
            return

        profile_url = f"https://steamcommunity.com/profiles/{steam_id}"
        try:
            webbrowser.open(profile_url)
            self._logManager.add_log(f"🔗 [{login}] Открыт профиль Steam")
        except Exception as exc:
            self._logManager.add_log(f"❌ [{login}] Не удалось открыть профиль: {exc}")

    def start_booster_selected(self):
        selected_accounts = self.accountsManager.selected_accounts.copy()
        if not selected_accounts:
            self._logManager.add_log("⚠️ Нет выделенных аккаунтов для activity booster")
            return

        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        booster_script = os.path.join(project_root, "activity_booster.js")
        if not os.path.isfile(booster_script):
            self._logManager.add_log(f"❌ Файл activity_booster.js не найден: {booster_script}")
            return

        self._logManager.add_log(f"🎮 Start booster: {len(selected_accounts)} аккаунтов")

        for acc in selected_accounts:
            min_minutes, max_minutes, game_appids = self._resolve_booster_settings(acc)

            if not acc.shared_secret:
                self._logManager.add_log(f"⚠️ [{acc.login}] Нет shared_secret (mafile), пропускаю")
                continue

            steam_id = str(getattr(acc, "steam_id", "") or "").strip()
            if not steam_id or steam_id == "0":
                self._logManager.add_log(f"⚠️ [{acc.login}] Нет steamid в mafile, пропускаю")
                continue

            existing = self.booster_processes.get(acc.login)
            if existing and existing.poll() is None:
                self._logManager.add_log(f"⚠️ [{acc.login}] booster уже запущен")
                continue

            cmd = [
                "node",
                booster_script,
                acc.login,
                acc.password,
                acc.shared_secret,
                steam_id,
                str(min_minutes),
                str(max_minutes),
                ",".join(str(x) for x in game_appids),
            ]
            try:
                proc = subprocess.Popen(
                    cmd,
                    cwd=project_root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env={**os.environ, "NODE_NO_WARNINGS": "1"},
                )
                time.sleep(1)
                if proc.poll() is not None:
                    self._logManager.add_log(
                        f"❌ [{acc.login}] Activity booster завершился сразу после запуска. "
                        "Проверьте shared_secret/пароль и список appid."
                    )
                    continue
                self.booster_processes[acc.login] = proc
                acc.setColor("#4f8cff")
                games_info = ",".join(str(x) for x in game_appids) if game_appids else "random"
                self._logManager.add_log(
                    f"✅ [{acc.login}] Activity booster запущен ({min_minutes}-{max_minutes} мин., games: {games_info})"
                )
            except FileNotFoundError:
                self._logManager.add_log("❌ Не найден Node.js (команда node)")
                return
            except Exception as exc:
                self._logManager.add_log(f"❌ [{acc.login}] Ошибка запуска booster: {exc}")

        self.accountsManager.selected_accounts.clear()
        self.update_label()

    def _refresh_modern_levels_ui(self):
        """Обновляет уровни в новом UI (ui/app.py), если он доступен."""
        try:
            app = self.winfo_toplevel()
            if hasattr(app, "_refresh_level_labels"):
                app.after(0, app._refresh_level_labels)
        except Exception:
            pass
    def try_get_level_for_accounts(self, accounts):
        try:
            app = self.winfo_toplevel()
            if hasattr(app, "fetch_levels_for_accounts"):
                app.fetch_levels_for_accounts(accounts)
                return
            self._logManager.add_log("❌ App-level fetch_levels_for_accounts недоступен")
        except Exception as e:
            self._logManager.add_log(f"❌ Ошибка делегации get level: {e}")

    def try_get_level(self):
        try:
            app = self.winfo_toplevel()
            if hasattr(app, "fetch_levels_for_accounts"):
                
                selected_accounts = self.accountsManager.selected_accounts.copy()
                if not selected_accounts:
                    self._logManager.add_log("⚠️ Нет выделенных аккаунтов")
                    return
                app.fetch_levels_for_accounts(selected_accounts)
                return
            self._logManager.add_log("❌ App-level fetch_levels_for_accounts недоступен")
        except Exception as e:
            self._logManager.add_log(f"❌ Ошибка делегации get level: {e}")

    def kill_selected(self):
        print("💀 УБИВАЮ ВЫБРАННЫЕ аккаунты!")
        
        killed = 0
        for acc in self.accountsManager.selected_accounts[:]:
            try:
                if hasattr(acc, 'steamProcess') and acc.steamProcess:
                    try:
                        acc.steamProcess.kill()
                        print(f"💀 Steam [{acc.login}]: {acc.steamProcess.pid}")
                        killed += 1
                    except:
                        pass
                    acc.steamProcess = None
                    
                if hasattr(acc, 'CS2Process') and acc.CS2Process:
                    try:
                        acc.CS2Process.kill()
                        print(f"💀 CS2 [{acc.login}]: {acc.CS2Process.pid}")
                        killed += 1
                    except:
                        pass
                    acc.CS2Process = None

                booster_proc = self.booster_processes.get(acc.login)
                if booster_proc and booster_proc.poll() is None:
                    try:
                        booster_proc.kill()
                        killed += 1
                        print(f"💀 Booster [{acc.login}]: {booster_proc.pid}")
                    except Exception:
                        pass
                self.booster_processes.pop(acc.login, None)
                
                self._restore_account_color(acc)
                if self.accounts_list and self.accounts_list.is_farmed_account(acc):
                    print(f" [{acc.login}] Сброс - оранжевый цвет")
                elif self.accounts_list and self.accounts_list.is_drop_ready_account(acc):
                    print(f" [{acc.login}] Сброс - фиолетовый цвет")
                else:
                    print(f" [{acc.login}] Сброс - белый цвет")
                
            except Exception as e:
                print(f"⚠️ [{acc.login}] Ошибка: {e}")
        
        self.accountsManager.selected_accounts.clear()
        self.update_label()
        print(f" УБИТО {killed} процессов выбранных аккаунтов!")

    def select_first_4(self):
        if len(self.accountsManager.selected_accounts) < 4:
            if self.accounts_list:
                self.accounts_list.select_first_non_farmed(4)
            else:
                self._select_first_n(4)
        else:
            self.accountsManager.selected_accounts = []
            self.update_label()

    def select_unselect_all_accounts(self):
        all_accounts = self.accountsManager.accounts
        if not all_accounts:
            return

        if len(self.accountsManager.selected_accounts) == len(all_accounts):
            self.accountsManager.selected_accounts.clear()
        else:
            self.accountsManager.selected_accounts = list(all_accounts)

        self.update_label()
    def _select_first_n(self, n):
        for acc in self.accountsManager.accounts[:n]:
            if acc not in self.accountsManager.selected_accounts:
                self.accountsManager.selected_accounts.append(acc)
        self.update_label()

    def _resolve_cs2_cfg_folder(self, cs2_path):
        candidates = [
            os.path.join(cs2_path, "game", "csgo", "cfg"),
            os.path.join(cs2_path, "cfg"),
        ]
        for folder in candidates:
            if os.path.isdir(folder):
                return folder
        return None

    def _sync_required_cfg_files_to_cs2(self, cs2_path):
        cfg_folder = self._resolve_cs2_cfg_folder(cs2_path)
        if not cfg_folder:
            self._logManager.add_log("CS2 cfg folder not found")
            return False

        files_to_sync = [
            "cs2_machine_convars.vcfg",
            "cs2_video.txt",
            "cs2_video.txt.bak",
            "gamestate_integration_fsn.cfg",
            "fsn.cfg",
        ]

        for file_name in files_to_sync:
            source = os.path.join("settings", file_name)
            target = os.path.join(cfg_folder, file_name)

            if not os.path.isfile(source):
                self._logManager.add_log(f"Missing source file: {source}")
                return False

            try:
                shutil.copy2(source, target)
            except Exception as e:
                self._logManager.add_log(f"Failed to copy {file_name}: {e}")
                return False

        return True
        
    # ----------------- Helper Methods -----------------
    def _fetch_html(self, steam, url_suffix="gcpd/730/?tab=matchmaking"):
        try:
            steam.login()
        except Exception as e:
            self._logManager.add_log(f"[{steam.login}] ❌ Failed to login: {e}")
            return None
        try:
            resp = steam.session.get(f'https://steamcommunity.com/profiles/{steam.steamid}/{url_suffix}', timeout=10)
        except Exception as e:
            self._logManager.add_log(f"[{steam.login}] ❌ Failed to fetch page: {e}")
            return None
        if resp.status_code != 200:
            self._logManager.add_log(f"[{steam.login}] ❌ HTTP {resp.status_code}")
            return None
        return resp.text

    def _run_in_thread(self, func):
        thread = threading.Thread(target=func, daemon=True)
        thread.start()

    # ----------------- Stats Methods -----------------
    def try_get_premierRank(self):
        def worker():
            for acc in self.accountsManager.selected_accounts:
                steam = SteamLoginSession(acc.login, acc.password, acc.shared_secret)
                html = self._fetch_html(steam)
                if not html:
                    continue
                match = re.search(
                    r'<td>Premier</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td><td>([^<]*)</td>',
                    html
                )
                if match:
                    wins, ties, losses = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    skill = match.group(4).strip()
                    skill = int(skill) if skill.isdigit() else -1
                    self._logManager.add_log(f"[{acc.login}] Premier: W:{wins} T:{ties} L:{losses} R:{skill}")
                else:
                    self._logManager.add_log(f"[{acc.login}] ⚠ Premier stats not found")
        self._run_stat_with_lock(worker)

    def try_get_wingmanRank(self):
        def worker():
            for acc in self.accountsManager.selected_accounts:
                steam = SteamLoginSession(acc.login, acc.password, acc.shared_secret)
                html = self._fetch_html(steam)
                if not html:
                    continue
                match = re.search(
                    r'<td>Wingman</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td><td>([^<]*)</td>',
                    html
                )
                if match:
                    wins, ties, losses = int(match.group(1)), int(match.group(2)), int(match.group(3))
                    skill = match.group(4).strip()
                    skill = int(skill) if skill.isdigit() else -1
                    self._logManager.add_log(f"[{acc.login}] Wingman: W:{wins} T:{ties} L:{losses} R:{skill}")
                else:
                    self._logManager.add_log(f"[{acc.login}] ⚠ Wingman stats not found")
        self._run_stat_with_lock(worker)

    def try_get_mapStats(self):
        def worker():
            for acc in self.accountsManager.selected_accounts:
                steam = SteamLoginSession(acc.login, acc.password, acc.shared_secret)
                html = self._fetch_html(steam)
                if not html:
                    continue
                table_match = re.search(
                    r'<table class="generic_kv_table"><tr>\s*<th>Matchmaking Mode</th>\s*<th>Map</th>.*?</table>',
                    html, re.DOTALL
                )
                if not table_match:
                    self._logManager.add_log(f"[{acc.login}] ⚠ No map stats table found")
                    continue
                table_html = table_match.group(0)
                rows = re.findall(
                    r'<tr>\s*<td>([^<]+)</td><td>([^<]+)</td><td>(\d+)</td><td>(\d+)</td><td>(\d+)</td><td>([^<]*)</td>',
                    table_html
                )
                if rows:
                    for mode, map_name, wins, ties, losses, skill in rows:
                        wins, ties, losses = int(wins), int(ties), int(losses)
                        skill = skill.strip()
                        skill = int(skill) if skill.isdigit() else -1
                        self._logManager.add_log(
                            f"[{acc.login}] Map '{map_name}': W:{wins} T:{ties} L:{losses} R:{skill}"
                        )
        self._run_stat_with_lock(worker)

    def save_stats_to_html(self, filename="cs2_stats.html"):
        def worker():
            html_parts = [
                "<!DOCTYPE html><html><head><meta charset='UTF-8'><title>CS2 Stats</title>",
                "<style>body { background-color: #121212; color: #eee; font-family: 'Segoe UI', Tahoma, sans-serif; display: flex; flex-direction: column; align-items: center; padding: 20px; }",
                "h1 { color: #00bfff; margin-bottom: 30px; }.account-card { background: #1e1e1e; border-radius: 8px; padding: 15px; margin-bottom: 20px; width: 100%; max-width: 600px; box-shadow: 0 3px 8px rgba(0,0,0,0.5); }",
                ".account-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }.account-title { font-size: 1.3em; color: #ffcc00; }.account-level { font-size: 0.95em; color: #00ff90; }",
                "table { border-collapse: collapse; width: 100%; margin-bottom: 10px; font-size: 13px; } th, td { border: 1px solid #333; padding: 5px; text-align: center; } th { background-color: #222; color: #fff; }",
                "tr:nth-child(even) { background-color: #2a2a2a; } tr:hover { background-color: #333; }.wins { color: #00ff00; font-weight: bold; }.ties { color: #ffff66; font-weight: bold; }.losses { color: #ff5555; font-weight: bold; }",
                ".skill { color: #00bfff; font-weight: bold; }.missing { color: #ff5555; font-style: italic; font-size: 12px; }</style></head><body><h1>CS2 Account Stats</h1>"
            ]
            i = 1
            accounts = self.accountsManager.selected_accounts
            for acc in accounts:
                self._logManager.add_log(f"Collecting stats ({i}/{len(accounts)})")
                steam = SteamLoginSession(acc.login, acc.password, acc.shared_secret)
                level_html = self._fetch_html(steam, "gcpd/730")
                rank_match = re.search(r'CS:GO Profile Rank:\s*([^\n<]+)', level_html) if level_html else None
                xp_match = re.search(r'Experience points earned towards next rank:\s*([^\n<]+)', level_html) if level_html else None
                level = rank_match.group(1).strip() if rank_match else "N/A"
                xp = xp_match.group(1).strip() if xp_match else "N/A"
                stats_html = self._fetch_html(steam)
                html_parts.extend([
                    "<div class='account-card'>",
                    f"<div class='account-header'><div class='account-title'>{acc.login}</div><div class='account-level'>Level: {level} | XP: {xp}</div></div>"
                ])
                # Premier, Wingman, Map Stats (сокращено для компактности)
                html_parts.append("</div>")
                i += 1
            html_parts.extend(["</body></html>"])
            with open(filename, "w", encoding="utf-8") as f:
                f.write("\n".join(html_parts))
            self._logManager.add_log(f" Stats saved to {filename}")
        self._run_stat_with_lock(worker)

    def update_label(self):
        if hasattr(self.parent, 'update_label'):
            self.parent.update_label()
