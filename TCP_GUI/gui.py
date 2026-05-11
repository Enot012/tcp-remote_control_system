"""
gui.py — графический интерфейс администратора (tkinter).

Назначение:
    Главное окно сервера. Запускает сервер в фоновом потоке и предоставляет
    визуальный интерфейс для отправки команд, мониторинга подключений и лога.

Структура классов:

    OnlineDialog      — модальный диалог немедленной команды.
                        Флаги: CMD | SIMPL | EXPORT | IMPORT.
                        Поля динамически меняются под выбранный тип.
                        Получает список пользователей/групп из ServerState/GroupManager.
                        Отправляет команду через ServerApp.dispatch().

    ScheduledDialog   — модальный диалог отложенной команды.
                        Те же флаги + выбор цели (user/all/group).
                        Вызывает chart_new через ServerApp.dispatch().

    GroupDialog       — диалог управления группами (три вкладки):
                        «Создать» — group_new,
                        «Изменить» — group_add + group_rm,
                        «Удалить» — group_del.
                        Отображает теги-пилюли участников с кнопкой «×».

    CommandBar        — нижняя левая панель:
                        кнопки [Онлайн] [Отложенная] [Группы],
                        поле ручного ввода команды + кнопка «Отправить».
                        Строка подсказки обновляется при выборе команды из списка.

    UsersPanel        — правая панель:
                        Listbox онлайн-пользователей (обновляется каждые 2 с),
                        Listbox команд с подсказками (клик → автодополнение).

    ServerApp         — главное окно (1050×680):
                        • запускает srv.run_server() в daemon-потоке,
                        • регистрирует _on_log как callback для Logger,
                        • dispatch(cmd, args) — мост в asyncio через
                          asyncio.run_coroutine_threadsafe(),
                        • dispatch_raw(raw) — парсит строку ручного ввода,
                          для сложных команд открывает соответствующий диалог,
                        • _tick() — каждые 2 с обновляет UsersPanel и статус-лейбл,
                        • _on_close() — graceful shutdown + destroy окна.

Связь с asyncio:
    GUI (главный поток) ↔ asyncio (фоновый поток):
    • GUI → asyncio: asyncio.run_coroutine_threadsafe(coro, loop)
    • asyncio → GUI: Logger вызывает _log_callback, тот — root.after(0, …)

Импортирует: config.py, managers.py (set_log_callback), server.py.
Точка входа: if __name__ == "__main__": ServerApp().run()
"""
import asyncio
import threading
import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk

import json
import server as srv
from config import (Config,ServerCmd, COLORS, CMD_HINTS,
                    FONT_MONO_S, FONT_MONO, FONT_UI, FONT_UI_B, FONT_SMALL, ServerCmdParser)
from managers import Logger, set_log_callback


# ═══════════════════════════════════════════════════════════════════════════
# ДИАЛОГ — ОНЛАЙН КОМАНДА
# ═══════════════════════════════════════════════════════════════════════════

