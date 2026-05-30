"""
Обработчики команд и протоколов:
  CommandHandler    — логика всех серверных команд
  ServerDispatcher  — маршрутизирует ServerCmd → CommandHandler
  ProtocolHandler   — логика клиентских протоколов (OUTPUT, FILETRU, EXPORT, IMPORT)
  ProtocolDispatcher— маршрутизирует ClientMsg → ProtocolHandler
"""

import asyncio
import time
import json
from pathlib import Path
from typing import Dict, Optional, Callable

from config import Config, ServerCmd, ClientMsg, print_help, CMD_HINTS
from managers import (
    Logger, ServerState, UserManager, GroupManager,
    ScheduledManager, FileTransfer, BanManager, CommandMonitor,
)


# ═══════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ
# ═══════════════════════════════════════════════════════════════════════════

async def _aiter(lst: list):
    for item in lst:
        yield item


def _sync_to_async(fn: Callable) -> Callable:
    """Оборачивает синхронный метод в корутину для единообразия диспетчера."""
    async def wrapper(args: list):
        fn(args)
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК СЕРВЕРНЫХ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class CommandHandler:
    """Вся бизнес-логика серверных команд."""

    def __init__(self, state: ServerState, user_mgr: UserManager,
                 group_mgr: GroupManager, sched_mgr: ScheduledManager,
                 ban_mgr: BanManager, monitor: CommandMonitor, template):
        self._state     = state
        self._user_mgr  = user_mgr
        self._group_mgr = group_mgr
        self._sched_mgr = sched_mgr
        self._ban_mgr   = ban_mgr
        self._monitor   = monitor
        self._template  = template

    # ── helpers ──────────────────────────────────────────────────────────

    def _resolve(self, name: str) -> Optional[str]:
        if name == "all":
            return name
        if name.startswith("group:"):
            group_name = name[6:]
            return group_name if self._group_mgr.get_members(group_name) is not None else None
        return self._user_mgr.validate(name)

    def _require_connected(self, target: str) -> list:
        real = self._targets(target)
        if not real:
            print(f" Пользователи не подключены")
            return []
        return real

    def _targets(self, target: str) -> list:
        """Разворачивает 'all' / group / username в список подключённых клиентов."""
        if target == "all":
            return self._state.get_all_clients()
        if target.startswith("group:"):
            group_name = target[6:]
            return self._group_mgr.get_online_user(group_name)
        real = self._user_mgr.validate(target)
        return [real] if self._state.is_connected(real) else []

    def _sub(self, text: str, cid: str) -> str:
        return self._sched_mgr.sub_path(text, cid)

    async def _drain(self, cid: str):
        writer = self._state.get_writer(cid)
        if writer:
            await writer.drain()

    async def _send_simpl(self, cid: str, commands: list, templ_name : str):
        self._state.register_command(cid, f"simpl ({len(commands)} команд) - шаблон {templ_name}", "FILETRU", len(commands))
        for cmd in commands:
            self._state.get_writer(cid).write(f"FILETRU:{self._sub(cmd, cid)}\n".encode())
            await self._drain(cid)
            await asyncio.sleep(0.2)

    def _read_code_file(self) -> list:
        try:
            # ad= []
            # with open(Config.FILE_CODE,'r',encoding="utf-8") as f:
            #     l = f.readline()
            #     if l.strip():
            #      ad.append(l)
            #
            # return ad
            return [l.strip() for l in Config.FILE_CODE.read_text("utf-8").splitlines() if l.strip()]
        except FileNotFoundError:
            print(f" Файл {Config.FILE_CODE} не найден")
            return []

    def _ask_who(self, msg) -> Optional[str]:
        if not msg:
            print(" Поле не может быть пустым")
            return False
        if msg.upper() == "EXIT":
            return "EXIT"
        if msg.startswith("group:"):
            name = msg[6:]
            if self._group_mgr.get_members(name):
                return msg
            print(f" Группа '{name}' не найдена")
            return False
        real = self._resolve(msg)
        if real:
            return real
        print(f" Пользователь '{msg}' не найден")
        return False

    def _ask_cmd(self, cmd_type: str):
        if cmd_type == ServerCmd.CMD:
            while True:
                command = input("Команда (EXIT — отмена): ").strip().lower()
                if command == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if command:
                    return {"command": command}
                print(" Команда не может быть пустой")

        elif cmd_type == ServerCmd.SIMPL:
            print("Для загрузки из code.txt оставте поле пустым (EXIT — отмена)")
            while True:
                flag=input("Введите имя шаблона:").strip().lower()

                if flag == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if not flag:
                    if not Config.FILE_CODE.exists():
                        print(f" Файл {Config.FILE_CODE} не найден")
                        return ServerCmd.EXIT
                    print (f" Будут выполнены команды из {Config.FILE_CODE}")
                    return {"template_type":"default"}
                else:
                    if self._template.check_template_name(flag):
                        print (f" Будут выполнены команды из шаблона '{flag}'")
                        return {"template_type":flag}
                    print(f"Имя шаблона '{flag}' не найдено")

        elif cmd_type == ServerCmd.IMPORT:
            print("Будьте внимательны в указании слеша!")
            while True:
                src = input("Путь на сервере (EXIT — отмена): ").strip()
                if src.upper() == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if not src:
                    print(" Путь не может быть пустым")
                    continue
                if not Path(self._sched_mgr.sub_serv_path(src)).exists():
                    print(f" Путь '{src}' не существует")
                    continue
                break
            while True:
                dst = input("Путь на клиенте (EXIT — отмена): ").strip()
                if dst.upper() == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if dst:
                    return {"source_path": src, "dest_path": dst}
                print(" Путь не может быть пустым")

        elif cmd_type == ServerCmd.EXPORT:
            while True:
                src = input("Путь на клиенте (EXIT — отмена): ").strip()
                if src.upper() == ServerCmd.EXIT:
                    return ServerCmd.EXIT
                if src:
                    break
                print(" Путь не может быть пустым")
            dst = input("Путь на сервере [не обязателен]: ").strip()
            if dst.upper() == ServerCmd.EXIT:
                return ServerCmd.EXIT
            return {"source_path": src, "dest_path": dst or "received"}

        return ServerCmd.EXIT

    def check_templ(self, name) -> bool:
        """Возвращает True если в шаблоне нету команд. Удаляет шаблон"""
        if len (self._template.get_comd_template_name (name)) <= 0:
            self._template.delete (name)
            Logger.log ("CMD", f"Шаблон '{name}' удален. Шаблон пуст. ")
            return True
        return False

    def check_group(self, name) ->bool:
        """Возвращает True если в группе нету пользователей. Удаляет группу"""
        if len(self._group_mgr.get_members(name)) <= 0:
            self._group_mgr.delete(name)
            Logger.log ("CMD", f"Группа '{name}' удалена. Группа пуста. ")
            return True
        return False



    # ── основные команды ──────────────────────────────────────────────────

    async def cmd(self, args: list):
        if len(args) < 2:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.CMD,'Формат не найден!')}")
            return
        target, command = args[0], " ".join(args[1:])
        if not self._resolve(target):
            print("Пользователь не найден")
            return
        user = self._require_connected(target)
        if user:
            for cid in user:
                c = self._sub(command, cid)
                self._state.register_command(cid, c, "CMD", 1)
                self._state.get_writer(cid).write(f"CMD:{c}\n".encode())
                await self._drain(cid)
            print(f" CMD → {target}")

    async def simpl(self, args: list):
        if len(args) < 1:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.SIMPL,'Формат не найден!')}")
            return
        if not self._require_connected(args[0]):
            print("Пользователь не найден")
            return

        # Если передан второй аргумент — имя шаблона
        if len(args) > 1:
            templ_name = args[1]
            commands = self._template.get_comd_template_name(templ_name)
            if not commands:
                print(f" Шаблон '{templ_name}' не найден или пуст")
                return
        else:
            commands = self._read_code_file()
            if not commands:
                return

        user = self._require_connected(args[0])
        if user:
            for cid in user:
                await self._send_simpl(cid, commands,args[1] if args[1] else "default")
                print(f" {len(commands)} команд → {cid}")

    async def export(self, args: list):
        if len(args) < 2:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.EXPORT,'Формат не найден!')}")
            return
        user = self._require_connected(args[0])
        if user:
            for cid in user:
                src = self._sub(args[1], cid)
                dst = args[2] if len(args) > 2 else "received"
                self._state.register_command(cid, f"export {src}", "EXPORT", 1)
                self._state.get_writer(cid).write(f"EXPORT;{src};{dst}\n".encode())
                await self._drain(cid)
                print(f" Запрос экспорта → {cid}")

    async def import_(self, args: list):
        if len(args) < 2:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.IMPORT,'Формат не найден!')}")
            return
        target = args[0]
        src    = self._sched_mgr.sub_serv_path(args[1])
        dst    = args[2] if len(args) > 2 else "received"
        sent   = 0
        for cid in self._targets(target):
            d = self._sub(dst, cid)
            self._state.register_command(cid, f"import {src}", "IMPORT", 1)
            await FileTransfer.send_to_client(cid, src, d, self._state)
            self._state.unregister_command(cid)
            sent += 1
        print(f" Отправлено {sent} клиентам")

    async def save(self, args: list):
        if len(args) < 2:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.SAVE,'Формат не найден!')}")
            return
        target, filename = args[0], args[1]
        now     = time.strftime("%Y-%m-%d %H:%M:%S")
        targets = self._state.get_all_clients() if target == "all" else [target]
        saved   = []

        for cid in targets:
            real = self._resolve(cid) or cid
            out  = self._state.get_last_output(real)
            if not out or not out.get("content"):
                print(f" Нет данных от {real}")
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
                print(f" Ошибка: {e}")

        if target == "all":
            print(f" Сохранено {len(saved)}/{len(targets)}")
        elif saved:
            print(f" → {Config.DIR_SAVE / (filename + '.txt')}")

    def list_users(self, args: list):
        users = self._user_mgr.get_users_data()
        if not users:
            print(" Нет пользователей")
            return
        print(f"\n{'=' * 134}")
        print(f"{'№':<4} {'Username':<20} {'Alias':<20} {'Status':<8} {'OS':<8} {'Path':<25} {'Time':<20} {'Group':<25}")
        print(f"{'=' * 134}")
        for i, (uname, info) in enumerate(users.items(), 1):
            t = info["last_login"] if info["status"] == "ON" else info["last_logout"]
            print(f"{i:<4} {uname:<20} {info['alias']:<20} {info['status']:<8} "
                  f"{info['OS']:<8} {info['default_path']:<25} {t or '':<20} {','.join(info['users_in_group']):<25}")
        online = sum(1 for u in users.values() if u["status"] == "ON")
        print(f"{'=' * 134}\nВсего: {len(users)} | Онлайн: {online}\n")

    def rename(self, args: list):
        if len(args) < 2:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.RENAME,'Формат не найден!')}")
            return
        target, new_alias = args[0], args[1][:10].replace(" ", "_")
        found = self._resolve(target)
        if not found:
            print(f" '{target}' не найден")
            return
        users = self._user_mgr.get_users_data()
        for uname, info in users.items():
            if info["alias"] == new_alias and uname != found:
                print(f" Alias '{new_alias}' уже занят")
                return
        old = users[found]["alias"]
        users[found]["alias"] = new_alias
        self._user_mgr.save_user_data(users)
        print(f" {found}: '{old}' → '{new_alias}'")

    def status(self, args: list):
        cmds = self._state.get_all_commands()
        if not cmds:
            print(" Нет активных команд")
            return
        print(f"\n{'=' * 80}\nАКТИВНЫЕ КОМАНДЫ\n{'=' * 80}")
        for cid, info in cmds.items():
            elapsed = time.time() - info["start_time"]
            print(f"  {cid}: {info['type']} ({elapsed:.1f}s) — {info['command']}")
        print(f"{'=' * 80}\n")

    async def cancel(self, args: list):
        if len(args) < 1:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.CANCEL,'Формат не найден!')}")
            return
        target = args[0]
        if self._state.has_command(target):
            writer = self._state.get_writer(target)
            if writer:
                writer.write(b"CMD:CANCEL_MANUAL\n")
                await self._drain(target)
            self._state.unregister_command(target)
            print(f" Отменено для {target}")
        else:
            print(f" У {target} нет активных команд")

    async def kick(self, args: list):
        if len(args) < 1:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.KICK,'Формат не найден!')}")
            return
        target = args[0]
        if target == "all":
            n = sum([
                1 async for cid in _aiter(self._state.get_all_clients())
                if await self._ban_mgr.kick(cid, "Отключены администратором")
            ])
            print(f" Отключено: {n}")
        else:
            real = self._resolve(target) or target
            if self._state.is_connected(real):
                await self._ban_mgr.kick(real)
                print(f" {real} отключен")
            else:
                print(f" {real} не подключен")

    # ── группы ───────────────────────────────────────────────────────────

    def group_new(self, args: list):
        if len(args) < 1:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.GROUP_NEW,'Формат не найден!')}")
            return
        name = args[0]
        if self._group_mgr.get_members(name) is not None:
            print(f" Группа '{name}' уже существует")
            return
        members = self._collect_users("добавления")
        if members:
            try:
                self._group_mgr.create(name, members)
                print(f" Группа '{name}' создана ({len(members)} уч.)")
            except Exception as e:
                print(f" Ошибка: {e}")
        else:
            print(" Группа не создана")

    def group_list(self, args: list):
        groups = self._group_mgr.get_data()
        if not groups:
            print(" Нет групп")
            return
        print(f"\n{'=' * 60}\nГРУППЫ\n{'=' * 60}")
        for name, members in groups.items():
            print(f"  {name} ({len(members)} уч.):")
            for m in members:
                print(f"    - {m}")
        print(f"{'=' * 60}\n")

    def group_del(self, args: list):
        if len(args) < 1:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.GROUP_DEL,'Формат не найден!')}")
            return
        try:
            self._group_mgr.delete(args[0])
            print(f" Группа '{args[0]}' удалена")
        except Exception as e:
            print(f" Ошибка: {e}")

    def group_add(self, args: list):
        self._group_modify(args, add=True)

    def group_rm(self, args: list):
        self._group_modify(args, add=False)

    def _group_modify(self, args: list, add: bool):
        if len(args) < 1:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.GROUP_ADD if add else ServerCmd.GROUP_RM,'Формат не найден!')}")
            return
        name = args[0]
        if self._group_mgr.get_members(name) is None:
            print(f" Группа '{name}' не найдена")
            return
        verb    = "добавления" if add else "удаления"
        users   = self._collect_users(verb)
        if not users:
            return
        fn      = self._group_mgr.add_users if add else self._group_mgr.remove_users
        skipped = fn(name, users)
        if self.check_group(name):
            return
        done    = len(users) - len(skipped)
        action  = "Добавлено" if add else "Удалено"
        print(f" {action}: {done}. Пропущено: {skipped}")

    def _collect_users(self, verb: str) -> list:
        """Интерактивный сбор пользователей из stdin."""
        print(f"Введите пользователей для {verb} (EXIT — завершить):")
        users = []
        while True:
            u = input("  > ").strip()
            if u == ServerCmd.EXIT.upper():
                break
            real = self._resolve(u)
            if real and real not in users:
                users.append(real)
            else:
                print(f" '{u}' не найден или уже в списке")
        return users
    @staticmethod
    def _collect_command(name):
        print (f"Введите команды для шаблона {name} (EXIT — завершить):")
        command= []
        while True:
            c= input(" > ").strip()
            if c == ServerCmd.EXIT.upper():
                break
            if c not in command:
                command.append(c)
            else:
                print(f" Команда '{c}' уже в списке")
        return command


    # ── отложенные команды ────────────────────────────────────────────────

    def chart_new(self, args: list):
        print("\n" + "=" * 60 + "\nСОЗДАНИЕ ОТЛОЖЕННОЙ КОМАНДЫ\n" + "=" * 60)
        valid_types = [ServerCmd.CMD, ServerCmd.SIMPL, ServerCmd.IMPORT, ServerCmd.EXPORT]

        while True:
            raw    = input("Цель (all / username / group:name / EXIT): ").strip()
            target = self._ask_who(raw)
            if target == ServerCmd.EXIT:
                print(" Отмена")
                break
            if not target:
                continue

            while True:
                cmd_type = input("Тип (CMD/SIMPL/IMPORT/EXPORT/EXIT): ").strip().lower()
                if cmd_type == ServerCmd.EXIT:
                    print(" Отмена")
                    break
                if cmd_type in [ServerCmd.CMD, ServerCmd.SIMPL, ServerCmd.IMPORT, ServerCmd.EXPORT]:
                    data_cmd = self._ask_cmd(cmd_type)
                    if data_cmd == ServerCmd.EXIT:
                        print(" Отмена отложенной команды")
                        break
                    try:
                        self._sched_mgr.create(target, cmd_type, data_cmd)
                        print(f" Добавлено для '{target}'")
                    except Exception as e:
                        print(f" Ошибка: {e}")
                    break
                print(f" Неверный тип '{cmd_type}'. Допустимые: {','.join(valid_types)}")
            break



    def chart_list(self, args: list):
        cmds = self._sched_mgr.get_all()
        if not cmds:
            print(" Нет отложенных команд")
            return
        print(f"\n{'=' * 60}\nОТЛОЖЕННЫЕ КОМАНДЫ\n{'=' * 60}")
        for i, cmd in enumerate(cmds):
            ctype = cmd["command_type"]
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            print(f"[{i}] {cmd['target']} → {ctype}: {label}")
            if n := len(cmd.get("completed_users", [])):
                print(f"    ✓ Выполнено: {n}")
            if n := len(cmd.get("expected_users", [])):
                print(f"    ⏳ Ожидает: {n}")
        print(f"{'=' * 60}\n")

    def chart_del(self, args: list):
        if len(args) < 1:
            print(f"Формат: {CMD_HINTS.get(ServerCmd.CHART_DEL,'Формат не найден!')}")
            return
        try:
            self._sched_mgr.delete(int(args[0]))
            print(f" Команда [{args[0]}] удалена")
        except Exception as e:
            print(f" Ошибка: {e}")

    def chart_comd(self, args: list = None):
        data      = self._sched_mgr.get_data()
        completed = data.get("completed", [])
        active    = data.get("commands", [])

        print(f"\n{'=' * 70}\nВЫПОЛНЕННЫЕ: {len(completed)}\n{'=' * 70}")
        for i, cmd in enumerate(completed):
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            print(f"\n[{i}] {cmd['target']} → {cmd['command_type']}: {label} ({cmd.get('completed_at','?')}) ✓")
            for u in cmd.get("completed_users", []):
                print(f"    ├─ {u} ✓")

        print(f"\n{'=' * 70}\nВ ПРОЦЕССЕ\n{'=' * 70}")
        for i, cmd in enumerate(active):
            cu    = cmd.get("completed_users", [])
            eu    = cmd.get("expected_users",  [])
            label = cmd.get("command", cmd.get("source_path", "code.txt"))
            print(f"\n[{i}] {cmd['target']} → {cmd['command_type']}: {label}")
            for u in cu:
                print(f"    ├─ {u} ✓")
            for e in eu:
                print(f"    ├─ {e} ")
        print(f"{'=' * 70}\n")


    # ── шаблоны команд ────────────────────────────────────────────────

    def template_new(self, args: list) -> str:
        """args[templ_name]"""
        if not args:
            print( f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_NEW,'Формат не найден!')}")
            return
        templ_name=args[0]
        if templ_name in self._template.get_all_template_name():
            print(f"Имя '{templ_name}' уже используется")
            return
        try:
            templ_comd = self._collect_command(templ_name)
            self._template.create (templ_name, templ_comd)
            print(f"Шаблон '{templ_name}' создан с {len(templ_comd)} командами\n{', '.join(templ_comd)}")
        except Exception as e:
            print (f"Ошибка: {e}")

    def template_add(self, args: list) -> str:
        """args[templ_name new_comd1 new_comd2 ...]"""
        if len(args)<2:
            print( f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_ADD,'Формат не найден!')}")
            return
        templ_name=args[0]
        templ_comd=args[1:]
        try:
            skip=self._template.add_comd(templ_name,templ_comd)
            add=len(templ_comd)-len(skip)
            print (f"В шаблон '{templ_name}' добавлено: {add}/{len(templ_comd)} команд \nпропущено:{','.join(skip)}")
        except Exception as e:
            print (f"Ошибка: {e}")

    def template_rm(self, args: list) -> str:
        """args[templ_name index1 index2 ...]"""
        if len(args)<2:
            print (f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_RM,'Формат не найден!')}")
            return
        templ_name=args[0]
        templ_comd=args[1:]
        try:
            add=self._template.rm_comd(templ_name,templ_comd)
            if add == len(templ_comd):
                if self.check_templ(templ_name):
                    return
                print (f"Из шаблона '{templ_name}' удалены команды!")
            else:
                print (f"Из шаблона '{templ_name}' удалено: {add}/{len(templ_comd)} команд")
        except Exception as e:
            print (f"Ошибка: {e}")

    def template_del(self, args: list) -> str:
        """args[templ_name comd]"""
        if not args:
            print (f"Формат: {CMD_HINTS.get(ServerCmd.TEMPLATE_DEL,'Формат не найден!')}")
            return
        try:
            self._template.delete(args[0])
            print (f"Шаблон '{args[0]}' удален")
        except Exception as e:
            print (f"Ошибка: {e}")

    def template_list(self, args: list) -> str:
        info = self._template.list_all_templates()
        if not info:
            print("Нет шаблонов")
        else:
            print(info)


