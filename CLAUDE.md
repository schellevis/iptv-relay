# IPTV Relay

Een lichtgewicht IPTV relay server die één van meerdere geconfigureerde streams doorstuurt naar collega's via HTTP met Basic Auth.

## Bestanden

| Bestand           | Doel                                               |
|-------------------|----------------------------------------------------|
| `relay.py`        | Hoofdscript — HTTP server + CLI stream-selector    |
| `config.yaml`     | Configuratie: streams, poort, gebruikersnaam/ww    |
| `requirements.txt`| Python dependencies (`aiohttp`, `pyyaml`)          |

## Starten

```bash
pip install -r requirements.txt
python relay.py
# of met een alternatief config-bestand:
python relay.py --config /pad/naar/config.yaml
```

## CLI bediening

| Toets | Actie                        |
|-------|------------------------------|
| `1-9` | Switch naar stream nummer N  |
| `r`   | Herlaad config.yaml          |
| `q`   | Stop de server               |

## Endpoints

| URL                  | Beschrijving                                              |
|----------------------|-----------------------------------------------------------|
| `/stream`            | De actieve stream (proxied); werkt in VLC, Kodi, etc.    |
| `/playlist.m3u`      | M3U playlist om te delen met collega's                    |
| `/status`            | Plaintext overzicht van actieve stream + beschikbare kanalen |

Alle endpoints vereisen Basic Auth (zie `config.yaml`).

## config.yaml

```yaml
server:
  host:     "0.0.0.0"   # 0.0.0.0 = bereikbaar op het LAN
  port:     2205
  username: "iptv"
  password: "changeme"  # verander dit!

# Optie A: streams direct opgeven (toets 1-9 selecteert)
streams:
  1:
    name: "NPO 1"
    url:  "http://..."
  2:
    name: "NPO 2"
    url:  "http://..."

# Optie B: laad uit een .m3u bestand (overschrijft streams hierboven)
# m3u_file: "channels.m3u"
```

## Streams delen met collega's

Geef collega's deze URL (werkt in VLC, Kodi, Jellyfin, of een IPTV-app):

```
http://iptv:changeme@JOUW_IP:2205/playlist.m3u
```

Of de directe stream:

```
http://iptv:changeme@JOUW_IP:2205/stream
```

Collega's zien altijd de stream die jij op dat moment hebt geselecteerd via de CLI.

## Streamformaten

| Formaat        | Werking                                                        |
|----------------|----------------------------------------------------------------|
| MPEG-TS (HTTP) | Bytes worden direct doorgestuurd                               |
| HLS (`.m3u8`)  | Playlist wordt herschreven zodat segmenten ook via de relay lopen |

## Uitbreiden

- **Meer dan 9 streams via toetsenbord**: voeg arrow-key navigatie toe in `_keyboard_loop()`
- **HTTPS**: zet een nginx reverse proxy met TLS voor de relay
- **Meerdere gelijktijdige streams**: elk endpoint een eigen actieve stream — nu is er één gedeelde `state.current_id`
