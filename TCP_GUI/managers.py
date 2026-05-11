"""
managers.py — менеджеры состояния, пользователей, групп,
              отложенных команд, логирования и передачи файлов.

Назначение:
    Вся работа с данными и I/O, не связанная с бизнес-логикой команд.
    handlers.py и server.py используют эти классы как сервисный слой.

Что содержит:
    get_local_time()  — текущее время с учётом Config.TIMEZONE_OFFSET.
    ensure_dirs()     — создаёт все рабочие директории при старте сервера.

    Logger            — статические методы log() и crash().
                        log() пишет в файл YYYY-MM-DD.log и вызывает
                        глобальный _log_callback (подписывается GUI).
                        crash() пишет трейсбек + состояние в crash.log.
    set_log_callback()— GUI регистрирует свой обработчик строк лога.

    ServerState       — in-memory состояние сервера:
                        • словарь подключённых клиентов (username → StreamWriter),
                        • буферы вывода (чанки от клиентов),
                        • активные команды (тип, время, счётчики),
                        • очередь индексов отложенных команд,
                        • last_output каждого клиента.
                        Сохраняет снимок в server_state.json каждые
                        Config.STATE_SAVE_INTERVAL секунд.

    UserManager       — CRUD пользователей поверх users.json:
                        регистрация (с транслитерацией кириллицы → alias),
                        logout, валидация имени/alias, история сессий
                        (history/<username>.json).

    GroupManager      — CRUD групп поверх json/groups.json:
                        создание, удаление, добавление/удаление участников,
                        получение онлайн-участников группы.

    ScheduledManager  — очередь отложенных команд (json/scheduled_commands.json):
                        добавление задач (CMD/SIMPL/IMPORT/EXPORT),
                        выборка задач для конкретного пользователя,
                        пометка выполненных, подстановка {user}/{path}/{path_serv}.

    BanManager        — отправляет KICK-сообщение и закрывает соединение.

    CommandMonitor    — фоновый asyncio-цикл:
                        предупреждает клиента при превышении WARNING_TIMEOUT,
                        отменяет команду при превышении COMMAND_TIMEOUT;
                        сохраняет каждый вывод в trash/output_<alias>.txt.

    FileTransfer      — статические методы бинарной передачи файлов по TCP:
                        list_files(), send_file(), receive_file(),
                        send_to_client() (инициирует IMPORT на стороне сервера).

Импортирует: config.py.
Импортируется из: handlers.py, server.py, gui.py.
"""

import asyncio

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Any

from config import Config
from test_to_OOP.TCP_server_v3_4 import ServerCmd


# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════

def get_local_time() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=Config.TIMEZONE_OFFSET)


