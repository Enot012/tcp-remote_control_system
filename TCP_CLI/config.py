"""
Конфигурация, Enum'ы и парсеры.
"""

import os
import re
import time
import json
from enum import StrEnum
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional


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
    FILE_GROUPS    = DIR_JSON / "groups.json"
    FILE_SCHEDULED = DIR_JSON / "scheduled_commands.json"
    FILE_TEMPLATE  = DIR_JSON / "templates.json"
    FILE_KEY       = DIR_JSON / "authority_key"


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
    TEMPLATE_NEW  = "template_new"
    TEMPLATE_ADD  = "template_add"
    TEMPLATE_RM   = "template_rm"
    TEMPLATE_DEL  = "template_del"
    TEMPLATE_LIST = "template_list"


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
              Config.DIR_SCHEDULED_RESULTS, Config.DIR_FOR_SEND]:
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
        ("template_new  <n>",                           "Добавить шаблон команд"),
        ("template_list",                               "Список шаблонов"),
        ("template_del  <n>",                           "Удалить шаблон"),
        ("template_add  <n> <command>",                 "Добавить команду в шаблон"),
        ("template_rm   <n> <indexes>",                 "Удалить команду из шаблона"),
        ("", ""),
        ("list",                                        "Список пользователей"),
        ("rename <user> <alias>",                       "Переименовать пользователя"),
        ("status",                                      "Активные команды"),
        ("cancel <client>",                             "Отменить команду"),
        ("kick <client|all>",                           "Отключить клиента/всех"),
        ("help",                                        "Эта справка"),
        ("EXIT",                                        "Остановить сервер"),
    ]
    for cmd, desc in rows:
        print(f"  {cmd:<42} - {desc}" if cmd else "")
    print("=" * 80 + "\n")

s=ServerCmd
CMD_HINTS = {
    s.CMD:           "cmd <user> <comd1, comd2 ...>",
    s.SIMPL:         "simpl <user> [template_name]",
    s.EXPORT:        "export <user> <path_cli> [path_serv]",
    s.IMPORT:        "import <user> <path_serv> [path_cli]",
    s.SAVE:          "save <user> <filename>",
    s.LIST:          "list",
    s.RENAME:        "rename <user> <alias>",
    s.STATUS:        "status",
    s.CANCEL:        "cancel <user>",
    s.KICK:          "kick <user|all>",
    s.GROUP_NEW:     "group_new <name> [user1 user2 ...]",
    s.GROUP_LIST:    "group_list",
    s.GROUP_DEL:     "group_del <name>",
    s.GROUP_ADD:     "group_add <group> <user1> [user2 ...]",
    s.GROUP_RM:      "group_rm <group> <user1> [user2 ...]",
    s.CHART_NEW:     "chart_new <type> <user> [args...]",
    s.CHART_LIST:    "chart_list",
    s.CHART_DEL:     "chart_del <index>",
    s.CHART_COMD:    "chart_comd",
    s.TEMPLATE_NEW:  "template_new <name> <commands>",
    s.TEMPLATE_ADD:  "template_add <name> <commands>",
    s.TEMPLATE_RM:   "template_rm <name> <indexes>",
    s.TEMPLATE_DEL:  "template_del <index>",
    s.TEMPLATE_LIST: "template_list ",

}
