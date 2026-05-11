"""
server.py — asyncio TCP-сервер; точка входа фонового потока.

Назначение:
    Запускает и удерживает asyncio event loop в отдельном потоке.
    GUI импортирует run_server() и геттеры состояния.

Что содержит:
    Глобальные переменные:
        _loop        — asyncio event loop (доступен GUI через get_loop()).
        _server_obj  — объект asyncio.Server.
        _dispatcher  — ServerDispatcher (доступен GUI через get_dispatcher()).
        _state       — ServerState (доступен GUI через get_state()).
        _user_mgr    — UserManager (доступен GUI через get_user_mgr()).

    get_loop() / get_dispatcher() / get_state() / get_user_mgr()
                 — геттеры для моста GUI ↔ asyncio.

    handle_client()  — корутина для каждого TCP-подключения:
                       читает первую строку (username,os,path),
                       регистрирует клиента, запускает отложенные команды,
                       входит в цикл чтения и передаёт сообщения
                       в ProtocolDispatcher; при отключении — cleanup.

    _run_scheduled() — выполняет отложенные задачи из ScheduledManager
                       сразу после подключения клиента.

    _periodic_save() — каждые Config.STATE_SAVE_INTERVAL секунд вызывает
                       ServerState.save().

    _cleanup()       — graceful shutdown: помечает всех пользователей
                       как OFF, сохраняет состояние.

    _main()          — инициализирует все менеджеры, запускает сервер,
                       запускает gather(serve_forever, monitor_loop,
                       _periodic_save).

    run_server()     — вызывается из GUI:
                       threading.Thread(target=run_server).start()
                       Внутри запускает asyncio.run(_main()).

Импортирует: config.py, managers.py, handlers.py.
Импортируется из: gui.py.
"""

import asyncio

import time
import traceback

from typing import Optional

from TCP_server_v3_4 import ServerCmd
from config import Config, ClientMsgParser
from managers import (
    BanManager, CommandMonitor, ensure_dirs, FileTransfer,
    GroupManager, Logger, ScheduledManager, ServerState, UserManager,TemplateManager,
)
from handlers import (
    CommandHandler, ProtocolDispatcher, ProtocolHandler,
    ServerDispatcher,
)

# Глобальный loop — GUI получает его через get_loop()
_loop: Optional[asyncio.AbstractEventLoop] = None
_server_obj  = None
_dispatcher: Optional[ServerDispatcher]    = None
_state:      Optional[ServerState]         = None
_user_mgr:   Optional[UserManager]         = None
_template_mgr: Optional[TemplateManager]   = None


def get_loop() -> Optional[asyncio.AbstractEventLoop]:
    return _loop


def get_dispatcher() -> Optional[ServerDispatcher]:
    return _dispatcher


def get_state() -> Optional[ServerState]:
    return _state


def get_user_mgr() -> Optional[UserManager]:
    return _user_mgr

def get_template_mgr():
    return _template_mgr

# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТКА КЛИЕНТА
# ═══════════════════════════════════════════════════════════════════════════

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        state: ServerState, user_mgr: UserManager,
                        sched_mgr: ScheduledManager, monitor: CommandMonitor):
    addr      = writer.get_extra_info("peername")
    client_id = None

    try:
        raw   = await asyncio.wait_for(reader.readline(), timeout=10)
        parts = raw.decode("utf-8").strip().split(",")
        if len(parts) < 3 or not parts[0]:
            writer.close()
            await writer.wait_closed()
            return

        client_id = parts[0]
        user_mgr.register(client_id, parts[1], parts[2])
        state.add_client(client_id, writer)
        Logger.log("CONNECT", f"подключился ({addr[0]}:{addr[1]})", client_id)
        state.save()

        proto_handler    = ProtocolHandler(client_id, state, user_mgr, sched_mgr, monitor)
        proto_dispatcher = ProtocolDispatcher(proto_handler)

        await _run_scheduled(client_id, writer, state, sched_mgr)

        consecutive_errors = 0

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
                continue
            except ConnectionError:
                break
            except Exception as e:
                Logger.log("ERROR", f"Ошибка цикла: {e}", client_id)
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    Logger.log("ERROR", "Много ошибок — отключение", client_id)
                    break

    except Exception as e:
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


