"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    TCP SERVER - СИСТЕМА УДАЛЁННОГО УПРАВЛЕНИЯ                ║
║                                 Финальна версия                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

ВОЗМОЖНОСТИ:
- Удалённое выполнение команд на клиентах
- Двусторонняя передача файлов (импорт/экспорт)
- Система пользователей с автоматической регистрацией
- Модерация: kick
- Полное логирование всех действий
- Автоматическое восстановление после сбоев
- Мониторинг таймаутов команд
- Сохранение результатов выполнения команд
- Поддержка флага 'all' для массовых операций
"""

import asyncio
import time
import os
import json
import signal
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Any


# ═══════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════

class Config:
    """Централизованная конфигурация сервера"""
    # Директории
    BASE_DIR = ".../HA_10"
    DIR_SAVE = f"{BASE_DIR}/save"
    DIR_TRASH = f"{BASE_DIR}/trash"
    DIR_HISTORY = f"{BASE_DIR}/history"
    DIR_FILES = f"{BASE_DIR}/files"
    DIR_LOGS = f"{BASE_DIR}/logs"
    DIR_JSON = f"{BASE_DIR}/json"
    DIR_SCHEDULED_RESULTS = f"{DIR_FILES}/scheduled_commands"

    # Файлы
    FILE_CODE = f"{BASE_DIR}/code.txt"
    FILE_USERS = f"{BASE_DIR}/users.json"
    FILE_STATE = f"{BASE_DIR}/server_state.json"
    FILE_CRASH_LOG = f"{BASE_DIR}/crash.log"
    FILE_GROUPS = f"{DIR_JSON}/groups.json"
    FILE_SCHEDULED = f"{DIR_JSON}/scheduled_commands.json"

    # Параметры сети
    HOST = "0.0.0.0"
    PORT = 9000

    # Параметры передачи
    CHUNK_SIZE = 65536  # 64KB
    
    # Таймауты
    COMMAND_TIMEOUT = 120  # 2 минуты
    WARNING_TIMEOUT = 90  # Предупреждение за 30 секунд
    STATE_SAVE_INTERVAL = 30  # Сохранение состояния каждые 30 секунд
    READ_TIMEOUT = 300  # 5 минут таймаут на чтение (защита от зависаний)

    # Часовой пояс
    TIMEZONE_OFFSET = +1  # +1 час от UTC


# Глобальное состояние
clients: Dict[str, asyncio.StreamWriter] = {}
output_buffers: Dict[str, Dict[str, Any]] = {}
last_outputs: Dict[str, Dict[str, Any]] = {}
active_commands: Dict[str, Dict[str, Any]] = {}
scheduled_tracking: Dict[str, list] = {}  # Отслеживание выполняемых отложенных команд {client_id: [индексы команд]}


# ═══════════════════════════════════════════════════════════════════════════
# УТИЛИТЫ
# ═══════════════════════════════════════════════════════════════════════════
def print_command():
    """Выводит список доступных команд сервера"""
    print("\n" + "=" * 80)
    print("ДОСТУПНЫЕ КОМАНДЫ:")
    print("  CMD <client|all> <команда>                   - Выполнить команду")
    print("  export <client|all> <path_client>            - Получить файлы с клиента")
    print("  import <client|all> <path_ser> [path_client] - Отправить файлы клиенту/всем")
    print("  save <client> <name>                         - Сохранить результат")
    print("  simpl <client|all>                           - Выполнить команды из файла")
    print()
    print("  chart_new                                    - Создать отложенную команду")
    print("  chart_list                                   - Показать отложенные команды")
    print("  chart_del <index>                            - Удалить отложенную команду")
    print("  chart_comd                                   - Вывести список выполненых команд")
    print()
    print("  group_new <name>                             - Создать группу")
    print("  group_list                                   - Показать группы")
    print("  group_del <name>                             - Удалить группу")
    print()
    print("  list                                       - Список пользователей")
    print("  status                                     - Активные команды")
    print("  cancel <client>                            - Отменить команду")
    print("  kick <client|all>                          - Отключить клиента/всех")
    print("  help                                       - Вывести список команд")
    print("  EXIT                                       - Остановить сервер")
    print("=" * 80 + "\n")
def get_local_time() -> datetime:
    """Получает текущее время с учётом часового пояса"""
    return datetime.now(timezone.utc) + timedelta(hours=Config.TIMEZONE_OFFSET)


def ensure_dirs():
    """Создаёт необходимые директории"""
    for dir_path in [Config.DIR_SAVE, Config.DIR_TRASH, Config.DIR_HISTORY,
                     Config.DIR_FILES, Config.DIR_LOGS, Config.DIR_JSON,
                     Config.DIR_SCHEDULED_RESULTS]:
        os.makedirs(dir_path, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# СИСТЕМА ЛОГИРОВАНИЯ
# ═══════════════════════════════════════════════════════════════════════════

class Logger:
    """Централизованная система логирования"""

    @staticmethod
    def log(level: str, message: str, client_id: Optional[str] = None,
            show_console: bool = True):
        """Универсальный метод логирования"""
        local_time = get_local_time()
        timestamp = local_time.strftime("%Y-%m-%d %H:%M:%S")

        log_entry = f"[{timestamp}] [{level}]"
        if client_id:
            log_entry += f" [{client_id}]"
        log_entry += f" {message}\n"

        if show_console:
            print(log_entry.strip())

        log_file = os.path.join(Config.DIR_LOGS, f"{local_time.strftime('%Y-%m-%d')}.log")
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(log_entry)
        except Exception:
            pass

    @staticmethod
    def crash(exception: Exception, traceback_str: str):
        """Логирование критических ошибок"""
        local_time = get_local_time()
        timestamp = local_time.strftime("%Y-%m-%d %H:%M:%S")

        crash_info = f"""
{'=' * 80}
КРИТИЧЕСКАЯ ОШИБКА СЕРВЕРА
Время: {timestamp}
Исключение: {type(exception).__name__}: {str(exception)}
{'=' * 80}
{traceback_str}
{'=' * 80}

Состояние на момент сбоя:
- Подключено клиентов: {len(clients)}
- Активных команд: {len(active_commands)}
- Клиенты: {list(clients.keys())}

