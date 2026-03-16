# Modules/AutoAcceptModule.py
import threading
import time
from collections import Counter

from Managers.AccountsManager import AccountManager
from Managers.LobbyManager import LobbyManager
from Managers.LogManager import LogManager


class AutoAcceptModule:
    _disable_final_clicks = False
    _last_disable_log_match_id = None

    def __init__(self):
        self._running = False
        self._thread = None
        self.logManager = LogManager()
        self.accountManager = AccountManager()

    @classmethod
    def final_clicks_disabled(cls):
        return cls._disable_final_clicks

    @classmethod
    def reset_final_clicks_state(cls):
        cls._disable_final_clicks = False
        cls._last_disable_log_match_id = None

    def _register_same_match(self, match_id, seen_count=0):
        if match_id is None:
            return

        # Строго по ТЗ: останавливаем цикл только если одновременно видим >= 4 одинаковых match_id.
        if seen_count < 4:
            return

        AutoAcceptModule._disable_final_clicks = True

        if AutoAcceptModule._last_disable_log_match_id != match_id:
            AutoAcceptModule._last_disable_log_match_id = match_id

            self.logManager.add_log("[A.Accept] auto accept found")

    @staticmethod
    def _click_accept_button(acc, click_delay=0.2):
        acc.last_match_id = None
        win_width, win_height = acc.getWindowSize()
        center_x = win_width // 2
        center_y = (win_height // 2) - 20
        acc.ClickMouse(center_x, center_y)
        time.sleep(click_delay)

    def _accept_for_accounts(self, accounts):

        time.sleep(1)

        for acc in accounts:
            self._click_accept_button(acc, click_delay=0.2)
            self._click_accept_button(acc, click_delay=0.2)

    def _auto_accept_loop(self):
        lobbyManager = LobbyManager()

        while self._running:
            if not lobbyManager.isValid():
                accounts = [acc for acc in self.accountManager.accounts if acc.isCSValid()]
                self._check_accounts(accounts, lobbyManager)
            else:
                team1_accounts = [lobbyManager.team1.leader] + lobbyManager.team1.bots
                team2_accounts = [lobbyManager.team2.leader] + lobbyManager.team2.bots
                accounts = team1_accounts + team2_accounts
                self._check_accounts(accounts, lobbyManager)

            time.sleep(0.5)

    def _check_accounts(self, accounts, lobbyManager: LobbyManager):
        if not accounts or len(accounts) < 4:
            return

        # Если last_match_id вообще нигде не заполнен — смысла нет
        valid_accounts = [acc for acc in accounts if acc.last_match_id is not None]
        if len(valid_accounts) < 4:
            return

        match_ids = [acc.last_match_id for acc in valid_accounts]
        top_match_id, top_count = Counter(match_ids).most_common(1)[0]

        # Строго по ТЗ: только 4+ одинаковых id одновременно.
        if top_match_id is None or top_count < 4:
            return

        matched_accounts = [acc for acc in accounts if acc.last_match_id == top_match_id]

        self._register_same_match(top_match_id, seen_count=top_count)

        lifted = lobbyManager.lift_all_cs2_windows()
        if lifted:
            time.sleep(0.5)

        self._accept_for_accounts(matched_accounts)

    def start(self):
        if not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._auto_accept_loop, daemon=True)
            self._thread.start()
            print("AutoAccept started")

    def stop(self):
        if self._running:
            self._running = False
            if self._thread is not None:
                self._thread.join(timeout=1)
            print("AutoAccept stopped")

    def toggle(self):
        if self._running:
            self.stop()
        else:
            self.start()