# ═══════════════════════════════════════════════════════════════════════════
# ДИСПЕТЧЕР СЕРВЕРНЫХ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class ServerDispatcher:
    """Маршрутизирует ServerCmd → метод CommandHandler."""

    def __init__(self, handler: CommandHandler):
        h = handler
        s = _sync_to_async
        c=ServerCmd
        self._map: Dict[ServerCmd, Callable] = {
            c.CMD:        h.cmd,
            c.SIMPL:      h.simpl,
            c.EXPORT:     h.export,
            c.IMPORT:     h.import_,
            c.SAVE:       h.save,
            c.LIST:       s(h.list_users),
            c.RENAME:     s(h.rename),
            c.STATUS:     s(h.status),
            c.CANCEL:     h.cancel,
            c.KICK:       h.kick,
            c.HELP:       s(lambda _: print_help()),
            c.GROUP_NEW:  s(h.group_new),
            c.GROUP_LIST: s(h.group_list),
            c.GROUP_DEL:  s(h.group_del),
            c.GROUP_ADD:  s(h.group_add),
            c.GROUP_RM:   s(h.group_rm),
            c.CHART_NEW:  s(h.chart_new),
            c.CHART_LIST: s(h.chart_list),
            c.CHART_DEL:  s(h.chart_del),
            c.CHART_COMD: s(h.chart_comd),
            c.TEMPLATE_NEW: s (h.template_new),
            c.TEMPLATE_ADD: s (h.template_add),
            c.TEMPLATE_RM: s (h.template_rm),
            c.TEMPLATE_DEL: s (h.template_del),
            c.TEMPLATE_LIST: s (h.template_list)
        }

    async def dispatch(self, cmd: ServerCmd, args: list):
        fn = self._map.get(cmd)
        if fn:
            await fn(args)
        else:
            print(f" Команда '{cmd}' не реализована")


