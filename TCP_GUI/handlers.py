"""
handlers.py — обработчики серверных команд и клиентского протокола.

Назначение:
    Вся бизнес-логика — здесь. Методы не вызывают input() и не знают о GUI.
    Данные (target, command, paths…) приходят как аргументы из ServerDispatcher,
    который в свою очередь вызывается из GUI или напрямую из server.py.

Что содержит:
    _sync_to_async()    — оборачивает синхронный метод в корутину,
                          чтобы ServerDispatcher мог вызывать всё единообразно.

    CommandHandler      — реализует каждую ServerCmd:
                          cmd, simpl, export, import_, save, cancel, kick,
                          list_users, rename, status,
                          group_new/list/del/add/rm,
                          chart_new/list/del/comd.
                          Принимает: ServerState, UserManager, GroupManager,
                          ScheduledManager, BanManager, CommandMonitor.
                          Возвращает строку-результат для лога.

    ServerDispatcher    — маршрутизирует ServerCmd → метод CommandHandler.
                          Используется: server.py (_dispatcher.dispatch),
                          gui.py (ServerApp.dispatch).

    ProtocolHandler     — обрабатывает входящие ClientMsg от одного клиента:
                          on_output_start/chunk/end  — собирает вывод команды,
                          on_filetru_start/chunk/end — вывод SIMPL-команд,
                          on_export_start            — принимает файлы от клиента,
                          on_import_complete/error   — подтверждение/ошибка IMPORT.
                          Создаётся отдельно для каждого подключения в server.py.

    ProtocolDispatcher  — маршрутизирует ClientMsg → метод ProtocolHandler.
                          Используется: server.py (handle_client).

Импортирует: config.py, managers.py.
Импортируется из: server.py.
"""

import asyncio
import json
import time
from pathlib import Path
# from tempfile import template
from typing import Callable, Dict, Optional

from config import Config, ServerCmd, ClientMsg,CMD_HINTS
from managers import (
    BanManager, CommandMonitor, FileTransfer,
    GroupManager, Logger, ScheduledManager,
    ServerState, UserManager, TemplateManager
)


# ═══════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНАЯ ОБЁРТКА
# ═══════════════════════════════════════════════════════════════════════════