class OnlineDialog(tk.Toplevel):
    """
    Кнопка «Онлайн» в CommandBar.
    Флаги: CMD | SIMPL | EXPORT | IMPORT
    Поля меняются в зависимости от выбранного флага.
    """
    FLAGS = [ServerCmd.CMD, ServerCmd.BATCH, ServerCmd.SIMPL, ServerCmd.EXPORT, ServerCmd.IMPORT]
    def __init__(self, parent, app: "ServerApp"):
        super().__init__(parent)
        self._app = app
        self.title("Онлайн команда")
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)
        self.grab_set()

        self._flag = tk.StringVar(value=ServerCmd.CMD)
        self._build()
        self._on_flag()
        self._center(parent)

    def _center(self, parent):
        """Центрирует диалог относительно родительского окна."""
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _label(self, parent, text):
        """Создаёт стилизованный Label."""
        return tk.Label(parent, text=text, bg=COLORS["bg"],
                        fg=COLORS["text_dim"], font=FONT_SMALL)

    def _entry(self, parent, **kw):
        """Создаёт стилизованный Entry."""
        return tk.Entry(parent, bg=COLORS["input_bg"], fg=COLORS["text"],
                        insertbackground=COLORS["text"], relief="flat",
                        font=FONT_MONO_S, **kw)

    def _build(self):
        """Строит всё содержимое диалога."""
        pad = dict(padx=14, pady=6)

        # ── флаги ────────────────────────────────────────────────────────
        flag_frame = tk.Frame(self, bg=COLORS["bg"])
        flag_frame.pack(fill="x", **pad)
        tk.Label(flag_frame, text="Тип:", bg=COLORS["bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL).pack(side="left", padx=(0, 8))
        self._flag_btns = {}
        for f in self.FLAGS:
            btn = tk.Button(
                flag_frame, text=f, font=FONT_SMALL, relief="flat", bd=0,
                padx=10, pady=3, cursor="hand2",
                command=lambda x=f: self._select_flag(x),
            )
            btn.pack(side="left", padx=2)
            self._flag_btns[f] = btn

        # ── цель (target) ─────────────────────────────────────────────────
        tf = tk.Frame(self, bg=COLORS["bg"])
        tf.pack(fill="x", **pad)
        self._label(tf, "Цель (user / all / group:name)").pack(anchor="w")
        self._target_var = tk.StringVar()
        self._target_combo = ttk.Combobox(
            tf, textvariable=self._target_var, font=FONT_MONO_S, width=35,
        )
        self._target_combo.pack(fill="x", pady=(2, 0))
        self._refresh_targets()

        # ── динамические поля ──────────────────────────────────────────────
        self._fields_frame = tk.Frame(self, bg=COLORS["bg"])
        self._fields_frame.pack(fill="x", padx=14)

        self._cmd_frame    = self._build_cmd_fields()
        self._batch_frame = self._build_batch_fields ()
        self._simpl_frame  = self._build_simpl_fields()
        self._export_frame = self._build_path_fields("Путь на клиенте", "Путь на сервере (необяз.)")
        self._import_frame = self._build_path_fields("Путь на сервере", "Путь на клиенте (необяз.)")

        # ── кнопки ────────────────────────────────────────────────────────
        btn_frame = tk.Frame(self, bg=COLORS["bg"])
        btn_frame.pack(fill="x", padx=14, pady=12)
        tk.Button(btn_frame, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(btn_frame, text="▶  Отправить", font=FONT_UI_B,
                  bg=COLORS["accent"], fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self._submit).pack(side="right")

        self._bind_enter(self._cmd_entry)

    def _build_cmd_fields(self):
        """Строит поля для флага CMD."""
        f = tk.Frame(self._fields_frame, bg=COLORS["bg"])
        self._label(f, "Команда").pack(anchor="w", pady=(6, 1))
        self._cmd_entry = self._entry(f, width=46)
        self._cmd_entry.pack(fill="x")
        return f

    def _build_batch_fields(self):
        """Строит поле для флага BATCH — многострочный Text, каждая строка = команда."""
        f = tk.Frame (self._fields_frame, bg=COLORS["bg"])
        self._label (f, "Команды (каждая с новой строки)").pack (anchor="w", pady=(6, 1))
        self._batch_text = tk.Text (
            f, bg=COLORS["input_bg"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief="flat",
            font=FONT_MONO_S, width=46, height=8,
            wrap="none",
        )
        self._batch_text.pack (fill="x")
        # скроллбар
        sb = tk.Scrollbar (f, command=self._batch_text.yview)
        sb.pack (side="right", fill="y")
        self._batch_text.configure (yscrollcommand=sb.set)
        return f

    def _build_simpl_fields(self):
        """Строит поля для флага SIMPL — инфо + список доступных шаблонов."""
        f = tk.Frame (self._fields_frame, bg=COLORS["bg"])
        tk.Label (f, text="Выполнит все строки из code.txt",
                  bg=COLORS["bg"], fg=COLORS["text_dim"],
                  font=FONT_SMALL, pady=6).pack ()

        self._label (f, "Или выберите шаблон (необязательно)").pack (anchor="w", pady=(4, 1))
        self._simpl_template_var = tk.StringVar ()
        self._simpl_template_cb = ttk.Combobox (
            f, textvariable=self._simpl_template_var,
            font=FONT_MONO_S, width=35, state="readonly",
        )

        self._simpl_template_cb.pack (fill="x", pady=(0, 4))
        self._refresh_templates ()
        self._simpl_template_cb.bind ("<Button-1>", lambda e: self._refresh_templates ())
        return f

    def _build_path_fields(self, label1: str, label2: str):
        """Строит два поля пути (используется для EXPORT и IMPORT)."""
        f  = tk.Frame(self._fields_frame, bg=COLORS["bg"])
        self._label(f, label1).pack(anchor="w", pady=(6, 1))
        e1 = self._entry(f, width=46)
        e1.pack(fill="x")
        self._label(f, label2).pack(anchor="w", pady=(6, 1))
        e2 = self._entry(f, width=46)
        e2.pack(fill="x")
        if not hasattr(self, "_path_entries"):
            self._path_entries = {}
        key = label1[:3]
        self._path_entries[key] = (e1, e2)
        return f

    def _select_flag(self, flag: ServerCmd):
        """Устанавливает активный флаг и обновляет UI."""
        self._flag.set(flag)
        self._on_flag()

    def _on_flag(self):
        """Перерисовывает кнопки флагов и показывает нужный фрейм полей."""
        flag = self._flag.get()
        for f, btn in self._flag_btns.items():
            c = COLORS[f]
            if f == flag:
                btn.configure(bg=c, fg="white")
            else:
                btn.configure(bg=COLORS["panel"], fg=COLORS["text_dim"])

        for frame in (self._cmd_frame, self._batch_frame, self._simpl_frame,
                      self._export_frame, self._import_frame):
            frame.pack_forget ()
        {
            ServerCmd.CMD: self._cmd_frame,
            ServerCmd.BATCH: self._batch_frame,
            ServerCmd.SIMPL: self._simpl_frame,
            ServerCmd.EXPORT: self._export_frame,
            ServerCmd.IMPORT: self._import_frame,
        }[flag].pack (fill="x")

    def _refresh_targets(self):
        """Обновляет список целей из текущего состояния сервера."""
        state = srv.get_state()
        choices = ["all"]
        if state:
            choices += state.get_all_clients()

        self._target_combo["values"] = choices

    def _refresh_templates(self):
        """Загружает список шаблонов из TemplateManager."""
        tmgr = srv.get_template_mgr ()  # нужно добавить геттер в server.py
        names = ["default"] + tmgr.get_all_template_name () if tmgr else [""]

        self._simpl_template_cb["values"] = names

    def _bind_enter(self, *widgets):
        """Привязывает Enter → _submit ко всем переданным виджетам и самому окну."""
        for w in widgets:
            w.bind("<Return>", lambda e: self._submit())
        self.bind("<Return>", lambda e: self._submit())

    def _submit(self):
        """Валидирует поля и отправляет команду через app.dispatch."""
        flag   = self._flag.get().lower()
        target = self._target_var.get().strip()
        if not target:
            messagebox.showerror("Ошибка", "Укажите цель", parent=self)
            return

        if flag == ServerCmd.CMD:
            cmd = self._cmd_entry.get().strip()
            if not cmd:
                messagebox.showerror("Ошибка", "Введите команду", parent=self)
                return
            args = [target] + cmd.split()
            self._app.dispatch(ServerCmd.CMD, args)

        elif flag == ServerCmd.BATCH:
            raw = self._batch_text.get ("1.0", "end").strip ()
            if not raw:
                messagebox.showerror ("Ошибка", "Введите хотя бы одну команду", parent=self)
                return
            commands = [line.strip () for line in raw.splitlines () if line.strip ()]
            self._app.dispatch (ServerCmd.BATCH, [target] + commands)

        elif flag == ServerCmd.SIMPL:
            # self._app.dispatch(ServerCmd.SIMPL, [target])
            template = self._simpl_template_var.get ().strip ()
            args = [target] + ([template] if template else [])
            self._app.dispatch (ServerCmd.SIMPL, args)

        elif flag == ServerCmd.EXPORT:
            e1, e2 = self._path_entries["Пут"]
            src = e1.get().strip()
            dst = e2.get().strip()
            if not src:
                messagebox.showerror("Ошибка", "Укажите путь клиента", parent=self)
                return
            args = [target, src] + ([dst] if dst else [])
            self._app.dispatch(ServerCmd.EXPORT, args)

        elif flag == ServerCmd.IMPORT:
            e1, e2 = self._path_entries.get("Пут", (None, None))
            entries = list(self._path_entries.values())
            if len(entries) >= 2:
                e1, e2 = entries[1]
            else:
                e1, e2 = entries[0]
            src = e1.get().strip()
            dst = e2.get().strip()
            if not src:
                messagebox.showerror("Ошибка", "Укажите путь сервера", parent=self)
                return
            args = [target, src] + ([dst] if dst else [])
            self._app.dispatch(ServerCmd.IMPORT, args)

        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
# ДИАЛОГ — ОТЛОЖЕННАЯ КОМАНДА
# ═══════════════════════════════════════════════════════════════════════════

class ScheduledDialog(tk.Toplevel):
    """
    Кнопка «Отложенная» в CommandBar.
    Поля меняются по флагу CMD / SIMPL / EXPORT / IMPORT.
    """
    FLAGS = [ServerCmd.CMD, ServerCmd.SIMPL, ServerCmd.EXPORT, ServerCmd.IMPORT]
    def __init__(self, parent, app: "ServerApp"):
        super().__init__(parent)
        self._app = app
        self.title("Новая отложенная команда")
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)
        self.grab_set()

        self._flag = tk.StringVar(value=ServerCmd.CMD)
        self._build()
        self._on_flag()
        self._center(parent)

    def _center(self, parent):
        """Центрирует диалог относительно родительского окна."""
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _lbl(self, parent, text):
        """Создаёт стилизованный Label."""
        return tk.Label(parent, text=text, bg=COLORS["bg"],
                        fg=COLORS["text_dim"], font=FONT_SMALL)

    def _ent(self, parent, **kw):
        """Создаёт стилизованный Entry."""
        return tk.Entry(parent, bg=COLORS["input_bg"], fg=COLORS["text"],
                        insertbackground=COLORS["text"], relief="flat",
                        font=FONT_MONO_S, **kw)

    def _build(self):
        """Строит всё содержимое диалога."""
        pad = dict(padx=14, pady=5)

        # ── флаги ────────────────────────────────────────────────────────
        ff = tk.Frame(self, bg=COLORS["bg"])
        ff.pack(fill="x", **pad)
        self._lbl(ff, "Тип:").pack(side="left", padx=(0, 8))
        self._flag_btns: dict = {}
        for f in self.FLAGS:
            b = tk.Button(ff, text=f, font=FONT_SMALL, relief="flat", bd=0,
                          padx=10, pady=3, cursor="hand2",
                          command=lambda x=f: self._sel(x))
            b.pack(side="left", padx=2)
            self._flag_btns[f] = b

        # ── цель ─────────────────────────────────────────────────────────
        tf = tk.Frame(self, bg=COLORS["bg"])
        tf.pack(fill="x", **pad)
        self._lbl(tf, "Цель (user / all / group:name)").pack(anchor="w")
        self._target_var = tk.StringVar()
        self._target_cb  = ttk.Combobox(tf, textvariable=self._target_var,
                                        font=FONT_MONO_S, width=38)
        self._target_cb.pack(fill="x", pady=(2, 0))
        self._fill_targets()

        # ── динамические поля ──────────────────────────────────────────────
        self._df = tk.Frame(self, bg=COLORS["bg"])
        self._df.pack(fill="x", padx=14)

        # CMD
        self._cmd_f = tk.Frame(self._df, bg=COLORS["bg"])
        self._lbl(self._cmd_f, "Команда").pack(anchor="w", pady=(6, 1))
        self._cmd_e = self._ent(self._cmd_f, width=48)
        self._cmd_e.pack(fill="x")

        # SIMPL
        self._simpl_f = tk.Frame(self._df, bg=COLORS["bg"])
        tk.Label (self._simpl_f, text="Выполнит строки из code.txt",
                  bg=COLORS["bg"], fg=COLORS["text_dim"],
                  font=FONT_SMALL, pady=6).pack ()

        self._lbl (self._simpl_f, "Или выберите шаблон (необязательно)").pack (anchor="w", pady=(4, 1))
        self._simpl_template_var = tk.StringVar ()
        self._simpl_template_cb = ttk.Combobox (
            self._simpl_f, textvariable=self._simpl_template_var,
            font=FONT_MONO_S, width=38, state="readonly",
        )
        self._simpl_template_cb.bind ("<Button-1>", lambda e: self._refresh_sched_templates ())
        self._simpl_template_cb.pack (fill="x", pady=(0, 6))
        self._refresh_sched_templates ()


        # IMPORT
        self._import_f = tk.Frame(self._df, bg=COLORS["bg"])
        self._lbl(self._import_f, "Путь на сервере (источник)").pack(anchor="w", pady=(6, 1))
        self._imp_src = self._ent(self._import_f, width=48)
        self._imp_src.pack(fill="x")
        self._lbl(self._import_f, "Путь на клиенте (назначение)").pack(anchor="w", pady=(6, 1))
        self._imp_dst = self._ent(self._import_f, width=48)
        self._imp_dst.pack(fill="x")

        # EXPORT
        self._export_f = tk.Frame(self._df, bg=COLORS["bg"])
        self._lbl(self._export_f, "Путь на клиенте (источник)").pack(anchor="w", pady=(6, 1))
        self._exp_src = self._ent(self._export_f, width=48)
        self._exp_src.pack(fill="x")
        self._lbl(self._export_f, "Путь на сервере (назначение)").pack(anchor="w", pady=(6, 1))
        self._exp_dst = self._ent(self._export_f, width=48)
        self._exp_dst.pack(fill="x")

        # ── кнопки ────────────────────────────────────────────────────────
        bf = tk.Frame(self, bg=COLORS["bg"])
        bf.pack(fill="x", padx=14, pady=12)
        tk.Button(bf, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="+ Создать", font=FONT_UI_B, bg=COLORS["sched"],
                  fg="white", relief="flat", padx=14, pady=5, cursor="hand2",
                  command=self._submit).pack(side="right")

        self._bind_enter(self._cmd_e, self._imp_src, self._imp_dst, self._exp_src, self._exp_dst)

    def _fill_targets(self):
        """Заполняет комбобокс целей из UserManager и состояния сервера."""

        um    = srv.get_user_mgr()
        opts  = ["all"]
        if um:
            opts += um.get_all_usernames()
        self._target_cb["values"] = opts

    def _refresh_sched_templates(self):
        """Загружает список шаблонов из TemplateManager."""
        tmgr = srv.get_template_mgr ()

        names = ["default"] + tmgr.get_all_template_name () if tmgr else ["None"]

        self._simpl_template_cb["values"] = names
        self._simpl_template_cb.set ("")

    def _sel(self, flag: str):
        """Устанавливает активный флаг и обновляет UI."""
        self._flag.set(flag)
        self._on_flag()

    def _on_flag(self):
        """Перерисовывает кнопки флагов и показывает нужный фрейм полей."""
        flag = self._flag.get()
        for f, btn in self._flag_btns.items():
            c = COLORS[f]
            btn.configure(bg=c if f == flag else COLORS["panel"],
                          fg="white" if f == flag else COLORS["text_dim"])
        for fr in (self._cmd_f, self._simpl_f, self._import_f, self._export_f):
            fr.pack_forget()
        {ServerCmd.CMD: self._cmd_f, ServerCmd.SIMPL: self._simpl_f,
         ServerCmd.IMPORT: self._import_f, ServerCmd.EXPORT: self._export_f}[flag].pack(fill="x")

    def _bind_enter(self, *widgets):
        """Привязывает Enter → _submit ко всем переданным виджетам и самому окну."""
        for w in widgets:
            w.bind("<Return>", lambda e: self._submit())
        self.bind("<Return>", lambda e: self._submit())

    def _submit(self):
        """Валидирует поля и отправляет отложенную команду через app.dispatch."""
        flag   = self._flag.get()
        target = self._target_var.get().strip()
        if not target:
            messagebox.showerror("Ошибка", "Укажите цель", parent=self)
            return

        args = [flag.lower(), target]

        if flag == ServerCmd.CMD:
            cmd = self._cmd_e.get().strip()
            if not cmd:
                messagebox.showerror("Ошибка", "Введите команду", parent=self)
                return
            args.append(cmd)

        elif flag == ServerCmd.IMPORT:
            src = self._imp_src.get().strip()
            dst = self._imp_dst.get().strip()
            if not src or not dst:
                messagebox.showerror("Ошибка", "Заполните оба пути", parent=self)
                return
            args += [src, dst]

        elif flag == ServerCmd.EXPORT:
            src = self._exp_src.get().strip()
            dst = self._exp_dst.get().strip()
            if not src or not dst:
                messagebox.showerror("Ошибка", "Заполните оба пути", parent=self)
                return
            args += [src, dst]


        elif flag == ServerCmd.SIMPL:
            template = self._simpl_template_var.get ().strip ()
            if template:
                args.append (template)

        self._app.dispatch(ServerCmd.CHART_NEW, args)
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
# ДИАЛОГ — ГРУППЫ
# ═══════════════════════════════════════════════════════════════════════════

class GroupDialog(tk.Toplevel):
    """
    Управление группами — три вкладки:
      Создать  — новая группа + список участников
      Изменить — добавить / удалить участников в существующей группе
      Удалить  — удалить группу целиком
    """

    TABS = [ServerCmd.GROUP_NEW, ServerCmd.GROUP_EDIT, ServerCmd.GROUP_DEL]

    def __init__(self, parent, app: "ServerApp"):
        super().__init__(parent)
        self._app = app
        self.title("Управление группами")
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)
        self.grab_set()
        self._members: list = []          # для вкладки Создать
        self._rm_members: list = []       # для вкладки Изменить → удалить
        self._build()
        self._center(parent)

    def _center(self, parent):
        """Центрирует диалог относительно родительского окна."""
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _lbl(self, parent, text, pady=(4, 1)):
        """Создаёт стилизованный Label."""
        return tk.Label(parent, text=text, bg=COLORS["bg"],
                        fg=COLORS["text_dim"], font=FONT_SMALL)

    def _ent(self, parent, width=36, **kw):
        """Создаёт стилизованный Entry."""
        return tk.Entry(parent, bg=COLORS["input_bg"], fg=COLORS["text"],
                        insertbackground=COLORS["text"], relief="flat",
                        font=FONT_MONO_S, width=width, **kw)

    def _all_usernames(self):
        """Возвращает список всех известных пользователей."""
        um = srv.get_user_mgr()
        return um.get_all_usernames() if um else []

    def _all_groups(self):
        """Возвращает список всех групп (через GroupManager или напрямую из JSON)."""
        gm = srv.get_group_mgr() if hasattr(srv, "get_group_mgr") else None
        if gm:
            return gm.get_all_group()
        try:

            with open(Config.FILE_GROUPS, encoding="utf-8") as f:
                return list(json.load(f).keys())
        except Exception:
            return []

    def _group_members(self, group_name: str) -> list:
        """Читает участников конкретной группы из JSON."""
        try:
            with open(Config.FILE_GROUPS, encoding="utf-8") as f:
                return json.load(f).get(group_name, [])
        except Exception:
            return []

    # ══════════════════════════════════════════════════════════════════
    # СТРОИМ ОКНО
    # ══════════════════════════════════════════════════════════════════

    def _build(self):
        """Строит вкладки и все три панели."""
        tab_frame = tk.Frame(self, bg=COLORS["panel"])
        tab_frame.pack(fill="x")
        self._tab_btns = {}
        self._active_tab = tk.StringVar(value=ServerCmd.GROUP_NEW)
        for t in self.TABS:
            text = {ServerCmd.GROUP_NEW: "CREATE", ServerCmd.GROUP_EDIT: "EDIT", ServerCmd.GROUP_DEL: "DELETE"}[t]
            b = tk.Button(tab_frame, text=text, font=FONT_SMALL, relief="flat",
                          bd=0, padx=12, pady=6, cursor="hand2",
                          command=lambda x=t: self._switch_tab(x))
            b.pack(side="left")
            self._tab_btns[t] = b

        self._tab_container = tk.Frame(self, bg=COLORS["bg"])
        self._tab_container.pack(fill="both", expand=True)

        self._pane_create = self._build_create(self._tab_container)
        self._pane_edit   = self._build_edit(self._tab_container)
        self._pane_delete = self._build_delete(self._tab_container)

        self._switch_tab(self._active_tab.get())

    def _switch_tab(self, tab: str):
        """Переключает активную вкладку и перекрашивает кнопки."""
        self._active_tab.set(tab)
        for t, btn in self._tab_btns.items():
            if t == tab:
                btn.configure(bg=COLORS[t], fg="white")
            else:
                btn.configure(bg=COLORS["panel"], fg=COLORS["text_dim"])
        for pane in (self._pane_create, self._pane_edit, self._pane_delete):
            pane.pack_forget()
        {
            ServerCmd.GROUP_NEW:  self._pane_create,
            ServerCmd.GROUP_EDIT: self._pane_edit,
            ServerCmd.GROUP_DEL:  self._pane_delete,
        }[tab].pack(fill="both", expand=True)

    # ══════════════════════════════════════════════════════════════════
    # ВКЛАДКА: СОЗДАТЬ
    # ══════════════════════════════════════════════════════════════════

    def _build_create(self, parent) -> tk.Frame:
        """Строит вкладку создания новой группы."""
        f = tk.Frame(parent, bg=COLORS["bg"])

        self._lbl(f, "Имя группы").pack(anchor="w", padx=14, pady=(10, 1))
        self._name_var = tk.StringVar()
        self._name_ent = self._ent(f, textvariable=self._name_var)
        self._name_ent.pack(padx=14, fill="x")

        self._lbl(f, "Участники").pack(anchor="w", padx=14, pady=(8, 1))

        add_row = tk.Frame(f, bg=COLORS["bg"])
        add_row.pack(fill="x", padx=14)
        self._c_user_var = tk.StringVar()
        self._c_user_cb  = ttk.Combobox(add_row, textvariable=self._c_user_var,
                                         values=self._all_usernames(),
                                         font=FONT_MONO_S, width=26)
        self._c_user_cb.pack(side="left", padx=(0, 6))
        tk.Button(add_row, text="+ Добавить", bg=COLORS["accent"], fg="white",
                  font=FONT_SMALL, relief="flat", padx=8, cursor="hand2",
                  command=self._c_add).pack(side="left")

        self._c_tags = tk.Frame(f, bg=COLORS["input_bg"], padx=6, pady=6)
        self._c_tags.pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(self._c_tags, text="Нет участников", bg=COLORS["input_bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL).pack()

        bf = tk.Frame(f, bg=COLORS["bg"])
        bf.pack(fill="x", padx=14, pady=12)
        tk.Button(bf, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="Создать группу", font=FONT_UI_B,
                  bg=COLORS["group"], fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self._submit_create).pack(side="right")

        self._c_user_cb.bind("<Return>", lambda e: self._c_add())
        self._name_ent.bind("<Return>",  lambda e: self._c_user_cb.focus())
        return f

    def _c_add(self):
        """Добавляет пользователя в список участников новой группы."""
        name = self._c_user_var.get().strip()
        if not name or name in self._members:
            return
        self._members.append(name)
        self._refresh_tags(self._c_tags, self._members, self._c_remove, COLORS["group"])
        self._c_user_var.set("")

    def _c_remove(self, name: str):
        """Убирает пользователя из списка участников новой группы."""
        self._members.remove(name)
        self._refresh_tags(self._c_tags, self._members, self._c_remove, COLORS["group"])

    def _submit_create(self):
        """Отправляет команду GROUP_NEW с именем и участниками."""
        name = self._name_var.get().strip()
        if not name:
            messagebox.showerror("Ошибка", "Введите имя группы", parent=self)
            return
        self._app.dispatch(ServerCmd.GROUP_NEW, [name] + self._members)
        self.destroy()

    # ══════════════════════════════════════════════════════════════════
    # ВКЛАДКА: ИЗМЕНИТЬ
    # ══════════════════════════════════════════════════════════════════

    def _build_edit(self, parent) -> tk.Frame:
        """Строит вкладку редактирования существующей группы."""
        f = tk.Frame(parent, bg=COLORS["bg"])

        self._lbl(f, "Группа").pack(anchor="w", padx=14, pady=(10, 1))
        self._e_group_var = tk.StringVar()
        self._e_group_cb  = ttk.Combobox(f, textvariable=self._e_group_var,
                                          values=self._all_groups(),
                                          font=FONT_MONO_S, width=34)
        self._e_group_cb.pack(padx=14, fill="x")
        self._e_group_cb.bind("<<ComboboxSelected>>", lambda e: self._e_load_members())

        self._lbl(f, "Добавить участников").pack(anchor="w", padx=14, pady=(8, 1))
        add_row = tk.Frame(f, bg=COLORS["bg"])
        add_row.pack(fill="x", padx=14)
        self._e_add_var = tk.StringVar()
        self._e_add_cb  = ttk.Combobox(add_row, textvariable=self._e_add_var,
                                        values=self._all_usernames(),
                                        font=FONT_MONO_S, width=26)
        self._e_add_cb.pack(side="left", padx=(0, 6))
        tk.Button(add_row, text="+ Добавить", bg=COLORS["accent2"], fg="white",
                  font=FONT_SMALL, relief="flat", padx=8, cursor="hand2",
                  command=self._e_add_user).pack(side="left")

        self._e_add_tags = tk.Frame(f, bg=COLORS["input_bg"], padx=6, pady=4)
        self._e_add_tags.pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(self._e_add_tags, text="Никого не добавлено",
                 bg=COLORS["input_bg"], fg=COLORS["text_dim"], font=FONT_SMALL).pack()
        self._e_add_list: list = []

        self._lbl(f, "Удалить участников").pack(anchor="w", padx=14, pady=(8, 1))
        rm_row = tk.Frame(f, bg=COLORS["bg"])
        rm_row.pack(fill="x", padx=14)
        self._e_rm_var = tk.StringVar()
        self._e_rm_cb  = ttk.Combobox(rm_row, textvariable=self._e_rm_var,
                                       font=FONT_MONO_S, width=26)
        self._e_rm_cb.pack(side="left", padx=(0, 6))
        tk.Button(rm_row, text="− Удалить", bg=COLORS["error"], fg="white",
                  font=FONT_SMALL, relief="flat", padx=8, cursor="hand2",
                  command=self._e_rm_user).pack(side="left")

        self._e_rm_tags = tk.Frame(f, bg=COLORS["input_bg"], padx=6, pady=4)
        self._e_rm_tags.pack(fill="x", padx=14, pady=(4, 0))
        tk.Label(self._e_rm_tags, text="Никого не выбрано",
                 bg=COLORS["input_bg"], fg=COLORS["text_dim"], font=FONT_SMALL).pack()
        self._e_rm_list: list = []

        bf = tk.Frame(f, bg=COLORS["bg"])
        bf.pack(fill="x", padx=14, pady=12)
        tk.Button(bf, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="Применить", font=FONT_UI_B,
                  bg=COLORS["group"], fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self._submit_edit).pack(side="right")

        self._e_add_cb.bind("<Return>", lambda e: self._e_add_user())
        self._e_rm_cb.bind("<Return>",  lambda e: self._e_rm_user())
        return f

    def _e_load_members(self):
        """Загружает текущих участников группы в комбобокс удаления."""
        group = self._e_group_var.get().strip()
        members = self._group_members(group)
        self._e_rm_cb["values"] = members
        self._e_rm_list.clear()
        self._e_add_list.clear()
        self._refresh_tags(self._e_add_tags, self._e_add_list,
                           self._e_undo_add, COLORS["accent2"])
        self._refresh_tags(self._e_rm_tags, self._e_rm_list,
                           self._e_undo_rm, COLORS["error"])

    def _e_add_user(self):
        """Добавляет пользователя в список «добавить»."""
        name = self._e_add_var.get().strip()
        if not name or name in self._e_add_list:
            return
        self._e_add_list.append(name)
        self._refresh_tags(self._e_add_tags, self._e_add_list,
                           self._e_undo_add, COLORS["accent2"])
        self._e_add_var.set("")

    def _e_undo_add(self, name: str):
        """Убирает пользователя из списка «добавить»."""
        self._e_add_list.remove(name)
        self._refresh_tags(self._e_add_tags, self._e_add_list,
                           self._e_undo_add, COLORS["accent2"])

    def _e_rm_user(self):
        """Добавляет пользователя в список «удалить»."""
        name = self._e_rm_var.get().strip()
        if not name or name in self._e_rm_list:
            return
        self._e_rm_list.append(name)
        self._refresh_tags(self._e_rm_tags, self._e_rm_list,
                           self._e_undo_rm, COLORS["error"])
        self._e_rm_var.set("")

    def _e_undo_rm(self, name: str):
        """Убирает пользователя из списка «удалить»."""
        self._e_rm_list.remove(name)
        self._refresh_tags(self._e_rm_tags, self._e_rm_list,
                           self._e_undo_rm, COLORS["error"])

    def _submit_edit(self):
        """Отправляет GROUP_ADD и/или GROUP_RM на основе собранных списков."""
        group = self._e_group_var.get().strip()
        if not group:
            messagebox.showerror("Ошибка", "Выберите группу", parent=self)
            return
        if self._e_add_list:
            self._app.dispatch(ServerCmd.GROUP_ADD, [group] + self._e_add_list)
        if self._e_rm_list:
            self._app.dispatch(ServerCmd.GROUP_RM,  [group] + self._e_rm_list)
        if not self._e_add_list and not self._e_rm_list:
            messagebox.showinfo("Нет изменений", "Ничего не выбрано", parent=self)
            return
        self.destroy()

    # ══════════════════════════════════════════════════════════════════
    # ВКЛАДКА: УДАЛИТЬ
    # ══════════════════════════════════════════════════════════════════

    def _build_delete(self, parent) -> tk.Frame:
        """Строит вкладку удаления группы."""
        f = tk.Frame(parent, bg=COLORS["bg"])

        self._lbl(f, "Группа для удаления").pack(anchor="w", padx=14, pady=(10, 1))
        self._d_group_var = tk.StringVar()
        self._d_group_cb  = ttk.Combobox(f, textvariable=self._d_group_var,
                                          values=self._all_groups(),
                                          font=FONT_MONO_S, width=34)
        self._d_group_cb.pack(padx=14, fill="x")

        tk.Label(f, text="Группа будет удалена безвозвратно",
                 bg=COLORS["bg"], fg=COLORS["warn"],
                 font=FONT_SMALL).pack(anchor="w", padx=14, pady=(10, 0))

        bf = tk.Frame(f, bg=COLORS["bg"])
        bf.pack(fill="x", padx=14, pady=(0, 8))
        tk.Button(bf, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="Удалить группу", font=FONT_UI_B,
                  bg=COLORS["error"], fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self._submit_delete).pack(side="right")
        return f

    def _submit_delete(self):
        """Запрашивает подтверждение и отправляет GROUP_DEL."""
        group = self._d_group_var.get().strip()
        if not group:
            messagebox.showerror("Ошибка", "Выберите группу", parent=self)
            return
        if not messagebox.askyesno("Подтверждение",
                                   f"Удалить группу '{group}'?", parent=self):
            return
        self._app.dispatch(ServerCmd.GROUP_DEL, [group])
        self.destroy()

    # ══════════════════════════════════════════════════════════════════
    # ОБЩИЙ РЕНДЕР ТЕГОВ
    # ══════════════════════════════════════════════════════════════════

    def _refresh_tags(self, container: tk.Frame, members: list,
                      remove_fn, color: str):
        """Перерисовывает цветные теги участников в контейнере."""
        for w in container.winfo_children():
            w.destroy()
        if not members:
            empty_text = "Никого не выбрано" if color == COLORS["error"] else "Нет участников"
            tk.Label(container, text=empty_text, bg=COLORS["input_bg"],
                     fg=COLORS["text_dim"], font=FONT_SMALL).pack()
            return
        row = None
        for i, m in enumerate(members):
            if i % 4 == 0:
                row = tk.Frame(container, bg=COLORS["input_bg"])
                row.pack(anchor="w")
            tag = tk.Frame(row, bg=color, padx=4, pady=2)
            tag.pack(side="left", padx=2, pady=2)
            tk.Label(tag, text=m, bg=color, fg="white", font=FONT_SMALL).pack(side="left")
            tk.Button(tag, text="×", bg=color, fg="white", font=FONT_SMALL,
                      relief="flat", bd=0, cursor="hand2",
                      command=lambda x=m: remove_fn(x)).pack(side="left")

# ═══════════════════════════════════════════════════════════════════════════
#  Шаблоны команд
# ═══════════════════════════════════════════════════════════════════════════

class TemplateDialog(tk.Toplevel):
    """
    Управление шаблонами — три вкладки:
      Создать  — новый шаблон + список команд
      Изменить — добавить / удалить команды в существующем шаблоне
      Удалить  — удалить шаблон целиком
    """

    TABS = [ServerCmd.TEMPLATE_NEW, ServerCmd.TEMPLATE_ADD, ServerCmd.TEMPLATE_DEL]

    def __init__(self, parent, app: "ServerApp"):
        super().__init__(parent)
        self._app = app
        self.title("Управление шаблонами")
        self.configure(bg=COLORS["bg"])
        self.resizable(False, False)
        self.grab_set()
        self._build()
        self._center(parent)

    def _center(self, parent):
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width()  - self.winfo_width())  // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _lbl(self, parent, text):
        return tk.Label(parent, text=text, bg=COLORS["bg"],
                        fg=COLORS["text_dim"], font=FONT_SMALL)

    def _ent(self, parent, width=36, **kw):
        return tk.Entry(parent, bg=COLORS["input_bg"], fg=COLORS["text"],
                        insertbackground=COLORS["text"], relief="flat",
                        font=FONT_MONO_S, width=width, **kw)

    def _all_templates(self):
        tmgr = srv.get_template_mgr()
        return tmgr.get_all_template_name() if tmgr else []

    def _build(self):
        # ── вкладки ───────────────────────────────────────────────────────
        tab_frame = tk.Frame(self, bg=COLORS["bg"])
        tab_frame.pack(fill="x", padx=14, pady=(12, 0))

        self._tab_var  = tk.StringVar(value=self.TABS[0])
        self._tab_btns = {}
        labels = {
            ServerCmd.TEMPLATE_NEW: "CREAT",
            ServerCmd.TEMPLATE_ADD: "EDIT",
            ServerCmd.TEMPLATE_DEL: "DELETE",
        }
        for tab in self.TABS:
            btn = tk.Button(tab_frame, text=labels[tab], font=FONT_UI_B,
                            bg=COLORS["panel"], fg=COLORS["text_dim"],
                            relief="flat", padx=12, pady=4, cursor="hand2",
                            command=lambda t=tab: self._sel(t))
            btn.pack(side="left", padx=(0, 4))
            self._tab_btns[tab] = btn

        # ── контент ───────────────────────────────────────────────────────
        self._content = tk.Frame(self, bg=COLORS["bg"])
        self._content.pack(fill="both", padx=14, pady=8)

        self._pane_new  = self._build_new(self._content)
        self._pane_edit = self._build_edit(self._content)
        self._pane_del  = self._build_delete(self._content)

        self._sel(self.TABS[0])

    def _sel(self, tab):
        self._tab_var.set(tab)
        for t, btn in self._tab_btns.items():
            active = t == tab
            btn.configure(
                bg=COLORS[t] if active else COLORS["panel"],
                fg="white" if active else COLORS["text_dim"],
            )
        for pane in (self._pane_new, self._pane_edit, self._pane_del):
            pane.pack_forget()
        {
            ServerCmd.TEMPLATE_NEW: self._pane_new,
            ServerCmd.TEMPLATE_ADD: self._pane_edit,
            ServerCmd.TEMPLATE_DEL: self._pane_del,
        }[tab].pack(fill="both")

    # ══════════════════════════════════════════════════════════════════
    # ВКЛАДКА: СОЗДАТЬ
    # ══════════════════════════════════════════════════════════════════

    def _build_new(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=COLORS["bg"])

        self._lbl(f, "Название шаблона").pack(anchor="w", pady=(6, 1))
        self._n_name = self._ent(f, width=46)
        self._n_name.pack(fill="x")

        self._lbl(f, "Команды (каждая с новой строки)").pack(anchor="w", pady=(8, 1))
        self._n_text = tk.Text(
            f, bg=COLORS["input_bg"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief="flat",
            font=FONT_MONO_S, width=46, height=7, wrap="none",
        )
        self._n_text.pack(fill="x")

        bf = tk.Frame(f, bg=COLORS["bg"])
        bf.pack(fill="x", pady=(10, 4))
        tk.Button(bf, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="+ Создать", font=FONT_UI_B, bg=COLORS["accent2"],
                  fg="white", relief="flat", padx=14, pady=5, cursor="hand2",
                  command=self._submit_new).pack(side="right")
        return f

    def _submit_new(self):
        name = self._n_name.get().strip()
        if not name:
            messagebox.showerror("Ошибка", "Введите название шаблона", parent=self)
            return
        raw = self._n_text.get("1.0", "end").strip()
        if not raw:
            messagebox.showerror("Ошибка", "Введите хотя бы одну команду", parent=self)
            return
        commands = [l.strip() for l in raw.splitlines() if l.strip()]
        self._app.dispatch(ServerCmd.TEMPLATE_NEW, [name] + commands)
        self.destroy()

    # ══════════════════════════════════════════════════════════════════
    # ВКЛАДКА: ИЗМЕНИТЬ
    # ══════════════════════════════════════════════════════════════════

    def _build_edit(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=COLORS["bg"])

        self._lbl(f, "Шаблон").pack(anchor="w", pady=(6, 1))
        self._e_tmpl_var = tk.StringVar()
        self._e_tmpl_cb  = ttk.Combobox(f, textvariable=self._e_tmpl_var,
                                         font=FONT_MONO_S, width=44)
        self._e_tmpl_cb.pack(fill="x")
        self._e_tmpl_cb.bind("<Button-1>", lambda e: self._refresh_edit_templates())
        self._e_tmpl_cb.bind("<<ComboboxSelected>>", lambda e: self._load_edit_commands())

        # добавить команды
        self._lbl(f, "Добавить команды (каждая с новой строки)").pack(anchor="w", pady=(8, 1))
        self._e_add_text = tk.Text(
            f, bg=COLORS["input_bg"], fg=COLORS["text"],
            insertbackground=COLORS["text"], relief="flat",
            font=FONT_MONO_S, width=46, height=4, wrap="none",
        )
        self._e_add_text.pack(fill="x")

        # удалить по индексам
        self._lbl(f, "Удалить команды (индексы через пробел)").pack(anchor="w", pady=(8, 1))

        self._e_cmds_box = tk.Listbox(
            f, bg=COLORS["input_bg"], fg=COLORS["text_dim"],
            font=FONT_MONO_S, height=4, relief="flat",
            selectmode="extended", selectbackground=COLORS["error"],
        )
        self._e_cmds_box.pack(fill="x")

        bf = tk.Frame(f, bg=COLORS["bg"])
        bf.pack(fill="x", pady=(10, 4))
        tk.Button(bf, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="Сохранить", font=FONT_UI_B, bg=COLORS["accent"],
                  fg="white", relief="flat", padx=14, pady=5, cursor="hand2",
                  command=self._submit_edit).pack(side="right")
        return f

    def _refresh_edit_templates(self):
        self._e_tmpl_cb["values"] = self._all_templates()

    def _load_edit_commands(self):
        """Загружает команды шаблона в Listbox."""
        tmgr = srv.get_template_mgr()
        if not tmgr:
            return
        name = self._e_tmpl_var.get().strip()
        cmds = tmgr.get_comd_template_name(name) if name else []
        self._e_cmds_box.delete(0, "end")
        for i, c in enumerate(cmds):
            self._e_cmds_box.insert("end", f"{i}: {c}")

    def _submit_edit(self):
        name = self._e_tmpl_var.get().strip()
        if not name:
            messagebox.showerror("Ошибка", "Выберите шаблон", parent=self)
            return
        # добавить
        raw = self._e_add_text.get("1.0", "end").strip()
        if raw:
            commands = [l.strip() for l in raw.splitlines() if l.strip()]
            self._app.dispatch(ServerCmd.TEMPLATE_ADD, [name] + commands)
        # удалить выделенные
        selected = [int(self._e_cmds_box.get(i).split(":")[0])
                    for i in self._e_cmds_box.curselection()]
        if selected:
            self._app.dispatch(ServerCmd.TEMPLATE_RM, [name] + [str(i) for i in selected])
        if not raw and not selected:
            messagebox.showinfo("Нет изменений", "Ничего не выбрано", parent=self)
            return
        self.destroy()

    # ══════════════════════════════════════════════════════════════════
    # ВКЛАДКА: УДАЛИТЬ
    # ══════════════════════════════════════════════════════════════════

    def _build_delete(self, parent) -> tk.Frame:
        f = tk.Frame(parent, bg=COLORS["bg"])

        self._lbl(f, "Шаблон для удаления").pack(anchor="w", pady=(6, 1))
        self._d_tmpl_var = tk.StringVar()
        self._d_tmpl_cb  = ttk.Combobox(f, textvariable=self._d_tmpl_var,
                                         font=FONT_MONO_S, width=44)
        self._d_tmpl_cb.pack(fill="x")
        self._d_tmpl_cb.bind("<Button-1>", lambda e: self._refresh_del_templates())

        tk.Label(f, text="Шаблон будет удалён безвозвратно",
                 bg=COLORS["bg"], fg=COLORS["warn"],
                 font=FONT_SMALL).pack(anchor="w", pady=(10, 0))

        bf = tk.Frame(f, bg=COLORS["bg"])
        bf.pack(fill="x", pady=(8, 4))
        tk.Button(bf, text="Отмена", font=FONT_UI, bg=COLORS["panel"],
                  fg=COLORS["text"], relief="flat", padx=14, pady=5,
                  command=self.destroy).pack(side="right", padx=(6, 0))
        tk.Button(bf, text="Удалить шаблон", font=FONT_UI_B,
                  bg=COLORS["error"], fg="white", relief="flat",
                  padx=14, pady=5, cursor="hand2",
                  command=self._submit_delete).pack(side="right")
        return f

    def _refresh_del_templates(self):
        self._d_tmpl_cb["values"] = self._all_templates()

    def _submit_delete(self):
        name = self._d_tmpl_var.get().strip()
        if not name:
            messagebox.showerror("Ошибка", "Выберите шаблон", parent=self)
            return
        if not messagebox.askyesno("Подтверждение",
                                   f"Удалить шаблон '{name}'?", parent=self):
            return
        self._app.dispatch(ServerCmd.TEMPLATE_DEL, [name])
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
# ПАНЕЛЬ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════

class CommandBar(tk.Frame):
    """
    Нижняя левая область:
      [Онлайн] [Отложенная] [Группы]
      поле ввода + кнопка отправить
    """

    def __init__(self, parent, app: "ServerApp"):
        super().__init__(parent, bg=COLORS["bg"])
        self._app = app
        self._build()

    def _build(self):
        """Строит кнопки режимов, строку подсказки и строку ввода."""
        # ── режимы ────────────────────────────────────────────────────────
        mode_frame = tk.Frame(self, bg=COLORS["bg"])
        mode_frame.pack(fill="x", pady=(4, 2))

        for text, color, cmd in [
            ("▶  Онлайн",     "accent",  self._open_online),
            ("⏱  Отложенная", "sched",   self._open_sched),
            ("👥 Группы",     "group",   self._open_group),
            ("📋 Шаблоны", "accent2", self._open_template),
        ]:
            tk.Button(mode_frame, text=text, font=FONT_UI_B,
                      bg=COLORS[color], fg="white", relief="flat",
                      padx=10, pady=4, cursor="hand2",
                      command=cmd).pack(side="left", padx=(0, 6))

        # ── подсказка ──────────────────────────────────────────────────────
        self._hint_var = tk.StringVar(value="▸ выберите команду или введите вручную")
        tk.Label(self, textvariable=self._hint_var, bg=COLORS["panel"],
                 fg=COLORS["accent"], font=FONT_MONO_S,
                 anchor="w", padx=8, pady=3).pack(fill="x", pady=(0, 3))

        # ── строка ввода ──────────────────────────────────────────────────
        input_frame = tk.Frame(self, bg=COLORS["bg"])
        input_frame.pack(fill="x")

        self._input_var = tk.StringVar()
        self._entry = tk.Entry(input_frame, textvariable=self._input_var,
                               bg=COLORS["input_bg"], fg=COLORS["text"],
                               insertbackground=COLORS["text"],
                               relief="flat", font=FONT_MONO,
                               width=38)
        self._entry.pack(side="left", fill="x", expand=True, ipady=5)
        self._entry.bind("<Return>", lambda e: self._submit_raw())

        tk.Button(input_frame, text="Отправить", font=FONT_UI,
                  bg=COLORS["accent"], fg="white", relief="flat",
                  padx=10, pady=4, cursor="hand2",
                  command=self._submit_raw).pack(side="left", padx=(6, 0))

    def _submit_raw(self):
        """Читает строку ввода и передаёт в app.dispatch_raw."""
        raw = self._input_var.get().strip()
        if not raw:
            return
        self._input_var.set("")
        self._app.dispatch_raw(raw)

    def _open_online(self):
        """Открывает диалог онлайн-команды."""
        OnlineDialog(self._app.root, self._app)

    def _open_sched(self):
        """Открывает диалог отложенной команды."""
        ScheduledDialog(self._app.root, self._app)

    def _open_group(self):
        """Открывает диалог управления группами."""
        GroupDialog(self._app.root, self._app)

    def _open_template(self):
        """Открывает диалог управления шаблонами."""
        TemplateDialog (self._app.root, self._app)


# ═══════════════════════════════════════════════════════════════════════════
# ПАНЕЛЬ ПОЛЬЗОВАТЕЛЕЙ
# ═══════════════════════════════════════════════════════════════════════════

class UsersPanel(tk.Frame):
    """Правая панель: онлайн-пользователи и список команд с автодополнением."""

    def __init__(self, parent, cmd):
        super().__init__(parent, bg=COLORS["bg"], width=240)
        self.pack_propagate(False)
        self._com = cmd   # ссылка на CommandBar для вставки подсказок
        self._build()

    def _build(self):
        """Строит список онлайн-пользователей и список доступных команд."""
        # ── онлайн ────────────────────────────────────────────────────────
        tk.Label(self, text="ОНЛАЙН", bg=COLORS["bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL).pack(anchor="w", padx=8, pady=(8, 2))

        frame = tk.Frame(self, bg=COLORS["panel"], padx=4, pady=4)
        frame.pack(fill="x", padx=6)

        sb = tk.Scrollbar(frame)
        sb.pack(side="right", fill="y")

        self._users_list = tk.Listbox(
            frame, yscrollcommand=sb.set, height=8,
            bg=COLORS["panel"], fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            font=FONT_MONO_S, relief="flat",
            activestyle="none", bd=0,
        )
        self._users_list.pack(fill="both", expand=True)
        sb.config(command=self._users_list.yview)

        # ── команды ───────────────────────────────────────────────────────
        tk.Label(self, text="КОМАНДЫ", bg=COLORS["bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL).pack(anchor="w", pady=(8, 2))

        frame2 = tk.Frame(self, bg=COLORS["bg"])
        frame2.pack(fill="both", expand=True)

        sb2 = tk.Scrollbar(frame2, bg=COLORS["panel"])
        sb2.pack(side="right", fill="y")

        self._cmd_list = tk.Listbox(
            frame2, yscrollcommand=sb2.set,
            bg=COLORS["panel"], fg=COLORS["text"],
            selectbackground=COLORS["accent"],
            selectforeground="white",
            font=FONT_MONO_S, relief="flat",
            activestyle="none", bd=0,
        )
        self._cmd_list.pack(fill="both", expand=True)
        sb2.config(command=self._cmd_list.yview)

        for name in CMD_HINTS:
            self._cmd_list.insert("end", f"  {name}")
        self._cmd_list.bind("<<ListboxSelect>>", self._on_select)

    def _on_select(self, _):
        """Вставляет выбранную команду и подсказку в строку ввода CommandBar."""
        sel = self._cmd_list.curselection()
        if not sel:
            return
        name = self._cmd_list.get(sel[0]).strip()
        hint = CMD_HINTS.get(name, "")
        self._com._hint_var.set(f"▸ {hint}")
        self._com._input_var.set(name + " ")
        self._com._entry.focus()
        self._com._entry.icursor("end")

    def update_users(self, connected: list):
        """Обновляет список онлайн-пользователей."""
        self._users_list.delete(0, "end")
        for u in connected:
            self._users_list.insert("end", f"  ● {u}")


# ═══════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ ОКНО
# ═══════════════════════════════════════════════════════════════════════════

class ServerApp:

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("TCP Server Manager")
        self.root.configure(bg=COLORS["bg"])
        self.root.geometry("1050x680")
        self.root.minsize(800, 550)

        self._build()
        set_log_callback(self._on_log)

        # Запускаем сервер в отдельном потоке
        self._server_thread = threading.Thread(target=srv.run_server, daemon=True)
        self._server_thread.start()

        # Периодическое обновление UI
        self.root.after(1500, self._tick)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build(self):
        """Строит основную компоновку: заголовок, лог, командную строку, правую панель."""
        # ── заголовок ──────────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=COLORS["panel"], pady=6)
        header.pack(fill="x")
        tk.Label(header, text="⬡  TCP SERVER MANAGER",
                 bg=COLORS["panel"], fg=COLORS["accent"],
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=14)
        self._status_lbl = tk.Label(header, text="● Запуск...",
                                     bg=COLORS["panel"], fg=COLORS["warn"],
                                     font=FONT_SMALL)
        self._status_lbl.pack(side="right", padx=14)

        # ── основная сетка ────────────────────────────────────────────────
        main = tk.Frame(self.root, bg=COLORS["bg"])
        main.pack(fill="both", expand=True, padx=8, pady=8)

        left = tk.Frame(main, bg=COLORS["bg"])
        left.pack(side="left", fill="both", expand=True)

        # Лог
        tk.Label(left, text="ВЫВОД", bg=COLORS["bg"],
                 fg=COLORS["text_dim"], font=FONT_SMALL).pack(anchor="w")
        self._log = scrolledtext.ScrolledText(
            left, bg=COLORS["bg"], fg=COLORS["text"],
            font=FONT_MONO, relief="flat", state="disabled",
            wrap="word", height=25
        )
        self._log.pack(fill="both", expand=True)
        self._log.tag_config("ok",   foreground=COLORS["accent2"])
        self._log.tag_config("err",  foreground=COLORS["error"])
        self._log.tag_config("warn", foreground=COLORS["warn"])
        self._log.tag_config("info", foreground=COLORS["text_dim"])
        self._log.tag_config("cmd",  foreground=COLORS["accent"])

        # Правая панель
        right = tk.Frame(main, bg=COLORS["bg"], width=260)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        sep = tk.Frame(left, bg=COLORS["border"], height=1)
        sep.pack(fill="x", pady=4)

        self._cmd_bar = CommandBar(left, self)
        self._cmd_bar.pack(fill="x")

        sep2 = tk.Frame(right, bg=COLORS["border"], height=1)
        sep2.pack(fill="x", pady=4)

        self._users_panel = UsersPanel(right, self._cmd_bar)
        self._users_panel.pack(fill="both", expand=True)

    # ── мост asyncio ──────────────────────────────────────────────────────

    def dispatch(self, cmd: ServerCmd, args: list):
        """Отправляет команду в asyncio-цикл сервера (вызывается из диалогов)."""
        loop = srv.get_loop()
        disp = srv.get_dispatcher()
        if not loop or not disp:
            self._log_line("[ERROR] Сервер ещё не готов", "err")
            return

        asyncio.run_coroutine_threadsafe(disp.dispatch(cmd, args), loop)

    def dispatch_raw(self, raw: str):
        """Парсит строку ввода и маршрутизирует: диалог или прямой dispatch."""

        try:
            cmd, args = ServerCmdParser.parse(raw)
        except ValueError as e:
            self._log_line(f"[ОШИБКА] {e}", "err")
            return
        if cmd == ServerCmd.EXIT:
            self._on_close()
            return
        if cmd in (ServerCmd.IMPORT, ServerCmd.EXPORT):
            dlg = OnlineDialog(self.root, self)
            dlg._select_flag(cmd)
            return
        if cmd in ServerCmd.CHART_NEW:
            ScheduledDialog(self.root, self)
            return
        if cmd in (ServerCmd.GROUP_NEW, ServerCmd.GROUP_DEL, ServerCmd.GROUP_RM):
            GroupDialog(self.root, self)
            return
        self.dispatch(cmd, args)

    # ── логирование ───────────────────────────────────────────────────────

    def _on_log(self, line: str):
        """Callback из Logger — вызывается из asyncio-потока, планирует вставку в GUI."""
        self.root.after(0, self._log_line, line, self._classify(line))

    def _classify(self, line: str) -> str:
        """Определяет тег цвета для строки лога по ключевым словам."""
        l = line.lower()
        if "error" in l or "критическая" in l:
            return "err"
        if "warning" in l or "warn" in l:
            return "warn"
        if "connect" in l or "disconnect" in l or "вход" in l:
            return "ok"
        if "cmd" in l or "export" in l or "import" in l or "simpl" in l:
            return "cmd"
        return "info"

    def _log_line(self, line: str, tag: str = "info"):
        """Вставляет строку в лог-виджет с заданным тегом."""
        self._log.config(state="normal")
        self._log.insert("end", line + "\n", tag)
        self._log.see("end")
        self._log.config(state="disabled")

    # ── периодическое обновление ──────────────────────────────────────────

    def _tick(self):
        """Обновляет список пользователей и статус-лейбл каждые 2 секунды."""
        state = srv.get_state()
        if state:
            clients = state.get_all_clients()
            self._users_panel.update_users(clients)
            n = len(clients)
            self._status_lbl.config(
                text=f"● Онлайн: {n}",
                fg=COLORS["online"] if n else COLORS["text_dim"],
            )
        else:
            self._status_lbl.config(text="● Запуск...", fg=COLORS["warn"])

        self.root.after(2000, self._tick)

    # ── закрытие ──────────────────────────────────────────────────────────

    def _on_close(self):
        """Запускает корутину _cleanup и закрывает окно через 300 мс."""
        state    = srv.get_state()
        user_mgr = srv.get_user_mgr()
        loop     = srv.get_loop()
        if state and user_mgr and loop:
            async def _shutdown():
                srv._cleanup(state, user_mgr)
            asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        self.root.after(300, self.root.destroy)

    def run(self):
        """Запускает tkinter mainloop."""
        self.root.mainloop()


# ═══════════════════════════════════════════════════════════════════════════
# ТОЧКА ВХОДА
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ServerApp().run()
