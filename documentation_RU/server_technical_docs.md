# 📚 Документация сервера - Техническая структура

## 🏗️ Архитектура

### Глобальные структуры данных

```python
clients: Dict[str, asyncio.StreamWriter]
```
**Назначение:** Хранение активных подключений клиентов  
**Ключ:** `client_id` (имя клиента)  
**Значение:** `StreamWriter` для отправки данных  
**Использование:** Отправка команд, проверка онлайн-статуса

```python
output_buffers: Dict[str, Dict[str, Any]]
```
**Назначение:** Временное накопление результатов по частям  
**Структура:**
```python
{
    "client_id": {
        "type": "OUTPUT" | "FILETRU",  # Тип данных
        "lines": [...],                # Накопленные строки
        "chunks": int,                 # Кол-во полученных чанков
        "total": int                   # Ожидаемое кол-во
    }
}
```
**Использование:** Сборка результатов из `OUTPUT:CHUNK:` сообщений

```python
last_outputs: Dict[str, Dict[str, Any]]
```
**Назначение:** Последний полный результат от каждого клиента  
**Структура:**
```python
{
    "client_id": {
        "content": str,      # Полный вывод
        "type": str,         # Тип команды
        "timestamp": str     # Время получения
    }
}
```
**Использование:** Команда `save` для сохранения результатов

```python
active_commands: Dict[str, Dict[str, Any]]
```
**Назначение:** Отслеживание выполняемых команд  
**Структура:**
```python
{
    "client_id": {
        "start_time": float,           # Время начала
        "command": str,                # Текст команды
        "type": str,                   # CMD/FILETRU/IMPORT/EXPORT
        "total_commands": int,         # Для SIMPL: кол-во команд
        "received_commands": int,      # Для SIMPL: получено
        "accumulated_output": [str]    # Для SIMPL: накопленные результаты
    }
}
```
**Использование:** Накопление результатов SIMPL, таймауты, сохранение

```python
scheduled_tracking: Dict[str, list]
```
**Назначение:** Отслеживание выполняемых отложенных команд  
**Структура:**
```python
{
    "client_id": [0, 2, 5]  # Индексы команд из scheduled_commands.json
}
```
**Использование:** Отметка выполнения отложенных команд

---

## 📦 Классы

### `Config`
**Назначение:** Централизованная конфигурация сервера

**Константы:**
```python
BASE_DIR = ".../HA_12"
DIR_SAVE, DIR_TRASH, DIR_HISTORY, DIR_FILES, DIR_LOGS, DIR_JSON
FILE_CODE, FILE_USERS, FILE_STATE, FILE_CRASH_LOG
FILE_GROUPS, FILE_SCHEDULED
HOST = "0.0.0.0"
PORT = 9000
CHUNK_SIZE = 65536
COMMAND_TIMEOUT = 120      # Предупреждение о долгой команде
WARNING_TIMEOUT = 90       # Первое предупреждение
READ_TIMEOUT = 300         # Таймаут чтения (отключение клиента)
STATE_SAVE_INTERVAL = 30   # Сохранение состояния
TIMEZONE_OFFSET = +1       # Часовой пояс
```

---

### `Logger`
**Назначение:** Централизованная система логирования

**Методы:**
```python
@staticmethod
def log(level: str, message: str, client_id: Optional[str] = None, 
        show_console: bool = True)
```
- Логирует события в файл и консоль
- Уровни: INFO, ERROR, CONNECT, DISCONNECT, CMD_START, KICK, etc.
- Формат: `[2025-02-23 10:30:00] [INFO] [client_id] message`

---

### `UserManager`
**Назначение:** Управление пользователями

**Методы:**

```python
@staticmethod
def load_users() -> Dict
```
Загрузка `users.json` с информацией о пользователях

```python
@staticmethod
def register(client_id: str) -> str
```
Регистрация нового подключения, возврат alias

```python
@staticmethod
def get_by_alias(alias: str) -> Optional[str]
```
Получение `client_id` по alias

```python
@staticmethod
def update_status(client_id: str, status: str)
```
Обновление статуса (ON/OFF) и времени

**Структура users.json:**
```json
{
  "users": {
    "enot": {
      "alias": "SuperUser",
      "status": "ON",
      "first_login": "2025-02-23 10:00:00",
      "last_login": "2025-02-23 10:30:00",
      "last_logout": "2025-02-22 18:00:00"
    }
  }
}
```

---

### `StateManager`
**Назначение:** Сохранение состояния сервера

**Методы:**

```python
@staticmethod
def save()
```
Сохраняет текущее состояние в `server_state.json`:
```json
{
  "online_users": ["enot", "user2"],
  "active_commands_count": 3,
  "last_save": "2025-02-23 10:30:00"
}
```

---

### `BanManager`
**Назначение:** Управление отключением клиентов

**Методы:**

```python
@staticmethod
async def kick_user(username: str, reason: str = "Отключен администратором")
```
- Отправляет `KICK:reason` клиенту
- Закрывает соединение
- Логирует действие

