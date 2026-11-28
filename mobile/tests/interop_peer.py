#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""interop_peer.py —— 用 python 版 file_core 充当对端，验证 C# 移植版的协议一致性。

用法（在 C#/tests 目录下）：
    python interop_peer.py cfg-encode <host> <port> <password>
    python interop_peer.py cfg-decode <token>
    python interop_peer.py server <dir> <host> <port> [password] [count]
    python interop_peer.py send <host> <port> <file> [password]
    python interop_peer.py recv <host> <port> <name> <dir> [password]
    python interop_peer.py control-serve <host> <port> [password]
    python interop_peer.py control-send <host> <port> <text> [password]
    python interop_peer.py addresses
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PY_DIR = os.path.normpath(os.path.join(HERE, "..", "..", "PC"))
sys.path.insert(0, PY_DIR)

import file_core as core  # noqa: E402


def out(*parts) -> None:
    print(*parts, flush=True)


def serve(dir_path: str, host: str, port: int, password: str, count: int) -> int:
    os.makedirs(dir_path, exist_ok=True)
    srv = core.make_listener(host, port, False)
    out(f"READY port={port}")

    def emit(event: dict) -> None:
        etype = event.get("type")
        if etype == "start":
            out(f"EVENT start {event.get('direction')} {event.get('name')} "
                f"size={event.get('size')} resume={event.get('resumed_from')}")
        elif etype == "done":
            data = event.get("result") or {}
            out(f"EVENT done {data.get('direction')} {data.get('name')} "
                f"size={data.get('size')} matched={data.get('matched')} "
                f"sha={data.get('local_sha256')}")
        elif etype == "error":
            out(f"EVENT error {event.get('direction')} {event.get('name')} {event.get('error')}")

    locks = core.NameLocks()
    handled = 0
    while handled < count:
        try:
            client, addr = srv.accept()
        except OSError:
            break
        peer = core.format_peer(addr)
        conn = core.Conn(client)
        keep = False
        try:
            hello = core.read_hello(conn)
            core.authenticate(conn, dir_path, password)
            if hello.get("role") == "control":
                out(f"CONTROL hello peer={peer}")
                link = core.ControlLink.from_accepted(
                    conn, peer,
                    on_text=lambda text, ts: out(f"SAY {text}"),
                    on_push=lambda name, size, token: out(f"PUSH {name} size={size} token={token}"),
                    on_notice=lambda text, ts: out(f"NOTICE {text}"),
                    on_state=lambda up: out(f"STATE up={up}"),
                )
                keep = True
                time.sleep(3.0)
                link.close()
                handled += 1
                continue
            core.handle_transfer_session(conn, dir_path, timeout=30.0, locks=locks,
                                         emit=emit)
            handled += 1
        except core.HelloRejected as exc:
            out(f"REJECTED {exc}")
        except core.ProtocolError as exc:
            out(f"PROTOCOL_ERROR {exc}")
        except (ConnectionError, OSError) as exc:
            out(f"CONN_ERROR {exc}")
        finally:
            if not keep:
                try:
                    conn.sock.close()
                except OSError:
                    pass
    try:
        srv.close()
    except OSError:
        pass
    out("DONE")
    return 0


def send(host: str, port: int, path: str, password: str) -> int:
    try:
        conn, peer, _reply = core.resolve_and_connect(
            host, port, "", connect_timeout=5.0, timeout=30.0,
            role="transfer", app="py_interop/1.0",
            extra_hello={"client_id": "pypeer"}, password=password)
    except core.ConnectError as exc:
        out(f"CONNECT_ERROR {exc}")
        return 1
    out(f"CONNECTED {peer}")
    try:
        result = core.send_file(conn, path, on_stage=lambda s: out(f"STAGE {s.get('stage')}"),
                                on_progress=lambda snap: None)
        out(f"RESULT ok={result.ok} direction={result.direction} name={result.name} "
            f"size={result.size} matched={result.matched} sha={result.local_sha256} "
            f"error={result.error}")
        conn.send_msg({"cmd": "bye"})
        conn.recv_msg()
        return 0 if result.ok else 1
    finally:
        try:
            conn.sock.close()
        except OSError:
            pass


