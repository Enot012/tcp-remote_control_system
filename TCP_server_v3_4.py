"""
╔══════════════════════════════════════════════════════════════════════════╗
║                TCP SERVER - СИСТЕМА УДАЛЁННОГО УПРАВЛЕНИЯ                ║
║                                    V3.4                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

УЛУЧШЕНАЯ ВЕРСИЯ 3.3:
-Добавлены классы
    -ServerCmdParser    >   парсит строку консоли, отделяя команду
    -ClientMsgParser    >   парсит сообщение от клиента, отделяя протокол передачи, от данных
    -CommandHandler     >   логика всех консольных  команд(cmd,simpl,export,import)
    -ProtocolHandler    >   логика протоколов(OUTPUT,FILETRU)
    -ServerDispatcher   >   маршрутизирует ServerCmd → метод CommandHandler
    -ProtocolDispatcher >   маршрутизирует ClientMsg → метод ProtocolHandler
    -ServerCmd          >   StrEnum для описания стека консольных команд
    -ClientMsg          >   StrEnum для описания стека протоколов
-Изменения
    -GroupManager — методы переименованы и появился _sync_users:
        create_group()            >   create()
        delete_group()            >   delete()
        add_users_to_group()      >   add_users()
        remove_users_from_group() >   remove_users()
        get_group_members()       >   get_members()
        get_all_group_names()     >   get_all_names()
        + новый _sync_users() — синхронизирует users_in_group при изменении группы

    -ScheduledManager — методы переименованы:
        create_command()          >   create()
        delete_command()          >   delete()
        mark_completed()          >   mark_done()
        get_all_commands()        >   get_all()
        get_commands_for_user()   >   get_for_user()
        replace_user_path()       >   sub_path()
        + новый sub_serv_path() — замена {path_serv} -> Config.DIR_FOR_SEND

    -def server_input
        Убрана цепочка if/elif заменена на вызов ServerDispatcher
"""


import asyncio
import time
import os
import json
import signal
import sys
import re
from enum import StrEnum
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Any, Callable




# ═══════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════

class Config:
    BASE_DIR              = Path("home")
    DIR_SAVE              = BASE_DIR / "save"
    DIR_TRASH             = BASE_DIR / "trash"
    DIR_HISTORY           = BASE_DIR / "history"
    DIR_FILES             = BASE_DIR / "files"
    DIR_LOGS              = BASE_DIR / "logs"
    DIR_JSON              = BASE_DIR / "json"
    DIR_SCHEDULED_RESULTS = BASE_DIR / "files" / "scheduled_commands"
    DIR_FOR_SEND          = BASE_DIR / "send_file"

    FILE_CODE      = BASE_DIR / "code.txt"
    FILE_USERS     = BASE_DIR / "users.json"
    FILE_STATE     = BASE_DIR / "server_state.json"
    FILE_CRASH_LOG = BASE_DIR / "crash.log"
    FILE_GROUPS    = BASE_DIR / "json" / "groups.json"
    FILE_SCHEDULED = BASE_DIR / "json" / "scheduled_commands.json"


    HOST = "0.0.0.0"
    PORT = 9000

    CHUNK_SIZE          = 65536
    COMMAND_TIMEOUT     = 120
    WARNING_TIMEOUT     = 90
    STATE_SAVE_INTERVAL = 30
    READ_TIMEOUT        = 300
    TIMEZONE_OFFSET     = +1


# ═══════════════════════════════════════════════════════════════════════════
# ENUM — СЕРВЕРНЫЕ КОМАНДЫ И КЛИЕНТСКИЕ ПРОТОКОЛЫ
# ═══════════════════════════════════════════════════════════════════════════

class ServerCmd(StrEnum):
    """Команды, вводимые администратором в консоли"""
    CMD        = "cmd"
    SIMPL      = "simpl"
    EXPORT     = "export"
    IMPORT     = "import"
    SAVE       = "save"
    LIST       = "list"
    RENAME     = "rename"
    STATUS     = "status"
    CANCEL     = "cancel"
    KICK       = "kick"
    HELP       = "help"
    EXIT       = "exit"
    GROUP_NEW  = "group_new"
    GROUP_LIST = "group_list"
    GROUP_DEL  = "group_del"
    GROUP_ADD  = "group_add"
    GROUP_RM   = "group_rm"
    CHART_NEW  = "chart_new"
    CHART_LIST = "chart_list"
    CHART_DEL  = "chart_del"
    CHART_COMD = "chart_comd"


class ClientMsg(StrEnum):
    """Протокольные сообщения, приходящие от клиента"""
    EXPORT_START    = "export:start"
    IMPORT_COMPLETE = "import:complete"
    IMPORT_ERROR    = "import:error"
    OUTPUT_START    = "output:start"
    OUTPUT_CHUNK    = "output:chunk"
    OUTPUT_END      = "output:end"
    FILETRU_START   = "filetru:start"
    FILETRU_CHUNK   = "filetru:chunk"
    FILETRU_END     = "filetru:end"




def resolve_and_check(target,user_mgr,state)->str|None:
    real = user_mgr.validate_users(target) or target
    if not state.is_connected(real):
        return None
    return real


# ═══════════════════════════════════════════════════════════════════════════
# ПАРСЕРЫ
# ═══════════════════════════════════════════════════════════════════════════

class ServerCmdParser:
    """Парсит строку консоли → (ServerCmd, args)"""

    @staticmethod
    def parse(raw: str) -> tuple[ServerCmd, list[str]]:
        parts = raw.strip().split()
        if not parts:
            raise ValueError("Пустой ввод")
        try:
            cmd = ServerCmd(parts[0].lower())
        except ValueError:
            raise ValueError(f"Неизвестная команда: '{parts[0]}'")
        return cmd, parts[1:]


class ClientMsgParser:
    """Определяет тип сообщения от клиента и выделяет payload."""

    # Точные совпадения (без payload после двоеточия)
    _EXACT: Dict[str, ClientMsg] = {
        "output:end":       ClientMsg.OUTPUT_END,
        "filetru:end":      ClientMsg.FILETRU_END,
        "import:complete":  ClientMsg.IMPORT_COMPLETE,
    }

    @staticmethod
    def parse(raw: str) -> tuple[ClientMsg, str]:
        lower = raw.lower().strip()

        if lower in ClientMsgParser._EXACT:
            return ClientMsgParser._EXACT[lower], ""

        for member in ClientMsg:
            prefix = member.value + ":"
            if lower.startswith(prefix):
                return member, raw[len(prefix):]

        raise ValueError(f"Неизвестное сообщение: {raw[:60]}")


# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════

def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=Config.TIMEZONE_OFFSET)


def ensure_dirs():
    for d in [Config.DIR_SAVE, Config.DIR_TRASH, Config.DIR_HISTORY,
              Config.DIR_FILES, Config.DIR_LOGS, Config.DIR_JSON,
              Config.DIR_SCHEDULED_RESULTS,Config.DIR_FOR_SEND]:
        os.makedirs(d, exist_ok=True)


def print_help():
    print("\n" + "=" * 80)
    print("ДОСТУПНЫЕ КОМАНДЫ:")
    rows = [
        ("CMD <client|all|group:> <команда>",           "Выполнить команду"),
        ("export <client|all|group:> <path> [dest]",    "Получить файлы с клиента"),
        ("import <client|all|group:> <path> [dest]",    "Отправить файлы клиенту"),
        ("save <client|all> <filename>",                "Сохранить последний вывод"),
        ("simpl <client|all|group:>",                   "Выполнить команды из code.txt"),
        ("", ""),
        ("chart_new",                                   "Создать отложенную команду"),
        ("chart_list",                                  "Список отложенных команд"),
        ("chart_del <index>",                           "Удалить отложенную команду"),
        ("chart_comd",                                  "Выполненные команды"),
        ("", ""),
        ("group_new <n>",                               "Создать группу"),
        ("group_list",                                  "Список групп"),
        ("group_del <n>",                               "Удалить группу"),
        ("group_add <n>",                               "Добавить пользователей в группу"),
        ("group_rm  <n>",                               "Убрать пользователей из группы"),
        ("", ""),
        ("list",                                 "Список пользователей"),
        ("rename <user> <alias>",                "Переименовать пользователя"),
        ("status",                               "Активные команды"),
        ("cancel <client>",                      "Отменить команду"),
        ("kick <client|all>",                    "Отключить клиента/всех"),
        ("help",                                 "Эта справка"),
        ("EXIT",                                 "Остановить сервер"),
    ]
    for cmd, desc in rows:
        print(f"  {cmd:<42} - {desc}" if cmd else "")
    print("=" * 80 + "\n")


