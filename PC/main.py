#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_gui.py —— 基于 IPv6 的文件传输 + 文字消息图形界面（Tkinter，仅用标准库）

同一个程序既能当服务器，也能当客户端：
 * 服务器：启动监听，接收别人上传的文件，客户端也能下载这里的文件；
 * 客户端：连接服务器，上传本地文件、下载服务器文件、与对方互发文字。

界面功能
--------
* 进度条 + 百分比 + 已传/总量 + 实时速度 + 剩余时间；
* 每个文件同时显示本机 SHA-256 与对端 SHA-256，并给出「✔ 一致 / ✗ 不一致」结论；
* 文字消息走独立的长连接，传大文件时也能照常聊天；
* 支持取消（保留 .part，之后可续传）、断线续传、同名自动改名、防目录穿越。

运行
----
    python file_gui.py
    python file_gui.py --role client --host 2001:db8::5 --port 9000
    python file_gui.py --role server --port 9000 --dir received

同机演示：开两个窗口，一个点「启动监听」，另一个选客户端连 ::1 即可。
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import file_core as core

APP_TITLE = "IPv6 文件传输工具"
CONFIG_NAME = "file_gui_config.json"
MAX_UI_FILES = 1000
DRAIN_INTERVAL_MS = 60
MAX_EVENTS_PER_TICK = 200
MAX_LOG_LINES = 500

