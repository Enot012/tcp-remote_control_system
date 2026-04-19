import asyncio
import time
import os
import json
import signal
import sys
import re
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Any
from enum import StrEnum


# ═══════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════

class Config:
    """Централизованная конфигурация сервера"""
    BASE_DIR                = Path("home")
    DIR_SAVE                = BASE_DIR / "save"
    DIR_TRASH               = BASE_DIR / "trash"
    DIR_HISTORY             = BASE_DIR / "history"
    DIR_FILES               = BASE_DIR / "files"
    DIR_LOGS                = BASE_DIR / "logs"
    DIR_JSON                = BASE_DIR / "json"
    DIR_SCHEDULED_RESULTS   = BASE_DIR / "files" / "scheduled_commands"

    FILE_CODE       = BASE_DIR / "code.txt"
    FILE_USERS      = BASE_DIR / "users.json"
    FILE_STATE      = BASE_DIR / "server_state.json"
    FILE_CRASH_LOG  = BASE_DIR / "crash.log"
    FILE_GROUPS     = BASE_DIR / "json" / "groups.json"
    FILE_SCHEDULED  = BASE_DIR / "json" / "scheduled_commands.json"

    HOST = "0.0.0.0"
    PORT = 9000

    CHUNK_SIZE          = 65536   # 64 KB
    COMMAND_TIMEOUT     = 120     # 2 минуты
    WARNING_TIMEOUT     = 90
    STATE_SAVE_INTERVAL = 30
    READ_TIMEOUT        = 300     # 5 минут

    TIMEZONE_OFFSET = +1

class Command(StrEnum):
    CMD="CMD"
    SIMPL='simpl'
    EXPORT="export"
    IMPORT="import"

# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════

def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=Config.TIMEZONE_OFFSET)


def ensure_dirs():
    for d in [Config.DIR_SAVE, Config.DIR_TRASH, Config.DIR_HISTORY,
              Config.DIR_FILES, Config.DIR_LOGS, Config.DIR_JSON,
              Config.DIR_SCHEDULED_RESULTS]:
        os.makedirs(d, exist_ok=True)