Активные команды:
"""
        for client_id, cmd_info in active_commands.items():
            elapsed = time.time() - cmd_info["start_time"]
            crash_info += f"  - {client_id}: {cmd_info['type']} ({elapsed:.1f}s) - {cmd_info['command']}\n"

        crash_info += f"\n{'=' * 80}\n\n"

        try:
            with open(Config.FILE_CRASH_LOG, "a", encoding="utf-8") as f:
                f.write(crash_info)
        except Exception:
            pass

        print(crash_info)


# ═══════════════════════════════════════════════════════════════════════════
# УПРАВЛЕНИЕ СОСТОЯНИЕМ
# ═══════════════════════════════════════════════════════════════════════════

class StateManager:
    """Управление состоянием сервера"""

    @staticmethod
    def save() -> bool:
        """Сохраняет текущее состояние"""
        try:
            local_time = get_local_time()
            state = {
                "timestamp": time.time(),
                "datetime": local_time.strftime("%Y-%m-%d %H:%M:%S"),
                "connected_clients": list(clients.keys()),
                "active_commands": {
                    client_id: {
                        "command": cmd["command"],
                        "type": cmd["type"],
                        "start_time": cmd["start_time"],
                        "elapsed": time.time() - cmd["start_time"]
                    }
                    for client_id, cmd in active_commands.items()
                },
                "output_buffers": {
                    client_id: {
                        "type": buf["type"],
                        "chunks": buf["chunks"],
                        "total": buf["total"]
                    }
                    for client_id, buf in output_buffers.items()
                }
            }

            with open(Config.FILE_STATE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)

            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения состояния: {e}", show_console=False)
            return False

    @staticmethod
    def load() -> Optional[Dict]:
        """Загружает предыдущее состояние"""
        if not os.path.exists(Config.FILE_STATE):
            return None

        try:
            with open(Config.FILE_STATE, "r", encoding="utf-8") as f:
                state = json.load(f)

            # Игнорируем старые состояния (> 10 минут)
            age = time.time() - state["timestamp"]
            if age > 600:
                Logger.log("INFO", f"Состояние устарело ({age / 60:.1f} мин)", show_console=False)
                return None

            return state
        except Exception as e:
            Logger.log("ERROR", f"Ошибка загрузки состояния: {e}", show_console=False)
            return None


# ═══════════════════════════════════════════════════════════════════════════
# УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ
# ═══════════════════════════════════════════════════════════════════════════

class UserManager:
    """Управление пользователями и их историей"""

    CYRILLIC_MAP = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya'
    }

    @staticmethod
    def load_users() -> Dict:
        """Загружает данные пользователей"""
        if not os.path.exists(Config.FILE_USERS):
            return {"users": {}}
        try:
            with open(Config.FILE_USERS, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"users": {}}

    @staticmethod
    def save_users(data: Dict):
        """Сохраняет данные пользователей"""
        try:
            with open(Config.FILE_USERS, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения пользователей: {e}")

    @staticmethod
    def transliterate(username: str) -> str:
        """Транслитерация кириллицы в латиницу"""
        result = []
        for char in username:
            if 'а' <= char.lower() <= 'я' or char in 'ЁёЪъЬь':
                lower_char = char.lower()
                trans = UserManager.CYRILLIC_MAP.get(lower_char, char)
                if char.isupper() and trans:
                    trans = trans.capitalize()
                result.append(trans)
            else:
                result.append(char)
        return ''.join(result).replace(" ","_")

    @staticmethod
    def get_by_alias(alias: str) -> Optional[str]:
        """Находит username по alias"""
        users_data = UserManager.load_users()
        for username, info in users_data["users"].items():
            if info["alias"].lower() == alias.lower():
                return username
        return None

    @staticmethod
    def register(username: str) -> str:
        """Регистрирует или обновляет пользователя"""
        users_data = UserManager.load_users()
        time_now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        if username not in users_data["users"]:
            alias = UserManager.transliterate(username)
            users_data["users"][username] = {
                "alias": alias,
                "status": "ON",
                "last_login": time_now,
                "last_logout": None
            }
            Logger.log("INFO", f"Новый пользователь: {username} (alias: {alias})")
        else:
            alias = users_data["users"][username]["alias"]
            users_data["users"][username]["status"] = "ON"
            users_data["users"][username]["last_login"] = time_now
            Logger.log("INFO", f"Вход: {username} ({alias})")

        UserManager.save_users(users_data)
        UserManager._log_session(username, alias, "login")
        return alias

    @staticmethod
    def logout(username: str):
        """Выполняет logout пользователя"""
        users_data = UserManager.load_users()
        time_now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        if username in users_data["users"]:
            alias = users_data["users"][username]["alias"]
            users_data["users"][username]["status"] = "OFF"
            users_data["users"][username]["last_logout"] = time_now
            UserManager.save_users(users_data)
            UserManager._log_session(username, alias, "logout")
            Logger.log("INFO", f"Выход: {username} ({alias})")

    @staticmethod
    def _log_session(username: str, alias: str, action: str):
        """Логирует сессию пользователя"""
        history_file = os.path.join(Config.DIR_HISTORY, f"{alias}.json")

        if os.path.exists(history_file):
            try:
                with open(history_file, "r", encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = {"username": username, "alias": alias, "sessions": []}
        else:
            history = {"username": username, "alias": alias, "sessions": []}

        time_now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        if action == "login":
            history["sessions"].append({"login": time_now, "logout": None})
        elif action == "logout" and history["sessions"]:
            for session in reversed(history["sessions"]):
                if session["logout"] is None:
                    session["logout"] = time_now
                    break

        try:
            with open(history_file, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            Logger.log("ERROR", f"Ошибка записи истории для {alias}: {e}", show_console=False)


# ═══════════════════════════════════════════════════════════════════════════
# СИСТЕМА МОДЕРАЦИИ (KICK)
# ═══════════════════════════════════════════════════════════════════════════

class BanManager:
    """Управление кик пользователей"""

    @staticmethod
    async def kick_user(username: str, reason: str = "Отключен администратором"):
        """Отключает пользователя (kick)"""
        if username not in clients:
            return False

        try:
            writer = clients[username]

            # Отправляем сообщение о причине отключения
            kick_msg = f"KICK:{reason}\n"
            writer.write(kick_msg.encode('utf-8'))
            await writer.drain()

            # Даём время прочитать сообщение
            await asyncio.sleep(0.5)

            # Закрываем соединение
            writer.close()
            await writer.wait_closed()

            Logger.log("KICK", f"Пользователь {username} отключен. Причина: {reason}", show_console=True)
            return True

        except Exception as e:
            Logger.log("ERROR", f"Ошибка при kick {username}: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════
# СИСТЕМА ГРУПП
# ═══════════════════════════════════════════════════════════════════════════

class GroupManager:
    """Управление группами пользователей"""

    @staticmethod
    def load_groups() -> Dict[str, list]:
        """Загружает группы из файла"""
        if not os.path.exists(Config.FILE_GROUPS):
            return {}
        try:
            with open(Config.FILE_GROUPS, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            Logger.log("ERROR", f"Ошибка загрузки групп: {e}")
            return {}

    @staticmethod
    def save_groups(groups: Dict[str, list]) -> bool:
        """Сохраняет группы в файл"""
        try:
            with open(Config.FILE_GROUPS, "w", encoding="utf-8") as f:
                json.dump(groups, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения групп: {e}")
            return False

    @staticmethod
    def get_group_members(name: str) -> Optional[list]:
        """Получает список участников группы"""
        groups = GroupManager.load_groups()
        return groups.get(name)


# ═══════════════════════════════════════════════════════════════════════════
# СИСТЕМА ОТЛОЖЕННЫХ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class ScheduledCommandManager:
    """Управление отложенными командами"""

    @staticmethod
    def load_data() -> Dict:
        """Загружает все данные (команды + завершённые)"""
        if not os.path.exists(Config.FILE_SCHEDULED):
            return {"commands": [], "completed": []}
        try:
            with open(Config.FILE_SCHEDULED, "r", encoding="utf-8") as f:
                data = json.load(f)
                if "completed" not in data:
                    data["completed"] = []
                return data
        except Exception as e:
            Logger.log("ERROR", f"Ошибка загрузки отложенных команд: {e}")
            return {"commands": [], "completed": []}

    @staticmethod
    def save_data(data: Dict) -> bool:
        """Сохраняет все данные"""
        try:
            with open(Config.FILE_SCHEDULED, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения отложенных команд: {e}")
            return False

    @staticmethod
    def get_expected_users(target: str) -> list:
        """Получает список пользователей для target"""
        if target == "all":
            # Берём всех из users.json
            users_data = UserManager.load_users()
            return list(users_data["users"].keys())
        elif target.startswith("group:"):
            # Берём участников группы
            group_name = target[6:]
            members = GroupManager.get_group_members(group_name)
            return members if members else []
        else:
            # Конкретный пользователь
            return [target]

    @staticmethod
    def get_commands_for_user(username: str) -> list:
        """Получает невыполненные команды для пользователя"""
        data = ScheduledCommandManager.load_data()
        user_commands = []

        for cmd in data["commands"]:
            # Проверяем, ожидается ли этот пользователь
            if username in cmd.get("expected_users", []):
                user_commands.append(cmd)

        return user_commands

    @staticmethod
    def mark_completed(cmd_index: int, username: str, output: str):
        """Отмечает команду как выполненную для пользователя"""
        data = ScheduledCommandManager.load_data()
        
        if cmd_index >= len(data["commands"]):
            return False

        cmd = data["commands"][cmd_index]
        target = cmd["target"]

        # Переносим пользователя из expected в completed
        if username in cmd.get("expected_users", []):
            cmd["expected_users"].remove(username)
            cmd.setdefault("completed_users", []).append(username)

        # Записываем вывод в файл
        ScheduledCommandManager.write_output(target, username, output)

        # Если все выполнили - переносим в completed
        if not cmd["expected_users"]:
            cmd["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            data["completed"].append(cmd)
            data["commands"].pop(cmd_index)

        ScheduledCommandManager.save_data(data)
        return True

    @staticmethod
    def write_output(target: str, username: str, output: str):
        """Записывает вывод команды в файл"""
        # Определяем имя файла
        if target == "all":
            filename = "ALL.txt"
        elif target.startswith("group:"):
            group_name = target[6:]
            filename = f"group_{group_name}.txt"
        else:
            filename = f"{target}.txt"

        filepath = os.path.join(Config.DIR_SCHEDULED_RESULTS, filename)

        # Записываем в формате: username\n[output]\n\n\n
        try:
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(f"{username}\n")
                f.write(f"{output}\n\n\n")
        except Exception as e:
            Logger.log("ERROR", f"Ошибка записи вывода: {e}")

    @staticmethod
    def replace_user_placeholder(text: str, username: str) -> str:
        """Заменяет {User} на имя пользователя"""
        return text.replace("{user}", username)

    @staticmethod
    def remove_user_from_expected(username: str):
        """Удаляет пользователя из expected_users всех команд (при удалении из группы)"""
        data = ScheduledCommandManager.load_data()
        modified = False

        for cmd in data["commands"]:
            if username in cmd.get("expected_users", []):
                cmd["expected_users"].remove(username)
                modified = True

                # Если список пуст - переносим в completed
                if not cmd["expected_users"]:
                    cmd["completed_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                    data["completed"].append(cmd)
                    data["commands"].remove(cmd)

        if modified:
            ScheduledCommandManager.save_data(data)




# ═══════════════════════════════════════════════════════════════════════════
# СИСТЕМА ТАЙМАУТОВ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class CommandMonitor:
    """Мониторинг и управление командами"""

    @staticmethod
    def register(client_id: str, command: str, cmd_type: str, cmd_count: int):
        """Регистрирует начало выполнения команды"""
        active_commands[client_id] = {
            "start_time": time.time(),
            "command": command,
            "type": cmd_type,
            "total_commands": cmd_count,      # Для SIMPL - количество команд
            "received_commands": 0,           # Сколько результатов получено
            "accumulated_output": []          # Накопленные выводы
        }
        Logger.log("CMD_START", f"{cmd_type}: {command}", client_id, show_console=False)
        StateManager.save()

    @staticmethod
    def save_command_output(client_id: str, command: str, output: str, cmd_type: str):
        """Сохраняет вывод команды в txt файл"""
        try:
            with open(Config.FILE_USERS, "r", encoding="utf-8") as f:
                data=json.load(f)
                alias=data['users'][f"{client_id}"]["alias"]
            # Получаем текущее время выполнения
            execution_time = get_local_time().strftime("%Y-%m-%d %H:%M:%S")

            # Формируем имя файла
            filename = f"output_command_{alias}.txt"
            filepath = os.path.join(Config.DIR_TRASH, filename)

            # Формируем содержимое файла
            content = f"""Время выполнения: {execution_time}