---

### `GroupManager`
**Назначение:** Управление группами пользователей

**Методы:**

```python
@staticmethod
def load_groups() -> Dict[str, list]
```
Загрузка `groups.json`

```python
@staticmethod
def save_groups(groups: Dict[str, list]) -> bool
```
Сохранение групп

```python
@staticmethod
def get_group_members(name: str) -> Optional[list]
```
Получение списка участников группы

**Структура groups.json:**
```json
{
  "admins": ["user1", "user2"],
  "developers": ["user3", "user4", "user5"]
}
```

---

### `ScheduledCommandManager`
**Назначение:** Управление отложенными командами

**Методы:**

```python
@staticmethod
def load_data() -> Dict
```
Загрузка `scheduled_commands.json`

```python
@staticmethod
def save_data(data: Dict) -> bool
```
Сохранение отложенных команд

```python
@staticmethod
def get_expected_users(target: str) -> list
```
Формирование списка пользователей для цели:
- `all` → все из `users.json`
- `group:name` → участники группы
- `username` → один пользователь

```python
@staticmethod
def get_commands_for_user(username: str) -> list
```
Получение невыполненных команд для пользователя

```python
@staticmethod
def mark_completed(cmd_index: int, username: str, output: str)
```
Отметка команды как выполненной:
- Переносит пользователя из `expected_users` → `completed_users`
- Записывает результат в файл
- Если `expected_users` пуст → переносит в `completed`

```python
@staticmethod
def write_output(target: str, username: str, output: str)
```
Запись результата в файл:
- `all` → `ALL.txt`
- `group:name` → `group_name.txt`
- `username` → `username.txt`

```python
@staticmethod
def replace_user_placeholder(text: str, username: str) -> str
```
Замена `{User}` на имя пользователя

**Структура scheduled_commands.json:**
```json
{
  "commands": [
    {
      "target": "all",
      "command_type": "CMD",
      "command": "whoami",
      "created_at": "2025-02-23 10:00:00",
      "expected_users": ["user1", "user2"],
      "completed_users": []
    }
  ],
  "completed": [
    {
      "target": "group:admins",
      "command_type": "SIMPL",
      "created_at": "2025-02-22 18:00:00",
      "completed_at": "2025-02-23 09:00:00",
      "expected_users": [],
      "completed_users": ["admin1", "admin2"]
    }
  ]
}
```

---

### `CommandMonitor`
**Назначение:** Мониторинг выполнения команд

**Методы:**

```python
@staticmethod
def register(client_id: str, command: str, cmd_type: str, cmd_count: int)
```
Регистрация начала выполнения команды в `active_commands`

```python
@staticmethod
def save_command_output(client_id: str, command: str, output: str, cmd_type: str)
```
Сохранение результата в файл:
- Получает alias пользователя
- Сохраняет в `~/trash/output_command_{alias}.txt`
- Формат: время, команда, тип, вывод

```python
@staticmethod
def unregister(client_id: str)
```
Удаление команды из `active_commands` после завершения

---

### `FileTransfer`
**Назначение:** Передача файлов между сервером и клиентами

**Методы:**

```python
@staticmethod
async def import_to_client(client_id: str, source_path: str, dest_dir: str)
```
Отправка файлов клиенту:
1. Сканирует путь (файл/директория)
2. Отправляет `IMPORT:START:{count}`
3. Для каждого файла: метаданные + чанки по 64KB
4. Отправляет `IMPORT:END`

```python
@staticmethod
async def send_file(writer: StreamWriter, file_path: Path, rel_path: str)
```
Отправка одного файла:
- Метаданные: имя, размер, относительный путь
- Содержимое по чанкам 64KB

```python
@staticmethod
async def receive_file(reader: StreamReader, save_path: Path, file_size: int) -> bool
```
Получение файла от клиента:
- Создаёт директории
- Читает по чанкам 64KB
- Верификация размера

---

## 🔄 Основные функции

### `async def handle_client(reader, writer)`
**Назначение:** Главный обработчик подключения клиента

**Процесс:**
1. **Подключение:** Получение `client_id`, регистрация
2. **Выполнение отложенных команд:** Автоматическое выполнение при подключении
3. **Основной цикл:** Обработка входящих сообщений
   - `EXPORT:START:` → получение файлов
   - `OUTPUT:START:` → начало результата
   - `OUTPUT:CHUNK:` → накопление результата
   - `OUTPUT:END` → завершение, сохранение
   - `FILETRU:START:` → начало результата SIMPL
4. **Отключение:** Обновление статуса, логирование

**Защита от сбоев:**
- Таймауты чтения (300 сек)
- try-except блоки для изоляции ошибок
- Счётчик последовательных ошибок
- Graceful degradation

---

### `async def server_input(server)`
**Назначение:** Обработка команд администратора

**Поддерживаемые команды:**

