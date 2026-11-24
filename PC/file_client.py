#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_client.py —— 基于 IPv6 的 TCP 文件传输客户端（命令行版）

配套服务器：file_server.py；图形界面：file_gui.py。
三者共用 file_core.py 中的协议实现，可直接互相传输文件。

用法
----
    python file_client.py --host 2001:db8::5 put a.bin photo.zip
    python file_client.py --host 2001:db8::5 list
    python file_client.py --host 2001:db8::5 get report.pdf --out-dir ./dl
    python file_client.py --host fe80::abcd --zone 12 put data.bin
    python file_client.py --host ::1 put ./big.bin

断点续传
--------
接收方目录里的 "<名字>.part" 会保留；重跑同样的命令即从断点继续。
上传偏移由服务器决定，下载偏移由本地分片大小决定；整文件 SHA-256 校验，
不一致会自动删除分片并从 0 重传一次。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import file_core as core

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONNECT = 2
EXIT_HASH = 3


class Stats:
    """收集失败次数，供 main 选择退出码。"""

    __slots__ = ("failed", "hash_failed")

    def __init__(self) -> None:
        self.failed = 0
        self.hash_failed = 0


# --------------------------------------------------------------------------- #
# 命令行进度显示
# --------------------------------------------------------------------------- #
class TextProgress:
    """把内核进度快照渲染成单行文字（tty 原地刷新，重定向时每秒一行）。"""

    def __init__(self, label: str, quiet: bool) -> None:
        self.label = label
        self.quiet = quiet
        self.tty = sys.stdout.isatty()
        self._last = 0.0
        self.last_snapshot = None
        self.finished_printed = False

    def feed(self, snapshot: dict) -> None:
        self.last_snapshot = snapshot
        if self.quiet:
            return
        if float(snapshot.get("fraction", 0.0)) >= 0.999:
            self._write(self._line(snapshot, final=True), final=True)
            self.finished_printed = True
            return
        now = time.monotonic()
        if now - self._last < (0.1 if self.tty else 1.0):
            return
        self._last = now
        self._write(self._line(snapshot), final=False)

    def close(self) -> None:
        if self.quiet or self.finished_printed:
            return
        snapshot = self.last_snapshot or {"fraction": 0.0, "done": 0, "total": 0}
        self._write(self._line(snapshot, final=True), final=True)

    def _line(self, snapshot: dict, final: bool = False) -> str:
        fraction = float(snapshot.get("fraction", 0.0))
        done = int(snapshot.get("done", 0))
        total = int(snapshot.get("total", 0))
        head = (f"{self.label} {fraction * 100:5.1f}% "
                f"{core.human_bytes(done)}/{core.human_bytes(total)}")
        if final:
            return head
        eta = snapshot.get("eta")
        eta_text = f" ETA {core.format_duration(eta)}" if eta else ""
        return f"{head} {core.human_bytes(float(snapshot.get('rate', 0.0)))}/s{eta_text}"

    @staticmethod
    def _write(line: str, final: bool) -> None:
        if sys.stdout.isatty():
            sys.stdout.write("\r" + line + " " * 8 + ("\n" if final else ""))
        else:
            sys.stdout.write(line + "\n")
        sys.stdout.flush()


# --------------------------------------------------------------------------- #
# 上传
# --------------------------------------------------------------------------- #
def put_one(conn: core.Conn, path: Path, args, stats: Stats) -> bool:
    name = path.name
    printer = TextProgress(name, args.quiet)

    def on_stage(stage: dict) -> None:
        if stage.get("stage") == "start" and stage.get("resumed_from") and not args.quiet:
            print(f"[续传] {name} 从 {core.human_bytes(stage['resumed_from'])} 处继续"
                  f"（共 {core.human_bytes(stage.get('size', 0))}）")
        elif stage.get("stage") == "restart":
            print(f"[重试] {name} 完整性校验失败，服务器已清理残留，重新完整上传")

    try:
        result = core.send_file(conn, str(path), resume=bool(args.resume),
                                on_progress=printer.feed, on_stage=on_stage,
                                limit_rate=args.limit_rate)
    finally:
        printer.close()

    if result.ok:
        extra = f"，续传自 {core.human_bytes(result.resumed_from)}" if result.resumed_from else ""
        moved = max(result.size - result.resumed_from, 0)
        print(f"[成功] 上传 {name} -> 服务器端 {result.final_name}"
              f"（{core.human_bytes(result.size)}{extra}"
              f"{core.timing_suffix(moved, result.seconds)}）")
        return True

    if str(result.error).lower() == "hash_mismatch":
        stats.hash_failed += 1
    print(f"[失败] 上传 {name}：{result.error}")
    return False


