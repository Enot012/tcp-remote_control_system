"""
╔══════════════════════════════════════════════════════════════════════════╗
║                TCP CLIENT - СИСТЕМА УДАЛЁННОГО УПРАВЛЕНИЯ                ║
║                                    V3.2                                  ║
╚══════════════════════════════════════════════════════════════════════════╝

УЛУЧШЕНАЯ ВЕРСИЯ 3.1
-Добавленые классы
    -RawBuffer  безопасная работа с сокетом
    -Connection > управление подключением и переподключением
    -CommandExecutor > выполнение команд с таймаутом
    -FileTransfer > отправка/приём файлов
    -OutputSender > отправка вывода чанками
    -MessageHandler > обработка всех сообщений от сервера
    -ServerMsg > Enum для описания стека сообщений от сервера
"""



import socket
import threading
import subprocess
import os
import json
import time
import sys
from pathlib import Path
from typing import Optional
from enum import StrEnum


# ═══════════════════════════════════════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ═══════════════════════════════════════════════════════════════════════════

class Config:
    HOST               = "192.168.0.50"
    PORT               = 9000
    CHUNK_SIZE         = 65536
    RECONNECT_DELAY    = 5
    MAX_RECONNECT      = 0        # 0 = бесконечно
    CONNECT_TIMEOUT    = 10
    HANDSHAKE_TIMEOUT  = 3
    CMD_TIMEOUT        = 30




class ServerMsg(StrEnum):
    """Сообщения которые приходят от сервера"""
    CMD          = "cmd"
    FILETRU      = "filetru"
    IMPORT_START = "import:start"
    EXPORT       = "export"
    KICK         = "kick"
    SHUTDOWN     = "server_shutdown"


# ═══════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════

class Logger:

    @staticmethod
    def log(level: str, message: str):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [{level}] {message}")


# ═══════════════════════════════════════════════════════════════════════════
# ИДЕНТИФИКАЦИЯ КЛИЕНТА
# ═══════════════════════════════════════════════════════════════════════════

class ClientIdentity:
    """Кто мы — имя, ОС, домашняя папка. Создаётся один раз при старте."""

    def __init__(self):
        self.os_name   = sys.platform
        self.username  = (
            os.environ.get("USERNAME", "unknown_win")
            if self.os_name == "win32"
            else os.environ.get("USER", "unknown_else")
        )
        self.home_path = os.path.expanduser("~")

    def handshake_str(self) -> str:
        """Строка регистрации которую ждёт сервер"""
        return f"{self.username},{self.os_name},{self.home_path}\n"

    def get_encoding(self) -> str:
        """Кодировка вывода команд — зависит от ОС"""
        return "cp866" if self.os_name == "win32" else "utf-8"


# ═══════════════════════════════════════════════════════════════════════════
# СЫРОЙ БУФЕР — РАБОТА С СОКЕТОМ
# ═══════════════════════════════════════════════════════════════════════════

class RawBuffer:
    """
    Поточнобезопасный буфер байт.
    Сокет — один, но читают из него два места:
    текстовые строки (receive) и бинарные файлы (import).
    Буфер гарантирует что байты не теряются и не перемешиваются.
    """

    def __init__(self):
        self._buf  = b""
        self._lock = threading.Lock()

    def feed(self, data: bytes):
        """Добавить данные в буфер"""
        with self._lock:
            self._buf += data

    def read_line(self, sock: socket.socket) -> str:
        """Блокирующее чтение строки до \\n"""
        with self._lock:
            while b"\n" not in self._buf:
                self._lock.release()
                try:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("Соединение закрыто")
                finally:
                    self._lock.acquire()
                self._buf += chunk

            line, self._buf = self._buf.split(b"\n", 1)
            return line.decode("utf-8", errors="ignore").strip()

    def read_exact(self, sock: socket.socket, size: int) -> bytes:
        """Блокирующее чтение ровно size байт"""
        with self._lock:
            while len(self._buf) < size:
                self._lock.release()
                try:
                    chunk = sock.recv(min(Config.CHUNK_SIZE, size - len(self._buf)))
                    if not chunk:
                        raise ConnectionError("Соединение закрыто")
                finally:
                    self._lock.acquire()
                self._buf += chunk

            data, self._buf = self._buf[:size], self._buf[size:]
            return data




