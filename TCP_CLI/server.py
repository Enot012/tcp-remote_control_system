"""
TCP-сервер: точка входа, обработка клиентов, периодика и сигналы.
"""

import asyncio
import time
import signal
import sys
import socket
import traceback

from config import Config, ServerCmd, ClientMsgParser, ServerCmdParser, ensure_dirs, print_help
from managers import (
    Logger, ServerState, UserManager, GroupManager,
    ScheduledManager, CommandMonitor, BanManager, FileTransfer,TemplateManager
)
from handlers import (
    CommandHandler, ServerDispatcher,
    ProtocolHandler, ProtocolDispatcher,
)


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
# ОТЛОЖЕННЫЕ КОМАНДЫ ПРИ ПОДКЛЮЧЕНИИ
# ═══════════════════════════════════════════════════════════════════════════

async def _run_scheduled(client_id: str, writer: asyncio.StreamWriter,
                         state: ServerState, template_mgr: TemplateManager,
                         sched_mgr: ScheduledManager ):
    """Выполняет накопленные отложенные команды при подключении клиента."""
    user_cmds    = sched_mgr.get_for_user(client_id)
    all_commands = sched_mgr.get_all()

    for cmd_data in user_cmds:
        try:
            idx      = all_commands.index(cmd_data)
            cmd_type = cmd_data["command_type"]

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
                    commands=template_mgr.get_comd_template_name(tmpl_type)
                if commands:
                    state.register_command(client_id, f"simpl ({len(commands)} команд) - шаблон {tmpl_type}",
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
# ОБРАБОТКА КЛИЕНТА
# ═══════════════════════════════════════════════════════════════════════════

async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                        state: ServerState, user_mgr: UserManager,
                        sched_mgr: ScheduledManager, monitor: CommandMonitor, template_mgr : TemplateManager):
    addr      = writer.get_extra_info("peername")
    client_id = None
    ip = socket.gethostbyname(socket.gethostname())
    try:
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

        proto_handler    = ProtocolHandler(client_id, state, user_mgr, sched_mgr, monitor)
        proto_dispatcher = ProtocolDispatcher(proto_handler)

        await _run_scheduled(client_id, writer, state, template_mgr, sched_mgr)

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
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

async def TCPServer():
    ensure_dirs()

    state     = ServerState()
    user_mgr  = UserManager()
    group_mgr = GroupManager(user_mgr, state)
    sched_mgr = ScheduledManager(user_mgr, group_mgr)
    monitor   = CommandMonitor(state, user_mgr)
    ban_mgr   = BanManager(state)
    template  = TemplateManager()

    handler    = CommandHandler(state, user_mgr, group_mgr, sched_mgr, ban_mgr, monitor, template)
    dispatcher = ServerDispatcher(handler)
    template_mgr = TemplateManager()

    setup_signal_handlers(state, user_mgr)
    Logger.log("INFO", "Запуск сервера...")

    prev = ServerState.load()
    if prev:
        Logger.log("INFO", f"Найдено состояние от {prev['datetime']}")

    try:
        server = await asyncio.start_server(
            lambda r, w: handle_client(r, w, state, user_mgr, sched_mgr, monitor, template_mgr),
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
        Logger.crash(e, traceback.format_exc(), ServerState())