#### Выполнение команд
- `CMD <client|all> <команда>` - выполнить команду
- `simpl <client|all>` - выполнить команды из `code.txt`
- `cancel <client>` - отменить выполнение

#### Файлы
- `import <client|all> <путь_на_сервере> [путь_на_клиенте]` - отправить файл
- `export <client> <путь_на_клиенте> [не_обязателен]`- получить файл
- `save <client> <имя>` - сохранить последний результат

#### Управление
- `list` - список пользователей
- `status` - активные команды
- `kick <client|all>` - отключить

#### Отложенные команды
- `chart_new` - создать отложенную команду
- `chart_list` - показать активные
- `chart_comd` - показать выполненные
- `chart_del <index>` - удалить

#### Группы
- `group_new <name>` - создать группу
- `group_list` - показать группы
- `group_del <name>` - удалить группу

---

## 📡 Протокол связи

### Сервер → Клиент

```python
CMD:{команда}\n                    # Выполнить команду
FILETRU:{команда}\n                # Выполнить команду (из SIMPL)
EXPORT;{source};{dest}\n           # Запрос файлов
IMPORT:START:{count}\n             # Начало передачи файлов
FILE:META:{json}\n                 # Метаданные файла
FILE:CHUNK:{size}\n{data}          # Чанк файла
IMPORT:END\n                       # Конец передачи
KICK:{reason}\n                    # Отключение
```

### Клиент → Сервер

```python
OUTPUT:START:{count}\n             # Начало результата
OUTPUT:CHUNK:{escaped_data}\n      # Чанк результата
OUTPUT:END\n                       # Конец результата
EXPORT:START:{json}\n              # Начало отправки файлов
FILE:META:{json}\n                 # Метаданные файла
FILE:CHUNK:{size}\n{data}          # Чанк файла
EXPORT:COMPLETE\n                  # Конец передачи
```

---

## 🔧 Особенности реализации

### Чанкирование результатов
**Проблема:** Большие выводы могут быть обрезаны  
**Решение:** Разбивка на чанки с заменой `\n` на `<<<NL>>>`

```python
# Клиент
for i in range(0, len(output), chunk_size):
    chunk = output[i:i+chunk_size]
    escaped = chunk.replace('\n', '<<<NL>>>')
    send(f"OUTPUT:CHUNK:{escaped}\n")

# Сервер
chunk_restored = chunk_data.replace('<<<NL>>>', '\n')
output_buffers[client_id]["lines"].append(chunk_restored)
```

### Накопление результатов SIMPL
**Проблема:** SIMPL отправляет N команд → N результатов  
**Решение:** Накопление в `accumulated_output`

```python
# При каждом OUTPUT:END
cmd_info["accumulated_output"].append(full_output)
cmd_info["received_commands"] += 1

# Когда все получены
if received >= total:
    combined = "\n\n".join(accumulated_output)
    save_command_output(client_id, command, combined, "OUTPUT")
```

### Использование alias в файлах
**Зачем:** Читаемые имена файлов вместо client_id

```python
# Получение alias
with open(Config.FILE_USERS, "r", encoding="utf-8") as f:
    data = json.load(f)
    alias = data['users'][client_id]["alias"]

# Использование
filename = f"output_command_{alias}.txt"
client_dir = Path(Config.DIR_FILES) / alias
```

---

## 📊 Диаграмма потока данных

```
┌─────────────┐
│  Админ      │
│  вводит     │
│  команду    │
└──────┬──────┘
       ↓
┌──────────────────────────────────────┐
│  server_input()                      │
│  Парсинг команды                     │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  CommandMonitor.register()           │
│  active_commands[client] = {...}     │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  clients[client_id].write()          │
│  Отправка команды клиенту            │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  handle_client()                     │
│  Ожидание результата                 │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  OUTPUT:CHUNK: → output_buffers      │
│  Накопление по частям                │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  OUTPUT:END                          │
│  accumulated_output.append()         │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  Все результаты получены?            │
│  received >= total                   │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  CommandMonitor.save_command_output()│
│  ~/trash/output_command_{alias}.txt  │
└──────┬───────────────────────────────┘
       ↓
┌──────────────────────────────────────┐
│  last_outputs[client] = {...}        │
│  Сохранение для команды save         │
└──────────────────────────────────────┘
```

---

## 🎯 Ключевые моменты

1. **Асинхронность:** `asyncio` позволяет обрабатывать множество клиентов одновременно
2. **Накопление результатов:** Для SIMPL собираем все результаты перед сохранением
3. **Чанкирование:** Большие данные передаются по частям для надёжности
4. **Отложенные команды:** Автоматическое выполнение при подключении
5. **Группы:** Массовая отправка команд по группам
6. **Алиасы:** Удобные имена пользователей для файлов
7. **Защита от сбоев:** Изоляция ошибок, таймауты, graceful degradation
8. **Логирование:** Полная история действий

---

**Версия:** 3.0  
**Последнее обновление:** 2025-02-23