# ═══════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК КЛИЕНТСКОГО ПРОТОКОЛА
# ═══════════════════════════════════════════════════════════════════════════

class ProtocolHandler:
    """Вся логика обработки входящих сообщений от конкретного клиента."""

    def __init__(self, client_id: str, state: ServerState,
                 user_mgr: UserManager, sched_mgr: ScheduledManager,
                 monitor: CommandMonitor):
        self._cid      = client_id
        self._state    = state
        self._user_mgr = user_mgr
        self._sched    = sched_mgr
        self._monitor  = monitor

    # ── внутренние helpers ───────────────────────────────────────────────

    def _writer(self) -> Optional[asyncio.StreamWriter]:
        return self._state.get_writer(self._cid)

    def _finish_command(self, combined: str):
        """Сохраняет вывод, помечает отложенную команду и снимает регистрацию."""
        cmd_info = self._state.get_command(self._cid)
        if not cmd_info:
            return
        self._monitor.save_output(self._cid, cmd_info["command"], combined, cmd_info["type"])
        if self._state.has_scheduled(self._cid):
            idx = self._state.pop_scheduled(self._cid)
            self._sched.mark_done(idx, self._cid, combined)
        self._state.unregister_command(self._cid)
        self._state.clear_buffer(self._cid)

    def _accumulate(self, chunk: str) -> Optional[str]:
        """
        Накапливает вывод в cmd_info.
        Возвращает объединённый результат когда все чанки получены,
        иначе None (для SIMPL — очищает lines для следующей итерации).
        """
        cmd_info = self._state.get_command(self._cid)
        if not cmd_info:
            return None
        cmd_info["accumulated_output"].append(chunk)
        cmd_info["received_commands"] += 1
        if cmd_info["received_commands"] >= cmd_info["total_commands"]:
            return "\n\n".join(cmd_info["accumulated_output"])
        buf = self._state.get_buffer(self._cid)
        if buf:
            buf["lines"] = []
        return None

    def _print_output(self, label: str, text: str, comd="команда не указана'"):
        print(f"\n{'=' * 80}\n[{label} от {self._cid} -> {comd}]\n{'=' * 80}\n{text}\n{'=' * 80}\n")

    # ── OUTPUT ───────────────────────────────────────────────────────────

    async def on_output_start(self, payload: str):
        self._state.init_buffer(self._cid, "OUTPUT", int(payload) if payload.isdigit() else 0)

    def on_output_chunk(self, payload: str):
        self._state.append_chunk(self._cid, payload.replace("<<<NL>>>", "\n"))

    def on_output_end(self, _: str = ""):
        output   = self._state.flush_buffer(self._cid)
        cmd_info = self._state.get_command(self._cid)
        self._print_output("OUTPUT", output,cmd_info['command'])
        combined = self._accumulate(output)
        if combined is not None:
            self._finish_command(combined)

    # ── FILETRU ──────────────────────────────────────────────────────────

    async def on_filetru_start(self, payload: str):
        self._state.init_buffer(self._cid, "FILETRU", int(payload) if payload.isdigit() else 0)

    def on_filetru_chunk(self, payload: str):
        self._state.append_chunk(self._cid, payload.replace("<<<NL>>>", "\n"))

    def on_filetru_end(self, _: str = ""):
        output   = self._state.flush_buffer(self._cid)
        cmd_info = self._state.get_command(self._cid)
        if cmd_info:
            cmd_info["accumulated_output"].append(output)
            cmd_info["accumulated_output"].append("-"*40)

            cmd_info["received_commands"] += 1
            Logger.log("DEBUG",
                       f"FILETRU: {cmd_info['received_commands']}/{cmd_info['total_commands']}",
                       self._cid)

            Logger.log("DEBUG",f"FILETRU:{cmd_info['accumulated_output']} >{cmd_info['received_commands']}")
            if cmd_info["received_commands"] >= cmd_info["total_commands"]:
                combined = "\n\n".join(cmd_info["accumulated_output"])
                self._print_output("FILETRU", combined, cmd_info['command'])
                self._finish_command(combined)
        self._state.clear_buffer(self._cid)

    # ── EXPORT ───────────────────────────────────────────────────────────

    async def on_export_start(self, payload: str, reader: asyncio.StreamReader):
        try:
            meta     = json.loads(payload)
            count    = meta["count"]
            dest_dir = meta.get("dest_dir", "received")
            save_dir = Path(Config.DIR_FILES) / self._cid / dest_dir
            writer   = self._writer()

            print(f" Получение {count} файлов от {self._cid} → {save_dir}")

            for _ in range(count):
                meta_line = await asyncio.wait_for(reader.readline(), timeout=30)
                meta_str  = meta_line.decode("utf-8", errors="?").strip()
                if not meta_str.startswith("FILE:META:"):
                    break
                file_meta = json.loads(meta_str[10:])
                save_path = save_dir / file_meta["rel_path"]
                if not await FileTransfer.receive_file(reader, save_path, file_meta["size"]):
                    if writer:
                        writer.write(b"EXPORT:ABORT\n")
                        await writer.drain()
                    break

            confirm = await asyncio.wait_for(reader.readline(), timeout=10)
            if confirm.decode("utf-8", errors="ignore").strip() == "EXPORT:COMPLETE":
                Logger.log("EXPORT", "✓ Завершён", self._cid)
                if self._state.has_scheduled(self._cid):
                    idx = self._state.pop_scheduled(self._cid)
                    self._sched.mark_done(idx, self._cid, f"EXPORT: {count} файлов [OK]")
                self._state.unregister_command(self._cid)

        except Exception as e:
            Logger.log("ERROR", f"Ошибка EXPORT: {e}", self._cid)

    # ── IMPORT ───────────────────────────────────────────────────────────

    def on_import_complete(self, _: str = ""):
        Logger.log("IMPORT", "✓ Завершён", self._cid)
        self._state.unregister_command(self._cid)

    def on_import_error(self, payload: str):
        Logger.log("ERROR", f"Импорт: {payload}", self._cid)
        self._state.unregister_command(self._cid)