class Connection:
    """Управляет сокетом и переподключением"""

    def __init__(self, identity: ClientIdentity, buf: RawBuffer):
        self._identity      = identity
        self._buf           = buf
        self._sock: Optional[socket.socket] = None
        self._connected     = False
        self._reconnect_cnt = 0
        self._lock          = threading.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> bool:
        """Подключается и отправляет handshake. Возвращает True при успехе."""
        while True:
            if Config.MAX_RECONNECT > 0 and self._reconnect_cnt >= Config.MAX_RECONNECT:
                Logger.log("ERROR", f"Достигнут лимит попыток ({Config.MAX_RECONNECT})")
                return False
            try:
                Logger.log("INFO", f"Подключение к {Config.HOST}:{Config.PORT}...")
                sock = socket.socket()
                sock.settimeout(Config.CONNECT_TIMEOUT)
                sock.connect((Config.HOST, Config.PORT))
                sock.settimeout(None)

                # Handshake
                sock.sendall(self._identity.handshake_str().encode("utf-8"))

                # Читаем возможное начальное сообщение (KICK или молчание)
                sock.settimeout(Config.HANDSHAKE_TIMEOUT)
                try:
                    initial = sock.recv(4096)
                    if initial:
                        self._buf.feed(initial)
                except socket.timeout:
                    pass
                sock.settimeout(None)

                self._sock          = sock
                self._connected     = True
                self._reconnect_cnt = 0
                Logger.log("SUCCESS", f"Подключён как {self._identity.username}")
                return True

            except (socket.timeout, ConnectionRefusedError) as e:
                self._reconnect_cnt += 1
                Logger.log("ERROR", f"{e} (попытка {self._reconnect_cnt})")
                time.sleep(Config.RECONNECT_DELAY)

            except Exception as e:
                self._reconnect_cnt += 1
                Logger.log("ERROR", f"Ошибка: {e} (попытка {self._reconnect_cnt})")
                time.sleep(Config.RECONNECT_DELAY)

    def disconnect(self):
        self._connected = False
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def send(self, data: bytes):
        if self._sock:
            self._sock.sendall(data)

    def get_sock(self) -> Optional[socket.socket]:
        return self._sock




class CommandExecutor:
    """Выполняет shell-команды и возвращает результат строкой"""

    def __init__(self, identity: ClientIdentity):
        self._encoding = identity.get_encoding()

    def run(self, cmd: str) -> str:
        if cmd == "CANCEL_TIMEOUT":
            return "КОМАНДА ОТМЕНЕНА ПО ТАЙМАУТУ"
        if cmd == "CANCEL_MANUAL":
            return "КОМАНДА ОТМЕНЕНА ВРУЧНУЮ"
        try:
            proc = subprocess.run(
                cmd, shell=True, capture_output=True,
                encoding=self._encoding, errors="replace",
                timeout=Config.CMD_TIMEOUT
            )
            result = proc.stdout or ""
            if proc.stderr:
                result += f"\n[STDERR]:\n{proc.stderr}"
            return result.strip() or f"Выполнено. Code: {proc.returncode}"
        except subprocess.TimeoutExpired:
            return "ОШИБКА: Таймаут команды"
        except Exception as e:
            return f"ОШИБКА: {type(e).__name__}: {e}"




