import json
import os


class SettingsManager:
    _instance = None
    _file_path = os.path.join("settings", "settings.json")
    _settings = {}
    _hidden_keys = {
        "SteamMutexInitDelay",
        "SteamMutexName",
        "SteamMutexCloseAttempts",
        "SteamMutexRetryDelay",
        "SteamMutexInitTimeout",
        "SteamMutexPollInterval",
    }

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(SettingsManager, cls).__new__(cls)
            cls._instance._load()
        return cls._instance

    def _load(self):
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)

        if not os.path.exists(self._file_path):
            self._settings = {}
            self._save()
            return

        with open(self._file_path, "r", encoding="utf-8") as f:
            try:
                self._settings = json.load(f)
            except json.JSONDecodeError:
                self._settings = {}
                self._save()  # Перезапишем пустым словарём, если файл битый
                return

        if self._remove_hidden_keys():
            self._save()

    def _save(self):
        self._remove_hidden_keys()
        with open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(self._settings, f, indent=4, ensure_ascii=False)

    def _remove_hidden_keys(self):
        removed = False
        for key in self._hidden_keys:
            if key in self._settings:
                self._settings.pop(key, None)
                removed = True
        return removed

    def get(self, key, default=None):
        """
        Получение значения настройки.
        Если ключ отсутствует, создаёт его с default и возвращает default.
        """
        if key in self._hidden_keys:
            return default

        if key not in self._settings:
            self._settings[key] = default
            self._save()
        return self._settings[key]

    def set(self, key, value):
        if key in self._hidden_keys:
            self.delete(key)
            return

        self._settings[key] = value
        self._save()

    def delete(self, key):
        if key in self._settings:
            del self._settings[key]
            self._save()

    def all(self):
        return {k: v for k, v in self._settings.items() if k not in self._hidden_keys}