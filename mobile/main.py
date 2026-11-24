#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_gui_mobile.py —— IPv6 文件传输工具 · 手机版（Pydroid 3 / Tkinter）

设计目标：竖屏、单手可点、按钮大、标签少，四个页签完成全部操作：
    连接 → 发送 → 接收 → 聊天

功能与电脑版一致：
* 服务器 / 客户端两种角色；
* 选择文件 → 待发送列表 → 开始发送（服务器可发给选中/全部在线客户端）；
* 接收目录可设置，接收列表自动刷新，一键复制路径；
* 进度、速度、剩余时间、SHA-256 校验；
* 文字消息（服务器可群发/定向）；
* 服务器收文件后自动通知客户端；服务器可把任意本地文件推送给客户端。

使用（Pydroid 3）：
    1. 把整个 mobile 文件夹拷到手机（如 /storage/emulated/0/Documents/Ipv6Transfer）；
    2. Pydroid 3 打开 file_gui_mobile.py，点运行；
    3. 首次运行如提示安装 Tkinter，按提示安装即可；
    4. 允许存储权限，接收目录默认在 Download/IPv6Transfer 下。
"""

from __future__ import annotations

import json
import os
import queue
import socket
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import uuid
from pathlib import Path
from tkinter import messagebox, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import file_core as core  # noqa: E402

APP_TITLE = "IPv6 文件传输 · 手机版"
CONFIG_NAME = "mobile_config.json"
MAX_UI_FILES = 300
DRAIN_MS = 80
AUTO_REFRESH_MS = 3000
UI_FONT_CANDIDATES = (
    "Noto Sans CJK SC", "Noto Sans SC", "Source Han Sans SC", "Droid Sans Fallback",
    "WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Microsoft YaHei UI",
    "DejaVu Sans", "Helvetica",
)
MONO_FONT_CANDIDATES = (
    "DejaVu Sans Mono", "Droid Sans Mono", "Noto Sans Mono",
    "Liberation Mono", "Courier New",
)
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
# 通用小工具
# --------------------------------------------------------------------------- #
def pick_font(families, candidates, fallback="TkDefaultFont") -> str:
    for name in candidates:
        if name in families:
            return name
    return fallback


def config_path() -> str:
    """优先放在脚本目录；不可写时放到用户主目录。"""
    candidate = os.path.join(HERE, CONFIG_NAME)
    try:
        with open(candidate, "a", encoding="utf-8"):
            pass
        return candidate
    except OSError:
        return os.path.join(os.path.expanduser("~"), CONFIG_NAME)


def load_config() -> dict:
    try:
        with open(config_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(data: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def default_recv_dir(sub: str) -> str:
    """Android 上优先用公共 Download 目录，失败则退回主目录/当前目录。"""
    bases = (
        "/storage/emulated/0/Download",
        "/sdcard/Download",
        os.path.expanduser("~"),
        os.getcwd(),
    )
    for base in bases:
        try:
            path = os.path.join(base, "IPv6Transfer", sub)
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    path = os.path.abspath(sub)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


def copy_text(root: tk.Misc, text: str) -> bool:
    text = str(text or "")
    if not text:
        return False
    if os.name == "posix" and _android_clipboard_set(text):
        return True
    try:
        root.clipboard_clear()
        root.clipboard_append(text)
        return True
    except tk.TclError:
        pass
    if _android_clipboard_set(text):
        return True
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def _android_clipboard_set(text: str) -> bool:
    for module_name in ("android", "androidhelper"):
        try:
            module = __import__(module_name, fromlist=["Android"])
            droid = module.Android()
            droid.setClipboard(str(text))
            return True
        except Exception:
            continue
    return False


# --------------------------------------------------------------------------- #
# 手机文件选择器（不依赖系统文件对话框，Pydroid 上也能用）
# --------------------------------------------------------------------------- #
class FileBrowser(tk.Toplevel):
    """竖屏文件浏览器：mode='file' 选文件，mode='dir' 选文件夹。"""

    def __init__(self, master, mode: str, start: str, ui_font: str):
        super().__init__(master)
        self.mode = mode
        self.result = None
        self.ui_font = ui_font
        self.title("选择文件" if mode == "file" else "选择文件夹")
        self.transient(master)
        self.grab_set()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{int(sw * 0.94)}x{int(sh * 0.88)}+{int(sw * 0.03)}+{int(sh * 0.05)}")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        top = ttk.Frame(self, padding=(8, 8, 8, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.path_var = tk.StringVar(value=os.path.abspath(start or os.path.expanduser("~")))
        path_entry = ttk.Entry(top, textvariable=self.path_var, font=(self.ui_font, 10))
        path_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(top, text="前往", command=self._go_path).grid(row=0, column=1, padx=(6, 0))

        quick = ttk.Frame(self, padding=(8, 0, 8, 4))
        quick.grid(row=1, column=0, sticky="ew")
        for label, path in (("手机存储", "/storage/emulated/0"),
                            ("下载", "/storage/emulated/0/Download"),
                            ("主目录", os.path.expanduser("~")),
                            ("上一级", None)):
            ttk.Button(quick, text=label, width=6,
                       command=lambda p=path: self._quick(p)).pack(side="left", expand=True,
                                                                   fill="x", padx=2)

        body = ttk.Frame(self, padding=(8, 0))
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(body, font=(self.ui_font, 10), activestyle="none",
                                  selectmode="browse", exportselection=False)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind("<Double-1>", lambda _e: self._open_selected())
        self.entries = []

        bottom = ttk.Frame(self, padding=8)
        bottom.grid(row=3, column=0, sticky="ew")
        for index in range(3):
            bottom.columnconfigure(index, weight=1)
        ttk.Button(bottom, text="进入目录", command=self._open_selected).grid(
            row=0, column=0, sticky="ew", padx=2)
        ttk.Button(bottom, text="选择", command=self._choose).grid(
            row=0, column=1, sticky="ew", padx=2)
        ttk.Button(bottom, text="取消", command=self._cancel).grid(
            row=0, column=2, sticky="ew", padx=2)

        self._refresh(self.path_var.get())

    @classmethod
    def ask(cls, master, mode: str, start: str, ui_font: str):
        dialog = cls(master, mode, start, ui_font)
        master.wait_window(dialog)
        return dialog.result

    # -- 列表 ---------------------------------------------------------------
    def _refresh(self, path: str) -> None:
        path = os.path.abspath(path or "/")
        try:
            names = os.listdir(path)
        except OSError as exc:
            messagebox.showerror("无法打开目录", f"{path}\n{exc}", parent=self)
            return
        self.path_var.set(path)
        entries = []
        for name in names:
            full = os.path.join(path, name)
            try:
                is_dir = os.path.isdir(full)
            except OSError:
                is_dir = False
            entries.append((name, full, is_dir))
        entries.sort(key=lambda item: (not item[2], item[0].lower()))
        self.entries = entries
        self.listbox.delete(0, "end")
        for name, _full, is_dir in entries:
            self.listbox.insert("end", (name + "/") if is_dir else name)

    def _selected(self):
        selection = self.listbox.curselection()
        if not selection:
            return None
        index = selection[0]
        if 0 <= index < len(self.entries):
            return self.entries[index]
        return None

    def _quick(self, path) -> None:
        if path is None:
            path = os.path.dirname(self.path_var.get())
        self._refresh(path)

    def _go_path(self) -> None:
        self._refresh(self.path_var.get().strip())

    def _open_selected(self) -> None:
        entry = self._selected()
        if not entry:
            return
        _name, full, is_dir = entry
        if is_dir:
            self._refresh(full)
        elif self.mode == "file":
            self._choose()

    def _choose(self) -> None:
        if self.mode == "dir":
            self.result = self.path_var.get()
            self.destroy()
            return
        entry = self._selected()
        if not entry or entry[2]:
            messagebox.showinfo("请选择文件", "请先点选一个文件，再点「选择」。", parent=self)
            return
        self.result = entry[1]
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class MobileApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.config = load_config()
        self.client_id = uuid.uuid4().hex[:12]
        self.events: "queue.Queue[dict]" = queue.Queue()
        self.root.title(APP_TITLE)

        families = set(tkfont.families(root))
        default_family = next(iter(sorted(families)), "Helvetica")
        self.font_family = self.config.get("font") or pick_font(
            families, UI_FONT_CANDIDATES, default_family)
        if self.font_family not in families:
            self.font_family = default_family
        self.mono_family = pick_font(families, MONO_FONT_CANDIDATES, self.font_family)
        if self.mono_family not in families:
            self.mono_family = self.font_family
        self.has_cjk_font = any(name in families for name in UI_FONT_CANDIDATES[:7])

        # 状态变量
        self.role = tk.StringVar(value=self.config.get("role", "client"))
        self.port_var = tk.StringVar(value=str(self.config.get("port", core.DEFAULT_PORT)))
        self.host_var = tk.StringVar(value=self.config.get("host", "::1"))
        self.zone_var = tk.StringVar(value=str(self.config.get("zone", "")))
        self.password_var = tk.StringVar(value=str(self.config.get("password", "")))
        self.recv_dir_var = tk.StringVar(
            value=self.config.get("recv_dir") or default_recv_dir("received"))
        self.client_recv_dir_var = tk.StringVar(
            value=self.config.get("client_recv_dir") or default_recv_dir("downloads"))
        self.status_text = tk.StringVar(value="未连接")
        self.peer_text = tk.StringVar(value="")
        self.task_text = tk.StringVar(value="空闲")
        self.stats_text = tk.StringVar(value="等待任务")
        self.pending_summary = tk.StringVar(value="还没有选择文件")
        self.hint_text = tk.StringVar(value="")
        self.font_var = tk.StringVar(value=self.font_family)

        # 网络状态
        self.state = "idle"
        self.file_only_mode = False
        self.server_sock = None
        self.server_stop = threading.Event()
        self.name_locks = core.NameLocks()
        self.controls: dict = {}
        self.control_peers: dict = {}
        aliases = self.config.get("client_aliases") or {}
        self.client_aliases: dict = {}
        if isinstance(aliases, dict):
            for address, alias in aliases.items():
                if isinstance(alias, str) and alias.strip():
                    self.client_aliases[str(address)] = alias.strip()[:32]
        self.peer_keys: list = []
        self.peer_addr = ""
        self.push_registry: dict = {}
        self.push_queue: list = []
        self.transfer = None
        self.rows: dict = {}
        self.server_rows: dict = {}
        self.seq = 0
        self.pending_files: list = []
        self.closing = False
        self._recv_signature = None
        self._fit_job = None
        self.measure_font = None
        self._last_pick_dir = os.path.expanduser("~")

        self._build_ui()
        self._configure_style()
        self._apply_role(initial=True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(DRAIN_MS, self._drain)
        self.root.after(AUTO_REFRESH_MS, self._auto_refresh_tick)
        self._set_state("idle")
        self.root.after(100, self._fill_addresses)
        self.root.after(250, self._startup_log)

    def _startup_log(self) -> None:
        self.log("手机版已就绪：选「服务器」点启动监听，或选「客户端」填地址后连接。")
        if not self.has_cjk_font:
            self.log("提示：未找到常见中文字体，若显示为方块可在「连接」页底部更换界面字体。")

    # ------------------------------------------------------------------ #
    # 界面搭建
    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        geometry = os.environ.get("MOBILE_GEOMETRY") or (
            f"{int(sw * 0.98)}x{int(sh * 0.94)}+{int(sw * 0.01)}+{int(sh * 0.02)}")
        self.root.geometry(geometry)
        self.root.minsize(300, 460)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="IPv6 文件传输", font=(self.font_family, 14, "bold")).grid(
            row=0, column=0, sticky="w")
        self.dot = tk.Label(header, text="●", fg=COLORS["idle"],
                            font=(self.font_family, 11))
        self.dot.grid(row=0, column=1, sticky="e")
        ttk.Label(header, textvariable=self.status_text, font=(self.font_family, 10)).grid(
            row=1, column=0, sticky="w")
        ttk.Label(header, textvariable=self.peer_text, foreground=COLORS["muted"],
                  font=(self.font_family, 9), wraplength=sw - 30).grid(
            row=1, column=1, sticky="e")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=6, pady=(0, 6))
        self.tab_conn = ttk.Frame(self.notebook, padding=8)
        self.tab_send = ttk.Frame(self.notebook, padding=8)
        self.tab_recv = ttk.Frame(self.notebook, padding=8)
        self.tab_chat = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(self.tab_conn, text="连接")
        self.notebook.add(self.tab_send, text="发送")
        self.notebook.add(self.tab_recv, text="接收")
        self.notebook.add(self.tab_chat, text="聊天")
        self._build_conn_tab()
        self._build_send_tab()
        self._build_recv_tab()
        self._build_chat_tab()
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

    def _build_conn_tab(self) -> None:
        tab = self.tab_conn
        tab.columnconfigure(1, weight=1)
        row = 0

        ttk.Label(tab, text="角色", font=(self.font_family, 10, "bold")).grid(
            row=row, column=0, sticky="w")
        role_box = ttk.Frame(tab)
        role_box.grid(row=row, column=1, sticky="ew")
        ttk.Radiobutton(role_box, text="服务器", value="server", variable=self.role,
                        command=self._on_role_change).pack(side="left")
        ttk.Radiobutton(role_box, text="客户端", value="client", variable=self.role,
                        command=self._on_role_change).pack(side="left", padx=(12, 0))
        row += 1

        ttk.Label(tab, text="端口").grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(tab, textvariable=self.port_var, width=8).grid(
            row=row, column=1, sticky="w", pady=4)
        row += 1

        # 服务器设置
        self.server_frame = ttk.LabelFrame(tab, text="服务器", padding=6)
        self.server_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        self.server_frame.columnconfigure(1, weight=1)
        ttk.Label(self.server_frame, text="接收目录").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.server_frame, textvariable=self.recv_dir_var).grid(
            row=0, column=1, sticky="ew", padx=4)
        ttk.Button(self.server_frame, text="浏览", width=5,
                   command=self._pick_server_dir).grid(row=0, column=2)
        ttk.Label(self.server_frame, text="连接密码").grid(row=1, column=0, sticky="w",
                                                          pady=(4, 0))
        ttk.Entry(self.server_frame, textvariable=self.password_var, show="*").grid(
            row=1, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Label(self.server_frame, text="留空=不验证", foreground=COLORS["muted"],
                  font=(self.font_family, 8)).grid(row=1, column=2, sticky="e",
                                                   pady=(4, 0))
        btns = ttk.Frame(self.server_frame)
        btns.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        self.start_btn = ttk.Button(btns, text="启动监听", command=self.start_server)
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.stop_btn = ttk.Button(btns, text="停止监听", command=self.stop_server)
        self.stop_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        ttk.Label(self.server_frame, text="在线客户端（不选=全部）",
                  foreground=COLORS["muted"], font=(self.font_family, 9)).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        self.online_list = tk.Listbox(self.server_frame, height=2, exportselection=False,
                                      font=(self.font_family, 10), activestyle="none")
        self.online_list.grid(row=4, column=0, columnspan=3, sticky="ew")
        online_xscroll = ttk.Scrollbar(self.server_frame, orient="horizontal",
                                       command=self.online_list.xview)
        online_xscroll.grid(row=5, column=0, columnspan=3, sticky="ew")
        self.online_list.configure(xscrollcommand=online_xscroll.set)
        self.online_list.bind("<<ListboxSelect>>", lambda _e: self._refresh_buttons())
        self.online_list.bind("<Double-Button-1>",
                              lambda _e: self._set_alias_for_selected())
        self.alias_btn = ttk.Button(self.server_frame, text="设置选中客户端别名",
                                    command=self._set_alias_for_selected)
        self.alias_btn.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        row += 1

        # 客户端设置
        self.client_frame = ttk.LabelFrame(tab, text="客户端", padding=6)
        self.client_frame.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        self.client_frame.columnconfigure(1, weight=1)
        ttk.Label(self.client_frame, text="服务器地址").grid(row=0, column=0, sticky="w")
        ttk.Entry(self.client_frame, textvariable=self.host_var).grid(
            row=0, column=1, columnspan=2, sticky="ew", padx=4)
        ttk.Label(self.client_frame, text="接口索引").grid(row=1, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.client_frame, textvariable=self.zone_var, width=8).grid(
            row=1, column=1, sticky="w", padx=4, pady=(4, 0))
        ttk.Label(self.client_frame, text="接收目录").grid(row=3, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.client_frame, textvariable=self.client_recv_dir_var).grid(
            row=3, column=1, sticky="ew", padx=4, pady=(4, 0))
        ttk.Button(self.client_frame, text="浏览", width=5,
                   command=self._pick_client_dir).grid(row=3, column=2, pady=(4, 0))
        ttk.Label(self.client_frame, text="连接密码").grid(row=2, column=0, sticky="w", pady=(4, 0))
        ttk.Entry(self.client_frame, textvariable=self.password_var, show="*").grid(
            row=2, column=1, columnspan=2, sticky="ew", padx=4, pady=(4, 0))
        cbtns = ttk.Frame(self.client_frame)
        cbtns.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        cbtns.columnconfigure(0, weight=1)
        cbtns.columnconfigure(1, weight=1)
        self.connect_btn = ttk.Button(cbtns, text="连接", command=self.connect_peer)
        self.connect_btn.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.disconnect_btn = ttk.Button(cbtns, text="断开", command=self.disconnect_peer)
        self.disconnect_btn.grid(row=0, column=1, sticky="ew", padx=(4, 0))
        row += 1

        # 本机地址
        addr_box = ttk.LabelFrame(tab, text="本机 IPv6 地址", padding=6)
        addr_box.grid(row=row, column=0, columnspan=2, sticky="ew", pady=4)
        addr_box.columnconfigure(0, weight=1)
        self.addr_list = tk.Listbox(addr_box, height=2, exportselection=False,
                                    font=(self.mono_family, 9), activestyle="none")
        self.addr_list.grid(row=0, column=0, sticky="ew")
        addr_xscroll = ttk.Scrollbar(addr_box, orient="horizontal",
                                     command=self.addr_list.xview)
        addr_xscroll.grid(row=1, column=0, sticky="ew")
        self.addr_list.configure(xscrollcommand=addr_xscroll.set)
        addr_bar = ttk.Frame(addr_box)
        addr_bar.grid(row=2, column=0, sticky="ew", pady=(4, 0))
        addr_bar.columnconfigure(0, weight=1)
        addr_bar.columnconfigure(1, weight=1)
        addr_bar.columnconfigure(2, weight=1)
        ttk.Button(addr_bar, text="复制选中", command=self._copy_address).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(addr_bar, text="刷新地址", command=self._fill_addresses).grid(
            row=0, column=1, sticky="ew", padx=3)
        self.config_btn = ttk.Button(addr_bar, text="分享配置",
                                     command=self._on_config_button)
        self.config_btn.grid(row=0, column=2, sticky="ew", padx=(3, 0))
        row += 1

        # 字体
        font_box = ttk.Frame(tab)
        font_box.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        font_box.columnconfigure(1, weight=1)
        ttk.Label(font_box, text="界面字体").grid(row=0, column=0, sticky="w")
        families = sorted(tkfont.families(self.root))
        self.font_combo = ttk.Combobox(font_box, textvariable=self.font_var,
                                       values=families, state="readonly", height=10)
        self.font_combo.grid(row=0, column=1, sticky="ew", padx=4)
        self.font_combo.bind("<<ComboboxSelected>>", lambda _e: self._apply_font())

    def _build_send_tab(self) -> None:
        tab = self.tab_send
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(6, weight=1)

        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)
        bar.columnconfigure(2, weight=1)
        ttk.Button(bar, text="添加文件", command=self._pick_files).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        self.send_btn = ttk.Button(bar, text="开始发送", command=self.start_send)
        self.send_btn.grid(row=0, column=1, sticky="ew", padx=3)
        self.cancel_btn = ttk.Button(bar, text="取消", command=self.cancel_transfer)
        self.cancel_btn.grid(row=0, column=2, sticky="ew", padx=(3, 0))

        ttk.Label(tab, textvariable=self.pending_summary, foreground=COLORS["muted"],
                  font=(self.font_family, 9)).grid(row=1, column=0, sticky="w", pady=(6, 2))
        pending_box = ttk.Frame(tab)
        pending_box.grid(row=2, column=0, sticky="ew")
        pending_box.columnconfigure(0, weight=1)
        self.pending_list = tk.Listbox(pending_box, height=3, exportselection=False,
                                       font=(self.font_family, 10), activestyle="none")
        self.pending_list.grid(row=0, column=0, sticky="ew")
        pscroll = ttk.Scrollbar(pending_box, orient="vertical", command=self.pending_list.yview)
        pscroll.grid(row=0, column=1, sticky="ns")
        self.pending_list.configure(yscrollcommand=pscroll.set)
        pending_bar = ttk.Frame(tab)
        pending_bar.grid(row=3, column=0, sticky="ew", pady=(3, 0))
        pending_bar.columnconfigure(0, weight=1)
        pending_bar.columnconfigure(1, weight=1)
        ttk.Button(pending_bar, text="移除选中", command=self._remove_pending).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(pending_bar, text="清空", command=self._clear_pending).grid(
            row=0, column=1, sticky="ew", padx=(3, 0))

        prog = ttk.LabelFrame(tab, text="当前任务", padding=6)
        prog.grid(row=4, column=0, sticky="ew", pady=(8, 4))
        prog.columnconfigure(0, weight=1)
        ttk.Label(prog, textvariable=self.task_text, font=(self.font_family, 10, "bold")).grid(
            row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(prog, maximum=1000, value=0)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(3, 2))
        ttk.Label(prog, textvariable=self.stats_text, font=(self.mono_family, 8),
                  foreground=COLORS["muted"]).grid(row=2, column=0, sticky="w")

        hist_head = ttk.Frame(tab)
        hist_head.grid(row=5, column=0, sticky="ew", pady=(2, 2))
        hist_head.columnconfigure(0, weight=1)
        ttk.Label(hist_head, text="传输记录").grid(row=0, column=0, sticky="w")
        ttk.Button(hist_head, text="左移", width=4,
                   command=lambda: self.history.xview_scroll(-1, "pages")).grid(
            row=0, column=1, padx=(4, 0))
        ttk.Button(hist_head, text="右移", width=4,
                   command=lambda: self.history.xview_scroll(1, "pages")).grid(
            row=0, column=2, padx=(4, 0))
        ttk.Button(hist_head, text="详情", width=5, command=self._show_details).grid(
            row=0, column=3, padx=(4, 0))

        hist_box = ttk.Frame(tab)
        hist_box.grid(row=6, column=0, sticky="nsew")
        hist_box.columnconfigure(0, weight=1)
        hist_box.rowconfigure(0, weight=1)
        self.history = ttk.Treeview(hist_box, columns=("name", "peer", "dir", "status", "check"),
                                    show="headings", height=5, selectmode="browse")
        for key, title, width, anchor, stretch in (
            ("name", "文件", 150, "w", True),
            ("peer", "对端", 100, "w", False),
            ("dir", "方向", 60, "center", False),
            ("status", "状态", 80, "center", False),
            ("check", "校验", 50, "center", False),
        ):
            self.history.heading(key, text=title)
            self.history.column(key, width=width, anchor=anchor, stretch=stretch)
        self.history.grid(row=0, column=0, sticky="nsew")
        hscroll = ttk.Scrollbar(hist_box, orient="vertical", command=self.history.yview)
        hscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(hist_box, orient="horizontal", command=self.history.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.history.configure(yscrollcommand=hscroll.set, xscrollcommand=xscroll.set)
        self.history.tag_configure("ok", foreground=COLORS["ok"])
        self.history.tag_configure("bad", foreground=COLORS["bad"])
        self.history.tag_configure("run", foreground=COLORS["run"])
        self.history.tag_configure("muted", foreground=COLORS["muted"])
        self.history.bind("<Configure>", lambda _e: self._fit_history_columns())
        self.history.bind("<ButtonRelease-1>", self._on_history_tap)

    def _build_recv_tab(self) -> None:
        tab = self.tab_recv
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(2, weight=1)
        self.recv_path_label = ttk.Label(tab, text="", wraplength=240,
                                        font=(self.mono_family, 8),
                                        foreground=COLORS["muted"])
        self.recv_path_label.grid(row=0, column=0, sticky="w")
        bar = ttk.Frame(tab)
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 4))
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)
        bar.columnconfigure(2, weight=1)
        bar.columnconfigure(3, weight=1)
        ttk.Button(bar, text="复制路径", command=self._copy_receive_path).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(bar, text="刷新", command=lambda: self._refresh_local_list(force=True)).grid(
            row=0, column=1, sticky="ew", padx=3)
        ttk.Button(bar, text="左移",
                   command=lambda: self.recv_tree.xview_scroll(-1, "pages")).grid(
            row=0, column=2, sticky="ew", padx=3)
        ttk.Button(bar, text="右移",
                   command=lambda: self.recv_tree.xview_scroll(1, "pages")).grid(
            row=0, column=3, sticky="ew", padx=(3, 0))

        box = ttk.Frame(tab)
        box.grid(row=2, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.recv_tree = ttk.Treeview(box, columns=("name", "size", "mtime"),
                                     show="headings", height=8)
        for key, title, width, anchor, stretch in (
            ("name", "名称", 150, "w", True),
            ("size", "大小", 70, "e", False),
            ("mtime", "修改时间", 100, "w", False),
        ):
            self.recv_tree.heading(key, text=title)
            self.recv_tree.column(key, width=width, anchor=anchor, stretch=stretch)
        self.recv_tree.grid(row=0, column=0, sticky="nsew")
        rscroll = ttk.Scrollbar(box, orient="vertical", command=self.recv_tree.yview)
        rscroll.grid(row=0, column=1, sticky="ns")
        rxscroll = ttk.Scrollbar(box, orient="horizontal", command=self.recv_tree.xview)
        rxscroll.grid(row=1, column=0, sticky="ew")
        self.recv_tree.configure(yscrollcommand=rscroll.set,
                                 xscrollcommand=rxscroll.set)
        self.recv_tree.bind("<Configure>", lambda _e: self._fit_recv_columns())
        self.recv_tree.bind("<ButtonRelease-1>", self._on_recv_tap)
        ttk.Label(tab, text="收到文件后会自动出现在这里（每 3 秒自动刷新）。",
                  foreground=COLORS["muted"], font=(self.font_family, 9)).grid(
            row=3, column=0, sticky="w", pady=(4, 0))

    def _build_chat_tab(self) -> None:
        tab = self.tab_chat
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        self.chat = tk.Text(tab, height=8, width=40, wrap="word", state="disabled",
                            font=(self.font_family, 10), background="#ffffff",
                            relief="flat", padx=8, pady=6)
        self.chat.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.chat.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.chat.configure(yscrollcommand=scroll.set)
        self.chat.tag_configure("me", foreground=COLORS["me"])
        self.chat.tag_configure("peer", foreground=COLORS["peer"])
        self.chat.tag_configure("sys", foreground=COLORS["sys"])
        self.chat.tag_configure("stamp", foreground="#9aa0a6")

        input_row = ttk.Frame(tab)
        input_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        input_row.columnconfigure(0, weight=1)
        self.chat_input = tk.Text(input_row, height=2, width=30, wrap="word",
                                  font=(self.font_family, 10), relief="solid",
                                  borderwidth=1, padx=6, pady=4)
        self.chat_input.grid(row=0, column=0, sticky="ew")
        self.chat_send_btn = ttk.Button(input_row, text="发送", width=6,
                                        command=self.send_text)
        self.chat_send_btn.grid(row=0, column=1, sticky="ns", padx=(6, 0))
        self.hint_label = ttk.Label(tab, textvariable=self.hint_text,
                                    foreground=COLORS["muted"],
                                    font=(self.font_family, 9), wraplength=240)
        self.hint_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.root.bind("<Configure>", self._on_resize)

    def _configure_style(self) -> None:
        style = ttk.Style(self.root)
        for theme in ("clam", "default"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure(".", font=(self.font_family, 10))
        style.configure("TButton", font=(self.font_family, 10), padding=(8, 7))
        style.configure("TLabel", font=(self.font_family, 10))
        style.configure("TLabelframe.Label", font=(self.font_family, 10, "bold"))
        try:
            self.measure_font = tkfont.Font(root=self.root,
                                            font=(self.font_family, 9))
            line_height = self.measure_font.metrics("linespace")
        except Exception:
            self.measure_font = None
            line_height = 20
        style.configure("Treeview", font=(self.font_family, 9),
                        rowheight=max(30, line_height + 10))
        style.configure("Treeview.Heading", font=(self.font_family, 9, "bold"))
        style.configure("TNotebook.Tab", font=(self.font_family, 10), padding=(10, 6))
        self._schedule_table_fit(100)

    def _schedule_table_fit(self, delay: int = 80) -> None:
        if self.closing:
            return
        if self._fit_job is not None:
            return
        try:
            self._fit_job = self.root.after(delay, self._run_table_fit)
        except tk.TclError:
            self._fit_job = None

    def _run_table_fit(self) -> None:
        self._fit_job = None
        try:
            self._fit_tables()
        except tk.TclError:
            pass

    def _fit_tables(self) -> None:
        self._fit_history_columns()
        self._fit_recv_columns()

    def _fit_history_columns(self) -> None:
        if not hasattr(self, "history"):
            return
        try:
            width = self.history.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            return
        width = max(width, 240)
        peer = max(int(width * 0.20), 80)
        direction = max(int(width * 0.10), 40)
        status = max(int(width * 0.14), 46)
        check = max(int(width * 0.18), 62)
        name = max(width - peer - direction - status - check, 90)
        longest_name = 0
        longest_peer = 0
        if self.measure_font is not None:
            try:
                for item in self.history.get_children():
                    text = str(self.history.set(item, "name"))
                    longest_name = max(longest_name,
                                       self.measure_font.measure(text) + 28)
                    peer_text = str(self.history.set(item, "peer"))
                    longest_peer = max(longest_peer,
                                       self.measure_font.measure(peer_text) + 24)
            except tk.TclError:
                longest_name = longest_peer = 0
        name = max(name, min(longest_name, 2400))
        peer = max(peer, min(longest_peer, 900))
        for key, value, minimum, stretch in (
            ("name", name, 80, False),
            ("peer", peer, 70, False),
            ("dir", direction, 40, False),
            ("status", status, 44, False),
            ("check", check, 60, False),
        ):
            self.history.column(key, width=value, minwidth=minimum,
                                stretch=stretch)

    def _fit_recv_columns(self) -> None:
        if not hasattr(self, "recv_tree"):
            return
        try:
            width = self.recv_tree.winfo_width()
        except tk.TclError:
            return
        if width <= 1:
            return
        width = max(width, 240)
        size = max(int(width * 0.18), 54)
        mtime = max(int(width * 0.27), 92)
        name = max(width - size - mtime, 100)
        longest = 0
        if self.measure_font is not None:
            try:
                for item in self.recv_tree.get_children():
                    text = str(self.recv_tree.set(item, "name"))
                    longest = max(longest, self.measure_font.measure(text) + 28)
            except tk.TclError:
                longest = 0
        name = max(name, min(longest, 2400))
        for key, value, minimum in (
            ("name", name, 80),
            ("size", size, 50),
            ("mtime", mtime, 88),
        ):
            self.recv_tree.column(key, width=value, minwidth=minimum,
                                  stretch=False)

    # ------------------------------------------------------------------ #
    # 事件泵 / 状态
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
        for _ in range(150):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(event)
            except Exception as exc:          # 单个事件出错不拖垮界面
                try:
                    self.log(f"[界面异常] {exc}")
                except tk.TclError:
                    return
                print(f"[界面异常] {type(exc).__name__}: {exc}", file=sys.stderr)
        if not self.closing:
            try:
                self.root.after(DRAIN_MS, self._drain)
            except tk.TclError:
                pass

    def _handle(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "log":
            self.log(event.get("text", ""))
        elif etype == "state":
            self._set_state(event.get("state", "idle"), event.get("peer"))
        elif etype == "text":
            self._append_chat(event.get("text", ""), from_peer=True, ts=event.get("ts"),
                              sender=event.get("peer"))
        elif etype == "notice":
            self._append_chat(event.get("text", ""), from_peer=True,
                              ts=event.get("ts"), system=True)
        elif etype == "push":
            self._handle_push(event)
        elif etype == "control_ready":
            self._adopt_control(event.get("link"), event.get("key", ""),
                                event.get("peer", ""))
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
        elif etype == "transfer_end":
            self._finish_batch()
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
        else:
            self.log(f"未知事件：{etype}")

    def _set_state(self, state: str, peer: str = None) -> None:
        self.state = state
        if peer is not None:
            self.peer_addr = peer
            self.peer_text.set(peer)
        color = {
            "idle": COLORS["idle"], "listening": COLORS["listen"],
            "connected": COLORS["online"], "file_only": COLORS["online"],
            "busy": COLORS["busy"], "connecting": COLORS["busy"],
        }.get(state, COLORS["idle"])
        text = {
            "idle": "未连接", "connecting": "连接中…", "listening": "监听中",
            "connected": "已连接", "file_only": "已连接（仅文件）", "busy": "传输中",
        }.get(state, state)
        if self.role.get() == "server" and state in ("listening", "connected"):
            text = "监听中" + (f" · {len(self.controls)} 个客户端在线"
                              if self.controls else "")
        self.dot.config(fg=color)
        self.status_text.set(text)
        self._refresh_buttons()

    def log(self, message: str) -> None:
        # 手机版没有单独日志区，写到聊天页的系统消息，方便回看
        self._append_chat(message, from_peer=False, system=True, quiet=True)

    def _on_resize(self, event) -> None:
        if event.widget is not self.root:
            return
        width = max(self.root.winfo_width() - 50, 200)
        try:
            self.recv_path_label.configure(wraplength=width)
            self.hint_label.configure(wraplength=width)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # 角色与连接
    # ------------------------------------------------------------------ #
    def _apply_role(self, initial: bool = False) -> None:
        is_server = self.role.get() == "server"
        if is_server:
            self.client_frame.grid_remove()
            self.server_frame.grid()
            self.send_btn.configure(text="开始发送")
        else:
            self.server_frame.grid_remove()
            self.client_frame.grid()
            self.send_btn.configure(text="开始上传")
        if hasattr(self, "config_btn"):
            self.config_btn.configure(text="分享配置" if is_server else "导入配置")
        self._recv_signature = None
        self._refresh_peers_view()
        self._refresh_local_list()
        self._refresh_buttons()
        if not initial:
            self.log("已切换到服务器模式。" if is_server else "已切换到客户端模式。")

    def _on_role_change(self) -> None:
        if self.state == "busy":
            messagebox.showinfo("正在传输", "请等传输结束或取消后再切换角色。")
            self.role.set("server" if self.role.get() == "client" else "client")
            return
        self.stop_server()
        self.disconnect_peer()
        self._apply_role()

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
            enable(self.connect_btn, not connected and not locked)
            enable(self.disconnect_btn, connected)
            enable(self.send_btn, connected and not locked and bool(self.pending_files))
            enable(self.start_btn, False)
            enable(self.stop_btn, False)
        else:
            enable(self.start_btn, not listening and not locked)
            enable(self.stop_btn, listening and not locked)
            enable(self.send_btn, listening and not locked and has_control
                   and bool(self.pending_files))
            enable(self.connect_btn, False)
            enable(self.disconnect_btn, False)
        enable(self.cancel_btn, busy)
        enable(self.chat_send_btn, has_control)
        if has_control:
            if str(self.chat_input.cget("state")) == "disabled":
                self.chat_input.configure(state="normal")
        else:
            self.chat_input.configure(state="disabled")
        self._update_hint()

    def _update_hint(self) -> None:
        if self.role.get() == "server":
            count = len(self.controls)
            if count:
                self.hint_text.set(f"{count} 个客户端在线：不选中则群发，选中则只发给选中项。")
            else:
                self.hint_text.set("还没有客户端连接；客户端连接后即可收发消息。")
        else:
            if self.controls:
                self.hint_text.set(f"已连接 {self.peer_addr}，点「发送」发出消息。")
            elif self.file_only_mode:
                self.hint_text.set("仅文件模式：对方是命令行服务器，不支持文字消息。")
            else:
                self.hint_text.set("未连接：先在「连接」页填服务器地址并连接。")

    def _adopt_control(self, link, key: str, peer: str = "") -> None:
        if link is None:
            if key:
                existing = self.controls.get(key)
                if existing is not None and not existing.is_closed:
                    return
                if key in self.controls:
                    self.controls.pop(key, None)
                    self.control_peers.pop(key, None)
                    self.log("连接已断开。")
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
                self.peer_text.set(self.peer_addr)
                self.log(f"已连接到 {self.peer_addr}。")
                self.notebook.select(self.tab_send)
            else:
                display = self._peer_label(key)
                self.log(f"客户端 {display} 已连接。")
        if self.role.get() == "server":
            self._refresh_peers_view()
            self._set_state("connected" if self.controls else
                            ("listening" if self.server_sock is not None else "idle"))
        else:
            if self.controls:
                self._set_state("connected")
            else:
                self.peer_addr = ""
                self.peer_text.set("")
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
        self.peer_text.set(f"{len(keys)} 个客户端在线" if keys else "")

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
                row["remote"] = self._remote_label(
                    peer or self.control_peers.get(key, ""), client_id or key)
                self._update_row(row_id)
        self._schedule_table_fit()

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
        alias = self._ask_alias_dialog(peer, current)
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

    def _ask_alias_dialog(self, peer: str, current: str):
        window = tk.Toplevel(self.root)
        window.title("设置客户端别名")
        window.transient(self.root)
        window.grab_set()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"{int(sw * 0.92)}x{int(sh * 0.42)}+{int(sw * 0.04)}+{int(sh * 0.2)}")
        window.columnconfigure(0, weight=1)
        ttk.Label(window, text=f"客户端：{peer}",
                  font=(self.font_family, 10), wraplength=sw - 60).grid(
            row=0, column=0, sticky="w", padx=10, pady=(12, 4))
        ttk.Label(window, text="输入便于识别的别名（如：我的手机 / 小明电脑）：",
                  foreground=COLORS["muted"], font=(self.font_family, 9),
                  wraplength=sw - 60).grid(row=1, column=0, sticky="w", padx=10)
        value = tk.StringVar(value=current)
        entry = ttk.Entry(window, textvariable=value)
        entry.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 0))
        entry.focus_set()
        result = {"value": None}

        def save() -> None:
            result["value"] = value.get()
            window.destroy()

        bar = ttk.Frame(window)
        bar.grid(row=3, column=0, sticky="ew", padx=10, pady=10)
        for index in range(3):
            bar.columnconfigure(index, weight=1)
        ttk.Button(bar, text="保存", command=save).grid(
            row=0, column=0, sticky="ew", padx=(0, 3))
        ttk.Button(bar, text="清除别名",
                   command=lambda: (result.update({"value": ""}),
                                    window.destroy())).grid(
            row=0, column=1, sticky="ew", padx=3)
        ttk.Button(bar, text="取消", command=window.destroy).grid(
            row=0, column=2, sticky="ew", padx=(3, 0))
        window.bind("<Return>", lambda _e: save())
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self.root.wait_window(window)
        return result["value"]

    def _target_peer_links(self):
        if not self.controls:
            return []
        if self.role.get() == "client":
            return list(self.controls.items())
        selected = [self.peer_keys[i] for i in self.online_list.curselection()
                    if 0 <= i < len(self.peer_keys)]
        if not selected:
            return list(self.controls.items())
        return [(key, self.controls[key]) for key in selected if key in self.controls]

    # ------------------------------------------------------------------ #
    # 服务器
    # ------------------------------------------------------------------ #
    def start_server(self) -> None:
        if self.server_sock is not None:
            return
        try:
            port = int(self.port_var.get())
        except ValueError:
            messagebox.showerror("端口无效", "端口必须是 1..65535 的整数")
            return
        if not (1 <= port <= 65535):
            messagebox.showerror("端口无效", "端口必须是 1..65535 的整数")
            return
        directory = self.recv_dir_var.get().strip() or default_recv_dir("received")
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("接收目录不可用", str(exc))
            return
        self.recv_dir_var.set(directory)
        password = self.password_var.get()
        if len(password) > core.MAX_PASSWORD_CHARS:
            messagebox.showerror("密码太长",
                                 f"连接密码最多 {core.MAX_PASSWORD_CHARS} 个字符")
            return
        try:
            sock = core.make_listener("::", port, False)
        except OSError as exc:
            messagebox.showerror("监听失败", f"无法监听 [::]:{port}\n{exc}")
            return
        self.server_sock = sock
        self.server_stop.clear()
        self.push_registry.clear()
        self._recv_signature = None
        threading.Thread(target=self._accept_loop, args=(sock,), daemon=True).start()
        self._set_state("listening")
        self._fill_addresses()
        self.notebook.select(self.tab_send)
        self.log(f"服务器已启动，端口 {port}，接收目录：{directory}")
        if password:
            self.log("已启用连接密码，客户端必须填写相同密码才能连接。")

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
        for link in list(self.controls.values()):
            try:
                link.close()
            except OSError:
                pass
        self.controls.clear()
        self.control_peers.clear()
        self._refresh_peers_view()
        self._set_state("idle")
        self.log("服务器已停止。")

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
                             daemon=True).start()

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
                keep_socket = True
                return
            def emit(event: dict, peer=peer, cid=client_id) -> None:
                self._server_emit(peer, cid, event)

            core.handle_transfer_session(conn, self.recv_dir_var.get(),
                                         timeout=120.0, locks=self.name_locks,
                                         emit=emit, path_resolver=self._resolve_push_path)
        except core.HelloRejected:
            pass
        except core.ProtocolError as exc:
            self.post("log", text=f"{peer} 协议错误：{exc}")
        except (ConnectionError, OSError):
            pass
        except Exception as exc:
            self.post("log", text=f"{peer} 会话异常：{exc}")
        finally:
            if not keep_socket:
                try:
                    conn.sock.close()
                except OSError:
                    pass
            self.post("server_session_end", peer=peer)

    def _server_emit(self, peer: str, client_id: str, event: dict) -> None:
        etype = event.get("type")
        if etype == "start":
            self.post("server_start", peer=peer, client_id=client_id,
                      direction=event.get("direction", "receive"),
                      name=event.get("name", ""), size=event.get("size", 0),
                      resumed_from=event.get("resumed_from", 0))
        elif etype == "progress":
            self.post("server_progress", peer=peer, direction=event.get("direction"),
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
        row = self._new_row(direction, name, event.get("size", 0))
        self.rows[row]["server_peer"] = peer
        self.rows[row]["server_client_id"] = client_id
        self.rows[row]["remote"] = self._remote_label(peer, client_id)
        self.server_rows[(event.get("peer"), event.get("direction"), name)] = row
        self.rows[row]["status"] = "进行中"
        self._update_row(row)
        self.task_text.set(f"{direction}：{name}")
        self.progress.configure(value=0)
        self.stats_text.set("正在传输…")

    def _handle_server_progress(self, event: dict) -> None:
        row = self.server_rows.get((event.get("peer"), event.get("direction"),
                                    event.get("name")))
        if row is None:
            return
        self._handle_progress({"row": row, "done": event.get("done", 0),
                               "total": event.get("total", 0),
                               "fraction": event.get("fraction", 0.0),
                               "rate": event.get("rate", 0.0),
                               "eta": event.get("eta")})

    def _handle_server_done(self, event: dict) -> None:
        data = event.get("result") or {}
        row = self.server_rows.get((event.get("peer"), data.get("direction"),
                                    data.get("name")))
        if row is not None:
            self._handle_done({"row": row, "result": data})

    def _handle_server_failed(self, event: dict) -> None:
        row = self.server_rows.get((event.get("peer"), event.get("direction"),
                                    event.get("name")))
        if row is not None:
            self._handle_failed({"row": row, "message": event.get("message", ""),
                                 "result": event.get("result") or {}})
        else:
            self.log(f"{event.get('peer')} {event.get('name')}：{event.get('message')}")

    def _handle_server_session_end(self, event: dict) -> None:
        peer = event.get("peer")
        for key, row_id in list(self.server_rows.items()):
            if key[0] != peer:
                continue
            row = self.rows.get(row_id)
            if row is not None and not row.get("finished"):
                self._handle_failed({"row": row_id,
                                     "message": "连接中断，未完成部分已保留为 .part",
                                     "result": {}})

    def _maybe_finish_server(self) -> None:
        pending = [r for r in self.rows.values()
                   if r["direction"] in ("接收", "发送") and not r["finished"]]
        if not pending:
            self.progress.configure(value=0)
            self.task_text.set("空闲")
            self.stats_text.set("等待任务")
            self._refresh_local_list(force=True)
            self._refresh_buttons()

    def _resolve_push_path(self, token: str):
        path = self.push_registry.get(str(token or ""))
        return path if path and os.path.isfile(path) else None

    # ------------------------------------------------------------------ #
    # 客户端连接
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
        if len(self.password_var.get()) > core.MAX_PASSWORD_CHARS:
            messagebox.showerror("密码太长",
                                 f"连接密码最多 {core.MAX_PASSWORD_CHARS} 个字符")
            return
        self._set_state("connecting")
        threading.Thread(target=self._connect_worker,
                         args=(host, port, self.zone_var.get().strip(),
                               self.password_var.get()),
                         daemon=True).start()

    def _connect_worker(self, host: str, port: int, zone: str, password: str) -> None:
        try:
            link = core.ControlLink.connect(
                host, port, zone, timeout=120.0, connect_timeout=10.0,
                password=password,
                on_text=lambda text, ts: self.post("text", text=text, from_peer=True,
                                                   ts=ts, peer="服务器"),
                on_state=lambda up: self.post("control_down", key=self.client_id,
                                              peer="") if not up else None,
                on_push=lambda name, size, token: self.post(
                    "push", name=name, size=size, token=token, peer="服务器"),
                on_notice=lambda text, ts: self.post("notice", text=text, ts=ts,
                                                     peer="服务器"),
                client_id=self.client_id,
            )
            self.post("control_ready", link=link, key=self.client_id, peer=link.peer)
        except core.ConnectError as exc:
            message = str(exc)
            if "未启用文本消息" in message:
                self.file_only_mode = True
                self.post("log", text="对端不支持文字消息，已进入仅文件模式。")
                self.post("state", state="file_only", peer=f"[{host}]:{port}")
            else:
                self.post("connect_failed", message=message)

    def disconnect_peer(self) -> None:
        for link in list(self.controls.values()):
            try:
                link.close()
            except OSError:
                pass
        self.controls.clear()
        self.control_peers.clear()
        self.file_only_mode = False
        self.peer_addr = ""
        self.peer_text.set("")
        self._set_state("idle")
        self._refresh_buttons()
        self.log("已断开连接。")

    # ------------------------------------------------------------------ #
    # 待发送文件
    # ------------------------------------------------------------------ #
    def _pick_files(self) -> None:
        path = FileBrowser.ask(self.root, "file", self._last_pick_dir, self.font_family)
        if not path:
            return
        self._last_pick_dir = os.path.dirname(path)
        self.add_pending_files([path])

    def add_pending_files(self, paths) -> None:
        known = {str(p.resolve()) for p in self.pending_files}
        added = 0
        for raw in paths:
            path = Path(raw)
            if not path.is_file():
                continue
            key = str(path.resolve())
            if key in known:
                continue
            known.add(key)
            self.pending_files.append(path)
            added += 1
        if added:
            self._refresh_pending_view()
            self.log(f"已添加 {added} 个待发送文件。")

    def queue_upload(self, paths) -> None:
        self.pending_files = [Path(p) for p in paths if Path(p).is_file()]
        self._refresh_pending_view()

    def _refresh_pending_view(self) -> None:
        self.pending_list.delete(0, "end")
        total = 0
        for path in self.pending_files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            total += size
            self.pending_list.insert("end", f"{path.name}  ({core.human_bytes(size)})")
        if self.pending_files:
            self.pending_summary.set(f"待发送 {len(self.pending_files)} 个，共 "
                                     f"{core.human_bytes(total)}")
        else:
            self.pending_summary.set("还没有选择文件")
        self._refresh_buttons()

    def _remove_pending(self) -> None:
        selection = self.pending_list.curselection()
        if not selection:
            return
        for index in sorted(selection, reverse=True):
            if 0 <= index < len(self.pending_files):
                self.pending_files.pop(index)
        self._refresh_pending_view()

    def _clear_pending(self) -> None:
        self.pending_files.clear()
        self._refresh_pending_view()

    def _pick_server_dir(self) -> None:
        path = FileBrowser.ask(self.root, "dir", self.recv_dir_var.get(),
                               self.font_family)
        if path:
            self.recv_dir_var.set(path)
            self._recv_signature = None
            self._refresh_local_list(force=True)

    def _pick_client_dir(self) -> None:
        path = FileBrowser.ask(self.root, "dir", self.client_recv_dir_var.get(),
                               self.font_family)
        if path:
            self.client_recv_dir_var.set(path)
            self._recv_signature = None
            self._refresh_local_list(force=True)

    # ------------------------------------------------------------------ #
    # 发送
    # ------------------------------------------------------------------ #
    def start_send(self) -> None:
        if self.role.get() == "client":
            self._start_upload()
        else:
            self._send_from_server()

    def _start_upload(self) -> None:
        if self.state in ("idle", "listening", "busy", "connecting"):
            return
        files = [p for p in self.pending_files if p.is_file()]
        if not files:
            return
        pairs = []
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            pairs.append((path, self._new_row("上传", path.name, size)))
        self.pending_files.clear()
        self._refresh_pending_view()
        self.transfer = {"cancel": threading.Event(), "sock": None}
        self._set_state("busy")
        self.log(f"开始上传 {len(pairs)} 个文件。")
        threading.Thread(target=self._upload_worker, args=(pairs,), daemon=True).start()

    def _upload_worker(self, pairs) -> None:
        host = self.host_var.get().strip()
        port = int(self.port_var.get())
        zone = self.zone_var.get().strip()
        cancel = self.transfer["cancel"]
        conn = None
        try:
            conn, _peer, _reply = core.resolve_and_connect(
                host, port, zone, connect_timeout=10.0, timeout=120.0,
                role="transfer", app="file_gui_mobile/1.0",
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
                )
                if cancel.is_set() and not result.ok:
                    self.post("cancelled", row=row_id)
                else:
                    self.post("done" if result.ok else "failed", row=row_id,
                              result=result.data, message=result.error)
        except (core.ConnectError, ConnectionError, OSError, core.ProtocolError) as exc:
            if cancel.is_set():
                for _path, row_id in pairs:
                    if not self.rows.get(row_id, {}).get("finished"):
                        self.post("cancelled", row=row_id)
            else:
                self.post("log", text=f"上传中断：{exc}")
                for _path, row_id in pairs:
                    if not self.rows.get(row_id, {}).get("finished"):
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

    def _send_from_server(self) -> None:
        files = [p for p in self.pending_files if p.is_file()]
        if not files:
            messagebox.showinfo("没有文件", "请先添加要发送的文件。")
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
                token = uuid.uuid4().hex
                self.push_registry[token] = absolute
                try:
                    link.send_push(path.name, size, token)
                    sent += 1
                except (OSError, ConnectionError, core.ProtocolError, ValueError) as exc:
                    failed += 1
                    self.push_registry.pop(token, None)
                    self.log(f"发送给 {self._peer_label(key)} 失败：{exc}")
        self.pending_files.clear()
        self._refresh_pending_view()
        self.log(f"已通知 {len(targets)} 个客户端接收 {len(files)} 个文件"
                 + (f"（失败 {failed} 次）" if failed else "。"))

    # ------------------------------------------------------------------ #
    # 接收
    # ------------------------------------------------------------------ #
    def _handle_push(self, event: dict) -> None:
        if self.role.get() != "client":
            return
        name = event.get("name") or ""
        size = int(event.get("size") or 0)
        token = event.get("token")
        if not name:
            return
        self.push_queue.append((name, token if isinstance(token, str) else None))
        self.log(f"收到服务器发来的文件：{name}（{core.human_bytes(size)}）")
        if self.state not in ("busy", "connecting") and self.controls:
            items = self.push_queue[:]
            self.push_queue.clear()
            self.start_receive(items)

    def start_receive(self, items) -> None:
        if self.state in ("busy", "connecting") or not items:
            return
        target = self._current_recv_dir()
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("接收目录不可用", str(exc))
            return
        pairs = [(name, token, self._new_row("推送", name, 0))
                 for name, token in items]
        self.transfer = {"cancel": threading.Event(), "sock": None}
        self._set_state("busy")
        self.log(f"开始接收 {len(pairs)} 个文件到 {target}。")
        threading.Thread(target=self._download_worker, args=(pairs, target),
                         daemon=True).start()

    def _download_worker(self, pairs, target: str) -> None:
        host = self.host_var.get().strip()
        port = int(self.port_var.get())
        zone = self.zone_var.get().strip()
        cancel = self.transfer["cancel"]
        conn = None
        try:
            conn, _peer, _reply = core.resolve_and_connect(
                host, port, zone, connect_timeout=10.0, timeout=120.0,
                role="transfer", app="file_gui_mobile/1.0",
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
                )
                if cancel.is_set() and not result.ok:
                    self.post("cancelled", row=row_id)
                else:
                    self.post("done" if result.ok else "failed", row=row_id,
                              result=result.data, message=result.error)
        except (core.ConnectError, ConnectionError, OSError, core.ProtocolError) as exc:
            if cancel.is_set():
                for _name, _token, row_id in pairs:
                    if not self.rows.get(row_id, {}).get("finished"):
                        self.post("cancelled", row=row_id)
            else:
                self.post("log", text=f"接收中断：{exc}")
                for _name, _token, row_id in pairs:
                    if not self.rows.get(row_id, {}).get("finished"):
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
        if not self.transfer:
            return
        self.transfer["cancel"].set()
        sock = self.transfer.get("sock")
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.log("正在取消传输（未完成部分保留为 .part，可续传）。")

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
        self._refresh_local_list(force=True)
        self._refresh_buttons()
        if self.role.get() == "client" and self.push_queue and self.controls:
            items = self.push_queue[:]
            self.push_queue.clear()
            self.start_receive(items)

    def _current_recv_dir(self) -> str:
        if self.role.get() == "client":
            return os.path.abspath(self.client_recv_dir_var.get().strip()
                                   or default_recv_dir("downloads"))
        return os.path.abspath(self.recv_dir_var.get().strip()
                               or default_recv_dir("received"))

    def _refresh_local_list(self, force: bool = False) -> None:
        directory = self._current_recv_dir()
        try:
            self.recv_path_label.configure(text=f"接收目录：{directory}")
        except tk.TclError:
            return
        files = []
        try:
            with os.scandir(directory) as it:
                for entry in it:
                    if not entry.is_file() or entry.name.endswith(".part"):
                        continue
                    st = entry.stat()
                    files.append({"name": entry.name, "size": st.st_size,
                                  "mtime": st.st_mtime})
        except OSError:
            files = []
        files.sort(key=lambda item: item["name"].lower())
        entries = files[:MAX_UI_FILES]
        signature = tuple((e["name"], e["size"], e["mtime"]) for e in entries)
        if force or signature != self._recv_signature:
            self._recv_signature = signature
            for item in self.recv_tree.get_children():
                self.recv_tree.delete(item)
            for entry in entries:
                stamp = time.strftime("%m-%d %H:%M", time.localtime(entry["mtime"]))
                self.recv_tree.insert("", "end", values=(
                    entry["name"], core.human_bytes(entry["size"]), stamp))
            self._schedule_table_fit(120)

    def _auto_refresh_tick(self) -> None:
        if self.closing:
            return
        try:
            self._refresh_local_list()
            self.root.after(AUTO_REFRESH_MS, self._auto_refresh_tick)
        except tk.TclError:
            pass

    def _copy_receive_path(self) -> None:
        directory = self._current_recv_dir()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass
        if copy_text(self.root, directory):
            self.log(f"已复制接收目录：{directory}")

    # ------------------------------------------------------------------ #
    # 传输记录
    # ------------------------------------------------------------------ #
    def _new_row(self, direction: str, name: str, size: int) -> str:
        self.seq += 1
        row_id = f"row{self.seq}"
        remote = "服务器" if self.role.get() == "client" else "—"
        self.history.insert("", "end", iid=row_id,
                            values=(name, remote, direction, "等待", "—"),
                            tags=("muted",))
        self.rows[row_id] = {
            "direction": direction, "name": name, "size": size,
            "status": "等待", "matched": None, "local_sha256": "", "peer_sha256": "",
            "resumed_from": 0, "finished": False, "server_peer": "",
            "server_client_id": "", "remote": remote,
        }
        self._schedule_table_fit()
        return row_id

    def _handle_start(self, event: dict) -> None:
        row = self.rows.get(event.get("row"))
        if row is None:
            return
        row["status"] = "进行中"
        self._update_row(event.get("row"))
        self.task_text.set(f"{row['direction']}：{row['name']}")
        self.progress.configure(value=0)
        self.stats_text.set("正在传输…")

    def _handle_progress(self, event: dict) -> None:
        row = self.rows.get(event.get("row"))
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
        self.stats_text.set(f"{fraction * 100:4.0f}% · {core.human_bytes(done)}/"
                            f"{core.human_bytes(total)} · {core.human_bytes(rate)}/s · "
                            f"剩余 {eta_text}")
        self.task_text.set(f"{row['direction']}：{row['name']}")
        self._update_row(event.get("row"))

    def _handle_stage(self, event: dict) -> None:
        row = self.rows.get(event.get("row"))
        if row is None:
            return
        stage = (event.get("stage") or {}).get("stage")
        if stage == "restart":
            self.log(f"{row['name']} 校验不一致，正在从头重传。")
        elif stage == "start":
            resumed = int((event.get("stage") or {}).get("resumed_from", 0))
            row["resumed_from"] = resumed
            if resumed:
                self.log(f"{row['name']} 从 {core.human_bytes(resumed)} 处续传。")

    def _handle_done(self, event: dict) -> None:
        row = self.rows.get(event.get("row"))
        if row is None:
            return
        data = event.get("result") or {}
        row.update({
            "name": data.get("final_name") or row["name"],
            "size": data.get("size", row["size"]),
            "status": "完成",
            "local_sha256": data.get("local_sha256", ""),
            "peer_sha256": data.get("peer_sha256", ""),
            "matched": bool(data.get("matched")),
            "resumed_from": data.get("resumed_from", 0),
            "finished": True,
        })
        self.progress.configure(value=1000)
        self.stats_text.set(f"100% · {core.human_bytes(row['size'])}")
        self._update_row(event.get("row"))
        self._schedule_table_fit()
        self.log(f"{row['direction']}完成：{row['name']}，"
                 f"{'哈希一致' if row['matched'] else '哈希不一致'}")
        if row["direction"] == "接收":
            verdict = "SHA-256 校验通过" if row["matched"] else "哈希不一致"
            self._send_notice(row.get("server_client_id") or "",
                              f"已收到文件 {row['name']}（{core.human_bytes(row['size'])}，{verdict}）")
            self._maybe_finish_server()
        elif row["direction"] == "发送":
            self._maybe_finish_server()
        elif row["direction"] == "推送" and row.get("matched"):
            self._send_notice("", f"手机已接收文件 {row['name']}（校验一致）")

    def _handle_failed(self, event: dict) -> None:
        row = self.rows.get(event.get("row"))
        if row is None:
            return
        data = event.get("result") or {}
        row.update({
            "status": "失败",
            "local_sha256": data.get("local_sha256", row.get("local_sha256", "")),
            "peer_sha256": data.get("peer_sha256", row.get("peer_sha256", "")),
            "finished": True,
        })
        self._update_row(event.get("row"))
        self.log(f"{row['direction']}失败：{row['name']} —— {event.get('message', '')}")
        if row["direction"] == "接收":
            self._send_notice(row.get("server_client_id") or "",
                              f"文件 {row['name']} 接收失败：{event.get('message', '')}")
            self._maybe_finish_server()

    def _handle_cancelled(self, event: dict) -> None:
        row = self.rows.get(event.get("row"))
        if row is None:
            return
        row["status"] = "已取消"
        row["finished"] = True
        self._update_row(event.get("row"))
        self.log(f"{row['direction']}已取消：{row['name']}（.part 已保留）")

    def _update_row(self, row_id: str) -> None:
        row = self.rows.get(row_id)
        if row is None:
            return
        check = "—"
        tag = "muted"
        if row["status"] == "完成":
            check = "一致" if row["matched"] else "不一致"
            tag = "ok" if row["matched"] else "bad"
        elif row["status"] == "失败":
            tag = "bad"
        elif row["status"] == "进行中":
            tag = "run"
        self.history.item(row_id, values=(row["name"], row.get("remote", "—"),
                                          row["direction"], row["status"], check),
                          tags=(tag,))

    def _show_details(self) -> None:
        selection = self.history.selection()
        if not selection:
            messagebox.showinfo("提示", "请先在记录里选中一行。")
            return
        row = self.rows.get(selection[0]) or {}
        window = tk.Toplevel(self.root)
        window.title("哈希详情")
        window.transient(self.root)
        window.grab_set()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"{int(sw * 0.92)}x{int(sh * 0.45)}+{int(sw * 0.04)}+{int(sh * 0.2)}")
        window.columnconfigure(1, weight=1)
        local = row.get("local_sha256") or "（未计算）"
        peer = row.get("peer_sha256") or "（对端未提供）"
        verdict = ("一致：两边文件完全相同" if row.get("matched")
                   else ("不一致，请检查传输过程" if row.get("matched") is False
                         else "传输完成后显示比对结果"))
        for index, (label, value) in enumerate((("本机 SHA-256", local),
                                                ("对端 SHA-256", peer))):
            ttk.Label(window, text=label).grid(row=index, column=0, sticky="w",
                                               padx=10, pady=6)
            entry = tk.Entry(window, font=(self.mono_family, 8), relief="flat",
                             readonlybackground="#f3f4f6")
            entry.insert(0, value)
            entry.configure(state="readonly")
            entry.grid(row=index, column=1, sticky="ew", padx=6)
            ttk.Button(window, text="复制", width=5,
                       command=lambda v=value: self._copy_value(v)).grid(
                row=index, column=2, padx=(0, 10))
        ttk.Label(window, text=verdict, foreground=COLORS["ok"],
                  font=(self.font_family, 10, "bold")).grid(
            row=2, column=0, columnspan=3, sticky="w", padx=10, pady=(6, 4))
        ttk.Button(window, text="关闭", command=window.destroy).grid(
            row=3, column=0, columnspan=3, sticky="ew", padx=10, pady=8)
        self._details_window = window

    def _copy_value(self, value: str) -> None:
        if value and not value.startswith("（"):
            copy_text(self.root, value)
            self.log("已复制到剪贴板。")

    # ------------------------------------------------------------------ #
    # 点击记录 / 文件：完整名称详情
    # ------------------------------------------------------------------ #
    def _on_history_tap(self, event) -> None:
        try:
            region = self.history.identify_region(event.x, event.y)
            if region not in ("cell", "tree"):
                return
            item = self.history.identify_row(event.y)
        except tk.TclError:
            return
        if item:
            self._show_record_detail(item)

    def _on_recv_tap(self, event) -> None:
        try:
            region = self.recv_tree.identify_region(event.x, event.y)
            if region not in ("cell", "tree"):
                return
            item = self.recv_tree.identify_row(event.y)
        except tk.TclError:
            return
        if not item:
            return
        values = self.recv_tree.item(item, "values")
        if not values:
            return
        name = str(values[0])
        path = os.path.join(self._current_recv_dir(), name)
        self._show_file_detail(
            "文件详情", name,
            [("大小", str(values[1]) if len(values) > 1 else ""),
             ("修改时间", str(values[2]) if len(values) > 2 else ""),
             ("完整路径", path)],
            copy_path=path)

    def _show_record_detail(self, row_id: str) -> None:
        row = self.rows.get(row_id)
        if row is None:
            return
        check = "—"
        if row.get("status") == "完成":
            check = "一致" if row.get("matched") else "不一致"
        meta = [("方向", row.get("direction", "")),
                ("对端", row.get("remote", "—")),
                ("大小", core.human_bytes(int(row.get("size") or 0))),
                ("状态", row.get("status", "")),
                ("校验", check)]
        copy_path = None
        if row.get("direction") in ("接收", "推送"):
            copy_path = os.path.join(self._current_recv_dir(), row.get("name", ""))
            meta.append(("完整路径", copy_path))

        def open_hash() -> None:
            try:
                self.history.selection_set(row_id)
                self.history.focus(row_id)
            except tk.TclError:
                pass
            self._show_details()

        self._show_file_detail("记录详情", row.get("name", ""), meta,
                               copy_path=copy_path, on_hash=open_hash)

    def _show_file_detail(self, title: str, name: str, meta, copy_path=None,
                          on_hash=None) -> None:
        window = tk.Toplevel(self.root)
        window.title(title)
        window.transient(self.root)
        window.grab_set()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"{int(sw * 0.94)}x{int(sh * 0.62)}+{int(sw * 0.03)}+{int(sh * 0.12)}")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(window, text="完整文件名", font=(self.font_family, 10, "bold")).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        name_box = tk.Text(window, height=3, wrap="char", font=(self.mono_family, 8),
                           relief="solid", borderwidth=1, padx=6, pady=6)
        name_box.grid(row=1, column=0, sticky="nsew", padx=10)
        name_box.insert("1.0", name)
        name_box.configure(state="disabled")

        meta_box = ttk.Frame(window)
        meta_box.grid(row=2, column=0, sticky="ew", padx=10, pady=(8, 0))
        meta_box.columnconfigure(1, weight=1)
        for index, (label, value) in enumerate(meta):
            ttk.Label(meta_box, text=label, foreground=COLORS["muted"]).grid(
                row=index, column=0, sticky="nw", pady=2)
            ttk.Label(meta_box, text=value, wraplength=sw - 120,
                      justify="left").grid(row=index, column=1, sticky="w",
                                           padx=(8, 0), pady=2)

        actions = []
        if copy_path:
            actions.append(("复制路径", lambda p=copy_path: self._copy_value(p)))
        if on_hash is not None:
            actions.append(("哈希详情", on_hash))
        bar = ttk.Frame(window)
        bar.grid(row=3, column=0, sticky="ew", padx=10, pady=10)
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="复制文件名",
                   command=lambda n=name: self._copy_value(n)).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(bar, text="关闭", command=window.destroy).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        for index, (label, command) in enumerate(actions):
            def run(cmd=command, win=window):
                win.destroy()
                cmd()
            ttk.Button(bar, text=label, command=run).grid(
                row=1, column=index, sticky="ew", padx=4, pady=(6, 0))
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self._detail_window = window

    # ------------------------------------------------------------------ #
    # 聊天
    # ------------------------------------------------------------------ #
    def send_text(self) -> None:
        text = self.chat_input.get("1.0", "end-1c").strip()
        if not text:
            return
        targets = self._target_peer_links()
        if not targets:
            return
        sent = 0
        failures = []
        for key, link in targets:
            try:
                link.send_text(text)
                sent += 1
            except (OSError, ConnectionError, core.ProtocolError, ValueError) as exc:
                failures.append(str(exc))
        if sent == 0:
            self.log("发送失败：" + ("；".join(failures) or "没有可用连接"))
            return
        if len(targets) == 1:
            label = "我" if self.role.get() == "client" else \
                f"我 → {self._peer_label(targets[0][0], with_addr=False)}"
        else:
            label = f"我（{'群发' if sent == len(self.controls) else '发送'} {sent} 人）"
        self._append_chat(text, from_peer=False, sender=label)
        self.chat_input.delete("1.0", "end")

    def _append_chat(self, text: str, from_peer: bool, ts=None, system: bool = False,
                     sender: str = None, quiet: bool = False) -> None:
        try:
            if not self.chat.winfo_exists():
                return
        except tk.TclError:
            return
        if from_peer and self.role.get() == "server" and sender:
            alias = self._alias_for_peer(sender)
            if alias:
                sender = alias
        if system:
            text = str(text)
            for mark in ("✔", "✗", "✓", "✘"):
                text = text.replace(" " + mark, "").replace(mark, "")
            text = text.strip()
        stamp = time.strftime("%H:%M:%S", time.localtime((ts or 0) / 1000)) if ts \
            else time.strftime("%H:%M:%S")
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
        if from_peer and not system and not quiet:
            if self.notebook.index("current") != self.notebook.index(self.tab_chat):
                self.notebook.tab(self.tab_chat, text="聊天 ●")

    def _on_tab_changed(self, _event=None) -> None:
        try:
            current = self.notebook.index("current")
            if current == self.notebook.index(self.tab_chat):
                self.notebook.tab(self.tab_chat, text="聊天")
            elif current == self.notebook.index(self.tab_send):
                self.root.after(40, self._fit_history_columns)
            elif current == self.notebook.index(self.tab_recv):
                self.root.after(40, self._fit_recv_columns)
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # 地址与字体
    # ------------------------------------------------------------------ #
    def _fill_addresses(self) -> None:
        try:
            entries = core.list_ipv6_addresses()
        except Exception as exc:
            entries = []
            self.log(f"枚举 IPv6 地址失败：{exc}")
        entries = sorted(entries, key=lambda e: (
            core.SCOPE_ORDER.get(e.get("scope"), 9),
            bool(e.get("deprecated")), bool(e.get("temporary")), e.get("ip", "")))
        self.addr_list.delete(0, "end")
        self.address_entries = entries[:6]
        for entry in entries[:6]:
            self.addr_list.insert("end", entry["address"])
        if not entries:
            self.addr_list.insert("end", "未检测到 IPv6 地址（请确认 Wi-Fi 已开启 IPv6）")
            self.log("未检测到本机 IPv6 地址：请确认手机 Wi-Fi 有 IPv6；"
                     "必要时在「设置→WLAN→当前网络」查看。"
                     f"（平台 {sys.platform}，Python {sys.version.split()[0]}，"
                     f"getifaddrs={hasattr(socket, 'getifaddrs')}，"
                     f"proc={os.path.exists('/proc/net/if_inet6')}）")

    def _copy_address(self) -> None:
        selection = self.addr_list.curselection()
        if not selection:
            return
        value = str(self.addr_list.get(selection[0]))
        if ":" in value and "未检测" not in value:
            if copy_text(self.root, value):
                self.log(f"已复制地址：{value}")

    # ------------------------------------------------------------------ #
    # 连接配置串
    # ------------------------------------------------------------------ #
    def _on_config_button(self) -> None:
        if self.role.get() == "server":
            self._share_connection_config()
        else:
            self._import_connection_config()

    def _share_host(self) -> tuple:
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

    def _ask_manual_address(self, title: str, prompt: str):
        """不依赖 tkinter.simpledialog 的简易输入框（Pydroid 精简 Tk 也可能可用）。"""
        window = tk.Toplevel(self.root)
        window.title(title)
        window.transient(self.root)
        window.grab_set()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"{int(sw * 0.92)}x{int(sh * 0.42)}+{int(sw * 0.04)}+{int(sh * 0.2)}")
        window.columnconfigure(0, weight=1)
        ttk.Label(window, text=prompt, wraplength=sw - 60,
                  font=(self.font_family, 10)).grid(
            row=0, column=0, sticky="w", padx=10, pady=(12, 6))
        value = tk.StringVar()
        entry = ttk.Entry(window, textvariable=value, font=(self.mono_family, 10))
        entry.grid(row=1, column=0, sticky="ew", padx=10)
        entry.focus_set()
        result = {"value": None}

        def ok() -> None:
            result["value"] = value.get().strip() or None
            window.destroy()

        bar = ttk.Frame(window)
        bar.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="确定", command=ok).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(bar, text="取消", command=window.destroy).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        window.bind("<Return>", lambda _e: ok())
        window.protocol("WM_DELETE_WINDOW", window.destroy)
        self.root.wait_window(window)
        return result["value"]

    def _share_connection_config(self) -> None:
        host, scope = self._share_host()
        if not host:
            manual = self._ask_manual_address(
                "手动输入本机地址",
                "没有自动检测到本机 IPv6 地址。\n"
                "请在手机「设置 → WLAN → 当前网络」查看 IPv6 地址并输入：")
            if not manual:
                return
            entry = core.parse_ipv6_address(manual)
            if entry is None:
                messagebox.showerror("地址无效", "请输入有效的 IPv6 地址，例如 2409:xxxx::1")
                return
            host, scope = entry["address"], entry["scope"]
        if host.split("%", 1)[0] == "::1":
            messagebox.showwarning("只有回环地址", "::1 只能本机使用，其他设备无法连接。")
            return
        try:
            port = int(self.port_var.get())
            token = core.encode_connection_config(host, port, self.password_var.get())
        except (TypeError, ValueError) as exc:
            messagebox.showerror("生成失败", str(exc))
            return

        window = tk.Toplevel(self.root)
        window.title("连接配置")
        window.transient(self.root)
        window.grab_set()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"{int(sw * 0.92)}x{int(sh * 0.55)}+{int(sw * 0.04)}+{int(sh * 0.15)}")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        ttk.Label(window, text="把整段发给客户端，客户端点「导入配置」粘贴即可：",
                  font=(self.font_family, 10), wraplength=sw - 60).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 4))
        text = tk.Text(window, height=6, wrap="char", font=(self.mono_family, 8),
                       relief="solid", borderwidth=1, padx=6, pady=6)
        text.grid(row=1, column=0, sticky="nsew", padx=10)
        text.insert("1.0", token)
        text.configure(state="disabled")
        hint = "包含地址、端口和密码，请私发。"
        hint += "\n如果地址列表里有多个全局地址，优先选稳定的那个（临时地址会变）。"
        if scope == "link-local":
            hint += "\n当前是链路本地地址，只有同一网段可用。"
        ttk.Label(window, text=hint, foreground=COLORS["muted"],
                  font=(self.font_family, 9), wraplength=sw - 60).grid(
            row=2, column=0, sticky="w", padx=10, pady=(4, 0))
        bar = ttk.Frame(window)
        bar.grid(row=3, column=0, sticky="ew", padx=10, pady=8)
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="复制连接串",
                   command=lambda: (copy_text(self.root, token),
                                    self.log("连接串已复制到剪贴板。"))).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(bar, text="关闭", command=window.destroy).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        self._config_window = window
        self.log(f"已生成连接配置串：{host}，端口 {port}"
                 + ("（含密码）" if self.password_var.get() else "") + "。")

    def _import_connection_config(self) -> None:
        clipboard = self._read_clipboard_text()
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

    def _read_clipboard_text(self) -> str:
        getters = (
            self._read_android_clipboard,
            lambda: self.root.clipboard_get(),
            lambda: self.root.selection_get(selection="CLIPBOARD"),
            self._read_pyperclip,
        )
        for getter in getters:
            try:
                value = getter()
            except Exception:
                continue
            if isinstance(value, bytes):
                try:
                    value = value.decode("utf-8", "replace")
                except Exception:
                    continue
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _read_android_clipboard() -> str:
        for module_name in ("android", "androidhelper"):
            try:
                module = __import__(module_name, fromlist=["Android"])
                droid = module.Android()
                result = droid.getClipboard()
                value = getattr(result, "result", result)
                if isinstance(value, dict):
                    value = value.get("result")
                if isinstance(value, str):
                    return value
            except Exception:
                continue
        return ""

    @staticmethod
    def _read_pyperclip() -> str:
        try:
            import pyperclip  # type: ignore
            value = pyperclip.paste()
            if isinstance(value, str):
                return value
        except Exception:
            pass
        return ""

    def _show_import_dialog(self, initial: str = "") -> None:
        window = tk.Toplevel(self.root)
        window.title("导入连接配置")
        window.transient(self.root)
        window.grab_set()
        sw, sh = window.winfo_screenwidth(), window.winfo_screenheight()
        window.geometry(f"{int(sw * 0.94)}x{int(sh * 0.62)}+{int(sw * 0.03)}+{int(sh * 0.12)}")
        window.columnconfigure(0, weight=1)
        window.rowconfigure(3, weight=1)
        ttk.Label(window, text="粘贴服务器发来的 FT6. 开头连接串：",
                  font=(self.font_family, 10)).grid(
            row=0, column=0, sticky="w", padx=10, pady=(10, 2))
        hint_var = tk.StringVar()
        if initial:
            hint_var.set("已从剪贴板读到内容；如果不是完整配置串，请重新粘贴。")
        else:
            hint_var.set("没有读到剪贴板内容：请长按输入框粘贴，或点「从文件导入」；"
                         "也可以让服务器把连接串发到聊天里再点导入。")
        ttk.Label(window, textvariable=hint_var, foreground=COLORS["muted"],
                  font=(self.font_family, 9), wraplength=sw - 50).grid(
            row=1, column=0, sticky="w", padx=10, pady=(0, 4))

        text = tk.Text(window, height=7, wrap="char", font=(self.mono_family, 8),
                       relief="solid", borderwidth=1, padx=6, pady=6)
        text.grid(row=3, column=0, sticky="nsew", padx=10)
        text.insert("1.0", initial)

        def paste() -> None:
            clip = self._read_clipboard_text()
            if not clip:
                messagebox.showinfo("剪贴板不可用",
                                    "没有读到剪贴板文本。\n"
                                    "请长按输入框手动粘贴，或点「从文件导入」。",
                                    parent=window)
                return
            text.delete("1.0", "end")
            text.insert("1.0", clip)
            hint_var.set("已读取剪贴板，请点「导入」。")

        def from_file() -> None:
            path = FileBrowser.ask(window, "file", self._last_pick_dir, self.font_family)
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as handle:
                    content = handle.read()
            except OSError as exc:
                messagebox.showerror("读取失败", str(exc), parent=window)
                return
            text.delete("1.0", "end")
            text.insert("1.0", content.strip())
            hint_var.set(f"已读取文件：{os.path.basename(path)}，请点「导入」。")
            window.grab_set()

        def do_import() -> None:
            token = text.get("1.0", "end-1c").strip()
            try:
                data = core.decode_connection_config(token)
            except ValueError as exc:
                messagebox.showerror("导入失败", str(exc), parent=window)
                return
            self._apply_imported_config(data)
            window.destroy()

        first_bar = ttk.Frame(window)
        first_bar.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        first_bar.columnconfigure(0, weight=1)
        first_bar.columnconfigure(1, weight=1)
        ttk.Button(first_bar, text="读取剪贴板", command=paste).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(first_bar, text="从文件导入", command=from_file).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))

        bar = ttk.Frame(window)
        bar.grid(row=4, column=0, sticky="ew", padx=10, pady=(2, 10))
        bar.columnconfigure(0, weight=1)
        bar.columnconfigure(1, weight=1)
        ttk.Button(bar, text="导入", command=do_import).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(bar, text="取消", command=window.destroy).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        self._import_window = window

    def _apply_imported_config(self, data: dict) -> None:
        self.host_var.set(data["host"])
        self.port_var.set(str(data["port"]))
        self.password_var.set(data["password"])
        self.zone_var.set("")
        suffix = "（含连接密码）" if data.get("password") else ""
        self.log(f"已导入连接配置：{data['host']}，端口 {data['port']}{suffix}。点「连接」即可。")
        if self.controls:
            self.log("当前已连接，请先断开再重新连接以应用新配置。")

    def _apply_font(self) -> None:
        family = self.font_var.get()
        if not family:
            return
        self.font_family = family
        self._configure_style()
        for widget in (self.chat, self.chat_input):
            widget.configure(font=(family, 10))
        for widget in (self.pending_list, self.online_list, self.addr_list):
            widget.configure(font=(family, 10))
        self.log(f"界面字体已切换为：{family}")

    # ------------------------------------------------------------------ #
    # 通知 / 生命周期
    # ------------------------------------------------------------------ #
    def _send_notice(self, key: str, text: str) -> None:
        targets = []
        if key:
            link = self.controls.get(key)
            if link is not None:
                targets.append(link)
        else:
            targets.extend(self.controls.values())
        for link in targets:
            try:
                link.send_notice(text)
            except (OSError, ConnectionError, core.ProtocolError, ValueError):
                pass

    def on_close(self) -> None:
        if self.state == "busy":
            if not messagebox.askyesno("正在传输", "还有文件在传输，确定退出吗？"):
                return
        self.closing = True
        self.cancel_transfer()
        for link in list(self.controls.values()):
            try:
                link.close()
            except OSError:
                pass
        self.controls.clear()
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
            "font": self.font_family,
        })
        self.root.destroy()


def main() -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        root = tk.Tk()
        MobileApp(root)
        root.mainloop()
        return 0
    except Exception:
        try:
            import traceback
            text = traceback.format_exc()
            for path in (os.path.join(HERE, "mobile_crash.log"),
                         os.path.join(os.path.expanduser("~"), "mobile_crash.log")):
                try:
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(text)
                    break
                except OSError:
                    continue
        except Exception:
            pass
        raise


if __name__ == "__main__":
    sys.exit(main())