class FileTransfer:
    """Отправка (EXPORT) и приём (IMPORT) файлов"""

    def __init__(self, conn: Connection, buf: RawBuffer):
        self._conn = conn
        self._buf  = buf

    def export(self, source_path: str, dest_dir: str):
        """Клиент отправляет файлы на сервер"""
        path  = Path(source_path)
        if not path.exists():
            self._conn.send(f"EXPORT:ERROR:Путь не найден: {source_path}\n".encode())
            return

        files = self._list_files(path)
        if not files:
            self._conn.send(f"EXPORT:ERROR:Нет файлов в {source_path}\n".encode())
            return

        meta = json.dumps({"count": len(files), "dest_dir": dest_dir, "source": path.name})
        self._conn.send(f"EXPORT:START:{meta}\n".encode())
        Logger.log("EXPORT", f"{len(files)} файлов → сервер")

        for fi in files:
            if not self._send_file(fi["path"], fi["rel_path"], fi["size"]):
                self._conn.send(b"EXPORT:ABORT\n")
                return

        self._conn.send(b"EXPORT:COMPLETE\n")
        Logger.log("EXPORT", "Завершён")

    def import_files(self, meta_payload: str):
        """Клиент получает файлы от сервера"""
        try:
            meta         = json.loads(meta_payload)
            count        = meta["count"]
            dest_dir     = meta.get("dest_dir", "received")
            Logger.log("IMPORT", f"{count} файлов из '{meta.get('source')}' → {dest_dir}")

            for _ in range(count):
                sock     = self._conn.get_sock()
                meta_line = self._buf.read_line(sock)
                if not meta_line.startswith("FILE:META:"):
                    Logger.log("ERROR", f"Ожидался FILE:META, получено: {meta_line}")
                    break
                file_meta = json.loads(meta_line[10:])
                self._receive_file(file_meta, dest_dir, count)

            self._conn.send(b"IMPORT:COMPLETE\n")
            Logger.log("IMPORT", "Завершён")

        except Exception as e:
            Logger.log("ERROR", f"Ошибка импорта: {e}")
            self._conn.send(f"IMPORT:ERROR:{e}\n".encode())

    # ── private ──────────────────────────────────────────────────────────

    def _send_file(self, path: str, rel_path: str, size: int) -> bool:
        try:
            meta = json.dumps({"rel_path": rel_path, "size": size})
            self._conn.send(f"FILE:META:{meta}\n".encode())
            sent = 0
            with open(path, "rb") as f:
                while chunk := f.read(Config.CHUNK_SIZE):
                    self._conn.send(chunk)
                    sent += len(chunk)
                    print(f"\r  {rel_path}: {sent * 100 // size if size else 100}%",
                          end="", flush=True)
            print(f"\r  ✓ {rel_path} ({size / 1024:.1f} KB)")
            self._conn.send(b"FILE:END\n")
            return True
        except Exception as e:
            Logger.log("ERROR", f"Ошибка отправки {rel_path}: {e}")
            return False

    def _receive_file(self, file_meta: dict, dest_dir: str, total_count: int):
        rel_path  = file_meta["rel_path"]
        size      = file_meta["size"]
        dest      = Path(dest_dir)
        save_path = dest if (total_count == 1 and dest.suffix) else dest / rel_path
        save_path.parent.mkdir(parents=True, exist_ok=True)

        received = 0
        sock     = self._conn.get_sock()
        with open(save_path, "wb") as f:
            while received < size:
                chunk = self._buf.read_exact(sock, min(Config.CHUNK_SIZE, size - received))
                f.write(chunk)
                received += len(chunk)
                print(f"\r  {save_path.name}: {received * 100 // size if size else 100}%",
                      end="", flush=True)
        print(f"\r  ✓ {save_path.name} ({size / 1024:.1f} KB)")

        end_marker = self._buf.read_line(sock)
        if not end_marker.startswith("FILE:END"):
            Logger.log("WARNING", f"Неожиданный маркер: {end_marker}")

    @staticmethod
    def _list_files(path: Path) -> list:
        if path.is_file():
            return [{"path": str(path), "rel_path": path.name, "size": path.stat().st_size}]
        return [
            {"path": str(f), "rel_path": str(f.relative_to(path)), "size": f.stat().st_size}
            for f in path.rglob("*") if f.is_file()
        ]




