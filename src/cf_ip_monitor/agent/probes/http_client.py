"""
通过指定 IP 发起 HTTPS 请求 (相当于 curl --resolve)。

实现要点:
- TCP 连接到 (ip, port)
- TLS 握手时把 SNI 设为目标 hostname (否则 Cloudflare 不会路由到正确租户)
- HTTP/1.1 请求里 Host 头也设成目标 hostname
- 证书校验关闭, 因为我们走 IP 直连 (Cloudflare 边缘统一证书也能过校验, 但关掉更稳)
"""
from __future__ import annotations

import socket
import ssl
import time
from typing import Iterator, Optional, Tuple


_USER_AGENT = "cf-ip-monitor/1.0"


def _make_ssl_ctx() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect_tls(ip: str, port: int, sni: str, timeout: float) -> ssl.SSLSocket:
    sock = socket.create_connection((ip, port), timeout=timeout)
    sock.settimeout(timeout)
    ctx = _make_ssl_ctx()
    ssock = ctx.wrap_socket(sock, server_hostname=sni)
    return ssock


def http_get(
    ip: str,
    host: str,
    path: str,
    port: int = 443,
    timeout: float = 5.0,
) -> Tuple[int, bytes]:
    """简易 HTTPS GET, 一次性读完返回 body。仅适用于小响应 (如 /cdn-cgi/trace)。"""
    ssock = connect_tls(ip, port, host, timeout)
    try:
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {_USER_AGENT}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        )
        ssock.sendall(req.encode("ascii"))
        buf = bytearray()
        while True:
            try:
                chunk = ssock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            buf.extend(chunk)
        return _parse_http_response(bytes(buf))
    finally:
        try:
            ssock.close()
        except Exception:
            pass


def http_stream_download(
    ip: str,
    host: str,
    path: str,
    max_bytes: int,
    timeout: float,
    port: int = 443,
) -> Tuple[int, float, Optional[str]]:
    """流式下载, 返回 (实际下载字节数, 持续秒数, error_or_None)。

    计时起点为请求发送完毕之后, 即只计算实际传输阶段, 排除 TLS/请求延迟。
    """
    try:
        ssock = connect_tls(ip, port, host, timeout)
    except Exception as e:
        return 0, 0.0, f"connect_failed:{e}"

    try:
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            f"User-Agent: {_USER_AGENT}\r\n"
            f"Accept: */*\r\n"
            f"Connection: close\r\n\r\n"
        )
        ssock.sendall(req.encode("ascii"))

        # 先读 header
        header_buf = bytearray()
        while b"\r\n\r\n" not in header_buf:
            chunk = ssock.recv(4096)
            if not chunk:
                return 0, 0.0, "no_response_header"
            header_buf.extend(chunk)
            if len(header_buf) > 65536:
                return 0, 0.0, "header_too_large"
        head, _, leftover = bytes(header_buf).partition(b"\r\n\r\n")
        first_line = head.split(b"\r\n", 1)[0].decode("ascii", "ignore")
        status = _parse_status(first_line)
        if status >= 400:
            return 0, 0.0, f"http_status:{status}"

        downloaded = len(leftover)
        start = time.perf_counter()
        deadline = start + timeout
        while downloaded < max_bytes:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            ssock.settimeout(max(0.1, remaining))
            try:
                chunk = ssock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            downloaded += len(chunk)
        duration = max(0.001, time.perf_counter() - start)
        return downloaded, duration, None
    except Exception as e:
        return 0, 0.0, f"transfer_failed:{e}"
    finally:
        try:
            ssock.close()
        except Exception:
            pass


# ---------------------------------------------------------------- parse helpers
def _parse_http_response(raw: bytes) -> Tuple[int, bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    first = head.split(b"\r\n", 1)[0].decode("ascii", "ignore")
    return _parse_status(first), body


def _parse_status(status_line: str) -> int:
    parts = status_line.split(" ", 2)
    if len(parts) >= 2 and parts[1].isdigit():
        return int(parts[1])
    return 0
