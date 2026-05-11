"""
config.py — конфигурация, перечисления, парсеры, константы GUI.

Назначение:
    Единственный файл, который не импортирует ничего из проекта.
    Все остальные модули импортируют отсюда — это «корень» зависимостей.

Что содержит:
    Config          — пути к директориям и файлам, сетевые параметры,
                      таймауты, размер чанка, смещение часового пояса.
    ServerCmd       — StrEnum всех команд администратора (cmd, simpl,
                      export, import, save, kick, группы, отложенные и др.).
    ClientMsg       — StrEnum протокольных сообщений от клиента
                      (output:*, filetru:*, export:start, import:*).
    ServerCmdParser — парсит строку консоли/GUI → (ServerCmd, args: list).
    ClientMsgParser — определяет тип входящего TCP-сообщения → (ClientMsg, payload).
    validate_username() — нормализует и валидирует имя пользователя.
    CMD_HINTS       — словарь подсказок синтаксиса для GUI (UsersPanel).
    FLAGS           — список команд, требующих диалога (CMD/SIMPL/EXPORT/IMPORT).
    COLORS          — цветовая схема тёмной темы для всех tkinter-виджетов.
    FONT_*          — константы шрифтов (моноширинный, UI, мелкий).

Не импортирует: ничего из проекта.
Импортируется из: managers.py, handlers.py, server.py, gui.py.
"""

import re
from enum import StrEnum
from pathlib import Path
from typing import Dict


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
    FILE_TEMPLATE  = DIR_JSON / "template_comd.json"

    HOST = "0.0.0.0"
    PORT = 9000

    CHUNK_SIZE          = 65536 # Размер чанка при передаче файлов 65536 = 64 КБ
    COMMAND_TIMEOUT     = 120   # Максимальное время выполнения команд клиентом, после, CANCEL
    WARNING_TIMEOUT     = 90    # 30 секундное предупреждение перед CANCEL
    STATE_SAVE_INTERVAL = 30    # Интервал автосохранения состояния сервера (выгрузка данных в Config.FILE_STATE)
    READ_TIMEOUT        = 300   # Время ожидание данных от клиента, после continue  для того что бы asyncio.wait_for не висел бесконечно при зависшем клиенте.
    TIMEZONE_OFFSET     = +1    # Смещение времени от UTC


# ═══════════════════════════════════════════════════════════════════════════
# ENUM — СЕРВЕРНЫЕ КОМАНДЫ И КЛИЕНТСКИЕ ПРОТОКОЛЫ
# ═══════════════════════════════════════════════════════════════════════════

class ServerCmd(StrEnum):
    """Команды, вводимые администратором"""
    BATCH         = "batch"
    CMD           = "cmd"
    SIMPL         = "simpl"
    EXPORT        = "export"
    IMPORT        = "import"
    SAVE          = "save"
    LIST          = "list"
    RENAME        = "rename"
    STATUS        = "status"
    CANCEL        = "cancel"
    KICK          = "kick"
    HELP          = "help"
    EXIT          = "exit"
    GROUP_NEW     = "group_new"
    GROUP_LIST    = "group_list"
    GROUP_DEL     = "group_del"
    GROUP_ADD     = "group_add"
    GROUP_RM      = "group_rm"
    CHART_NEW     = "chart_new"
    CHART_LIST    = "chart_list"
    CHART_DEL     = "chart_del"
    CHART_COMD    = "chart_comd"
    GROUP_EDIT    = "chart_edit" # non-use
    TEMPLATE_NEW  = "template_new"
    TEMPLATE_ADD  = "template_add"
    TEMPLATE_RM   = "template_rm"
    TEMPLATE_DEL  = "template_del"
    TEMPLATE_LIST = "template_list"

class ClientMsg(StrEnum):
    """Протокольные сообщения от клиента"""
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
# ВАЛИДАЦИЯ ИМЕНИ
# ═══════════════════════════════════════════════════════════════════════════

def validate_username(name: str) -> str:
    """Проверяет и нормализует имя пользователя."""
    name = name.strip().lower()
    if not name:
        raise ValueError("Имя не может быть пустым")
    if len(name) > 32:
        raise ValueError("Имя длиннее 32 символов")
    if not re.match(r'^[\w\-\.]+$', name):
        raise ValueError(f"Недопустимые символы: {name}")
    return name

# ═══════════════════════════════════════════════════════════════════════════
# Вспомогательные классы\словари для GUI
# ═══════════════════════════════════════════════════════════════════════════

FONT_MONO  = ("Consolas", 11)
FONT_MONO_S= ("Consolas", 10)
FONT_UI    = ("Segoe UI", 10)
FONT_UI_B  = ("Segoe UI", 10, "bold")
FONT_SMALL = ("Segoe UI", 9)

# ── подсказка команд ───────────────────────────────────────────────────────
s=ServerCmd
CMD_HINTS = {
    s.CMD:           "cmd <user> <comd1, comd2 ...>",
    s.BATCH:         "batch <user> <commands>",
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
    s.EXIT:          "exit",
    s.HELP:          "help",
}


FLAGS = [ServerCmd.CMD, ServerCmd.BATCH, ServerCmd.SIMPL, ServerCmd.EXPORT, ServerCmd.IMPORT]

# ── палитра ────────────────────────────────────────────────────────────────
COLORS = {
    "bg":        "#1E1E2E",
    "panel":     "#27273A",
    "border":    "#3A3A52",
    "accent":    "#7C6AF7",
    "accent2":   "#1D9E75",
    "warn":      "#EF9F27",
    "error":     "#D85A30",
    "text":      "#CDD6F4",
    "text_dim":  "#6C7086",
    "input_bg":  "#313244",
    "online":    "#1D9E75",
    "offline":   "#585B70",
    "sched": "#7C3AED",
    "group": "#0E7490",
    ServerCmd.CMD:    "#185FA5",
    ServerCmd.SIMPL:  "#3B6D11",
    ServerCmd.IMPORT: "#854F0B",
    ServerCmd.EXPORT: "#534AB7",
    ServerCmd.GROUP_NEW: "#1D9E75",
    ServerCmd.GROUP_EDIT: "#7C6AF7",
    ServerCmd.GROUP_DEL: "#D85A30",
    ServerCmd.BATCH: "#185FA5",
    ServerCmd.TEMPLATE_NEW: "#1D9E75",
    ServerCmd.TEMPLATE_ADD: "#7C6AF7",
    ServerCmd.TEMPLATE_DEL: "#D85A30"
}



