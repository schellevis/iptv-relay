# IPTV Relay

A lightweight IPTV relay server that forwards one of several configured streams to others via HTTP. You switch the active stream live via a CLI or web UI.

## How it works

```
upstream source  →  relay (your machine)  →  others (VLC / Kodi / IPTV app)
```

- One upstream connection per active stream, fanned out to all viewers
- Supports MPEG-TS (direct byte forwarding) and HLS (`.m3u8`, playlist rewrite)
- Stream switching via keyboard or web UI without clients reconnecting

## Installation

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Edit config.yaml: token, port, and stream URLs
python relay.py
```

Or with an alternative config file:

```bash
python relay.py --config /path/to/config.yaml
```

Or pass a .m3u file directly (overrides `m3u_file` from the config):

```bash
python relay.py channels.m3u
```

## Configuration

Copy `config.yaml.example` to `config.yaml` (listed in `.gitignore`):

| Key           | Description                                              |
|---------------|----------------------------------------------------------|
| `server.host` | Listen address (`0.0.0.0` = all interfaces)              |
| `server.port` | Port number                                              |
| `server.token`| Random string — embedded in the playlist URL             |
| `server.public_base_url` | Optional externally shared base URL (e.g. `https://tv.example.com`) |
| `server.proxy_allow_private_hosts` | Optional list of private hosts allowed for HLS segment proxying |
| `streams`     | Numbered streams (name + URL)                            |
| `m3u_file`    | Alternative: load streams from a .m3u file               |

If `server.public_base_url` is not set and `server.host` is `0.0.0.0`/`::`, the relay auto-detects a local IP for generated links.

## CLI controls

| Key / input      | Action                         |
|------------------|--------------------------------|
| `1`–`9` + Enter  | Switch to stream N             |
| `r`              | Reload config.yaml             |
| `q`              | Stop the server                |
| Ctrl-C           | Stop the server                |

## Endpoints

All URLs contain the token from the config as security.

| URL                          | Description                                      |
|------------------------------|--------------------------------------------------|
| `/stream-{token}`            | Active stream (proxied; works in VLC, Kodi)      |
| `/playlist-{token}.m3u`      | M3U playlist to share with others                |
| `/control-{token}`           | Web UI: switch streams via browser               |
| `/status-{token}`            | Plaintext overview of streams                    |

### Sharing with others

Give others the playlist URL (shown in the terminal on startup):

```
http://YOUR_IP:2205/playlist-{token}.m3u
```

Or the web UI for self-service switching:

```
http://YOUR_IP:2205/control-{token}
```

## Stream formats

| Format         | How it works                                                       |
|----------------|--------------------------------------------------------------------|
| MPEG-TS (HTTP) | Bytes are forwarded directly via a shared broadcaster              |
| HLS (`.m3u8`)  | Playlist is rewritten so segments also flow through the relay      |

## File overview

| File                 | Purpose                                                   |
|----------------------|-----------------------------------------------------------|
| `relay.py`           | HTTP server + CLI stream selector                         |
| `config.yaml.example`| Configuration template (copy to `config.yaml`)            |
| `config.yaml`        | Your local config with real URLs/token (in `.gitignore`)  |
| `requirements.txt`   | Python dependencies (`aiohttp`, `pyyaml`)                 |