# ═══════════════════════════════════════════════════════════════════════════
# ОТЛОЖЕННЫЕ КОМАНДЫ (выполняются при подключении клиента)
# ═══════════════════════════════════════════════════════════════════════════

async def _run_scheduled(client_id: str, writer: asyncio.StreamWriter,
                         state: ServerState, sched_mgr: ScheduledManager):
    user_cmds    = sched_mgr.get_for_user(client_id)
    all_commands = sched_mgr.get_all()

    for cmd_data in user_cmds:
        try:
            idx      = all_commands.index(cmd_data)
            cmd_type = cmd_data["command_type"].lower()

            def sub(text: str) -> str:
                return sched_mgr.sub_path(text, client_id)

            if cmd_type == ServerCmd.CMD:
                command = sub(cmd_data["command"])
                state.register_command(client_id, command, "CMD", 1)
                writer.write(f"CMD:{command}\n".encode())
                await writer.drain()
                state.push_scheduled(client_id, idx)

            elif cmd_type == ServerCmd.SIMPL:
                tmpl_type = cmd_data["template_type"]
                commands = []
                if tmpl_type == "default":
                    if Config.FILE_CODE.exists():
                        commands = [l.strip() for l in
                                    Config.FILE_CODE.read_text("utf-8").splitlines() if l.strip()]

                else:
                    commands=_template_mgr.get_comd_template_name(tmpl_type)
                if commands:
                    state.register_command(client_id, f"simpl ({len(commands)} команд)",
                                           "FILETRU", len(commands))
                    for cmd in commands:
                        writer.write(f"FILETRU:{sub(cmd)}\n".encode())
                        await writer.drain()
                        await asyncio.sleep(0.2)
                    state.push_scheduled(client_id, idx)




            elif cmd_type == ServerCmd.IMPORT:
                src = sub(cmd_data["source_path"])
                dst = sub(cmd_data["dest_path"])
                state.register_command(client_id, f"import {src}", "IMPORT", 1)
                await FileTransfer.send_to_client(client_id, src, dst, state)
                state.unregister_command(client_id)
                sched_mgr.mark_done(idx, client_id, f"IMPORT: {src} → {dst} [OK]")

            elif cmd_type == ServerCmd.EXPORT:
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
# ПЕРИОДИКА И ЗАВЕРШЕНИЕ
# ═══════════════════════════════════════════════════════════════════════════

async def _periodic_save(state: ServerState):
    while True:
        await asyncio.sleep(Config.STATE_SAVE_INTERVAL)
        state.save()


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


# ═══════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════

async def _main():
    global _loop, _server_obj, _dispatcher, _state, _user_mgr, _template_mgr

    ensure_dirs()
    _loop = asyncio.get_running_loop()

    _state    = ServerState()
    _user_mgr = UserManager()
    group_mgr = GroupManager(_user_mgr, _state)
    sched_mgr = ScheduledManager(_user_mgr, group_mgr)
    monitor   = CommandMonitor(_state, _user_mgr)
    ban_mgr   = BanManager(_state)
    _template_mgr = TemplateManager()

    handler     = CommandHandler(_state, _user_mgr, group_mgr, sched_mgr, ban_mgr, monitor, _template_mgr)
    _dispatcher = ServerDispatcher(handler)

    Logger.log("INFO", f"Запуск TCP-сервера {Config.HOST}:{Config.PORT}")

    prev = ServerState.load()
    if prev:
        Logger.log("INFO", f"Найдено состояние от {prev['datetime']}")

    try:
        _server_obj = await asyncio.start_server(
            lambda r, w: handle_client(r, w, _state, _user_mgr, sched_mgr, monitor),
            Config.HOST, Config.PORT,
        )
        Logger.log("INFO", f"Сервер слушает {Config.HOST}:{Config.PORT}")

        async with _server_obj:
            await asyncio.gather(
                _server_obj.serve_forever(),
                monitor.monitor_loop(),
                _periodic_save(_state),
            )
    except Exception as e:
        Logger.log("CRITICAL", f"Критическая ошибка: {e}")
        Logger.crash(e, traceback.format_exc(), _state)
        _cleanup(_state, _user_mgr)
        raise


def run_server():
    """Вызывается из GUI в отдельном потоке: threading.Thread(target=run_server).start()"""
    asyncio.run(_main())


# ── запуск без GUI (режим терминала) ──────────────────────────────────────
if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        Logger.crash(e, traceback.format_exc(), ServerState())