Команда: {command}
Тип: {cmd_type}
{'=' * 80}
Вывод команды:
{'=' * 80}
{output}
{'=' * 80}
"""

            # Сохраняем файл
            with open(filepath, "a", encoding="utf-8") as f:
                f.write(content)

            Logger.log("SAVE_OUTPUT", f"Вывод сохранён в {filename}", client_id, show_console=False)

        except Exception as e:
            Logger.log("ERROR", f"Ошибка сохранения вывода команды: {e}", client_id)

    @staticmethod
    def unregister(client_id: str):
        """Удаляет команду из отслеживания"""
        if client_id in active_commands:
            elapsed = time.time() - active_commands[client_id]["start_time"]
            cmd_type = active_commands[client_id]['type']
            Logger.log("CMD_END", f"{cmd_type} завершена за {elapsed:.1f}s",
                       client_id, show_console=False)
            del active_commands[client_id]
            StateManager.save()

    @staticmethod
    async def monitor_loop():
        """Цикл мониторинга таймаутов"""
        warned_clients = set()

        while True:
            await asyncio.sleep(5)
            current_time = time.time()

            for client_id, cmd_info in list(active_commands.items()):
                elapsed = current_time - cmd_info["start_time"]

                # Предупреждение
                if elapsed > Config.WARNING_TIMEOUT and client_id not in warned_clients:
                    remaining = Config.COMMAND_TIMEOUT - elapsed
                    Logger.log("WARNING",
                               f"команда выполняется {elapsed:.0f}s, осталось {remaining:.0f}s",
                               client_id, show_console=False)

                    if client_id in clients:
                        try:
                            msg = f"\n Команда выполняется {elapsed:.0f}s. " \
                                  f"Осталось {remaining:.0f}s до завершения.\n"
                            clients[client_id].write(msg.encode('utf-8'))
                            await clients[client_id].drain()
                        except Exception:
                            pass

                    warned_clients.add(client_id)

                # Таймаут
                if elapsed > Config.COMMAND_TIMEOUT:
                    Logger.log("TIMEOUT",
                               f"команда превысила лимит ({elapsed:.0f}s): {cmd_info['command']}",
                               client_id)

                    if client_id in clients:
                        try:
                            clients[client_id].write(b"CMD:CANCEL_TIMEOUT\n")
                            await clients[client_id].drain()
                        except Exception:
                            pass

                    CommandMonitor.unregister(client_id)
                    warned_clients.discard(client_id)

                    if client_id in output_buffers:
                        output_buffers[client_id] = {
                            "type": None, "lines": [], "chunks": 0, "total": 0
                        }


# ═══════════════════════════════════════════════════════════════════════════
# ПЕРЕДАЧА ФАЙЛОВ
# ═══════════════════════════════════════════════════════════════════════════

class FileTransfer:
    """Управление передачей файлов"""

    @staticmethod
    def list_files(path: Path):
        """Рекурсивный список файлов"""
        files = []

        if not path.exists():
            return []

        if path.is_file():
            return [{
                "path": str(path),
                "rel_path": path.name,
                "size": path.stat().st_size
            }]

        for item in path.rglob("*"):
            if item.is_file():
                try:
                    files.append({
                        "path": str(item),
                        "rel_path": str(item.relative_to(path)),
                        "size": item.stat().st_size
                    })
                except Exception as e:
                    Logger.log("ERROR", f"Ошибка обработки {item}: {e}", show_console=False)

        return files

    @staticmethod
    async def send_file(writer: asyncio.StreamWriter, file_path: str,
                        rel_path: str, size: int) -> bool:
        """Отправка одного файла клиенту"""
        try:
            # Метаданные
            meta = json.dumps({"rel_path": rel_path, "size": size})
            writer.write(f"FILE:META:{meta}\n".encode('utf-8'))
            await writer.drain()

            # Данные
            sent = 0
            with open(file_path, "rb") as f:
                while chunk := f.read(Config.CHUNK_SIZE):
                    writer.write(chunk)
                    await writer.drain()
                    sent += len(chunk)
                    progress = sent * 100 // size if size > 0 else 100
                    print(f"\r  {rel_path}: {progress}% ", end="", flush=True)

            print(f"\r  {rel_path} ({size / 1024:.1f} KB)")

            # Маркер окончания
            writer.write(b"FILE:END\n")
            await writer.drain()
            return True

        except Exception as e:
            Logger.log("ERROR", f"Ошибка отправки {rel_path}: {e}")
            return False

    @staticmethod
    async def receive_file(reader: asyncio.StreamReader, dest_path: Path,
                           size: int) -> bool:
        """Получение одного файла от клиента"""
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
                    progress = received * 100 // size if size > 0 else 100
                    print(f"\r  {dest_path.name}: {progress}% ", end="", flush=True)

            print(f"\r  {dest_path.name} ({size / 1024:.1f} KB)")

            # Ждём маркер окончания
            end_marker = await reader.readline()
            end_str = end_marker.decode('utf-8', errors='ignore').strip()
            if not end_str.startswith("FILE:END"):
                Logger.log("WARNING", f"Неожиданный маркер: {end_str}", show_console=False)

            return True

        except Exception as e:
            Logger.log("ERROR", f"Ошибка получения файла: {e}")
            return False

    @staticmethod
    async def import_to_client(client_id: str, source_path: str, dest_dir: str):
        """Отправка файлов клиенту (IMPORT)"""
        if client_id not in clients:
            print(f" Клиент {client_id} не подключен")
            return

        writer = clients[client_id]

        try:
            path = Path(source_path)
            if not path.exists():
                print(f" Путь не существует: {source_path}")
                return

            files = FileTransfer.list_files(path)
            if not files:
                print(f" Нет файлов для передачи")
                return

            # Отправляем метаданные импорта
            metadata = {
                "count": len(files),
                "dest_dir": dest_dir,
                "source": path.name
            }
            writer.write(f"IMPORT:START:{json.dumps(metadata)}\n".encode('utf-8'))
            await writer.drain()

            total_size = sum(f["size"] for f in files)
            print(f" Отправка {len(files)} файлов ({total_size / 1024 / 1024:.2f} MB)")

            # Отправляем файлы
            for file_info in files:
                if not await FileTransfer.send_file(writer, file_info["path"],
                                                    file_info["rel_path"], file_info["size"]):
                    return

            print(" Ожидание подтверждения...")

        except Exception as e:
            Logger.log("ERROR", f"Ошибка импорта: {e}", client_id)


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТКА КЛИЕНТОВ
# ═══════════════════════════════════════════════════════════════════════════

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Обработка подключения клиента с защитой от падений"""
    addr = writer.get_extra_info('peername')
    client_id = None

    try:
        # Получаем ID клиента с таймаутом
        data = await asyncio.wait_for(reader.readline(), timeout=10)
        client_id = data.decode('utf-8').strip()

        if not client_id:
            writer.close()
            await writer.wait_closed()
            return

        # Регистрируем
        alias = UserManager.register(client_id)
        clients[client_id] = writer
        output_buffers[client_id] = {"type": None, "lines": [], "chunks": 0, "total": 0}

        Logger.log("CONNECT", f"подключился ({addr})", client_id, show_console=True)
        StateManager.save()

        # Выполняем отложенные команды для этого клиента
        user_commands = ScheduledCommandManager.get_commands_for_user(client_id)
        if user_commands:
            Logger.log("SCHEDULED", f"Выполнение {len(user_commands)} отложенных команд", client_id)
            scheduled_tracking[client_id] = []  # Инициализируем список для отслеживания
            
            # Получаем полные данные для определения индексов
            all_data = ScheduledCommandManager.load_data()
            all_commands = all_data["commands"]
            
            for cmd_data in user_commands:
                try:
                    # Находим индекс команды в общем списке
                    cmd_index = all_commands.index(cmd_data)
                    cmd_type = cmd_data["command_type"]

                    if cmd_type == "CMD":
                        # Отправляем через стандартный протокол CMD
                        command = ScheduledCommandManager.replace_user_placeholder(cmd_data["command"], client_id)
                        CommandMonitor.register(client_id, command, "CMD")
                        writer.write(f"CMD:{command}\n".encode('utf-8'))
                        await writer.drain()
                        # Запоминаем что это отложенная команда
                        scheduled_tracking[client_id].append(cmd_index)
                        Logger.log("SCHEDULED", f"CMD: {command}", client_id, show_console=False)

                    elif cmd_type == "SIMPL":
                        # Читаем команды из code.txt и отправляем через FILETRU
                        try:
                            with open(Config.FILE_CODE, "r", encoding="utf-8") as f:
                                commands = [line.strip() for line in f if line.strip()]
                            
                            if commands:
                                CommandMonitor.register(client_id, f"simpl ({len(commands)} команд)", "FILETRU",len(commands))
                                
                                for cmd in commands:
                                    # Заменяем {User} в каждой команде
                                    cmd_processed = ScheduledCommandManager.replace_user_placeholder(cmd, client_id)
                                    writer.write(f"FILETRU:{cmd_processed}\n".encode('utf-8'))
                                    await writer.drain()
                                    await asyncio.sleep(0.2)
                                
                                # Запоминаем - будет много результатов, но отмечаем после последнего
                                scheduled_tracking[client_id].append(cmd_index)
                                Logger.log("SCHEDULED", f"SIMPL: {len(commands)} команд", client_id, show_console=False)
                        except FileNotFoundError:
                            Logger.log("ERROR", f"Файл {Config.FILE_CODE} не найден", client_id)

                    elif cmd_type == "IMPORT":
                        # Импорт файла - используем существующую функцию FileTransfer
                        source_path = ScheduledCommandManager.replace_user_placeholder(cmd_data["source_path"], client_id)
                        dest_dir = ScheduledCommandManager.replace_user_placeholder(cmd_data["dest_path"], client_id)
                        CommandMonitor.register(client_id, f"import {source_path}", "IMPORT",1)
                        await FileTransfer.import_to_client(client_id, source_path, dest_dir)
                        CommandMonitor.unregister(client_id)
                        # Отмечаем как выполненную (IMPORT выполняется сразу)
                        ScheduledCommandManager.mark_completed(cmd_index, client_id, f"IMPORT: {source_path} → {dest_dir} [OK]")
                        Logger.log("SCHEDULED", f"IMPORT: {source_path} → {dest_dir}", client_id, show_console=False)

                    elif cmd_type == "EXPORT":
                        # Экспорт файла - отправляем команду EXPORT
                        source_path = ScheduledCommandManager.replace_user_placeholder(cmd_data["source_path"], client_id)
                        dest_dir = ScheduledCommandManager.replace_user_placeholder(cmd_data["dest_path"], client_id)
                        CommandMonitor.register(client_id, f"export {source_path}", "EXPORT",1)
                        writer.write(f"EXPORT;{source_path};{dest_dir}\n".encode('utf-8'))
                        await writer.drain()
                        # EXPORT будет отмечен когда файлы получены
                        scheduled_tracking[client_id].append(cmd_index)
                        Logger.log("SCHEDULED", f"EXPORT: {source_path} → {dest_dir}", client_id, show_console=False)

                    await asyncio.sleep(0.3)  # Небольшая задержка между командами

                except Exception as e:
                    Logger.log("ERROR", f"Ошибка выполнения отложенной команды: {e}", client_id)

        # Основной цикл с защитой от падений
        consecutive_errors = 0
        max_consecutive_errors = 5

        while True:
            try:
                # Чтение с таймаутом для защиты от зависаний
                data = await asyncio.wait_for(reader.readline(), timeout=Config.READ_TIMEOUT)

                if not data:
                    Logger.log("INFO", "Клиент закрыл соединение", client_id, show_console=False)
                    break

                msg = data.decode('utf-8', errors='ignore').strip()
                if not msg:
                    continue

                # Сброс счётчика ошибок при успешном чтении
                consecutive_errors = 0

                # ========== EXPORT - обработка с защитой от ошибок ==========
                try:
                    if msg.startswith("EXPORT:START:"):
                        metadata = json.loads(msg[13:])
                        count = metadata["count"]
                        dest_dir = metadata.get("dest_dir", "received")

                        # Получаем alias пользователя
                        try:
                            with open(Config.FILE_USERS, "r", encoding="utf-8") as f:
                                users_data = json.load(f)
                                alias = users_data['users'][client_id]["alias"]
                        except:
                            alias = client_id  # Fallback
                        
                        client_dir = Path(Config.DIR_FILES) / alias
                        save_dir = client_dir / dest_dir

                        print(f" Получение {count} файлов от {alias} в {save_dir}")
                        Logger.log("EXPORT", f"Получение {count} файлов", client_id, show_console=False)

                        for _ in range(count):
                            file_meta_line = await asyncio.wait_for(reader.readline(), timeout=30)
                            file_meta_str = file_meta_line.decode('utf-8', errors='ignore').strip()

                            if not file_meta_str.startswith("FILE:META:"):
                                Logger.log("ERROR", f"Ожидался FILE:META, получено: {file_meta_str}",
                                           client_id, show_console=False)
                                break

                            file_meta = json.loads(file_meta_str[10:])
                            save_path = save_dir / file_meta["rel_path"]

                            if not await FileTransfer.receive_file(reader, save_path, file_meta["size"]):
                                writer.write(b"EXPORT:ABORT\n")
                                await writer.drain()
                                break

                        # Подтверждение
                        confirm = await asyncio.wait_for(reader.readline(), timeout=10)
                        confirm_str = confirm.decode('utf-8', errors='ignore').strip()

                        if confirm_str == "EXPORT:COMPLETE":
                            Logger.log("EXPORT", "✓ Экспорт завершён успешно", client_id, show_console=True)
                            
                            # Проверяем была ли это отложенная команда
                            if client_id in scheduled_tracking and scheduled_tracking[client_id]:
                                cmd_index = scheduled_tracking[client_id].pop(0)
                                ScheduledCommandManager.mark_completed(cmd_index, client_id, f"EXPORT: {count} файлов [OK]")
                                Logger.log("SCHEDULED", f"Команда [{cmd_index}] отмечена как выполненная", client_id)
                            
                            CommandMonitor.unregister(client_id)

                        continue

                except (json.JSONDecodeError, KeyError) as e:
                    Logger.log("ERROR", f"Ошибка парсинга EXPORT: {e}", client_id)
                    consecutive_errors += 1
                    continue
                except asyncio.TimeoutError:
                    Logger.log("ERROR", "Таймаут при EXPORT", client_id)
                    consecutive_errors += 1
                    continue
                except Exception as e:
                    Logger.log("ERROR", f"Ошибка EXPORT: {e}", client_id)
                    consecutive_errors += 1
                    continue

                # ========== IMPORT - обработка с защитой ==========
                try:
                    if msg == "IMPORT:COMPLETE":
                        Logger.log("IMPORT", " Импорт завершён успешно", client_id, show_console=True)
                        CommandMonitor.unregister(client_id)
                        continue

                    if msg.startswith("IMPORT:ERROR:"):
                        error = msg[13:]
                        Logger.log("ERROR", f"Импорт: {error}", client_id, show_console=True)
                        CommandMonitor.unregister(client_id)
                        continue

                except Exception as e:
                    Logger.log("ERROR", f"Ошибка IMPORT: {e}", client_id)
                    consecutive_errors += 1
                    continue

                # ========== OUTPUT chunks - обработка с защитой ==========
                try:
                    if msg.startswith("OUTPUT:START:"):
                        buffer = output_buffers[client_id]
                        buffer["type"] = "OUTPUT"
                        buffer["lines"] = []
                        buffer["chunks"] = 0
                        try:
                            buffer["total"] = int(msg.split(":")[-1])
                        except (ValueError, IndexError):
                            buffer["total"] = 0

                        # Не вызываем unregister здесь - команда ещё не завершена
                        continue

                    if msg.startswith("OUTPUT:CHUNK:"):
                        chunk_data = msg[13:]
                        chunk_restored = chunk_data.replace('<<<NL>>>', '\n')
                        output_buffers[client_id]["lines"].append(chunk_restored)
                        output_buffers[client_id]["chunks"] += 1
                        continue

                    if msg == "OUTPUT:END":
                        buffer = output_buffers[client_id]
                        full_output = '\n'.join(buffer["lines"])

                        last_outputs[client_id] = {
                            "content": full_output,
                            "type": "OUTPUT",
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                        }

                        print(f"\n{'=' * 80}\n[РЕЗУЛЬТАТ от {client_id}]\n{'=' * 80}")
                        print(full_output)
                        print(f"{'=' * 80}\n")

                        # Проверяем это SIMPL (несколько команд) или обычная команда
                        if client_id in active_commands:
                            cmd_info = active_commands[client_id]
                            
                            # Накапливаем вывод
                            cmd_info["accumulated_output"].append(full_output)
                            cmd_info["received_commands"] += 1


                            
                            # Проверяем получили ли все результаты
                            if cmd_info["received_commands"] >= cmd_info["total_commands"]:
                                # Все результаты получены - объединяем и сохраняем
                                combined_output = "\n\n".join(cmd_info["accumulated_output"])

                                command = cmd_info["command"]
                                CommandMonitor.save_command_output(client_id, command, combined_output, "OUTPUT")
                                
                                # Проверяем была ли это отложенная команда
                                if client_id in scheduled_tracking and scheduled_tracking[client_id]:
                                    cmd_index = scheduled_tracking[client_id].pop(0)
                                    ScheduledCommandManager.mark_completed(cmd_index, client_id, combined_output)
                                    Logger.log("SCHEDULED", f"Команда [{cmd_index}] отмечена как выполненная", client_id)
                                
                                # Завершаем отслеживание ПОСЛЕ получения всех результатов
                                CommandMonitor.unregister(client_id)
                                
                                # Очищаем буфер ТОЛЬКО когда все результаты получены
                                buffer["type"] = None
                                buffer["lines"] = []
                            else:
                                # Ещё не все - НЕ unregister и НЕ очищаем буфер
                                Logger.log("DEBUG", f"SIMPL: Ожидаем ещё результаты...", client_id)
                                # Очищаем буфер для следующей команды, но не type
                                buffer["lines"] = []

                        continue

                except Exception as e:
                    Logger.log("ERROR", f"Ошибка OUTPUT: {e}", client_id)
                    consecutive_errors += 1
                    continue

                # ========== FILETRU chunks - обработка с защитой ==========
                try:
                    if msg.startswith("FILETRU:START:"):
                        buffer = output_buffers[client_id]
                        buffer["type"] = "FILETRU"
                        buffer["lines"] = []
                        buffer["chunks"] = 0
                        try:
                            buffer["total"] = int(msg.split(":")[-1])
                        except (ValueError, IndexError):
                            buffer["total"] = 0

                        # Не вызываем unregister здесь - команда ещё не завершена
                        continue

                    if msg.startswith("FILETRU:CHUNK:"):
                        chunk_data = msg[14:]
                        chunk_restored = chunk_data.replace('<<<NL>>>', '\n')
                        output_buffers[client_id]["lines"].append(chunk_restored)
                        output_buffers[client_id]["chunks"] += 1
                        continue

                    if msg == "FILETRU:END":
                        buffer = output_buffers[client_id]
                        full_output = '\n'.join(buffer["lines"])

                        last_outputs[client_id] = {
                            "content": full_output,
                            "type": "FILETRU",
                            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
                        }

                        print(f"\n{'=' * 80}\n[FILETRU от {client_id}]\n{'=' * 80}")
                        print(full_output)
                        print(f"{'=' * 80}\n")

                        if client_id in active_commands:
                            cmd_info = active_commands[client_id]

                            # НАКАПЛИВАЕМ
                            cmd_info["accumulated_output"].append(full_output)
                            cmd_info["received_commands"] += 1

                            Logger.log(
                                "DEBUG",
                                f"FILETRU: {cmd_info['received_commands']}/{cmd_info['total_commands']}",
                                client_id
                            )

                            # Проверяем получили ли всё
                            if cmd_info["received_commands"] >= cmd_info["total_commands"]:
                                combined_output = "\n\n".join(cmd_info["accumulated_output"])

                                CommandMonitor.save_command_output(
                                    client_id,
                                    cmd_info["command"],
                                    combined_output,
                                    "FILETRU"
                                )

                                CommandMonitor.unregister(client_id)

                        buffer["type"] = None
                        buffer["lines"] = []
                        continue

                except Exception as e:
                    Logger.log("ERROR", f"Ошибка FILETRU: {e}", client_id)
                    consecutive_errors += 1
                    continue

                # Неизвестное сообщение - логируем, но не падаем
                Logger.log("WARNING", f"Неизвестное сообщение: {msg[:50]}...",
                           client_id, show_console=False)

            except asyncio.TimeoutError:
                Logger.log("WARNING", "Таймаут чтения (нет активности 5 мин)",
                           client_id, show_console=False)
                # Не отключаем сразу, клиент может быть просто неактивен
                continue

            except ConnectionError as e:
                Logger.log("INFO", f"Проблема соединения: {e}", client_id, show_console=False)
                break

            except Exception as e:
                Logger.log("ERROR", f"Неожиданная ошибка в цикле: {e}", client_id)
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    Logger.log("ERROR",
                               f"Слишком много ошибок ({consecutive_errors}), отключение клиента",
                               client_id)
                    break
                continue

    except Exception as e:
        Logger.log("ERROR", f"Критическая ошибка обработки клиента: {e}", client_id)
        import traceback
        Logger.crash(e, traceback.format_exc())

    finally:
        # Очистка ресурсов ВСЕГДА выполняется
        if client_id:
            if client_id in clients:
                del clients[client_id]
            if client_id in active_commands:
                CommandMonitor.unregister(client_id)
            UserManager.logout(client_id)
            Logger.log("DISCONNECT", "отключился", client_id, show_console=True)
            StateManager.save()

        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# СЕРВЕРНЫЙ ИНТЕРФЕЙС