def cmd_put(conn: core.Conn, args, stats: Stats) -> int:
    failures = 0
    for raw in args.files:
        path = Path(raw)
        if not path.is_file():
            print(f"[跳过] 不是普通文件：{path}")
            failures += 1
            continue
        if not put_one(conn, path, args, stats):
            failures += 1
    return failures


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def get_one(conn: core.Conn, name: str, args, stats: Stats) -> bool:
    base = core.sanitize_name(name)
    printer = TextProgress(base, args.quiet)

    def on_stage(stage: dict) -> None:
        if stage.get("stage") == "start" and stage.get("resumed_from") and not args.quiet:
            print(f"[续传] {base} 从 {core.human_bytes(stage['resumed_from'])} 处继续"
                  f"（共 {core.human_bytes(stage.get('size', 0))}）")
        elif stage.get("stage") == "restart":
            print(f"[重试] {base} 校验失败，已删除本地分片，重新下载")

    try:
        result = core.recv_file(conn, base, args.out_dir, resume=bool(args.resume),
                                on_progress=printer.feed, on_stage=on_stage,
                                limit_rate=args.limit_rate)
    finally:
        printer.close()

    if result.ok:
        extra = f"，续传自 {core.human_bytes(result.resumed_from)}" if result.resumed_from else ""
        moved = max(result.size - result.resumed_from, 0)
        shown = os.path.relpath(result.path) if result.path.startswith(os.getcwd()) else result.path
        print(f"[成功] 下载 {base} -> {shown}"
              f"（{core.human_bytes(result.size)}{extra}"
              f"{core.timing_suffix(moved, result.seconds)}）")
        return True

    if "校验" in str(result.error):
        stats.hash_failed += 1
    print(f"[失败] 下载 {base}：{result.error}")
    return False


def cmd_get(conn: core.Conn, args, stats: Stats) -> int:
    failures = 0
    for name in args.names:
        if not get_one(conn, name, args, stats):
            failures += 1
    return failures


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def cmd_list(conn: core.Conn, args) -> int:
    try:
        files, root = core.list_remote(conn)
    except core.ProtocolError as exc:
        print(f"[失败] 获取列表：{exc}")
        return 1

    if not files:
        print(f"服务器目录为空：{root}")
        return 0

    name_width = max(disp_width("名称"), *(disp_width(str(f.get("name", ""))) for f in files))
    print(f"{pad('名称', name_width)}  {'大小':>10}  修改时间")
    print("-" * (name_width + 34))
    for item in files:
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(item.get("mtime", 0))))
        print(f"{pad(str(item.get('name', '')), name_width)}  "
              f"{core.human_bytes(item.get('size', 0)):>10}  {stamp}")
    print(f"\n共 {len(files)} 个文件")
    return 0


def disp_width(text: str) -> int:
    import unicodedata
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))


# --------------------------------------------------------------------------- #
# 连接
# --------------------------------------------------------------------------- #
def open_connection(args) -> core.Conn:
    conn, peer, reply = core.resolve_and_connect(
        args.host, args.port, args.zone or "",
        connect_timeout=args.connect_timeout, timeout=args.timeout,
        role="transfer", app="file_client/1.0",
        password=getattr(args, "password", "") or "")
    if not args.quiet:
        local = conn.sock.getsockname()
        print(f"已连接 {peer}（本地 [{local[0]}]:{local[1]}，服务器目录 {reply.get('root')}）")
    return conn


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def parse_rate(text: str) -> int:
    value = str(text).strip().lower()
    if value in ("", "0", "none", "off"):
        return 0
    scale = 1
    if value[-1] in ("k", "m", "g"):
        scale = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[value[-1]]
        value = value[:-1]
    try:
        rate = float(value) * scale
    except ValueError:
        raise argparse.ArgumentTypeError("速率写法不对，例如 500K、2M、1048576") from None
    if rate < 0:
        raise argparse.ArgumentTypeError("速率不能为负")
    return int(rate)