COLORS = {
    "idle": "#9aa0a6",
    "listen": "#1a73e8",
    "online": "#188038",
    "busy": "#f29900",
    "error": "#d93025",
    "muted": "#5f6368",
    "ok": "#188038",
    "bad": "#d93025",
    "run": "#f29900",
    "me": "#1a73e8",
    "peer": "#188038",
    "sys": "#5f6368",
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def enable_dpi_awareness() -> None:
    """Windows 高 DPI 下让字形与窗口尺寸保持一致。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def pick_font(families, candidates, fallback="TkDefaultFont") -> str:
    for name in candidates:
        if name in families:
            return name
    return fallback


def open_in_file_manager(path: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        messagebox.showerror("打开目录失败", str(exc))


def config_path() -> Path:
    return Path(__file__).resolve().with_name(CONFIG_NAME)


def load_config() -> dict:
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    try:
        config_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class App:
    """所有界面状态都只在主线程改动；工作线程通过 self.post() 投递事件。"""

    def __init__(self, root: tk.Tk, args, config: dict) -> None:
        self.root = root
        self.config = config or {}
        self.events: "queue.Queue[dict]" = queue.Queue()

        families = set(tkfont.families(root))
        self.ui_font = pick_font(families, ("Microsoft YaHei UI", "Microsoft YaHei",
                                            "Segoe UI", "PingFang SC", "Noto Sans CJK SC"))
        self.mono_font = pick_font(families, ("Cascadia Mono", "Consolas", "JetBrains Mono",
                                              "Menlo", "DejaVu Sans Mono"))
        try:
            self.scale = max(1.0, round(root.winfo_fpixels("1i") / 96.0, 2))
        except tk.TclError:
            self.scale = 1.0

        # 连接状态
        self.role = tk.StringVar(value=self.config.get("role", "server"))
        self.port_var = tk.StringVar(value=str(self.config.get("port", core.DEFAULT_PORT)))
        self.host_var = tk.StringVar(value=self.config.get("host", "::1"))
        self.zone_var = tk.StringVar(value=str(self.config.get("zone", "")))
        self.password_var = tk.StringVar(value=str(self.config.get("password", "")))
        self.recv_dir_var = tk.StringVar(value=self.config.get("recv_dir", "received"))
        client_recv = self.config.get("client_recv_dir") or self.config.get("save_dir") or "downloads"
        self.client_recv_dir_var = tk.StringVar(value=client_recv)
        self.status_text = tk.StringVar(value="未连接")
        self.peer_text = tk.StringVar(value="对端：—")
        self.task_text = tk.StringVar(value="空闲")
        self.stats_text = tk.StringVar(value="等待任务")
        self.hash_local = tk.StringVar(value="")
        self.hash_peer = tk.StringVar(value="")
        self.hash_verdict = tk.StringVar(value="选择一条传输记录查看校验结果")
        self.char_count = tk.StringVar(value=f"0/{core.MAX_TEXT_CHARS}")
        self.pending_summary = tk.StringVar(value="还没有选择文件")
        self.addr_detail = tk.StringVar(value="")

        self.state = "idle"             # idle / listening / connected / file_only / busy
        self.file_only_mode = False
        self.server_sock = None
        self.server_thread = None
        self.server_stop = threading.Event()
        self.name_locks = core.NameLocks()
        self.client_id = uuid.uuid4().hex[:12]   # 本实例身份，文件连接与控制连接共用
        self.controls: dict = {}        # key -> ControlLink（服务器可多个，客户端最多一个）
        self.control_peers: dict = {}   # key -> 显示用对端地址
        aliases = self.config.get("client_aliases") or {}
        self.client_aliases: dict = {}
        if isinstance(aliases, dict):
            for address, alias in aliases.items():
                if isinstance(alias, str) and alias.strip():
                    self.client_aliases[str(address)] = alias.strip()[:32]
        self.peer_keys: list = []       # 在线列表顺序：索引 -> key
        self.peer_addr = ""
        self.transfer = None            # 当前批次：{"cancel": Event, "sock": sock, "limit_rate": int}
        self.push_queue: list = []      # 收到推送但还没开始下载的 (文件名, token)
        self.push_registry: dict = {}   # 服务器：token -> 本地文件绝对路径
        self.rows: dict = {}
        self.server_rows: dict = {}
        self.seq = 0
        self.pending_files: list = []
        self.current_progress_row = None
        self.closing = False
        self._table_fit_job = None

        self._build_ui()
        self._configure_style()
        self._apply_role(initial=True)
        self._schedule_file_table_fit(150)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(DRAIN_INTERVAL_MS, self._drain)
        self._set_state("idle")
        try:
            self._fill_local_addresses(int(self.port_var.get()))
        except ValueError:
            self._fill_local_addresses(core.DEFAULT_PORT)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self.root.after(3000, self._auto_refresh_tick)
        self.log("界面已就绪。服务器模式点「启动监听」；客户端模式填地址后点「连接」。")

    # ------------------------------------------------------------------ #
    # 构建界面
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(self.px(1180), int(screen_w * 0.94))
        height = min(self.px(780), int(screen_h * 0.92))
        geometry = self.config.get("geometry") or f"{width}x{height}"
        self.root.geometry(geometry)
        self.root.minsize(min(self.px(1000), width), min(self.px(700), height))
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        # -- 顶部 -------------------------------------------------------
        header = ttk.Frame(self.root, padding=(16, 12, 16, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(2, weight=1)

        ttk.Label(header, text=APP_TITLE, font=(self.ui_font, 16, "bold")).grid(row=0, column=0, sticky="w")
        self.dot = tk.Label(header, text="●", fg=COLORS["idle"], font=(self.ui_font, 13))
        self.dot.grid(row=0, column=1, padx=(18, 4))
        ttk.Label(header, textvariable=self.status_text, font=(self.ui_font, 10)).grid(row=0, column=2, sticky="w")
        ttk.Label(header, textvariable=self.peer_text, foreground=COLORS["muted"],
                  font=(self.ui_font, 9)).grid(row=0, column=3, sticky="e")

        # -- 主体 -------------------------------------------------------
        body = ttk.Frame(self.root, padding=(16, 6, 16, 4))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsw", padx=(0, 14))
        left.columnconfigure(0, minsize=self.px(318))

        self._build_connection_panel(left)
        self._build_address_panel(left)

        self.notebook = ttk.Notebook(body)
        self.notebook.grid(row=0, column=1, sticky="nsew")
        self.tab_file = ttk.Frame(self.notebook, padding=10)
        self.tab_text = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.tab_file, text="  文件传输  ")
        self.notebook.add(self.tab_text, text="  文字消息  ")
        self._build_file_tab()
        self._build_text_tab()

        # -- 日志 -------------------------------------------------------
        log_frame = ttk.LabelFrame(self.root, text="日志", padding=(8, 4))
        log_frame.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 12))
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, height=8, wrap="none", state="disabled",
                                font=(self.mono_font, 8), background="#fbfbfb",
                                relief="flat", borderwidth=0)
        self.log_text.grid(row=0, column=0, sticky="ew")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

    def _build_connection_panel(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="连接", padding=10)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(1, weight=1)

        ttk.Radiobutton(box, text="作为服务器（让别人连我）", value="server",
                        variable=self.role, command=self._on_role_change).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Radiobutton(box, text="作为客户端（连接别人）", value="client",
                        variable=self.role, command=self._on_role_change).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(0, 8))

        # 服务器设置
        self.server_frame = ttk.Frame(box)
        self.server_frame.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.server_frame.columnconfigure(1, weight=1)
        ttk.Label(self.server_frame, text="监听端口").grid(row=0, column=0, sticky="w", pady=2)
        self.port_entry = ttk.Entry(self.server_frame, textvariable=self.port_var, width=8)
        self.port_entry.grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(self.server_frame, text="接收目录").grid(row=1, column=0, sticky="w", pady=2)
        self.recv_entry = ttk.Entry(self.server_frame, textvariable=self.recv_dir_var)
        self.recv_entry.grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Button(self.server_frame, text="浏览…", width=6,
                   command=self._choose_recv_dir).grid(row=1, column=2, padx=(6, 0))
        ttk.Label(self.server_frame, text="在线客户端").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.alias_btn = ttk.Button(self.server_frame, text="设置别名", width=7,
                                    command=self._set_alias_for_selected)
        self.alias_btn.grid(row=2, column=2, sticky="e", pady=(8, 0))
        self.online_list = tk.Listbox(self.server_frame, height=3, selectmode="extended",
                                      exportselection=False, font=(self.mono_font, 9),
                                      activestyle="none")
        self.online_list.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(2, 0))
        self.online_list.bind("<<ListboxSelect>>", lambda _e: self._refresh_buttons())
        self.online_list.bind("<Double-Button-1>",
                              lambda _e: self._set_alias_for_selected())
        ttk.Label(self.server_frame, text="双击客户端可设别名；不选=群发，选中=只发给选中项。",
                  foreground=COLORS["muted"], font=(self.ui_font, 8)).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(2, 0))
        ttk.Label(self.server_frame, text="连接密码").grid(
            row=5, column=0, sticky="w", pady=(6, 0))
        self.server_password_entry = ttk.Entry(self.server_frame,
                                               textvariable=self.password_var, show="*")
        self.server_password_entry.grid(row=5, column=1, sticky="ew", pady=(6, 0))
        ttk.Label(self.server_frame, text="留空=不验证", foreground=COLORS["muted"],
                  font=(self.ui_font, 8)).grid(
            row=5, column=2, sticky="e", padx=(4, 0), pady=(6, 0))
        self.start_btn = ttk.Button(self.server_frame, text="启动监听", command=self.start_server)
        self.start_btn.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.stop_btn = ttk.Button(self.server_frame, text="停止监听", command=self.stop_server)
        self.stop_btn.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(4, 0))

        # 客户端设置
        self.client_frame = ttk.Frame(box)
        self.client_frame.grid(row=3, column=0, columnspan=3, sticky="ew")
        self.client_frame.columnconfigure(1, weight=1)
        ttk.Label(self.client_frame, text="服务器 IPv6").grid(row=0, column=0, sticky="w", pady=2)
        self.host_entry = ttk.Entry(self.client_frame, textvariable=self.host_var)
        self.host_entry.grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(self.client_frame, text="端口").grid(row=1, column=0, sticky="w", pady=2)
        self.client_port_entry = ttk.Entry(self.client_frame, textvariable=self.port_var, width=8)
        self.client_port_entry.grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(self.client_frame, text="接口索引").grid(row=2, column=0, sticky="w", pady=2)
        self.zone_entry = ttk.Entry(self.client_frame, textvariable=self.zone_var, width=8)
        self.zone_entry.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(self.client_frame, text="链路本地地址需填接口索引（如 12）；也可用下方「导入配置」粘贴连接串",
                  foreground=COLORS["muted"], font=(self.ui_font, 8), wraplength=296).grid(
            row=3, column=0, columnspan=3, sticky="w")
        ttk.Label(self.client_frame, text="接收目录").grid(row=4, column=0, sticky="w", pady=2)
        self.client_recv_entry = ttk.Entry(self.client_frame, textvariable=self.client_recv_dir_var)
        self.client_recv_entry.grid(row=4, column=1, sticky="ew", pady=2)
        ttk.Button(self.client_frame, text="浏览…", width=6,
                   command=self._choose_client_recv_dir).grid(row=4, column=2, padx=(6, 0))
        ttk.Label(self.client_frame, text="连接密码").grid(row=5, column=0, sticky="w", pady=2)
        self.client_password_entry = ttk.Entry(self.client_frame,
                                               textvariable=self.password_var, show="*")
        self.client_password_entry.grid(row=5, column=1, sticky="ew", pady=2)
        self.connect_btn = ttk.Button(self.client_frame, text="连接", command=self.connect_peer)
        self.connect_btn.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(8, 0))
        self.disconnect_btn = ttk.Button(self.client_frame, text="断开", command=self.disconnect_peer)
        self.disconnect_btn.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))

        ttk.Label(box, text="提示：首次监听时请在防火墙弹窗中允许访问。",
                  foreground=COLORS["muted"], font=(self.ui_font, 8), wraplength=296).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

    def _build_address_panel(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="本机 IPv6 地址", padding=8)
        box.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        box.columnconfigure(0, weight=1)
        self.addr_list = tk.Listbox(box, height=5, font=(self.mono_font, 9),
                                    activestyle="none", exportselection=False)
        self.addr_list.grid(row=0, column=0, sticky="ew")
        addr_scroll = ttk.Scrollbar(box, orient="vertical", command=self.addr_list.yview)
        addr_scroll.grid(row=0, column=1, sticky="ns")
        self.addr_list.configure(yscrollcommand=addr_scroll.set)
        addr_hscroll = ttk.Scrollbar(box, orient="horizontal", command=self.addr_list.xview)
        addr_hscroll.grid(row=1, column=0, columnspan=2, sticky="ew")
        self.addr_list.configure(xscrollcommand=addr_hscroll.set)
        self.addr_list.bind("<<ListboxSelect>>", lambda _e: self._on_address_select())
        buttons = ttk.Frame(box)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        buttons.columnconfigure(0, weight=1)
        buttons.columnconfigure(1, weight=1)
        buttons.columnconfigure(2, weight=1)
        ttk.Button(buttons, text="复制选中地址", command=self._copy_address).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(buttons, text="刷新地址", command=self._refresh_local_addresses).grid(
            row=0, column=1, sticky="ew", padx=3)
        self.config_btn = ttk.Button(buttons, text="分享配置", command=self._on_config_button)
        self.config_btn.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        ttk.Label(box, textvariable=self.addr_detail, foreground=COLORS["muted"],
                  font=(self.ui_font, 8), wraplength=300, justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.addr_hint = ttk.Label(box, text="启动监听后自动列出。", foreground=COLORS["muted"],
                                   font=(self.ui_font, 8), wraplength=300)
        self.addr_hint.grid(row=4, column=0, columnspan=2, sticky="w", pady=(2, 0))
        self.address_entries = []

    def _build_file_tab(self) -> None:
        tab = self.tab_file
        tab.columnconfigure(0, weight=4)
        tab.columnconfigure(1, weight=3)
        tab.rowconfigure(3, weight=1)

        # 工具栏
        toolbar = ttk.Frame(tab)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.choose_btn = ttk.Button(toolbar, text="选择文件…", command=self.choose_files)
        self.choose_btn.pack(side="left")
        self.upload_btn = ttk.Button(toolbar, text="开始上传", command=self.start_send)
        self.upload_btn.pack(side="left", padx=(6, 0))

        # 待发送文件（两种角色共用）
        self.pending_box = ttk.LabelFrame(tab, text="待发送文件", padding=(8, 4))
        self.pending_box.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.pending_box.columnconfigure(0, weight=1)
        pending_head = ttk.Frame(self.pending_box)
        pending_head.grid(row=0, column=0, columnspan=2, sticky="ew")
        ttk.Label(pending_head, textvariable=self.pending_summary,
                  font=(self.ui_font, 9)).pack(side="left")
        self.pending_clear_btn = ttk.Button(pending_head, text="清空选择",
                                            command=self._clear_pending)
        self.pending_clear_btn.pack(side="right")
        self.pending_remove_btn = ttk.Button(pending_head, text="移除选中",
                                             command=self._remove_pending_selected)
        self.pending_remove_btn.pack(side="right", padx=(0, 6))
        pcols = ("name", "size", "dir")
        self.pending_tree = ttk.Treeview(self.pending_box, columns=pcols, show="headings", height=3)
        for key, title, width, anchor in (
            ("name", "名称", 160, "w"),
            ("size", "大小", 78, "e"),
            ("dir", "所在目录", 240, "w"),
        ):
            self.pending_tree.heading(key, text=title)
            self.pending_tree.column(key, width=self.px(width), anchor=anchor,
                                     stretch=(key in ("name", "dir")))
        self.pending_tree.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        pending_scroll = ttk.Scrollbar(self.pending_box, orient="vertical",
                                       command=self.pending_tree.yview)
        pending_scroll.grid(row=1, column=1, sticky="ns", pady=(2, 0))
        self.pending_tree.configure(yscrollcommand=pending_scroll.set)
        self.pending_tree.bind("<<TreeviewSelect>>", lambda _e: self._refresh_buttons())

        # 进度
        prog_box = ttk.LabelFrame(tab, text="当前任务", padding=(10, 4))
        prog_box.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        prog_box.columnconfigure(0, weight=1)
        ttk.Label(prog_box, textvariable=self.task_text, font=(self.ui_font, 10, "bold")).grid(
            row=0, column=0, sticky="w")
        self.cancel_btn = ttk.Button(prog_box, text="取消传输", command=self.cancel_transfer, width=10)
        self.cancel_btn.grid(row=0, column=1, rowspan=2, sticky="e", padx=(8, 0))
        self.progress = ttk.Progressbar(prog_box, maximum=1000, value=0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(3, 2))
        ttk.Label(prog_box, textvariable=self.stats_text, font=(self.mono_font, 9),
                  foreground=COLORS["muted"]).grid(row=2, column=0, sticky="w")

        # 传输记录
        hist_box = ttk.LabelFrame(tab, text="传输记录", padding=(6, 4))
        hist_box.grid(row=3, column=0, sticky="nsew", padx=(0, 8))
        hist_box.columnconfigure(0, weight=1)
        hist_box.rowconfigure(0, weight=1)
        columns = ("time", "dir", "peer", "name", "size", "status", "check")
        self.history = ttk.Treeview(hist_box, columns=columns, show="headings", height=6,
                                    selectmode="browse")
        for key, title, width, anchor in (
            ("time", "时间", 54, "w"),
            ("dir", "方向", 40, "center"),
            ("peer", "对端", 150, "w"),
            ("name", "文件", 70, "w"),
            ("size", "大小", 62, "e"),
            ("status", "状态", 74, "center"),
            ("check", "校验", 40, "center"),
        ):
            self.history.heading(key, text=title)
            self.history.column(key, width=self.px(width), anchor=anchor,
                                stretch=(key == "name"))
        self.history.grid(row=0, column=0, sticky="nsew")
        hist_scroll = ttk.Scrollbar(hist_box, orient="vertical", command=self.history.yview)
        hist_scroll.grid(row=0, column=1, sticky="ns")
        hist_xscroll = ttk.Scrollbar(hist_box, orient="horizontal",
                                     command=self.history.xview)
        hist_xscroll.grid(row=1, column=0, sticky="ew")
        self.history.configure(yscrollcommand=hist_scroll.set,
                               xscrollcommand=hist_xscroll.set)
        self.history.tag_configure("ok", foreground=COLORS["ok"])
        self.history.tag_configure("bad", foreground=COLORS["bad"])
        self.history.tag_configure("run", foreground=COLORS["run"])
        self.history.tag_configure("muted", foreground=COLORS["muted"])
        self.history.bind("<<TreeviewSelect>>", lambda _e: self._show_hash_detail())
        self.history.bind("<Configure>", lambda _e: self._schedule_file_table_fit())

        # 本机接收目录文件列表（两种角色共用）
        remote_box = ttk.LabelFrame(tab, text="接收目录中的文件", padding=(6, 4))
        remote_box.grid(row=3, column=1, sticky="nsew")
        remote_box.columnconfigure(0, weight=1)
        remote_box.rowconfigure(0, weight=1)
        self.remote_caption = remote_box
        rcols = ("name", "size", "mtime")
        self.remote_tree = ttk.Treeview(remote_box, columns=rcols, show="headings", height=6)
        for key, title, width, anchor in (
            ("name", "名称", 80, "w"),
            ("size", "大小", 56, "e"),
            ("mtime", "修改时间", 130, "w"),
        ):
            self.remote_tree.heading(key, text=title)
            self.remote_tree.column(key, width=self.px(width), anchor=anchor,
                                    stretch=(key == "name"))
        self.remote_tree.grid(row=0, column=0, sticky="nsew")
        remote_scroll = ttk.Scrollbar(remote_box, orient="vertical", command=self.remote_tree.yview)
        remote_scroll.grid(row=0, column=1, sticky="ns")
        remote_xscroll = ttk.Scrollbar(remote_box, orient="horizontal",
                                       command=self.remote_tree.xview)
        remote_xscroll.grid(row=1, column=0, sticky="ew")
        self.remote_tree.configure(yscrollcommand=remote_scroll.set,
                                   xscrollcommand=remote_xscroll.set)
        self.remote_tree.bind("<Configure>",
                              lambda _e: self._schedule_file_table_fit())
        self.receive_open_btn = ttk.Button(remote_box, text="打开接收文件夹",
                                           command=self._open_recv_dir)
        self.receive_open_btn.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        # 哈希详情
        hash_box = ttk.LabelFrame(tab, text="哈希详情（选中一条记录）", padding=(10, 4))
        hash_box.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        hash_box.columnconfigure(1, weight=1)
        for row, (label, var) in enumerate((("本机 SHA-256", self.hash_local),
                                            ("对端 SHA-256", self.hash_peer))):
            ttk.Label(hash_box, text=label).grid(row=row, column=0, sticky="w", pady=1)
            entry = tk.Entry(hash_box, textvariable=var, state="readonly",
                             font=(self.mono_font, 9), relief="flat",
                             readonlybackground="#f5f5f5")
            entry.grid(row=row, column=1, sticky="ew", padx=(8, 6), pady=1)
            ttk.Button(hash_box, text="复制", width=5,
                       command=lambda v=var: self._copy_text(v.get())).grid(row=row, column=2, pady=1)
        ttk.Label(hash_box, text="结论").grid(row=2, column=0, sticky="w", pady=(2, 0))
        self.verdict_label = tk.Label(hash_box, textvariable=self.hash_verdict, anchor="w",
                                      fg=COLORS["muted"], font=(self.ui_font, 10, "bold"))
        self.verdict_label.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(2, 0))

    # ------------------------------------------------------------------ #
    # 表格列宽 / 横向滚动
    # ------------------------------------------------------------------ #
    def _schedule_file_table_fit(self, delay: int = 80) -> None:
        if self.closing:
            return
        if self._table_fit_job is not None:
            return
        try:
            self._table_fit_job = self.root.after(delay, self._run_file_table_fit)
        except tk.TclError:
            self._table_fit_job = None

    def _run_file_table_fit(self) -> None:
        self._table_fit_job = None
        try:
            self._fit_file_tables()
        except tk.TclError:
            pass

    def _fit_file_tables(self) -> None:
        self._fit_history_columns()
        self._fit_remote_columns()

    def _fit_history_columns(self) -> None:
        if not hasattr(self, "history"):
            return
        try:
            width = self.history.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            return
        try:
            measure = tkfont.Font(root=self.root, font=(self.ui_font, 9))
        except tk.TclError:
            measure = None
        longest_name = longest_peer = 0
        if measure is not None:
            for item in self.history.get_children():
                longest_name = max(longest_name,
                                   measure.measure(str(self.history.set(item, "name"))) + self.px(20))
                longest_peer = max(longest_peer,
                                   measure.measure(str(self.history.set(item, "peer"))) + self.px(18))
        time_w = self.px(54)
        dir_w = self.px(40)
        size_w = self.px(62)
        status_w = self.px(74)
        check_w = self.px(40)
        peer_w = max(self.px(120), min(longest_peer, self.px(340)))
        name_w = max(self.px(160), min(longest_name, self.px(1400)))
        fixed = time_w + dir_w + peer_w + size_w + status_w + check_w
        if name_w + fixed < width:
            name_w += width - (name_w + fixed)
        for key, value, anchor in (
            ("time", time_w, "w"),
            ("dir", dir_w, "center"),
            ("peer", peer_w, "w"),
            ("name", name_w, "w"),
            ("size", size_w, "e"),
            ("status", status_w, "center"),
            ("check", check_w, "center"),
        ):
            self.history.column(key, width=value, minwidth=self.px(30),
                                anchor=anchor, stretch=False)

    def _fit_remote_columns(self) -> None:
        if not hasattr(self, "remote_tree"):
            return
        try:
            width = self.remote_tree.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            return
        try:
            measure = tkfont.Font(root=self.root, font=(self.ui_font, 9))
        except tk.TclError:
            measure = None
        longest = 0
        if measure is not None:
            for item in self.remote_tree.get_children():
                longest = max(longest,
                              measure.measure(str(self.remote_tree.set(item, "name"))) + self.px(20))
        size_w = self.px(70)
        mtime_w = self.px(120)
        name_w = max(self.px(180), min(longest, self.px(1400)))
        fixed = size_w + mtime_w
        if name_w + fixed < width:
            name_w += width - (name_w + fixed)
        for key, value, anchor in (
            ("name", name_w, "w"),
            ("size", size_w, "e"),
            ("mtime", mtime_w, "w"),
        ):
            self.remote_tree.column(key, width=value, minwidth=self.px(50),
                                    anchor=anchor, stretch=False)

    def _build_text_tab(self) -> None:
        tab = self.tab_text
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        self.chat = tk.Text(tab, wrap="word", state="disabled", font=(self.ui_font, 10),
                            background="#ffffff", relief="flat", padx=10, pady=8)
        self.chat.grid(row=0, column=0, sticky="nsew")
        chat_scroll = ttk.Scrollbar(tab, orient="vertical", command=self.chat.yview)
        chat_scroll.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=chat_scroll.set)
        self.chat.tag_configure("me", foreground=COLORS["me"])
        self.chat.tag_configure("peer", foreground=COLORS["peer"])
        self.chat.tag_configure("sys", foreground=COLORS["sys"])
        self.chat.tag_configure("stamp", foreground="#9aa0a6")

        input_frame = ttk.Frame(tab)
        input_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        input_frame.columnconfigure(0, weight=1)
        self.input = tk.Text(input_frame, height=3, wrap="word", font=(self.ui_font, 10),
                             relief="solid", borderwidth=1, padx=8, pady=6)
        self.input.grid(row=0, column=0, sticky="ew")
        self.input.bind("<Return>", self._on_return)
        self.input.bind("<KeyRelease>", lambda _e: self._update_char_count())
        self.send_btn = ttk.Button(input_frame, text="发送（Enter）", command=self.send_text)
        self.send_btn.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        ttk.Label(input_frame, textvariable=self.char_count, foreground=COLORS["muted"],
                  font=(self.ui_font, 8)).grid(row=1, column=0, sticky="e", pady=(2, 0))
        self.text_hint = ttk.Label(tab, text="未连接：连接对方后即可发送文字消息。",
                                   foreground=COLORS["muted"], font=(self.ui_font, 9))
        self.text_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("vista", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        line_height = tkfont.Font(family=self.ui_font, size=9).metrics("linespace")
        style.configure(".", font=(self.ui_font, 10))
        style.configure("TButton", padding=(8, 4))
        style.configure("TLabel", font=(self.ui_font, 10))
        style.configure("TLabelframe.Label", font=(self.ui_font, 10, "bold"))
        style.configure("Treeview", font=(self.ui_font, 9), rowheight=line_height + 6)
        style.configure("Treeview.Heading", font=(self.ui_font, 9, "bold"))

    def px(self, value: float) -> int:
        """按系统 DPI 缩放像素尺寸（字体点值本身已随 DPI 放大）。"""
        return int(round(value * self.scale))

    # ------------------------------------------------------------------ #
    # 事件泵
    # ------------------------------------------------------------------ #
    def post(self, event_type: str, **payload) -> None:
        payload["type"] = event_type
        self.events.put(payload)

    def _drain(self) -> None:
        if self.closing:
            return
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        for _ in range(MAX_EVENTS_PER_TICK):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(event)
            except Exception as exc:          # 单条事件出错不应拖垮界面
                try:
                    self.log(f"[界面异常] {exc}")
                except tk.TclError:
                    return
                print(f"[界面异常] {type(exc).__name__}: {exc}", file=sys.stderr)
        if not self.closing:
            try:
                self.root.after(DRAIN_INTERVAL_MS, self._drain)
            except tk.TclError:
                pass

    def _handle(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "log":
            self.log(event.get("text", ""))
        elif etype == "state":
            self._set_state(event.get("state", "idle"), event.get("peer"))
        elif etype == "text":
            self._append_chat(event.get("text", ""), from_peer=bool(event.get("from_peer", True)),
                              ts=event.get("ts"), sender=event.get("peer"))
        elif etype == "notice":
            text = event.get("text", "")
            self._append_chat(text, from_peer=True, ts=event.get("ts"), system=True)
            self.log(f"收到系统通知：{text}")
        elif etype == "push":
            self._handle_push(event)
        elif etype == "control_ready":
            self._adopt_control(event.get("link"), event.get("key", ""), event.get("peer", ""))
        elif etype == "control_down":
            self._adopt_control(None, event.get("key", ""), event.get("peer", ""))
        elif etype == "start":
            self._handle_start(event)
        elif etype == "progress":
            self._handle_progress(event)
        elif etype == "stage":
            self._handle_stage(event)
        elif etype == "done":
            self._handle_done(event)
        elif etype == "failed":
            self._handle_failed(event)
        elif etype == "cancelled":
            self._handle_cancelled(event)
        elif etype == "server_start":
            self._handle_server_start(event)
        elif etype == "server_progress":
            self._handle_server_progress(event)
        elif etype == "server_done":
            self._handle_server_done(event)
        elif etype == "server_failed":
            self._handle_server_failed(event)
        elif etype == "server_session_end":
            self._handle_server_session_end(event)
        elif etype == "connect_failed":
            message = event.get("message", "")
            self.log(f"连接失败：{message}")
            messagebox.showerror("连接失败", message)
        elif etype == "transfer_end":
            self._finish_batch()
        elif etype == "refresh_local":
            self._refresh_local_list()
        else:
            self.log(f"未知事件：{etype}")

    # ------------------------------------------------------------------ #
    # 状态与日志
    # ------------------------------------------------------------------ #
    def _set_state(self, state: str, peer: str = None) -> None:
        self.state = state
        if peer is not None:
            self.peer_addr = peer
            self.peer_text.set(f"对端：{peer}" if peer else "对端：—")
        color = {
            "idle": COLORS["idle"],
            "listening": COLORS["listen"],
            "connected": COLORS["online"],
            "file_only": COLORS["online"],
            "busy": COLORS["busy"],
            "error": COLORS["error"],
        }.get(state, COLORS["idle"])
        text = {
            "idle": "未连接",
            "connecting": "连接中…",
            "listening": "监听中",
            "connected": "已连接",
            "file_only": "已连接（仅文件）",
            "busy": "传输中",
            "error": "出错",
        }.get(state, state)
        if self.role.get() == "server" and state in ("listening", "connected"):
            count = len(self.controls)
            text = "监听中" + (f" · {count} 个客户端在线" if count else "")
        self.dot.config(fg=color)
        self.status_text.set(text)
        self._refresh_buttons()

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        lines = int(self.log_text.index("end-1c").split(".")[0])
        if lines > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _refresh_buttons(self) -> None:
        role = self.role.get()
        busy = self.state == "busy"
        locked = self.state in ("busy", "connecting")
        has_control = bool(self.controls)
        connected = has_control or self.state == "file_only"
        listening = self.server_sock is not None

        def enable(widget, flag):
            widget.state(["!disabled"] if flag else ["disabled"])

        if role == "client":
            enable(self.choose_btn, connected and not locked)
            enable(self.upload_btn, connected and not locked and bool(self.pending_files))
            enable(self.connect_btn, not connected and not locked)
            enable(self.disconnect_btn, connected)
            enable(self.start_btn, False)
            enable(self.stop_btn, False)
        else:
            enable(self.choose_btn, listening and not locked)
            enable(self.upload_btn, listening and not locked and has_control
                   and bool(self.pending_files))
            enable(self.start_btn, not listening and not locked)
            enable(self.stop_btn, listening and not locked)
            enable(self.connect_btn, False)
            enable(self.disconnect_btn, False)
        enable(self.pending_remove_btn, not locked and bool(self.pending_tree.selection()))
        enable(self.pending_clear_btn, not locked and bool(self.pending_files))
        enable(self.receive_open_btn, True)
        enable(self.cancel_btn, busy)
        enable(self.send_btn, has_control)
        self._update_text_hint()
        if has_control:
            if str(self.input.cget("state")) == "disabled":
                self.input.configure(state="normal")
        elif str(self.input.cget("state")) != "disabled":
            self.input.configure(state="disabled")

    def _update_text_hint(self) -> None:
        if self.role.get() == "server":
            count = len(self.controls)
            if count:
                self.text_hint.configure(
                    text=f"{count} 个客户端在线：不选中则群发，选中则只发给选中项。")
            else:
                self.text_hint.configure(text="还没有客户端连接；客户端连接后即可收发消息。")
        else:
            if self.controls:
                self.text_hint.configure(
                    text=f"已连接 {self.peer_addr}，Enter 发送，Shift+Enter 换行。")
            elif self.file_only_mode:
                self.text_hint.configure(text="仅文件模式：对端是命令行服务器，不支持文字消息。")
            else:
                self.text_hint.configure(text="未连接：连接对方后即可发送文字消息。")

    # ------------------------------------------------------------------ #
    # 角色切换
    # ------------------------------------------------------------------ #
    def _apply_role(self, initial: bool = False) -> None:
        is_server = self.role.get() == "server"
        if is_server:
            self.client_frame.grid_remove()
            self.server_frame.grid()
            self.upload_btn.configure(text="开始发送")
        else:
            self.server_frame.grid_remove()
            self.client_frame.grid()
            self.upload_btn.configure(text="开始上传")
        if hasattr(self, "config_btn"):
            self.config_btn.configure(text="分享配置" if is_server else "导入配置")
        self.pending_box.grid()
        self._layout_toolbar(is_server)
        self._refresh_peers_view()
        self._recv_signature = None
        self._refresh_local_list()
        if not initial:
            try:
                self._fill_local_addresses(int(self.port_var.get()))
            except ValueError:
                self._fill_local_addresses(core.DEFAULT_PORT)
            self.log("已切换到服务器模式。" if is_server else "已切换到客户端模式。")
        self._refresh_buttons()

    def _layout_toolbar(self, is_server: bool) -> None:
        for button in (self.choose_btn, self.upload_btn):
            button.pack_forget()
        self.choose_btn.pack(side="left")
        self.upload_btn.pack(side="left", padx=(6, 0))

    def _on_role_change(self) -> None:
        if self.state == "busy":
            messagebox.showinfo("正在传输", "请等待当前传输结束或取消后再切换角色。")
            self.role.set("server" if self.role.get() == "client" else "client")
            return
        self.stop_server()
        self.disconnect_peer()
        self._apply_role()

    # ------------------------------------------------------------------ #
    # 服务器模式
    # ------------------------------------------------------------------ #
    def start_server(self) -> None:
        if self.server_sock is not None:
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("端口无效", "监听端口必须是 1..65535 的整数")
            return
        if not (1 <= port <= 65535):
            messagebox.showerror("端口无效", "监听端口必须是 1..65535 的整数")
            return
        recv_dir = self.recv_dir_var.get().strip() or "received"
        password = self.password_var.get()
        if len(password) > core.MAX_PASSWORD_CHARS:
            messagebox.showerror("密码太长",
                                 f"连接密码最多 {core.MAX_PASSWORD_CHARS} 个字符")
            return
        try:
            os.makedirs(recv_dir, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("接收目录不可用", str(exc))
            return
        try:
            sock = core.make_listener("::", port, False)
        except OSError as exc:
            messagebox.showerror("监听失败", f"无法监听 [::]:{port}\n{exc}\n\n"
                                             "请检查端口是否被占用、防火墙是否放行。")
            self.log(f"监听失败：{exc}")
            return

        self.server_sock = sock
        self.server_stop.clear()
        self.push_registry.clear()
        self._recv_signature = None
        self.server_thread = threading.Thread(target=self._accept_loop, args=(sock,),
                                              name="gui-accept", daemon=True)
        self.server_thread.start()
        self._set_state("listening")
        self.log(f"已启动监听 [::]:{port}，接收目录：{os.path.abspath(recv_dir)}")
        if password:
            self.log("已启用连接密码，客户端必须填写相同密码才能连接。")
        self._fill_local_addresses(port)
        self._refresh_local_list()

    def stop_server(self) -> None:
        if self.server_sock is None:
            return
        self.server_stop.set()
        try:
            self.server_sock.close()
        except OSError:
            pass
        self.server_sock = None
        self.push_registry.clear()
        self._recv_signature = None
        for link in list(self.controls.values()):
            try:
                link.close()
            except OSError:
                pass
        self.controls.clear()
        self.control_peers.clear()
        self._refresh_peers_view()
        self._set_state("idle")
        self.log("已停止监听。")

    def _accept_loop(self, sock: socket.socket) -> None:
        while not self.server_stop.is_set():
            try:
                client, addr = sock.accept()
            except OSError:
                break
            if self.server_stop.is_set():
                try:
                    client.close()
                except OSError:
                    pass
                break
            threading.Thread(target=self._serve_client, args=(client, addr),
                             name="gui-serve", daemon=True).start()

    def _serve_client(self, client: socket.socket, addr) -> None:
        peer = core.format_peer(addr)
        conn = core.Conn(client)
        keep_socket = False
        try:
            hello = core.read_hello(conn)
            client_id = str(hello.get("client_id") or peer)
            core.authenticate(conn, self.recv_dir_var.get(), self.password_var.get())
            if hello.get("role") == "control":
                link = core.ControlLink.from_accepted(
                    conn, peer,
                    on_text=lambda text, ts, peer=peer: self.post(
                        "text", text=text, from_peer=True, ts=ts, peer=peer),
                    on_state=lambda up, cid=client_id, peer=peer: (
                        self.post("control_down", key=cid, peer=peer) if not up else None),
                    on_push=lambda name, size, token, peer=peer: self.post(
                        "push", name=name, size=size, token=token, peer=peer),
                    on_notice=lambda text, ts, peer=peer: self.post(
                        "notice", text=text, ts=ts, peer=peer),
                )
                self.post("control_ready", link=link, key=client_id, peer=peer)
                self.log(f"{peer} 文字通道已连接")
                keep_socket = True
                return
            self.log(f"{peer} 文件通道已连接")

            def emit(event: dict, peer=peer, client_id=client_id) -> None:
                self._server_emit(peer, client_id, event)

            core.handle_transfer_session(conn, self.recv_dir_var.get(),
                                         timeout=float(self._timeout()),
                                         locks=self.name_locks, emit=emit,
                                         path_resolver=self._resolve_push_path)
        except core.HelloRejected as exc:
            self.log(f"{peer} 握手失败：{exc}")
        except socket.timeout:
            self.log(f"{peer} 空闲超时，断开")
        except core.ProtocolError as exc:
            self.log(f"{peer} 协议错误：{exc}")
        except (ConnectionError, OSError) as exc:
            self.log(f"{peer} 文件通道中断：{exc}")
        except Exception as exc:                      # 防御：线程异常必须可见
            self.log(f"{peer} 会话异常：{exc}")
        finally:
            if not keep_socket:
                try:
                    conn.sock.close()
                except OSError:
                    pass
            self.post("server_session_end", peer=peer)

    def _handle_server_session_end(self, event: dict) -> None:
        peer = event.get("peer")
        for key, row_id in list(self.server_rows.items()):
            if key[0] != peer:
                continue
            row = self.rows.get(row_id)
            if row is not None and not row.get("finished"):
                self._handle_failed({"row": row_id, "message": "连接中断，未完成部分已保留为 .part",
                                     "result": {}})

    def _server_emit(self, peer: str, client_id: str, event: dict) -> None:
        """工作线程里只投递事件；界面控件的更新全部在主线程完成。"""
        etype = event.get("type")
        if etype == "start":
            self.post("server_start", peer=peer, client_id=client_id,
                      direction=event.get("direction", "receive"),
                      name=event.get("name", ""), size=event.get("size", 0),
                      resumed_from=event.get("resumed_from", 0))
        elif etype == "progress":
            self.post("server_progress", peer=peer, direction=event.get("direction", "receive"),
                      name=event.get("name", ""), done=event.get("done", 0),
                      total=event.get("total", 0), fraction=event.get("fraction", 0.0),
                      rate=event.get("rate", 0.0), eta=event.get("eta"))
        elif etype == "done":
            self.post("server_done", peer=peer, result=event.get("result") or {})
        elif etype == "error":
            self.post("server_failed", peer=peer, name=event.get("name", ""),
                      message=event.get("error", ""), result=event)

    def _handle_server_start(self, event: dict) -> None:
        direction = "接收" if event.get("direction") == "receive" else "发送"
        name = event.get("name", "")
        peer = event.get("peer") or ""
        client_id = event.get("client_id") or ""
        row = self._new_row(direction=direction, name=name,
                            size=event.get("size", 0), status="进行中")
        self.rows[row]["resumed_from"] = event.get("resumed_from", 0)
        self.rows[row]["server_peer"] = peer
        self.rows[row]["server_client_id"] = client_id
        self.rows[row]["remote"] = self._remote_label(peer, client_id)
        self._update_row(row)
        self.server_rows[(event.get("peer"), event.get("direction"), name)] = row
        self.current_progress_row = row
        self.task_text.set(f"{direction}：{name}")
        self.progress.configure(value=0)
        self.stats_text.set("正在传输…")
        if event.get("resumed_from"):
            self.log(f"{name} 从 {core.human_bytes(event['resumed_from'])} 处续传。")

    def _handle_server_progress(self, event: dict) -> None:
        row = self.server_rows.get((event.get("peer"), event.get("direction"), event.get("name")))
        if row is None:
            return
        self._handle_progress({
            "row": row,
            "done": event.get("done", 0),
            "total": event.get("total", 0),
            "fraction": event.get("fraction", 0.0),
            "rate": event.get("rate", 0.0),
            "eta": event.get("eta"),
        })

    def _handle_server_done(self, event: dict) -> None:
        data = event.get("result") or {}
        row = self.server_rows.get((event.get("peer"), data.get("direction"), data.get("name")))
        if row is not None:
            self._handle_done({"row": row, "result": data})

    def _handle_server_failed(self, event: dict) -> None:
        row = self.server_rows.get((event.get("peer"), event.get("direction"), event.get("name")))
        if row is not None:
            self._handle_failed({"row": row, "message": event.get("message", ""),
                                 "result": event.get("result") or {}})
        else:
            self.post("log", text=f"{event.get('peer')} {event.get('name')}：{event.get('message')}")

    # ------------------------------------------------------------------ #
    # 客户端模式
    # ------------------------------------------------------------------ #
    def connect_peer(self) -> None:
        if self.controls:
            return
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("地址为空", "请填写服务器的 IPv6 地址")
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("端口无效", "端口必须是 1..65535 的整数")
            return
        zone = self.zone_var.get().strip()
        if len(self.password_var.get()) > core.MAX_PASSWORD_CHARS:
            messagebox.showerror("密码太长",
                                 f"连接密码最多 {core.MAX_PASSWORD_CHARS} 个字符")
            return
        self._set_state("connecting")
        self.log(f"正在连接 [{host}]:{port} …")
        threading.Thread(target=self._connect_worker,
                         args=(host, port, zone, self.password_var.get()),
                         name="gui-connect", daemon=True).start()

    def _connect_worker(self, host: str, port: int, zone: str, password: str) -> None:
        try:
            link = core.ControlLink.connect(
                host, port, zone,
                timeout=float(self._timeout()),
                connect_timeout=10.0,
                password=password,
                on_text=lambda text, ts: self.post("text", text=text, from_peer=True,
                                                   ts=ts, peer="服务器"),
                on_state=lambda up: self.post("control_down", key=self.client_id, peer="") if not up else None,
                on_push=lambda name, size, token: self.post(
                    "push", name=name, size=size, token=token, peer="服务器"),
                on_notice=lambda text, ts: self.post("notice", text=text, ts=ts, peer="服务器"),
                client_id=self.client_id,
            )
            self.post("control_ready", link=link, key=self.client_id, peer=link.peer)
        except core.ConnectError as exc:
            message = str(exc)
            if "未启用文本消息" in message:
                self.file_only_mode = True
                self.post("log", text="对端不支持文字消息（命令行服务器），已进入仅文件模式。")
                self.post("state", state="file_only", peer=f"[{host}]:{port}")
            else:
                self.post("log", text=f"连接失败：{message}")
                self.post("state", state="idle", peer="")
                self.post("connect_failed", message=message)

    def disconnect_peer(self) -> None:
        had_peer = bool(self.controls) or self.state in ("connected", "file_only")
        for link in list(self.controls.values()):
            try:
                link.close()
            except OSError:
                pass
        self.controls.clear()
        self.control_peers.clear()
        self.file_only_mode = False
        self._adopt_control(None, "")
        if self.state in ("connected", "file_only"):
            self._set_state("listening" if self.server_sock is not None else "idle")
        if had_peer:
            self.log("已断开连接。")

    def _adopt_control(self, link, key: str, peer: str = "") -> None:
        if link is None:
            if key:
                existing = self.controls.get(key)
                if existing is not None and not existing.is_closed:
                    return          # 旧链路的迟到断开通知，忽略
                if key in self.controls:
                    self.controls.pop(key, None)
                    self.control_peers.pop(key, None)
                    self.log(f"客户端 {peer or key} 已断开。"
                             if self.role.get() == "server" else "与服务器的连接已断开。")
            else:
                for old in list(self.controls.values()):
                    try:
                        old.close()
                    except OSError:
                        pass
                self.controls.clear()
                self.control_peers.clear()
        else:
            old = self.controls.get(key)
            if old is not None and old is not link:
                try:
                    old.close()
                except OSError:
                    pass
            self.controls[key] = link
            self.control_peers[key] = peer or link.peer
            if self.role.get() == "client":
                self.peer_addr = peer or link.peer
                self.peer_text.set(f"对端：{self.peer_addr}")
                self.log(f"与 {self.peer_addr} 建立文字通道。")
                self._append_chat(f"已连接到 {self.peer_addr}。", from_peer=False, system=True)
            else:
                display = self._peer_label(key)
                self.log(f"客户端 {display} 已连接：可以发消息，也可以推送文件。")
                self._append_chat(f"客户端 {display} 已连接。", from_peer=False, system=True)
                self.notebook.tab(1, text="  文字消息 ●  ")

        if self.role.get() == "server":
            self._refresh_peers_view()
            if self.controls:
                self._set_state("connected")
            else:
                self._set_state("listening" if self.server_sock is not None else "idle")
        else:
            if self.controls:
                self._set_state("connected")
            else:
                self.peer_addr = ""
                self.peer_text.set("对端：—")
                if self.state in ("connected", "file_only"):
                    self._set_state("listening" if self.server_sock is not None else "idle")
        self._refresh_buttons()

    def _refresh_peers_view(self) -> None:
        if self.role.get() != "server":
            return
        selected = {self.peer_keys[i] for i in self.online_list.curselection()
                    if 0 <= i < len(self.peer_keys)}
        keys = sorted(self.controls)
        self.peer_keys = keys
        self.online_list.delete(0, "end")
        for key in keys:
            self.online_list.insert("end", self._peer_label(key))
        for index, key in enumerate(keys):
            if key in selected:
                self.online_list.selection_set(index)
        count = len(keys)
        self.peer_text.set(f"在线：{count} 个客户端" if count else "对端：—")
        if self.server_sock is not None:
            self.status_text.set("监听中" + (f" · {count} 个客户端在线" if count else ""))
        self._update_text_hint()
        self._refresh_buttons()

    @staticmethod
    def _peer_ip(peer: str) -> str:
        text = str(peer or "").strip()
        if text.startswith("[") and "]:" in text:
            return text[1:text.index("]")]
        if text.startswith("[") and text.endswith("]"):
            return text[1:-1]
        return text.rsplit(":", 1)[0] if ":" in text else text

    def _alias_for_peer(self, peer: str) -> str:
        if self.role.get() != "server":
            return ""
        return self.client_aliases.get(self._peer_ip(peer), "")

    def _peer_label(self, key: str, with_addr: bool = True) -> str:
        peer = self.control_peers.get(key, str(key))
        alias = self._alias_for_peer(peer)
        if alias:
            return f"{alias}  ({peer})" if with_addr else alias
        return peer

    def _remote_label(self, peer: str, client_id: str = None) -> str:
        """传输记录「对端」列：服务器视角显示客户端别名/IP，客户端视角统一显示服务器。"""
        if self.role.get() == "client":
            return "服务器"
        if client_id and client_id in self.control_peers:
            peer = self.control_peers[client_id]
        address = self._peer_ip(peer)
        alias = self.client_aliases.get(address, "")
        return alias or address or "—"

    def _refresh_remote_rows(self, key: str, address: str) -> None:
        for row_id, row in self.rows.items():
            client_id = row.get("server_client_id") or ""
            peer = row.get("server_peer") or ""
            if client_id == key or (peer and self._peer_ip(peer) == address):
                row["remote"] = self._remote_label(peer or self.control_peers.get(key, ""),
                                                   client_id or key)
                self._update_row(row_id)
        self._schedule_file_table_fit()

    def _set_alias_for_selected(self) -> None:
        if self.role.get() != "server":
            return
        selection = self.online_list.curselection()
        if not selection:
            messagebox.showinfo("设置别名", "请先在线客户端列表里选中一个客户端。")
            return
        index = selection[0]
        if not (0 <= index < len(self.peer_keys)):
            return
        key = self.peer_keys[index]
        peer = self.control_peers.get(key, key)
        address = self._peer_ip(peer)
        current = self.client_aliases.get(address, "")
        alias = simpledialog.askstring(
            "设置客户端别名",
            f"客户端：{peer}\n\n输入便于识别的别名（留空可清除）：",
            initialvalue=current, parent=self.root)
        if alias is None:
            return
        alias = alias.strip()[:32]
        if alias:
            self.client_aliases[address] = alias
            self.log(f"已为客户端 {peer} 设置别名：{alias}")
        else:
            self.client_aliases.pop(address, None)
            self.log(f"已清除客户端 {peer} 的别名。")
        self._refresh_peers_view()
        self._refresh_remote_rows(key, address)

    # ------------------------------------------------------------------ #
    # 选择文件 / 目录
    # ------------------------------------------------------------------ #
    def choose_files(self) -> None:
        paths = filedialog.askopenfilenames(title="选择要上传的文件")
        if not paths:
            return
        self.add_pending_files(paths)

    def add_pending_files(self, paths) -> None:
        """把文件加入「待发送文件」列表（按绝对路径去重）。"""
        known = {str(path.resolve()) for path in self.pending_files}
        added = []
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in known:
                continue
            known.add(key)
            added.append(path)
        if not added:
            return
        self.pending_files.extend(added)
        self._refresh_pending_view()
        self.log(f"已添加 {len(added)} 个待发送文件，共 {len(self.pending_files)} 个。")

    def queue_upload(self, paths) -> None:
        """供自动化测试使用：直接替换待发送列表。"""
        self.pending_files = [Path(p) for p in paths if Path(p).is_file()]
        self._refresh_pending_view()

    def _refresh_pending_view(self) -> None:
        for item in self.pending_tree.get_children():
            self.pending_tree.delete(item)
        total = 0
        for index, path in enumerate(self.pending_files):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            total += size
            self.pending_tree.insert("", "end", iid=str(index), values=(
                path.name, core.human_bytes(size), str(path.parent)))
        if self.pending_files:
            self.pending_summary.set(
                f"已选择 {len(self.pending_files)} 个文件，共 {core.human_bytes(total)}")
        else:
            self.pending_summary.set("还没有选择文件")
        self._refresh_buttons()

    def _remove_pending_selected(self) -> None:
        selected = sorted((int(iid) for iid in self.pending_tree.selection()), reverse=True)
        if not selected:
            return
        for index in selected:
            if 0 <= index < len(self.pending_files):
                self.pending_files.pop(index)
        self._refresh_pending_view()
        self.log("已从待发送列表移除选中文件。")

    def _clear_pending(self) -> None:
        if not self.pending_files:
            return
        self.pending_files.clear()
        self._refresh_pending_view()
        self.log("已清空待发送文件。")

    def _choose_recv_dir(self) -> None:
        path = filedialog.askdirectory(title="选择接收目录")
        if path:
            self.recv_dir_var.set(path)
            self._recv_signature = None
            self._refresh_local_list()
            self.log(f"服务器接收目录改为：{path}")

    def _choose_client_recv_dir(self) -> None:
        path = filedialog.askdirectory(title="选择客户端接收目录")
        if path:
            self.client_recv_dir_var.set(path)
            self._recv_signature = None
            self._refresh_local_list()
            self.log(f"客户端接收目录改为：{path}")

    def _current_recv_dir(self) -> str:
        if self.role.get() == "client":
            raw = self.client_recv_dir_var.get().strip() or "downloads"
        else:
            raw = self.recv_dir_var.get().strip() or "received"
        return os.path.abspath(raw)

    def _resolve_push_path(self, token: str):
        path = self.push_registry.get(str(token or ""))
        if path and os.path.isfile(path):
            return path
        return None

    def _open_recv_dir(self) -> None:
        path = self._current_recv_dir()
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            self.log(f"无法创建接收目录：{exc}")
            messagebox.showerror("打开文件夹失败", f"无法创建目录：{path}\n{exc}")
            return
        self.log(f"打开接收文件夹：{path}")
        open_in_file_manager(path)

    def _copy_address(self) -> None:
        selection = self.addr_list.curselection()
        if not selection:
            self.log("请先在地址列表中选中一个地址。")
            return
        raw = str(self.addr_list.get(selection[0]))
        self._copy_text(raw.split("  (")[0].strip())

    def _copy_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.log("已复制到剪贴板。")

    # ------------------------------------------------------------------ #
    # 连接配置串
    # ------------------------------------------------------------------ #
    def _on_config_button(self) -> None:
        if self.role.get() == "server":
            self._share_connection_config()
        else:
            self._import_connection_config()

    def _share_host(self) -> tuple:
        """优先用地址列表里选中的地址，其次全局/ULA，最后链路本地。"""
        entries = list(getattr(self, "address_entries", []))
        selection = self.addr_list.curselection()
        if selection and selection[0] < len(entries):
            entry = entries[selection[0]]
            return entry["address"], entry.get("scope", "")
        for scope in ("global", "ula"):
            for entry in entries:
                if entry.get("scope") == scope and not entry.get("deprecated"):
                    return entry["address"], scope
        for entry in entries:
            if entry.get("scope") == "link-local":
                return entry["address"], "link-local"
        return "", ""

    def _share_connection_config(self) -> None:
        host, scope = self._share_host()
        if not host:
            manual = simpledialog.askstring(
                "手动输入本机地址",
                "没有自动检测到本机 IPv6 地址。\n"
                "请在系统「网络/WLAN 详情」里查看本机 IPv6 并输入（可带 %接口索引）：",
                parent=self.root)
            if not manual or not manual.strip():
                return
            entry = core.parse_ipv6_address(manual.strip())
            if entry is None:
                messagebox.showerror("地址无效", "请输入有效的 IPv6 地址，例如 2409:xxxx::1")
                return
            host, scope = entry["address"], entry["scope"]
        if host.split("%", 1)[0] == "::1":
            messagebox.showwarning("只有回环地址",
                                   "当前只有 ::1，其他设备无法连接。\n"
                                   "请检查 WLAN 是否已启用 IPv6。")
            return
        try:
            port = int(self.port_var.get())
            token = core.encode_connection_config(host, port, self.password_var.get())
        except (TypeError, ValueError) as exc:
            messagebox.showerror("生成失败", str(exc))
            return

        window = tk.Toplevel(self.root)
        window.title("客户端连接配置")
        window.transient(self.root)
        window.grab_set()
        window.geometry(f"{self.px(620)}x{self.px(300)}")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(window, text="把下面整段发给客户端；客户端点「导入配置」粘贴即可：",
                  font=(self.ui_font, 10)).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        token_box = tk.Text(window, height=4, wrap="char", font=(self.mono_font, 9),
                            relief="solid", borderwidth=1, padx=6, pady=6)
        token_box.grid(row=1, column=0, sticky="nsew", padx=12)
        token_box.insert("1.0", token)
        token_box.configure(state="disabled")
        hint = "包含地址、端口和连接密码，请私发，不要发到公开群聊。"
        hint += "\n如果地址列表里有多个全局地址，优先选稳定的那个（临时地址重连后会变）。"
        if scope == "link-local":
            hint += "\n注意：当前是链路本地地址，只有同一网段的设备能用。"
        ttk.Label(window, text=hint, foreground=COLORS["muted"],
                  font=(self.ui_font, 8), wraplength=self.px(580)).grid(
            row=2, column=0, sticky="w", padx=12, pady=(6, 0))
        bar = ttk.Frame(window)
        bar.grid(row=3, column=0, sticky="ew", padx=12, pady=10)
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="复制连接串", command=lambda: self._copy_text(token)).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(bar, text="关闭", command=window.destroy).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        self._config_window = window
        self.log(f"已生成连接配置串：{host}，端口 {port}"
                 + ("（含密码）" if self.password_var.get() else "") + "。")

    def _import_connection_config(self) -> None:
        clipboard = ""
        try:
            clipboard = str(self.root.clipboard_get() or "").strip()
        except Exception:
            clipboard = ""
        sources = [clipboard]
        try:
            sources.append(self.chat.get("1.0", "end-1c"))
        except tk.TclError:
            pass
        for source in sources:
            if not (source or "").strip():
                continue
            try:
                data = core.decode_connection_config(source)
            except Exception:
                continue
            if data is not None:
                self._apply_imported_config(data)
                return
        self._show_import_dialog(clipboard)

    def _show_import_dialog(self, initial: str = "") -> None:
        window = tk.Toplevel(self.root)
        window.title("导入连接配置")
        window.transient(self.root)
        window.grab_set()
        window.geometry(f"{self.px(620)}x{self.px(340)}")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(window, text="粘贴服务器发来的 FT6. 开头连接串：",
                  font=(self.ui_font, 10)).grid(row=0, column=0, sticky="w", padx=12, pady=(12, 4))
        token_box = tk.Text(window, height=5, wrap="char", font=(self.mono_font, 9),
                            relief="solid", borderwidth=1, padx=6, pady=6)
        token_box.grid(row=1, column=0, sticky="nsew", padx=12)
        if initial:
            token_box.insert("1.0", initial)

        def paste() -> None:
            try:
                token_box.delete("1.0", "end")
                token_box.insert("1.0", self.root.clipboard_get().strip())
            except Exception:
                messagebox.showinfo("剪贴板不可用",
                                    "没有读到剪贴板文本，请手动粘贴。", parent=window)

        def do_import() -> None:
            token = token_box.get("1.0", "end-1c").strip()
            try:
                data = core.decode_connection_config(token)
            except ValueError as exc:
                messagebox.showerror("导入失败", str(exc), parent=window)
                return
            self._apply_imported_config(data)
            window.destroy()

        bar = ttk.Frame(window)
        bar.grid(row=2, column=0, sticky="ew", padx=12, pady=10)
        for index in range(3):
            bar.columnconfigure(index, weight=1)
        ttk.Button(bar, text="从剪贴板粘贴", command=paste).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(bar, text="导入", command=do_import).grid(
            row=0, column=1, sticky="ew", padx=3)
        ttk.Button(bar, text="取消", command=window.destroy).grid(
            row=0, column=2, sticky="ew", padx=(3, 0))
        self._import_window = window

    def _apply_imported_config(self, data: dict) -> None:
        self.host_var.set(data["host"])
        self.port_var.set(str(data["port"]))
        self.password_var.set(data["password"])
        self.zone_var.set("")           # 地址里可能已带 %接口索引，避免重复
        suffix = "（含连接密码）" if data.get("password") else ""
        self.log(f"已导入连接配置：{data['host']}，端口 {data['port']}{suffix}。点「连接」即可。")
        if self.controls:
            self.log("当前已连接，请先「断开」再重新连接以应用新配置。")

    def _refresh_local_addresses(self) -> None:
        try:
            port = int(self.port_var.get())
        except ValueError:
            port = core.DEFAULT_PORT
        self._fill_local_addresses(port)
        self.log(f"已刷新本机 IPv6 地址（{len(self.address_entries)} 个）。")

    def _fill_local_addresses(self, port: int) -> None:
        if not hasattr(self, "addr_list"):
            return
        try:
            entries = core.list_ipv6_addresses()
        except Exception as exc:              # 枚举失败不应影响界面
            entries = []
            self.log(f"枚举本机 IPv6 地址失败：{exc}")
        entries = sorted(entries, key=lambda e: (
            core.SCOPE_ORDER.get(e.get("scope"), 9),
            bool(e.get("deprecated")),
            bool(e.get("temporary")),
            e.get("ip", ""),
        ))
        self.address_entries = entries
        self.addr_list.delete(0, "end")
        for entry in entries:
            self.addr_list.insert("end", entry["address"])
        if not entries:
            self.addr_list.insert("end", "::1")
            self.addr_list.selection_set(0)
            self.addr_detail.set("说明：回环地址 · 仅同机测试")
            self.addr_hint.configure(
                text="未检测到可远程使用的 IPv6 地址：请检查网卡是否已启用 IPv6。")
            return
        self.addr_list.selection_set(0)
        self._on_address_select()
        if self.role.get() == "client":
            self.addr_hint.configure(
                text="仅供参考：这里仅列出本机地址；服务器地址请由对方提供。")
            return
        has_remote = any(entry["scope"] in ("global", "ula") and not entry["deprecated"]
                         for entry in entries)
        hint = f"客户端连接：地址 + 端口 {port}"
        if any(entry["scope"] == "link-local" for entry in entries):
            hint += "；链路本地还需填接口索引"
        if not has_remote:
            hint += "。当前只有链路本地/已弃用地址，可能只能同网段连接"
        self.addr_hint.configure(text=hint + "。")

    def _on_address_select(self) -> None:
        selection = self.addr_list.curselection()
        if not selection:
            self.addr_detail.set("")
            return
        index = selection[0]
        if index < len(self.address_entries):
            entry = self.address_entries[index]
            detail = core.format_address_entry(entry).split("  (", 1)[-1].rstrip(")")
            self.addr_detail.set(f"说明：{detail}")
        else:
            self.addr_detail.set("说明：回环地址 · 仅同机测试")

    # ------------------------------------------------------------------ #
    # 上传
    # ------------------------------------------------------------------ #
    def start_upload(self, limit_rate: int = 0) -> None:
        if self.state in ("idle", "listening"):
            messagebox.showinfo("尚未连接", "请先连接服务器。")
            return
        if self.state in ("busy", "connecting"):
            return
        files = [p for p in self.pending_files if p.is_file()]
        if not files:
            messagebox.showinfo("没有文件", "请先选择要上传的文件。")
            return
        pairs = []
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            pairs.append((path, self._new_row("上传", path.name, size, "等待", str(path))))
        self.pending_files = []
        self._refresh_pending_view()
        self.transfer = {"cancel": threading.Event(), "sock": None, "limit_rate": limit_rate}
        self._set_state("busy")
        self.log(f"开始上传 {len(pairs)} 个文件。")
        threading.Thread(target=self._upload_worker, args=(pairs,), name="gui-upload",
                         daemon=True).start()

    def _upload_worker(self, pairs) -> None:
        host, port = self.host_var.get().strip(), int(self.port_var.get())
        zone = self.zone_var.get().strip()
        cancel = self.transfer["cancel"]
        conn = None
        try:
            conn, _peer, _reply = core.resolve_and_connect(
                host, port, zone, connect_timeout=10.0, timeout=float(self._timeout()),
                role="transfer", app="file_gui/1.0",
                extra_hello={"client_id": self.client_id},
                password=self.password_var.get())
            self.transfer["sock"] = conn.sock
            for path, row_id in pairs:
                if cancel.is_set():
                    self.post("cancelled", row=row_id)
                    continue
                self.post("start", row=row_id)
                result = core.send_file(
                    conn, str(path), resume=True,
                    on_progress=lambda snap, rid=row_id: self.post("progress", row=rid, **snap),
                    on_stage=lambda stage, rid=row_id: self.post("stage", row=rid, stage=stage),
                    should_cancel=cancel.is_set,
                    limit_rate=self.transfer.get("limit_rate", 0),
                )
                if cancel.is_set() and not result.ok:
                    self.post("cancelled", row=row_id)
                else:
                    self.post("done" if result.ok else "failed", row=row_id,
                              result=result.data, message=result.error)
            if cancel.is_set():
                self.post("log", text="上传已取消，已接收部分保留为 .part，可重试续传。")
        except (core.ConnectError, ConnectionError, OSError, core.ProtocolError) as exc:
            if cancel.is_set():
                for _path, row_id in pairs:
                    row = self.rows.get(row_id)
                    if row is not None and not row.get("finished"):
                        self.post("cancelled", row=row_id)
            else:
                self.post("log", text=f"上传中断：{exc}")
                for _path, row_id in pairs:
                    row = self.rows.get(row_id)
                    if row is not None and not row.get("finished"):
                        self.post("failed", row=row_id, message=str(exc), result={})
        finally:
            if conn is not None:
                try:
                    conn.send_msg({"cmd": "bye"})
                    conn.recv_msg()
                except (OSError, core.ProtocolError):
                    pass
                try:
                    conn.sock.close()
                except OSError:
                    pass
            self.post("transfer_end")

    # ------------------------------------------------------------------ #
    # 接收推送 / 发送
    # ------------------------------------------------------------------ #
    def _target_peer_links(self):
        """服务器：选中的在线客户端；未选中则全部。客户端：唯一的服务器。"""
        if not self.controls:
            return []
        if self.role.get() == "client":
            return list(self.controls.items())
        selected = [self.peer_keys[i] for i in self.online_list.curselection()
                    if 0 <= i < len(self.peer_keys)]
        if not selected:
            return list(self.controls.items())
        return [(key, self.controls[key]) for key in selected if key in self.controls]

    def _handle_push(self, event: dict) -> None:
        if self.role.get() != "client":
            return
        name = event.get("name") or ""
        size = int(event.get("size") or 0)
        token = event.get("token")
        if not name:
            return
        self.push_queue.append((name, token if isinstance(token, str) else None))
        text = f"收到服务器发来的文件：{name}（{core.human_bytes(size)}），已加入接收队列。"
        self.log(text)
        self._append_chat(text, from_peer=True, system=True)
        if self.state not in ("busy", "connecting") and self.controls:
            items = self.push_queue[:]
            self.push_queue.clear()
            self.start_download(items, direction="推送")

    def start_send(self, limit_rate: int = 0) -> None:
        """客户端=上传到服务器；服务器=把待发送文件推送给在线客户端。"""
        if self.role.get() == "client":
            self.start_upload(limit_rate)
        else:
            self._send_from_server()

    def _send_from_server(self) -> None:
        files = [p for p in self.pending_files if p.is_file()]
        if not files:
            messagebox.showinfo("没有文件", "请先选择要发送的文件。")
            return
        targets = self._target_peer_links()
        if not targets:
            messagebox.showinfo("没有客户端", "还没有客户端连接，无法发送。")
            return
        sent = failed = 0
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            absolute = os.path.abspath(path)
            for key, link in targets:
                display = self._peer_label(key)
                token = uuid.uuid4().hex
                self.push_registry[token] = absolute
                try:
                    link.send_push(path.name, size, token)
                    sent += 1
                except (OSError, ConnectionError, core.ProtocolError, ValueError) as exc:
                    failed += 1
                    self.push_registry.pop(token, None)
                    self.log(f"发送给 {display} 失败：{exc}")
        self.pending_files.clear()
        self._refresh_pending_view()
        scope = "全部客户端" if len(targets) == len(self.controls) else f"{len(targets)} 个选中客户端"
        suffix = f"，失败 {failed} 次" if failed else ""
        self.log(f"已向{scope}发送 {len(files)} 个文件（通知 {sent} 次{suffix}），等待对方接收。")

    def start_download(self, items, direction: str = "推送") -> None:
        """接收服务器推送的文件；items 为 [(文件名, token)]。"""
        if self.state in ("busy", "connecting") or not items:
            return
        if self.role.get() != "client":
            return
        target = self._current_recv_dir()
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("接收目录不可用", str(exc))
            return
        pairs = [(name, token, self._new_row(direction, name, 0, "等待"))
                 for name, token in items]
        self.transfer = {"cancel": threading.Event(), "sock": None, "limit_rate": 0}
        self._set_state("busy")
        self.log(f"开始接收 {len(pairs)} 个文件到 {target}。")
        threading.Thread(target=self._download_worker, args=(pairs, target), name="gui-download",
                         daemon=True).start()

    def _download_worker(self, pairs, target: str) -> None:
        host, port = self.host_var.get().strip(), int(self.port_var.get())
        zone = self.zone_var.get().strip()
        cancel = self.transfer["cancel"]
        conn = None
        try:
            conn, _peer, _reply = core.resolve_and_connect(
                host, port, zone, connect_timeout=10.0, timeout=float(self._timeout()),
                role="transfer", app="file_gui/1.0",
                extra_hello={"client_id": self.client_id},
                password=self.password_var.get())
            self.transfer["sock"] = conn.sock
            for name, token, row_id in pairs:
                if cancel.is_set():
                    self.post("cancelled", row=row_id)
                    continue
                self.post("start", row=row_id)
                result = core.recv_file(
                    conn, name, target, resume=True, token=token,
                    on_progress=lambda snap, rid=row_id: self.post("progress", row=rid, **snap),
                    on_stage=lambda stage, rid=row_id: self.post("stage", row=rid, stage=stage),
                    should_cancel=cancel.is_set,
                    limit_rate=self.transfer.get("limit_rate", 0),
                )
                if cancel.is_set() and not result.ok:
                    self.post("cancelled", row=row_id)
                else:
                    self.post("done" if result.ok else "failed", row=row_id,
                              result=result.data, message=result.error)
            if cancel.is_set():
                self.post("log", text="接收已取消，已收部分保留为 .part，可重试续传。")
        except (core.ConnectError, ConnectionError, OSError, core.ProtocolError) as exc:
            if cancel.is_set():
                for _name, _token, row_id in pairs:
                    row = self.rows.get(row_id)
                    if row is not None and not row.get("finished"):
                        self.post("cancelled", row=row_id)
            else:
                self.post("log", text=f"接收中断：{exc}")
                for _name, _token, row_id in pairs:
                    row = self.rows.get(row_id)
                    if row is not None and not row.get("finished"):
                        self.post("failed", row=row_id, message=str(exc), result={})
        finally:
            if conn is not None:
                try:
                    conn.send_msg({"cmd": "bye"})
                    conn.recv_msg()
                except (OSError, core.ProtocolError):
                    pass
                try:
                    conn.sock.close()
                except OSError:
                    pass
            self.post("transfer_end")

    def cancel_transfer(self) -> None:
        transfer = self.transfer
        if not transfer:
            return
        transfer["cancel"].set()
        sock = transfer.get("sock")
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.log("正在取消当前传输（已收字节保留为 .part，可续传）。")

    def _finish_batch(self) -> None:
        self.transfer = None
        self.progress.configure(value=0)
        self.task_text.set("空闲")
        self.stats_text.set("等待任务")
        if self.controls:
            self._set_state("connected")
        elif self.server_sock is not None:
            self._set_state("listening")
        elif self.file_only_mode:
            self._set_state("file_only")
        else:
            self._set_state("idle")
        self._refresh_local_list()
        self._refresh_buttons()
        if self.role.get() == "client" and self.push_queue and self.controls:
            items = self.push_queue[:]
            self.push_queue.clear()
            self.start_download(items, direction="推送")

    # ------------------------------------------------------------------ #
    # 传输记录
    # ------------------------------------------------------------------ #
    def _new_row(self, direction: str, name: str, size: int, status: str, path: str = "") -> str:
        self.seq += 1
        row_id = f"row{self.seq}"
        remote = "服务器" if self.role.get() == "client" else "—"
        values = (time.strftime("%H:%M:%S"), direction, remote, name,
                  core.human_bytes(size), status, "—")
        self.history.insert("", "end", iid=row_id, values=values, tags=("run",))
        self.history.see(row_id)
        self._schedule_file_table_fit()
        self.rows[row_id] = {
            "direction": direction, "raw_name": name, "name": name,
            "size": size, "status": status, "path": path,
            "remote": remote,
            "local_sha256": "", "peer_sha256": "", "matched": None,
            "resumed_from": 0, "finished": False, "server_peer": "",
            "server_client_id": "",
        }
        return row_id

    def _handle_start(self, event: dict) -> None:
        row_id = event.get("row")
        row = self.rows.get(row_id)
        if row is None:
            return
        row["status"] = "进行中"
        self.current_progress_row = row_id
        self._update_row(row_id)
        self.task_text.set(f"{row['direction']}：{row['name']}")
        self.progress.configure(value=0)
        self.stats_text.set("正在传输…")

    def _handle_progress(self, event: dict) -> None:
        row_id = event.get("row")
        row = self.rows.get(row_id)
        if row is None:
            return
        fraction = float(event.get("fraction", 0.0))
        done = int(event.get("done", 0))
        total = int(event.get("total", 0))
        rate = float(event.get("rate", 0.0))
        eta = event.get("eta")
        if total:
            row["size"] = total
        self.progress.configure(value=int(fraction * 1000))
        eta_text = core.format_duration(eta) if eta else "--:--"
        self.stats_text.set(
            f"{fraction * 100:5.1f}%   {core.human_bytes(done)}/{core.human_bytes(total)}"
            f"   {core.human_bytes(rate)}/s   剩余 {eta_text}"
        )
        self.task_text.set(f"{row['direction']}：{row['name']}")
        self._update_row(row_id)

    def _handle_stage(self, event: dict) -> None:
        row_id = event.get("row")
        stage = (event.get("stage") or {}).get("stage")
        row = self.rows.get(row_id)
        if row is None:
            return
        if stage == "restart":
            self.log(f"{row['name']} 校验不一致，正在从头重传一次。")
        elif stage == "start":
            row["resumed_from"] = int((event.get("stage") or {}).get("resumed_from", 0))
            if row["resumed_from"]:
                self.log(f"{row['name']} 从 {core.human_bytes(row['resumed_from'])} 处续传。")

    def _handle_done(self, event: dict) -> None:
        row_id = event.get("row")
        data = event.get("result") or {}
        row = self.rows.get(row_id)
        if row is None:
            return
        row.update({
            "name": data.get("final_name") or row["name"],
            "size": data.get("size", row["size"]),
            "status": "完成",
            "local_sha256": data.get("local_sha256", ""),
            "peer_sha256": data.get("peer_sha256", ""),
            "matched": bool(data.get("matched")),
            "resumed_from": data.get("resumed_from", 0),
            "path": data.get("path", row.get("path", "")),
            "finished": True,
        })
        self.progress.configure(value=1000)
        self.stats_text.set(
            f"100.0%   {core.human_bytes(row['size'])}/{core.human_bytes(row['size'])}"
            f"   {core.human_rate(max(row['size'] - row['resumed_from'], 0), float(data.get('seconds', 0) or 0))}"
            f"   剩余 00:00"
        )
        self._update_row(row_id)
        self.log(f"{row['direction']}完成：{row['name']}（{core.human_bytes(row['size'])}），"
                 f"{'哈希一致 ✔' if row['matched'] else '哈希不一致 ✗'}")
        self.history.selection_set(row_id)
        self._show_hash_detail()
        if row["direction"] == "接收":
            key = row.get("server_client_id") or ""
            verdict = "SHA-256 校验通过" if row["matched"] else "哈希不一致"
            self._send_notice(key, f"已收到文件 {row['name']}（{core.human_bytes(row['size'])}，{verdict}）")
        elif row["direction"] == "推送" and row.get("matched"):
            self._send_notice("", f"客户端已接收推送文件 {row['name']}（校验一致）")
        if row["direction"] in ("接收", "发送"):
            self._maybe_finish_server()

    def _send_notice(self, key: str, text: str) -> None:
        """给指定客户端（key 为空表示客户端模式下发给服务器）发系统通知。"""
        targets = []
        if key:
            link = self.controls.get(key)
            if link is not None:
                targets.append((self._peer_label(key), link))
        else:
            targets.extend((self._peer_label(k), link)
                           for k, link in self.controls.items())
        for display, link in targets:
            try:
                link.send_notice(text)
            except (OSError, ConnectionError, core.ProtocolError, ValueError) as exc:
                self.log(f"向 {display} 发送通知失败：{exc}")

    def _maybe_finish_server(self) -> None:
        pending = [r for r in self.rows.values()
                   if r["direction"] in ("接收", "发送") and not r["finished"]]
        if not pending:
            self._refresh_local_list()
            self._finish_batch()

    def _handle_failed(self, event: dict) -> None:
        row_id = event.get("row")
        row = self.rows.get(row_id)
        if row is None:
            return
        data = event.get("result") or {}
        row.update({
            "status": "失败",
            "local_sha256": data.get("local_sha256", row.get("local_sha256", "")),
            "peer_sha256": data.get("peer_sha256", row.get("peer_sha256", "")),
            "matched": data.get("matched") if data.get("matched") is not None else row.get("matched"),
            "finished": True,
        })
        self._update_row(row_id)
        self.log(f"{row['direction']}失败：{row['name']} —— {event.get('message', '')}")
        self.history.selection_set(row_id)
        self._show_hash_detail()
        if row["direction"] == "接收":
            self._send_notice(row.get("server_client_id") or "",
                              f"文件 {row['name']} 接收失败：{event.get('message', '')}")
        if row["direction"] in ("接收", "发送"):
            self._refresh_local_list()
            self._maybe_finish_server()

    def _handle_cancelled(self, event: dict) -> None:
        row_id = event.get("row")
        row = self.rows.get(row_id)
        if row is None:
            return
        row["status"] = "已取消"
        row["finished"] = True
        self._update_row(row_id)
        self.log(f"{row['direction']}已取消：{row['name']}（.part 已保留，可重试续传）")
        if row["direction"] in ("接收", "发送"):
            self._maybe_finish_server()
        self._show_hash_detail()

    def _update_row(self, row_id: str) -> None:
        row = self.rows.get(row_id)
        if row is None:
            return
        check = "—"
        tag = "muted"
        if row["status"] == "完成":
            check = "✔" if row["matched"] else "✗"
            tag = "ok" if row["matched"] else "bad"
        elif row["status"] == "失败":
            check = "✗" if row.get("matched") is False else "—"
            tag = "bad"
        elif row["status"].startswith("已取消"):
            tag = "muted"
        elif row["status"] == "进行中":
            tag = "run"
        self.history.item(row_id, values=(
            self.history.item(row_id, "values")[0],
            row["direction"], row.get("remote", "—"), row["name"],
            core.human_bytes(row["size"]), row["status"], check,
        ), tags=(tag,))

    def _show_hash_detail(self) -> None:
        selection = self.history.selection()
        if not selection:
            self.hash_local.set("")
            self.hash_peer.set("")
            self.hash_verdict.set("选择一条传输记录查看校验结果")
            self.verdict_label.configure(fg=COLORS["muted"])
            return
        row = self.rows.get(selection[0]) or {}
        local = row.get("local_sha256") or ""
        peer = row.get("peer_sha256") or ""
        self.hash_local.set(local or "（尚未计算）")
        self.hash_peer.set(peer or "（对端未提供）")
        if local and peer:
            if local == peer:
                self.hash_verdict.set("✔ 一致：本机文件与对端文件完全相同")
                self.verdict_label.configure(fg=COLORS["ok"])
            else:
                self.hash_verdict.set("✗ 不一致：两边内容不同，请检查传输过程")
                self.verdict_label.configure(fg=COLORS["bad"])
        elif row.get("status") == "失败":
            self.hash_verdict.set("✗ 传输失败，无法比对")
            self.verdict_label.configure(fg=COLORS["bad"])
        else:
            self.hash_verdict.set("传输完成后显示比对结果")
            self.verdict_label.configure(fg=COLORS["muted"])

    # ------------------------------------------------------------------ #
    # 本机文件列表 / 文字消息
    # ------------------------------------------------------------------ #
    def _refresh_local_list(self) -> None:
        directory = self._current_recv_dir()
        files = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if not entry.is_file() or entry.name.endswith(".part"):
                        continue
                    st = entry.stat()
                    files.append({"name": entry.name, "size": st.st_size, "mtime": st.st_mtime})
        except OSError:
            files = []
        files.sort(key=lambda item: item["name"].lower())
        entries = files[:MAX_UI_FILES]
        signature = tuple((entry["name"], entry["size"], entry["mtime"]) for entry in entries)
        if signature != getattr(self, "_recv_signature", None):
            self._recv_signature = signature
            for item in self.remote_tree.get_children():
                self.remote_tree.delete(item)
            for entry in entries:
                stamp = time.strftime("%m-%d %H:%M", time.localtime(entry["mtime"]))
                self.remote_tree.insert("", "end", values=(
                    entry["name"], core.human_bytes(entry["size"]), stamp))
            self._schedule_file_table_fit()
        self._refresh_buttons()

    def _auto_refresh_tick(self) -> None:
        if self.closing:
            return
        try:
            self._refresh_local_list()
            self.root.after(3000, self._auto_refresh_tick)
        except tk.TclError:
            pass

    def _on_return(self, event):
        if event.state & 0x0001:          # Shift 按下 -> 换行
            return None
        self.send_text()
        return "break"

    def _update_char_count(self) -> None:
        text = self.input.get("1.0", "end-1c")
        self.char_count.set(f"{len(text)}/{core.MAX_TEXT_CHARS}")

    def send_text(self) -> None:
        text = self.input.get("1.0", "end-1c")
        if not text.strip():
            return
        if len(text) > core.MAX_TEXT_CHARS:
            messagebox.showwarning("消息过长", f"单条消息最多 {core.MAX_TEXT_CHARS} 个字符。")
            return
        targets = self._target_peer_links()
        if not targets:
            return
        sent = 0
        failures = []
        for peer, link in targets:
            try:
                link.send_text(text)
                sent += 1
            except (OSError, ConnectionError, core.ProtocolError, ValueError) as exc:
                failures.append(f"{peer}: {exc}")
        if sent == 0:
            self.log("发送文字失败：" + ("；".join(failures) or "没有可用的连接"))
            return
        if len(targets) == 1:
            display = self._peer_label(targets[0][0], with_addr=False)
            label = "我" if self.role.get() == "client" else f"我 → {display}"
        elif sent == len(self.controls):
            label = f"我（群发 {sent} 人）"
        else:
            label = f"我（发给 {sent} 人）"
        self._append_chat(text, from_peer=False, sender=label)
        self.input.delete("1.0", "end")
        self._update_char_count()
        if failures:
            self.log("部分消息发送失败：" + "；".join(failures))

    def _append_chat(self, text: str, from_peer: bool, ts=None, system: bool = False,
                     sender: str = None) -> None:
        if from_peer and self.role.get() == "server" and sender:
            alias = self._alias_for_peer(sender)
            if alias:
                sender = alias
        stamp = time.strftime("%H:%M:%S", time.localtime((ts or 0) / 1000)) if ts else time.strftime("%H:%M:%S")
        self.chat.configure(state="normal")
        if system:
            self.chat.insert("end", f"[{stamp}] ", "stamp")
            self.chat.insert("end", "系统：", "sys")
            self.chat.insert("end", f"{text}\n", "sys")
        else:
            who = sender or ("对方" if from_peer else "我")
            tag = "peer" if from_peer else "me"
            self.chat.insert("end", f"[{stamp}] ", "stamp")
            self.chat.insert("end", f"{who}：", tag)
            self.chat.insert("end", f"{text}\n", tag)
        self.chat.see("end")
        self.chat.configure(state="disabled")
        if from_peer and not system:
            self.log(f"收到 {sender or '对端'} 的文字消息（{len(text)} 字）。")
            if self.notebook.index("current") != 1:
                self.notebook.tab(1, text="  文字消息 ●  ")
        elif not from_peer:
            self.notebook.tab(1, text="  文字消息  ")

    def _on_tab_changed(self, _event=None) -> None:
        if self.notebook.index("current") == 1:
            self.notebook.tab(1, text="  文字消息  ")

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def _timeout(self) -> float:
        return 120.0

    def on_close(self) -> None:
        if self.state == "busy":
            if not messagebox.askyesno("正在传输", "还有文件在传输，确定要退出吗？\n"
                                                   "未完成的部分会保留为 .part，下次可续传。"):
                return
        self.closing = True
        self.cancel_transfer()
        for link in list(self.controls.values()):
            try:
                link.close()
            except OSError:
                pass
        self.controls.clear()
        self.control_peers.clear()
        if self.server_sock is not None:
            self.server_stop.set()
            try:
                self.server_sock.close()
            except OSError:
                pass
        save_config({
            "role": self.role.get(),
            "port": self.port_var.get(),
            "host": self.host_var.get(),
            "zone": self.zone_var.get(),
            "recv_dir": self.recv_dir_var.get(),
            "client_recv_dir": self.client_recv_dir_var.get(),
            "password": self.password_var.get(),
            "client_aliases": self.client_aliases,
            "geometry": self.root.winfo_geometry(),
        })
        self.root.destroy()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IPv6 文件传输 + 文字消息图形界面",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python file_gui.py\n"
            "  python file_gui.py --role server --port 9000 --dir received\n"
            "  python file_gui.py --role client --host 2001:db8::5\n"
        ),
    )
    parser.add_argument("--role", choices=("server", "client"), help="启动时的角色")
    parser.add_argument("--host", help="客户端模式：服务器 IPv6 地址")
    parser.add_argument("--port", type=int, help="端口（默认 9000）")
    parser.add_argument("--dir", help="服务器模式：接收目录")
    parser.add_argument("--zone", help="链路本地地址的接口索引")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    enable_dpi_awareness()
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
    except (OSError, ValueError):
        pass

    config = load_config()
    root = tk.Tk()
    try:
        root.iconbitmap(default=os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "app_icon_256.ico"))
    except Exception:
        print("图标设置失败")
    app = App(root, args, config)

    if args.role:
        app.role.set(args.role)
        app._apply_role(initial=True)
    if args.host:
        app.host_var.set(args.host)
    if args.port:
        app.port_var.set(str(args.port))
    if args.dir:
        app.recv_dir_var.set(args.dir)
    if args.zone:
        app.zone_var.set(args.zone)

    if args.role == "server":
        root.after(200, app.start_server)
    elif args.role == "client" and args.host:
        root.after(200, app.connect_peer)

    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
