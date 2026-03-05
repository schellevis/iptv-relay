#!/usr/bin/env python3
"""
IPTV Relay Server

Proxies one of several configured IPTV streams to colleagues via HTTP with
Basic Authentication.  Switch between streams live from the CLI by typing
the stream number and pressing Enter.

Usage:
    python relay.py [--config config.yaml]

Endpoints:
    /stream-{token}      → the currently active stream (proxied)
    /playlist-{token}.m3u → shareable M3U playlist
    /status-{token}      → plain-text status overview
"""

import argparse
import asyncio
import collections
import html
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
import socket
import sys
import time
from urllib.parse import quote, urljoin, urlsplit

import aiohttp
import yaml
from aiohttp import web

# ── Logging ───────────────────────────────────────────────────────────────────

_log_lines: collections.deque = collections.deque(maxlen=5)
_in_keyboard_loop: bool = False
_LOG_FULL_FMT = logging.Formatter("%(asctime)s  %(levelname)-7s  %(message)s")
_LOG_UI_FMT   = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")


class _UILogHandler(logging.Handler):
    """Before keyboard loop: print normally. During loop: show in UI area."""
    def emit(self, record: logging.LogRecord) -> None:
        if _in_keyboard_loop:
            _log_lines.append(_LOG_UI_FMT.format(record))
            _print_ui()
        else:
            sys.stdout.write(_LOG_FULL_FMT.format(record) + "\n")
            sys.stdout.flush()


logging.basicConfig(level=logging.INFO, handlers=[_UILogHandler()])
log = logging.getLogger(__name__)

CHUNK = 65_536  # bytes per read from upstream
HOST_CHECK_TTL = 120.0  # seconds
_host_check_cache: dict[tuple[str, int], tuple[float, bool]] = {}


# ── State ─────────────────────────────────────────────────────────────────────

class State:
    current_id: int | None = None
    streams: dict[int, dict] = {}   # id → {"name": str, "url": str}
    config: dict = {}
    broadcaster: "Broadcaster | None" = None
    stream_path: str = "/stream-changeme"
    playlist_path: str = "/playlist-changeme.m3u"
    status_path: str = "/status-changeme"
    control_path: str = "/control-changeme"
    switch_path: str = "/api/switch-changeme"
    proxy_secret: str = ""

state = State()


# ── Broadcaster ───────────────────────────────────────────────────────────────

