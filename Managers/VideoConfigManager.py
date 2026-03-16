import os
import re
from typing import Tuple

import wmi

from Managers.SettingsManager import SettingsManager


_VENDOR_PRIORITY = {
    0x10DE: 3,  # NVIDIA
    0x1002: 2,  # AMD
    0x8086: 1,  # Intel
}


class VideoConfigManager:
    def __init__(self):
        self._settings_manager = SettingsManager()
        self._video_cfg_path = os.path.join("settings", "cs2_video.txt")

    def sync_on_startup(self) -> Tuple[int, int, str]:
        vendor_id, device_id = self._detect_best_gpu_ids()
        source = "detected"

        if vendor_id <= 0 or device_id <= 0:
            vendor_id = int(self._settings_manager.get("VendorID", 0) or 0)
            device_id = int(self._settings_manager.get("DeviceID", 0) or 0)
            source = "settings_fallback"

        self._settings_manager.set("VendorID", vendor_id)
        self._settings_manager.set("DeviceID", device_id)
        self._replace_video_ids(vendor_id, device_id)
        return vendor_id, device_id, source

    def _detect_best_gpu_ids(self) -> Tuple[int, int]:
        candidates = []

        try:
            controllers = wmi.WMI().Win32_VideoController()
        except Exception:
            return 0, 0

        for gpu in controllers:
            pnp = getattr(gpu, "PNPDeviceID", "") or ""
            ven_match = re.search(r"VEN_([0-9A-Fa-f]{4})", pnp)
            dev_match = re.search(r"DEV_([0-9A-Fa-f]{4})", pnp)
            if not ven_match or not dev_match:
                continue

            try:
                vendor_id = int(ven_match.group(1), 16)
                device_id = int(dev_match.group(1), 16)
            except ValueError:
                continue

            try:
                memory = int(getattr(gpu, "AdapterRAM", 0) or 0)
            except (TypeError, ValueError):
                memory = 0

            candidates.append({
                "vendor": vendor_id,
                "device": device_id,
                "priority": _VENDOR_PRIORITY.get(vendor_id, 0),
                "memory": memory,
            })

        if not candidates:
            return 0, 0

        best = max(candidates, key=lambda item: (item["priority"], item["memory"]))
        return best["vendor"], best["device"]

    def _replace_video_ids(self, vendor_id: int, device_id: int) -> bool:
        if not os.path.exists(self._video_cfg_path):
            return False

        try:
            with open(self._video_cfg_path, "r", encoding="utf-8") as file:
                content = file.read()

            content = re.sub(
                r'("VendorID"\s+")[^"]*(")',
                rf'\g<1>{vendor_id}\2',
                content,
                count=1,
            )
            content = re.sub(
                r'("DeviceID"\s+")[^"]*(")',
                rf'\g<1>{device_id}\2',
                content,
                count=1,
            )

            with open(self._video_cfg_path, "w", encoding="utf-8") as file:
                file.write(content)
            return True
        except Exception:
            return False