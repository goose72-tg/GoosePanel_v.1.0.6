import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pyautogui
import requests
from requests.adapters import HTTPAdapter
from requests.exceptions import ReadTimeout
from urllib3.util.retry import Retry


class TelegramBotManager:
    _MAX_PENDING_UPDATES = 300

    def __init__(self, token, handlers, log_callback=None, proxy_pool=None, suppress_logs=True):
        self.token = (token or "").strip()
        self.handlers = handlers or {}
        self.log_callback = log_callback or (lambda _msg: None)
        self.suppress_logs = bool(suppress_logs)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self.proxy_pool = list(proxy_pool or [])
        self._proxy_index = -1
        self._proxy_lock = threading.Lock()

        self.session = None
        self._configure_session()

        self._running = False
        self._thread = None
        self._offset = 0

        worker_count = min(24, max(6, (os.cpu_count() or 2) * 2))
        self._workers = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tg-bot-worker")
        self._pending_updates = threading.BoundedSemaphore(self._MAX_PENDING_UPDATES)
        self._drop_warning_at = 0.0

        self._screenshot_lock = threading.Lock()
        self._menu_message_ids = {}
        self._processed_update_ids = set()
        self._processed_update_ids_lock = threading.Lock()

    def _log(self, message):
        if self.suppress_logs:
            return
        self.log_callback(message)

    def _next_proxy(self):
        with self._proxy_lock:
            if not self.proxy_pool:
                return None
            self._proxy_index = (self._proxy_index + 1) % len(self.proxy_pool)
            return self.proxy_pool[self._proxy_index]

    def _set_session_proxy(self, proxy_url):
        with self._proxy_lock:
            self.session.proxies.clear()
            if proxy_url:
                self.session.proxies.update({"http": proxy_url, "https": proxy_url})

    def update_proxy_pool(self, proxy_pool):
        with self._proxy_lock:
            self.proxy_pool = list(proxy_pool or [])
            self._proxy_index = -1
        self._rotate_proxy()

    def _rotate_proxy(self):
        self._set_session_proxy(self._next_proxy())

    def _configure_session(self):
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass

        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            read=3,
            connect=3,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._rotate_proxy()

    def _build_workers(self):
        worker_count = min(24, max(6, (os.cpu_count() or 2) * 2))
        self._workers = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="tg-bot-worker")

    def start(self):
        if not self.token:
            return False
        if self._running:
            return True

        if getattr(self._workers, "_shutdown", False):
            self._build_workers()

        # Reinitialize HTTP session on every start to guarantee healthy adapters/pools.
        self._configure_session()

        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="tg-bot-poller")
        self._thread.start()
        self._log("🤖 Telegram bot started")
        return True

    def stop(self):
        self._running = False

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

        self._workers.shutdown(wait=False, cancel_futures=True)

        try:
            self.session.close()
        except Exception:
            pass

    def _request(self, method, payload=None, timeout=30):
        if not self.session:
            return None

        try:
            if method == "getUpdates":
                response = self.session.get(
                    f"{self.base_url}/{method}",
                    params=payload or {},
                    timeout=timeout,
                )
            else:
                response = self.session.post(
                    f"{self.base_url}/{method}",
                    json=payload or {},
                    timeout=timeout,
                )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                description = data.get("description", "unknown Telegram API error")
                if "message is not modified" in description.lower():
                    return {"not_modified": True}
                self._rotate_proxy()
                self._log(f"⚠️ Telegram API returned not-ok ({method}): {description}")
                return None
            return data.get("result")
        except ReadTimeout:
            if method == "getUpdates":
                # Long polling timeout is normal on unstable links; keep loop healthy without noisy logs.
                return []
            self._rotate_proxy()
            self._log(f"⚠️ Telegram timeout ({method})")
            return None
        except Exception as exc:
            self._rotate_proxy()
            self._log(f"⚠️ Telegram API error ({method}): {exc}")
            return None

    def _send_message(self, chat_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("sendMessage", payload)

    def _edit_message(self, chat_id, message_id, text, reply_markup=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self._request("editMessageText", payload)

    def _delete_message(self, chat_id, message_id):
        payload = {"chat_id": chat_id, "message_id": message_id}
        return self._request("deleteMessage", payload)

    def _answer_callback(self, callback_id, text=None):
        if not callback_id:
            return
        payload = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        self._request("answerCallbackQuery", payload, timeout=10)

    def _send_photo(self, chat_id, image_path):
        for attempt in range(1, 4):
            try:
                with open(image_path, "rb") as image_file:
                    response = self.session.post(
                        f"{self.base_url}/sendPhoto",
                        data={"chat_id": chat_id},
                        files={"photo": image_file},
                        timeout=120,
                    )
                response.raise_for_status()
                data = response.json()
                if data.get("ok"):
                    return True
                raise RuntimeError(data.get("description", "Telegram returned ok=false"))
            except Exception as exc:
                self._rotate_proxy()
                self._log(f"⚠️ Telegram sendPhoto error (attempt {attempt}/3): {exc}")
                if attempt < 3:
                    time.sleep(1)

        try:
            with open(image_path, "rb") as image_file:
                response = self.session.post(
                    f"{self.base_url}/sendDocument",
                    data={"chat_id": chat_id},
                    files={"document": image_file},
                    timeout=120,
                )
            response.raise_for_status()
            data = response.json()
            if data.get("ok"):
                return True
            raise RuntimeError(data.get("description", "Telegram returned ok=false"))
        except Exception as exc:
            self._rotate_proxy()
            self._log(f"⚠️ Telegram sendDocument fallback error: {exc}")
            return False

    @staticmethod
    def _prepare_screenshot_file(tmp_file):
        image = pyautogui.screenshot()
        rgb_image = image.convert("RGB")
        rgb_image.save(tmp_file, format="JPEG", quality=85, optimize=True)

    def _poll_loop(self):
        error_backoff = 1
        while self._running:
            try:
                updates = self._request(
                    "getUpdates",
                    {"offset": self._offset, "timeout": 25, "limit": 100},
                    timeout=35,
                )

                if not updates:
                    error_backoff = 1
                    continue

                for update in updates:
                    self._offset = max(self._offset, update.get("update_id", 0) + 1)
                    self._dispatch_update(update)

                error_backoff = 1
            except Exception as exc:
                self._rotate_proxy()
                self._log(f"⚠️ Telegram loop error: {exc}")
                time.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, 10)

    def _dispatch_update(self, update):
        update_id = update.get("update_id")
        if update_id is not None:
            with self._processed_update_ids_lock:
                if update_id in self._processed_update_ids:
                    return
                self._processed_update_ids.add(update_id)
                if len(self._processed_update_ids) > 5000:
                    self._processed_update_ids = set(sorted(self._processed_update_ids)[-2000:])
        if not self._pending_updates.acquire(blocking=False):
            now = time.time()
            if now >= self._drop_warning_at:
                self._drop_warning_at = now + 5
                self._log("⚠️ Telegram queue is overloaded, dropping update to keep bot responsive")
            return

        def _run():
            try:
                self._handle_update(update)
            except Exception as exc:
                self._log(f"⚠️ Telegram update handler error: {exc}")
            finally:
                self._pending_updates.release()

        try:
            self._workers.submit(_run)
        except Exception as exc:
            self._pending_updates.release()
            self._log(f"⚠️ Failed to submit Telegram update to worker: {exc}")

    def _handle_update(self, update):
        message = update.get("message")
        callback_query = update.get("callback_query")

        if message:
            self._handle_message(message)
        elif callback_query:
            self._handle_callback_query(callback_query)

    def _handle_message(self, message):
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return

        if text == "/start":
            self._send_message(
                chat_id,
                "Выберите раздел:",
                reply_markup={
                    "keyboard": [[{"text": "Functionals"}], [{"text": "Configurations"}]],
                    "resize_keyboard": True,
                },
            )
            return

        if text == "Functionals":
            self._send_functionals_menu(chat_id)
            return

        if text == "Configurations":
            self._send_config_menu(chat_id)
            return

    def _send_functionals_menu(self, chat_id):
        self._show_or_update_menu(
            chat_id,
            "Functionals:",
            {
                "inline_keyboard": [
                    [{"text": "📸 Screenshot", "callback_data": "fn:screenshot"}],
                    [{"text": "📊 Launched accs stats", "callback_data": "fn:launchedstats"}],
                    [{"text": "🧩 Select 4 unfarmed", "callback_data": "fn:select4"}],
                    [{"text": "🛠 Select accounts manually", "callback_data": "fn:accounts:0"}],
                    [{"text": "🎮 Make lobbies and search game", "callback_data": "fn:makelobbiessearch"}],
                    [{"text": "🚀 Start selected accounts", "callback_data": "fn:launch"}],
                    [{"text": "🧹 Kill all cs & steam", "callback_data": "fn:killall"}],
                ]
            },
        )

    def _show_or_update_menu(self, chat_id, text, reply_markup, message_id=None):
        target_message_id = message_id or self._menu_message_ids.get(chat_id)

        if target_message_id:
            edited = self._edit_message(chat_id, target_message_id, text, reply_markup=reply_markup)
            if edited is not None:
                self._menu_message_ids[chat_id] = target_message_id
                return edited

            self._delete_message(chat_id, target_message_id)
            if self._menu_message_ids.get(chat_id) == target_message_id:
                self._menu_message_ids.pop(chat_id, None)

        sent = self._send_message(chat_id, text, reply_markup=reply_markup)
        if isinstance(sent, dict) and sent.get("message_id"):
            self._menu_message_ids[chat_id] = sent["message_id"]
        return sent

    def _build_accounts_page(self, page):
        get_accounts = self.handlers.get("get_accounts")
        accounts = get_accounts() if get_accounts else []
        page_size = 12
        total_pages = max(1, (len(accounts) + page_size - 1) // page_size)
        page = max(0, min(page, total_pages - 1))
        start = page * page_size
        end = start + page_size
        chunk = accounts[start:end]

        keyboard = []
        for item in chunk:
            select_emoji = "✅" if item.get("selected") else "⭕️"
            farm_emoji = "❗️" if item.get("state") == "Farmed" else "❕"
            launch_suffix = " ▶️" if item.get("status") == "🟢" else ""
            keyboard.append([
                {
                    "text": f"{select_emoji}{farm_emoji}{item['state']} | {item['login']} {launch_suffix}",
                    "callback_data": f"fn:acctoggle:{item['index']}:{page}",
                }
            ])

        keyboard.append([
            {"text": "(<)", "callback_data": f"fn:accounts:{max(0, page - 1)}"},
            {"text": f"{page + 1}/{total_pages}", "callback_data": "noop"},
            {"text": "(>)", "callback_data": f"fn:accounts:{min(total_pages - 1, page + 1)}"},
        ])
        keyboard.append([
            {"text": "🟢 Start selected accs", "callback_data": "fn:launch"},
            {"text": "🟡 Select 4 unfarmed", "callback_data": "fn:select4"},
        ])
        keyboard.append([
            {"text": "Back", "callback_data": "fn:back"},
        ])

        return page, total_pages, keyboard

    def _send_config_menu(self, chat_id, message_id=None):
        get_config = self.handlers.get("get_config")
        config = get_config() if get_config else {}

        rows = []
        mapping = [
            ("Auto accept game", "AutoAcceptEnabled"),
            ("Auto match in start", "AutoMatchInStartEnabled"),
            ("Automatic account switching", "AutomaticAccountSwitchingEnabled"),
        ]

        for title, key in mapping:
            enabled = bool(config.get(key, False))
            rows.append([{"text": title, "callback_data": "noop"}])
            rows.append([
                {
                    "text": "✅ ON" if enabled else "❌ ON",
                    "callback_data": f"cfg:set:{key}:1",
                },
                {
                    "text": "✅ OFF" if not enabled else "❌ OFF",
                    "callback_data": f"cfg:set:{key}:0",
                },
            ])

        get_proxy_status = self.handlers.get("get_proxy_status")
        proxy_status = (get_proxy_status() if get_proxy_status else "off") or "off"
        rows.append([{"text": f"Proxy: {proxy_status}", "callback_data": "noop"}])

        reply_markup = {"inline_keyboard": rows}
        self._show_or_update_menu(chat_id, "Configurations:", reply_markup, message_id=message_id)

    def _handle_callback_query(self, callback_query):
        callback_id = callback_query.get("id")
        data = callback_query.get("data", "")
        message = callback_query.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        message_id = message.get("message_id")

        if not chat_id:
            return

        if data == "noop":
            self._answer_callback(callback_id)
            return

        if data == "fn:back":
            self._send_functionals_menu(chat_id)
            self._answer_callback(callback_id)
            return

        if data.startswith("fn:accounts:"):
            try:
                page = int(data.split(":")[-1])
            except (TypeError, ValueError):
                self._answer_callback(callback_id, "Некорректная страница")
                return

            _, _, keyboard = self._build_accounts_page(page)
            self._show_or_update_menu(chat_id, "Account list:", {"inline_keyboard": keyboard}, message_id=message_id)
            self._answer_callback(callback_id)
            return

        if data.startswith("fn:acctoggle:"):
            parts = data.split(":")
            if len(parts) < 4:
                self._answer_callback(callback_id, "Некорректные данные")
                return

            try:
                account_index = int(parts[2])
                page = int(parts[3])
            except (TypeError, ValueError):
                self._answer_callback(callback_id, "Некорректные данные")
                return

            toggle_account = self.handlers.get("toggle_account")
            if toggle_account:
                toggle_account(account_index)

            _, _, keyboard = self._build_accounts_page(page)
            self._show_or_update_menu(chat_id, "Account list:", {"inline_keyboard": keyboard}, message_id=message_id)
            self._answer_callback(callback_id)
            return

        fn_actions = {
            "fn:launch": "launch_selected",
            "fn:select4": "select4",
            "fn:killall": "killall",
            "fn:makelobbiessearch": "make_lobbies_search",
        }
        if data in fn_actions:
            action = self.handlers.get(fn_actions[data])
            if action:
                action()

            if data == "fn:select4" and message_id:
                _, _, keyboard = self._build_accounts_page(0)
                self._show_or_update_menu(chat_id, "Account list:", {"inline_keyboard": keyboard}, message_id=message_id)
            self._answer_callback(callback_id, "Done")
            return

        if data == "fn:launchedstats":
            get_levels = self.handlers.get("get_launched_levels")
            text = get_levels() if get_levels else "No launched accounts"
            self._show_or_update_menu(
                chat_id,
                f"Launched accounts stats:\n{text}",
                {"inline_keyboard": [[{"text": "Back", "callback_data": "fn:back"}]]},
                message_id=message_id,
            )
            self._answer_callback(callback_id)
            return

        if data == "fn:screenshot":
            self._answer_callback(callback_id, "Скриншот готовится...")
            tmp_file = Path(tempfile.gettempdir()) / f"telegram_panel_screen_{int(time.time())}.jpg"
            try:
                with self._screenshot_lock:
                    self._prepare_screenshot_file(tmp_file)
                sent = self._send_photo(chat_id, tmp_file)
                if not sent:
                    self._send_message(chat_id, "Не удалось отправить скриншот. Попробуйте ещё раз.")
            except Exception as exc:
                self._send_message(chat_id, f"Screenshot error: {exc}")
            finally:
                try:
                    tmp_file.unlink(missing_ok=True)
                except Exception:
                    pass
            return

        if data.startswith("cfg:set:"):
            _, _, key, value_raw = data.split(":")
            set_config = self.handlers.get("set_config")
            if set_config:
                set_config(key, value_raw == "1")
            self._send_config_menu(chat_id, message_id=message_id)
            self._answer_callback(callback_id)
            return