class OutputSender:
    """Отправляет текстовый вывод команды чанками"""

    def __init__(self, conn: Connection):
        self._conn = conn

    def send(self, prefix: str, text: str, chunk_size: int = 100):
        lines = text.split("\n")
        self._conn.send(f"{prefix}:START:{len(lines)}\n".encode())
        for i in range(0, len(lines), chunk_size):
            chunk = "\n".join(lines[i:i + chunk_size]).replace("\n", "<<<NL>>>")
            self._conn.send(f"{prefix}:CHUNK:{chunk}\n".encode("utf-8", errors="replace"))
        self._conn.send(f"{prefix}:END\n".encode())


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК СООБЩЕНИЙ ОТ СЕРВЕРА
# ═══════════════════════════════════════════════════════════════════════════

class MessageHandler:
    """
    Зеркало серверного ProtocolHandler.
    Получает строку от сервера и вызывает нужный обработчик.
    """

    def __init__(self, conn: Connection, buf: RawBuffer,
                 executor: CommandExecutor, transfer: FileTransfer,
                 sender: OutputSender):
        self._conn     = conn
        self._buf      = buf
        self._executor = executor
        self._transfer = transfer
        self._sender   = sender

    def handle(self, msg: str) -> bool:
        """Возвращает False если нужно завершить работу"""

        if msg.startswith("CMD:"):
            cmd    = msg[4:].strip()
            Logger.log("CMD", cmd)
            result = self._executor.run(cmd)
            self._sender.send("OUTPUT", result)

        elif msg.startswith("FILETRU:"):
            cmd    = msg[8:].strip()
            Logger.log("FILETRU", cmd)
            result = self._executor.run(cmd)
            self._sender.send("FILETRU", result)

        elif msg.startswith("IMPORT:START:"):
            self._transfer.import_files(msg[13:])

        elif msg.startswith("EXPORT;"):
            parts = msg[7:].split(";", 1)
            if len(parts) == 2:
                self._transfer.export(parts[0].strip(), parts[1].strip())
            else:
                self._conn.send("EXPORT:ERROR:Неверный формат\n")

        elif msg.startswith("KICK:"):
            Logger.log("KICK", msg[5:].strip())
            return False   # сигнал завершить работу

        elif msg == "SERVER_SHUTDOWN":
            Logger.log("INFO", "Сервер остановлен")
            return False

        else:
            print(f"\n{msg}")

        return True




class TCPClient:
    """Собирает все компоненты и запускает клиент"""

    def __init__(self):
        self._identity = ClientIdentity()
        self._buf      = RawBuffer()
        self._conn     = Connection(self._identity, self._buf)
        self._executor = CommandExecutor(self._identity)
        self._transfer = FileTransfer(self._conn, self._buf)
        self._sender   = OutputSender(self._conn)
        self._handler  = MessageHandler(
            self._conn, self._buf,
            self._executor, self._transfer, self._sender
        )

    def run(self):
        if not self._conn.connect():
            Logger.log("CRITICAL", "Не удалось подключиться")
            sys.exit(1)

        # Поток приёма сообщений
        recv_thread = threading.Thread(target=self._receive_loop, daemon=True)
        recv_thread.start()
        recv_thread.join()

    def _receive_loop(self):
        while True:
            try:
                sock = self._conn.get_sock()
                msg  = self._buf.read_line(sock)
                if not msg:
                    continue
                if not self._handler.handle(msg):
                    break   # KICK или SHUTDOWN
            except ConnectionError:
                Logger.log("ERROR", "Соединение разорвано")
                self._conn.disconnect()
                if not self._conn.connect():
                    break
            except Exception as e:
                Logger.log("ERROR", f"Ошибка: {e}")
                self._conn.disconnect()
                if not self._conn.connect():
                    break




if __name__ == "__main__":
    try:
        TCPClient().run()
    except KeyboardInterrupt:
        Logger.log("INFO", "Завершение работы (Ctrl+C)")