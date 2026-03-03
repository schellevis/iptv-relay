# IPTV Relay

Een lichtgewicht IPTV relay server die één van meerdere geconfigureerde streams doorstuurt naar collega's via HTTP. De actieve stream wissel je live via een CLI of een web-UI.

## Werking

```
upstream bron  →  relay (jouw machine)  →  collega's (VLC / Kodi / IPTV-app)
```

- Eén upstream verbinding per actieve stream, fans uit naar alle kijkers
- Ondersteunt MPEG-TS (directe byte-doorstuur) en HLS (`.m3u8`, playlist-rewrite)
- Stream-wissel via toetsenbord of web-UI zonder dat clients opnieuw verbinden

## Installatie

```bash
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Pas config.yaml aan: token, poort en stream-URLs
python relay.py
```

Of met een alternatief config-bestand:

```bash
python relay.py --config /pad/naar/config.yaml
```

Of direct een .m3u meegeven (overschrijft `m3u_file` uit de config):

```bash
python relay.py kanalen.m3u
```

## Configuratie

Kopieer `config.yaml.example` naar `config.yaml` (staat in `.gitignore`):

| Sleutel       | Beschrijving                                          |
|---------------|-------------------------------------------------------|
| `server.host` | Luisteradres (`0.0.0.0` = alle interfaces)            |
| `server.port` | Poortnummer                                           |
| `server.token`| Willekeurige string — zit verwerkt in de playlist-URL |
| `streams`     | Genummerde streams (naam + URL)                       |
| `m3u_file`    | Alternatief: laad streams uit een .m3u bestand        |

## CLI bediening

| Toets / invoer   | Actie                          |
|------------------|--------------------------------|
| `1`–`9` + Enter  | Switch naar stream N           |
| `r`              | Herlaad config.yaml            |
| `q`              | Stop de server                 |
| Ctrl-C           | Stop de server                 |

## Endpoints

Alle URLs bevatten de token uit de config als beveiliging.

| URL                          | Beschrijving                                   |
|------------------------------|------------------------------------------------|
| `/stream`                    | Actieve stream (proxied; werkt in VLC, Kodi)   |
| `/playlist-{token}.m3u`      | M3U playlist om te delen met collega's         |
| `/control-{token}`           | Web-UI: stream wisselen via browser            |
| `/status`                    | Plaintext overzicht van streams                |

### Delen met collega's

Geef collega's de playlist-URL (zichtbaar in de terminal bij het opstarten):

```
http://JOUW_IP:2205/playlist-{token}.m3u
```

Of de web-UI voor zelf wisselen:

```
http://JOUW_IP:2205/control-{token}
```

## Streamformaten

| Formaat        | Werking                                                        |
|----------------|----------------------------------------------------------------|
| MPEG-TS (HTTP) | Bytes worden direct doorgestuurd via een gedeelde broadcaster  |
| HLS (`.m3u8`)  | Playlist wordt herschreven zodat segmenten ook via de relay lopen |

## Bestandsoverzicht

| Bestand              | Doel                                                  |
|----------------------|-------------------------------------------------------|
| `relay.py`           | HTTP server + CLI stream-selector                     |
| `config.yaml.example`| Configuratie-sjabloon (kopieer naar `config.yaml`)    |
| `config.yaml`        | Jouw lokale config met echte URLs/token (in `.gitignore`) |
| `requirements.txt`   | Python dependencies (`aiohttp`, `pyyaml`)             |