def recv(host: str, port: int, name: str, dir_path: str, password: str) -> int:
    os.makedirs(dir_path, exist_ok=True)
    try:
        conn, peer, _reply = core.resolve_and_connect(
            host, port, "", connect_timeout=5.0, timeout=30.0,
            role="transfer", app="py_interop/1.0",
            extra_hello={"client_id": "pypeer"}, password=password)
    except core.ConnectError as exc:
        out(f"CONNECT_ERROR {exc}")
        return 1
    out(f"CONNECTED {peer}")
    try:
        result = core.recv_file(conn, name, dir_path)
        out(f"RESULT ok={result.ok} direction={result.direction} name={result.name} "
            f"final={result.final_name} size={result.size} matched={result.matched} "
            f"sha={result.local_sha256} error={result.error}")
        conn.send_msg({"cmd": "bye"})
        conn.recv_msg()
        return 0 if result.ok else 1
    finally:
        try:
            conn.sock.close()
        except OSError:
            pass


def control_serve(host: str, port: int, password: str) -> int:
    srv = core.make_listener(host, port, False)
    out(f"READY port={port}")
    client, addr = srv.accept()
    peer = core.format_peer(addr)
    conn = core.Conn(client)
    try:
        hello = core.read_hello(conn)
        core.authenticate(conn, HERE, password)
        out(f"HELLO role={hello.get('role')} client_id={hello.get('client_id')}")
        done = threading.Event()

        def on_text(text: str, ts: int) -> None:
            out(f"SAY {text}")
            try:
                link.send_notice("收到：" + text)
                link.send_push("pushed.bin", 1234, "tok-1")
            except Exception as exc:  # noqa: BLE001
                out(f"SEND_FAIL {exc}")
            done.set()

        def on_state(up: bool) -> None:
            out(f"STATE up={up}")
            if not up:
                done.set()

        link = core.ControlLink.from_accepted(conn, peer, on_text=on_text, on_state=on_state)
        done.wait(timeout=10.0)
        # 让 notice / push 真正发完再关，避免 close 时的 RST 丢掉最后一帧
        time.sleep(0.6)
        link.close()
    finally:
        try:
            conn.sock.close()
        except OSError:
            pass
        try:
            srv.close()
        except OSError:
            pass
    out("DONE")
    return 0


def control_send(host: str, port: int, text: str, password: str) -> int:
    got: list = []
    done = threading.Event()

    def on_notice(value: str, ts: int) -> None:
        got.append(("notice", value))
        out(f"NOTICE {value}")

    def on_push(name: str, size: int, token) -> None:
        got.append(("push", name, size, token))
        out(f"PUSH {name} size={size} token={token}")

    def on_text(value: str, ts: int) -> None:
        out(f"SAY {value}")

    try:
        link = core.ControlLink.connect(
            host, port, "", timeout=30.0, connect_timeout=5.0, password=password,
            on_text=on_text, on_notice=on_notice, on_push=on_push,
            on_state=lambda up: done.set() if not up else None,
            client_id="pyclient")
    except core.ConnectError as exc:
        out(f"CONNECT_ERROR {exc}")
        return 1
    out(f"CONNECTED {link.peer}")
    try:
        link.send_text(text)
        deadline = time.time() + 8.0
        while time.time() < deadline and len(got) < 2:
            time.sleep(0.1)
        out(f"RESULT notices={sum(1 for g in got if g[0] == 'notice')} "
            f"pushes={sum(1 for g in got if g[0] == 'push')}")
        return 0
    finally:
        link.close()


def push_serve(dir_path: str, host: str, port: int, file_path: str, password: str) -> int:
    """作为服务器：先通过文字通道推送文件，再接受传输连接、用 token 提供文件。"""
    token = "tok-py"
    os.makedirs(dir_path, exist_ok=True)
    srv = core.make_listener(host, port, False)
    name = os.path.basename(file_path)
    size = os.path.getsize(file_path)
    out(f"READY port={port} name={name} size={size}")

    control, addr = srv.accept()
    conn = core.Conn(control)
    hello = core.read_hello(conn)
    core.authenticate(conn, dir_path, password)
    out(f"CONTROL peer={core.format_peer(addr)} role={hello.get('role')}")
    link = core.ControlLink.from_accepted(conn, core.format_peer(addr))
    link.send_push(name, size, token)
    out("PUSH_SENT")

    transfer, addr2 = srv.accept()
    conn2 = core.Conn(transfer)
    try:
        core.read_hello(conn2)
        core.authenticate(conn2, dir_path, password)
        core.handle_transfer_session(
            conn2, dir_path, timeout=30.0,
            emit=lambda event: out(f"EVENT {event.get('type')} "
                                   f"{(event.get('result') or {}).get('name') or event.get('name')}"),
            path_resolver=lambda t: file_path if t == token and os.path.isfile(file_path) else None)
        out(f"SERVED {core.format_peer(addr2)}")
    finally:
        try:
            conn2.sock.close()
        except OSError:
            pass
    try:
        link.send_notice("文件已发送")
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.3)
    link.close()
    try:
        srv.close()
    except OSError:
        pass
    out("DONE")
    return 0