# ═══════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════

class Logger:

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
    def crash(exc: Exception, tb: str, state: "ServerState"):
        ts   = get_local_time().strftime("%Y-%m-%d %H:%M:%S")
        cmds = state.get_all_commands()
        lines = [
            f"\n{'=' * 80}", f"КРИТИЧЕСКАЯ ОШИБКА: {ts}",
            f"{type(exc).__name__}: {exc}", f"{'=' * 80}", tb,
            f"Клиентов: {len(state.get_all_clients())}  Команд: {len(cmds)}",
        ]
        for cid, info in cmds.items():
            elapsed = time.time() - info["start_time"]
            lines.append(f"  {cid}: {info['type']} ({elapsed:.1f}s) — {info['command']}")
        lines.append(f"{'=' * 80}\n")
        text = "\n".join(lines)
        try:
            with open(Config.FILE_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        print(text)


# ═══════════════════════════════════════════════════════════════════════════
# СОСТОЯНИЕ СЕРВЕРА
# ═══════════════════════════════════════════════════════════════════════════

class ServerState:

    def __init__(self):
        self._clients:            Dict[str, asyncio.StreamWriter] = {}
        self._output_buffers:     Dict[str, Dict[str, Any]]       = {}
        self._last_outputs:       Dict[str, Dict[str, Any]]       = {}
        self._active_commands:    Dict[str, Dict[str, Any]]       = {}
        self._scheduled_tracking: Dict[str, list]                 = {}

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

    def append_chunk(self, username: str, chunk: str):
        buf = self._output_buffers.get(username)
        if buf:
            buf["lines"].append(chunk)
            buf["chunks"] += 1

    def get_buffer(self, username: str) -> Optional[Dict]:
        return self._output_buffers.get(username)

    def flush_buffer(self, username: str) -> str:
        buf    = self._output_buffers.get(username, {})
        result = "\n".join(buf.get("lines", []))
        self._last_outputs[username] = {
            "type":      buf.get("type"),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "content":   result,
        }
        if buf:
            buf["lines"] = []
        return result

    def clear_buffer(self, username: str):
        buf = self._output_buffers.get(username)
        if buf:
            buf.update({"type": None, "lines": [], "chunks": 0})

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

    def unregister_command(self, username: str):
        info = self._active_commands.pop(username, None)
        if info:
            elapsed = time.time() - info["start_time"]
            Logger.log("CMD_END", f"{info['type']} завершена за {elapsed:.1f}s",
                       username, show_console=False)

    def get_command(self, username: str) -> Optional[Dict]:
        return self._active_commands.get(username)

    def get_all_commands(self) -> Dict:
        return dict(self._active_commands)

    def has_command(self, username: str) -> bool:
        return username in self._active_commands

    # ── scheduled tracking ───────────────────────────────────────────────

    def push_scheduled(self, username: str, idx: int):
        self._scheduled_tracking.setdefault(username, []).append(idx)

    def pop_scheduled(self, username: str) -> Optional[int]:
        q = self._scheduled_tracking.get(username)
        return q.pop(0) if q else None

    def has_scheduled(self, username: str) -> bool:
        return bool(self._scheduled_tracking.get(username))

    # ── persistence ──────────────────────────────────────────────────────

    def save(self):
        try:
            data = {
                "timestamp":         time.time(),
                "datetime":          get_local_time().strftime("%Y-%m-%d %H:%M:%S"),
                "connected_clients": self.get_all_clients(),
                "active_commands": {
                    cid: {
                        "command":    info.get("command", "?"),
                        "type":       info["type"],
                        "start_time": info["start_time"],
                        "elapsed":    time.time() - info["start_time"],
                    }
                    for cid, info in self._active_commands.items()
                },
            }
            with open(Config.FILE_STATE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения состояния: {e}", show_console=False)

    @staticmethod
    def load() -> Optional[Dict]:
        if not Config.FILE_STATE.exists():
            return None
        try:
            with open(Config.FILE_STATE, "r", encoding="utf-8") as f:
                s = json.load(f)
            return s if time.time() - s["timestamp"] < 600 else None
        except Exception:
            return None




# ═══════════════════════════════════════════════════════════════════════════
# ПОЛЬЗОВАТЕЛИ
# ═══════════════════════════════════════════════════════════════════════════

class UserManager:

    __CYRILLIC_MAP = {
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo',
        'ж':'zh','з':'z','и':'i','й':'y','к':'k','л':'l','м':'m',
        'н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u',
        'ф':'f','х':'h','ц':'ts','ч':'ch','ш':'sh','щ':'sch',
        'ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
    }

    def __init__(self):
        self._cache: Optional[Dict] = None

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

    def _transliterate(self, name: str) -> str:
        out = []
        for ch in name:
            if 'а' <= ch.lower() <= 'я' or ch in 'ЁёЪъЬь':
                tr = self.__CYRILLIC_MAP.get(ch.lower(), ch)
                out.append(tr.capitalize() if ch.isupper() and tr else tr)
            else:
                out.append(ch)
        return ''.join(out).replace(" ", "_")

    def _make_alias(self, username: str, users: Dict) -> str:
        base  = self._transliterate(username)[:10]
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
            raise ValueError("Имя длиннее 32 символов")
        if not re.match(r'^[\w\-\.]+$', name):
            raise ValueError(f"Недопустимые символы: {name}")
        return name

    # ── public ───────────────────────────────────────────────────────────

    def register(self, username: str, os_name: str, home_path: str) -> str:
        username = self._validate_name(username)
        users    = self._load()["users"]
        now      = time.strftime("%Y-%m-%d %H:%M:%S")

        for existing, info in users.items():
            if info.get("default_path") == home_path:
                info.update({"status": "ON", "last_login": now})
                self._save()
                self._log_session(existing, "login")
                Logger.log("INFO", f"Вход: {existing} ({info['alias']})")
                return info["alias"]

        alias = self._make_alias(username, users)
        users[alias] = {
            "alias": alias, "status": "ON", "OS": os_name,
            "default_path": home_path, "users_in_group": [],
            "last_login": now, "last_logout": None,
        }
        self._save()
        self._log_session(alias, "login")
        Logger.log("INFO", f"Новый пользователь: {alias}")
        return alias

    def logout(self, username: str):
        users = self._load()["users"]
        if username in users:
            users[username].update({
                "status": "OFF",
                "last_logout": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._save()
            self._log_session(username, "logout")
            Logger.log("INFO", f"Выход: {username}")

    def validate(self, name: str) -> Optional[str]:
        users = self._load().get("users", {})
        if name in users:
            return name
        for uname, info in users.items():
            if info["alias"].lower() == name.lower():
                return uname
        return None

    def get_users_data(self) -> Dict:
        return self._load().get("users", {})

    def get_user_info(self, username: str) -> Optional[Dict]:
        return self._load()["users"].get(username)

    def get_all_usernames(self) -> list:
        return list(self._load().get("users", {}).keys())

    def save_user_data(self, user_data: Dict) -> bool:
        required = {"alias","status","OS","default_path","users_in_group","last_login","last_logout"}
        for uname, info in user_data.items():
            if not required.issubset(info):
                raise ValueError(f"'{uname}': отсутствуют обязательные поля")
            if info["status"] not in ("ON", "OFF"):
                raise ValueError(f"'{uname}': некорректный статус")
        self._cache["users"] = user_data
        return self._save()

    def _log_session(self, username: str, action: str):
        alias        = self._load()["users"].get(username, {}).get("alias", username)
        history_file = Config.DIR_HISTORY / f"{username}.json"
        try:
            history = json.loads(history_file.read_text("utf-8")) if history_file.exists() else \
                      {"username": username, "alias": alias, "sessions": []}
        except Exception:
            history = {"username": username, "alias": alias, "sessions": []}

        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if action == "login":
            history["sessions"].append({"login": now, "logout": None})
        elif action == "logout":
            for s in reversed(history["sessions"]):
                if s["logout"] is None:
                    s["logout"] = now
                    break
        try:
            history_file.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")
        except Exception as e:
            Logger.log("ERROR", f"История {alias}: {e}", show_console=False)


# ═══════════════════════════════════════════════════════════════════════════
# ГРУППЫ
# ═══════════════════════════════════════════════════════════════════════════

class GroupManager:

    def __init__(self, user_mgr: UserManager,state :ServerState):
        self._cache:    Optional[Dict] = None
        self._user_mgr: UserManager    = user_mgr
        self._state=state

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

    def _sync_users(self, group_name: str, members: list, add: bool):
        user_data = self._user_mgr.get_users_data()
        for user in members:
            groups = user_data.get(user, {}).get("users_in_group", [])
            if add and group_name not in groups:
                groups.append(group_name)
            elif not add and group_name in groups:
                groups.remove(group_name)
        self._user_mgr.save_user_data(user_data)

    def get_members(self, group_name: str) -> Optional[list]:
        return self._load().get(group_name)

    def get_all_group(self) -> list:
        return list(self._load().keys())

    def get_data(self) -> Dict:
        return dict(self._load())

    def create(self, group_name: str, members: list) -> bool:
        groups = self.get_data()
        if group_name in groups:
            raise ValueError(f"Группа '{group_name}' уже существует")
        groups[group_name] = members
        self._cache = groups
        self._sync_users(group_name, members, add=True)
        return self._save()

    def delete(self, group_name: str) -> bool:
        groups = self.get_data()
        if group_name not in groups:
            raise NameError(f"Группа '{group_name}' не найдена")
        self._sync_users(group_name, groups[group_name], add=False)
        del groups[group_name]
        self._cache = groups
        return self._save()

    def add_users(self, group_name: str, users: list) -> list:
        if group_name not in self._load():
            raise NameError(f"Группа '{group_name}' не найдена")
        skipped = []
        for user in users:
            if user in self._load()[group_name]:
                skipped.append(user)
            else:
                self._load()[group_name].append(user)
        self._sync_users(group_name, [u for u in users if u not in skipped], add=True)
        self._save()
        return skipped

    def remove_users(self, group_name: str, users: list) -> list:
        cache=self._load()
        if group_name not in cache:
            raise NameError(f"Группа '{group_name}' не найдена")
        skipped = []
        for user in users:
            if user in cache[group_name]:
                cache[group_name].remove(user)
            else:
                skipped.append(user)
        self._sync_users(group_name, [u for u in users if u not in skipped], add=False)
        self._save()
        return skipped

    def get_online_user(self,group_name) ->list:
        members=self.get_members(group_name)
        online=[]
        for u in members:
            if self._state.is_connected(u):
                online.append(u)
        print(f"Онлайн {len(online)}/{len(members)}\n{','.join(online)}")
        return online


# ═══════════════════════════════════════════════════════════════════════════
# ОТЛОЖЕННЫЕ КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════════════

class ScheduledManager:

    VALID_TYPES = {ServerCmd.CMD, ServerCmd.SIMPL, ServerCmd.IMPORT, ServerCmd.EXPORT}

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

    def _resolve_targets(self, target: str) -> list:
        if target == "ALL":
            return self._user_mgr.get_all_usernames()
        if target.startswith("group:"):
            return self._group_mgr.get_members(target[6:]) or []
        return [target]

    def get_all(self) -> list:
        return self._load().get("commands", [])

    def get_for_user(self, username: str) -> list:
        return [c for c in self._load().get("commands", [])
                if username in c.get("expected_users", [])]

    def create(self, target: str, cmd_type: str, extra: Dict) -> bool:
        cmd_type = cmd_type.upper()
        if cmd_type not in self.VALID_TYPES:
            raise ValueError(f"Неверный тип: {cmd_type}")
        if cmd_type == "CMD" and "command" not in extra:
            raise ValueError("CMD требует поле 'command'")
        if cmd_type in ("IMPORT", "EXPORT") and not {"source_path","dest_path"}.issubset(extra):
            raise ValueError("IMPORT/EXPORT требуют 'source_path' и 'dest_path'")

        if target != "ALL" and not target.startswith("group:"):
            resolved = self._user_mgr.validate(target)
            if not resolved:
                raise NameError(f"Пользователь '{target}' не найден")
            target = resolved

        entry = {
            "target":          target,
            "command_type":    cmd_type,
            "created_at":      time.strftime("%Y-%m-%d %H:%M:%S"),
            "expected_users":  self._resolve_targets(target),
            "completed_users": [],
        }
        entry.update(extra)
        self._load()["commands"].append(entry)
        return self._save()

    def delete(self, index: int) -> bool:
        cmds = self._load().get("commands", [])
        if not (0 <= index < len(cmds)):
            raise IndexError(f"Неверный индекс: {index}")
        cmds.pop(index)
        return self._save()

    def mark_done(self, cmd_index: int, username: str, output: str) -> bool:
        cmds = self._load().get("commands", [])
        if cmd_index >= len(cmds):
            return False
        cmd = cmds[cmd_index]
        if username in cmd.get("expected_users", []):
            cmd["expected_users"].remove(username)
            cmd.setdefault("completed_users", []).append(username)
        self._write_output(cmd["target"], username, output)
        if not cmd["expected_users"]:
            cmd["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            self._load()["completed"].append(cmd)
            cmds.pop(cmd_index)
        return self._save()

    def _write_output(self, target: str, username: str, output: str):
        if target == "ALL":
            fname = "ALL.txt"
        elif target.startswith("group:"):
            fname = f"group_{target[6:]}.txt"
        else:
            fname = f"{target}.txt"
        try:
            with open(Config.DIR_SCHEDULED_RESULTS / fname, "a", encoding="utf-8") as f:
                f.write(f"{username}\n{output}\n\n\n")
        except Exception as e:
            Logger.log("ERROR", f"Ошибка записи вывода: {e}")

    def sub_path(self, text: str, username: str) -> str:
        text = text.replace("{user}", username)
        if "{path}" in text:
            info = self._user_mgr.get_user_info(username)
            text = text.replace("{path}", info.get("default_path", "") if info else "")
        return text

    @staticmethod
    def sub_serv_path(text) -> str:
        if "{path_serv}" in text:
            path_server=text.replace("{path_serv}",str(Config.DIR_FOR_SEND.resolve()))
            return Path(path_server)
        return text


# ═══════════════════════════════════════════════════════════════════════════
# ПЕРЕДАЧА ФАЙЛОВ
# ═══════════════════════════════════════════════════════════════════════════

class FileTransfer:

    @staticmethod
    def list_files(path: Path) -> list:
        if not path.exists():
            return []
        if path.is_file():
            return [{"path": str(path), "rel_path": path.name, "size": path.stat().st_size}]
        return [
            {"path": str(f), "rel_path": str(f.relative_to(path)), "size": f.stat().st_size}
            for f in path.rglob("*") if f.is_file()
        ]

    @staticmethod
    async def send_file(writer: asyncio.StreamWriter, file_path: str,
                        rel_path: str, size: int) -> bool:
        try:
            writer.write(f"FILE:META:{json.dumps({'rel_path': rel_path, 'size': size})}\n".encode())
            await writer.drain()
            sent = 0
            with open(file_path, "rb") as f:
                while chunk := f.read(Config.CHUNK_SIZE):
                    writer.write(chunk)
                    await writer.drain()
                    sent += len(chunk)
                    print(f"\r  {rel_path}: {sent * 100 // size if size else 100}% ", end="", flush=True)
            print(f"\r  {rel_path} ({size / 1024:.1f} KB)")
            writer.write(b"FILE:END\n")
            await writer.drain()
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка отправки {rel_path}: {e}")
            return False

    @staticmethod
    async def receive_file(reader: asyncio.StreamReader, dest: Path, size: int) -> bool:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            received = 0
            with open(dest, "wb") as f:
                while received < size:
                    chunk = await reader.read(min(Config.CHUNK_SIZE, size - received))
                    if not chunk:
                        raise ConnectionError("Разрыв соединения")
                    f.write(chunk)
                    received += len(chunk)
                    print(f"\r  {dest.name}: {received * 100 // size if size else 100}% ", end="", flush=True)
            print(f"\r  {dest.name} ({size / 1024:.1f} KB)")
            end = await reader.readline()
            if not end.decode("utf-8", errors="ignore").strip().startswith("FILE:END"):
                Logger.log("WARNING", "Неожиданный маркер", show_console=False)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка получения: {e}")
            return False

    @staticmethod
    async def send_to_client(client_id: str, source: str, dest: str,
                             state: "ServerState") -> bool:
        writer = state.get_writer(client_id)
        if not writer:
            return False
        path  = Path(source)
        files = FileTransfer.list_files(path)
        if not files:
            print(f" Нет файлов: {source}")
            return False
        meta = {"count": len(files), "dest_dir": dest, "source": path.name}
        writer.write(f"IMPORT:START:{json.dumps(meta)}\n".encode())
        await writer.drain()
        total = sum(f["size"] for f in files)
        print(f" Отправка {len(files)} файлов ({total / 1024 / 1024:.2f} MB)")
        for fi in files:
            if not await FileTransfer.send_file(writer, fi["path"], fi["rel_path"], fi["size"]):
                return False
        print(" Ожидание подтверждения...")
        return True


# ═══════════════════════════════════════════════════════════════════════════
# КИК
# ═══════════════════════════════════════════════════════════════════════════

class BanManager:

    def __init__(self, state: "ServerState"):
        self._state = state

    async def kick(self, username: str, reason: str = "Отключен администратором") -> bool:
        writer = self._state.get_writer(username)
        if not writer:
            return False
        try:
            writer.write(f"KICK:{reason}\n".encode())
            await writer.drain()
            await asyncio.sleep(0.5)
            writer.close()
            await writer.wait_closed()
            Logger.log("KICK", f"{username}: {reason}")
            return True
        except Exception as e:
            Logger.log("ERROR", f"kick {username}: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════
# МОНИТОРИНГ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class CommandMonitor:

    def __init__(self, state: ServerState, user_mgr: UserManager):
        self._state    = state
        self._user_mgr = user_mgr

    def save_output(self, client_id: str, command: str, output: str, cmd_type: str):
        try:
            info     = self._user_mgr.get_user_info(client_id)
            alias    = info["alias"] if info else client_id
            now      = get_local_time().strftime("%Y-%m-%d %H:%M:%S")
            filepath = Config.DIR_TRASH / f"output_{alias}.txt"
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"Время: {now}\nТип: {cmd_type}\nКоманда: {command}\n"
                        f"{'=' * 80}\n{output}\n{'=' * 80}\n\n")
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения вывода: {e}", client_id)

    async def monitor_loop(self):
        warned: set = set()
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for cid, info in list(self._state.get_all_commands().items()):
                elapsed = now - info["start_time"]
                if elapsed > Config.WARNING_TIMEOUT and cid not in warned:
                    remaining = Config.COMMAND_TIMEOUT - elapsed
                    writer = self._state.get_writer(cid)
                    if writer:
                        try:
                            writer.write(
                                f"\n Команда {elapsed:.0f}s, осталось {remaining:.0f}s\n".encode()
                            )
                            await writer.drain()
                        except Exception:
                            pass
                    warned.add(cid)
                if elapsed > Config.COMMAND_TIMEOUT:
                    Logger.log("TIMEOUT", f"превышен лимит: {info['command']}", cid)
                    writer = self._state.get_writer(cid)
                    if writer:
                        try:
                            writer.write(b"CMD:CANCEL_TIMEOUT\n")
                            await writer.drain()
                        except Exception:
                            pass
                    self._state.unregister_command(cid)
                    self._state.clear_buffer(cid)
                    warned.discard(cid)


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК СЕРВЕРНЫХ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class CommandHandler:
    """Вся бизнес-логика серверных команд."""

    def __init__(self, state: ServerState, user_mgr: UserManager,
                 group_mgr: GroupManager, sched_mgr: ScheduledManager,
                 ban_mgr: BanManager, monitor: CommandMonitor):
        self._state     = state
        self._user_mgr  = user_mgr
        self._group_mgr = group_mgr
        self._sched_mgr = sched_mgr
        self._ban_mgr   = ban_mgr
        self._monitor   = monitor

    # ── helpers ──────────────────────────────────────────────────────────

    def _resolve(self, name: str) -> Optional[str]:
        if name == "all":
            return name
        if name.startswith("group:"):
            group_name=name[6:]
            return group_name if group_name in self._group_mgr.get_all_group() else []
        return self._user_mgr.validate(name)

    def _require_connected(self, target: str) -> list:
        real = self._targets(target)
        if not  real:
            print(f" Пользователи не подключены")
            return []
        return real

    def _targets(self, target: str) -> list:
        """Разворачивает 'all' / group / username в список подключённых клиентов."""
        if target == "all":
            return self._state.get_all_clients()
        if target.startswith("group:"):
            group_name=target[6:]
            user=self._group_mgr.get_online_user(group_name)
            return user
        real = self._user_mgr.validate(target)
        return [real] if self._state.is_connected(real) else []

    def _sub(self, text: str, cid: str) -> str:
        return self._sched_mgr.sub_path(text, cid)

    async def _drain(self, cid: str):
        writer = self._state.get_writer(cid)
        if writer:
            await writer.drain()

    async def _send_simpl(self, cid: str, commands: list):
        self._state.register_command(cid, f"simpl ({len(commands)} команд)", "FILETRU", len(commands))
        for cmd in commands:
            self._state.get_writer(cid).write(f"FILETRU:{self._sub(cmd, cid)}\n".encode())
            await self._drain(cid)
            await asyncio.sleep(0.2)

    def _read_code_file(self) -> list:
        try:
            return [l.strip() for l in Config.FILE_CODE.read_text("utf-8").splitlines() if l.strip()]
        except FileNotFoundError:
            print(f" Файл {Config.FILE_CODE} не найден")
            return []

    # ── команды ──────────────────────────────────────────────────────────

    async def cmd(self, args: list):
        if len(args) < 2:
            print("Формат: CMD <client|all|group:> <команда>")
            return
        target, command = args[0], " ".join(args[1:])

        if not self._resolve(target):
            print("Пользователь не найден")
            return
        user=self._require_connected(target)
        if user:
            for cid in user:
                c = self._sub(command, cid)
                self._state.register_command(cid, c, "CMD", 1)
                self._state.get_writer(cid).write(f"CMD:{c}\n".encode())
                await self._drain(cid)
            print(f" CMD → {target}")




    async def simpl(self, args: list):
        if len(args) < 1:
            print("Формат: simpl <client|all|group:>")
            return
        if not self._resolve(args[0]):
            print("Пользователь не найден")
            return
        commands = self._read_code_file()
        if not commands:
            return
        user=self._require_connected(args[0])
        if user:
            for cid in user:
                await self._send_simpl(cid, commands)
                print(f" {len(commands)} команд → {cid}")


    async def export(self, args: list):
        if len(args) < 2:
            print("Формат: export <client|all|group:> <path> [dest]")
            return
        user = self._require_connected(args[0])
        if user:
            for cid in user:
                src = self._sub(args[1],cid)
                dst = args[2] if len(args) > 2 else "received"
                self._state.register_command(cid, f"export {src}", "EXPORT", 1)
                self._state.get_writer(cid).write(f"EXPORT;{src};{dst}\n".encode())
                await self._drain(cid)
                print(f" Запрос экспорта → {cid}")


    async def import_(self, args: list):
        if len(args) < 2:
            print("Формат: import <client|all|group:> <path_server> [path_client]")
            return
        target = args[0]
        src    =  self._sched_mgr.sub_serv_path(args[1])
        dst  = args[2] if len(args) > 2 else "received"
        sent = 0
        user= self._require_connected(target)
        if user:
            for cid in self._targets(target):
                d = self._sub(dst, cid)
                self._state.register_command(cid, f"import {src}", "IMPORT", 1)
                await FileTransfer.send_to_client(cid, src, d, self._state)
                self._state.unregister_command(cid)
                sent += 1
            print(f" Отправлено {sent} клиентам")

    async def save(self, args: list):
        if len(args) < 2:
            print("Формат: save <client|all> <filename>")
            return
        target, filename = args[0], args[1]
        now     = time.strftime("%Y-%m-%d %H:%M:%S")
        targets = self._state.get_all_clients() if target == "all" else [target]
        saved   = []

        for cid in targets:
            real = self._resolve(cid) or cid
            out  = self._state.get_last_output(real)
            if not out or not out.get("content"):
                print(f" Нет данных от {real}")
                continue
            cmd_info    = self._state.get_command(real)
            command_str = cmd_info["command"] if cmd_info else "—"
            fname       = f"{filename}.txt" if target != "all" else f"{real}_save.txt"
            try:
                mode = "a" if target == "all" else "w"
                with open(Config.DIR_SAVE / fname, mode, encoding="utf-8") as f:
                    f.write(
                        f"Пользователь: {real}\nВремя: {now}\n"
                        f"Тип: {out['type']}\nКоманда: {command_str}\n"
                        f"{'=' * 50}\n{out['content']}\n"
                    )
                saved.append(real)
                Logger.log("SAVE", f"→ {fname}", real)
            except Exception as e:
                print(f" Ошибка: {e}")

        if target == "all":
            print(f" Сохранено {len(saved)}/{len(targets)}")
        elif saved:
            print(f" → {Config.DIR_SAVE / (filename + '.txt')}")

    def list_users(self, args: list):
        users = self._user_mgr.get_users_data()
        if not users:
            print(" Нет пользователей")
            return
        print(f"\n{'=' * 134}")
        print(f"{'№':<4} {'Username':<20} {'Alias':<20} {'Status':<8} {'OS':<8} {'Path':<25} {'Time':<20} {'Group':<25}")
        print(f"{'=' * 134}")
        for i, (uname, info) in enumerate(users.items(), 1):
            t = info["last_login"] if info["status"] == "ON" else info["last_logout"]
            print(f"{i:<4} {uname:<20} {info['alias']:<20} {info['status']:<8} "
                  f"{info['OS']:<8} {info['default_path']:<25} {t or '':<20} {','.join(info['users_in_group']):<25}")
        online = sum(1 for u in users.values() if u["status"] == "ON")
        print(f"{'=' * 134}\nВсего: {len(users)} | Онлайн: {online}\n")


    def rename(self, args: list):
        if len(args) < 2:
            print("Формат: rename <user> <alias>")
            return
        target, new_alias = args[0], args[1][:10].replace(" ", "_")
        found = self._resolve(target)
        if not found:
            print(f" '{target}' не найден")
            return
        users = self._user_mgr.get_users_data()
        for uname, info in users.items():
            if info["alias"] == new_alias and uname != found:
                print(f" Alias '{new_alias}' уже занят")
                return
        old = users[found]["alias"]
        users[found]["alias"] = new_alias
        self._user_mgr.save_user_data(users)
        print(f" {found}: '{old}' → '{new_alias}'")

    def status(self, args: list):
        cmds = self._state.get_all_commands()
        if not cmds:
            print(" Нет активных команд")
            return
        print(f"\n{'=' * 80}\nАКТИВНЫЕ КОМАНДЫ\n{'=' * 80}")
        for cid, info in cmds.items():
            elapsed = time.time() - info["start_time"]
            print(f"  {cid}: {info['type']} ({elapsed:.1f}s) — {info['command']}")
        print(f"{'=' * 80}\n")

    async def cancel(self, args: list):
        if len(args) < 1:
            print("Формат: cancel <client>")
            return
        target = args[0]
        if self._state.has_command(target):
            writer = self._state.get_writer(target)
            if writer:
                writer.write(b"CMD:CANCEL_MANUAL\n")
                await self._drain(target)
            self._state.unregister_command(target)
            print(f" Отменено для {target}")
        else:
            print(f" У {target} нет активных команд")

    async def kick(self, args: list):
        if len(args) < 1:
            print("Формат: kick <client|all>")
            return
        target = args[0]
        if target == "all":
            n = sum([
                1 async for cid in _aiter(self._state.get_all_clients())
                if await self._ban_mgr.kick(cid, "Отключены администратором")
            ])
            print(f" Отключено: {n}")
        else:
            real = self._resolve(target) or target
            if self._state.is_connected(real):
                await self._ban_mgr.kick(real)
                print(f" {real} отключен")
            else:
                print(f" {real} не подключен")

    # ── группы ───────────────────────────────────────────────────────────

    def group_new(self, args: list):
        if len(args) < 1:
            print("Формат: group_new <n>")
            return
        name = args[0]
        if self._group_mgr.get_members(name) is not None:
            print(f" Группа '{name}' уже существует")
            return
        members = self._collect_users("добавления")
        if members:
            try:
                self._group_mgr.create(name, members)
                print(f" Группа '{name}' создана ({len(members)} уч.)")
            except Exception as e:
                print(f" Ошибка: {e}")
        else:
            print(" Группа не создана")

    def group_list(self, args: list):
        groups = self._group_mgr.get_data()
        if not groups:
            print(" Нет групп")
            return
        print(f"\n{'=' * 60}\nГРУППЫ\n{'=' * 60}")
        for name, members in groups.items():
            print(f"  {name} ({len(members)} уч.):")
            for m in members:
                print(f"    - {m}")
        print(f"{'=' * 60}\n")

    def group_del(self, args: list):
        if len(args) < 1:
            print("Формат: group_del <n>")
            return
        try:
            self._group_mgr.delete(args[0])
            print(f" Группа '{args[0]}' удалена")
        except Exception as e:
            print(f" Ошибка: {e}")

    def group_add(self, args: list):
        self._group_modify(args, add=True)

    def group_rm(self, args: list):
        self._group_modify(args, add=False)

    def _group_modify(self, args: list, add: bool):
        if len(args) < 1:
            print(f"Формат: group_{'add' if add else 'rm'} <n>")
            return
        name = args[0]
        if self._group_mgr.get_members(name) is None:
            print(f" Группа '{name}' не найдена")
            return
        verb    = "добавления" if add else "удаления"
        users   = self._collect_users(verb)
        if not users:
            return
        fn      = self._group_mgr.add_users if add else self._group_mgr.remove_users
        skipped = fn(name, users)
        done    = len(users) - len(skipped)
        action  = "Добавлено" if add else "Удалено"
        print(f" {action}: {done}. Пропущено: {skipped}")

    def _collect_users(self, verb: str) -> list:
        """Интерактивный сбор пользователей из stdin."""
        print(f"Введите пользователей для {verb} (EXIT — завершить):")
        users = []
        while True:
            u = input("  > ").strip()
            if u.upper() == "EXIT":
                break
            real = self._resolve(u)
            if real and real not in users:
                users.append(real)
            else:
                print(f" '{u}' не найден или уже в списке")
        return users

    # ── отложенные команды ────────────────────────────────────────────────

    def chart_new(self, args: list):
        print("\n" + "=" * 60 + "\nСОЗДАНИЕ ОТЛОЖЕННОЙ КОМАНДЫ\n" + "=" * 60)
        valid_types = [ServerCmd.CMD, ServerCmd.SIMPL, ServerCmd.IMPORT, ServerCmd.EXPORT]

        while True:

            raw    = input("Цель (all / username / group:name / EXIT): ").strip()
            target = self.ask_who(raw)
            if target == ServerCmd.EXIT:
                print (" Отмена")
                break

            if target:
                pass
            else:
                continue


            while True:

                cmd_type = input ("Тип (CMD/SIMPL/IMPORT/EXPORT/EXIT): ").strip ().lower()
                if cmd_type == ServerCmd.EXIT:
                    print (" Отмена")
                    break
                if cmd_type in [ServerCmd.CMD, ServerCmd.SIMPL, ServerCmd.IMPORT, ServerCmd.EXPORT]:
                    data_cmd=self.ask_cmd(cmd_type)
                    if data_cmd == "EXIT":
                        print (" Отмена отложенной команды")
                        break

                    try:
                        self._sched_mgr.create (target, cmd_type, data_cmd)
                        print (f" Добавлено для '{target}'")
                    except Exception as e:
                        print (f" Ошибка: {e}")
                    break
                print (f" Неверный тип '{cmd_type}'. Допустимые: {', '.join (valid_types)}")
            break


    def ask_who(self,msg) -> Optional[str]:
        if not msg:
            print (" Поле не может быть пустым")
            return False
        if msg.upper () == "EXIT":
            return "EXIT"
        if msg.startswith ("group:"):
            name = msg[6:]
            if self._group_mgr.get_members(name):
                return msg
            print(f" Группа '{name}' не найдена")
            return False
        real = self._resolve(msg)
        if real:
            return real
        print(f" Пользователь '{msg}' не найден")
        return False

    def ask_cmd(self,cmd_type:str):
        if cmd_type == ServerCmd.CMD:
            while True:
                command = input ("Команда (EXIT — отмена): ").strip ()
                if command.upper () == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if command:
                    return {"command": command}
                print (" Команда не может быть пустой")

        elif cmd_type == ServerCmd.SIMPL:
            if not Config.FILE_CODE.exists ():
                print (f" Файл {Config.FILE_CODE} не найден")
                return ServerCmd.EXIT
            print (f" Будут выполнены команды из {Config.FILE_CODE}")
            return {}

        elif cmd_type == ServerCmd.IMPORT:
            print ("Будьте внимательны в указании слеша!")
            while True:
                src = input ("Путь на сервере (EXIT — отмена): ").strip ()
                if src.upper () == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if not src:
                    print (" Путь не может быть пустым")
                    continue
                if not Path (self._sched_mgr.sub_serv_path(src)).exists ():
                    print (f" Путь '{src}' не существует")
                    continue
                break
            while True:
                dst = input ("Путь на клиенте (EXIT — отмена): ").strip ()
                if dst.upper () == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if dst:
                    return {"source_path": src, "dest_path": dst}
                print (" Путь не может быть пустым")

        elif cmd_type == ServerCmd.EXPORT:
            while True:
                src = input ("Путь на клиенте (EXIT — отмена): ").strip ()
                if src.upper () == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if src:
                    break
                print (" Путь не может быть пустым")
            dst = input ("Путь на сервере [не обязателен]: ").strip ()
            if dst.upper () == ServerCmd.EXIT:
                return ServerCmd.EXIT
            return {"source_path": src, "dest_path": dst or "received"}

        return ServerCmd.EXIT

    # def _resolve_chart_target(self, raw: str) -> Optional[str]:
    #     if raw.upper() == "EXIT":
    #         return None
    #     if raw.upper() == "ALL":
    #         return "ALL"
    #     if raw.startswith("group:"):
    #         name = raw[6:]
    #         if self._group_mgr.get_members(name):
    #             return raw
    #         print(f" Группа '{name}' не найдена")
    #         return None
    #     real = self._resolve(raw)
    #     if real:
    #         return real
    #     print(f" Пользователь '{raw}' не найден")
    #     return None
    #
    # def _ask_cmd_extra(self, cmd_type: str) -> Optional[Dict]:
    #     if cmd_type == "CMD":
    #         while True:
    #             cmd = input("Команда (EXIT — отмена): ").strip()
    #             if cmd.upper() == "EXIT":
    #                 return None
    #             if cmd:
    #                 return {"command": cmd}
    #             print(" Не может быть пустой")
    #
    #     elif cmd_type == "SIMPL":
    #         if not Config.FILE_CODE.exists():
    #             print(f" Файл {Config.FILE_CODE} не найден")
    #             return None
    #         return {}
    #
    #     elif cmd_type in ("IMPORT", "EXPORT"):
    #         prompt_src = "Путь на сервере" if cmd_type == "IMPORT" else "Путь на клиенте"
    #         while True:
    #             src = input(f"{prompt_src} (EXIT): ").strip()
    #             if src.upper() == "EXIT":
    #                 return None
    #             if src and (cmd_type == "EXPORT" or Path(src).exists()):
    #                 break
    #             print(" Путь пуст или не существует")
    #         while True:
    #             dst = input("Путь назначения (EXIT): ").strip()
    #             if dst.upper() == "EXIT":
    #                 return None
    #             if dst:
    #                 return {"source_path": src, "dest_path": dst}
    #             print(" Не может быть пустым")
    #
    #     return None

    def chart_list(self, args: list):
        cmds = self._sched_mgr.get_all()
        if not cmds:
            print(" Нет отложенных команд")
            return
        print(f"\n{'=' * 60}\nОТЛОЖЕННЫЕ КОМАНДЫ\n{'=' * 60}")
        for i, cmd in enumerate(cmds):
            ctype = cmd["command_type"]
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            print(f"[{i}] {cmd['target']} → {ctype}: {label}")
            if n := len(cmd.get("completed_users", [])):
                print(f"    ✓ Выполнено: {n}")
            if n := len(cmd.get("expected_users", [])):
                print(f"    ⏳ Ожидает: {n}")
        print(f"{'=' * 60}\n")

    def chart_del(self, args: list):
        if len(args) < 1:
            print("Формат: chart_del <index>")
            return
        try:
            self._sched_mgr.delete(int(args[0]))
            print(f" Команда [{args[0]}] удалена")
        except Exception as e:
            print(f" Ошибка: {e}")

    def chart_comd(self):
        data      = self._sched_mgr.get_all()
        completed = data.get("completed", [])
        active    = data.get("commands", [])

        print(f"\n{'=' * 70}\nВЫПОЛНЕННЫЕ: {len(completed)}\n{'=' * 70}")
        for i, cmd in enumerate(completed):
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            print(f"\n[{i}] {cmd['target']} → {cmd['command_type']}: {label} ({cmd.get('completed_at','?')}) ✓")
            for u in cmd.get("completed_users", []):
                print(f"    ├─ {u} ✓")

        print(f"\n{'=' * 70}\nВ ПРОЦЕССЕ\n{'=' * 70}")
        for i, cmd in enumerate(active):
            cu = cmd.get("completed_users", [])
            eu = cmd.get("expected_users",  [])

            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            print(f"\n[{i}] {cmd['target']} → {cmd['command_type']}: {label}")
            for u in cu:
                print(f"    ├─ {u} ✓")
            for e in eu:
                print (f"    ├─ {e} ")
        print(f"{'=' * 70}\n")


# ═══════════════════════════════════════════════════════════════════════════
# ДИСПЕТЧЕР СЕРВЕРНЫХ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

def _sync_to_async(fn: Callable) -> Callable:
    """Оборачивает синхронный метод в корутину для единообразия диспетчера."""
    async def wrapper(args: list):
        fn(args)
    return wrapper


class ServerDispatcher:
    """Маршрутизирует ServerCmd → метод CommandHandler."""

    def __init__(self, handler: CommandHandler):
        h = handler
        s = _sync_to_async  # короткий псевдоним

        self._map: Dict[ServerCmd, Callable] = {
            ServerCmd.CMD:        h.cmd,
            ServerCmd.SIMPL:      h.simpl,
            ServerCmd.EXPORT:     h.export,
            ServerCmd.IMPORT:     h.import_,
            ServerCmd.SAVE:       h.save,
            ServerCmd.LIST:       s(h.list_users),
            ServerCmd.RENAME:     s(h.rename),
            ServerCmd.STATUS:     s(h.status),
            ServerCmd.CANCEL:     h.cancel,
            ServerCmd.KICK:       h.kick,
            ServerCmd.HELP:       s(lambda _: print_help()),
            ServerCmd.GROUP_NEW:  s(h.group_new),
            ServerCmd.GROUP_LIST: s(h.group_list),
            ServerCmd.GROUP_DEL:  s(h.group_del),
            ServerCmd.GROUP_ADD:  s(h.group_add),
            ServerCmd.GROUP_RM:   s(h.group_rm),
            ServerCmd.CHART_NEW:  s(h.chart_new),
            ServerCmd.CHART_LIST: s(h.chart_list),
            ServerCmd.CHART_DEL:  s(h.chart_del),
            ServerCmd.CHART_COMD: s(h.chart_comd),
        }

    async def dispatch(self, cmd: ServerCmd, args: list):
        fn = self._map.get(cmd)
        if fn:
            await fn(args)
        else:
            print(f" Команда '{cmd}' не реализована")


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК КЛИЕНТСКОГО ПРОТОКОЛА
# ═══════════════════════════════════════════════════════════════════════════

class ProtocolHandler:
    """Вся логика обработки входящих сообщений от конкретного клиента."""

    def __init__(self, client_id: str, state: ServerState,
                 user_mgr: UserManager, sched_mgr: ScheduledManager,
                 monitor: CommandMonitor):
        self._cid      = client_id
        self._state    = state
        self._user_mgr = user_mgr
        self._sched    = sched_mgr
        self._monitor  = monitor

    # ── внутренние helpers ───────────────────────────────────────────────

    def _writer(self) -> Optional[asyncio.StreamWriter]:
        return self._state.get_writer(self._cid)

    def _finish_command(self, combined: str):
        """Сохраняет вывод, помечает отложенную команду и снимает регистрацию."""
        cmd_info = self._state.get_command(self._cid)
        if not cmd_info:
            return
        self._monitor.save_output(self._cid, cmd_info["command"], combined, cmd_info["type"])
        if self._state.has_scheduled(self._cid):
            idx = self._state.pop_scheduled(self._cid)
            self._sched.mark_done(idx, self._cid, combined)
        self._state.unregister_command(self._cid)
        self._state.clear_buffer(self._cid)


    def _accumulate(self, chunk: str) -> Optional[str]:
        """
        Накапливает вывод в cmd_info.
        Возвращает объединённый результат когда все чанки получены,
        иначе None (для SIMPL — очищает lines для следующей итерации).
        """
        cmd_info = self._state.get_command(self._cid)
        if not cmd_info:
            return None
        cmd_info["accumulated_output"].append(chunk)
        cmd_info["received_commands"] += 1
        if cmd_info["received_commands"] >= cmd_info["total_commands"]:
            return "\n\n".join(cmd_info["accumulated_output"])
        # SIMPL: ещё ждём следующую порцию
        buf = self._state.get_buffer(self._cid)
        if buf:
            buf["lines"] = []
        return None

    def _print_output(self, label: str, text: str):
        print(f"\n{'=' * 80}\n[{label} от {self._cid}]\n{'=' * 80}\n{text}\n{'=' * 80}\n")

    # ── OUTPUT ───────────────────────────────────────────────────────────

    async def on_output_start(self, payload: str):
        self._state.init_buffer(self._cid, "OUTPUT", int(payload) if payload.isdigit() else 0)

    def on_output_chunk(self, payload: str):
        self._state.append_chunk(self._cid, payload.replace("<<<NL>>>", "\n"))

    def on_output_end(self, _: str = ""):
        output   = self._state.flush_buffer(self._cid)
        self._print_output("OUTPUT", output)
        combined = self._accumulate(output)
        if combined is not None:
            self._finish_command(combined)

    # ── FILETRU ──────────────────────────────────────────────────────────

    async def on_filetru_start(self, payload: str):
        self._state.init_buffer(self._cid, "FILETRU", int(payload) if payload.isdigit() else 0)

    def on_filetru_chunk(self, payload: str):
        self._state.append_chunk(self._cid, payload.replace("<<<NL>>>", "\n"))

    def on_filetru_end(self, _: str = ""):
        output   = self._state.flush_buffer(self._cid)
        cmd_info = self._state.get_command(self._cid)
        if cmd_info:
            cmd_info["accumulated_output"].append(output)
            cmd_info["received_commands"] += 1
            Logger.log("DEBUG",
                       f"FILETRU: {cmd_info['received_commands']}/{cmd_info['total_commands']}",
                       self._cid)
            if cmd_info["received_commands"] >= cmd_info["total_commands"]:
                combined = "\n\n".join(cmd_info["accumulated_output"])
                self._print_output ("FILETRU", combined)
                self._finish_command(combined)

        self._state.clear_buffer(self._cid)

    # ── EXPORT ───────────────────────────────────────────────────────────

    async def on_export_start(self, payload: str, reader: asyncio.StreamReader):
        try:
            meta     = json.loads(payload)
            count    = meta["count"]
            dest_dir = meta.get("dest_dir", "received")
            info     = self._user_mgr.get_user_info(self._cid)
            alias    = info["alias"] if info else self._cid
            save_dir = Path(Config.DIR_FILES) / alias / dest_dir
            writer   = self._writer()

            print(f" Получение {count} файлов от {alias} → {save_dir}")

            for _ in range(count):
                meta_line = await asyncio.wait_for(reader.readline(), timeout=30)
                meta_str  = meta_line.decode("utf-8", errors="ignore").strip()
                if not meta_str.startswith("FILE:META:"):
                    break
                file_meta = json.loads(meta_str[10:])
                save_path = save_dir / file_meta["rel_path"]
                if not await FileTransfer.receive_file(reader, save_path, file_meta["size"]):
                    if writer:
                        writer.write(b"EXPORT:ABORT\n")
                        await writer.drain()
                    break

            confirm = await asyncio.wait_for(reader.readline(), timeout=10)
            if confirm.decode("utf-8", errors="ignore").strip() == "EXPORT:COMPLETE":
                Logger.log("EXPORT", "✓ Завершён", self._cid)
                if self._state.has_scheduled(self._cid):
                    idx = self._state.pop_scheduled(self._cid)
                    self._sched.mark_done(idx, self._cid, f"EXPORT: {count} файлов [OK]")
                self._state.unregister_command(self._cid)

        except Exception as e:
            Logger.log("ERROR", f"Ошибка EXPORT: {e}", self._cid)

    # ── IMPORT ───────────────────────────────────────────────────────────

    def on_import_complete(self, _: str = ""):
        Logger.log("IMPORT", "✓ Завершён", self._cid)
        self._state.unregister_command(self._cid)

    def on_import_error(self, payload: str):
        Logger.log("ERROR", f"Импорт: {payload}", self._cid)
        self._state.unregister_command(self._cid)


# ═══════════════════════════════════════════════════════════════════════════
# ДИСПЕТЧЕР КЛИЕНТСКОГО ПРОТОКОЛА
# ═══════════════════════════════════════════════════════════════════════════

class ProtocolDispatcher:
    """
    Маршрутизирует ClientMsg → метод ProtocolHandler.
    Единая точка входа: dispatch(msg_type, payload, reader).
    """

    def __init__(self, handler: ProtocolHandler):
        h = handler

        # Асинхронные обработчики (payload + возможно reader)
        self._async_handlers: Dict[ClientMsg, Callable] = {
            ClientMsg.OUTPUT_START:  h.on_output_start,
            ClientMsg.FILETRU_START: h.on_filetru_start,
            # EXPORT_START требует reader — обрабатывается отдельно
        }

        # Синхронные обработчики (вызываются с payload)
        self._sync_handlers: Dict[ClientMsg, Callable] = {
            ClientMsg.OUTPUT_CHUNK:    h.on_output_chunk,
            ClientMsg.OUTPUT_END:      h.on_output_end,
            ClientMsg.FILETRU_CHUNK:   h.on_filetru_chunk,
            ClientMsg.FILETRU_END:     h.on_filetru_end,
            ClientMsg.IMPORT_COMPLETE: h.on_import_complete,
            ClientMsg.IMPORT_ERROR:    h.on_import_error,
        }

        self._handler = h

    async def dispatch(self, msg_type: ClientMsg, payload: str,
                       reader: asyncio.StreamReader):
        if msg_type == ClientMsg.EXPORT_START:
            await self._handler.on_export_start(payload, reader)

        elif msg_type in self._async_handlers:
            await self._async_handlers[msg_type](payload)

        elif msg_type in self._sync_handlers:
            self._sync_handlers[msg_type](payload)

        else:
            Logger.log("WARNING", f"Нет обработчика для {msg_type}", show_console=False)


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТКА КЛИЕНТА
# ═══════════════════════════════════════════════════════════════════════════

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        state: ServerState, user_mgr: UserManager,
                        sched_mgr: ScheduledManager, monitor: CommandMonitor):
    addr      = writer.get_extra_info("peername")
    client_id = None

    try:
        # Рукопожатие: "username,os,home_path"
        raw       = await asyncio.wait_for(reader.readline(), timeout=10)
        parts     = raw.decode("utf-8").strip().split(",")
        client_id = parts[0]

        if not client_id:
            writer.close()
            await writer.wait_closed()
            return

        user_mgr.register(client_id, parts[1], parts[2])
        state.add_client(client_id, writer)
        Logger.log("CONNECT", f"подключился ({addr})", client_id)
        state.save()

        # Диспетчеры для этого клиента
        proto_handler    = ProtocolHandler(client_id, state, user_mgr, sched_mgr, monitor)
        proto_dispatcher = ProtocolDispatcher(proto_handler)

        # Отложенные команды
        await _run_scheduled(client_id, writer, state, user_mgr, sched_mgr)

        # Основной цикл приёма сообщений
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

                try:
                    msg_type, payload = ClientMsgParser.parse(msg)
                    await proto_dispatcher.dispatch(msg_type, payload, reader)
                except ValueError:
                    Logger.log("WARNING", f"Неизвестное сообщение: {msg[:60]}",
                               client_id, show_console=False)

            except asyncio.TimeoutError:
                Logger.log("WARNING", "Таймаут 5 мин", client_id, show_console=False)
                continue
            except ConnectionError:
                break
            except Exception as e:
                Logger.log("ERROR", f"Ошибка цикла: {e}", client_id)
                consecutive_errors += 1
                if consecutive_errors >= MAX_ERRORS:
                    Logger.log("ERROR", "Слишком много ошибок, отключение", client_id)
                    break

    except Exception as e:
        import traceback
        Logger.log("ERROR", f"Критическая ошибка: {e}", client_id)
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


async def _run_scheduled(client_id: str, writer: asyncio.StreamWriter,
                         state: ServerState, user_mgr: UserManager,
                         sched_mgr: ScheduledManager):
    """Выполняет накопленные отложенные команды при подключении клиента."""
    user_cmds    = sched_mgr.get_for_user(client_id)
    all_commands = sched_mgr.get_all()

    for cmd_data in user_cmds:
        try:
            idx      = all_commands.index(cmd_data)
            cmd_type = cmd_data["command_type"]

            def sub(text: str) -> str:
                return sched_mgr.sub_path(text, client_id)

            if cmd_type == "CMD":
                command = sub(cmd_data["command"])
                state.register_command(client_id, command, "CMD", 1)
                writer.write(f"CMD:{command}\n".encode())
                await writer.drain()
                state.push_scheduled(client_id, idx)

            elif cmd_type == "SIMPL":
                if Config.FILE_CODE.exists():
                    commands = [l.strip() for l in
                                Config.FILE_CODE.read_text("utf-8").splitlines() if l.strip()]
                    if commands:
                        state.register_command(client_id, f"simpl ({len(commands)} команд)",
                                               "FILETRU", len(commands))
                        for cmd in commands:
                            writer.write(f"FILETRU:{sub(cmd)}\n".encode())
                            await writer.drain()
                            await asyncio.sleep(0.2)
                        state.push_scheduled(client_id, idx)

            elif cmd_type == "IMPORT":
                src = sub(cmd_data["source_path"])
                dst = sub(cmd_data["dest_path"])
                state.register_command(client_id, f"import {src}", "IMPORT", 1)
                await FileTransfer.send_to_client(client_id, src, dst, state)
                state.unregister_command(client_id)
                sched_mgr.mark_done(idx, client_id, f"IMPORT: {src} → {dst} [OK]")

            elif cmd_type == "EXPORT":
                src = sub(cmd_data["source_path"])
                dst = sub(cmd_data["dest_path"])
                state.register_command(client_id, f"export {src}", "EXPORT", 1)
                writer.write(f"EXPORT;{src};{dst}\n".encode())
                await writer.drain()
                state.push_scheduled(client_id, idx)

            await asyncio.sleep(0.3)

        except Exception as e:
            Logger.log("ERROR", f"Ошибка отложенной команды: {e}", client_id)


# ═══════════════════════════════════════════════════════════════════════════
# СЕРВЕРНЫЙ ВВОД
# ═══════════════════════════════════════════════════════════════════════════

async def server_input(server, dispatcher: ServerDispatcher,
                       state: ServerState, user_mgr: UserManager):
    """Чистый цикл ввода: parse → dispatch. Вся логика в CommandHandler."""
    loop = asyncio.get_event_loop()

    while True:
        raw = await loop.run_in_executor(None, input, "Server> ")
        if not raw.strip():
            continue

        try:
            cmd, args = ServerCmdParser.parse(raw)
        except ValueError as e:
            print(f" {e}")
            continue

        if cmd == ServerCmd.EXIT:
            print("\n Остановка сервера...")
            _cleanup(state, user_mgr)
            server.close()
            await server.wait_closed()
            break

        try:
            await dispatcher.dispatch(cmd, args)
        except Exception as e:
            print(f" Ошибка: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# ЗАВЕРШЕНИЕ / СИГНАЛЫ / ПЕРИОДИКА
# ═══════════════════════════════════════════════════════════════════════════

def _cleanup(state: ServerState, user_mgr: UserManager):
    Logger.log("INFO", "Graceful shutdown...")
    users = user_mgr.get_users_data()
    now   = time.strftime("%Y-%m-%d %H:%M:%S")
    for uname in list(state.get_all_clients()):
        if uname in users:
            users[uname].update({"status": "OFF", "last_logout": now})
            user_mgr._log_session(uname, "logout")
    user_mgr.save_user_data(users)
    state.save()
    Logger.log("INFO", "Сервер остановлен")


def setup_signal_handlers(state: ServerState, user_mgr: UserManager):
    def handler(signum, frame):
        Logger.log("WARNING", f"Сигнал {signum}")
        _cleanup(state, user_mgr)
        sys.exit(0)
    signal.signal(signal.SIGINT,  handler)
    signal.signal(signal.SIGTERM, handler)


async def periodic_save(state: ServerState):
    while True:
        await asyncio.sleep(Config.STATE_SAVE_INTERVAL)
        state.save()


async def _aiter(lst: list):
    for item in lst:
        yield item


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def TCPServer():
    ensure_dirs()

    # Зависимости
    state     = ServerState()
    user_mgr  = UserManager()
    group_mgr = GroupManager(user_mgr,state)
    sched_mgr = ScheduledManager(user_mgr, group_mgr)
    monitor   = CommandMonitor(state, user_mgr)
    ban_mgr   = BanManager(state)


    # Обработчик и диспетчер серверных команд
    handler    = CommandHandler(state, user_mgr, group_mgr, sched_mgr, ban_mgr, monitor)
    dispatcher = ServerDispatcher(handler)

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
        Logger.log("INFO", f"Запущен на {Config.HOST}:{Config.PORT}")
        print_help()

        async with server:
            await asyncio.gather(
                server.serve_forever(),
                server_input(server, dispatcher, state, user_mgr),
                monitor.monitor_loop(),
                periodic_save(state),
            )
    except Exception as e:
        import traceback
        Logger.log("CRITICAL", f"Критическая ошибка: {e}")
        Logger.crash(e, traceback.format_exc(), state)
        _cleanup(state, user_mgr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(TCPServer())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        import traceback
        Logger.crash(e, traceback.format_exc(), ServerState())