# ═══════════════════════════════════════════════════════════════════════════
# ДИСПЕТЧЕР КЛИЕНТСКОГО ПРОТОКОЛА
# ═══════════════════════════════════════════════════════════════════════════

class ProtocolDispatcher:
    """
    Маршрутизирует ClientMsg → метод ProtocolHandler.
    Единая точка входа: dispatch(msg_type, payload, reader).
    """

    def __init__(self, handler: ProtocolHandler):
        h = handler

        self._async_handlers: Dict[ClientMsg, Callable] = {
            ClientMsg.OUTPUT_START:  h.on_output_start,
            ClientMsg.FILETRU_START: h.on_filetru_start,
        }

        self._sync_handlers: Dict[ClientMsg, Callable] = {
            ClientMsg.OUTPUT_CHUNK:    h.on_output_chunk,
            ClientMsg.OUTPUT_END:      h.on_output_end,
            ClientMsg.FILETRU_CHUNK:   h.on_filetru_chunk,
            ClientMsg.FILETRU_END:     h.on_filetru_end,
            ClientMsg.IMPORT_COMPLETE: h.on_import_complete,
            ClientMsg.IMPORT_ERROR:    h.on_import_error,
        }

        self._handler = h

    async def dispatch(self, msg_type: ClientMsg, payload: str,
                       reader: asyncio.StreamReader):
        if msg_type == ClientMsg.EXPORT_START:
            await self._handler.on_export_start(payload, reader)

        elif msg_type in self._async_handlers:
            await self._async_handlers[msg_type](payload)

        elif msg_type in self._sync_handlers:
            self._sync_handlers[msg_type](payload)

        else:
            Logger.log("WARNING", f"Нет обработчика для {msg_type}", show_console=False)
