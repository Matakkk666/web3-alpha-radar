"""Local HTTP CONNECT proxy that forwards to an upstream SOCKS proxy with auth.

Chromium cannot authenticate to SOCKS5, so Playwright talks to this loopback HTTP
proxy instead.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote, unquote, urlsplit

from python_socks.async_.asyncio import Proxy

logger = logging.getLogger(__name__)

_server: asyncio.AbstractServer | None = None
_port: int | None = None
_lock = asyncio.Lock()


def normalized_upstream(proxy_url: str) -> str:
    url = urlsplit(proxy_url)
    if not url.scheme or not url.hostname:
        raise ValueError("PROXY_URL must include a scheme and hostname")
    scheme = {"socks5h": "socks5", "socks4a": "socks4"}.get(url.scheme.lower(), url.scheme.lower())
    userinfo = ""
    if url.username is not None:
        userinfo = quote(unquote(url.username), safe="")
        if url.password is not None:
            userinfo += ":" + quote(unquote(url.password), safe="")
        userinfo += "@"
    port = f":{url.port}" if url.port else ""
    return f"{scheme}://{userinfo}{url.hostname}{port}"


def socks_needs_http_bridge(proxy_url: str | None) -> bool:
    if not proxy_url:
        return False
    url = urlsplit(proxy_url)
    return url.scheme.lower() in {"socks5", "socks5h", "socks4", "socks4a"} and bool(url.username)


async def _pipe(src: asyncio.StreamReader, dest: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dest.write(data)
            await dest.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            dest.close()
        except Exception:
            pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, upstream: str) -> None:
    try:
        header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        first = header.split(b"\r\n", 1)[0].decode("latin-1")
        method, target, *_ = first.split(" ")
        if method.upper() != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return
        host, port_s = target.rsplit(":", 1)
        dest_host = host.strip("[]")
        dest_port = int(port_s)
        sock = await Proxy.from_url(upstream).connect(dest_host=dest_host, dest_port=dest_port)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        remote_reader, remote_writer = await asyncio.open_connection(sock=sock)
        await asyncio.gather(_pipe(reader, remote_writer), _pipe(remote_reader, writer))
    except Exception:
        logger.exception("HTTP/SOCKS bridge request failed")
        try:
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except Exception:
            pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def ensure_bridge(proxy_url: str) -> int:
    """Start (once) a loopback HTTP proxy; return its port."""
    global _server, _port
    async with _lock:
        if _server is not None and _port is not None:
            return _port
        upstream = normalized_upstream(proxy_url)

        async def client_connected(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await _handle(reader, writer, upstream)

        _server = await asyncio.start_server(client_connected, "127.0.0.1", 0)
        _port = _server.sockets[0].getsockname()[1]
        logger.info("Started local HTTP proxy bridge on 127.0.0.1:%s", _port)
        return _port