def ensure_dirs():
    for d in [Config.DIR_SAVE, Config.DIR_TRASH, Config.DIR_HISTORY,
              Config.DIR_FILES, Config.DIR_LOGS, Config.DIR_JSON,
              Config.DIR_SCHEDULED_RESULTS, Config.DIR_FOR_SEND]:
        os.makedirs(d, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════

# Глобальный callback — GUI подписывается, чтобы получать строки лога
_log_callback = None

def set_log_callback(fn):
    """GUI вызывает это один раз при старте, чтобы получать все логи."""
    global _log_callback
    _log_callback = fn


class Logger:

    @staticmethod
    def log(level: str, message: str, client_id: Optional[str] = None,
            show_console: bool = True):
        local_time = get_local_time()
        timestamp  = local_time.strftime("%Y-%m-%d %H:%M:%S")
        entry = f"[{timestamp}]"
        if level:
            entry+=f"[{level}]"
        if client_id:
            entry += f" [{client_id}]"
        entry += f" {message}"

        if show_console:
            print(entry)

        # Отправляем строку в GUI (если подключён)
        if _log_callback:
            try:
                _log_callback(entry)
            except Exception:
                pass

        log_file = Config.DIR_LOGS / f"{local_time.strftime('%Y-%m-%d')}.log"
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
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
            "start_time":        time.time(),
            "command":           command,
            "type":              cmd_type,
            "total_commands":    cmd_count,
            "received_commands": 0,
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

    # ── public ───────────────────────────────────────────────────────────

    def register(self, username: str, os_name: str, home_path: str) -> str:
        from config import validate_username
        username = validate_username(username)
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

    def __init__(self, user_mgr: UserManager, state: ServerState):
        self._cache:    Optional[Dict] = None
        self._user_mgr: UserManager    = user_mgr
        self._state                    = state

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

    def get_online_users(self, group_name: str) -> list:
        members = self.get_members(group_name) or []
        return [u for u in members if self._state.is_connected(u)]

    def create(self, group_name: str, members: list) -> bool:
        groups = self._load()
        if group_name in groups:
            raise ValueError(f"Группа '{group_name}' уже существует")
        groups[group_name] = members
        self._sync_users(group_name, members, add=True)
        return self._save()

    def delete(self, group_name: str) -> bool:
        groups = self._load()
        if group_name not in groups:
            raise NameError(f"Группа '{group_name}' не найдена")
        self._sync_users(group_name, groups[group_name], add=False)
        del groups[group_name]
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
        cache = self._load()
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
        if target == "all":
            return self._user_mgr.get_all_usernames()
        if target.startswith("group:"):
            return self._group_mgr.get_members(target[6:]) or []
        return [target]

    def get_all(self) -> list:
        return self._load().get("commands", [])

    def get_completed(self) -> list:
        return self._load().get("completed", [])

    def get_for_user(self, username: str) -> list:
        return [c for c in self._load().get("commands", [])
                if username in c.get("expected_users", [])]

    def create(self, target: str, cmd_type: str, extra: Dict) -> bool:
        """
        Создаёт отложенную команду.
        extra для CMD:    {"command": "..."}
        extra для SIMPL:  {}
        extra для IMPORT: {"source_path": "...", "dest_path": "..."}
        extra для EXPORT: {"source_path": "...", "dest_path": "..."}
        """
        cmd_type = cmd_type.lower()
        if cmd_type not in self.VALID_TYPES:
            raise ValueError(f"Неверный тип: {cmd_type}")
        if cmd_type == ServerCmd.CMD and "command" not in extra:
            raise ValueError("CMD требует поле 'command'")
        if cmd_type in (ServerCmd.IMPORT, ServerCmd.EXPORT) and \
                not {"source_path", "dest_path"}.issubset(extra):
            raise ValueError("IMPORT/EXPORT требуют 'source_path' и 'dest_path'")

        if target not in ("all",) and not target.startswith("group:"):
            resolved = self._user_mgr.validate(target)
            if not resolved:
                raise NameError(f"Пользователь '{target}' не найден")
            target = resolved

        entry = {
            "target":          target,
            "command_type":    cmd_type.upper(),
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
        if target == "all":
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
        """Заменяет {user} и {path} на данные пользователя."""
        text = text.replace("{user}", username)
        if "{path}" in text:
            info = self._user_mgr.get_user_info(username)
            text = text.replace("{path}", info.get("default_path", "") if info else "")
        return text

    @staticmethod
    def sub_serv_path(text: str) -> str:
        """Заменяет {path_serv} на абсолютный путь send_file."""
        if "{path_serv}" in text:
            return str(Path(text.replace("{path_serv}", str(Config.DIR_FOR_SEND.resolve()))))
        return text
# ═══════════════════════════════════════════════════════════════════════════
# Списки команд
# ═══════════════════════════════════════════════════════════════════════════

class TemplateManager:

    def __init__(self):
        self._cache = None

    def _load(self) -> Dict:
        if self._cache is None:
            try:
                with open(Config.FILE_TEMPLATE, "r", encoding="utf-8") as f:
                    self._cache = json.load(f)
            except Exception:
                self._cache = {"default":""}
        return self._cache

    def _save(self) -> bool:
        if self._cache is None:
            return False
        try:
            tmp = Config.FILE_TEMPLATE.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cache, f, ensure_ascii=False, indent=2)
            tmp.replace(Config.FILE_TEMPLATE)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения шаблонных команд: {e}")
            self._cache = None
            return False
    def get_all_template_name(self) -> list:
        return list(self._load().keys())

    def get_comd_template_name(self,name)->list:
            return self._load().get(name,[])

    def check_template_name(self,name) -> bool:
        return name in self.get_all_template_name()

    def create(self, name: str, commands: list) -> bool:
        if name in self.get_all_template_name():
            raise NameError(f" Имя '{name}' уже используется")
        if not isinstance(commands,list):
            raise TypeError("Команды должны быть типом list")
        self._load()[name]=commands
        return self._save()

    def add_comd(self, name: str, commands: list) -> list:
        if name not in self.get_all_template_name():
            raise NameError(f"Имя '{name}' не найдено")

        if not isinstance(commands,list):
            raise TypeError("Команды должны быть списком")

        command = self.get_comd_template_name (name)
        skipped=[]
        for c in commands:
            if c in command:
                skipped.append(c)
            else:
                command.append(c)

        self._save()
        return skipped

    def rm_comd(self, name: str, indices: list) -> str:
        data = self._load ()
        if name not in data:
            raise KeyError(f"Шаблон '{name}' не найден")
        try:
            indexes = [int (i) for i in indices]
        except ValueError:
            return "Индексы должны быть числами"
        commands = data[name]
        data[name] = [cmd for i, cmd in enumerate (commands) if i not in set (indexes)]
        self._save ()

        return len (indexes)

    def delete(self, name: str) -> bool:
        data = self._load ()
        if name not in data:
            raise KeyError(f"Шаблон '{name}' не найден")
        del data[name]
        return self._save()

    def list_all_templte(self) -> str:
        data=self._load()
        if not data:
            return "Нет шаблонов"
        lines = []
        for name,comd in self._load().items():
            lines.append(f"Шалоны: {len(data.keys())}")
            lines.append(f"{name} -> {','.join(comd)} ")
        return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════════════════
# КИК
# ═══════════════════════════════════════════════════════════════════════════

class BanManager:

    def __init__(self, state: ServerState):
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
                    Logger.log("TIMEOUT", f"превышен лимит: {info['command']}", cid, False)
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
            writer.write(b"FILE:END\n")
            await writer.drain()
            Logger.log("INFO", f"Отправлен файл {rel_path} ({size / 1024:.1f} KB)",
                       show_console=False)
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
            end = await reader.readline()
            if not end.decode("utf-8", errors="ignore").strip().startswith("FILE:END"):
                Logger.log("WARNING", "Неожиданный маркер", show_console=False)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка получения: {e}")
            return False

    @staticmethod
    async def send_to_client(client_id: str, source: str, dest: str,
                             state: ServerState) -> bool:
        writer = state.get_writer(client_id)
        if not writer:
            return False
        path  = Path(source)
        files = FileTransfer.list_files(path)
        if not files:
            Logger.log("WARNING", f"Нет файлов: {source}")
            return False
        meta = {"count": len(files), "dest_dir": dest, "source": path.name}
        writer.write(f"IMPORT:START:{json.dumps(meta)}\n".encode())
        await writer.drain()
        for fi in files:
            if not await FileTransfer.send_file(writer, fi["path"], fi["rel_path"], fi["size"]):
                return False
        return True