def push_recv(host: str, port: int, dir_path: str, password: str) -> int:
    """作为客户端：收到 push 后另开一条连接，用 token 把文件拉下来。"""
    os.makedirs(dir_path, exist_ok=True)
    done = threading.Event()
    result: dict = {}

    def on_push(name: str, size: int, token) -> None:
        out(f"PUSH {name} size={size} token={token}")
        try:
            conn, _peer, _reply = core.resolve_and_connect(
                host, port, "", connect_timeout=5.0, timeout=30.0,
                role="transfer", app="py_interop/1.0",
                extra_hello={"client_id": "pypeer"}, password=password)
            try:
                item = core.recv_file(conn, name, dir_path, token=token)
                out(f"RESULT ok={item.ok} name={item.final_name} size={item.size} "
                    f"matched={item.matched} error={item.error}")
                result["ok"] = item.ok
                conn.send_msg({"cmd": "bye"})
                conn.recv_msg()
                out("BYE_SENT")
            finally:
                conn.sock.close()
        except Exception as exc:  # noqa: BLE001
            out(f"FAIL {exc}")
            result["ok"] = False
        finally:
            done.set()

    try:
        link = core.ControlLink.connect(
            host, port, "", timeout=30.0, connect_timeout=5.0, password=password,
            on_push=on_push,
            on_notice=lambda text, ts: out(f"NOTICE {text}"),
            on_state=lambda up: None if up else done.set(),
            client_id="pyclient")
    except core.ConnectError as exc:
        out(f"CONNECT_ERROR {exc}")
        return 1
    out(f"CONNECTED {link.peer}")
    try:
        done.wait(20.0)
        return 0 if result.get("ok") else 1
    finally:
        link.close()

def main(argv: list) -> int:
    # 用 "-" 表示空密码（命令行里不好传空字符串）
    argv = [item if item != "-" else "" for item in argv]
    if len(argv) < 2:
        out(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "cfg-encode":
        out(core.encode_connection_config(argv[2], int(argv[3]),
                                          argv[4] if len(argv) > 4 else ""))
        return 0
    if cmd == "cfg-decode":
        data = core.decode_connection_config(argv[2])
        out(f"host={data['host']} port={data['port']} password={data['password']}")
        return 0
    if cmd == "server":
        return serve(argv[2], argv[3], int(argv[4]),
                     argv[5] if len(argv) > 5 and argv[5] != "-" else "",
                     int(argv[6]) if len(argv) > 6 else 1)
    if cmd == "send":
        return send(argv[2], int(argv[3]), argv[4],
                    argv[5] if len(argv) > 5 else "")
    if cmd == "recv":
        return recv(argv[2], int(argv[3]), argv[4], argv[5],
                    argv[6] if len(argv) > 6 else "")
    if cmd == "push-serve":
        return push_serve(argv[2], argv[3], int(argv[4]), argv[5],
                          argv[6] if len(argv) > 6 else "")
    if cmd == "push-recv":
        return push_recv(argv[2], int(argv[3]), argv[4],
                         argv[5] if len(argv) > 5 else "")
    if cmd == "control-serve":
        return control_serve(argv[2], int(argv[3]),
                             argv[4] if len(argv) > 4 else "")
    if cmd == "control-send":
        return control_send(argv[2], int(argv[3]), argv[4],
                            argv[5] if len(argv) > 5 else "")
    if cmd == "addresses":
        for entry in core.list_ipv6_addresses():
            out(core.format_address_entry(entry))
        return 0
    out(f"未知命令: {cmd}")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