def _sync_to_async(fn: Callable) -> Callable:
    """Оборачивает синхронный метод в корутину для единообразия диспетчера."""
    async def wrapper(args: list):
        return fn(args)
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК СЕРВЕРНЫХ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class CommandHandler:
    """
    Вся бизнес-логика серверных команд.

    Ключевое отличие от v3.4: методы НЕ вызывают input().
    Все данные (target, command, paths...) передаются как аргументы.
    GUI собирает их через диалог и передаёт сюда.
    """

    def __init__(self, state: ServerState, user_mgr: UserManager,
                 group_mgr: GroupManager, sched_mgr: ScheduledManager,
                 ban_mgr: BanManager, monitor: CommandMonitor, template: TemplateManager):
        self._state     = state
        self._user_mgr  = user_mgr
        self._group_mgr = group_mgr
        self._sched_mgr = sched_mgr
        self._ban_mgr   = ban_mgr
        self._monitor   = monitor
        self._template  = template

    # ── helpers ──────────────────────────────────────────────────────────

    def _resolve(self, name: str) -> Optional[str]:
        """Возвращает реальное имя: 'all', 'group:X', или username."""
        if name == "all":
            return name
        if name.startswith("group:"):
            gname = name[6:]
            return name if gname in self._group_mgr.get_all_group() else None
        return self._user_mgr.validate(name)

    def _targets(self, target: str) -> list:
        """Разворачивает 'all' / group: / username в список подключённых."""
        if target == "all":
            return self._state.get_all_clients()
        if target.startswith("group:"):
            return self._group_mgr.get_online_users(target[6:])
        real = self._user_mgr.validate(target)
        return [real] if real and self._state.is_connected(real) else []

    def _sub(self, text: str, cid: str) -> str:
        """Вставляет плесхолдеры"""
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

    @staticmethod
    def _read_code_file() -> list:
        try:
            return [l.strip() for l in Config.FILE_CODE.read_text("utf-8").splitlines() if l.strip()]
        except FileNotFoundError:
            Logger.log("ERROR", f"Файл {Config.FILE_CODE} не найден")
            return []

    def check_templ(self,name) -> bool:
        """Возвращает True если в шаблоне нету команд. Удаляет шаблон"""
        if len(self._template.get_comd_template_name(name)) <= 0:
            self._template.delete(name)
            Logger.log("CMD",f"Шаблон '{name}' удален. Шаблон пуст. ")
            return True
        return False

    def check_group(self, name) ->bool:
        """Возвращает True если в группе нету пользователей. Удаляет группу"""
        if len(self._group_mgr.get_members(name)) <= 0:
            self._group_mgr.delete(name)
            Logger.log ("CMD", f"Группа '{name}' удалена. Группа пуста. ")
            return True
        return False


    # ── онлайн-команды ────────────────────────────────────────────────────

    async def batch(self, args: list) -> str:
        """
        Мульти-cmd, принимает больше одной команды
        args: [target, comd1, comd2 ...]
        """

        if len (args) < 2:
            return f"Формат: {CMD_HINTS.get (ServerCmd.BATCH, 'Формат не найден!')}"
        target = args[0]
        commands = args[1:]
        users = self._targets (target)
        if not users:
            return "Нет подключённых пользователей"
        for u in users:
            await self._send_simpl (u, commands)
        return f"BATCH ({len (commands)} команд) → {target}"


    async def cmd(self, args: list) -> str:
        """
        args: [target, *command_parts]
        Пример: ["user1", "ls", "-la"]
        """
        if len(args) < 2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.CMD,'Формат не найден!')}"
        target, command = args[0], " ".join(args[1:])
        if not self._resolve(target):
            return f"Пользователь/группа '{target}' не найден"
        users = self._targets(target)
        if not users:
            return "Нет подключённых пользователей"
        for cid in users:
            c = self._sub(command, cid)
            self._state.register_command(cid, c, "CMD", 1)
            self._state.get_writer(cid).write(f"CMD:{c}\n".encode())
            await self._drain(cid)
        return f"CMD → {target} ({len(users)} польз.)"


    async def simpl(self, args: list) -> str:
        """args: [target, template_name?]"""
        if not args:
            return f"Формат: {CMD_HINTS.get (ServerCmd.SIMPL, 'Формат не найден!')}"

        target = args[0]
        if not self._resolve (target):
            return f"'{target}' не найден"

        # определяем откуда брать команды
        if len (args) > 1 and (template_name := args[1] if self._template.check_template_name(args[1]) else ''):
            commands = self._template.get_comd_template_name (template_name)
        else:
            commands = self._read_code_file ()
            if not commands:
                return "Файл code.txt пуст или не найден"

        users = self._targets (target)
        if not users:
            return "Нет подключённых пользователей"
        for cid in users:
            await self._send_simpl (cid, commands)

        return f"SIMPL ({len (commands)} команд) → {target}"

    async def export(self, args: list) -> str:
        """args: [target, src_path, dest_path?]"""
        if len(args) < 2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.EXPORT,'Формат не найден!')}"
        users = self._targets(args[0])
        if not users:
            return "Нет подключённых пользователей"
        for cid in users:
            src = self._sub(args[1], cid)
            dst = args[2] if len(args) > 2 else "received"
            self._state.register_command(cid, f"export {src}", "EXPORT", 1)
            self._state.get_writer(cid).write(f"EXPORT;{src};{dst}\n".encode())
            await self._drain(cid)
        return f"EXPORT → {args[0]} ({len(users)} польз.)"

    async def import_(self, args: list) -> str:
        """args: [target, src_server, dest_client?]"""
        if len(args) < 2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.IMPORT,'Формат не найден!')}"
        target = args[0]
        src    = self._sched_mgr.sub_serv_path(args[1])
        dst    = args[2] if len(args) > 2 else "received"
        users  = self._targets(target)
        if not users:
            return "Нет подключённых пользователей"
        sent = 0
        for cid in users:
            d = self._sub(dst, cid)
            self._state.register_command(cid, f"import {src}", "IMPORT", 1)
            await FileTransfer.send_to_client(cid, src, d, self._state)
            self._state.unregister_command(cid)
            sent += 1
        return f"IMPORT отправлено {sent} клиентам"

    async def save(self, args: list) -> str:
        """args: [target, filename]"""
        if len(args) < 2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.SAVE,'Формат не найден!')}"
        target, filename = args[0], args[1]
        now     = time.strftime("%Y-%m-%d %H:%M:%S")
        targets = self._state.get_all_clients() if target == "all" else [target]
        saved   = []
        for cid in targets:
            real = self._resolve(cid) or cid
            out  = self._state.get_last_output(real)
            if not out or not out.get("content"):
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
                Logger.log("ERROR", f"Ошибка сохранения: {e}")
        return f"Сохранено: {len(saved)}/{len(targets)}"

    def list_users(self, args: list) -> str:
        """Возвращает таблицу пользователей строкой (для лога и GUI)."""
        users = self._user_mgr.get_users_data()
        if not users:
            return "Нет пользователей"
        lines = [f"{'№':<4} {'Username':<20} {'Alias':<20} {'Status':<8} "
                 f"{'OS':<8} {'Path':<25} {'Time':<20} {'Group':<25}"]
        lines.append("-" * 130)
        for i, (uname, info) in enumerate(users.items(), 1):
            t = info["last_login"] if info["status"] == "ON" else info["last_logout"]
            lines.append(
                f"{i:<4} {uname:<20} {info['alias']:<20} {info['status']:<8} "
                f"{info['OS']:<8} {info['default_path']:<25} {t or '':<20} "
                f"{','.join(info['users_in_group']):<25}"
            )
        online = sum(1 for u in users.values() if u["status"] == "ON")
        lines.append(f"\nВсего: {len(users)} | Онлайн: {online}")
        return "\n".join(lines)

    def rename(self, args: list) -> str:
        """args: [target, new_alias]"""
        if len(args) < 2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.RENAME,'Формат не найден!')}"
        target, new_alias = args[0], args[1][:10].replace(" ", "_")
        found = self._resolve(target)
        if not found or found in ("all",) or found.startswith("group:"):
            return f"'{target}' не найден"
        users = self._user_mgr.get_users_data()
        for uname, info in users.items():
            if info["alias"] == new_alias and uname != found:
                return f"Alias '{new_alias}' уже занят"
        old = users[found]["alias"]
        users[found]["alias"] = new_alias
        self._user_mgr.save_user_data(users)
        return f"{found}: '{old}' → '{new_alias}'"

    def status(self, args: list) -> str:
        cmds = self._state.get_all_commands()
        if not cmds:
            return "Нет активных команд"
        lines = []
        for cid, info in cmds.items():
            elapsed = time.time() - info["start_time"]
            lines.append(f"  {cid}: {info['type']} ({elapsed:.1f}s) — {info['command']}")
        return "\n".join(lines)

    async def cancel(self, args: list) -> str:
        """args: [target]"""
        if not args:
            return f"Формат: {CMD_HINTS.get(ServerCmd.CANCEL,'Формат не найден!')}"
        target = args[0]
        if self._state.has_command(target):
            writer = self._state.get_writer(target)
            if writer:
                writer.write(b"CMD:CANCEL_MANUAL\n")
                await self._drain(target)
            self._state.unregister_command(target)
            return f"Отменено для {target}"
        return f"У {target} нет активных команд"

    async def kick(self, args: list) -> str:
        """args: [target]  target = username | 'all'"""
        if not args:
            return f"Формат: {CMD_HINTS.get(ServerCmd.KICK,'Формат не найден!')}"
        target = args[0]
        if target == "all":
            n = 0
            for cid in list(self._state.get_all_clients()):
                if await self._ban_mgr.kick(cid, "Отключены администратором"):
                    n += 1
            return f"Отключено: {n}"
        real = self._resolve(target) or target
        if self._state.is_connected(real):
            await self._ban_mgr.kick(real)
            return f"{real} отключён"
        return f"{real} не подключён"

    # ── группы ───────────────────────────────────────────────────────────

    def group_new(self, args: list) -> str:
        """
        args: [name, user1, user2, ...]
        GUI сам собирает список пользователей и передаёт здесь.
        """
        if len(args) < 1:
            return f"Формат: {CMD_HINTS.get(ServerCmd.GROUP_NEW,'Формат не найден!')}"
        name    = args[0]
        members = args[1:]
        # Валидируем каждого пользователя
        resolved = []
        unknown  = []
        for u in members:
            r = self._user_mgr.validate(u)
            if r:
                resolved.append(r)
            else:
                unknown.append(u)
        if unknown:
            return f"Не найдены пользователи: {', '.join(unknown)}"
        try:
            self._group_mgr.create(name, resolved)
            return f"Группа '{name}' создана ({len(resolved)} уч.)"
        except Exception as e:
            return f"Ошибка: {e}"

    def group_list(self, args: list) -> str:
        groups = self._group_mgr.get_data()
        if not groups:
            return "Нет групп"
        lines = []
        for name, members in groups.items():
            online = sum(1 for m in members if self._state.is_connected(m))
            lines.append(f"  {name}  ({online}/{len(members)} онлайн): {', '.join(members)}")
        return "\n".join(lines)

    def group_del(self, args: list) -> str:
        if not args:
            return f"Формат: {CMD_HINTS.get(ServerCmd.GROUP_DEL,'Формат не найден!')}"
        try:
            self._group_mgr.delete(args[0])
            return f"Группа '{args[0]}' удалена"
        except Exception as e:
            return f"Ошибка: {e}"

    def group_add(self, args: list) -> str:
        """args: [group_name, user1, user2, ...]"""
        if len(args) < 2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.GROUP_ADD,'Формат не найден!')}"
        name, users = args[0], args[1:]
        resolved = []
        unknown  = []
        for u in users:
            r = self._user_mgr.validate(u)
            if r:
                resolved.append(r)
            else:
                unknown.append(u)
        if unknown:
            return f"Не найдены: {', '.join(unknown)}"
        try:
            skipped = self._group_mgr.add_users(name, resolved)
            done = len(resolved) - len(skipped)
            return f"Добавлено: {done}. Уже в группе: {skipped}"
        except Exception as e:
            return f"Ошибка: {e}"

    def group_rm(self, args: list) -> str:
        """args: [group_name, user1, user2, ...]"""
        if len(args) < 1:
            return f"Формат: {CMD_HINTS.get(ServerCmd.GROUP_RM,'Формат не найден!')}"
        name, users = args[0], args[1:]
        try:
            skipped = self._group_mgr.remove_users(name, users)
            done = len(users) - len(skipped)
            if self.check_group(name):
                return
            return f"Удалено: {done}. Не найдено: {skipped}"
        except Exception as e:
            return f"Ошибка: {e}"

    # ── отложенные команды ────────────────────────────────────────────────

    def chart_new(self, args: list) -> str:
        """
        GUI собирает все поля и передаёт как плоский список:
          CMD:    ["cmd",    target, command]
          SIMPL:  ["simpl",  target]
          IMPORT: ["import", target, src, dst]
          EXPORT: ["export", target, src, dst?]
        """
        if len(args) < 2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.CHART_NEW,'Формат не найден!')}"
        cmd_type = args[0].lower()
        target   = args[1]
        template = args[2]
        extra: dict = {}
        if cmd_type == ServerCmd.CMD:
            if len(args) < 3:
                return "CMD требует команду"
            extra = {"command": " ".join(args[2:])}
        elif cmd_type == ServerCmd.SIMPL:
            if template:
                extra = {"template_type": template}

        elif cmd_type in (ServerCmd.IMPORT, ServerCmd.EXPORT):
            if len(args) < 4:
                return f"{cmd_type.upper()} требует src и dst"
            extra = {"source_path": args[2], "dest_path": args[3]}
        else:
            return f"Неверный тип '{cmd_type}'. Допустимые: {', '.join([ServerCmd.CMD, ServerCmd.SIMPL, ServerCmd.IMPORT, ServerCmd.EXPORT])}"

        try:
            self._sched_mgr.create(target, cmd_type, extra)
            return f"Отложенная команда добавлена → '{target}'"
        except Exception as e:
            return f"Ошибка: {e}"

    def chart_list(self, args: list) -> str:
        cmds = self._sched_mgr.get_all()
        if not cmds:
            return "Нет отложенных команд"
        lines = []
        for i, cmd in enumerate(cmds):
            ctype = cmd["command_type"]
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            done  = len(cmd.get("completed_users", []))
            wait  = len(cmd.get("expected_users", []))
            lines.append(f"[{i}] {cmd['target']} → {ctype}: {label}  ✓{done} ⏳{wait}")
        return "\n".join(lines)

    def chart_del(self, args: list) -> str:
        if not args:
            return f"Формат: {CMD_HINTS.get(ServerCmd.CHART_DEL,'Формат не найден!')}"
        try:
            self._sched_mgr.delete(int(args[0]))
            return f"Команда [{args[0]}] удалена"
        except Exception as e:
            return f"Ошибка: {e}"

    def chart_comd(self, args: list) -> str:
        completed = self._sched_mgr.get_completed()
        active    = self._sched_mgr.get_all()
        lines     = [f"── Выполненные ({len(completed)}) ──"]
        for i, cmd in enumerate(completed):
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            lines.append(f"[{i}] {cmd['target']} → {cmd['command_type']}: {label} "
                         f"({cmd.get('completed_at','?')})")
            for u in cmd.get("completed_users", []):
                lines.append(f"    ✓ {u}")
        lines.append(f"\n── В процессе ({len(active)}) ──")
        for i, cmd in enumerate(active):
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            lines.append(f"[{i}] {cmd['target']} → {cmd['command_type']}: {label}")
            for u in cmd.get("completed_users", []):
                lines.append(f"    ✓ {u}")
            for u in cmd.get("expected_users", []):
                lines.append(f"    ⏳ {u}")
        return "\n".join(lines)

    # ── шаблоны команд ────────────────────────────────────────────────

    def template_new(self, args: list) -> str:
        """args[templ_name comd1 comd2 ...]"""
        if len(args)<2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_NEW,'Формат не найден!')}"
        templ_name=args[0]
        templ_comd=args[1:]
        try:
            self._template.create (templ_name, templ_comd)
            return f"Шалблон '{templ_name}' создан с {len (templ_comd)} командами \n{','.join (templ_comd)}"
        except Exception as e:
            return f"Ошибка: {e}"

    def template_add(self, args: list) -> str:
        """args[templ_name new_comd1 new_comd2 ...]"""
        if len(args)<2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_ADD,'Формат не найден!')}"
        templ_name=args[0]
        templ_comd=args[1:]
        try:
            skip=self._template.add_comd(templ_name,templ_comd)
            add=len(templ_comd)-len(skip)
            return f"В шаблон '{templ_name}' добавлено: {add}/{len(templ_comd)} команд \nпропущено:{','.join(skip)}"
        except Exception as e:
            return f"Ошибка: {e}"

    def template_rm(self, args: list) -> str:
        """args[templ_name index1 index2 ...]"""
        if len(args)<2:
            return f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_RM,'Формат не найден!')}"
        templ_name=args[0]
        templ_comd=args[1:]
        try:
            add=self._template.rm_comd(templ_name,templ_comd)
            if add==len(templ_comd):
                if self.check_templ(templ_name):
                    return
                return f"Из шаблона '{templ_name}' удалены команды!"
            else:
                return f"Из шаблона '{templ_name}' удалено: {add}/{len(templ_comd)} команд"
        except Exception as e:
            return f"Ошибка: {e}"

    def template_del(self, args: list) -> str:
        """args[templ_name comd]"""
        if not args:
            return f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_DEL,'Формат не найден!')}"

        try:
            self._template.delete(args[0])
            return f"Шаблон '{args[0]}' удален"
        except Exception as e:
            return f"Ошибка: {e}"

    def template_list(self, args: list) -> str:
        info =self._template.list_all_templte()
        if not info:
            return "Нет шаблонов"
        else:
            return info