def print_command():
    print("\n" + "=" * 80)
    print("ДОСТУПНЫЕ КОМАНДЫ:")
    print("  CMD <client|all> <команда>                       - Выполнить команду")
    print("  export <client|all> <path_client> [optional]     - Получить файлы с клиента")
    print("  import <client|all> <path_server> [path_client]  - Отправить файлы клиенту/всем")
    print("  save <client> <filename>                         - Сохранить результат")
    print("  simpl <client|all>                               - Выполнить команды из файла")
    print()
    print("  chart_new                                        - Создать отложенную команду")
    print("  chart_list                                       - Показать отложенные команды")
    print("  chart_del <index>                                - Удалить отложенную команду")
    print("  chart_comd                                       - Выполненные команды")
    print()
    print("  group_new <name>                                 - Создать группу")
    print("  group_list                                       - Показать группы")
    print("  group_del <name>                                 - Удалить группу")
    print("  group_add <name>                                 - Добавить пользователей в группу")
    print("  group_rm  <name>                                 - Удалить пользователей из группы")
    print()
    print("  list                                             - Список пользователей")
    print("  rename <user> <alias>                            - Переименовать пользователя")
    print("  status                                           - Активные команды")
    print("  cancel <client>                                  - Отменить команду")
    print("  kick <client|all>                                - Отключить клиента/всех")
    print("  help                                             - Вывести список команд")
    print("  EXIT                                             - Остановить сервер")
    print("=" * 80 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════

class Logger:
    """Централизованная система логирования"""

    @staticmethod
    def log(level: str, message: str, client_id: Optional[str] = None,
            show_console: bool = True):
        local_time = get_local_time()
        timestamp  = local_time.strftime("%Y-%m-%d %H:%M:%S")

        entry = f"[{timestamp}] [{level}]"
        if client_id:
            entry += f" [{client_id}]"
        entry += f" {message}\n"

        if show_console:
            print(entry.strip())

        log_file = Config.DIR_LOGS / f"{local_time.strftime('%Y-%m-%d')}.log"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass

    @staticmethod
    def crash(exception: Exception, traceback_str: str, state: "ServerState"):
        local_time = get_local_time()
        timestamp  = local_time.strftime("%Y-%m-%d %H:%M:%S")

        crash_info = (
            f"\n{'=' * 80}\n"
            f"КРИТИЧЕСКАЯ ОШИБКА СЕРВЕРА\n"
            f"Время: {timestamp}\n"
            f"Исключение: {type(exception).__name__}: {exception}\n"
            f"{'=' * 80}\n"
            f"{traceback_str}\n"
            f"{'=' * 80}\n\n"
            f"Состояние на момент сбоя:\n"
            f"- Подключено клиентов: {len(state.get_all_clients())}\n"
            f"- Активных команд: {len(state.get_all_commands())}\n"
            f"- Клиенты: {state.get_all_clients()}\n\n"
            f"Активные команды:\n"
        )
        for cid, info in state.get_all_commands().items():
            elapsed = time.time() - info["start_time"]
            crash_info += f"  - {cid}: {info['type']} ({elapsed:.1f}s) - {info['command']}\n"
        crash_info += f"\n{'=' * 80}\n\n"

        try:
            with open(Config.FILE_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(crash_info)
        except Exception:
            pass
        print(crash_info)


# ═══════════════════════════════════════════════════════════════════════════
# СОСТОЯНИЕ СЕРВЕРА
# ═══════════════════════════════════════════════════════════════════════════

class ServerState:
    """Единое хранилище runtime-состояния сервера"""

    def __init__(self):
        self._clients:             Dict[str, asyncio.StreamWriter] = {}
        self._output_buffers:      Dict[str, Dict[str, Any]]       = {}
        self._last_outputs:        Dict[str, Dict[str, Any]]       = {}
        self._active_commands:     Dict[str, Dict[str, Any]]       = {}
        self._scheduled_tracking:  Dict[str, list]                 = {}

    # ── clients ──────────────────────────────────────────────────────────

    def add_client(self, username: str, writer: asyncio.StreamWriter):
        self._clients[username] = writer
        self._output_buffers[username] = {"type": None, "lines": [], "chunks": 0, "total": 0}

    def remove_client(self, username: str):
        self._clients.pop(username, None)
        self._output_buffers.pop(username, None)
        self._scheduled_tracking.pop(username, None)

    def get_writer(self, username: str) -> Optional[asyncio.StreamWriter]:
        return self._clients.get(username)

    def get_all_clients(self) -> list:
        return list(self._clients.keys())

    def is_connected(self, username: str) -> bool:
        return username in self._clients

    # ── output buffers ───────────────────────────────────────────────────

    def init_buffer(self, username: str, buf_type: str, total: int = 0):
        self._output_buffers[username] = {
            "type": buf_type, "lines": [], "chunks": 0, "total": total
        }

    def append_chunk(self, username: str, chunk: str) -> bool:
        buf = self._output_buffers.get(username)
        if not buf:
            return False
        buf["lines"].append(chunk)
        buf["chunks"] += 1
        return True

    def get_buffer(self, username: str) -> Optional[Dict]:
        return self._output_buffers.get(username)

    def flush_buffer(self, username: str) -> Optional[str]:
        """Возвращает собранный вывод, сохраняет в last_outputs, очищает lines."""
        buf = self._output_buffers.get(username)
        if not buf:
            return None
        result = "\n".join(buf["lines"])
        self._last_outputs[username] = {
            "type":      buf["type"],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "content":   result,
        }
        buf["lines"] = []
        return result

    def clear_buffer(self, username: str):
        buf = self._output_buffers.get(username)
        if buf:
            buf["type"]   = None
            buf["lines"]  = []
            buf["chunks"] = 0

    def get_last_output(self, username: str) -> Optional[Dict]:
        return self._last_outputs.get(username)

    # ── active commands ──────────────────────────────────────────────────

    def register_command(self, username: str, command: str,
                         cmd_type: str, cmd_count: int = 1):
        self._active_commands[username] = {
            "start_time":         time.time(),
            "command":            command,
            "type":               cmd_type,
            "total_commands":     cmd_count,
            "received_commands":  0,
            "accumulated_output": [],
        }
        Logger.log("CMD_START", f"{cmd_type}: {command}", username, show_console=False)

    def unregister_command(self, username: str) -> bool:
        info = self._active_commands.pop(username, None)
        if info:
            elapsed = time.time() - info["start_time"]
            Logger.log("CMD_END", f"{info['type']} завершена за {elapsed:.1f}s",
                       username, show_console=False)
            return True
        return False

    def get_command(self, username: str) -> Optional[Dict]:
        return self._active_commands.get(username)

    def get_all_commands(self) -> Dict:
        return dict(self._active_commands)

    def has_command(self, username: str) -> bool:
        return username in self._active_commands

    # ── scheduled tracking ───────────────────────────────────────────────

    def push_scheduled(self, username: str, cmd_index: int):
        self._scheduled_tracking.setdefault(username, []).append(cmd_index)

    def pop_scheduled(self, username: str) -> Optional[int]:
        queue = self._scheduled_tracking.get(username)
        return queue.pop(0) if queue else None

    def has_scheduled(self, username: str) -> bool:
        return bool(self._scheduled_tracking.get(username))

    # ── persistence ──────────────────────────────────────────────────────

    def save(self) -> bool:
        try:
            state = {
                "timestamp":        time.time(),
                "datetime":         get_local_time().strftime("%Y-%m-%d %H:%M:%S"),
                "connected_clients": self.get_all_clients(),
                "active_commands": {
                    cid: {
                        "command": info.get("command", "unknown"),
                        "type":    info["type"],
                        "start_time": info["start_time"],
                        "elapsed": time.time() - info["start_time"],
                    }
                    for cid, info in self._active_commands.items()
                },
                "output_buffers": {
                    cid: {"type": buf["type"], "chunks": buf["chunks"], "total": buf["total"]}
                    for cid, buf in self._output_buffers.items()
                },
            }
            with open(Config.FILE_STATE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения состояния: {e}", show_console=False)
            return False

    @staticmethod
    def load() -> Optional[Dict]:
        if not Config.FILE_STATE.exists():
            return None
        try:
            with open(Config.FILE_STATE, "r", encoding="utf-8") as f:
                state = json.load(f)
            if time.time() - state["timestamp"] > 600:
                return None
            return state
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛИ
# ═══════════════════════════════════════════════════════════════════════════

class UserManager:
    """Управление пользователями и их историей"""

    __CYRILLIC_MAP = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
    }

    def __init__(self):
        self._cache: Optional[Dict] = None

    # ── internal I/O ─────────────────────────────────────────────────────

    def _load(self) -> Dict:
        if self._cache is None:
            try:
                with open(Config.FILE_USERS, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except Exception:
                self._cache = {"users": {}}
        return self._cache

    def _save(self) -> bool:
        if self._cache is None:
            return False
        try:
            tmp = Config.FILE_USERS.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            tmp.replace(Config.FILE_USERS)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения пользователей: {e}")
            self._cache = None
            return False

    # ── helpers ──────────────────────────────────────────────────────────

    def _transliterate(self, username: str) -> str:
        result = []
        for ch in username:
            if 'а' <= ch.lower() <= 'я' or ch in 'ЁёЪъЬь':
                trans = self.__CYRILLIC_MAP.get(ch.lower(), ch)
                if ch.isupper() and trans:
                    trans = trans.capitalize()
                result.append(trans)
            else:
                result.append(ch)
        return ''.join(result).replace(" ", "_")

    def _make_alias(self, username: str, users: Dict) -> str:
        base  = self._transliterate(username)[:10].replace(' ', '_')
        alias = base
        n = 2
        while alias in users:
            alias = f"{base}_{n}"
            n += 1
        return alias

    def _validate_name(self, name: str) -> str:
        name = name.strip().lower()
        if not name:
            raise ValueError("Имя не может быть пустым")
        if len(name) > 32:
            raise ValueError("Имя не длиннее 32 символов")
        if not re.match(r'^[\w\-\.]+$', name):
            raise ValueError(f"Недопустимые символы: {name}")
        return name

    # ── public API ───────────────────────────────────────────────────────

    def register(self, username: str, os_name: str, home_path: str) -> str:
        username = self._validate_name(username)
        users    = self._load()["users"]
        time_now = time.strftime("%Y-%m-%d %H:%M:%S")

        # Ищем по home_path (переподключение)
        for existing, info in users.items():
            if info.get("default_path") == home_path:
                info["status"]     = "ON"
                info["last_login"] = time_now
                self._save()
                self._log_session(existing, "login")
                Logger.log("INFO", f"Вход: {existing} ({info['alias']}) [path: {home_path}]")
                return info["alias"]

        # Новый пользователь
        alias = self._make_alias(username, users)
        users[alias] = {
            "alias":          alias,
            "status":         "ON",
            "OS":             os_name,
            "default_path":   home_path,
            "users_in_group": [],
            "last_login":     time_now,
            "last_logout":    None,
        }
        self._save()
        self._log_session(alias, "login")
        Logger.log("INFO", f"Новый пользователь: {alias}")
        return alias

    def logout(self, username: str):
        users    = self._load()["users"]
        time_now = time.strftime("%Y-%m-%d %H:%M:%S")

        if username in users:
            users[username]["status"]      = "OFF"
            users[username]["last_logout"] = time_now
            self._save()
            self._log_session(username, "logout")
            Logger.log("INFO", f"Выход: {username} ({users[username]['alias']})")

    def validate_users(self, username: str) -> Optional[str]:
        """Возвращает username если найден (по username или alias), иначе None."""
        users = self._load().get("users", {})
        if username in users:
            return username
        for uname, info in users.items():
            if info["alias"].lower() == username.lower():
                return uname
        return None

    def get_users_data(self) -> Dict:
        return self._load().get("users", {})

    def get_user_info(self, username: str) -> Optional[Dict]:
        return self._load()["users"].get(username)

    def get_all_usernames(self) -> list:
        return list(self._load().get("users", {}).keys())

    def save_user_data(self, user_data: Dict) -> bool:
        if not isinstance(user_data, dict):
            Logger.log("ERROR", "users должен быть словарём")
            return False
        required = {"alias", "status", "OS", "default_path",
                    "users_in_group", "last_login", "last_logout"}
        for uname, info in user_data.items():
            if not required.issubset(info.keys()):
                raise ValueError(f"Пользователь '{uname}': отсутствуют обязательные поля")
            if info["status"] not in ("ON", "OFF"):
                raise ValueError(f"Пользователь '{uname}': некорректный статус")
        self._cache["users"] = user_data
        return self._save()

    def _log_session(self, username: str, action: str):
        alias        = self._load()["users"].get(username, {}).get("alias", username)
        history_file = Config.DIR_HISTORY / f"{username}.json"

        if history_file.exists():
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = {"username": username, "alias": alias, "sessions": []}
        else:
            history = {"username": username, "alias": alias, "sessions": []}

        time_now = time.strftime("%Y-%m-%d %H:%M:%S")

        if action == "login":
            history["sessions"].append({"login": time_now, "logout": None})
        elif action == "logout":
            for session in reversed(history["sessions"]):
                if session["logout"] is None:
                    session["logout"] = time_now
                    break

        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            Logger.log("ERROR", f"Ошибка записи истории {alias}: {e}", show_console=False)


# ═══════════════════════════════════════════════════════════════════════════
# ГРУППЫ
# ═══════════════════════════════════════════════════════════════════════════

class GroupManager:
    """Управление группами пользователей"""

    def __init__(self, user_mgr: UserManager):
        self._cache:    Optional[Dict] = None
        self._user_mgr: UserManager    = user_mgr

    def _load(self) -> Dict:
        if self._cache is None:
            try:
                with open(Config.FILE_GROUPS, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except Exception:
                self._cache = {}
        return self._cache

    def _save(self) -> bool:
        if self._cache is None:
            return False
        try:
            tmp = Config.FILE_GROUPS.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            tmp.replace(Config.FILE_GROUPS)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения групп: {e}")
            self._cache = None
            return False

    def get_group_members(self, group_name: str) -> Optional[list]:
        return self._load().get(group_name)

    def get_all_group_names(self) -> list:
        return list(self._load().keys())

    def get_group_data(self) -> Dict:
        return dict(self._load())

    def save_group_data(self, group_data: Dict) -> bool:
        if not isinstance(group_data, dict):
            Logger.log("ERROR", "group_data должен быть словарём")
            return False
        for name, members in group_data.items():
            if not isinstance(members, list):
                raise ValueError(f"Группа '{name}': members должен быть списком")
        self._cache = group_data
        return self._save()

    def create_group(self, group_name: str, users_list: list) -> bool:
        if not isinstance(group_name, str) or not isinstance(users_list, list):
            raise TypeError("group_name — str, users_list — list")
        groups    = self.get_group_data()
        user_data = self._user_mgr.get_users_data()
        if group_name in groups:
            raise ValueError(f"Группа '{group_name}' уже существует")
        groups[group_name] = users_list
        for user in users_list:
            if group_name not in user_data[user]["users_in_group"]:
                user_data[user]["users_in_group"].append(group_name)
        self._user_mgr.save_user_data(user_data)
        return self.save_group_data(groups)

    def delete_group(self, group_name: str) -> bool:
        groups    = self.get_group_data()
        user_data = self._user_mgr.get_users_data()
        if group_name not in groups:
            raise NameError(f"Группа '{group_name}' не найдена")
        for user in groups[group_name]:
            if user in user_data and group_name in user_data[user]["users_in_group"]:
                user_data[user]["users_in_group"].remove(group_name)
        del groups[group_name]
        self.save_group_data(groups)
        self._user_mgr.save_user_data(user_data)
        return True

    def add_users_to_group(self, group_name: str, users_list: list) -> list:
        """Добавляет пользователей. Возвращает список тех, кого не добавили."""
        if group_name not in self._load():
            raise NameError(f"Группа '{group_name}' не найдена")
        user_data = self._user_mgr.get_users_data()
        not_added = []
        for user in users_list:
            if user in self._load()[group_name]:
                not_added.append(user)
                continue
            self._load()[group_name].append(user)
            if group_name not in user_data[user]["users_in_group"]:
                user_data[user]["users_in_group"].append(group_name)
        self._save()
        self._user_mgr.save_user_data(user_data)
        return not_added

    def remove_users_from_group(self, group_name: str, users_list: list) -> list:
        """Удаляет пользователей. Возвращает список тех, кого не нашли."""
        if group_name not in self._load():
            raise NameError(f"Группа '{group_name}' не найдена")
        user_data = self._user_mgr.get_users_data()
        not_removed = []
        for user in users_list:
            if user not in self._load()[group_name]:
                not_removed.append(user)
                continue
            self._load()[group_name].remove(user)
            if group_name in user_data.get(user, {}).get("users_in_group", []):
                user_data[user]["users_in_group"].remove(group_name)
        self._save()
        self._user_mgr.save_user_data(user_data)
        return not_removed


# ═══════════════════════════════════════════════════════════════════════════
# ОТЛОЖЕННЫЕ КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════════════

class ScheduledCommandManager:
    """Управление отложенными командами"""

    _VALID_TYPES = {"CMD", "SIMPL", "IMPORT", "EXPORT"}

    def __init__(self, user_mgr: UserManager, group_mgr: GroupManager):
        self._cache:     Optional[Dict] = None
        self._user_mgr:  UserManager    = user_mgr
        self._group_mgr: GroupManager   = group_mgr

    def _load(self) -> Dict:
        if self._cache is None:
            try:
                with open(Config.FILE_SCHEDULED, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
                self._cache.setdefault("completed", [])
            except Exception:
                self._cache = {"commands": [], "completed": []}
        return self._cache

    def _save(self) -> bool:
        if self._cache is None:
            return False
        try:
            tmp = Config.FILE_SCHEDULED.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            tmp.replace(Config.FILE_SCHEDULED)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения отложенных команд: {e}")
            self._cache = None
            return False

    def _resolve_expected_users(self, target: str) -> list:
        if target == "ALL":
            return self._user_mgr.get_all_usernames()
        if target.startswith("group:"):
            members = self._group_mgr.get_group_members(target[6:])
            return members if members else []
        return [target]

    # ── public API ───────────────────────────────────────────────────────

    def get_all_commands(self) -> list:
        return self._load().get("commands", [])

    def get_command(self, index: int) -> Optional[Dict]:
        commands = self._load().get("commands", [])
        if 0 <= index < len(commands):
            return commands[index]
        return None

    def create_command(self, target: str, cmd_type: str, extra: Dict) -> bool:
        """
        extra для CMD:    {"command": "..."}
        extra для SIMPL:  {}
        extra для IMPORT/EXPORT: {"source_path": "...", "dest_path": "..."}
        """
        cmd_type = cmd_type.upper()
        if cmd_type not in self._VALID_TYPES:
            raise ValueError(f"Неверный тип команды: {cmd_type}")

        if cmd_type == "CMD" and "command" not in extra:
            raise ValueError("Для CMD требуется поле 'command'")
        if cmd_type in ("IMPORT", "EXPORT") and not {"source_path", "dest_path"}.issubset(extra):
            raise ValueError("Для IMPORT/EXPORT требуются поля 'source_path' и 'dest_path'")

        if target != "ALL" and not target.startswith("group:"):
            resolved = self._user_mgr.validate_users(target)
            if not resolved:
                raise NameError(f"Пользователь '{target}' не найден")
            target = resolved


        command_data = {
            "target":           target,
            "command_type":     cmd_type,
            "created_at":       time.strftime("%Y-%m-%d %H:%M:%S"),
            "expected_users":   self._resolve_expected_users(target),
            "completed_users":  [],
        }
        command_data.update(extra)

        self._load()["commands"].append(command_data)
        return self._save()

    def delete_command(self, index: int) -> bool:
        commands = self._load().get("commands", [])
        if not (0 <= index < len(commands)):
            raise IndexError(f"Неверный индекс: {index}")
        commands.pop(index)
        return self._save()

    def get_commands_for_user(self, username: str) -> list:
        return [cmd for cmd in self._load().get("commands", [])
                if username in cmd.get("expected_users", [])]

    def mark_completed(self, cmd_index: int, username: str, output: str) -> bool:
        commands = self._load().get("commands", [])
        if cmd_index >= len(commands):
            Logger.log("WARNING", f"mark_completed: индекс {cmd_index} вне диапазона")
            return False

        cmd = commands[cmd_index]

        if username in cmd.get("expected_users", []):
            cmd["expected_users"].remove(username)
            cmd.setdefault("completed_users", []).append(username)

        self._write_output(cmd["target"], username, output)

        if not cmd["expected_users"]:
            cmd["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._load()["completed"].append(cmd)
            commands.pop(cmd_index)

        return self._save()

    def _write_output(self, target: str, username: str, output: str):
        if target == "ALL":
            filename = "ALL.txt"
        elif target.startswith("group:"):
            filename = f"group_{target[6:]}.txt"
        else:
            filename = f"{target}.txt"

        filepath = Config.DIR_SCHEDULED_RESULTS / filename
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"{username}\n{output}\n\n\n")
        except Exception as e:
            Logger.log("ERROR", f"Ошибка записи вывода: {e}")

    @staticmethod
    def replace_user_path(text: str, username: str, user_mgr: "UserManager") -> str:
        if "{path}" not in text:
            return text
        info = user_mgr.get_user_info(username)
        path = info.get("default_path", "") if info else ""
        if not path:
            Logger.log("WARNING", f"default_path не найден для {username}")
        return text.replace("{path}", path)

    @staticmethod
    def replace_user_placeholder(text: str, username: str) -> str:
        return text.replace("{user}", username)


# ═══════════════════════════════════════════════════════════════════════════
# МОНИТОРИНГ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class CommandMonitor:
    """Мониторинг таймаутов и сохранение вывода команд"""

    def __init__(self, state: ServerState, user_mgr: UserManager):
        self._state:    ServerState = state
        self._user_mgr: UserManager = user_mgr

    def save_command_output(self, client_id: str, command: str,
                            output: str, cmd_type: str):
        """Сохраняет вывод команды в файл trash/<alias>_output.txt"""
        try:
            info  = self._user_mgr.get_user_info(client_id)
            alias = info["alias"] if info else client_id

            exec_time = get_local_time().strftime("%Y-%m-%d %H:%M:%S")
            content   = (
                f"Время выполнения: {exec_time}\n"
                f"Тип: {cmd_type}\n"
                f"Команда: {command}\n"
                f"{'=' * 80}\n"
                f"{output}\n"
                f"{'=' * 80}\n\n"
            )

            filepath = Config.DIR_TRASH / f"output_command_{alias}.txt"
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(content)

            Logger.log("SAVE_OUTPUT", f"Вывод сохранён → {filepath.name}",
                       client_id, show_console=False)
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения вывода: {e}", client_id)

    async def monitor_loop(self):
        """Цикл мониторинга таймаутов"""
        warned: set = set()

        while True:
            await asyncio.sleep(5)
            now = time.time()

            for client_id, info in list(self._state.get_all_commands().items()):
                elapsed = now - info["start_time"]

                if elapsed > Config.WARNING_TIMEOUT and client_id not in warned:
                    remaining = Config.COMMAND_TIMEOUT - elapsed
                    Logger.log("WARNING",
                               f"команда {elapsed:.0f}s, осталось {remaining:.0f}s",
                               client_id, show_console=False)
                    writer = self._state.get_writer(client_id)
                    if writer:
                        try:
                            msg = (f"\n Команда выполняется {elapsed:.0f}s. "
                                   f"Осталось {remaining:.0f}s.\n")
                            writer.write(msg.encode("utf-8"))
                            await writer.drain()
                        except Exception:
                            pass
                    warned.add(client_id)

                if elapsed > Config.COMMAND_TIMEOUT:
                    Logger.log("TIMEOUT",
                               f"команда превысила лимит: {info['command']}",
                               client_id,show_console=False)
                    writer = self._state.get_writer(client_id)
                    if writer:
                        try:
                            writer.write(b"CMD:CANCEL_TIMEOUT\n")
                            await writer.drain()
                        except Exception:
                            pass
                    self._state.unregister_command(client_id)
                    self._state.clear_buffer(client_id)
                    warned.discard(client_id)


# ═══════════════════════════════════════════════════════════════════════════
# ПЕРЕДАЧА ФАЙЛОВ
# ═══════════════════════════════════════════════════════════════════════════

class FileTransfer:
    """Управление передачей файлов"""

    @staticmethod
    def list_files(path: Path) -> list:
        if not path.exists():
            return []
        if path.is_file():
            return [{"path": str(path), "rel_path": path.name, "size": path.stat().st_size}]
        result = []
        for item in path.rglob("*"):
            if item.is_file():
                try:
                    result.append({
                        "path":     str(item),
                        "rel_path": str(item.relative_to(path)),
                        "size":     item.stat().st_size,
                    })
                except Exception as e:
                    Logger.log("ERROR", f"Ошибка обработки {item}: {e}", show_console=False)
        return result

    @staticmethod
    async def send_file(writer: asyncio.StreamWriter, file_path: str,
                        rel_path: str, size: int) -> bool:
        try:
            meta = json.dumps({"rel_path": rel_path, "size": size})
            writer.write(f"FILE:META:{meta}\n".encode("utf-8"))
            await writer.drain()

            sent = 0
            with open(file_path, "rb") as f:
                while chunk := f.read(Config.CHUNK_SIZE):
                    writer.write(chunk)
                    await writer.drain()
                    sent += len(chunk)
                    pct = sent * 100 // size if size else 100
                    print(f"\r  {rel_path}: {pct}% ", end="", flush=True)

            print(f"\r  {rel_path} ({size / 1024:.1f} KB)")
            writer.write(b"FILE:END\n")
            await writer.drain()
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка отправки {rel_path}: {e}")
            return False

    @staticmethod
    async def receive_file(reader: asyncio.StreamReader,
                           dest_path: Path, size: int) -> bool:
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            received = 0
            with open(dest_path, "wb") as f:
                while received < size:
                    chunk = await reader.read(min(Config.CHUNK_SIZE, size - received))
                    if not chunk:
                        raise ConnectionError("Разрыв соединения")
                    f.write(chunk)
                    received += len(chunk)
                    pct = received * 100 // size if size else 100
                    print(f"\r  {dest_path.name}: {pct}% ", end="", flush=True)

            print(f"\r  {dest_path.name} ({size / 1024:.1f} KB)")

            end_marker = await reader.readline()
            if not end_marker.decode("utf-8", errors="ignore").strip().startswith("FILE:END"):
                Logger.log("WARNING", "Неожиданный маркер FILE:END", show_console=False)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка получения файла: {e}")
            return False

    @staticmethod
    async def import_to_client(client_id: str, source_path: str,
                               dest_dir: str, state: ServerState) -> bool:
        writer = state.get_writer(client_id)
        if not writer:
            print(f" Клиент {client_id} не подключен")
            return False

        path = Path(source_path)
        if not path.exists():
            print(f" Путь не существует: {source_path}")
            return False

        files = FileTransfer.list_files(path)
        if not files:
            print(" Нет файлов для передачи")
            return False

        metadata = {"count": len(files), "dest_dir": dest_dir, "source": path.name}
        writer.write(f"IMPORT:START:{json.dumps(metadata)}\n".encode("utf-8"))
        await writer.drain()

        total_size = sum(f["size"] for f in files)
        print(f" Отправка {len(files)} файлов ({total_size / 1024 / 1024:.2f} MB)")

        for fi in files:
            if not await FileTransfer.send_file(writer, fi["path"], fi["rel_path"], fi["size"]):
                return False

        print(" Ожидание подтверждения...")
        return True


# ═══════════════════════════════════════════════════════════════════════════
# КИК
# ═══════════════════════════════════════════════════════════════════════════

class BanManager:
    def __init__(self, state: ServerState):
        self._state = state

    async def kick_user(self, username: str, reason: str = "Отключен администратором") -> bool:
        writer = self._state.get_writer(username)
        if not writer:
            return False
        try:
            writer.write(f"KICK:{reason}\n".encode("utf-8"))
            await writer.drain()
            await asyncio.sleep(0.5)
            writer.close()
            await writer.wait_closed()
            Logger.log("KICK", f"{username} отключен. Причина: {reason}")
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка kick {username}: {e}")
            return False







# ═══════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════


def ask_who(msg: str, user_mgr: UserManager, group_mgr: GroupManager):
    if not msg:
        print(" Поле не может быть пустым")
        return False
    if msg.upper() == "EXIT":
        return "EXIT"
    if msg.startswith("group:"):
        name = msg[6:]
        if group_mgr.get_group_members(name):
            return msg
        print(f" Группа '{name}' не существует")
        return False
    if msg.upper() == "ALL":
        return "ALL"
    real = user_mgr.validate_users(msg)
    if real:
        return real
    print(f" Пользователь '{msg}' не найден")
    return False


def ask_cmd(cmd_type: str) -> Any:
    if cmd_type == "CMD":
        while True:
            command = input("Команда (EXIT — отмена): ").strip()
            if command.upper() == "EXIT":
                return "EXIT"
            if command:
                return {"command": command}
            print(" Команда не может быть пустой")

    elif cmd_type == "SIMPL":
        if not Config.FILE_CODE.exists():
            print(f" Файл {Config.FILE_CODE} не найден")
            return "EXIT"
        print(f" Будут выполнены команды из {Config.FILE_CODE}")
        return {}

    elif cmd_type == "IMPORT":
        print("Будьте внимательны в указании слеша!")
        while True:
            src = input("Путь на сервере (EXIT — отмена): ").strip()
            if src.upper() == "EXIT":
                return "EXIT"
            if not src:
                print(" Путь не может быть пустым")
                continue
            if not Path(src).exists():
                print(f" Путь '{src}' не существует")
                continue
            break
        while True:
            dst = input("Путь на клиенте (EXIT — отмена): ").strip()
            if dst.upper() == "EXIT":
                return "EXIT"
            if dst:
                return {"source_path": src, "dest_path": dst}
            print(" Путь не может быть пустым")

    elif cmd_type == "EXPORT":
        while True:
            src = input("Путь на клиенте (EXIT — отмена): ").strip()
            if src.upper() == "EXIT":
                return "EXIT"
            if src:
                break
            print(" Путь не может быть пустым")
        dst = input("Путь на сервере [не обязателен]: ").strip()
        if dst.upper() == "EXIT":
            return "EXIT"
        return {"source_path": src, "dest_path": dst or "received"}

    return "EXIT"


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТКА КЛИЕНТА
# ═══════════════════════════════════════════════════════════════════════════

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        state: ServerState, user_mgr: UserManager,
                        sched_mgr: ScheduledCommandManager,
                        monitor: CommandMonitor):
    addr      = writer.get_extra_info("peername")
    client_id = None

    try:
        raw  = await asyncio.wait_for(reader.readline(), timeout=10)
        parts = raw.decode("utf-8").strip().split(",")
        client_id = parts[0]

        if not client_id:
            writer.close()
            await writer.wait_closed()
            return

        alias = user_mgr.register(client_id, parts[1], parts[2])
        state.add_client(client_id, writer)
        Logger.log("CONNECT", f"подключился ({addr})", client_id)
        state.save()

        # ── отложенные команды ─────────────────────────────────────────
        user_cmds   = sched_mgr.get_commands_for_user(client_id)
        all_commands = sched_mgr.get_all_commands()

        for cmd_data in user_cmds:
            try:
                cmd_index = all_commands.index(cmd_data)
                cmd_type  = cmd_data["command_type"]

                def _replace(text: str) -> str:
                    return ScheduledCommandManager.replace_user_path(
                        ScheduledCommandManager.replace_user_placeholder(text, client_id),
                        client_id, user_mgr
                    )

                if cmd_type == "CMD":
                    command = _replace(cmd_data["command"])
                    state.register_command(client_id, command, "CMD", 1)
                    writer.write(f"CMD:{command}\n".encode("utf-8"))
                    await writer.drain()
                    state.push_scheduled(client_id, cmd_index)

                elif cmd_type == "SIMPL":
                    if Config.FILE_CODE.exists():
                        commands = [l.strip() for l in Config.FILE_CODE.read_text("utf-8").splitlines() if l.strip()]
                        if commands:
                            state.register_command(client_id, f"simpl ({len(commands)} команд)", "FILETRU", len(commands))
                            for cmd in commands:
                                writer.write(f"FILETRU:{_replace(cmd)}\n".encode("utf-8"))
                                await writer.drain()
                                await asyncio.sleep(0.2)

                            state.push_scheduled(client_id, cmd_index)


                elif cmd_type == "IMPORT":
                    src = _replace(cmd_data["source_path"])
                    dst = _replace(cmd_data["dest_path"])
                    state.register_command(client_id, f"import {src}", "IMPORT", 1)
                    await FileTransfer.import_to_client(client_id, src, dst, state)
                    state.unregister_command(client_id)
                    sched_mgr.mark_completed(cmd_index, client_id, f"IMPORT: {src} → {dst} [OK]")

                elif cmd_type == "EXPORT":
                    src = _replace(cmd_data["source_path"])
                    dst = _replace(cmd_data["dest_path"])
                    state.register_command(client_id, f"export {src}", "EXPORT", 1)
                    writer.write(f"EXPORT;{src};{dst}\n".encode("utf-8"))
                    await writer.drain()
                    state.push_scheduled(client_id, cmd_index)

                await asyncio.sleep(0.3)
            except Exception as e:
                Logger.log("ERROR", f"Ошибка отложенной команды: {e}", client_id)

        # ── основной цикл ──────────────────────────────────────────────
        consecutive_errors = 0
        MAX_ERRORS         = 5

        while True:
            try:
                data = await asyncio.wait_for(reader.readline(), timeout=Config.READ_TIMEOUT)
                if not data:
                    break

                msg = data.decode("utf-8", errors="ignore").strip()
                if not msg:
                    continue
                consecutive_errors = 0

                # ── EXPORT ────────────────────────────────────────────
                if msg.startswith("EXPORT:START:"):
                    try:
                        metadata = json.loads(msg[13:])
                        count    = metadata["count"]
                        dest_dir = metadata.get("dest_dir", "received")

                        info  = user_mgr.get_user_info(client_id)
                        alias = info["alias"] if info else client_id

                        save_dir = Path(Config.DIR_FILES) / alias / dest_dir
                        print(f" Получение {count} файлов от {alias} в {save_dir}")

                        for _ in range(count):
                            meta_line = await asyncio.wait_for(reader.readline(), timeout=30)
                            meta_str  = meta_line.decode("utf-8", errors="ignore").strip()
                            if not meta_str.startswith("FILE:META:"):
                                break
                            file_meta = json.loads(meta_str[10:])
                            save_path = save_dir / file_meta["rel_path"]
                            if not await FileTransfer.receive_file(reader, save_path, file_meta["size"]):
                                writer.write(b"EXPORT:ABORT\n")
                                await writer.drain()
                                break

                        confirm = await asyncio.wait_for(reader.readline(), timeout=10)
                        if confirm.decode("utf-8", errors="ignore").strip() == "EXPORT:COMPLETE":
                            Logger.log("EXPORT", "✓ Экспорт завершён", client_id)
                            if state.has_scheduled(client_id):
                                idx = state.pop_scheduled(client_id)
                                sched_mgr.mark_completed(idx, client_id, f"EXPORT: {count} файлов [OK]")
                            state.unregister_command(client_id)
                    except Exception as e:
                        Logger.log("ERROR", f"Ошибка EXPORT: {e}", client_id)
                        consecutive_errors += 1
                    continue

                # ── IMPORT ────────────────────────────────────────────
                if msg == "IMPORT:COMPLETE":
                    Logger.log("IMPORT", "✓ Импорт завершён", client_id)
                    state.unregister_command(client_id)
                    continue

                if msg.startswith("IMPORT:ERROR:"):
                    Logger.log("ERROR", f"Импорт: {msg[13:]}", client_id)
                    state.unregister_command(client_id)
                    continue

                # ── OUTPUT chunks ─────────────────────────────────────
                if msg.startswith("OUTPUT:START:"):
                    try:
                        total = int(msg.split(":")[-1])
                    except (ValueError, IndexError):
                        total = 0
                    state.init_buffer(client_id, "OUTPUT", total)
                    continue

                if msg.startswith("OUTPUT:CHUNK:"):
                    state.append_chunk(client_id, msg[13:].replace("<<<NL>>>", "\n"))
                    continue

                if msg == "OUTPUT:END":
                    full_output = state.flush_buffer(client_id)
                    print(f"\n{'=' * 80}\n[РЕЗУЛЬТАТ от {client_id}]\n{'=' * 80}")
                    print(full_output)
                    print(f"{'=' * 80}\n")

                    cmd_info = state.get_command(client_id)
                    if cmd_info:
                        cmd_info["accumulated_output"].append(full_output)
                        cmd_info["received_commands"] += 1

                        if cmd_info["received_commands"] >= cmd_info["total_commands"]:
                            combined = "\n\n".join(cmd_info["accumulated_output"])
                            monitor.save_command_output(
                                client_id, cmd_info["command"], combined, cmd_info["type"]
                            )
                            if state.has_scheduled(client_id):
                                idx = state.pop_scheduled(client_id)
                                sched_mgr.mark_completed(idx, client_id, combined)
                            state.unregister_command(client_id)
                            state.clear_buffer(client_id)
                        else:
                            # SIMPL: ещё ждём
                            buf = state.get_buffer(client_id)
                            if buf:
                                buf["lines"] = []
                    continue

                # ── FILETRU chunks ────────────────────────────────────
                if msg.startswith("FILETRU:START:"):
                    try:
                        total = int(msg.split(":")[-1])
                    except (ValueError, IndexError):
                        total = 0
                    state.init_buffer(client_id, "FILETRU", total)
                    continue

                if msg.startswith("FILETRU:CHUNK:"):
                    state.append_chunk(client_id, msg[14:].replace("<<<NL>>>", "\n"))
                    continue

                if msg == "FILETRU:END":
                    full_output = state.flush_buffer(client_id)
                    print(f"\n{'=' * 80}\n[FILETRU от {client_id}]\n{'=' * 80}")
                    print(full_output)
                    print(f"{'=' * 80}\n")

                    cmd_info = state.get_command(client_id)
                    if cmd_info:
                        cmd_info["accumulated_output"].append(full_output)
                        cmd_info["received_commands"] += 1
                        Logger.log("DEBUG",
                                   f"FILETRU: {cmd_info['received_commands']}/{cmd_info['total_commands']}",
                                   client_id)

                        if cmd_info["received_commands"] >= cmd_info["total_commands"]:
                            combined = "\n\n".join(cmd_info["accumulated_output"])
                            monitor.save_command_output(
                                client_id, cmd_info["command"], combined, "FILETRU"
                            )
                            if state.has_scheduled (client_id):
                                idx = state.pop_scheduled (client_id)
                                sched_mgr.mark_completed (idx, client_id, combined)
                            state.unregister_command (client_id)
                    state.clear_buffer(client_id)
                    continue

                Logger.log("WARNING", f"Неизвестное сообщение: {msg[:60]}",
                           client_id, show_console=False)

            except asyncio.TimeoutError:
                Logger.log("WARNING", "Таймаут чтения (5 мин)", client_id, show_console=False)
                continue
            except ConnectionError as e:
                Logger.log("INFO", f"Проблема соединения: {e}", client_id, show_console=False)
                break
            except Exception as e:
                Logger.log("ERROR", f"Ошибка в цикле: {e}", client_id)
                consecutive_errors += 1
                if consecutive_errors >= MAX_ERRORS:
                    Logger.log("ERROR", "Слишком много ошибок, отключение", client_id)
                    break

    except Exception as e:
        import traceback
        Logger.log("ERROR", f"Критическая ошибка клиента: {e}", client_id)
        Logger.crash(e, traceback.format_exc(), state)

    finally:
        if client_id:
            state.remove_client(client_id)
            state.unregister_command(client_id)
            user_mgr.logout(client_id)
            Logger.log("DISCONNECT", "отключился", client_id)
            state.save()
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# СЕРВЕРНЫЙ ИНТЕРФЕЙС
# ═══════════════════════════════════════════════════════════════════════════

async def server_input(server, state: ServerState, user_mgr: UserManager,
                       group_mgr: GroupManager, sched_mgr: ScheduledCommandManager,
                       ban_mgr: BanManager):
    loop = asyncio.get_event_loop()

    while True:
        msg = await loop.run_in_executor(None, input, "Server> ")
        if not msg:
            continue

        parts  = msg.split()
        part_0 = parts[0].upper() if parts else ""

        # ── EXIT ──────────────────────────────────────────────────────
        if part_0 == "EXIT":
            print("\n Остановка сервера...")
            _cleanup(state, user_mgr)
            server.close()
            await server.wait_closed()
            break

        # ── HELP ──────────────────────────────────────────────────────
        if part_0 == "HELP":
            print_command()
            continue

        # ── STATUS ────────────────────────────────────────────────────
        if part_0 == "STATUS":
            cmds = state.get_all_commands()
            if not cmds:
                print(" Нет активных команд")
            else:
                print(f"\n{'=' * 80}\nАКТИВНЫЕ КОМАНДЫ\n{'=' * 80}")
                for cid, info in cmds.items():
                    elapsed = time.time() - info["start_time"]
                    print(f"  {cid}: {info['type']} ({elapsed:.1f}s) — {info['command']}")
                print(f"{'=' * 80}\n")
            continue

        # ── CANCEL ────────────────────────────────────────────────────
        if part_0 == "CANCEL":
            if len(parts) < 2:
                print("Формат: cancel <client>")
                continue
            target = parts[1]
            if state.has_command(target):
                writer = state.get_writer(target)
                if writer:
                    writer.write(b"CMD:CANCEL_MANUAL\n")
                    await writer.drain()
                state.unregister_command(target)
                print(f" Команда отменена для {target}")
            else:
                print(f" У {target} нет активных команд")
            continue

        # ── KICK ──────────────────────────────────────────────────────
        if part_0 == "KICK":
            if len(parts) < 2:
                print("Формат: kick <client|all>")
                continue
            target = parts[1]
            if target == "all":
                kicked = 0
                for cid in list(state.get_all_clients()):
                    if await ban_mgr.kick_user(cid, "Отключены администратором"):
                        kicked += 1
                print(f" Отключено: {kicked}")
            else:
                real = user_mgr.validate_users(target)
                target = real or target
                if state.is_connected(target):
                    await ban_mgr.kick_user(target)
                    print(f" {target} отключен")
                else:
                    print(f" {target} не подключен")
            continue

        # ── LIST ──────────────────────────────────────────────────────
        if part_0 == "LIST":
            users = user_mgr.get_users_data()
            if not users:
                print(" Нет пользователей")
                continue
            print(f"\n{'=' * 134}")
            print(f"{'№':<4} {'Username':<20} {'Alias':<20} {'Status':<8} {'OS':<8} {'Path':<25} {'Time':<20} {'Group':<25}")
            print(f"{'=' * 135}")
            for i, (uname, info) in enumerate(users.items(), 1):
                t = info["last_login"] if info["status"] == "ON" else info["last_logout"]
                print(f"{i:<4} {uname:<20} {info['alias']:<20} {info['status']:<8} "
                      f"{info['OS']:<8} {info['default_path']:<25} {t or '':<20} {''.join(info['users_in_group']):<25}")
            online = sum(1 for u in users.values() if u["status"] == "ON")
            print(f"{'=' * 134}")
            print(f"Всего: {len(users)} | Онлайн: {online}\n")
            continue

        # ── RENAME ────────────────────────────────────────────────────
        if part_0 == "RENAME":
            if len(parts) < 3:
                print("Формат: rename <user> <new_alias>")
                continue
            target, new_alias = parts[1], parts[2][:10].replace(" ", "_")
            found = user_mgr.validate_users(target)
            if not found:
                print(f" Пользователь '{target}' не найден")
                continue
            users = user_mgr.get_users_data()
            for uname, info in users.items():
                if info["alias"] == new_alias and uname != found:
                    print(f" Alias '{new_alias}' уже занят")
                    break
            else:
                old = users[found]["alias"]
                users[found]["alias"] = new_alias
                user_mgr.save_user_data(users)
                print(f" {found}: '{old}' → '{new_alias}'")
            continue

        # ── CMD ───────────────────────────────────────────────────────
        if part_0 == "CMD":
            if len(parts) < 3:
                print("Формат: CMD <client|all> <command>")
                continue
            target  = parts[1]
            command = " ".join(parts[2:])

            if target == "all":
                state.register_command("all", command, "CMD", 1)
                for cid in state.get_all_clients():
                    cmd = ScheduledCommandManager.replace_user_path(command, cid, user_mgr)
                    writer = state.get_writer(cid)
                    writer.write(f"CMD:{cmd}\n".encode("utf-8"))
                    await writer.drain()
                print(" Команда отправлена всем")
            else:
                real = user_mgr.validate_users(target)
                target = real or target
                if state.is_connected(target):
                    command = ScheduledCommandManager.replace_user_path(command, target, user_mgr)
                    state.register_command(target, command, "CMD", 1)
                    state.get_writer(target).write(f"CMD:{command}\n".encode("utf-8"))
                    await state.get_writer(target).drain()
                    print(f" Команда → {target}")
                else:
                    print(f" {target} не подключен")
            continue

        # ── SIMPL ─────────────────────────────────────────────────────
        if part_0 == "SIMPL":
            if len(parts) < 2:
                print("Формат: simpl <client|all>")
                continue
            target = parts[1]

            try:
                commands = [l.strip() for l in Config.FILE_CODE.read_text("utf-8").splitlines() if l.strip()]
            except FileNotFoundError:
                print(f" Файл {Config.FILE_CODE} не найден")
                continue

            if not commands:
                print(" Файл команд пуст")
                continue

            targets = state.get_all_clients() if target == "all" else [target]

            for cid in targets:
                real = user_mgr.validate_users(cid) or cid
                if not state.is_connected(real):
                    print(f" {real} не подключен")
                    continue
                state.register_command(real, f"simpl ({len(commands)} команд)", "FILETRU", len(commands))
                for cmd in commands:
                    cmd = ScheduledCommandManager.replace_user_path(cmd, real, user_mgr)
                    state.get_writer(real).write(f"FILETRU:{cmd}\n".encode("utf-8"))
                    await state.get_writer(real).drain()
                    await asyncio.sleep(0.2)
                print(f" {len(commands)} команд → {real}")
            continue

        # ── EXPORT ────────────────────────────────────────────────────
        if part_0 == "EXPORT":
            if len(parts) < 3:
                print("Формат: export <client> <path_client> [dest]")
                continue
            target = parts[1]
            src    = parts[2]
            dst    = parts[3] if len(parts) > 3 else "received"

            real = user_mgr.validate_users (target) or target
            if state.is_connected (real):
                src = ScheduledCommandManager.replace_user_path(src, real, user_mgr)
                state.register_command(real, f"export {src}", "EXPORT", 1)
                state.get_writer(real).write(f"EXPORT;{src};{dst}\n".encode("utf-8"))
                await state.get_writer(real).drain()
                print(f" Запрос экспорта → {real}")
            else:
                print(f" {real} не подключен")
            continue

        # ── IMPORT ────────────────────────────────────────────────────
        if part_0 == "IMPORT":
            if len(parts) < 3:
                print("Формат: import <client|all> <path_server> [path_client]")
                continue
            target = parts[1]
            src    = parts[2]
            dst    = parts[3] if len(parts) > 3 else "received"

            targets = state.get_all_clients() if target == "all" else [target]
            sent = 0
            for cid in targets:
                real = user_mgr.validate_users(cid) or cid
                if not state.is_connected(real):
                    continue
                d = ScheduledCommandManager.replace_user_path(dst, real, user_mgr)
                state.register_command(real, f"import {src}", "IMPORT", 1)
                await FileTransfer.import_to_client(real, src, d, state)
                state.unregister_command(real)
                sent += 1
            print(f" Файлы отправлены {sent} клиентам")
            continue

        # ── SAVE ──────────────────────────────────────────────────────
        if part_0 == "SAVE":
            if len(parts) < 3:
                print("Формат: save <client|all> <filename>")
                continue
            target, filename = parts[1], parts[2]
            time_now = time.strftime("%Y-%m-%d %H:%M:%S")

            save_targets = state.get_all_clients() if target == "all" else [target]
            count_saved  = []

            for cid in save_targets:
                real = user_mgr.validate_users(cid) or cid
                out  = state.get_last_output(real)
                if not out or not out.get("content"):
                    print(f" Нет данных от {real}")
                    continue

                # Пытаемся получить последнюю команду из active_commands (или из last_output)
                cmd_info = state.get_command(real)
                command_str = cmd_info["command"] if cmd_info else out.get("command", "—")

                fname     = f"{filename}.txt" if target != "all" else f"{real}_save.txt"
                save_path = Config.DIR_SAVE / fname
                try:
                    with open(save_path, "a" if target == "all" else "w", encoding="utf-8") as f:
                        f.write(
                            f"Пользователь: {real}\n"
                            f"Время: {time_now}\n"
                            f"Тип: {out['type']}\n"
                            f"Команда: {command_str}\n"
                            f"{'=' * 50}\n"
                            f"{out['content']}\n"
                        )
                    count_saved.append(real)
                    Logger.log("SAVE", f"→ {fname}", real)
                except Exception as e:
                    print(f" Ошибка сохранения: {e}")

            if target == "all":
                print(f" Сохранено {len(count_saved)}/{len(save_targets)}: {count_saved}")
            else:
                if count_saved:
                    print(f" Сохранено в {Config.DIR_SAVE / (filename + '.txt')}")
            continue

        # ── GROUP_NEW ────────────────────────────────────────────────
        if part_0 == "GROUP_NEW":
            if len(parts) < 2:
                print("Формат: group_new <name>")
                continue
            g_name = parts[1]
            if group_mgr.get_group_members(g_name) is not None:
                print(f" Группа '{g_name}' уже существует")
                continue
            print(f"\nСоздание группы '{g_name}'. Введите пользователей (EXIT — завершить).")
            members = []
            while True:
                member = input("  > ").strip()
                if member.upper() == "EXIT":
                    break
                real = user_mgr.validate_users(member)
                if real and real not in members:
                    members.append(real)
                elif not real:
                    print(f" Пользователь '{member}' не найден")
            if members:
                try:
                    group_mgr.create_group(g_name, members)
                    print(f" Группа '{g_name}' создана ({len(members)} участников)")
                except Exception as e:
                    print(f" Ошибка: {e}")
            else:
                print(" Группа не создана (нет участников)")
            continue

        # ── GROUP_LIST ────────────────────────────────────────────────
        if part_0 == "GROUP_LIST":
            groups = group_mgr.get_group_data()
            if not groups:
                print(" Нет групп")
            else:
                print(f"\n{'=' * 60}\nГРУППЫ\n{'=' * 60}")
                for g, members in groups.items():
                    print(f"  {g} ({len(members)} участников):")
                    for m in members:
                        print(f"    - {m}")
                print(f"{'=' * 60}\n")
            continue

        # ── GROUP_DEL ────────────────────────────────────────────────
        if part_0 == "GROUP_DEL":
            if len(parts) < 2:
                print("Формат: group_del <name>")
                continue
            try:
                group_mgr.delete_group(parts[1])
                print(f" Группа '{parts[1]}' удалена")
            except Exception as e:
                print(f" Ошибка: {e}")
            continue

        # ── GROUP_ADD ────────────────────────────────────────────────
        if part_0 == "GROUP_ADD":
            if len(parts) < 2:
                print("Формат: group_add <name>")
                continue
            g_name = parts[1]
            if group_mgr.get_group_members(g_name) is None:
                print(f" Группа '{g_name}' не найдена")
                continue
            print("Введите пользователей для добавления (EXIT — завершить).")
            to_add = []
            while True:
                u = input("  > ").strip()
                if u.upper() == "EXIT":
                    break
                real = user_mgr.validate_users(u)
                if real:
                    to_add.append(real)
                else:
                    print(f" '{u}' не найден")
            if to_add:
                skipped = group_mgr.add_users_to_group(g_name, to_add)
                print(f" Добавлено: {len(to_add) - len(skipped)}. Пропущено (уже в группе): {skipped}")
            continue

        # ── GROUP_RM ─────────────────────────────────────────────────
        if part_0 == "GROUP_RM":
            if len(parts) < 2:
                print("Формат: group_rm <name>")
                continue
            g_name = parts[1]
            if group_mgr.get_group_members(g_name) is None:
                print(f" Группа '{g_name}' не найдена")
                continue
            print("Введите пользователей для удаления (EXIT — завершить).")
            to_rm = []
            while True:
                u = input("  > ").strip()
                if u.upper() == "EXIT":
                    break
                real = user_mgr.validate_users(u)
                if real:
                    to_rm.append(real)
                else:
                    print(f" '{u}' не найден")
            if to_rm:
                skipped = group_mgr.remove_users_from_group(g_name, to_rm)
                print(f" Удалено: {len(to_rm) - len(skipped)}. Не найдено в группе: {skipped}")
            continue

        # ── CHART_NEW ─────────────────────────────────────────────────
        if part_0 == "CHART_NEW":
            print("\n" + "=" * 60 + "\nСОЗДАНИЕ ОТЛОЖЕННОЙ КОМАНДЫ\n" + "=" * 60)
            valid_types = ["CMD", "SIMPL", "IMPORT", "EXPORT"]

            raw    = input("Цель (all / username / group:name / EXIT): ").strip()
            target = ask_who(raw, user_mgr, group_mgr)
            if target == "EXIT" or not target:
                print(" Отмена")
                continue

            cmd_type = input("Тип (CMD/SIMPL/IMPORT/EXPORT/EXIT): ").strip().upper()
            if cmd_type == "EXIT" or cmd_type not in valid_types:
                print(" Отмена или неверный тип")
                continue

            extra = ask_cmd(cmd_type)
            if extra == "EXIT":
                print(" Отмена")
                continue

            try:
                sched_mgr.create_command(target, cmd_type, extra)
                print(f" Команда добавлена для '{target}'")
            except Exception as e:
                print(f" Ошибка: {e}")
            continue

        # ── CHART_LIST ────────────────────────────────────────────────
        if part_0 == "CHART_LIST":
            commands = sched_mgr.get_all_commands()
            if not commands:
                print(" Нет активных отложенных команд")
            else:
                print(f"\n{'=' * 60}\nАКТИВНЫЕ ОТЛОЖЕННЫЕ КОМАНДЫ\n{'=' * 60}")
                for i, cmd in enumerate(commands):
                    ctype     = cmd["command_type"]
                    expected  = len(cmd.get("expected_users", []))
                    completed = len(cmd.get("completed_users", []))
                    if ctype == "CMD":
                        print(f"[{i}] {cmd['target']} → CMD: {cmd['command']}")
                    elif ctype == "SIMPL":
                        print(f"[{i}] {cmd['target']} → SIMPL (code.txt)")
                    elif ctype in ("IMPORT", "EXPORT"):
                        print(f"[{i}] {cmd['target']} → {ctype}: {cmd['source_path']} → {cmd['dest_path']}")
                    if completed:
                        print(f"    ✓ Выполнено: {completed}")
                    if expected:
                        print(f"    ⏳ Ожидает: {expected}")
                print(f"{'=' * 60}\n")
            continue

        # ── CHART_DEL ────────────────────────────────────────────────
        if part_0 == "CHART_DEL":
            if len(parts) < 2:
                print("Формат: chart_del <index>")
                continue
            try:
                sched_mgr.delete_command(int(parts[1]))
                print(f" Команда [{parts[1]}] удалена")
            except Exception as e:
                print(f" Ошибка: {e}")
            continue

        # ── CHART_COMD ───────────────────────────────────────────────
        if part_0 == "CHART_COMD":
            data      = sched_mgr._load()
            completed = data.get("completed", [])
            active    = data.get("commands", [])

            print(f"\n{'=' * 70}\nВЫПОЛНЕННЫЕ: {len(completed)}\n{'=' * 70}")
            for i, cmd in enumerate(completed):
                ctype = cmd["command_type"]
                at    = cmd.get("completed_at", "?")
                label = cmd.get("command", cmd.get("source_path", "code.txt"))
                print(f"\n[{i}] {cmd['target']} → {ctype}: {label} ({at}) ✓")
                for u in cmd.get("completed_users", []):
                    print(f"    ├─ {u} ")

            print(f"\n{'=' * 70}\nВ ПРОЦЕССЕ\n{'=' * 70}")
            for i, cmd in enumerate(active):
                cu = cmd.get("completed_users", [])
                eu = cmd.get("expected_users",  [])

                ctype = cmd["command_type"]
                label = cmd.get("command", cmd.get("source_path", "code.txt"))
                print(f"\n[{i}] {cmd['target']} → {ctype}: {label}")
                for u in cu:
                    print(f"    ├─ {u} ✓")
                for e in eu:
                    print (f"    ├─ {e} ")
            print(f"{'=' * 70}\n")
            continue

        print("Такой команды не существует")


# ═══════════════════════════════════════════════════════════════════════════
# ЗАВЕРШЕНИЕ / PERIODIC
# ═══════════════════════════════════════════════════════════════════════════

def _cleanup(state: ServerState, user_mgr: UserManager):
    Logger.log("INFO", "Graceful shutdown...")
    users    = user_mgr.get_users_data()
    time_now = time.strftime("%Y-%m-%d %H:%M:%S")
    for uname in list(state.get_all_clients()):
        if uname in users:
            users[uname]["status"]      = "OFF"
            users[uname]["last_logout"] = time_now
            user_mgr._log_session(uname, "logout")
    user_mgr.save_user_data(users)
    state.save()
    Logger.log("INFO", "Сервер остановлен")


def setup_signal_handlers(state: ServerState, user_mgr: UserManager):
    def handler(signum, frame):
        Logger.log("WARNING", f"Получен сигнал {signum}")
        _cleanup(state, user_mgr)
        sys.exit(0)
    signal.signal(signal.SIGINT,  handler)
    signal.signal(signal.SIGTERM, handler)


async def periodic_state_save(state: ServerState):
    while True:
        await asyncio.sleep(Config.STATE_SAVE_INTERVAL)
        state.save()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    ensure_dirs()

    # Создаём все зависимости
    state     = ServerState()
    user_mgr  = UserManager()
    group_mgr = GroupManager(user_mgr)
    sched_mgr = ScheduledCommandManager(user_mgr, group_mgr)
    monitor   = CommandMonitor(state, user_mgr)
    ban_mgr   = BanManager(state)


    setup_signal_handlers(state, user_mgr)
    Logger.log("INFO", "Запуск сервера...")

    prev = ServerState.load()
    if prev:
        Logger.log("INFO", f"Найдено состояние от {prev['datetime']}")

    try:
        server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, state, user_mgr, sched_mgr, monitor),
            Config.HOST, Config.PORT
        )
        Logger.log("INFO", f"Сервер запущен на {Config.HOST}:{Config.PORT}")
        Logger.log("INFO", f"Таймаут команд: {Config.COMMAND_TIMEOUT}s "
                           f"(предупреждение: {Config.WARNING_TIMEOUT}s)")
        print_command()

        async with server:
            await asyncio.gather(
                server.serve_forever(),
                server_input(server, state, user_mgr, group_mgr, sched_mgr, ban_mgr),
                monitor.monitor_loop(),
                periodic_state_save(state),
            )
    except Exception as e:
        import traceback
        Logger.log("CRITICAL", f"Критическая ошибка: {e}")
        Logger.crash(e, traceback.format_exc(), state)
        _cleanup(state, user_mgr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        Logger.crash(e, traceback.format_exc(), ServerState())
