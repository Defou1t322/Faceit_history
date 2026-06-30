#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FACEIT Match History — десктоп-GUI (tkinter).

Вся логика работы с API вынесена в faceit_core.py и переиспользуется также
веб-версией (faceit_web.py). Здесь только интерфейс.

Сборка в .exe: запустите  python build.py.
Зависимости:  pip install requests
"""

import os
import json
import queue
import threading
import traceback
from datetime import datetime, timezone
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox

import requests

from faceit_core import (
    API_BASE, REQUEST_TIMEOUT, MIN_REQUEST_INTERVAL, DATE_MODE_MAX,
    PARTY_MIN_MATCHES, PARTY_MIN_RATIO, DEFAULT_API_KEY, GAMES,
    PERIOD_LAST, PERIOD_DATES, PERIOD_OFFSET, PERIOD_MODES,
    Cancelled, Client, nickname_from_input, get_player_id, get_history,
    parse_match, compute_stats, party_nicknames, classify_role,
    _searched_team_indices, STATS_COLUMNS, stats_rows_flat, export_stats_csv,
    stats_to_tsv, EXPORT_COLUMNS, unix_to_str, parse_date_to_unix,
    flatten_rows, export_csv, export_xlsx, results_to_tsv, http_error_message,
)

CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".faceit_history.json")


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.last_results = []
        self.last_stats = []
        self.searched_ids = set()
        self.party_nicks = set()
        self._stats_sort = (None, False)

        root.title("FACEIT Match History")
        root.geometry("860x680")
        root.minsize(720, 520)

        cfg = self._load_config()

        outer = ttk.Frame(root, padding=10)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)

        # --- API key (общий для всех вкладок) ---
        ttk.Label(outer, text="API-ключ:").grid(row=0, column=0, sticky="w",
                                                 padx=6, pady=4)
        self.key_var = tk.StringVar(value=cfg.get("api_key", DEFAULT_API_KEY))
        self.key_entry = ttk.Entry(outer, textvariable=self.key_var, show="•")
        self.key_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(outer, text="показать", variable=self.show_key,
                        command=self._toggle_key).grid(row=0, column=2, padx=6, pady=4)

        # --- Notebook ---
        self.nb = ttk.Notebook(outer)
        self.nb.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        outer.rowconfigure(1, weight=1)

        self.tab_search = ttk.Frame(self.nb, padding=8)
        self.tab_stats = ttk.Frame(self.nb, padding=8)
        self.nb.add(self.tab_search, text="Поиск")
        self.nb.add(self.tab_stats, text="Статистика")

        self._build_search_tab(cfg)
        self._build_stats_tab()

        # --- Status bar ---
        self.status = tk.StringVar(value="Готово.")
        self.count_var = tk.StringVar(value="Запросов к API: 0")
        bar = ttk.Frame(root)
        bar.pack(fill="x")
        ttk.Label(bar, textvariable=self.count_var, anchor="e",
                  padding=(8, 3)).pack(side="right")
        self.status_lbl = ttk.Label(bar, textvariable=self.status, anchor="w",
                                    padding=(8, 3))
        self.status_lbl.pack(side="left", fill="x", expand=True)

        self.root.after(100, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- build: Поиск ----------
    def _build_search_tab(self, cfg):
        frm = self.tab_search
        frm.columnconfigure(0, weight=1)
        pad = {"padx": 6, "pady": 4}

        ttk.Label(frm, text="Игроки (по одному в строке — ник или ссылка):").grid(
            row=0, column=0, sticky="w", **pad)
        self.players_text = tk.Text(frm, height=4, width=60, wrap="none",
                                    font=("Consolas", 10))
        self.players_text.grid(row=1, column=0, sticky="ew", **pad)
        self.players_text.insert("1.0", cfg.get("players", cfg.get("player", "")))

        # Игра + период
        opts = ttk.Frame(frm)
        opts.grid(row=2, column=0, sticky="w", **pad)
        ttk.Label(opts, text="Игра:").pack(side="left")
        self.game_var = tk.StringVar(value=cfg.get("game", "cs2"))
        ttk.Combobox(opts, textvariable=self.game_var, values=GAMES,
                     width=10, state="readonly").pack(side="left", padx=(4, 16))
        ttk.Label(opts, text="Период:").pack(side="left")
        self.period_var = tk.StringVar(value=cfg.get("period_mode", PERIOD_LAST))
        period_cb = ttk.Combobox(opts, textvariable=self.period_var,
                                 values=PERIOD_MODES, width=24, state="readonly")
        period_cb.pack(side="left", padx=4)
        period_cb.bind("<<ComboboxSelected>>", lambda e: self._update_period_fields())

        # Динамические поля периода
        self.period_box = ttk.Frame(frm)
        self.period_box.grid(row=3, column=0, sticky="w", **pad)

        # last
        self.f_last = ttk.Frame(self.period_box)
        ttk.Label(self.f_last, text="Матчей:").pack(side="left")
        self.limit_var = tk.StringVar(value=str(cfg.get("limit", 20)))
        ttk.Spinbox(self.f_last, from_=1, to=2000, textvariable=self.limit_var,
                    width=7).pack(side="left", padx=4)

        # dates
        self.f_dates = ttk.Frame(self.period_box)
        ttk.Label(self.f_dates, text="От (YYYY-MM-DD):").pack(side="left")
        self.from_var = tk.StringVar(value=cfg.get("date_from", ""))
        ttk.Entry(self.f_dates, textvariable=self.from_var, width=12).pack(
            side="left", padx=(4, 12))
        ttk.Label(self.f_dates, text="До:").pack(side="left")
        self.to_var = tk.StringVar(value=cfg.get("date_to", ""))
        ttk.Entry(self.f_dates, textvariable=self.to_var, width=12).pack(
            side="left", padx=(4, 12))
        ttk.Label(self.f_dates, text="макс:").pack(side="left")
        self.max_var = tk.StringVar(value=str(cfg.get("date_max", 500)))
        ttk.Spinbox(self.f_dates, from_=1, to=DATE_MODE_MAX,
                    textvariable=self.max_var, width=7).pack(side="left", padx=4)

        # offset
        self.f_offset = ttk.Frame(self.period_box)
        ttk.Label(self.f_offset, text="С матча №:").pack(side="left")
        self.off_start_var = tk.StringVar(value=str(cfg.get("off_start", 0)))
        ttk.Spinbox(self.f_offset, from_=0, to=10000,
                    textvariable=self.off_start_var, width=7).pack(side="left", padx=4)
        ttk.Label(self.f_offset, text="по №:").pack(side="left")
        self.off_end_var = tk.StringVar(value=str(cfg.get("off_end", 100)))
        ttk.Spinbox(self.f_offset, from_=1, to=10000,
                    textvariable=self.off_end_var, width=7).pack(side="left", padx=4)
        ttk.Label(self.f_offset, text="(не включая верхнюю границу)").pack(
            side="left", padx=6)

        self._update_period_fields()

        # Кнопки
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, sticky="w", **pad)
        self.run_btn = ttk.Button(btns, text="Получить историю", command=self._run)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(btns, text="Отмена", command=self._cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left", padx=6)
        self.save_btn = ttk.Button(btns, text="Сохранить…",
                                   command=self._save_as, state="disabled")
        self.save_btn.pack(side="left", padx=6)
        self.tsv_btn = ttk.Button(btns, text="Копировать для Google Sheets",
                                  command=self._copy_tsv, state="disabled")
        self.tsv_btn.pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Очистить", command=self._clear).pack(side="left")

        # Лог
        self.text = scrolledtext.ScrolledText(frm, wrap="word", height=18,
                                              font=("Consolas", 10))
        self.text.grid(row=5, column=0, sticky="nsew", **pad)
        frm.rowconfigure(5, weight=1)
        self.text.configure(state="disabled")
        self.text.tag_config("info", foreground="#1a1a1a")
        self.text.tag_config("ok", foreground="#0a7d28")
        self.text.tag_config("err", foreground="#c01818")
        self.text.tag_config("warn", foreground="#b25a00")
        self.text.tag_config("head", foreground="#0a4da0",
                             font=("Consolas", 10, "bold"))
        self.text.tag_config("win", foreground="#0a7d28",
                             font=("Consolas", 10, "bold"))
        self.text.tag_config("lose", foreground="#8a8a8a")
        self.text.tag_config("self", foreground="#0a4da0",
                             font=("Consolas", 10, "bold"))
        self.text.tag_config("party", foreground="#7a1fa2",
                             font=("Consolas", 10, "bold"))
        self.text.tag_config("mate", foreground="#1a6e1a")

    def _update_period_fields(self):
        for f in (self.f_last, self.f_dates, self.f_offset):
            f.pack_forget()
        mode = self.period_var.get()
        if mode == PERIOD_DATES:
            self.f_dates.pack(side="left")
        elif mode == PERIOD_OFFSET:
            self.f_offset.pack(side="left")
        else:
            self.f_last.pack(side="left")

    # ---------- build: Статистика ----------
    def _build_stats_tab(self):
        frm = self.tab_stats
        frm.columnconfigure(0, weight=1)
        pad = {"padx": 6, "pady": 4}

        top = ttk.Frame(frm)
        top.grid(row=0, column=0, sticky="ew", **pad)
        ttk.Button(top, text="Построить по последнему поиску",
                   command=self._build_stats).pack(side="left")
        ttk.Label(top, text="Мин. матчей:").pack(side="left", padx=(16, 4))
        self.min_matches_var = tk.StringVar(value="2")
        ttk.Spinbox(top, from_=1, to=9999, textvariable=self.min_matches_var,
                    width=6, command=self._refresh_stats_view).pack(side="left")
        self.stats_save_btn = ttk.Button(top, text="Сохранить…",
                                         command=self._save_stats, state="disabled")
        self.stats_save_btn.pack(side="left", padx=(16, 6))
        self.stats_tsv_btn = ttk.Button(top, text="Копировать для Google Sheets",
                                        command=self._copy_stats_tsv, state="disabled")
        self.stats_tsv_btn.pack(side="left")

        ttk.Label(frm, text="Частота ников во всех найденных матчах (тиммейты + "
                            "соперники), связь с искомым (★) и средний KDA. "
                            "◆ — вероятная пати:").grid(
            row=1, column=0, sticky="w", **pad)

        cols = ("nickname", "role", "matches", "with", "vs",
                "avg_k", "avg_d", "avg_a", "avg_kda", "avg_kd")
        headers = {"nickname": "Никнейм", "role": "Роль", "matches": "Матчей",
                   "with": "вместе", "vs": "против",
                   "avg_k": "ср. K", "avg_d": "ср. D", "avg_a": "ср. A",
                   "avg_kda": "ср. KDA", "avg_kd": "ср. K/D"}
        widths = {"nickname": 210, "role": 90, "matches": 65, "with": 65,
                  "vs": 65, "avg_k": 60, "avg_d": 60, "avg_a": 60,
                  "avg_kda": 105, "avg_kd": 70}
        wrap = ttk.Frame(frm)
        wrap.grid(row=2, column=0, sticky="nsew", **pad)
        frm.rowconfigure(2, weight=1)
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings")
        for c in cols:
            self.tree.heading(c, text=headers[c],
                              command=lambda col=c: self._sort_stats(col))
            anchor = "w" if c == "nickname" else "center"
            self.tree.column(c, width=widths[c], anchor=anchor,
                             stretch=(c == "nickname"))
        self.tree.tag_configure("self", foreground="#0a4da0")
        self.tree.tag_configure("party", foreground="#7a1fa2")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.stats_status = tk.StringVar(value="Сначала выполните поиск на вкладке «Поиск».")
        ttk.Label(frm, textvariable=self.stats_status, anchor="w").grid(
            row=3, column=0, sticky="w", **pad)

    # ---------- helpers ----------
    def _toggle_key(self):
        self.key_entry.configure(show="" if self.show_key.get() else "•")

    def _log(self, msg, level="info"):
        self.text.configure(state="normal")
        self.text.insert("end", msg + "\n", level)
        self.text.see("end")
        self.text.configure(state="disabled")

    def _set_status(self, msg, level="info"):
        self.status.set(msg)
        colors = {"info": "", "ok": "#0a7d28", "err": "#c01818", "warn": "#b25a00"}
        try:
            self.status_lbl.configure(foreground=colors.get(level, ""))
        except tk.TclError:
            pass

    def _clear(self):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")
        self._set_status("Очищено.")

    def emit(self, kind, *payload):
        self.q.put((kind, payload))

    def _players_list(self):
        raw = self.players_text.get("1.0", "end")
        seen, out = set(), []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            nick = nickname_from_input(line)
            key = nick.lower()
            if key not in seen:
                seen.add(key)
                out.append(nick)
        return out

    def _build_period(self):
        """Собирает dict периода из UI. Бросает ValueError с понятным текстом."""
        mode = self.period_var.get()
        if mode == PERIOD_DATES:
            frm = parse_date_to_unix(self.from_var.get())
            to = parse_date_to_unix(self.to_var.get(), end_of_day=True)
            if frm and to and frm > to:
                raise ValueError("Дата «От» позже даты «До».")
            try:
                cap = max(1, int(self.max_var.get()))
            except ValueError:
                cap = DATE_MODE_MAX
            if not frm and not to:
                raise ValueError("Укажите хотя бы одну дату (От или До).")
            return {"mode": "dates", "from": frm, "to": to, "max": cap}
        if mode == PERIOD_OFFSET:
            try:
                start = max(0, int(self.off_start_var.get()))
                end = int(self.off_end_var.get())
            except ValueError:
                raise ValueError("Номера матчей должны быть числами.")
            if end <= start:
                raise ValueError("«По №» должно быть больше «С матча №».")
            return {"mode": "offset", "start": start, "end": end}
        # last
        try:
            limit = max(1, int(self.limit_var.get()))
        except ValueError:
            limit = 20
            self.limit_var.set("20")
        return {"mode": "last", "limit": limit}

    def _period_desc(self, period):
        if period["mode"] == "dates":
            a = unix_to_str(period["from"]) or "…"
            b = unix_to_str(period["to"]) or "…"
            return f"даты {a} — {b} (до {period['max']})"
        if period["mode"] == "offset":
            return f"матчи №{period['start']}…{period['end'] - 1}"
        return f"последние {period['limit']}"

    # ---------- run / cancel ----------
    def _run(self):
        if self.worker and self.worker.is_alive():
            return
        api_key = self.key_var.get().strip()
        players = self._players_list()
        if not api_key:
            self._set_status("Не введён API-ключ.", "err")
            self._log("Введите API-ключ (получить: developers.faceit.com → Apps).", "err")
            return
        if not players:
            self._set_status("Не указаны игроки.", "err")
            self._log("Укажите хотя бы один ник или ссылку (по одному в строке).", "err")
            return
        try:
            period = self._build_period()
        except ValueError as e:
            self._set_status(str(e), "err")
            self._log(str(e), "err")
            return
        game = self.game_var.get()

        self._save_config()
        self.cancel.clear()
        self.last_results = []
        self.searched_ids = set()
        self.party_nicks = set()
        self.count_var.set("Запросов к API: 0")
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.save_btn.configure(state="disabled")
        self.tsv_btn.configure(state="disabled")
        self._set_status("Запрос...", "info")

        self.worker = threading.Thread(
            target=self._work, args=(api_key, players, game, period), daemon=True)
        self.worker.start()

    def _cancel(self):
        self.cancel.set()
        self._set_status("Отмена...", "warn")

    def _work(self, api_key, players, game, period):
        client = Client(api_key,
                        on_count=lambda n: self.emit("count", n),
                        cancel=self.cancel)
        results = []
        try:
            self.emit("log", f"Игроков: {len(players)} | игра: {game} | "
                             f"период: {self._period_desc(period)}", "head")

            # 1) player_id для каждого игрока
            player_ids = {}
            for raw in players:
                nick = nickname_from_input(raw)
                pid = get_player_id(client, nick)
                player_ids[nick] = pid
                self.emit("log", f"  {nick} → {pid}", "info")
            self.searched_ids = set(player_ids.values())

            # 2) истории → объединённый набор уникальных match_id
            match_dates, order = {}, []
            for nick, pid in player_ids.items():
                self.emit("status", f"История: {nick}...", "info")
                hist = get_history(client, pid, game, period)
                self.emit("log", f"  {nick}: матчей в истории — {len(hist)}", "info")
                for item in hist:
                    mid = item.get("match_id")
                    if mid and mid not in match_dates:
                        match_dates[mid] = item.get("finished_at") or item.get("started_at")
                        order.append(mid)

            total = len(order)
            self.emit("log", f"Уникальных матчей к загрузке: {total}", "head")
            if not total:
                self.emit("status", "Матчи не найдены (проверьте игру/период).", "warn")
                self.emit("finished", [])
                return

            # 3) статистика каждого уникального матча — один раз
            for i, mid in enumerate(order, 1):
                if self.cancel.is_set():
                    self.emit("log", "Прервано пользователем.", "warn")
                    break
                self.emit("status", f"Матч {i}/{total}...", "info")
                m = parse_match(client, mid)
                if m is None:
                    self.emit("log", f"[{i}/{total}] {mid}: статистика недоступна.", "warn")
                    continue
                m["date"] = unix_to_str(match_dates[mid])
                results.append(m)

            self.last_results = results
            self.emit("finished", results)

        except Cancelled:
            self.last_results = results
            self.emit("log", "Прервано пользователем.", "warn")
            self.emit("finished", results)
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else None
            self.emit("log", http_error_message(status), "err")
            self.emit("status", http_error_message(status), "err")
            self.emit("done_error")
        except requests.ConnectionError:
            msg = "Нет соединения с FACEIT API. Проверьте интернет."
            self.emit("log", msg, "err")
            self.emit("status", msg, "err")
            self.emit("done_error")
        except requests.Timeout:
            msg = "Превышено время ожидания ответа FACEIT API."
            self.emit("log", msg, "err")
            self.emit("status", msg, "err")
            self.emit("done_error")
        except ValueError as e:           # игрок не найден, неверный период и т.п.
            self.emit("log", str(e), "err")
            self.emit("status", str(e), "err")
            self.emit("done_error")
        except Exception as e:
            self.emit("log", f"Непредвиденная ошибка: {e}", "err")
            self.emit("log", traceback.format_exc(), "err")
            self.emit("status", "Ошибка (подробности в логе).", "err")
            self.emit("done_error")

    # ---------- queue polling (main thread) ----------
    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(payload[0], payload[1] if len(payload) > 1 else "info")
                elif kind == "status":
                    self._set_status(payload[0], payload[1] if len(payload) > 1 else "info")
                elif kind == "count":
                    self.count_var.set(f"Запросов к API: {payload[0]}")
                elif kind == "finished":
                    res = payload[0] if payload else self.last_results
                    self._finish_ok(res)
                elif kind == "done_error":
                    self._finish_err()
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _render_match(self, m, idx):
        self._log("")
        head = f"── Матч #{idx}  [{m['match_id']}]"
        if m["map"]:
            head += f"  карта: {m['map']}"
        self._log(head, "head")
        self._log(f"   Счёт: {m['score']}", "info")
        s_teams = _searched_team_indices(m, self.searched_ids)
        for ti, team in enumerate(m["teams"]):
            tag = "win" if team["won"] else "lose"
            flag = "WIN " if team["won"] else "LOSE"
            self._log(f"   [{flag}] {team['name']}", tag)
            for p in team["players"]:
                is_s = p.get("player_id") in self.searched_ids
                if is_s:
                    mark, ptag = "★", "self"
                elif p["nickname"] in self.party_nicks and ti in s_teams:
                    mark, ptag = "◆", "party"
                elif ti in s_teams:
                    mark, ptag = "+", "mate"
                else:
                    mark, ptag = " ", "info"
                self._log(f"      {mark} {p['nickname']:<20} KDA {p['kda']:<10} "
                          f"K/D {p['kd_ratio']}", ptag)

    def _finish_ok(self, results):
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")
        if results:
            self.save_btn.configure(state="normal")
            self.tsv_btn.configure(state="normal")
            self._build_stats()        # считает статистику и party_nicks
            for i, m in enumerate(results, 1):
                self._render_match(m, i)
            self._render_party_summary()
            self._log(f"\nГотово. Обработано матчей: {len(results)}", "ok")
            self._set_status(f"Готово: {len(results)} матч(ей).", "ok")
        else:
            self._set_status("Завершено: данных нет.", "warn")

    def _render_party_summary(self):
        party = [s for s in self.last_stats if s["role"] == "пати?"]
        self._log("")
        self._log("── Анализ пати (★ искомый, ◆ вероятная пати) ──", "head")
        if not party:
            self._log("   Постоянных сокомандников не выявлено — похоже на соло-очередь.",
                      "info")
            return
        for s in party:
            self._log(f"   ◆ {s['nickname']:<20} вместе ×{s['with']}  "
                      f"против ×{s['vs']}  ср.KDA {s['avg_kda']}", "party")

    def _finish_err(self):
        self.run_btn.configure(state="normal")
        self.cancel_btn.configure(state="disabled")

    # ---------- статистика: представление ----------
    def _build_stats(self):
        if not self.last_results:
            self.stats_status.set("Нет данных. Сначала выполните поиск.")
            return
        self.last_stats = compute_stats(self.last_results, self.searched_ids)
        self.party_nicks = party_nicknames(self.last_stats)
        self._refresh_stats_view()
        if self.last_stats:
            self.stats_save_btn.configure(state="normal")
            self.stats_tsv_btn.configure(state="normal")

    def _refresh_stats_view(self):
        try:
            min_m = max(1, int(self.min_matches_var.get()))
        except ValueError:
            min_m = 1
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for s in self.last_stats:
            if not s["is_searched"] and s["matches"] < min_m:
                continue
            tag = "self" if s["is_searched"] else ("party" if s["role"] == "пати?" else "")
            self.tree.insert("", "end", tags=(tag,) if tag else (), values=(
                s["nickname"], s["role"], s["matches"], s["with"], s["vs"],
                f"{s['avg_k']:.2f}", f"{s['avg_d']:.2f}", f"{s['avg_a']:.2f}",
                s["avg_kda"], f"{s['avg_kd']:.2f}"))
            shown += 1
        self.stats_status.set(
            f"Уникальных ников: {len(self.last_stats)} | показано (≥{min_m} матчей): "
            f"{shown} | матчей в выборке: {len(self.last_results)}")

    def _sort_stats(self, col):
        key_map = {"nickname": lambda s: s["nickname"].lower(),
                   "role": lambda s: s["role"],
                   "matches": lambda s: s["matches"],
                   "with": lambda s: s["with"], "vs": lambda s: s["vs"],
                   "avg_k": lambda s: s["avg_k"], "avg_d": lambda s: s["avg_d"],
                   "avg_a": lambda s: s["avg_a"],
                   "avg_kda": lambda s: s["avg_k"],
                   "avg_kd": lambda s: s["avg_kd"]}
        prev_col, prev_rev = self._stats_sort
        rev = not prev_rev if prev_col == col else True
        self.last_stats.sort(key=key_map[col], reverse=rev)
        self._stats_sort = (col, rev)
        self._refresh_stats_view()

    # ---------- export: матчи ----------
    def _save_as(self):
        if not self.last_results:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx"), ("CSV", "*.csv"), ("JSON", "*.json")],
            initialfile="faceit_history.xlsx")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".csv":
                export_csv(path, self.last_results, self.searched_ids, self.party_nicks)
            elif ext == ".json":
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.last_results, f, ensure_ascii=False, indent=2)
            else:
                export_xlsx(path, self.last_results, self.searched_ids, self.party_nicks)
            self._set_status(f"Сохранено: {path}", "ok")
            self._log(f"Файл сохранён: {path}", "ok")
        except RuntimeError as e:          # openpyxl не установлен
            self._set_status("Excel недоступен — см. лог.", "err")
            self._log(str(e), "err")
        except OSError as e:
            self._set_status("Не удалось сохранить файл.", "err")
            self._log(f"Ошибка записи: {e}", "err")

    def _copy_tsv(self):
        if not self.last_results:
            return
        tsv = results_to_tsv(self.last_results, self.searched_ids, self.party_nicks)
        self.root.clipboard_clear()
        self.root.clipboard_append(tsv)
        n = len(flatten_rows(self.last_results, self.searched_ids, self.party_nicks))
        self._set_status(f"Скопировано строк: {n}. Вставьте в Google Sheets (Ctrl+V).", "ok")
        self._log("Данные (TSV) скопированы в буфер. Откройте лист Google Sheets "
                  "и вставьте через Ctrl+V — разложится по столбцам.", "ok")

    # ---------- export: статистика ----------
    def _save_stats(self):
        if not self.last_stats:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")],
            initialfile="faceit_stats.csv")
        if not path:
            return
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".json":
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(self.last_stats, f, ensure_ascii=False, indent=2)
            else:
                export_stats_csv(path, self.last_stats)
            self._set_status(f"Статистика сохранена: {path}", "ok")
        except OSError as e:
            self._set_status("Не удалось сохранить файл.", "err")
            self._log(f"Ошибка записи: {e}", "err")

    def _copy_stats_tsv(self):
        if not self.last_stats:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(stats_to_tsv(self.last_stats))
        self._set_status(f"Скопировано ников: {len(self.last_stats)}. "
                         f"Вставьте в Google Sheets (Ctrl+V).", "ok")

    # ---------- config ----------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def _save_config(self):
        data = {
            "api_key": self.key_var.get().strip(),
            "players": self.players_text.get("1.0", "end").strip(),
            "game": self.game_var.get(),
            "period_mode": self.period_var.get(),
            "limit": self.limit_var.get(),
            "date_from": self.from_var.get(),
            "date_to": self.to_var.get(),
            "date_max": self.max_var.get(),
            "off_start": self.off_start_var.get(),
            "off_end": self.off_end_var.get(),
        }
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _on_close(self):
        self._save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")     # на Windows красивее; иначе игнор
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
