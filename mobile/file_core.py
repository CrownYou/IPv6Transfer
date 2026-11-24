#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
file_core.py —— IPv6 文件传输内核（命令行与图形界面共用，仅用标准库）

包含四部分：
1. 帧收发：UTF-8 JSON 控制行 + 按声明长度直接跟随的裸字节；
2. 安全文件名、哈希、人类可读格式化、进度/速率计算；
3. 客户端传输：send_file / recv_file / list_remote（带进度、取消、限速）；
4. 服务端会话：handle_transfer_session（事件式 emit 回调）；
5. 文字通道：ControlLink（独立于文件连接长连接，支持 say / ping 保活）。

协议与 file_server.py / file_client.py 完全一致，GUI 与 CLI 可互传文件。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

PROTO_VERSION = 1
DEFAULT_PORT = 9000
AUTH_METHOD = "hmac-sha256"
CONFIG_PREFIX = "FT6"
MAX_PASSWORD_CHARS = 256
CHUNK_SIZE = 1 << 20            # 1 MiB，流式收发，内存占用恒定
MAX_LINE = 64 * 1024            # 单条控制消息上限
MAX_FILE_SIZE = 1 << 40         # 1 TiB
MAX_NAME_BYTES = 200
MAX_LIST_ENTRIES = 10000
MAX_TEXT_CHARS = 4000
PING_INTERVAL = 30.0
EMIT_INTERVAL = 0.1             # 进度回调最短间隔（秒）

INVALID_NAME_CHARS = set('<>:"/\\|?*')
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


# --------------------------------------------------------------------------- #
# 异常
# --------------------------------------------------------------------------- #
class ProtocolError(Exception):
    """对端数据不符合协议约定。"""


class HelloRejected(ProtocolError):
    """握手失败，且已经回送过错误应答。"""


class ConnectError(Exception):
    """无法建立到对端的连接。"""


class TransferCancelled(Exception):
    """用户取消了当前传输。"""


# --------------------------------------------------------------------------- #
# 带缓冲的帧收发
# --------------------------------------------------------------------------- #
class Conn:
    """JSON 行 + 裸字节混合收发器。"""

    __slots__ = ("sock", "_buf")

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = bytearray()

    # -- 控制消息 -----------------------------------------------------------
    def readline(self) -> bytes:
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                return line
            if len(self._buf) > MAX_LINE:
                raise ProtocolError("控制消息过长")
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("对端已关闭连接")
            self._buf.extend(chunk)

    def recv_msg(self) -> dict:
        line = self.readline().strip()
        if not line:
            raise ProtocolError("收到空控制消息")
        try:
            msg = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError(f"控制消息不是合法 JSON: {exc}") from exc
        if not isinstance(msg, dict):
            raise ProtocolError("控制消息必须是 JSON 对象")
        return msg

    def send_msg(self, msg: dict) -> None:
        payload = json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > MAX_LINE:
            raise ProtocolError("要发送的控制消息过长")
        self.sock.sendall(payload)

    # -- 文件内容 -----------------------------------------------------------
    def recv_to_fp(self, fp, count: int, on_chunk=None) -> None:
        """读取 count 字节写入 fp；每读到一块就回调 on_chunk(memoryview)。"""
        remaining = int(count)
        if self._buf and remaining > 0:
            take = min(remaining, len(self._buf))
            head = bytes(self._buf[:take])
            del self._buf[:take]
            fp.write(head)
            if on_chunk is not None:
                on_chunk(memoryview(head))
            remaining -= take
        if remaining <= 0:
            return
        view = memoryview(bytearray(min(CHUNK_SIZE, remaining)))
        while remaining > 0:
            want = min(len(view), remaining)
            got = self.sock.recv_into(view[:want])
            if got == 0:
                raise ConnectionError("对端在文件数据中途关闭了连接")
            fp.write(view[:got])
            if on_chunk is not None:
                on_chunk(view[:got])
            remaining -= got

    def send_from_fp(self, fp, count: int, on_chunk=None) -> None:
        """把 fp 中 count 字节写入连接；每发一块就回调 on_chunk(memoryview)。"""
        remaining = int(count)
        if remaining <= 0:
            return
        view = memoryview(bytearray(min(CHUNK_SIZE, remaining)))
        while remaining > 0:
            want = min(len(view), remaining)
            got = fp.readinto(view[:want])
            if not got:
                raise ProtocolError("本地文件比声明的长度短")
            self.sock.sendall(view[:got])
            if on_chunk is not None:
                on_chunk(view[:got])
            remaining -= got


# --------------------------------------------------------------------------- #
# 握手
# --------------------------------------------------------------------------- #
def parse_int(value, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"字段 {field_name} 必须是整数")
    return int(value)


def read_hello(conn: Conn) -> dict:
    """服务端读取并校验 hello；版本不符时先回错误再抛异常。"""
    msg = conn.recv_msg()
    if msg.get("cmd") != "hello":
        raise ProtocolError("期望 hello 握手")
    try:
        version = parse_int(msg.get("version", 0), "version")
    except ProtocolError:
        raise
    if version != PROTO_VERSION:
        conn.send_msg({"ok": False, "error": f"协议版本不匹配（服务器 {PROTO_VERSION}，客户端 {version}）"})
        raise HelloRejected(f"协议版本不匹配（客户端 {version}）")
    return msg


def ack_hello(conn: Conn, root_dir: str) -> None:
    conn.send_msg({"ok": True, "version": PROTO_VERSION, "root": os.path.abspath(root_dir)})


def _auth_digest(password: str, nonce: str) -> str:
    return hmac.new(str(password).encode("utf-8"), str(nonce).encode("ascii"),
                    hashlib.sha256).hexdigest()


def authenticate(conn: Conn, root_dir: str, password: str = "") -> None:
    """在 read_hello 之后执行密码认证并回送 ack。

    密码通过 HMAC-SHA256(challenge) 校验，不在网络上传输明文密码。
    服务器未设置密码时直接 ack，保持与旧客户端兼容。
    """
    password = str(password or "")
    if len(password) > MAX_PASSWORD_CHARS:
        raise ProtocolError(f"连接密码过长（最多 {MAX_PASSWORD_CHARS} 个字符）")
    if password:
        nonce = secrets.token_hex(16)
        conn.send_msg({
            "ok": False,
            "auth_required": True,
            "method": AUTH_METHOD,
            "nonce": nonce,
            "error": "服务器已启用连接密码",
        })
        reply = conn.recv_msg()
        if (not isinstance(reply, dict) or reply.get("cmd") != "auth"
                or not isinstance(reply.get("response"), str)):
            conn.send_msg({"ok": False, "error": "客户端未提供连接密码"})
            raise HelloRejected("客户端未完成密码认证")
        if not hmac.compare_digest(_auth_digest(password, nonce), reply["response"]):
            conn.send_msg({"ok": False, "error": "连接密码错误"})
            raise HelloRejected("连接密码错误")
    ack_hello(conn, root_dir)