def _common_options(parser: argparse.ArgumentParser, suppress_defaults: bool) -> None:
    def default(value):
        return argparse.SUPPRESS if suppress_defaults else value

    parser.add_argument("--host", default=default(None),
                        help="服务器的 IPv6 地址，如 2001:db8::5；链路本地地址需配 --zone")
    parser.add_argument("--port", type=int, default=default(core.DEFAULT_PORT),
                        help=f"服务器端口，默认 {core.DEFAULT_PORT}")
    parser.add_argument("--zone", default=default(None),
                        help="链路本地地址的接口索引（netsh interface ipv6 show address 可查）")
    parser.add_argument("--password", default=default(""),
                        help="服务器连接密码（服务器启用密码时必填）")
    parser.add_argument("--out-dir", default=default("downloads"),
                        help="下载保存目录，默认 ./downloads")
    parser.add_argument("--timeout", type=float, default=default(120.0),
                        help="单次读写的空闲超时秒数，默认 120")
    parser.add_argument("--connect-timeout", type=float, default=default(10.0),
                        help="建立连接的等待秒数，默认 10")
    parser.add_argument("--limit-rate", type=parse_rate, default=default(0),
                        help="限速（如 500K、2M），默认不限")
    parser.add_argument("--no-resume", dest="resume", action="store_false", default=default(True),
                        help="忽略已有 .part 分片，强制从头传")
    parser.add_argument("--quiet", action="store_true", default=default(False),
                        help="不显示进度条和连接横幅（仍打印每个文件的结果）")


def _parent() -> argparse.ArgumentParser:
    helper = argparse.ArgumentParser(add_help=False)
    _common_options(helper, suppress_defaults=True)
    return helper


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IPv6 TCP 文件传输客户端（配套 file_server.py / file_gui.py）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python file_client.py --host 2001:db8::5 put a.bin photo.zip\n"
            "  python file_client.py --host 2001:db8::5 list\n"
            "  python file_client.py --host 2001:db8::5 get report.pdf --out-dir ./dl\n"
            "  python file_client.py --host fe80::abcd --zone 12 put data.bin\n"
            "  python file_client.py --host ::1 put ./big.bin\n"
        ),
    )
    _common_options(parser, suppress_defaults=False)

    sub = parser.add_subparsers(dest="command", metavar="{put,get,list}")
    sub.required = True

    put = sub.add_parser("put", parents=[_parent()], help="上传一个或多个文件到服务器")
    put.add_argument("files", nargs="+", metavar="FILE", help="要上传的本地文件")

    get = sub.add_parser("get", parents=[_parent()], help="从服务器下载一个或多个文件")
    get.add_argument("names", nargs="+", metavar="NAME", help="服务器上的文件名")

    sub.add_parser("list", parents=[_parent()], help="列出服务器上的文件")
    return parser


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
    except (OSError, ValueError):
        pass

    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("请用 --host 指定服务器的 IPv6 地址，例如 --host 2001:db8::5 或 --host fe80::abcd --zone 12")

    try:
        conn = open_connection(args)
    except core.ConnectError as exc:
        print(f"[连接失败] {exc}", file=sys.stderr)
        return EXIT_CONNECT

    failures = 0
    stats = Stats()
    try:
        try:
            if args.command == "put":
                failures = cmd_put(conn, args, stats)
            elif args.command == "get":
                failures = cmd_get(conn, args, stats)
            else:
                failures = cmd_list(conn, args)
        finally:
            try:
                conn.send_msg({"cmd": "bye"})
                conn.recv_msg()
            except (OSError, core.ProtocolError):
                pass
    except (ConnectionError, OSError, core.ProtocolError) as exc:
        print(f"[中断] {exc}", file=sys.stderr)
        return EXIT_FAIL
    finally:
        try:
            conn.sock.close()
        except OSError:
            pass

    if failures:
        print(f"完成，但有 {failures} 个文件失败。", file=sys.stderr)
        return EXIT_HASH if stats.hash_failed else EXIT_FAIL
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