# ═══════════════════════════════════════════════════════════════════════════

async def server_input(server):
    """Обработка команд сервера"""
    loop = asyncio.get_event_loop()

    while True:
        msg = await loop.run_in_executor(None, input, "Server> ")
        msg = msg.strip()

        if not msg:
            continue

        # EXIT
        if msg == "EXIT":
            print("\n Остановка сервера...")
            cleanup_on_shutdown()
            server.close()
            await server.wait_closed()
            break

        # STATUS
        if msg == "status":
            if not active_commands:
                print(" Нет активных команд")
            else:
                print(f"\n{'=' * 80}\nАКТИВНЫЕ КОМАНДЫ\n{'=' * 80}")
                for client_id, cmd_info in active_commands.items():
                    elapsed = time.time() - cmd_info["start_time"]
                    print(f"{client_id}: {cmd_info['type']} ({elapsed:.1f}s) - {cmd_info['command']}")
                print(f"{'=' * 80}\n")
            continue
        if msg == "help":
            print_command()
            continue

        # CANCEL
        if msg.startswith("cancel "):
            target_id = msg.split()[1]
            if target_id in active_commands:
                if target_id in clients:
                    clients[target_id].write(b"CMD:CANCEL_MANUAL\n")
                    await clients[target_id].drain()
                CommandMonitor.unregister(target_id)
                print(f" Команда отменена для {target_id}")
            else:
                print(f" У {target_id} нет активных команд")
            continue

        # KICK - Отключить клиента
        if msg.startswith("kick "):
            parts = msg.split(" ", 1)
            if len(parts) < 2:
                print("Формат: kick <client|all>")
                continue

            target = parts[1].strip()

            if target == "all":
                # Кикаем всех клиентов
                kicked_count = 0
                for client_id in list(clients.keys()):
                    try:
                        await BanManager.kick_user(client_id, "Отключены администратором")
                        kicked_count += 1
                    except Exception as e:
                        Logger.log("ERROR", f"Ошибка кика {client_id}: {e}")
                print(f" Отключено клиентов: {kicked_count}")
            else:
                # Проверяем alias
                real_username = UserManager.get_by_alias(target)
                if real_username:
                    target = real_username

                if target in clients:
                    await BanManager.kick_user(target, "Отключен администратором")
                    print(f" Клиент {target} отключен")
                else:
                    print(f" Клиент {target} не подключен")
            continue


        # EXPORT
        if msg.startswith("export "):
            parts = msg.split(" ", 3)
            if len(parts) < 3:
                print("Формат: export client_id path_user [dest_dir]")
                continue

            target_id = parts[1]
            source_path = parts[2]
            dest_dir = parts[3] if len(parts) > 3 else "received"

            real_username = UserManager.get_by_alias(target_id)
            if real_username:
                target_id = real_username

            if target_id in clients:
                source_path = ScheduledCommandManager.replace_user_placeholder(source_path, target_id)
                CommandMonitor.register(target_id, f"export {source_path}", "EXPORT",1)
                clients[target_id].write(f"EXPORT;{source_path};{dest_dir}\n".encode('utf-8'))
                await clients[target_id].drain()
                print(f" Запрос экспорта отправлен {target_id}")
            else:
                print(f" Клиент {target_id} не подключен")
            continue

        # IMPORT
        if msg.startswith("import "):
            parts = msg.split(" ", 3)
            if len(parts) < 3:
                print("Формат: import <client|all> path_serv [path_user]")
                continue

            target = parts[1].strip()
            source_path = parts[2]
            dest_dir = parts[3] if len(parts) > 3 else "received"

            if target == "all":
                # Отправляем файлы всем клиентам
                sent_count = 0
                for client_id in list(clients.keys()):
                    try:
                        CommandMonitor.register(client_id, f"import {source_path}", "IMPORT",1)
                        await FileTransfer.import_to_client(client_id, source_path, dest_dir)
                        CommandMonitor.unregister(client_id)
                        sent_count += 1
                    except Exception as e:
                        Logger.log("ERROR", f"Ошибка импорта для {client_id}: {e}")
                        CommandMonitor.unregister(client_id)
                print(f" Файлы отправлены {sent_count} клиентам")
            else:
                # Отправляем одному клиенту
                real_username = UserManager.get_by_alias(target)
                if real_username:
                    target = real_username

                if target in clients:
                    dest_dir=ScheduledCommandManager.replace_user_placeholder(dest_dir, target)
                    CommandMonitor.register(target, f"import {source_path}", "IMPORT",1)
                    await FileTransfer.import_to_client(target, source_path, dest_dir)
                    CommandMonitor.unregister(target)
                else:
                    print(f" Клиент {target} не подключен")
            continue

        # LIST
        if msg == "list":
            users_data = UserManager.load_users()
            if not users_data["users"]:
                print(" Нет зарегистрированных пользователей")
            else:
                print(f"\n{'=' * 80}")
                print(f"{'№':<4} {'Username':<20} {'Alias':<20} {'Status':<8} {'Time':<20}")
                print(f"{'=' * 80}")

                for i, (username, info) in enumerate(users_data["users"].items(), 1):
                    status = info["status"]
                    time_field = info["last_login"] if status == "ON" else info["last_logout"]
                    print(f"{i:<4} {username:<20} {info['alias']:<20} {status:<8} {time_field:<20}")

                online_count = sum(1 for u in users_data['users'].values() if u['status'] == 'ON')
                print(f"{'=' * 80}")
                print(f"Всего: {len(users_data['users'])} | Онлайн: {online_count}\n")
            continue


        # SAVE
        if msg.startswith("save "):
            parts = msg.split(" ", 2)
            if len(parts) < 3:
                print("Формат: save client_id filename")
                continue

            target_id = parts[1]
            filename = parts[2]

            real_username = UserManager.get_by_alias(target_id)
            if real_username:
                target_id = real_username

            if target_id not in clients:
                print(f" Клиент {target_id} не подключен")
                continue

            if target_id not in last_outputs or not last_outputs[target_id]["content"]:
                print(f" Нет данных для сохранения от {target_id}")
                continue

            time_now = time.strftime("%Y-%m-%d %H:%M:%S")
            save_path = os.path.join(Config.DIR_SAVE, f"{filename}.txt")

            try:
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(f"Пользователь: {target_id}\n"
                            f"Время: {time_now}\n"
                            f"Тип: {last_outputs[target_id]['type']}\n"
                            f"{'=' * 50}\n"
                            f"{last_outputs[target_id]['content']}\n")

                print(f"\n Результат сохранён в {save_path}")
                Logger.log("SAVE", f"Сохранено в {filename}.txt", target_id)
            except Exception as e:
                print(f" Ошибка сохранения: {e}")
            continue

        # SIMPL
        if msg.startswith("simpl "):
            target = msg.split()[1].strip()

            if target == "all":
                # Отправляем команды всем клиентам
                try:
                    with open(Config.FILE_CODE, "r", encoding="utf-8") as f:
                        commands = [line.strip() for line in f if line.strip()]

                    if not commands:
                        print(" Файл команд пуст")
                        continue

                    for client_id in list(clients.keys()):
                        try:
                            CommandMonitor.register(client_id, f"simpl ({len(commands)} команд)", "FILETRU", len(commands))
                            for cmd in commands:
                                cmd=ScheduledCommandManager.replace_user_placeholder(cmd, client_id)
                                clients[client_id].write(f"FILETRU:{cmd}\n".encode('utf-8'))
                                await clients[client_id].drain()
                                await asyncio.sleep(0.2)
                            print(f" Отправлено {len(commands)} команд → {client_id}")
                        except Exception as e:
                            Logger.log("ERROR", f"Ошибка simpl для {client_id}: {e}")
                            CommandMonitor.unregister(client_id)
                except FileNotFoundError:
                    print(f" Файл {Config.FILE_CODE} не найден")
                continue

            # Одному клиенту
            real_username = UserManager.get_by_alias(target)
            if real_username:
                target = real_username

            if target not in clients:
                print(f" Клиент {target} не подключен")
                continue

            try:
                with open(Config.FILE_CODE, "r", encoding="utf-8") as f:
                    commands = [line.strip() for line in f if line.strip()]

                if not commands:
                    print(" Файл команд пуст")
                    continue

                CommandMonitor.register(target, f"simpl ({len(commands)} команд)", "FILETRU", len(commands))

                for cmd in commands:
                    cmd=ScheduledCommandManager.replace_user_placeholder(cmd, target)
                    clients[target].write(f"FILETRU:{cmd}\n".encode('utf-8'))
                    await clients[target].drain()
                    await asyncio.sleep(0.2)

                print(f" Отправлено {len(commands)} команд → {target}")
            except FileNotFoundError:
                print(f" Файл {Config.FILE_CODE} не найден")
                CommandMonitor.unregister(target)
            continue

        # CMD
        if msg.startswith("CMD "):
            parts = msg.split(" ", 2)
            if len(parts) < 3:
                print("Формат: CMD <client|all> команда")
                continue

            target_id, command = parts[1], parts[2]
            
            if target_id == "all":
                CommandMonitor.register("all", command, "CMD",1)
                for user in clients:
                    command = ScheduledCommandManager.replace_user_placeholder(command, user)
                    clients[user].write(f"CMD:{command}\n".encode('utf-8'))
                    await clients[user].drain()
                print(f" Команда отправлена всем клиентам")
                continue

            real_username = UserManager.get_by_alias(target_id)
            if real_username:
                target_id = real_username

            if target_id in clients:
                command=ScheduledCommandManager.replace_user_placeholder(command, target_id)
                CommandMonitor.register(target_id, command, "CMD",1)
                clients[target_id].write(f"CMD:{command}\n".encode('utf-8'))
                await clients[target_id].drain()
                print(f" Команда отправлена → {target_id}")
            else:
                print(f" Клиент {target_id} не подключен")
            continue

        # ═══════════════════════════════════════════════════════════════
        # КОМАНДЫ УПРАВЛЕНИЯ ГРУППАМИ
        # ═══════════════════════════════════════════════════════════════

        # GROUP_NEW
        if msg.startswith("group_new "):
            group_name = msg.split()[1]
            groups = GroupManager.load_groups()

            if group_name in groups:
                print(f" Группа '{group_name}' уже существует")
                continue

            print(f"\nСоздание группы '{group_name}'")
            print(" Введите имена пользователей (по одному). Для завершения: EXIT")

            members = []
            while True:
                member = input("  > ").strip()
                if member == "EXIT":
                    break
                if member:
                    members.append(member)
                    print(f"  + {member}")

            if members:
                groups[group_name] = members
                if GroupManager.save_groups(groups):
                    print(f" Группа '{group_name}' создана с {len(members)} участниками")
                else:
                    print(f" Ошибка создания группы")
            else:
                print(" Группа не создана (нет участников)")
            continue

        # GROUP_LIST
        if msg == "group_list":
            groups = GroupManager.load_groups()
            if not groups:
                print(" Нет групп")
            else:
                print(f"\n{'='*60}")
                print("ГРУППЫ:")
                print(f"{'='*60}")
                for group_name, members in groups.items():
                    print(f"📁 {group_name} ({len(members)} участников)")
                    for member in members:
                        print(f"   - {member}")
                    print()
                print(f"{'='*60}\n")
            continue

        # GROUP_DEL
        if msg.startswith("group_del "):
            group_name = msg.split()[1]
            groups = GroupManager.load_groups()

            if group_name not in groups:
                print(f" Группа '{group_name}' не найдена")
                continue

            del groups[group_name]
            if GroupManager.save_groups(groups):
                print(f" Группа '{group_name}' удалена")
            continue

        # ═══════════════════════════════════════════════════════════════
        # КОМАНДЫ УПРАВЛЕНИЯ ОТЛОЖЕННЫМИ КОМАНДАМИ
        # ═══════════════════════════════════════════════════════════════

        # CHART_NEW
        if msg == "chart_new":
            print("\n" + "="*60)
            print("СОЗДАНИЕ ОТЛОЖЕННОЙ КОМАНДЫ")
            print("="*60)

            target = input("Цель (all/username/group:name): ").strip()
            if not target:
                print(" Цель не может быть пустой")
                continue

            # Проверка существования группы
            if target.startswith("group:"):
                group_name = target[6:]
                if not GroupManager.get_group_members(group_name):
                    print(f" Группа '{group_name}' не существует")
                    continue

            cmd_type = input("Тип (CMD/SIMPL/IMPORT/EXPORT): ").strip().upper()
            if cmd_type not in ["CMD", "SIMPL", "IMPORT", "EXPORT"]:
                print(" Неверный тип команды")
                continue

            command_data = {
                "target": target,
                "command_type": cmd_type,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "expected_users": ScheduledCommandManager.get_expected_users(target),
                "completed_users": []
            }

            if cmd_type == "CMD":
                command = input("Команда: ").strip()
                if command:
                    command_data["command"] = command
                else:
                    print(" Команда не может быть пустой")
                    continue

            elif cmd_type == "SIMPL":
                # SIMPL не требует дополнительных параметров - использует code.txt
                print(" Будут выполнены команды из code.txt")
                # Проверяем что файл существует
                if not os.path.exists(Config.FILE_CODE):
                    print(f" Файл {Config.FILE_CODE} не найден")
                    continue

            elif cmd_type == "IMPORT":
                source = input("Путь на сервере: ").strip()
                dest = input("Путь на клиенте: ").strip()
                if source and dest:
                    command_data["source_path"] = source
                    command_data["dest_path"] = dest
                else:
                    print(" Оба пути обязательны")
                    continue

            elif cmd_type == "EXPORT":
                source = input("Путь на клиенте: ").strip()
                dest = input("Путь на сервере [received]: ").strip() or "received"
                if source:
                    command_data["source_path"] = source
                    command_data["dest_path"] = dest
                else:
                    print(" Путь на клиенте обязателен")
                    continue

            data = ScheduledCommandManager.load_data()
            data["commands"].append(command_data)
            if ScheduledCommandManager.save_data(data):
                print(f" Команда добавлена для '{target}'")
                print(f" Ожидает выполнения: {len(command_data['expected_users'])} пользователей")
            else:
                print(" Ошибка добавления команды")
            continue

        # CHART_LIST
        if msg == "chart_list":
            data = ScheduledCommandManager.load_data()
            commands = data["commands"]
            if not commands:
                print(" Нет активных отложенных команд")
            else:
                print(f"\n{'='*60}")
                print("АКТИВНЫЕ ОТЛОЖЕННЫЕ КОМАНДЫ:")
                print(f"{'='*60}")
                for i, cmd in enumerate(commands):
                    target = cmd["target"]
                    cmd_type = cmd["command_type"]
                    expected = len(cmd.get("expected_users", []))
                    completed = len(cmd.get("completed_users", []))

                    if cmd_type == "CMD":
                        print(f"[{i}] {target} → CMD: {cmd['command']}")
                    elif cmd_type == "SIMPL":
                        print(f"[{i}] {target} → SIMPL (команды из code.txt)")
                    elif cmd_type in ["IMPORT", "EXPORT"]:
                        print(f"[{i}] {target} → {cmd_type}: {cmd['source_path']} → {cmd['dest_path']}")
                    
                    if completed > 0:
                        print(f"    ✓ Выполнено: {completed}")
                    if expected > 0:
                        print(f"    ⏳ Ожидает: {expected}")
                print(f"{'='*60}\n")
            continue

        # CHART_COMD - Показать выполненные команды
        if msg == "chart_comd":
            data = ScheduledCommandManager.load_data()
            completed = data["completed"]
            active = data["commands"]

            if not completed and not any(cmd.get("completed_users") for cmd in active):
                print(" Нет выполненных команд")
                continue

            print(f"\n{'='*70}")
            print(f"ВЫПОЛНЕННЫЕ КОМАНДЫ: {len(completed)}")
            print(f"{'='*70}")

            # Полностью завершённые команды
            for i, cmd in enumerate(completed):
                target = cmd["target"]
                cmd_type = cmd["command_type"]
                completed_at = cmd.get("completed_at", "неизвестно")
                
                if cmd_type == "CMD":
                    print(f"\n[{i}] {target} → CMD: {cmd['command']} ({completed_at}) ✓ ЗАВЕРШЕНО")
                elif cmd_type == "SIMPL":
                    print(f"\n[{i}] {target} → SIMPL (code.txt) ({completed_at}) ✓ ЗАВЕРШЕНО")
                elif cmd_type in ["IMPORT", "EXPORT"]:
                    print(f"\n[{i}] {target} → {cmd_type}: {cmd['source_path']} ({completed_at}) ✓ ЗАВЕРШЕНО")
                
                # Показываем кто выполнил
                completed_users = cmd.get("completed_users", [])
                for user in completed_users:
                    print(f"    ├─ {user} ✓")

            # Частично выполненные команды
            print(f"\n{'='*70}")
            print("В ПРОЦЕССЕ ВЫПОЛНЕНИЯ:")
            print(f"{'='*70}")
            
            for i, cmd in enumerate(active):
                completed_users = cmd.get("completed_users", [])
                expected_users = cmd.get("expected_users", [])
                
                if completed_users:  # Показываем только если есть хоть кто-то кто выполнил
                    target = cmd["target"]
                    cmd_type = cmd["command_type"]
                    created_at = cmd.get("created_at", "неизвестно")
                    
                    if cmd_type == "CMD":
                        print(f"\n[{i}] {target} → CMD: {cmd['command']} ({created_at})")
                    elif cmd_type == "SIMPL":
                        print(f"\n[{i}] {target} → SIMPL (code.txt) ({created_at})")
                    elif cmd_type in ["IMPORT", "EXPORT"]:
                        print(f"\n[{i}] {target} → {cmd_type}: {cmd['source_path']} ({created_at})")
                    
                    # Показываем кто выполнил
                    for user in completed_users:
                        print(f"    ├─ {user} ✓")
                    
                    # Показываем сколько ещё ожидает
                    if expected_users:
                        print(f"    └─ Ожидает: {len(expected_users)} пользователей")

            print(f"{'='*70}\n")
            continue

        # CHART_DEL
        if msg.startswith("chart_del "):
            try:
                index = int(msg.split()[1])
                data = ScheduledCommandManager.load_data()
                commands = data["commands"]

                if 0 <= index < len(commands):
                    deleted = commands.pop(index)
                    if ScheduledCommandManager.save_data(data):
                        print(f" Команда [{index}] удалена")
                    else:
                        print(" Ошибка удаления")
                else:
                    print(f" Неверный индекс: {index}")
            except (ValueError, IndexError):
                print(" Формат: chart_del <index>")
            continue

        else:
            # Broadcast
            for w in clients.values():
                w.write(f"Server: {msg}\n".encode('utf-8'))
                await w.drain()


