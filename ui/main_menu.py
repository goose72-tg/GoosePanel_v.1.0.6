import customtkinter
import threading
import time
import keyboard

from Managers.AccountsManager import AccountManager
from Managers.LobbyManager import LobbyManager
from Managers.LogManager import LogManager
from Managers.SettingsManager import SettingsManager
from Modules.AutoAcceptModule import AutoAcceptModule


class MainMenu(customtkinter.CTkTabview):
    def __init__(self, parent):
        super().__init__(parent, width=250)
        self.grid(row=0, column=2, padx=(20, 0), pady=(0, 0), sticky="nsew")

        self._create_main_tab()

        self._logManager = LogManager()
        self._accountManager = AccountManager()
        self._lobbyManager = LobbyManager()
        self._settingsManager = SettingsManager()
        self.auto_accept_module = AutoAcceptModule()

        auto_accept_enabled = bool(self._settingsManager.get("AutoAcceptEnabled", True))
        if auto_accept_enabled:
            self.auto_accept_module.start()
            print("🚀 AutoAcceptModule: АВТОЗАПУСК ✓")

        self._create_buttons([
            ("Make lobbies", "darkgreen", self.make_lobbies),
            ("Disband lobbies", "darkblue", self.disband_lobbies),
            ("Shuffle lobbies", "darkblue", self.shuffle_lobbies),
            ("Make lobbies & Search game", "purple", self.make_lobbies_and_search_game),
        ])

        self._create_toggle("Auto Accept Game", self.toggle_auto_accept, default_value=auto_accept_enabled)

        self._cancel_requested = False
        self._hotkey_registered = False
        self._active_action_name = None
        self._cancel_notified_for_action = None
        self._last_hotkey_ts = 0.0
        self._register_global_cancel_hotkey()

    def _create_main_tab(self):
        self.add("Main Menu")
        self.tab("Main Menu").grid_columnconfigure(0, weight=1)

    def _create_buttons(self, buttons_data):
        self.buttons = {}
        for i, (text, color, command) in enumerate(buttons_data):
            button = customtkinter.CTkButton(
                self.tab("Main Menu"),
                text=text,
                fg_color=color,
                command=command
            )
            button.grid(row=i, column=0, padx=20, pady=10, sticky="ew")
            self.buttons[text] = button

    def _create_toggle(self, text, command, default_value=False):
        self.toggles = getattr(self, "toggles", {})
        toggle = customtkinter.CTkSwitch(
            self.tab("Main Menu"),
            text=text,
            command=command
        )
        toggle.grid(row=len(self.buttons) + len(self.toggles), column=0, padx=20, pady=10)
        if default_value:
            toggle.select()
        else:
            toggle.deselect()
        self.toggles[text] = toggle

    def _register_global_cancel_hotkey(self):
        if self._hotkey_registered:
            return
        try:
            keyboard.add_hotkey('ctrl+q', self._on_global_cancel_hotkey)
            self._hotkey_registered = True
            print("✅ Global Ctrl+Q hotkey registered")
        except Exception as e:
            print(f"⚠️ Cannot register global Ctrl+Q hotkey: {e}")

    def _on_global_cancel_hotkey(self):
        # анти-флуд: suppress key-repeat storms
        now = time.time()
        if now - self._last_hotkey_ts < 0.25:
            return
        self._last_hotkey_ts = now
        self._cancel_requested = True

    def _is_cancelled(self):
        if self._cancel_requested:
            return True
        try:
            return keyboard.is_pressed('ctrl+q')
        except Exception:
            return False

    def _format_cancel_message(self, action_name):
        mapping = {
            "Make lobbies": "Make lobbies",
            "Disband lobbies": "Disband lobbies",
            "Shuffle lobbies": "Shuffle lobbies",
            "Make lobbies & Search game": "Auto game canceled (Ctrl+q)",
        }
        return mapping.get(action_name or "", "Canceled action")

    def _notify_cancel_once(self, action_name):
        if self._cancel_notified_for_action == action_name:
            return
        self._cancel_notified_for_action = action_name
        msg = self._format_cancel_message(action_name)
        try:
            self._logManager.add_log(msg)
        except Exception:
            pass
        print(f"🛑 {msg}")

    def toggle_auto_accept(self):
        self.auto_accept_module.toggle()
        status = 'ON' if self.auto_accept_module._running else 'OFF'
        print(f"🔄 Auto Accept Game: {status}")
        self._lobbyManager.auto_accept = self.auto_accept_module._running
        self._settingsManager.set("AutoAcceptEnabled", self.auto_accept_module._running)
    def _set_all_buttons_state(self, state):
        for button in self.buttons.values():
            try:
                button.configure(state=state)
            except Exception:
                pass
    # -----------------------------
    # Universal countdown runner on button
    # -----------------------------
    def run_with_countdown_on_button(
        self,
        button_text,
        action,
        message="Completed",
        message_in_run="Running...",
        countdown=3,
        message_time=1
    ):
        button = self.buttons.get(button_text)
        if not button:
            return

        original_text = button.cget("text")
        self._active_action_name = button_text
        self._cancel_notified_for_action = None
        self._cancel_requested = False
        self._set_all_buttons_state("disabled")
        self._countdown_step(button, action, original_text, countdown, message, message_in_run, message_time)

    def _countdown_step(self, button, action, original_text, seconds, message, message_in_run, message_time):
        if self._is_cancelled():
            self._notify_cancel_once(self._active_action_name)
            button.configure(text=self._format_cancel_message(self._active_action_name), state="disabled")
            self.after(message_time * 1000, lambda: self._reset_button_text(button, original_text))
            return

        if seconds > 0:
            button.configure(text=f"{seconds}...")
            self.after(1000, lambda: self._countdown_step(
                button, action, original_text, seconds - 1, message, message_in_run, message_time
            ))
        else:
            button.configure(text=message_in_run)
            self.after(100, lambda: self._run_action_on_button(
                button, action, original_text, message, message_time
            ))

    def _run_action_on_button(self, button, action, original_text, message, message_time):
        def worker():
            ok = False
            cancelled = False
            try:

                if self._is_cancelled():
                    cancelled = True
                else:
                    res = action()
                    ok = bool(res) if res is not None else True
                    if not ok and self._is_cancelled():
                        cancelled = True
            except Exception as e:
                print(f"❌ Action error: {e}")
                ok = False

            def ui_done():

                if cancelled:
                    self._notify_cancel_once(self._active_action_name)
                    button.configure(text=self._format_cancel_message(self._active_action_name), state="disabled")
                else:
                    button.configure(text=message if ok else "Failed", state="disabled")
                self.after(message_time * 1000, lambda: self._reset_button_text(button, original_text))

            self.after(0, ui_done)

        threading.Thread(target=worker, daemon=True).start()

    def _reset_button_text(self, button, original_text):
        button.configure(text=original_text)
        self._set_all_buttons_state("normal")
        self._active_action_name = None

    # -----------------------------
    # Button actions
    # -----------------------------
    def make_lobbies(self):
        self.run_with_countdown_on_button(
            button_text="Make lobbies",
            action=self._lobbyManager.CollectLobby,
            message="Completed",
            message_in_run="Collecting lobbies...",
            countdown=3,
            message_time=1
        )

    def disband_lobbies(self):
        self.run_with_countdown_on_button(
            button_text="Disband lobbies",
            action=self._lobbyManager.DisbandLobbies,
            message="Completed",
            message_in_run="Disbanding lobbies...",
            countdown=1,
            message_time=1
        )

    def shuffle_lobbies(self):

        self.run_with_countdown_on_button(
            button_text="Shuffle lobbies",
            action=self._lobbyManager.Shuffle,
            message="Completed",
            message_in_run="Shuffling lobbies...",
            countdown=1,
            message_time=1
        )

    def make_lobbies_and_search_game(self):
        self.run_with_countdown_on_button(
            button_text="Make lobbies & Search game",
            action=self._lobbyManager.MakeLobbiesAndSearchGame,
            message="Completed",
            message_in_run="Making lobbies & Search",
            countdown=3,
            message_time=1
        )

    def trigger_make_lobbies_and_search_game_auto(self):
        """Надёжно имитирует клик по кнопке Main Menu для авто-сценария."""
        button_text = "Make lobbies & Search game"
        button = self.buttons.get(button_text)
        if not button:
            self._logManager.add_log("❌ Main Menu button not found: Make lobbies & Search game")
            return False

        try:
            if str(button.cget("state")) == "disabled":
                self._logManager.add_log("⚠️ Main Menu button is disabled: Make lobbies & Search game")
                return False
        except Exception:
            pass

        try:
            button.invoke()
            return True
        except Exception as e:
            self._logManager.add_log(f"❌ Button invoke error: {e}")
            return False