def accept_hello(conn: Conn, root_dir: str, password: str = "") -> dict:
    """读取 hello、完成密码认证并 ack；返回 hello 内容。"""
    hello = read_hello(conn)
    authenticate(conn, root_dir, password)
    return hello


def encode_connection_config(host: str, port: int, password: str = "") -> str:
    """把地址、端口、密码序列化成一段可复制粘贴的连接配置串。"""
    host = str(host or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise ValueError("IPv6 地址不能为空")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("端口必须是整数") from None
    if not 1 <= port <= 65535:
        raise ValueError("端口必须在 1..65535 之间")
    password = str(password or "")
    if len(password) > MAX_PASSWORD_CHARS:
        raise ValueError(f"密码最多 {MAX_PASSWORD_CHARS} 个字符")
    payload = {"v": 1, "host": host, "port": port}
    if password:
        payload["password"] = password
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    checksum = hashlib.sha256(body.encode("ascii")).hexdigest()[:8]
    return f"{CONFIG_PREFIX}.{body}.{checksum}"


def decode_connection_config(token: str) -> dict:
    """解析连接配置串，返回 {"host", "port", "password", "version"}。"""
    text = "".join(str(token or "").split())
    matches = re.findall(r"FT6\.[A-Za-z0-9_-]+\.[0-9a-fA-F]{8}", text)
    if matches:
        text = matches[-1]
    prefix = f"{CONFIG_PREFIX}."
    if not text.startswith(prefix):
        raise ValueError("不是有效的连接配置串（应以 FT6. 开头）")
    parts = text.split(".")
    if len(parts) != 3 or not parts[1] or len(parts[2]) != 8:
        raise ValueError("连接配置串格式不完整，请确认整段复制")
    body, checksum = parts[1], parts[2].lower()
    expected = hashlib.sha256(body.encode("ascii")).hexdigest()[:8]
    if not hmac.compare_digest(expected, checksum):
        raise ValueError("连接配置串校验失败，可能复制不完整或已被修改")
    try:
        padded = body + "=" * (-len(body) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("连接配置串内容损坏") from exc
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ValueError("不支持的连接配置串版本")
    host = str(data.get("host") or "").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not host:
        raise ValueError("连接配置里缺少 IPv6 地址")
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError("连接配置里的端口无效")
    password = data.get("password") or ""
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_CHARS:
        raise ValueError("连接配置里的密码无效")
    return {"host": host, "port": port, "password": password, "version": 1}


def resolve_and_connect(host: str, port: int, zone: str = "",
                        connect_timeout: float = 10.0, timeout: float = 120.0,
                        role: str = "transfer", app: str = "",
                        extra_hello: dict = None, password: str = "") -> tuple:
    """解析 IPv6 地址并逐个尝试连接，成功后完成 hello/密码握手。"""
    host = str(host).strip()
    if zone and "%" not in host:
        host = f"{host}%{zone}"
    try:
        infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ConnectError(
            f"无法解析 IPv6 地址 [{host}]:{port}（{exc}）\n"
            "  提示：链路本地地址需用 --zone <接口索引> 指定出口网卡；"
            "确认该地址确实属于对端的 IPv6 接口。"
        ) from exc

    errors = []
    seen = set()
    for family, socktype, proto, _canon, sockaddr in infos:
        if sockaddr in seen:
            continue
        seen.add(sockaddr)
        sock = socket.socket(family, socktype, proto)
        try:
            sock.settimeout(float(connect_timeout))
            sock.connect(sockaddr)
        except OSError as exc:
            errors.append(f"{sockaddr[0]}: {exc}")
            try:
                sock.close()
            except OSError:
                pass
            continue

        sock.settimeout(float(timeout))
        conn = Conn(sock)
        try:
            hello = {"cmd": "hello", "version": PROTO_VERSION, "role": role, "app": app}
            if extra_hello:
                hello.update(extra_hello)
            conn.send_msg(hello)
            reply = conn.recv_msg()
            if reply.get("auth_required"):
                nonce = reply.get("nonce")
                if not isinstance(nonce, str) or not nonce:
                    sock.close()
                    raise ConnectError("服务器要求密码认证，但握手数据不完整")
                if not password:
                    try:
                        conn.send_msg({"cmd": "auth", "response": ""})
                        conn.recv_msg()
                    except (OSError, ProtocolError):
                        pass
                    sock.close()
                    raise ConnectError("服务器已启用连接密码，请在客户端填写密码后重试")
                conn.send_msg({"cmd": "auth",
                               "response": _auth_digest(password, nonce)})
                reply = conn.recv_msg()
        except (OSError, ProtocolError) as exc:
            sock.close()
            raise ConnectError(f"握手失败：{exc}") from exc
        if not reply.get("ok"):
            sock.close()
            raise ConnectError(f"对端拒绝连接：{reply.get('error')}")

        peer = format_peer(sockaddr)
        return conn, peer, reply

    detail = "；".join(errors) if errors else f"没有可用地址：{infos}"
    raise ConnectError(
        f"连接 [{host}]:{port} 失败（{detail}）\n"
        "  排查：1) 对端是否已启动监听且端口正确；2) 两端 ping6 是否互通；"
        "3) 对端防火墙是否放行该端口。"
    )


# --------------------------------------------------------------------------- #
# 文件名与路径安全
# --------------------------------------------------------------------------- #
def sanitize_name(raw) -> str:
    """把对端给的名字收敛成一个安全的单段文件名（不含目录）。"""
    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolError("文件名为空")
    name = raw.replace("\\", "/").rstrip("/")
    name = name.rsplit("/", 1)[-1]
    name = "".join("_" if (ch in INVALID_NAME_CHARS or ord(ch) < 32) else ch for ch in name)
    name = name.strip().rstrip(". ")
    if name in ("", ".", ".."):
        raise ProtocolError("文件名不合法")
    if name.split(".")[0].upper() in RESERVED_NAMES:
        name = "_" + name
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ProtocolError("文件名过长")
    return name


def ensure_inside(root: str, candidate: str) -> str:
    """双保险：确认最终路径仍在目标目录内。"""
    root_abs = os.path.abspath(root)
    target_abs = os.path.abspath(candidate)
    if os.path.commonpath([root_abs, target_abs]) != root_abs:
        raise ProtocolError("目标路径越出目标目录")
    return target_abs


def part_path(root: str, name: str) -> str:
    return ensure_inside(root, os.path.join(root, name + ".part"))


def unique_final_name(root: str, name: str) -> str:
    """重名时加时间戳后缀，绝不覆盖已有文件。"""
    target = os.path.join(root, name)
    if not os.path.exists(target):
        return target
    stem, ext = os.path.splitext(name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    candidate = os.path.join(root, f"{stem}_{stamp}{ext}")
    counter = 2
    while os.path.exists(candidate):
        candidate = os.path.join(root, f"{stem}_{stamp}-{counter}{ext}")
        counter += 1
    return candidate


# --------------------------------------------------------------------------- #
# 哈希与格式化
# --------------------------------------------------------------------------- #
def sha256_of(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(CHUNK_SIZE), b""):
            hasher.update(block)
    return hasher.hexdigest()


def feed_prefix(fp, hasher, count: int) -> None:
    """把 fp 开头 count 字节喂给哈希器（续传时补算已有前缀）。"""
    remaining = count
    while remaining > 0:
        data = fp.read(min(CHUNK_SIZE, remaining))
        if not data:
            raise ProtocolError("分片文件比预期短")
        hasher.update(data)
        remaining -= len(data)


def human_bytes(num: float) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(value) < 1024.0 or unit == "TiB":
            return f"{int(value)} B" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def human_rate(num: float, seconds: float) -> str:
    return human_bytes(num / seconds if seconds > 0 else 0) + "/s"


def format_duration(seconds) -> str:
    if seconds is None or seconds < 0:
        return "--:--"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def timing_suffix(num: float, seconds: float) -> str:
    """耗时很短时不显示速率，避免出现 TiB/s 这类噪声。"""
    if seconds < 0.2:
        return f"，用时 {seconds * 1000:.0f}ms"
    return f"，用时 {seconds:.1f}s，{human_rate(num, seconds)}"


def format_peer(addr) -> str:
    host, port = addr[0], addr[1]
    if host.lower().startswith("::ffff:"):
        return f"{host[7:]}:{port} (IPv4-mapped)"
    return f"[{host}]:{port}"


# --------------------------------------------------------------------------- #
# 进度与限速
# --------------------------------------------------------------------------- #
class ProgressState:
    """记录字节进度并计算 EMA 速率与剩余时间；渲染交给调用方。"""

    __slots__ = ("total", "start", "done", "_t0", "_last_t", "_last_n", "_rate")

    def __init__(self, total: int, initial: int = 0) -> None:
        self.total = max(int(total), 0)
        self.start = max(int(initial), 0)
        self.done = self.start
        self._t0 = time.monotonic()
        self._last_t = self._t0
        self._last_n = self.done
        self._rate = 0.0

    def update(self, count: int) -> None:
        self.done += count

    def snapshot(self) -> dict:
        now = time.monotonic()
        interval = now - self._last_t
        if interval > 0:
            instant = (self.done - self._last_n) / interval
            self._rate = instant if self._rate <= 0 else 0.85 * self._rate + 0.15 * instant
            self._last_t = now
            self._last_n = self.done
        fraction = (self.done / self.total) if self.total else 1.0
        eta = None
        if self._rate > 0 and self.done < self.total:
            eta = (self.total - self.done) / self._rate
        return {
            "done": self.done,
            "total": self.total,
            "fraction": min(max(fraction, 0.0), 1.0),
            "rate": self._rate,
            "eta": eta,
            "elapsed": now - self._t0,
        }

    def final_snapshot(self) -> dict:
        """收尾时补一帧 100% 进度，保证界面/命令行最后显示到完成。"""
        snap = self.snapshot()
        snap["done"] = self.total
        snap["fraction"] = 1.0
        snap["eta"] = 0.0
        return snap


class RateLimiter:
    """按平均速率限速，便于慢链路演示与复现续传。"""

    __slots__ = ("rate", "sent", "t0")

    def __init__(self, bytes_per_sec: int) -> None:
        self.rate = float(bytes_per_sec or 0)
        self.sent = 0
        self.t0 = time.monotonic()

    def wait(self, count: int) -> None:
        if self.rate <= 0:
            return
        self.sent += count
        delay = self.sent / self.rate - (time.monotonic() - self.t0)
        if delay > 0:
            time.sleep(min(delay, 1.0))


def _make_sink(hasher, progress: ProgressState, on_progress, should_cancel, limiter, interval=EMIT_INTERVAL):
    """构造 per-chunk 回调：取消检查 + 哈希 + 进度 + 限速 + 限频上报。"""
    state = {"last": 0.0}

    def sink(chunk: memoryview) -> None:
        if should_cancel is not None and should_cancel():
            raise TransferCancelled("用户已取消")
        if hasher is not None:
            hasher.update(chunk)
        if progress is not None:
            progress.update(len(chunk))
        if limiter is not None:
            limiter.wait(len(chunk))
        if on_progress is not None and progress is not None:
            now = time.monotonic()
            if now - state["last"] >= interval:
                state["last"] = now
                on_progress(progress.snapshot())

    return sink


# --------------------------------------------------------------------------- #
# 单文件传输（客户端视角）
# --------------------------------------------------------------------------- #
@dataclass
class TransferResult:
    ok: bool
    direction: str                     # upload / download / receive / send
    name: str                          # 对端看到的名字
    final_name: str = ""
    path: str = ""
    size: int = 0
    local_sha256: str = ""
    peer_sha256: str = ""
    matched: bool = False
    resumed_from: int = 0
    seconds: float = 0.0
    error: str = ""
    retried: bool = False

    @property
    def data(self) -> dict:
        return {
            "ok": self.ok,
            "direction": self.direction,
            "name": self.name,
            "final_name": self.final_name,
            "path": self.path,
            "size": self.size,
            "local_sha256": self.local_sha256,
            "peer_sha256": self.peer_sha256,
            "matched": self.matched,
            "resumed_from": self.resumed_from,
            "seconds": self.seconds,
            "error": self.error,
            "retried": self.retried,
        }


def send_file(conn: Conn, path: str, *, resume: bool = True,
              on_progress=None, on_stage=None, should_cancel=None,
              limit_rate: int = 0, name: str = None) -> TransferResult:
    """上传一个本地文件；返回含双方哈希的结果。"""
    size = int(os.path.getsize(path))
    name = name or os.path.basename(path)
    limiter = RateLimiter(limit_rate) if limit_rate else None
    retried = False

    for attempt in (1, 2):
        began = time.monotonic()
        conn.send_msg({"cmd": "put", "name": name, "size": size, "resume": bool(resume)})
        reply = conn.recv_msg()
        if not reply.get("ok"):
            return TransferResult(False, "upload", name, size=size,
                                  error=str(reply.get("error") or "对端拒绝了上传"))

        start = int(reply.get("resume_from", 0))
        if start < 0 or start > size:
            return TransferResult(False, "upload", name, size=size,
                                  error=f"对端返回的续传偏移不合法（{start}）")
        if on_stage is not None:
            on_stage({"stage": "start", "name": name, "size": size, "resumed_from": start})

        hasher = hashlib.sha256()
        progress = ProgressState(size, start)
        sink = _make_sink(hasher, progress, on_progress, should_cancel, limiter)
        try:
            with open(path, "rb") as fp:
                if start:
                    feed_prefix(fp, hasher, start)
                fp.seek(start)
                conn.send_from_fp(fp, size - start, sink)
        except TransferCancelled:
            return TransferResult(False, "upload", name, size=size, resumed_from=start,
                                  error="已取消", local_sha256=hasher.hexdigest())

        local_hash = hasher.hexdigest()
        conn.send_msg({"cmd": "done", "sha256": local_hash})
        reply = conn.recv_msg()
        elapsed = max(time.monotonic() - began, 1e-6)
        if reply.get("ok"):
            if on_progress is not None:
                on_progress(progress.final_snapshot())
            peer_hash = str(reply.get("sha256") or "").lower()
            return TransferResult(True, "upload", name,
                                  final_name=str(reply.get("name") or name),
                                  path=os.path.abspath(path), size=size,
                                  local_sha256=local_hash, peer_sha256=peer_hash,
                                  matched=bool(peer_hash) and peer_hash == local_hash,
                                  resumed_from=start, seconds=elapsed, retried=retried)

        error = str(reply.get("error") or "上传失败")
        if reply.get("restart") and attempt == 1:
            retried = True
            if on_stage is not None:
                on_stage({"stage": "restart", "name": name, "size": size, "error": error})
            continue
        return TransferResult(False, "upload", name, size=size, resumed_from=start,
                              local_sha256=local_hash,
                              peer_sha256=str(reply.get("actual") or "").lower(),
                              seconds=elapsed, error=error, retried=retried)
    return TransferResult(False, "upload", name, size=size, error="上传失败")


def recv_file(conn: Conn, name: str, out_dir: str, *, resume: bool = True,
              on_progress=None, on_stage=None, should_cancel=None,
              limit_rate: int = 0, token: str = None) -> TransferResult:
    """从对端下载一个文件到 out_dir；返回含双方哈希的结果。
    token 用于服务器主动推送的任意路径文件（由 push 消息携带）。
    """
    os.makedirs(out_dir, exist_ok=True)
    base = sanitize_name(name)
    part = os.path.join(out_dir, base + ".part")
    limiter = RateLimiter(limit_rate) if limit_rate else None
    retried = False

    for attempt in (1, 2):
        began = time.monotonic()
        offset = os.path.getsize(part) if (resume and os.path.isfile(part)) else 0
        request = {"cmd": "get", "name": base, "offset": offset}
        if token:
            request["token"] = str(token)
        conn.send_msg(request)
        reply = conn.recv_msg()
        if not reply.get("ok"):
            return TransferResult(False, "download", base, error=str(reply.get("error") or "下载失败"))

        size = int(reply.get("size", -1))
        if size < 0:
            return TransferResult(False, "download", base, error="对端返回的文件尺寸不合法")
        peer_hash = str(reply.get("sha256") or "").lower()
        start = int(reply.get("resume_from", 0))
        if start < 0 or start > size:
            start = 0

        hasher = hashlib.sha256()
        if os.path.isfile(part):
            if start > 0:
                with open(part, "rb") as fp:
                    feed_prefix(fp, hasher, start)
                with open(part, "r+b") as fp:
                    fp.truncate(start)
            else:
                with open(part, "r+b") as fp:
                    fp.truncate(0)
        elif start > 0:
            start = 0
        if on_stage is not None:
            on_stage({"stage": "start", "name": base, "size": size, "resumed_from": start})

        progress = ProgressState(size, start)
        sink = _make_sink(hasher, progress, on_progress, should_cancel, limiter)
        try:
            with open(part, "ab") as fp:
                conn.recv_to_fp(fp, size - start, sink)
        except TransferCancelled:
            return TransferResult(False, "download", base, size=size, resumed_from=start,
                                  error="已取消", local_sha256=hasher.hexdigest())

        local_hash = hasher.hexdigest()
        actual_size = os.path.getsize(part)
        ok = actual_size == size and local_hash == peer_hash
        conn.send_msg({"cmd": "done", "ok": ok, "sha256": local_hash})
        elapsed = max(time.monotonic() - began, 1e-6)

        if ok:
            if on_progress is not None:
                on_progress(progress.final_snapshot())
            final = unique_final_name(out_dir, base)
            os.replace(part, final)
            return TransferResult(True, "download", base,
                                  final_name=os.path.basename(final),
                                  path=os.path.abspath(final), size=size,
                                  local_sha256=local_hash, peer_sha256=peer_hash,
                                  matched=True, resumed_from=start,
                                  seconds=elapsed, retried=retried)

        if os.path.exists(part):
            os.remove(part)
        if attempt == 1:
            retried = True
            if on_stage is not None:
                on_stage({"stage": "restart", "name": base, "size": size, "error": "hash_mismatch"})
            continue
        return TransferResult(False, "download", base, size=size, resumed_from=start,
                              local_sha256=local_hash, peer_sha256=peer_hash,
                              seconds=elapsed, error="完整性校验不一致", retried=retried)
    return TransferResult(False, "download", base, error="下载失败")


def list_remote(conn: Conn) -> tuple:
    conn.send_msg({"cmd": "list"})
    reply = conn.recv_msg()
    if not reply.get("ok"):
        raise ProtocolError(str(reply.get("error") or "获取列表失败"))
    return list(reply.get("files") or []), str(reply.get("root") or "")


# --------------------------------------------------------------------------- #
# 服务端会话（事件式）
# --------------------------------------------------------------------------- #
def _emit_progress(emit, direction: str, name: str, snapshot: dict) -> None:
    if emit is None:
        return
    event = {"type": "progress", "direction": direction, "name": name}
    event.update(snapshot)
    emit(event)


def _serve_put(conn: Conn, msg: dict, root: str, locks: "NameLocks", emit) -> None:
    name = sanitize_name(msg.get("name"))
    size = parse_int(msg.get("size"), "size")
    if size < 0 or size > MAX_FILE_SIZE:
        raise ProtocolError("文件尺寸超出范围")
    resume_requested = bool(msg.get("resume", True))
    key = name.casefold()
    if not locks.try_acquire(key):
        conn.send_msg({"ok": False, "error": f"文件 {name} 正在被其他客户端传输"})
        if emit is not None:
            emit({"type": "error", "direction": "receive", "name": name, "fatal": False,
                  "error": "同名文件正在被其他客户端传输"})
        return

    try:
        part = part_path(root, name)
        existing = os.path.getsize(part) if os.path.exists(part) else 0
        if not resume_requested or existing > size:
            if os.path.exists(part):
                with open(part, "r+b") as fp:
                    fp.truncate(0)
            existing = 0
        start = min(existing, size)
        conn.send_msg({"ok": True, "resume_from": start, "size": size, "name": name})
        if emit is not None:
            emit({"type": "start", "direction": "receive", "name": name,
                  "size": size, "resumed_from": start})

        began = time.monotonic()
        hasher = hashlib.sha256()
        if start:
            with open(part, "rb") as prefix:
                feed_prefix(prefix, hasher, start)
        progress = ProgressState(size, start)
        sink = _make_sink(hasher, progress, lambda snap: _emit_progress(emit, "receive", name, snap), None, None)
        with open(part, "ab") as fp:
            conn.recv_to_fp(fp, size - start, sink)

        done = conn.recv_msg()
        if done.get("cmd") != "done":
            raise ProtocolError("缺少 done 消息")
        claimed = str(done.get("sha256", "")).lower()
        if len(claimed) != 64 or any(ch not in "0123456789abcdef" for ch in claimed):
            raise ProtocolError("done.sha256 不是合法十六进制摘要")

        local_hash = hasher.hexdigest()
        actual_size = os.path.getsize(part)
        elapsed = max(time.monotonic() - began, 1e-6)
        if actual_size != size or local_hash != claimed:
            if os.path.exists(part):
                os.remove(part)
            conn.send_msg({"ok": False, "error": "hash_mismatch", "restart": True,
                           "expected": claimed, "actual": local_hash, "received_size": actual_size})
            if emit is not None:
                emit({"type": "error", "direction": "receive", "name": name, "fatal": False,
                      "error": f"完整性校验失败（收到 {actual_size}/{size} 字节），已删除分片",
                      "local_sha256": local_hash, "peer_sha256": claimed, "matched": False})
            return

        final = unique_final_name(root, name)
        ensure_inside(root, final)
        os.replace(part, final)
        conn.send_msg({"ok": True, "name": os.path.basename(final), "size": size, "sha256": local_hash})
        if emit is not None:
            emit({"type": "done", "result": TransferResult(
                True, "receive", name, final_name=os.path.basename(final),
                path=os.path.abspath(final), size=size,
                local_sha256=local_hash, peer_sha256=claimed, matched=local_hash == claimed,
                resumed_from=start, seconds=elapsed).data})
    finally:
        locks.release(key)


def _serve_get(conn: Conn, msg: dict, root: str, emit, path_resolver=None) -> None:
    name = sanitize_name(msg.get("name"))
    token = msg.get("token")
    resolved = None
    if token and path_resolver is not None:
        try:
            resolved = path_resolver(str(token))
        except Exception:
            resolved = None
    if resolved:
        path = resolved
    else:
        path = ensure_inside(root, os.path.join(root, name))
    if not os.path.isfile(path):
        conn.send_msg({"ok": False, "error": f"服务器上不存在文件 {name}"})
        if emit is not None:
            emit({"type": "error", "direction": "send", "name": name, "fatal": False,
                  "error": "文件不存在"})
        return
    size = os.path.getsize(path)
    offset = parse_int(msg.get("offset", 0), "offset")
    start = offset if 0 < offset <= size else 0
    digest = sha256_of(path)

    conn.send_msg({"ok": True, "name": name, "size": size, "sha256": digest, "resume_from": start})
    if emit is not None:
        emit({"type": "start", "direction": "send", "name": name,
              "size": size, "resumed_from": start})

    began = time.monotonic()
    progress = ProgressState(size, start)
    sink = _make_sink(None, progress, lambda snap: _emit_progress(emit, "send", name, snap), None, None)
    with open(path, "rb") as fp:
        fp.seek(start)
        conn.send_from_fp(fp, size - start, sink)
    ack = conn.recv_msg()
    elapsed = max(time.monotonic() - began, 1e-6)
    peer_hash = str(ack.get("sha256") or "").lower()
    matched = bool(ack.get("ok")) and peer_hash == digest
    if emit is not None:
        if ack.get("ok"):
            emit({"type": "done", "result": TransferResult(
                True, "send", name, final_name=name, path=os.path.abspath(path), size=size,
                local_sha256=digest, peer_sha256=peer_hash, matched=matched,
                resumed_from=start, seconds=elapsed).data})
        else:
            emit({"type": "error", "direction": "send", "name": name, "fatal": False,
                  "error": "客户端报告校验失败", "local_sha256": digest,
                  "peer_sha256": peer_hash, "matched": False})


def _serve_list(conn: Conn, root: str) -> None:
    files = []
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if not entry.is_file() or entry.name.endswith(".part"):
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                files.append({"name": entry.name, "size": st.st_size, "mtime": int(st.st_mtime)})
                if len(files) >= MAX_LIST_ENTRIES:
                    break
    except OSError as exc:
        conn.send_msg({"ok": False, "error": f"读取目录失败：{exc}"})
        return
    files.sort(key=lambda item: item["name"].lower())
    conn.send_msg({"ok": True, "files": files, "root": os.path.abspath(root)})


class NameLocks:
    """按文件名加锁，避免两个客户端同时写同一个分片。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks = {}

    def try_acquire(self, key: str) -> bool:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
        return lock.acquire(blocking=False)

    def release(self, key: str) -> None:
        with self._guard:
            lock = self._locks.get(key)
        if lock is not None and lock.locked():
            try:
                lock.release()
            except RuntimeError:
                pass


def handle_transfer_session(conn: Conn, root: str, *, timeout: float = 120.0,
                            locks: NameLocks = None, emit=None,
                            path_resolver=None) -> None:
    """服务端文件会话：处理 put/get/list/bye，直到对端关闭。"""
    locks = locks or NameLocks()
    conn.sock.settimeout(float(timeout))
    while True:
        msg = conn.recv_msg()
        cmd = str(msg.get("cmd", ""))
        if cmd == "put":
            _serve_put(conn, msg, root, locks, emit)
        elif cmd == "get":
            _serve_get(conn, msg, root, emit, path_resolver=path_resolver)
        elif cmd == "list":
            _serve_list(conn, root)
        elif cmd == "bye":
            conn.send_msg({"ok": True})
            return
        else:
            conn.send_msg({"ok": False, "error": f"未知命令: {cmd!r}"})
            return


# --------------------------------------------------------------------------- #
# 文字通道（独立于文件连接）
# --------------------------------------------------------------------------- #
class ControlLink:
    """长连接消息通道：聊天 say、系统通知 notice、文件推送 push、心跳 ping。"""

    def __init__(self, conn: Conn, peer: str, on_text=None, on_state=None,
                 on_push=None, on_notice=None) -> None:
        self.conn = conn
        self.sock = conn.sock
        self.peer = peer
        self._on_text = on_text
        self._on_state = on_state
        self._on_push = on_push
        self._on_notice = on_notice
        self._write_lock = threading.Lock()
        self._closed = False
        self._closing = False
        self._reader = threading.Thread(target=self._read_loop, name="control-reader", daemon=True)
        self._keepalive = threading.Thread(target=self._ping_loop, name="control-ping", daemon=True)

    # -- 构建 ---------------------------------------------------------------
    @classmethod
    def connect(cls, host: str, port: int, zone: str = "", *,
                timeout: float = 120.0, connect_timeout: float = 10.0,
                on_text=None, on_state=None, on_push=None, on_notice=None,
                client_id: str = "", password: str = "") -> "ControlLink":
        conn, peer, _reply = resolve_and_connect(host, port, zone,
                                                 connect_timeout=connect_timeout,
                                                 timeout=timeout, role="control",
                                                 app="file_gui/1.0",
                                                 extra_hello={"client_id": client_id} if client_id else None,
                                                 password=password)
        conn.sock.settimeout(None)          # 文字通道允许长时间空闲
        link = cls(conn, peer, on_text, on_state, on_push, on_notice)
        link.start()
        return link

    @classmethod
    def from_accepted(cls, conn: Conn, peer: str, on_text=None, on_state=None,
                      on_push=None, on_notice=None) -> "ControlLink":
        conn.sock.settimeout(None)
        link = cls(conn, peer, on_text, on_state, on_push, on_notice)
        link.start()
        return link

    # -- 生命周期 -----------------------------------------------------------
    @property
    def is_closed(self) -> bool:
        return self._closed

    def start(self) -> None:
        self._reader.start()
        self._keepalive.start()

    def _read_loop(self) -> None:
        try:
            while not self._closed:
                msg = self.conn.recv_msg()
                cmd = msg.get("cmd")
                if cmd is None:                       # ack / pong
                    continue
                if cmd == "say":
                    text = msg.get("text")
                    if isinstance(text, str) and text:
                        if self._on_text is not None:
                            self._on_text(text, int(msg.get("ts") or 0))
                        self._send({"ok": True, "ts": msg.get("ts")})
                    else:
                        self._send({"ok": False, "error": "text 必须是非空字符串"})
                elif cmd == "push":
                    try:
                        name = sanitize_name(msg.get("name"))
                        size = parse_int(msg.get("size"), "size")
                        if size < 0 or size > MAX_FILE_SIZE:
                            raise ProtocolError("文件尺寸超出范围")
                    except ProtocolError as exc:
                        self._send({"ok": False, "error": str(exc)})
                        continue
                    token = msg.get("token")
                    if not isinstance(token, str) or not token:
                        token = None
                    if self._on_push is not None:
                        self._on_push(name, size, token)
                    self._send({"ok": True})
                elif cmd == "notice":
                    text = msg.get("text")
                    if isinstance(text, str) and text.strip():
                        if self._on_notice is not None:
                            self._on_notice(text, int(msg.get("ts") or 0))
                        self._send({"ok": True})
                    else:
                        self._send({"ok": False, "error": "text 必须是非空字符串"})
                elif cmd == "ping":
                    self._send({"ok": True, "type": "pong"})
                elif cmd == "bye":
                    break
                else:
                    self._send({"ok": False, "error": f"未知命令: {cmd!r}"})
        except (OSError, ConnectionError, ProtocolError):
            pass
        finally:
            was_closed = self._closed
            self._closed = True
            try:
                self.sock.close()
            except OSError:
                pass
            if not was_closed and not self._closing and self._on_state is not None:
                self._on_state(False)

    def _ping_loop(self) -> None:
        while not self._closed:
            time.sleep(PING_INTERVAL)
            if self._closed:
                return
            try:
                self._send({"cmd": "ping"})
            except (OSError, ConnectionError, ProtocolError):
                return

    def _send(self, msg: dict) -> None:
        with self._write_lock:
            if self._closed:
                raise ConnectionError("文字通道已关闭")
            self.conn.send_msg(msg)

    def send_text(self, text: str) -> bool:
        text = str(text)
        if not text.strip():
            raise ValueError("消息不能为空")
        if len(text) > MAX_TEXT_CHARS:
            raise ValueError(f"单条消息最多 {MAX_TEXT_CHARS} 个字符")
        self._send({"cmd": "say", "text": text, "ts": int(time.time() * 1000)})
        return True

    def send_push(self, name: str, size: int, token: str = None) -> bool:
        """通知对端来下载某个文件（对端随后用 get 拉取；token 可指向任意本地文件）。"""
        try:
            clean = sanitize_name(name)
            size_int = parse_int(size, "size")
        except ProtocolError as exc:
            raise ValueError(str(exc)) from exc
        if size_int < 0 or size_int > MAX_FILE_SIZE:
            raise ValueError("文件尺寸超出范围")
        message = {"cmd": "push", "name": clean, "size": size_int,
                   "ts": int(time.time() * 1000)}
        if token:
            message["token"] = str(token)
        self._send(message)
        return True

    def send_notice(self, text: str) -> bool:
        """发送一条系统通知（对端以灰色系统消息显示）。"""
        clean = str(text).strip()
        if not clean:
            raise ValueError("通知内容不能为空")
        if len(clean) > MAX_TEXT_CHARS:
            clean = clean[:MAX_TEXT_CHARS]
        self._send({"cmd": "notice", "text": clean, "ts": int(time.time() * 1000)})
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        try:
            with self._write_lock:
                self.conn.send_msg({"cmd": "bye"})
        except (OSError, ConnectionError, ProtocolError):
            pass
        self._closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 服务器监听与地址展示（CLI 与 GUI 共用）
# --------------------------------------------------------------------------- #
def make_listener(host: str, port: int, ipv6_only: bool = False) -> socket.socket:
    srv = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    if hasattr(socket, "IPV6_V6ONLY"):
        try:
            srv.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1 if ipv6_only else 0)
        except OSError:
            pass
    srv.bind((host, port))
    srv.listen(64)
    return srv


# --------------------------------------------------------------------------- #
# 本机 IPv6 地址枚举（不依赖第三方库；Windows 走 netsh / ipconfig）
# --------------------------------------------------------------------------- #
SCOPE_ORDER = {"global": 0, "ula": 1, "link-local": 2, "other": 3, "loopback": 4}
SCOPE_LABELS = {
    "global": "全局地址",
    "ula": "ULA 私有地址",
    "link-local": "链路本地",
    "other": "其他 IPv6 地址",
    "loopback": "回环地址",
}
_TUNNEL_NETS = (
    ipaddress.IPv6Network("2001:0::/32"),      # Teredo
    ipaddress.IPv6Network("2002::/16"),        # 6to4
)
_NETSH_INTERFACE = re.compile(r"^\s*Interface\s+\d+:\s*(.*)$", re.IGNORECASE)
_NETSH_ADDRESS = re.compile(r"([0-9A-Fa-f:.]{2,}(?:%\d+)?)\s*$")
_IPCONFIG_ADDRESS = re.compile(r"([0-9A-Fa-f:]{2,}(?:%\d+)?)")
_IPCONFIG_SKIP = ("网关", "Gateway", "DNS", "服务器", "Server")
_IPCONFIG_IPV6_HINTS = ("IPv6", "ipv6")


def _decode_console(data: bytes) -> str:
    for encoding in ("utf-8", "mbcs", "gbk", "latin-1"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _run_command(args) -> str:
    try:
        proc = subprocess.run(args, capture_output=True, timeout=6, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return _decode_console(proc.stdout or b"")


def _classify_ipv6(ip) -> str:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "ula"
    if ip.is_global:
        return "global"
    return "other"


def _make_entry(address, interface: str = "", deprecated: bool = False,
                zone=None, source: str = "", temporary: bool = False) -> dict:
    token = str(address or "").strip()
    if token.startswith("[") and token.endswith("]"):
        token = token[1:-1]
    if "%" in token:
        token, zone_text = token.split("%", 1)
        try:
            zone = int(zone_text)
        except ValueError:
            pass
    try:
        ip = ipaddress.IPv6Address(token)
    except ValueError:
        return None
    if ip.is_multicast or ip.is_unspecified or ip.ipv4_mapped is not None:
        return None
    text = str(ip)
    if any(ip in network for network in _TUNNEL_NETS) or ":5efe:" in text or ":0:5efe:" in text:
        return None
    scope = _classify_ipv6(ip)
    display = text
    if scope == "link-local" and zone is not None:
        display = f"{text}%{zone}"
    if scope == "loopback":
        interface = ""
    return {
        "address": display,
        "ip": text,
        "scope": scope,
        "zone": zone,
        "interface": interface or "",
        "deprecated": bool(deprecated),
        "temporary": bool(temporary),
        "source": source,
    }


def _dedupe_entries(entries) -> list:
    best = {}
    for entry in entries:
        if not entry:
            continue
        key = (entry["ip"], entry["zone"] if entry["scope"] == "link-local" else None)
        score = ((2 if entry["interface"] else 0)
                 + (1 if entry["deprecated"] else 0)
                 + (1 if not entry.get("temporary") else 0))
        if key not in best or score > best[key][0]:
            best[key] = (score, entry)
    return [entry for _score, entry in best.values()]


def parse_netsh_ipv6(text: str) -> list:
    """解析 `netsh interface ipv6 show address` 输出。"""
    entries = []
    interface = ""
    for line in (text or "").splitlines():
        header = _NETSH_INTERFACE.match(line)
        if header:
            interface = header.group(1).strip()
            continue
        if "---" in line:
            continue
        match = _NETSH_ADDRESS.search(line)
        if not match:
            continue
        deprecated = "Deprecated" in line or "已弃用" in line or "弃用" in line
        temporary = "Temporary" in line or "临时" in line
        entry = _make_entry(match.group(1), interface=interface,
                            deprecated=deprecated, temporary=temporary, source="netsh")
        if entry:
            entries.append(entry)
    return entries


def parse_ipconfig_ipv6(text: str) -> list:
    """解析 `ipconfig` 输出中的 IPv6 地址行（排除网关/DNS）。"""
    entries = []
    for line in (text or "").splitlines():
        if not any(hint in line for hint in _IPCONFIG_IPV6_HINTS):
            continue
        if any(skip in line for skip in _IPCONFIG_SKIP):
            continue
        for token in _IPCONFIG_ADDRESS.findall(line):
            entry = _make_entry(token, source="ipconfig",
                                temporary=("临时" in line or "Temporary" in line))
            if entry:
                entries.append(entry)
    return entries


def _proc_net_entries() -> list:
    """Linux/Android：/proc/net/if_inet6 是枚举 IPv6 地址最可靠的方式。"""
    try:
        with open("/proc/net/if_inet6", "r", encoding="ascii", errors="ignore") as handle:
            text = handle.read()
    except OSError:
        return []
    return parse_proc_net_if_inet6(text)


def parse_proc_net_if_inet6(text: str) -> list:
    """解析 /proc/net/if_inet6。

    每行格式：地址(32 hex) ifindex(hex) prefix(hex) scope(hex) flags(hex) 接口名
    """
    entries = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) != 6:
            continue
        addr_hex, ifindex_hex, _prefix_hex, _scope_hex, flags_hex, interface = parts
        try:
            ip = ipaddress.IPv6Address(int(addr_hex, 16))
            ifindex = int(ifindex_hex, 16)
            flags = int(flags_hex, 16)
        except ValueError:
            continue
        entry = _make_entry(str(ip), interface=interface,
                            zone=ifindex if ip.is_link_local else None,
                            deprecated=bool(flags & 0x20),
                            temporary=bool(flags & 0x02),
                            source="proc")
        if entry:
            entries.append(entry)
    return entries


def parse_ip_addr_ipv6(text: str) -> list:
    """解析 `ip -6 addr show` 输出（Android/Linux 的备用枚举方式）。"""
    entries = []
    interface = ""
    ifindex = None
    for line in (text or "").splitlines():
        header = re.match(r"^\s*(\d+):\s+([^:@]+)[:@]", line)
        if header:
            ifindex = int(header.group(1))
            interface = header.group(2).strip()
            continue
        match = re.search(r"inet6\s+([0-9A-Fa-f:]+)(?:/\d+)?\s+scope\s+(\w+)", line)
        if not match:
            continue
        address, scope = match.group(1), match.group(2).lower()
        entry = _make_entry(address, interface=interface,
                            zone=ifindex if scope == "link" else None,
                            deprecated="deprecated" in line,
                            temporary="temporary" in line,
                            source="ip")
        if entry:
            entries.append(entry)
    return entries


def _ip_command_entries() -> list:
    for command in (["ip", "-6", "addr", "show"],
                    ["/system/bin/ip", "-6", "addr", "show"]):
        text = _run_command(command)
        if text.strip():
            entries = parse_ip_addr_ipv6(text)
            if entries:
                return entries
    return []


def _route_probe_entries() -> list:
    """最后兜底：UDP connect 不发送数据，只让系统选一条 IPv6 出口地址。"""
    probes = (("2001:4860:4860::8888", 53),
              ("2400:3200::1", 53),
              ("240c::6666", 53))
    for host, port in probes:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            sock.settimeout(0.5)
            sock.connect((host, port))
            address = sock.getsockname()[0]
        except OSError:
            continue
        finally:
            try:
                sock.close()
            except OSError:
                pass
        entry = _make_entry(address, source="route")
        if entry:
            return [entry]
    return []


def _link_local_probe_entries() -> list:
    """兜底探测链路本地地址：对每个接口 UDP connect fe80::1，取源地址。"""
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return []
    entries = []
    for index, name in interfaces:
        if index == 1 or name in ("lo", "lo0"):
            continue
        probe = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        try:
            probe.settimeout(0.2)
            probe.connect(("fe80::1", 53, 0, index))
            local = probe.getsockname()
        except OSError:
            continue
        finally:
            try:
                probe.close()
            except OSError:
                pass
        if not local:
            continue
        address = local[0]
        zone = local[3] if len(local) > 3 else index
        entry = _make_entry(address, interface=name, zone=zone, source="link-probe")
        if entry:
            entries.append(entry)
    return entries


def _ifaddrs_entries() -> list:
    getter = getattr(socket, "getifaddrs", None)
    if getter is None:
        return []
    try:
        items = getter()
    except OSError:
        return []
    entries = []
    for item in items:
        if item.family != socket.AF_INET6:
            continue
        address = item.address
        zone = None
        if isinstance(address, tuple):
            host = address[0]
            if len(address) > 3:
                zone = address[3]
        else:
            host = str(address)
        entry = _make_entry(host, interface=item.name or "", zone=zone, source="getifaddrs")
        if entry:
            entries.append(entry)
    return entries


def _getaddrinfo_entries() -> list:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
    except OSError:
        return []
    entries = []
    for info in infos:
        sockaddr = info[4]
        zone = sockaddr[3] if len(sockaddr) > 3 else None
        entry = _make_entry(sockaddr[0], zone=zone, source="getaddrinfo")
        if entry:
            entries.append(entry)
    return entries


def _is_android() -> bool:
    executable = str(getattr(sys, "executable", "") or "").lower()
    return bool(os.environ.get("ANDROID_ROOT")
                or os.environ.get("ANDROID_DATA")
                or os.path.exists("/system/build.prop")
                or hasattr(sys, "getandroidapilevel")
                or "pydroid" in executable
                or "ru.iiec.pydroid3" in executable)


def list_ipv6_addresses(include_loopback: bool = False) -> list:
    """枚举本机 IPv6 地址；默认不含 ::1，按 全局 > ULA > 链路本地 排序。"""
    entries = _ifaddrs_entries()
    if os.path.exists("/proc/net/if_inet6"):
        entries.extend(_proc_net_entries())
    if sys.platform == "win32":
        entries.extend(parse_netsh_ipv6(_run_command(
            ["netsh", "interface", "ipv6", "show", "address"])))
    entries.extend(_getaddrinfo_entries())

    real_scopes = {"global", "ula", "link-local", "other"}
    if not any(e["scope"] in ("global", "ula") for e in entries):
        entries.extend(_route_probe_entries())
    if not any(e["scope"] == "link-local" for e in entries):
        entries.extend(_link_local_probe_entries())
    if sys.platform == "win32" and not any(e["scope"] in real_scopes for e in entries):
        for command in (["ipconfig"], ["ipconfig", "/all"]):
            found = parse_ipconfig_ipv6(_run_command(command))
            if any(e["scope"] in real_scopes for e in found):
                entries.extend(found)
                break

    merged = _dedupe_entries(entries)
    if not include_loopback:
        merged = [e for e in merged if e["scope"] != "loopback"]
    merged.sort(key=lambda e: (SCOPE_ORDER.get(e["scope"], 9), e["deprecated"],
                               bool(e.get("temporary")), e["ip"]))
    return merged


def format_address_entry(entry: dict) -> str:
    """把地址条目渲染成「地址  (说明)」，供 GUI 列表与 CLI 横幅共用。"""
    scope = entry.get("scope", "other")
    parts = [SCOPE_LABELS.get(scope, SCOPE_LABELS["other"])]
    if entry.get("interface"):
        parts.append(str(entry["interface"]))
    if scope == "link-local":
        if entry.get("zone") is not None:
            parts.append(f"同网段需接口索引 {entry['zone']}")
        else:
            parts.append("同网段连接需接口索引")
    hint = {
        "global": "推荐远程连接",
        "ula": "仅内网可用",
        "loopback": "仅同机测试",
    }.get(scope)
    if hint:
        parts.append(hint)
    if entry.get("deprecated"):
        parts.append("已弃用")
    if entry.get("temporary"):
        parts.append("临时地址，会变化")
    return f"{entry.get('address', '')}  ({' · '.join(parts)})"


def describe_ipv6(addr: str) -> str:
    entry = _make_entry(addr, source="legacy")
    return SCOPE_LABELS.get(entry["scope"], "") if entry else ""


def parse_ipv6_address(address: str) -> dict:
    """把用户输入的地址解析成地址条目；无效时返回 None。"""
    return _make_entry(address, source="manual")
