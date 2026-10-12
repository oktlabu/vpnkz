#!/usr/bin/env python3
import json
import os
import shutil
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

BOT_DIR = Path("/opt/vpn-telegram-bot")
STATE_DIR = Path("/var/lib/vpn-healthcheck")
STATE_FILE = STATE_DIR / "state.json"
DISK_PATH = "/"
MIN_FREE_GB = 3.0
MAX_DISK_PERCENT = 85.0

_original_getaddrinfo = socket.getaddrinfo


def ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = ipv4_getaddrinfo


def load_env(path: Path) -> dict[str, str]:
    values = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def send(text: str) -> None:
    env = load_env(BOT_DIR / "telegram.env") | load_env(BOT_DIR / "admin.env")
    token = env["TELEGRAM_BOT_TOKEN"]
    body = urllib.parse.urlencode({"chat_id": env["ADMIN_TELEGRAM_ID"], "text": text}).encode()
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise RuntimeError(result)


def service_active(name: str) -> bool:
    return subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", name],
        check=False,
    ).returncode == 0


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if "--test" in sys.argv:
        send("✅ Мониторинг VPN включён: WireGuard, Telegram-бот и свободный диск проверяются каждые 5 минут.")
        return

    usage = shutil.disk_usage(DISK_PATH)
    free_gb = usage.free / (1024 ** 3)
    used_percent = usage.used / usage.total * 100
    current = {
        "wireguard": service_active("wg-quick@wg0.service"),
        "telegram_bot": service_active("vpn-telegram-bot.service"),
        "disk_ok": free_gb >= MIN_FREE_GB and used_percent < MAX_DISK_PERCENT,
    }
    previous = load_state()
    alerts = []
    labels = {
        "wireguard": "WireGuard",
        "telegram_bot": "Telegram-бот",
    }
    for key, label in labels.items():
        if not current[key] and previous.get(key) is not False:
            alerts.append(f"⚠️ {label} остановлен")
        elif current[key] and previous.get(key) is False:
            alerts.append(f"✅ {label} снова работает")
    if not current["disk_ok"] and previous.get("disk_ok") is not False:
        alerts.append(f"⚠️ На сервере мало места: свободно {free_gb:.1f} ГБ, занято {used_percent:.0f}%")
    elif current["disk_ok"] and previous.get("disk_ok") is False:
        alerts.append(f"✅ Место на сервере восстановлено: свободно {free_gb:.1f} ГБ")

    if alerts:
        send("\n".join(alerts))
    STATE_FILE.write_text(json.dumps(current), encoding="utf-8")


if __name__ == "__main__":
    main()
