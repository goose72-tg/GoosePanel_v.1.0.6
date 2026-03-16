import os
import shutil
import subprocess
import threading

import customtkinter

from Managers.AccountsManager import AccountManager
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager


class ConfigTab(customtkinter.CTkTabview):
    def __init__(self, parent):
        super().__init__(parent, width=250)
        self._settingsManager = SettingsManager()
        self._logManager = LogManager()
        self.accountsManager = AccountManager()

        self.grid(row=0, column=3, padx=(20, 20), pady=(0, 0), sticky="nsew")
        self.add("Config")
        self.tab("Config").grid_columnconfigure(0, weight=1)

        # --- Buttons for path selection ---
        b1 = customtkinter.CTkButton(
            self.tab("Config"),
            text="Select Steam path",
            command=lambda: self.set_path("SteamPath", "Steam", "C:/Program Files (x86)/Steam/steam.exe"),
        )
        b2 = customtkinter.CTkButton(
            self.tab("Config"),
            text="Select CS2 path",
            command=lambda: self.set_path(
                "CS2Path",
                "CS2",
                "C:/Program Files (x86)/Steam/steamapps/common/Counter-Strike Global Offensive",
            ),
        )
        b1.grid(row=0, column=0, padx=20, pady=10)
        b2.grid(row=1, column=0, padx=20, pady=10)

        # --- Switches ---
        self.bg_switch = customtkinter.CTkSwitch(
            self.tab("Config"),
            text="Remove background",
            command=lambda: self._settingsManager.set("RemoveBackground", self.bg_switch.get()),
        )
        self.bg_switch.grid(row=2, column=0, padx=10, pady=5)

        self.overlay_switch = customtkinter.CTkSwitch(
            self.tab("Config"),
            text="Disable Steam Overlay",
            command=lambda: self._settingsManager.set("DisableOverlay", self.overlay_switch.get()),
        )
        self.overlay_switch.grid(row=3, column=0, padx=10, pady=5)

        self.send_trade_button = customtkinter.CTkButton(
            self.tab("Config"),
            text="Send trade",
            fg_color="#ff1a1a",
            command=self.send_trade_selected,
        )
        self.send_trade_button.grid(row=4, column=0, padx=20, pady=(10, 5))

        self.settings_looter_button = customtkinter.CTkButton(
            self.tab("Config"),
            text="Settings looter",
            fg_color="#1b5e20",
            command=self.open_looter_settings,
        )
        self.settings_looter_button.grid(row=5, column=0, padx=20, pady=(5, 10))

        # --- Load saved values ---
        self.load_settings()

    def set_path(self, key, name, placeholder):
        """Opens a path input window and saves result in settingsManager."""
        value = self.open_path_window(name, placeholder)
        if value:
            self._settingsManager.set(key, value)

    def open_path_window(self, name, placeholder):
        """Opens a separate window for entering a path and returns the result."""
        result = {"value": None}

        win = customtkinter.CTkToplevel(self)
        win.title(f"Select {name} path")
        win.geometry("500x150")
        win.grab_set()

        label = customtkinter.CTkLabel(win, text=f"Enter {name} path:")
        label.pack(pady=(20, 5))

        entry = customtkinter.CTkEntry(win, placeholder_text=f"Example: {placeholder}", width=400)
        entry.pack(pady=5)

        def save_and_close():
            result["value"] = entry.get()
            win.destroy()

        btn = customtkinter.CTkButton(win, text="OK", command=save_and_close)
        btn.pack(pady=10)

        win.wait_window()
        return result["value"]

    def load_settings(self):
        """Load saved values from settingsManager and apply them."""
        bg_value = self._settingsManager.get("RemoveBackground", False)
        if bg_value is not None:
            self.bg_switch.select() if bg_value else self.bg_switch.deselect()

        overlay_value = self._settingsManager.get("DisableOverlay", False)
        if overlay_value is not None:
            self.overlay_switch.select() if overlay_value else self.overlay_switch.deselect()

        steam_path = self._settingsManager.get("SteamPath", "C:/Program Files (x86)/Steam/steam.exe")
        if steam_path:
            print(f"Loaded SteamPath: {steam_path}")

        cs2_path = self._settingsManager.get(
            "CS2Path", "C:/Program Files (x86)/Steam/steamapps/common/Counter-Strike Global Offensive"
        )
        if cs2_path:
            print(f"Loaded CS2Path: {cs2_path}")

    def _get_looter_script_path(self):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, "looter_core.js")

    def open_looter_settings(self):
        current_inventory = self._settingsManager.get("LooterInventory", "730/2")

        dialog = customtkinter.CTkInputDialog(
            text=(
                "Введите ссылку обмена Steam (trade offer link).\n"
                "Она будет использована кнопкой Send trade."
            ),
            title="Settings looter",
        )
        new_trade_link = dialog.get_input()

        if new_trade_link is None:
            self._logManager.add_log("⚠️ Настройки лутера не изменены")
            return

        new_trade_link = new_trade_link.strip()
        if not new_trade_link:
            self._logManager.add_log("❌ Пустая трейд ссылка. Настройки не сохранены")
            return

        inv_dialog = customtkinter.CTkInputDialog(
            text=(
                "Инвентари для отправки (например: 730/2 440/2 753/6).\n"
                "Разделители: пробел, запятая или ;\n"
                f"Текущее значение: {current_inventory}"
            ),
            title="Settings looter",
        )
        new_inventory = inv_dialog.get_input()

        if new_inventory is None:
            new_inventory = current_inventory
        else:
            new_inventory = new_inventory.strip() or "730/2"

        new_inventory = self._normalize_inventory_string(new_inventory)
        if not new_inventory:
            self._logManager.add_log("❌ Инвентари указаны некорректно. Использую значение по умолчанию 730/2")
            new_inventory = "730/2"

        self._settingsManager.set("LooterTradeLink", new_trade_link)
        self._settingsManager.set("LooterInventory", new_inventory)
        self._logManager.add_log("✅ Settings looter сохранены")

    def send_trade_selected(self, on_trade_sent=None):
        selected_accounts = self.accountsManager.selected_accounts.copy()
        if not selected_accounts:
            self._logManager.add_log("⚠️ Выберите аккаунты для отправки трейда")
            return

        trade_link = (self._settingsManager.get("LooterTradeLink", "") or "").strip()
        if not trade_link:
            self._logManager.add_log("❌ Сначала заполните trade link в Settings looter")
            return

        script_path = self._get_looter_script_path()
        if not os.path.isfile(script_path):
            self._logManager.add_log(f"❌ Файл looter_core.js не найден: {script_path}")
            return

        inventory_string = (self._settingsManager.get("LooterInventory", "730/2") or "730/2").strip() or "730/2"
        inventory_string = self._normalize_inventory_string(inventory_string)
        if not inventory_string:
            self._logManager.add_log("❌ В настройках looter нет валидных inventory pair")
            return

        self._logManager.add_log(f"🚚 Запускаю Send trade для {len(selected_accounts)} аккаунтов")
        threading.Thread(
            target=self._send_trade_worker,
            args=(selected_accounts, trade_link, inventory_string, script_path, on_trade_sent),
            daemon=True,
        ).start()

    def _extract_looter_error(self, stdout, stderr):
        lines = [line.strip() for line in (stdout or "").splitlines() if line.strip()]
        for line in reversed(lines):
            if "HandleError" in line:
                return line

        err_lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
        if err_lines:
            return err_lines[-1]
        return ""

    def _is_authorization_error(self, error_line):
        lowered = (error_line or "").lower()
        return (
            "steam login error" in lowered
            or "ratelimitexceeded" in lowered
            or "accountlogindeniedthrottle" in lowered
            or "toomanylogonfailures" in lowered
            or "invalidpassword" in lowered
            or "twofactor" in lowered
            or "invalidauthcode" in lowered
        )

    def _send_trade_worker(self, selected_accounts, trade_link, inventory_string, script_path, on_trade_sent=None):
        script_dir = os.path.dirname(script_path)

        if not self._ensure_looter_dependencies(script_dir):
            return

        for acc in selected_accounts:
            if not acc.shared_secret:
                self._logManager.add_log(f"⚠️ [{acc.login}] Нет shared_secret (mafile), пропускаю")
                continue

            if not getattr(acc, "identity_secret", None):
                self._logManager.add_log(f"⚠️ [{acc.login}] Нет identity_secret (mafile), пропускаю")
                continue

            cmd = [
                "node",
                script_path,
                acc.login,
                acc.password,
                acc.shared_secret,
                acc.identity_secret,
                trade_link,
                inventory_string,
            ]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    cwd=script_dir,
                    env={**os.environ, "NODE_NO_WARNINGS": "1"},
                )
            except FileNotFoundError:
                self._logManager.add_log("❌ Не найден Node.js (команда node)")
                return
            except subprocess.TimeoutExpired:
                self._logManager.add_log(f"⏰ [{acc.login}] Таймаут отправки трейда (180с)")
                continue
            except Exception as exc:
                self._logManager.add_log(f"❌ [{acc.login}] Ошибка отправки трейда: {exc}")
                continue

            stdout = (result.stdout or "").strip()
            stderr = (result.stderr or "").strip()

            if result.returncode == 0:
                sent_count = 0
                for line in stdout.splitlines():
                    if line.startswith("SENT_ITEMS_COUNT:"):
                        try:
                            sent_count = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            sent_count = 0
                        break

                self._logManager.add_log(f"{acc.login} succesfull send trade: {sent_count}")
                if callable(on_trade_sent):
                    try:
                        on_trade_sent(acc.login)
                    except Exception:
                        pass
            elif result.returncode == 10:
                self._logManager.add_log(f"{acc.login} inventory is empty")
            else:
                error_line = self._extract_looter_error(stdout, stderr)
                if self._is_authorization_error(error_line):
                    self._logManager.add_log(f"❌ [{acc.login}] Ошибка авторизации")
                elif error_line:
                    self._logManager.add_log(f"❌ [{acc.login}] Ошибка при отправке трейда: (проверьте на наличие блокировок)")
                else:
                    self._logManager.add_log(f"❌ [{acc.login}] Ошибка при отправке трейда (проверьте на наличие блокировок)")

    def _run_install_command(self, cmd, cwd, timeout=300):
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired:
            self._logManager.add_log("❌ Установка зависимостей не успела завершиться (таймаут 300с)")
            return False
        except Exception as exc:
            self._logManager.add_log(f"❌ Ошибка запуска установщика зависимостей: {exc}")
            return False

    def _install_looter_dependencies(self, script_dir):
        attempted = []

        install_commands = [
            ["npm", "install", "--no-audit", "--no-fund"],
            ["npm.cmd", "install", "--no-audit", "--no-fund"],
            ["corepack", "npm", "install", "--no-audit", "--no-fund"],
        ]

        for cmd in install_commands:
            attempted.append(" ".join(cmd))
            result = self._run_install_command(cmd, script_dir)
            if result is None:
                continue
            if result is False:
                return False
            return result

        node_path = shutil.which("node")
        if node_path:
            node_dir = os.path.dirname(node_path)
            npm_cli_candidates = [
                os.path.join(node_dir, "node_modules", "npm", "bin", "npm-cli.js"),
                os.path.join(node_dir, "..", "node_modules", "npm", "bin", "npm-cli.js"),
                os.path.join(
                    os.environ.get("ProgramFiles", "C:/Program Files"),
                    "nodejs",
                    "node_modules",
                    "npm",
                    "bin",
                    "npm-cli.js",
                ),
            ]

            for cli_path in npm_cli_candidates:
                cli_path = os.path.abspath(cli_path)
                if not os.path.isfile(cli_path):
                    continue

                cmd = ["node", cli_path, "install", "--no-audit", "--no-fund"]
                attempted.append(" ".join(cmd))
                result = self._run_install_command(cmd, script_dir)
                if result is None:
                    continue
                if result is False:
                    return False
                return result

        self._logManager.add_log("❌ Не удалось найти рабочий npm installer автоматически")
        if attempted:
            self._logManager.add_log("⚠️ Пробовал: " + " || ".join(attempted))
        self._logManager.add_log("⚠️ Установите Node.js LTS (включая npm) и перезапустите приложение")
        return None

    def _ensure_looter_dependencies(self, script_dir):
        package_json_path = os.path.join(script_dir, "package.json")
        if not os.path.isfile(package_json_path):
            self._logManager.add_log("❌ package.json для looter не найден. Переустановите сборку")
            return False

        steam_user_module = os.path.join(script_dir, "node_modules", "steam-user")
        if os.path.isdir(steam_user_module):
            return True

        self._logManager.add_log("📦 Не найдены Node.js зависимости looter. Выполняю авто-установку...")

        install_result = self._install_looter_dependencies(script_dir)
        if install_result is None:
            return False
        if install_result is False:
            return False

        if install_result.returncode != 0:
            stdout_tail = " | ".join((install_result.stdout or "").splitlines()[-8:])
            stderr_tail = " | ".join((install_result.stderr or "").splitlines()[-8:])
            self._logManager.add_log(f"❌ npm install завершился с code={install_result.returncode}")
            if stdout_tail:
                self._logManager.add_log(f"📄 npm stdout: {stdout_tail}")
            if stderr_tail:
                self._logManager.add_log(f"⚠️ npm stderr: {stderr_tail}")
            return False

        if not os.path.isdir(steam_user_module):
            self._logManager.add_log("❌ После npm install модуль steam-user всё ещё отсутствует")
            return False

        self._logManager.add_log("✅ Node.js зависимости looter установлены")
        return True

    def _normalize_inventory_string(self, inventory_string):
        pairs = []
        normalized_raw = (
            (inventory_string or "")
            .replace(';', ',')
            .replace('\n', ',')
            .replace('\t', ',')
            .replace(' ', ',')
        )
        for raw_pair in normalized_raw.split(','):
            pair = raw_pair.strip()
            if not pair:
                continue

            if pair == "400/2":
                self._logManager.add_log("⚠️ Исправил appid 400/2 -> 440/2 (TF2)")
                pair = "440/2"

            if '/' not in pair:
                self._logManager.add_log(f"⚠️ Пропущен некорректный inventory pair: {pair}")
                continue

            app_id, context_id = [v.strip() for v in pair.split('/', 1)]
            if not app_id.isdigit() or not context_id.isdigit():
                self._logManager.add_log(f"⚠️ Пропущен некорректный inventory pair: {pair}")
                continue

            normalized_pair = f"{app_id}/{context_id}"
            if normalized_pair not in pairs:
                pairs.append(normalized_pair)

        return ','.join(pairs)