class Broadcaster:
    """
    Maintains exactly ONE upstream MPEG-TS connection and fans out chunks to
    all subscribed client queues.  Connects lazily on first viewer, disconnects
    when the last viewer leaves.  On stream switch, keeps client connections
    alive so the player just sees a brief glitch instead of a full reconnect.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._url: str | None = None
        self._pending_cancels: int = 0  # tasks cancelled for a switch (suppress EOF)

    def set_url(self, url: str) -> None:
        """Change the active URL. Keeps existing client connections open during handover."""
        self._url = url
        if self._task and not self._task.done():
            self._pending_cancels += 1
            self._task.cancel()
            # New task is started by the old task's finally block, avoiding overlap
        elif self._queues:
            self._task = asyncio.create_task(self._run(url))

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._queues.add(q)
        if self._url and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(self._url))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)
        if not self._queues and self._task and not self._task.done():
            log.info("No viewers left — closing upstream connection")
            self._task.cancel()
            self._task = None

    def stop(self) -> None:
        """Force-close current upstream connection."""
        self._url = None
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None

    async def _run(self, url: str, delay: float = 0.0) -> None:
        if delay:
            await asyncio.sleep(delay)
        timeout = aiohttp.ClientTimeout(connect=10, sock_read=30)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as upstream:
                    if upstream.status >= 400:
                        log.warning("Upstream returned %d for %s", upstream.status, url)
                        return
                    log.info("Broadcaster connected to %s", url)
                    async for chunk in upstream.content.iter_chunked(CHUNK):
                        for q in list(self._queues):
                            try:
                                q.put_nowait(chunk)
                            except asyncio.QueueFull:
                                pass  # slow client; drop chunk rather than block
        except asyncio.CancelledError:
            pass
        except aiohttp.ClientError as e:
            log.error("Upstream error: %s", e)
        finally:
            if self._pending_cancels > 0:
                self._pending_cancels -= 1
                # Start the new stream after a brief pause so the old TCP
                # connection is fully gone before the source sees a new one
                if self._queues and self._url:
                    self._task = asyncio.create_task(self._run(self._url, delay=0.5))
            else:
                # Stream ended or last viewer left — signal clients to stop
                for q in list(self._queues):
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> None:
    with open(path) as f:
        state.config = yaml.safe_load(f) or {}

    state.streams = {}

    if m3u_path := state.config.get("m3u_file"):
        state.streams = _parse_m3u(m3u_path)
    else:
        for k, v in state.config.get("streams", {}).items():
            state.streams[int(k)] = {"name": v["name"], "url": v["url"]}

    max_streams = int(state.config.get("server", {}).get("max_streams", 30))
    if len(state.streams) > max_streams:
        keys = sorted(state.streams.keys())[:max_streams]
        state.streams = {k: state.streams[k] for k in keys}

    log.info("Loaded %d stream(s)", len(state.streams))


def _parse_m3u(filepath: str) -> dict[int, dict]:
    """Parse a standard M3U/M3U8 playlist file into numbered streams."""
    result: dict[int, dict] = {}
    with open(filepath) as f:
        lines = f.read().splitlines()

    idx = 1
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            m = re.search(r",(.+)$", line)
            name = m.group(1).strip() if m else f"Channel {idx}"
            i += 1
            while i < len(lines):
                url = lines[i].strip()
                if url and not url.startswith("#"):
                    result[idx] = {"name": name, "url": url}
                    idx += 1
                    break
                i += 1
        i += 1

    return result


# ── Handlers ──────────────────────────────────────────────────────────────────

async def handle_stream(request: web.Request) -> web.StreamResponse:
    sid = state.current_id
    if sid is None or sid not in state.streams:
        return web.Response(status=503, text="No stream selected\n")

    info = state.streams[sid]
    url = info["url"]
    log.info("Client %s → [%d] %s", request.remote, sid, info["name"])

    # HLS sub-playlist / segment passthrough (used when rewriting m3u8 URLs)
    if proxied_url := request.query.get("__url"):
        real_url = proxied_url
        if not await _is_safe_proxy_target(real_url, allow_private_hosts=_private_proxy_hosts()):
            return web.Response(status=400, text="Unsupported target URL\n")
        if not _validate_proxy_signature(real_url, request.query.get("__sig", "")):
            return web.Response(status=403, text="Invalid proxy signature\n")
        return await _proxy_hls_target(request, real_url)

    # Dispatch based on stream URL type
    if ".m3u8" in url:
        return await _proxy_hls_target(request, url)
    return await _proxy_direct(request)


async def handle_playlist(request: web.Request) -> web.Response:
    """Serve a single-entry M3U playlist pointing to /stream."""
    sid = state.current_id
    name = state.streams[sid]["name"] if sid is not None and sid in state.streams else "IPTV Relay"
    safe_name = name.replace("\r", " ").replace("\n", " ")
    stream_url = f"{_request_base_url(request)}{state.stream_path}"

    playlist = (
        "#EXTM3U\n"
        f"#EXTINF:-1,{safe_name}\n"
        f"{stream_url}\n"
    )
    return web.Response(text=playlist, content_type="audio/x-mpegurl")


async def handle_status(request: web.Request) -> web.Response:
    cur = state.streams.get(state.current_id, {}).get("name", "none")
    lines = [f"Active stream: {cur}", "", "Available streams:"]
    for k, v in sorted(state.streams.items()):
        mark = "  ◄ LIVE" if k == state.current_id else ""
        lines.append(f"  {k}.  {v['name']}{mark}")
    return web.Response(text="\n".join(lines) + "\n")


async def handle_control(request: web.Request) -> web.Response:
    """Web UI: stream selector for colleagues."""
    cur = state.current_id
    stream_items = ""
    for k, v in sorted(state.streams.items()):
        active = " active" if k == cur else ""
        safe_name = html.escape(v["name"], quote=False)
        stream_items += (
            f'<li class="{active}">'
            f'<form method="post" action="{state.switch_path}">'
            f'<input type="hidden" name="id" value="{k}">'
            f'<button type="submit">{k}. {safe_name}</button>'
            f"</form></li>\n"
        )
    cur_name = (
        html.escape(state.streams[cur]["name"], quote=False)
        if cur is not None and cur in state.streams
        else "—"
    )
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>IPTV Relay</title>
<style>
  body {{ font-family: sans-serif; max-width: 480px; margin: 2rem auto; padding: 0 1rem; }}
  h1 {{ font-size: 1.3rem; }}
  p.now {{ color: #555; margin-bottom: 1.5rem; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ margin: .4rem 0; }}
  button {{ width: 100%; padding: .6rem 1rem; font-size: 1rem;
            -webkit-appearance: none; appearance: none;
            background: #f0f0f0; color: #000; border: 1px solid #ccc; border-radius: 6px; cursor: pointer; text-align: left; }}
  li.active button {{ background: #2563eb; color: #fff; border-color: #2563eb; font-weight: bold; }}
  button:hover {{ opacity: .85; }}
</style>
</head>
<body>
<h1>IPTV Relay</h1>
<p class="now">Nu actief: <strong>{cur_name}</strong></p>
<ul>
{stream_items}</ul>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_api_switch(request: web.Request) -> web.Response:
    """POST /api/switch  —  body: form field 'id'."""
    try:
        data = await request.post()
        sid = int(data["id"])
    except (KeyError, ValueError):
        return web.Response(status=400, text="Missing or invalid 'id'\n")

    if sid not in state.streams:
        return web.Response(status=404, text=f"Stream {sid} not found\n")

    state.current_id = sid
    log.info("Web: switched to [%d] %s", sid, state.streams[sid]["name"])
    info = state.streams[sid]
    if state.broadcaster and ".m3u8" not in info["url"]:
        state.broadcaster.set_url(info["url"])
    elif state.broadcaster:
        state.broadcaster.stop()

    raise web.HTTPFound(state.control_path)


# ── Stream Proxying ───────────────────────────────────────────────────────────

async def _proxy_direct(request: web.Request) -> web.StreamResponse:
    """Forward MPEG-TS chunks from the shared broadcaster to this client."""
    resp = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
    await resp.prepare(request)

    if state.broadcaster is None:
        return resp

    q = state.broadcaster.subscribe()
    try:
        while True:
            chunk = await q.get()
            if chunk is None:
                break  # stream ended or channel switched
            await resp.write(chunk)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        state.broadcaster.unsubscribe(q)

    return resp


async def _proxy_hls_target(request: web.Request, url: str) -> web.Response:
    """Fetch HLS target URL: rewrite playlists, pass through media assets."""
    base = urljoin(url, "./")
    server_base = f"{_request_base_url(request)}{state.stream_path}"
    timeout = aiohttp.ClientTimeout(connect=10, sock_read=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as upstream:
                status = upstream.status
                body = await upstream.read()
                content_type = upstream.headers.get("Content-Type", "")
                cache_control = upstream.headers.get("Cache-Control")
                etag = upstream.headers.get("ETag")
                charset = upstream.charset or "utf-8"
    except aiohttp.ClientError as e:
        log.error("Failed to fetch HLS target %s: %s", url, e)
        return web.Response(status=502, text="Could not reach upstream\n")

    if status >= 400:
        return web.Response(status=502, text=f"Upstream returned {status}\n")

    if _is_hls_playlist(url, content_type, body):
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        rewritten = _rewrite_m3u8(text, base, server_base)
        return web.Response(text=rewritten, content_type="application/vnd.apple.mpegurl")

    headers = {}
    if content_type:
        headers["Content-Type"] = content_type
    if cache_control:
        headers["Cache-Control"] = cache_control
    if etag:
        headers["ETag"] = etag
    return web.Response(status=status, body=body, headers=headers)


def _rewrite_m3u8(content: str, base_url: str, server_base: str) -> str:
    """Replace segment/sub-playlist URLs with proxied versions."""
    out = []
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("#"):
            line = re.sub(
                r'URI="([^"]+)"',
                lambda m: _rewrite_uri_attr(m, base_url, server_base),
                line,
            )
        elif s:
            full = _resolve_hls_ref(s, base_url)
            line = _build_proxy_url(full, server_base)
        out.append(line)
    return "\n".join(out)


def _rewrite_uri_attr(match: re.Match[str], base_url: str, server_base: str) -> str:
    raw = match.group(1).strip()
    if raw.startswith("data:"):
        return match.group(0)
    full = _resolve_hls_ref(raw, base_url)
    return f'URI="{_build_proxy_url(full, server_base)}"'


def _resolve_hls_ref(ref: str, base_url: str) -> str:
    if ref.startswith(("http://", "https://")):
        return ref
    return urljoin(base_url, ref)


def _proxy_signature(url: str) -> str:
    if not state.proxy_secret:
        return ""
    return hmac.new(
        state.proxy_secret.encode("utf-8"),
        url.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_proxy_url(url: str, server_base: str) -> str:
    sig = _proxy_signature(url)
    return f"{server_base}?__url={quote(url, safe='')}&__sig={sig}"


def _validate_proxy_signature(url: str, signature: str) -> bool:
    expected = _proxy_signature(url)
    return bool(signature and expected) and hmac.compare_digest(signature, expected)


def _private_proxy_hosts() -> set[str]:
    hosts: set[str] = set()

    sid = state.current_id
    if sid is not None and sid in state.streams:
        stream_url = state.streams[sid]["url"]
        try:
            parsed = urlsplit(stream_url)
            if parsed.hostname:
                hosts.add(parsed.hostname.strip().lower())
        except ValueError:
            pass

    extra = state.config.get("server", {}).get("proxy_allow_private_hosts", [])
    if isinstance(extra, list):
        for host in extra:
            if isinstance(host, str) and host.strip():
                hosts.add(host.strip().lower())
    return hosts


async def _is_safe_proxy_target(url: str, allow_private_hosts: set[str] | None = None) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https") or not parts.netloc or not parts.hostname:
        return False
    host = parts.hostname.strip().lower()
    if allow_private_hosts and host in allow_private_hosts:
        return True
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return await _host_is_public(host, port)


def _public_base_url() -> str:
    srv = state.config.get("server", {})
    if raw := srv.get("public_base_url"):
        return str(raw).rstrip("/")

    scheme = str(srv.get("public_scheme", "http")).lower()
    host = str(srv.get("public_ip") or srv.get("public_host") or srv.get("host", "127.0.0.1"))
    host = _host_for_generated_urls(host)
    port = int(srv.get("public_port", srv.get("port", 8080)))
    host_for_url = f"[{host}]" if ":" in host and not host.startswith("[") else host

    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host_for_url}"
    return f"{scheme}://{host_for_url}:{port}"


def _request_base_url(request: web.Request) -> str:
    srv = state.config.get("server", {})
    if raw := srv.get("public_base_url"):
        return str(raw).rstrip("/")

    # Use the exact address the client requested, unless it's a wildcard bind host.
    try:
        parsed = urlsplit(f"{request.scheme}://{request.host}")
    except ValueError:
        return _public_base_url()

    req_host = parsed.hostname
    if not req_host:
        return _public_base_url()
    req_host = _host_for_generated_urls(req_host)

    req_port = parsed.port
    if req_port is None:
        req_port = 443 if request.scheme == "https" else 80
    host_for_url = f"[{req_host}]" if ":" in req_host and not req_host.startswith("[") else req_host

    if (request.scheme == "http" and req_port == 80) or (request.scheme == "https" and req_port == 443):
        return f"{request.scheme}://{host_for_url}"
    return f"{request.scheme}://{host_for_url}:{req_port}"


def _host_for_generated_urls(host: str) -> str:
    normalized = host.strip().strip("[]")
    if normalized not in {"", "0.0.0.0", "::"}:
        return normalized

    # Bind-all addresses are not valid for clients; prefer a reachable local IP.
    if detected := _detect_local_ip():
        return detected
    return "127.0.0.1"


def _detect_local_ip() -> str | None:
    targets = [("8.8.8.8", 80), ("1.1.1.1", 80)]
    for target in targets:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(target)
                ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue

    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None

    # Prefer non-loopback IPv4 as most IPTV clients expect this.
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv4Address) and not addr.is_loopback:
            return ip

    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if isinstance(addr, ipaddress.IPv6Address) and not addr.is_loopback:
            return ip

    return None


async def _host_is_public(host: str, port: int) -> bool:
    cache_key = (host, port)
    now = time.monotonic()
    if cached := _host_check_cache.get(cache_key):
        ts, safe = cached
        if now - ts < HOST_CHECK_TTL:
            return safe

    safe = await asyncio.to_thread(_resolve_host_public, host, port)
    _host_check_cache[cache_key] = (now, safe)
    return safe


def _resolve_host_public(host: str, port: int) -> bool:
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return False

    # Literal IP host.
    try:
        addr = ipaddress.ip_address(host)
        return addr.is_global
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror:
        return False

    resolved_ips = {info[4][0] for info in infos if info and info[4]}
    if not resolved_ips:
        return False

    for ip in resolved_ips:
        try:
            if not ipaddress.ip_address(ip).is_global:
                return False
        except ValueError:
            return False
    return True


def _is_hls_playlist(url: str, content_type: str, body: bytes) -> bool:
    path = urlsplit(url).path.lower()
    if path.endswith(".m3u8"):
        return True

    ct = content_type.lower()
    if "mpegurl" in ct:
        return True

    return body.lstrip().startswith(b"#EXTM3U")


# ── CLI ───────────────────────────────────────────────────────────────────────

_CLEAR = "\033[2J\033[H"
_BOLD  = "\033[1m"
_DIM   = "\033[2m"
_RED   = "\033[31m"
_GREEN = "\033[32m"
_RST   = "\033[0m"

BOX_W = 46


def _box(lines: list[str]) -> str:
    inner = BOX_W - 2
    top    = "╔" + "═" * inner + "╗"
    bottom = "╚" + "═" * inner + "╝"
    sep    = "╠" + "═" * inner + "╣"
    rows = [top]
    for line in lines:
        rows.append("║" + line.ljust(inner) + "║")
    rows.append(bottom)
    return "\n".join(rows)


def _print_ui() -> None:
    srv = state.config.get("server", {})
    port = srv.get("port", 8080)
    share_url = f"{_public_base_url()}{state.playlist_path}"
    control_url = f"{_public_base_url()}{state.control_path}"
    header = [
        f"  {_BOLD}IPTV Relay{_RST}".center(BOX_W + 6),
        f"  port {port}".center(BOX_W - 2),
        "",
        f"  {_DIM}Playlist:{_RST}  {share_url}",
        f"  {_DIM}Control: {_RST}  {_BOLD}{control_url}{_RST}",
        "",
    ]
    stream_lines: list[str] = []
    for k, v in sorted(state.streams.items()):
        if k == state.current_id:
            mark = f" {_GREEN}◄ LIVE{_RST}"
            prefix = f"  {_BOLD}[{k}]{_RST} "
        else:
            mark = ""
            prefix = f"  [{k}] "
        stream_lines.append(f"{prefix}{v['name']}{mark}")

    footer = [
        "",
        f"  {_DIM}[number + Enter] select   [r] reload   [q] quit{_RST}",
    ]

    print(_CLEAR, end="", flush=True)
    for line in header:
        print(line, end="\r\n")
    for line in stream_lines:
        print(line, end="\r\n")
    if _log_lines:
        print(f"\r\n  {_DIM}── log ─────────────────────────────────────{_RST}", end="\r\n")
        for line in _log_lines:
            print(f"  {_DIM}{line[:70]}{_RST}", end="\r\n")
    for line in footer:
        print(line, end="\r\n")
    print(end="\r\n", flush=True)


async def _keyboard_loop(config_path: str) -> None:
    import termios
    import tty

    loop = asyncio.get_event_loop()
    _print_ui()

    global _in_keyboard_loop
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    buf = ""
    try:
        tty.setraw(fd)
        _in_keyboard_loop = True
        while True:
            ch = await loop.run_in_executor(None, lambda: sys.stdin.read(1))

            if ch == "q":
                print("\r\nBye.\r\n")
                break

            elif ch == "r":
                buf = ""
                old_token = state.config.get("server", {}).get("token", "changeme")
                load_config(config_path)
                new_token = state.config.get("server", {}).get("token", "changeme")
                if new_token != old_token:
                    log.warning("Server token changed in config; restart required to apply new URL paths")

                if not state.streams:
                    state.current_id = None
                elif state.current_id not in state.streams:
                    state.current_id = min(state.streams)

                if state.broadcaster and state.current_id is None:
                    state.broadcaster.stop()
                elif state.broadcaster and state.current_id is not None:
                    info = state.streams[state.current_id]
                    if ".m3u8" not in info["url"]:
                        state.broadcaster.set_url(info["url"])
                    else:
                        state.broadcaster.stop()
                _print_ui()
                print("Config reloaded.", end="\r\n", flush=True)

            elif ch.isdigit():
                buf += ch
                # Show what the user is typing
                print(f"\r  > {buf} ", end="", flush=True)

            elif ch in ("\r", "\n"):
                if buf:
                    sid = int(buf)
                    buf = ""
                    if sid in state.streams:
                        state.current_id = sid
                        log.info("Switched to [%d] %s", sid, state.streams[sid]["name"])
                        info = state.streams[sid]
                        if state.broadcaster and ".m3u8" not in info["url"]:
                            state.broadcaster.set_url(info["url"])
                        elif state.broadcaster:
                            state.broadcaster.stop()
                        _print_ui()
                    else:
                        _print_ui()
                        print(f"  No stream {sid}", end="\r\n", flush=True)

            elif ch in ("\x7f", "\x08"):  # backspace
                buf = buf[:-1]
                print(f"\r  > {buf}  \r  > {buf}", end="", flush=True)

            elif ch == "\x03":  # Ctrl-C
                break
    finally:
        _in_keyboard_loop = False
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# ── Entry Point ───────────────────────────────────────────────────────────────

async def _run(config_path: str, m3u_override: str | None = None) -> None:
    load_config(config_path)
    if m3u_override:
        state.streams = _parse_m3u(m3u_override)
        log.info("Using M3U: %s (%d streams)", m3u_override, len(state.streams))

    state.broadcaster = Broadcaster()
    if state.streams:
        state.current_id = min(state.streams)
        info = state.streams[state.current_id]
        if ".m3u8" not in info["url"]:
            state.broadcaster.set_url(info["url"])

    srv = state.config["server"]
    host = srv.get("host", "0.0.0.0")
    port = int(srv.get("port", 8080))
    token = srv.get("token", "changeme")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token):
        raise ValueError(
            "server.token must be 8-128 chars and contain only A-Z, a-z, 0-9, '_' or '-'"
        )
    state.stream_path = f"/stream-{token}"
    state.playlist_path = f"/playlist-{token}.m3u"
    state.status_path = f"/status-{token}"
    state.control_path = f"/control-{token}"
    state.switch_path = f"/api/switch-{token}"
    state.proxy_secret = secrets.token_hex(32)

    app = web.Application()
    app.router.add_get(state.stream_path,    handle_stream)
    app.router.add_get(state.playlist_path,  handle_playlist)
    app.router.add_get(state.status_path,    handle_status)
    app.router.add_get(state.control_path,   handle_control)
    app.router.add_post(state.switch_path,   handle_api_switch)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()

    base = f"http://{host}:{port}"
    log.info("Listening on http://%s:%d", host, port)
    log.info("Playlist:  %s%s", base, state.playlist_path)
    log.info("Control:   %s%s", base, state.control_path)
    log.info("(only share the Control URL with others you trust to switch streams)")

    await _keyboard_loop(config_path)
    await runner.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="IPTV Relay Server")
    parser.add_argument(
        "--config", default="config.yaml", metavar="FILE",
        help="Path to config file (default: config.yaml)",
    )
    parser.add_argument(
        "m3u", nargs="?", metavar="FILE.m3u",
        help="M3U playlist to use directly (overrides config m3u_file)",
    )
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.config, m3u_override=args.m3u))
    except ValueError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