# ═══════════════════════════════════════════════════════════════════════════
# ДИСПЕТЧЕР СЕРВЕРНЫХ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class ServerDispatcher:
    """Маршрутизирует ServerCmd → метод CommandHandler. Возвращает строку-результат."""

    def __init__(self, handler: CommandHandler):
        h = handler
        s = _sync_to_async
        c = ServerCmd
        self._map: Dict[ServerCmd, Callable] = {
            c.BATCH:         h.batch,
            c.CMD:           h.cmd,
            c.SIMPL:         h.simpl,
            c.EXPORT:        h.export,
            c.IMPORT:        h.import_,
            c.SAVE:          h.save,
            c.LIST:          s(h.list_users),
            c.RENAME:        s(h.rename),
            c.STATUS:        s(h.status),
            c.CANCEL:        h.cancel,
            c.KICK:          h.kick,
            c.GROUP_NEW:     s(h.group_new),
            c.GROUP_LIST:    s(h.group_list),
            c.GROUP_DEL:     s(h.group_del),
            c.GROUP_ADD:     s(h.group_add),
            c.GROUP_RM:      s(h.group_rm),
            c.CHART_NEW:     s(h.chart_new),
            c.CHART_LIST:    s(h.chart_list),
            c.CHART_DEL:     s(h.chart_del),
            c.CHART_COMD:    s(h.chart_comd),
            c.TEMPLATE_NEW:  s(h.template_new),
            c.TEMPLATE_ADD:  s(h.template_add),
            c.TEMPLATE_RM:   s(h.template_rm),
            c.TEMPLATE_DEL:  s(h.template_del),
            c.TEMPLATE_LIST: s(h.template_list)

        }

    async def dispatch(self, cmd: ServerCmd, args: list) -> str:
        fn = self._map.get(cmd)
        if fn:
            result = await fn(args)
            if result:
                Logger.log("CMD", result)
            return result or ""
        return f"Команда '{cmd}' не реализована"


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК КЛИЕНТСКОГО ПРОТОКОЛА
# ═══════════════════════════════════════════════════════════════════════════

