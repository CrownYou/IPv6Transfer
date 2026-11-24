#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_server.py —— 基于 IPv6 的 TCP 文件传输服务器（命令行版）

配套客户端：file_client.py；图形界面：file_gui.py。
三者共用 file_core.py 中的协议实现，可直接互相传输文件。

快速开始
--------
    python file_server.py                          # 监听 [::]:9000，落盘 ./received
    python file_server.py --port 9100 --dir D:\\share
    python file_server.py --ipv6-only              # 严格只接受 IPv6 对端

说明
----
* 接收中的文件保存为 "<名字>.part"，校验通过后才改成正式文件名；中断后重跑客户端即可续传。
* 同名文件不会覆盖：正式落盘时自动加 "YYYYmmdd-HHMMSS" 时间戳后缀。
* 文件名只取最后一段并清洗，无法写到接收目录之外。
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time

import file_core as core


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def log(opts, message: str) -> None:
    if getattr(opts, "quiet", False):
        return
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def warn(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# 会话事件 -> 命令行输出
# --------------------------------------------------------------------------- #
def make_emit(opts, peer: str):
    def emit(event: dict) -> None:
        etype = event.get("type")
        if etype == "start":
            size = event.get("size", 0)
            start = event.get("resumed_from", 0)
            word = "接收" if event.get("direction") == "receive" else "发送"
            log(opts, f"{peer} 开始{word} {event.get('name')}（{core.human_bytes(size)}"
                      + (f"，从 {core.human_bytes(start)} 处续传" if start else "") + "）")
        elif etype == "done":
            result = event.get("result") or {}
            moved = max(result.get("size", 0) - result.get("resumed_from", 0), 0)
            suffix = core.timing_suffix(moved, result.get("seconds", 0))
            if result.get("direction") == "receive":
                log(opts, f"{peer} 接收完成 -> {result.get('final_name')}"
                          f"（{core.human_bytes(result.get('size', 0))}{suffix}）")
            else:
                log(opts, f"{peer} 发送完成 {result.get('name')}"
                          f"（{core.human_bytes(moved)}{suffix}）")
        elif etype == "error":
            warn(f"{peer} {event.get('name', '')}：{event.get('error')}")
    return emit


# --------------------------------------------------------------------------- #
# 连接处理
# --------------------------------------------------------------------------- #
def _worker(client: socket.socket, addr, opts, locks: core.NameLocks, slots) -> None:
    peer = core.format_peer(addr)
    conn = core.Conn(client)
    try:
        log(opts, f"连接建立 {peer}")
        hello = core.read_hello(conn)
        if hello.get("role") == "control":
            conn.send_msg({"ok": False, "error": "该服务器未启用文本消息（请使用 file_gui.py）"})
            log(opts, f"{peer} 请求文字通道，已拒绝（命令行服务器不支持文字消息）")
            return
        core.authenticate(conn, opts.dir, opts.password)
        core.handle_transfer_session(conn, opts.dir, timeout=opts.timeout,
                                     locks=locks, emit=make_emit(opts, peer))
    except core.HelloRejected as exc:
        warn(f"{peer} 握手失败：{exc}")
    except socket.timeout:
        warn(f"{peer} 空闲超时（{opts.timeout}s），断开")
    except core.ProtocolError as exc:
        warn(f"{peer} 协议错误：{exc}")
        try:
            conn.send_msg({"ok": False, "error": str(exc)})
        except OSError:
            pass
    except (ConnectionError, OSError) as exc:
        warn(f"{peer} 连接中断：{exc}（未完成的 .part 已保留，可续传）")
    finally:
        try:
            client.close()
        except OSError:
            pass
        log(opts, f"连接关闭 {peer}")
        slots.release()


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="IPv6 TCP 文件传输服务器（配套 file_client.py / file_gui.py）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python file_server.py\n"
            "  python file_server.py --port 9100 --dir D:\\share\n"
            "  python file_server.py --ipv6-only --max-clients 4\n"
        ),
    )
    parser.add_argument("--host", default="::", help="监听地址，默认 ::（所有 IPv6 地址）")
    parser.add_argument("--port", type=int, default=core.DEFAULT_PORT,
                        help=f"监听端口，默认 {core.DEFAULT_PORT}")
    parser.add_argument("--dir", default="received", help="接收目录，默认 ./received")
    parser.add_argument("--ipv6-only", action="store_true",
                        help="设置 IPV6_V6ONLY=1，只接受纯 IPv6 对端")
    parser.add_argument("--max-clients", type=int, default=8, help="最大并发连接数，默认 8")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="单次读写的空闲超时秒数，默认 120")
    parser.add_argument("--password", default="",
                        help="连接密码（留空表示不验证；客户端需填相同密码）")
    parser.add_argument("--quiet", action="store_true", help="不打印连接日志")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not (1 <= args.port <= 65535):
        parser.error("--port 必须在 1..65535 之间")
    if args.max_clients < 1:
        parser.error("--max-clients 至少为 1")
    if len(args.password) > core.MAX_PASSWORD_CHARS:
        parser.error(f"--password 最多 {core.MAX_PASSWORD_CHARS} 个字符")

    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(errors="replace")
            sys.stderr.reconfigure(errors="replace")
    except (OSError, ValueError):
        pass

    os.makedirs(args.dir, exist_ok=True)
    try:
        server = core.make_listener(args.host, args.port, args.ipv6_only)
    except OSError as exc:
        warn(f"无法监听 [{args.host}]:{args.port}：{exc}")
        warn("请检查端口是否被占用、是否具有管理员权限，以及防火墙设置。")
        return 2

    bound = server.getsockname()
    print("=" * 62)
    print(f" IPv6 文件服务器已启动：监听 [{bound[0]}]:{bound[1]}")
    print(f" 接收目录：{os.path.abspath(args.dir)}")
    print(f" 并发上限：{args.max_clients}    空闲超时：{args.timeout:g}s"
          f"    仅 IPv6：{'是' if args.ipv6_only else '否（兼容 IPv4-mapped）'}")
    print(f" 连接密码：{'已启用' if args.password else '未启用'}")
    print("-" * 62)
    print(" 客户端可用地址（把 <地址> 填到客户端的 --host）：")
    try:
        entries = core.list_ipv6_addresses()
    except Exception as exc:                       # 枚举失败不影响启动
        entries = []
        warn(f"枚举本机 IPv6 地址失败：{exc}")
    if entries:
        for index, entry in enumerate(entries):
            label = "推荐远程" if (index == 0 and entry["scope"] == "global") else "本机地址"
            print(f"   {label} : {core.format_address_entry(entry)}")
        if not any(e["scope"] in ("global", "ula") and not e["deprecated"] for e in entries):
            print("   注意     : 仅检测到链路本地/已弃用地址，跨网段可能无法连接。")
    else:
        print("   未检测到可远程使用的 IPv6 地址（请检查网卡是否启用 IPv6）")
    print("   同机测试 : ::1")
    print(" 若跨设备连不上：确认对方 ping6 可达、防火墙已放行该端口。")
    print("=" * 62, flush=True)

    locks = core.NameLocks()
    slots = threading.BoundedSemaphore(args.max_clients)
    try:
        while True:
            try:
                client, addr = server.accept()
            except OSError as exc:
                warn(f"accept 失败：{exc}")
                continue
            if not slots.acquire(blocking=False):
                try:
                    core.Conn(client).send_msg({"ok": False, "error": "服务器连接数已满，请稍后再试"})
                except OSError:
                    pass
                finally:
                    client.close()
                warn(f"拒绝 {core.format_peer(addr)}：连接数已满")
                continue
            threading.Thread(
                target=_worker,
                args=(client, addr, args, locks, slots),
                daemon=True,
            ).start()
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，正在退出。", flush=True)
    finally:
        try:
            server.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
