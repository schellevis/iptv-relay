#!/usr/bin/env python3
"""
IPTV Relay Server

Proxies one of several configured IPTV streams to colleagues via HTTP with
Basic Authentication.  Switch between streams live from the CLI by typing
the stream number and pressing Enter.

Usage:
    python relay.py [--config config.yaml]

Endpoints:
    /stream         → the currently active stream (proxied)
    /playlist.m3u   → shareable M3U playlist pointing to /stream
    /status         → plain-text status overview
"""

import argparse
import asyncio
import collections
import logging
import re
import sys
from urllib.parse import quote, unquote, urljoin

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


# ── State ─────────────────────────────────────────────────────────────────────

class State:
    current_id: int | None = None
    streams: dict[int, dict] = {}   # id → {"name": str, "url": str}
    config: dict = {}
    broadcaster: "Broadcaster | None" = None

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
        state.config = yaml.safe_load(f)

    state.streams = {}

    if m3u_path := state.config.get("m3u_file"):
        state.streams = _parse_m3u(m3u_path)
    else:
        for k, v in state.config.get("streams", {}).items():
            state.streams[int(k)] = {"name": v["name"], "url": v["url"]}

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
        real_url = unquote(proxied_url)
        if _looks_like_segment(real_url):
            return await _proxy_segment(real_url)
        return await _proxy_hls_playlist(request, real_url)

    # Dispatch based on stream URL type
    if ".m3u8" in url:
        return await _proxy_hls_playlist(request, url)
    return await _proxy_direct(request)


async def handle_playlist(request: web.Request) -> web.Response:
    """Serve a single-entry M3U playlist pointing to /stream."""
    sid = state.current_id
    name = state.streams[sid]["name"] if sid and sid in state.streams else "IPTV Relay"

    playlist = (
        "#EXTM3U\n"
        f"#EXTINF:-1,{name}\n"
        f"http://{request.host}/stream\n"
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
        stream_items += (
            f'<li class="{active}">'
            f'<form method="post" action="{state.config["_switch_path"]}">'
            f'<input type="hidden" name="id" value="{k}">'
            f'<button type="submit">{k}. {v["name"]}</button>'
            f"</form></li>\n"
        )
    cur_name = state.streams[cur]["name"] if cur and cur in state.streams else "—"
    html = f"""<!DOCTYPE html>
<html lang="nl">
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
            background: #f0f0f0; border: 1px solid #ccc; border-radius: 6px; cursor: pointer; text-align: left; }}
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
    if ".m3u8" not in info["url"]:
        state.broadcaster.set_url(info["url"])

    raise web.HTTPFound(request.headers.get("Referer", "/control"))


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


async def _proxy_hls_playlist(request: web.Request, url: str) -> web.Response:
    """Fetch an HLS M3U8 playlist and rewrite its URLs to route through us."""
    base = url.rsplit("/", 1)[0] + "/"
    server_base = f"http://{request.host}/stream"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as upstream:
                if upstream.status >= 400:
                    return web.Response(status=502, text="Upstream playlist error\n")
                text = await upstream.text()
    except aiohttp.ClientError as e:
        log.error("Failed to fetch playlist %s: %s", url, e)
        return web.Response(status=502, text="Could not reach upstream\n")

    rewritten = _rewrite_m3u8(text, base, server_base)
    return web.Response(text=rewritten, content_type="application/vnd.apple.mpegurl")


def _rewrite_m3u8(content: str, base_url: str, server_base: str) -> str:
    """Replace segment/sub-playlist URLs with proxied versions."""
    out = []
    for line in content.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            full = s if s.startswith("http") else urljoin(base_url, s)
            line = f"{server_base}?__url={quote(full, safe='')}"
        out.append(line)
    return "\n".join(out)


async def _proxy_segment(url: str) -> web.Response:
    """Fetch and return a single HLS segment."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as upstream:
                data = await upstream.read()
                ct = upstream.headers.get("Content-Type", "video/mp2t")
        return web.Response(body=data, content_type=ct)
    except aiohttp.ClientError as e:
        log.error("Segment fetch error: %s", e)
        return web.Response(status=502, text="Segment unavailable\n")


def _looks_like_segment(url: str) -> bool:
    path = url.split("?")[0].lower()
    return path.endswith((".ts", ".aac", ".mp4", ".fmp4", ".m4s"))


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
    token = srv.get("token", "changeme")
    share_url = f"http://94.142.244.36:{port}/playlist-{token}.m3u"

    control_url = f"http://94.142.244.36:{port}{state.config.get('_control_path', '/control')}"
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
                load_config(config_path)
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
                        if ".m3u8" not in info["url"]:
                            state.broadcaster.set_url(info["url"])
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
    playlist_path  = f"/playlist-{token}.m3u"
    control_path   = f"/control-{token}"
    switch_path    = f"/api/switch-{token}"

    # Store paths so handlers and _print_ui can use them
    state.config["_switch_path"]  = switch_path
    state.config["_control_path"] = control_path

    app = web.Application()
    app.router.add_get("/stream",      handle_stream)
    app.router.add_get(playlist_path,  handle_playlist)
    app.router.add_get("/status",      handle_status)
    app.router.add_get(control_path,   handle_control)
    app.router.add_post(switch_path,   handle_api_switch)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()

    base = f"http://94.142.244.36:{port}"
    log.info("Listening on http://%s:%d", host, port)
    log.info("Playlist:  %s%s", base, playlist_path)
    log.info("Control:   %s%s", base, control_path)
    log.info("(deel de Control-URL alleen met collega's die mogen wisselen)")

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
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
