#!/usr/bin/env python3
import json
import logging
import mimetypes
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_original_getaddrinfo = socket.getaddrinfo


def _ipv4_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    results = _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
    return [result for result in results if result[0] == socket.AF_INET]


socket.getaddrinfo = _ipv4_getaddrinfo

APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / "telegram.env"
DB_FILE = APP_DIR / "vpn_bot.sqlite3"


def load_env(path: Path) -> None:
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env(ENV_FILE)
admin_env = APP_DIR / "admin.env"
if admin_env.exists():
    load_env(admin_env)
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_ID = int(os.environ["ADMIN_TELEGRAM_ID"]) if os.environ.get("ADMIN_TELEGRAM_ID") else None
API = f"https://api.telegram.org/bot{TOKEN}"
PENDING_ACTIONS = {}
LOCAL_TZ = ZoneInfo("Asia/Oral")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_FILE)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS clients (
            name TEXT PRIMARY KEY,
            vpn_ip TEXT NOT NULL,
            paid_until TEXT,
            status TEXT NOT NULL,
            telegram_id INTEGER,
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    connection.commit()
    return connection


SEED_CLIENTS = [
    ("Abu", "10.66.66.3", None, "owner", None, "Владелец"),
    ("Rayana", "10.66.66.4", None, "owner", None, "Семья"),
    ("Yerassyl", "10.66.66.5", "2026-09-07", "active", None, "Оплачено"),
    ("nurdaulet_m", "10.66.66.6", "2026-09-23", "active", None, "Оплачено"),
    ("kairat-94", "10.66.66.7", "2026-09-23", "active", None, "Оплачено"),
    ("zhannur_t", "10.66.66.8", "2026-09-23", "active", None, "Оплачено"),
    ("dosmukhammed", "10.66.66.9", "2026-08-30", "refunded", None, "Возврат"),
    ("salimzhan", "10.66.66.10", "2026-08-31", "expired", None, "Триал завершён"),
    ("dastan", "10.66.66.11", "2026-09-03", "trial", None, "Триал"),
]


def initialize() -> None:
    with db() as connection:
        connection.executemany(
            """
            INSERT OR IGNORE INTO clients(name, vpn_ip, paid_until, status, telegram_id, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            SEED_CLIENTS,
        )


def api(method: str, request_timeout: int = 15, **params):
    body = urllib.parse.urlencode(params).encode()
    request = urllib.request.Request(f"{API}/{method}", data=body)
    with urllib.request.urlopen(request, timeout=request_timeout) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(payload)
    return payload["result"]


MAIN_MENU = {
    "keyboard": [
        [{"text": "👥 Клиенты"}, {"text": "⏳ Скоро оплата"}],
        [{"text": "🖥 Состояние сервера"}],
        [{"text": "➕ Создать клиента"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}


def send(chat_id: int, text: str, show_menu: bool = False) -> None:
    params = {"chat_id": chat_id, "text": text}
    if show_menu:
        params["reply_markup"] = json.dumps(MAIN_MENU, ensure_ascii=False)
    api("sendMessage", **params)


def send_inline(chat_id: int, text: str, keyboard: list[list[dict]]) -> None:
    api(
        "sendMessage",
        chat_id=chat_id,
        text=text,
        reply_markup=json.dumps({"inline_keyboard": keyboard}, ensure_ascii=False),
    )


def send_document(chat_id: int, path: Path, caption: str) -> None:
    boundary = "----vpn-bot-boundary"
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    parts = []
    for name, value in (("chat_id", str(chat_id)), ("caption", caption)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{path.name}\"\r\nContent-Type: {content_type}\r\n\r\n".encode()
    )
    parts.append(path.read_bytes())
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    request = urllib.request.Request(
        f"{API}/sendDocument",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if not payload.get("ok"):
        raise RuntimeError(payload)


def clients_text() -> str:
    today = date.today()
    lines = ["Клиенты VPN:"]
    with db() as connection:
        rows = connection.execute(
            "SELECT name, paid_until, status FROM clients ORDER BY vpn_ip"
        ).fetchall()
    for row in rows:
        paid_until = row["paid_until"] or "без срока"
        status = row["status"]
        if row["paid_until"] and date.fromisoformat(row["paid_until"]) < today and status in {"active", "trial"}:
            status = "expired"
        lines.append(f"• {row['name']} — {paid_until} — {status}")
    return "\n".join(lines)


def expiring_text() -> str:
    today = date.today()
    lines = ["Оплата и сроки:"]
    with db() as connection:
        rows = connection.execute(
            "SELECT name, paid_until, status FROM clients WHERE paid_until IS NOT NULL ORDER BY paid_until"
        ).fetchall()
    found = False
    for row in rows:
        if row["status"] in {"refunded", "owner"}:
            continue
        until = date.fromisoformat(row["paid_until"])
        days = (until - today).days
        if days <= 3:
            found = True
            label = "сегодня" if days == 0 else (f"через {days} дн." if days > 0 else f"просрочено на {-days} дн.")
            lines.append(f"• {row['name']} — {row['paid_until']} — {label}")
    if not found:
        lines.append("На ближайшие 3 дня никого нет.")
    return "\n".join(lines)


def daily_reminder_text() -> str | None:
    today = datetime.now(LOCAL_TZ).date()
    with db() as connection:
        rows = connection.execute(
            """
            SELECT name, paid_until FROM clients
            WHERE paid_until IS NOT NULL AND status IN ('active', 'trial')
            ORDER BY paid_until
            """
        ).fetchall()
    lines = []
    for row in rows:
        days = (date.fromisoformat(row["paid_until"]) - today).days
        if not 0 <= days <= 3:
            continue
        if days == 0:
            prefix = "Сегодня заканчивается"
        elif days == 1:
            prefix = "Завтра заканчивается"
        else:
            prefix = f"Через {days} дня заканчивается"
        lines.append(f"• {prefix} подписка {row['name']} ({row['paid_until']})")
    if not lines:
        return None
    return "Напоминание об оплате:\n" + "\n".join(lines)


def maybe_send_daily_reminder() -> None:
    if ADMIN_ID is None:
        return
    now = datetime.now(LOCAL_TZ)
    if now.hour < 10:
        return
    today_text = now.date().isoformat()
    with db() as connection:
        row = connection.execute("SELECT value FROM settings WHERE key = 'last_daily_reminder'").fetchone()
        if row and row["value"] == today_text:
            return
    reminder = daily_reminder_text()
    if reminder:
        send(ADMIN_ID, reminder)
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('last_daily_reminder', ?)",
            (today_text,),
        )


def check_healthcheck_watchdog() -> None:
    timer_active = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", "vpn-healthcheck.timer"],
        check=False,
    ).returncode == 0
    state_file = Path("/var/lib/vpn-healthcheck/state.json")
    state_fresh = state_file.exists() and (time.time() - state_file.stat().st_mtime) <= 12 * 60
    current = "ok" if timer_active and state_fresh else "failed"
    with db() as connection:
        row = connection.execute(
            "SELECT value FROM settings WHERE key = 'healthcheck_watchdog_state'"
        ).fetchone()
        previous = row["value"] if row else None
    if current == "failed" and previous != "failed" and ADMIN_ID is not None:
        send(ADMIN_ID, "⚠️ Health-check VPN не работает или не обновлялся больше 12 минут.")
    elif current == "ok" and previous == "failed" and ADMIN_ID is not None:
        send(ADMIN_ID, "✅ Health-check VPN снова работает.")
    with db() as connection:
        connection.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('healthcheck_watchdog_state', ?)",
            (current,),
        )


def server_text() -> str:
    load_1m = os.getloadavg()[0]
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        memory[key] = int(value.strip().split()[0])
    available_mb = memory.get("MemAvailable", 0) // 1024
    disk = shutil.disk_usage("/")
    free_gb = disk.free / (1024 ** 3)
    return (
        "Сервер работает.\n"
        f"CPU load: {load_1m:.2f}\n"
        f"Доступно RAM: {available_mb} МБ\n"
        f"Свободно на диске: {free_gb:.1f} ГБ"
    )


def show_client_picker(chat_id: int) -> None:
    with db() as connection:
        rows = connection.execute(
            "SELECT name, status FROM clients WHERE status NOT IN ('refunded') ORDER BY vpn_ip"
        ).fetchall()
    keyboard = [[{"text": f"{row['name']} · {row['status']}", "callback_data": f"client:{row['name']}"}] for row in rows]
    send_inline(chat_id, "Выбери клиента:", keyboard)


def show_client(chat_id: int, name: str) -> None:
    with db() as connection:
        row = connection.execute(
            "SELECT name, vpn_ip, paid_until, status, note FROM clients WHERE name = ?", (name,)
        ).fetchone()
    if row is None:
        send(chat_id, "Клиент не найден.")
        return
    text = (
        f"Клиент: {row['name']}\n"
        f"VPN IP: {row['vpn_ip']}\n"
        f"До даты: {row['paid_until'] or 'без срока'}\n"
        f"Статус: {row['status']}"
    )
    keyboard = []
    if row["status"] == "disabled":
        keyboard.append([{"text": "▶️ Возобновить подписку", "callback_data": f"enable:{name}"}])
    elif row["status"] != "refunded" and name != "Abu":
        keyboard.append([{"text": "⛔ Остановить подписку", "callback_data": f"stop:{name}"}])
    if name != "Abu":
        keyboard.append([{"text": "🗑 Удалить полностью", "callback_data": f"delete:{name}"}])
    keyboard.append([{"text": "← Назад к клиентам", "callback_data": "clients"}])
    send_inline(chat_id, text, keyboard)


def handle_callback(query: dict) -> None:
    chat_id = int(query["message"]["chat"]["id"])
    callback_id = query["id"]
    data = query.get("data", "")
    if ADMIN_ID is None or chat_id != ADMIN_ID:
        api("answerCallbackQuery", callback_query_id=callback_id, text="Доступ запрещён", show_alert="true")
        return
    api("answerCallbackQuery", callback_query_id=callback_id)
    if data == "clients":
        show_client_picker(chat_id)
    elif data.startswith("client:"):
        show_client(chat_id, data.split(":", 1)[1])
    elif data.startswith("stop:"):
        name = data.split(":", 1)[1]
        send_inline(
            chat_id,
            f"Остановить подписку {name}? Его текущий конфиг перестанет подключаться.",
            [
                [{"text": "Да, остановить", "callback_data": f"confirm_stop:{name}"}],
                [{"text": "Отмена", "callback_data": f"client:{name}"}],
            ],
        )
    elif data.startswith("confirm_stop:"):
        name = data.split(":", 1)[1]
        try:
            subprocess.run(
                ["sudo", "/usr/local/sbin/vpn-client-access", "disable", name],
                check=True,
                timeout=15,
                capture_output=True,
                text=True,
            )
            with db() as connection:
                connection.execute(
                    "UPDATE clients SET status = 'disabled', note = 'Остановлено администратором' WHERE name = ?",
                    (name,),
                )
            send(chat_id, f"Доступ {name} остановлен. Конфиг сохранён для возможного восстановления.")
        except (subprocess.SubprocessError, OSError) as error:
            stderr = getattr(error, "stderr", "")
            logging.exception("Failed to disable client %s: %s", name, stderr)
            send(chat_id, f"Не удалось остановить {name}. Проверь журнал сервера.")
    elif data.startswith("enable:"):
        name = data.split(":", 1)[1]
        send_inline(
            chat_id,
            f"Возобновить доступ {name} с прежним конфигом?",
            [
                [{"text": "Да, возобновить", "callback_data": f"confirm_enable:{name}"}],
                [{"text": "Отмена", "callback_data": f"client:{name}"}],
            ],
        )
    elif data.startswith("confirm_enable:"):
        name = data.split(":", 1)[1]
        try:
            subprocess.run(
                ["sudo", "/usr/local/sbin/vpn-client-access", "enable", name],
                check=True,
                timeout=15,
                capture_output=True,
                text=True,
            )
            restored_status = "owner" if name in {"Abu", "Rayana"} else "active"
            with db() as connection:
                connection.execute(
                    "UPDATE clients SET status = ?, note = 'Доступ возобновлён' WHERE name = ?",
                    (restored_status, name),
                )
            send(chat_id, f"Доступ {name} возобновлён. Старый конфиг снова работает.")
        except (subprocess.SubprocessError, OSError) as error:
            stderr = getattr(error, "stderr", "")
            logging.exception("Failed to enable client %s: %s", name, stderr)
            send(chat_id, f"Не удалось возобновить {name}. Проверь журнал сервера.")
    elif data.startswith("delete:"):
        name = data.split(":", 1)[1]
        if name == "Abu":
            send(chat_id, "Этот профиль защищён от удаления.")
            return
        send_inline(
            chat_id,
            f"Полностью удалить {name}? Ключи и конфиг восстановить будет невозможно.",
            [
                [{"text": "Да, удалить полностью", "callback_data": f"confirm_delete:{name}"}],
                [{"text": "Отмена", "callback_data": f"client:{name}"}],
            ],
        )
    elif data.startswith("confirm_delete:"):
        name = data.split(":", 1)[1]
        if name == "Abu":
            send(chat_id, "Этот профиль защищён от удаления.")
            return
        try:
            subprocess.run(
                ["sudo", "/usr/local/sbin/vpn-client-access", "delete", name],
                check=True,
                timeout=15,
                capture_output=True,
                text=True,
            )
            with db() as connection:
                connection.execute("DELETE FROM clients WHERE name = ?", (name,))
            send(chat_id, f"{name} полностью удалён: доступ, ключи, конфиг и запись в базе.", show_menu=True)
        except (subprocess.SubprocessError, OSError) as error:
            stderr = getattr(error, "stderr", "")
            logging.exception("Failed to delete client %s: %s", name, stderr)
            send(chat_id, f"Не удалось удалить {name}. Проверь журнал сервера.")


def handle_message(message: dict) -> None:
    chat_id = int(message["chat"]["id"])
    full_text = message.get("text", "").strip() if message.get("text") else ""
    text = full_text.split()[0].lower() if full_text else ""
    if ADMIN_ID is None:
        send(chat_id, f"Бот ожидает привязку администратора.\nВаш Telegram ID: {chat_id}")
        return
    if chat_id != ADMIN_ID:
        send(chat_id, "Доступ запрещён.")
        return
    if PENDING_ACTIONS.get(chat_id) == "create_client" and not full_text.startswith("/"):
        name = full_text.strip()
        if not __import__("re").fullmatch(r"[A-Za-z0-9_-]{2,40}", name):
            send(chat_id, "Имя должно содержать 2–40 символов: латинские буквы, цифры, _ или -. Попробуй ещё раз или /cancel")
            return
        try:
            result = subprocess.run(
                ["sudo", "/usr/local/sbin/vpn-client-access", "create", name],
                check=True,
                timeout=20,
                capture_output=True,
                text=True,
            )
            created = json.loads(result.stdout)
            with db() as connection:
                connection.execute(
                    "INSERT INTO clients(name, vpn_ip, paid_until, status, telegram_id, note) VALUES (?, ?, NULL, 'active', NULL, 'Без срока')",
                    (created["name"], created["ip"]),
                )
            PENDING_ACTIONS.pop(chat_id, None)
            send_document(
                chat_id,
                Path(created["config"]),
                f"Профиль {created['name']} создан. VPN IP: {created['ip']}. Доступ без срока.",
            )
            Path(created["config"]).unlink(missing_ok=True)
            send(chat_id, "Клиент добавлен в базу.", show_menu=True)
        except (subprocess.SubprocessError, OSError, json.JSONDecodeError, sqlite3.Error) as error:
            stderr = getattr(error, "stderr", "")
            logging.exception("Failed to create client %s: %s", name, stderr)
            send(chat_id, f"Не удалось создать {name}. Возможно, такое имя уже существует. Введи другое имя или /cancel")
        return
    if text == "/cancel":
        PENDING_ACTIONS.pop(chat_id, None)
        send(chat_id, "Создание отменено.", show_menu=True)
        return
    if text in {"/start", "/help"}:
        send(chat_id, "VPN Manager работает. Выбери действие:", show_menu=True)
    elif text == "/clients" or full_text == "👥 Клиенты":
        show_client_picker(chat_id)
    elif text == "/expiring" or full_text == "⏳ Скоро оплата":
        send(chat_id, expiring_text())
    elif text == "/status" or full_text == "🖥 Состояние сервера":
        send(chat_id, server_text())
    elif full_text == "➕ Создать клиента":
        PENDING_ACTIONS[chat_id] = "create_client"
        send(chat_id, "Введи имя клиента латиницей, например: arman\n\nОтмена: /cancel")
    else:
        send(chat_id, "Неизвестная команда. Используй /help")


def main() -> None:
    initialize()
    offset = 0
    next_reminder_check = 0.0
    next_healthcheck_watchdog = 0.0
    logging.info("VPN Telegram bot started")
    while True:
        try:
            if time.monotonic() >= next_reminder_check:
                maybe_send_daily_reminder()
                next_reminder_check = time.monotonic() + 60
            if time.monotonic() >= next_healthcheck_watchdog:
                check_healthcheck_watchdog()
                next_healthcheck_watchdog = time.monotonic() + 5 * 60
            updates = api(
                "getUpdates",
                request_timeout=5,
                offset=offset,
                timeout=0,
                allowed_updates=json.dumps(["message", "callback_query"]),
            )
            if not updates:
                time.sleep(1)
            for update in updates:
                offset = update["update_id"] + 1
                if "message" in update:
                    logging.info("Received message update %s", update["update_id"])
                    handle_message(update["message"])
                elif "callback_query" in update:
                    logging.info("Received callback update %s", update["update_id"])
                    handle_callback(update["callback_query"])
        except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as error:
            logging.warning("Polling error: %s", error)
            time.sleep(5)
        except Exception:
            logging.exception("Unexpected bot error")
            time.sleep(5)


if __name__ == "__main__":
    main()