class ProtocolHandler:
    """Логика обработки входящих сообщений от конкретного клиента."""

    def __init__(self, client_id: str, state: ServerState,
                 user_mgr: UserManager, sched_mgr: ScheduledManager,
                 monitor: CommandMonitor):
        self._cid      = client_id
        self._state    = state
        self._user_mgr = user_mgr
        self._sched    = sched_mgr
        self._monitor  = monitor
    @staticmethod
    def nl(text: str) -> str:
        """Декодирует эскейп переноса строки из протокола."""
        return text.replace("<<<NL>>>", "\n")

    def _writer(self) -> Optional[asyncio.StreamWriter]:
        return self._state.get_writer(self._cid)

    def _finish_command(self, combined: str):
        cmd_info = self._state.get_command(self._cid)
        if not cmd_info:
            return
        self._monitor.save_output(self._cid, self.nl(cmd_info["command"]), combined, cmd_info["type"])
        if self._state.has_scheduled(self._cid):
            idx = self._state.pop_scheduled(self._cid)
            self._sched.mark_done(idx, self._cid, combined)
        self._state.unregister_command(self._cid)

    async def on_output_start(self, payload: str, _reader):
        self._state.init_buffer(self._cid, "OUTPUT")
        Logger.log("OUTPUT", "начало", self._cid, show_console=False)

    async def on_output_chunk(self, payload: str, _reader):
        self._state.append_chunk(self._cid, self.nl(payload))

    async def on_output_end(self, payload: str, _reader):
        combined = self._state.flush_buffer (self._cid)
        cmd_chunks=self._state.get_buffer(self._cid)
        cmd_info = self._state.get_command (self._cid)
        cmd_str = cmd_info["command"] if cmd_info else "?"
        # Краткое резюме в лог
        Logger.log ("OUTPUT", f"[{cmd_str}] → {cmd_chunks['chunks']} чанков", self._cid)
        # Сам вывод — каждая строка отдельно чтобы читалось
        # for line in combined.splitlines ():
        #     if line.strip ():
        #         Logger.log ("",line, self._cid)
        Logger.log ("OUTPUT", combined, self._cid)
        self._finish_command (combined)

    async def on_filetru_start(self, payload: str, _reader):
        self._state.init_buffer(self._cid, "FILETRU")

    async def on_filetru_chunk(self, payload: str, _reader):
        self._state.append_chunk(self._cid, self.nl(payload))

    async def on_filetru_end(self, payload: str, _reader):
        combined = self._state.flush_buffer (self._cid)
        cmd_info = self._state.get_command (self._cid)
        if cmd_info:
            total = cmd_info.get ("total_commands", 1)
            received = cmd_info.get ("received_commands", 0) + 1
            cmd_info["received_commands"] = received
            self._monitor.save_output (self._cid, cmd_info["command"], combined, "FILETRU")
            # Показываем вывод этой команды
            Logger.log ("OUTPUT", f"[{received}/{total}] → {combined}", self._cid)
            if received >= total:
                Logger.log ("OUTPUT", f"все {total} команд выполнены", self._cid)
                idx = self._state.pop_scheduled (self._cid)
                self._sched.mark_done (idx, self._cid, combined)
                self._state.unregister_command (self._cid)


    async def on_export_start(self, payload: str, reader: asyncio.StreamReader):
        try:
            meta     = json.loads(payload)
            count    = meta.get("count", 1)
            dest_dir = Path(meta.get("dest_dir", Config.DIR_FILES))
            Logger.log("EXPORT", f"Получаю {count} файлов", self._cid)
            for _ in range(count):
                meta_line = await reader.readline()
                meta_data = json.loads(
                    meta_line.decode("utf-8", errors="ignore")
                    .strip().removeprefix("FILE:META:")
                )
                rel_path = meta_data["rel_path"]
                size     = meta_data["size"]
                dest     = dest_dir / rel_path
                await FileTransfer.receive_file(reader, dest, size)
            self._state.unregister_command(self._cid)
            if self._state.has_scheduled(self._cid):
                idx = self._state.pop_scheduled(self._cid)
                self._sched.mark_done(idx, self._cid, f"EXPORT OK {count} файлов")
            Logger.log("EXPORT", f"✓ Получено {count} файлов в {dest_dir}", self._cid)
        except Exception as e:
            Logger.log("ERROR", f"Ошибка экспорта: {e}", self._cid)

    async def on_import_complete(self, payload: str, _reader):
        cmd_info = self._state.get_command (self._cid)
        cmd_str = cmd_info["command"] if cmd_info else "?"
        Logger.log ("IMPORT", f"✓ [{cmd_str}] — клиент принял файл", self._cid)
        self._state.unregister_command (self._cid)

    async def on_import_error(self, payload: str, _reader):
        cmd_info = self._state.get_command (self._cid)
        cmd_str = cmd_info["command"] if cmd_info else "?"
        Logger.log ("ERROR", f"✗ [{cmd_str}] — ошибка на клиенте: {payload}", self._cid)
        self._state.unregister_command (self._cid)


class ProtocolDispatcher:
    """Маршрутизирует ClientMsg → метод ProtocolHandler."""

    def __init__(self, handler: ProtocolHandler):
        h = handler
        self._map: Dict[ClientMsg, Callable] = {
            ClientMsg.OUTPUT_START:    h.on_output_start,
            ClientMsg.OUTPUT_CHUNK:    h.on_output_chunk,
            ClientMsg.OUTPUT_END:      h.on_output_end,
            ClientMsg.FILETRU_START:   h.on_filetru_start,
            ClientMsg.FILETRU_CHUNK:   h.on_filetru_chunk,
            ClientMsg.FILETRU_END:     h.on_filetru_end,
            ClientMsg.EXPORT_START:    h.on_export_start,
            ClientMsg.IMPORT_COMPLETE: h.on_import_complete,
            ClientMsg.IMPORT_ERROR:    h.on_import_error,
        }

    async def dispatch(self, msg_type: ClientMsg, payload: str,
                       reader: asyncio.StreamReader):
        fn = self._map.get(msg_type)
        if fn:
            await fn(payload, reader)