# ═══════════════════════════════════════════════════════════════════════════
# SHUTDOWN И PERIODIC TASKS
# ═══════════════════════════════════════════════════════════════════════════

def cleanup_on_shutdown():
    """Очистка при остановке"""
    Logger.log("INFO", "Graceful shutdown...")

    users_data = UserManager.load_users()
    time_now = time.strftime("%Y-%m-%d %H:%M:%S")

    for username in list(clients.keys()):
        if username in users_data["users"]:
            users_data["users"][username]["status"] = "OFF"
            users_data["users"][username]["last_logout"] = time_now
            UserManager._log_session(username, users_data["users"][username]["alias"], "logout")

    UserManager.save_users(users_data)
    StateManager.save()
    Logger.log("INFO", "Сервер остановлен")


def setup_signal_handlers():
    """Обработчики сигналов"""

    def signal_handler(signum, frame):
        Logger.log("WARNING", f"Получен сигнал {signum}")
        cleanup_on_shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)


async def periodic_state_save():
    """Периодическое сохранение состояния"""
    while True:
        await asyncio.sleep(Config.STATE_SAVE_INTERVAL)
        StateManager.save()


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def main():
    ensure_dirs()
    setup_signal_handlers()

    Logger.log("INFO", "Запуск сервера...")
    prev_state = StateManager.load()

    if prev_state:
        Logger.log("INFO", f"Найдено состояние от {prev_state['datetime']}")
        Logger.log("INFO",
                   f"Было клиентов: {len(prev_state['connected_clients'])}, "
                   f"команд: {len(prev_state['active_commands'])}")

    try:
        server = await asyncio.start_server(handle_client, Config.HOST, Config.PORT)
        Logger.log("INFO", f"Сервер запущен на {Config.HOST}:{Config.PORT}")
        Logger.log("INFO",
                   f"Таймаут команд: {Config.COMMAND_TIMEOUT}s "
                   f"(предупреждение: {Config.WARNING_TIMEOUT}s)")
        print("dev by ENOT and claude")
        print_command()




        async with server:
            await asyncio.gather(
                server.serve_forever(),
                server_input(server),
                CommandMonitor.monitor_loop(),
                periodic_state_save()
            )

    except Exception as e:
        Logger.log("CRITICAL", f"Критическая ошибка: {e}")
        import traceback
        Logger.crash(e, traceback.format_exc())
        cleanup_on_shutdown()
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        Logger.log("INFO", "Получен Ctrl+C")
        cleanup_on_shutdown()
    except Exception as e:
        Logger.log("CRITICAL", f"Необработанная ошибка: {e}")
        import traceback

        Logger.crash(e, traceback.format_exc())
        cleanup_on_shutdown()
