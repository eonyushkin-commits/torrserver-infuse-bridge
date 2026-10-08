[Русский](README.md) | [English](README.en.md)

# 🎬 TorrServer to Infuse Bridge

Stream torrents in **Infuse** (Apple TV, iOS, macOS) without downloading them to disk. The project exposes your **TorrServer** torrents to Infuse as a regular WebDAV library: add a torrent, and within half a minute the movie or show appears in Infuse with a poster and description.

## ✨ Features

- **Virtual library.** Nothing is written to disk: the `.strm` tree is built on the fly from TorrServer's current state. Remove a torrent and it's gone from the library — nothing to clean up.
- **Folders Infuse understands.** Names are parsed with [guessit](https://github.com/guessit-io/guessit): `Movies/Title (Year)/…`, `TV/Show/Season 01/Show S01E05.strm`. Multi-episode files, multi-part movies (CD1/CD2) and Cyrillic titles are handled; samples are skipped.
- **Your password never ends up in links.** Streams are opened with a separate random token (`/s/<token>/stream/…`); the main password only protects WebDAV and the TorrServer web UI. Rotating the token is a single command.
- **HTTPS out of the box.** With a domain, the Caddy gateway obtains a Let's Encrypt certificate automatically — the password and the streams themselves are encrypted.
- **No git on the server.** The installer downloads three config files and prebuilt images; updating means switching the image version. A non-interactive mode is available for Ansible or cloud-init.

## ⚙️ How it works

| Service | Container | Purpose |
|---|---|---|
| **TorrServer** | `torrserver` | Torrents and HTTP streaming (not exposed directly) |
| **Bridge** | `infuse-bridge` | Builds the virtual library from TorrServer and serves it over WebDAV |
| **Gateway (Caddy)** | `infuse-gateway` | Basic Auth, token-protected streams, HTTPS |

1. You add a torrent to TorrServer.
2. Every 30 seconds the bridge polls TorrServer and fetches file lists **only for new** torrents.
3. Infuse reads the library over WebDAV through the gateway (password-protected).
4. On playback Infuse opens the link from the `.strm` file: the gateway checks the token and proxies the stream from TorrServer.

## 📋 Requirements

- A server (VPS or home) with Docker, **Docker Compose v2** (the `docker compose` command), and `curl`.
- The installer must run as `root` or via `sudo`.
- For HTTPS: a domain with an A record pointing to the server, plus open port 80 and the chosen TorrServer/WebDAV ports.

## 🚀 Installation

```bash
curl -fsSL https://raw.githubusercontent.com/eonyushkin-commits/torrserver-infuse-bridge/main/install.sh | sudo bash
```

The script asks a few questions, creates `/opt/torrserver-infuse-bridge` with `.env`, `docker-compose.yml`, `docker-compose.tls.yml`, and `Caddyfile`, pulls the images, and starts the containers.

### Parameters

| Parameter | Default | Description |
|---|---|---|
| Domain | — | With a domain everything runs over HTTPS. Without one — plain HTTP over IPv4 |
| IPv4 address | auto-detected | Only needed without a domain |
| TorrServer port | `443` with a domain, otherwise random `10000–59999` | TorrServer web UI and streams for Infuse |
| WebDAV port | `8443` with a domain, otherwise `8080` | The port Infuse connects to |
| Login | `admin` | Latin letters, digits, `.`, `_`, `-` |
| Password | — | At least 8 characters, anything except `'` |

### Non-interactive

```bash
curl -fsSL https://raw.githubusercontent.com/eonyushkin-commits/torrserver-infuse-bridge/main/install.sh -o install.sh
sudo BRIDGE_PASSWORD='a-very-long-password' bash install.sh -y --domain media.example.com
```

All options: `bash install.sh --help` (`--ip`, `--ts-port`, `--webdav-port`, `--user`, `--password-file`, `--version`, `--dir`, `--reconfigure`, `--rotate-token`).

## 🍏 Connecting Infuse

1. Infuse → **Settings** → **Add Files** → **Other...**
2. Protocol: **WebDAV**.
3. **Address** — your domain or server IP (without `http://`), **Username** and **Password** — the values from the install.
4. Under **Advanced**, set the **Port** to the WebDAV port (default `8443` with a domain, `8080` without). If you installed with a domain, enable HTTPS.
5. Tap **Save** and add the `Movies` and `TV` folders to Favorites (⭐).

## 🔄 Updating and management

To update, re-run the installer: it downloads fresh config files and images and keeps your settings from `.env`.

```bash
curl -fsSL https://raw.githubusercontent.com/eonyushkin-commits/torrserver-infuse-bridge/main/install.sh | sudo bash
```

To pin a version, pass a release: `--version v2.0.0` (the image gets tag `2.0.0`). Without it, `main` and the `latest` image are used.

Run these from `/opt/torrserver-infuse-bridge`:

```bash
docker compose ps                  # container status
docker compose logs -f bridge      # library log
docker compose pull && docker compose up -d   # update images only
docker compose down                # stop
```

| Task | How |
|---|---|
| Change password, ports, domain | `sudo bash install.sh --reconfigure` |
| Issue a new stream token | `sudo bash install.sh --rotate-token` — library links update automatically |
| Edit `.env` by hand | Edit the file, then run `docker compose up -d` |

## 📁 Files on the server

```
/opt/torrserver-infuse-bridge
├── .env                    # settings (mode 600)
├── docker-compose.yml      # containers
├── docker-compose.tls.yml  # port 80 for Let's Encrypt (enabled when a domain is set)
├── Caddyfile               # gateway; values come from .env
├── caddy/                  # Caddy certificates and state
└── ts/                     # TorrServer data (config, database, cache)
```

## 🔐 Security

- TorrServer and the bridge are not exposed directly: everything goes through the gateway.
- WebDAV and the TorrServer web UI are protected by Basic Auth. The password hash is computed when the gateway starts.
- Stream links contain only the token, never the password. Anyone who learns a link can watch streams but gets no access to TorrServer or the library. If a link leaks, issue a new token (`--rotate-token`).
- **With a domain**, all traffic is HTTPS: password, token, and video.
- **Without a domain**, traffic is unencrypted. Restrict the ports with a firewall (ufw, Security Groups) or connect through a VPN (WireGuard).

## 🧳 Migrating from version 1

Version 1 (nginx proxy, a separate WebDAV server, and a parser writing `.strm` files to `strm_library`) is migrated automatically: just run the installer as for an update. It takes the login, password, IP, and ports from the old `.env`, removes the old containers, `nginx.conf`, and `.htpasswd`. Afterwards:

- rescan the source in Infuse: movies and shows now live in `Movies/` and `TV/`;
- you can delete `strm_library/` and the old repository clone (`.git`, `README*`, `torr_to_strm.py`, etc.) in `/opt/torrserver-infuse-bridge`.

## 🩺 Troubleshooting

**The library shows up, but playback fails.** Links in `.strm` files point to `PUBLIC_URL` from `.env`. Make sure the TorrServer address and port are reachable from the Infuse device. After fixing it, run `docker compose up -d` — the library updates immediately.

**A new torrent doesn't appear.** The bridge waits until TorrServer has loaded the torrent's metadata and polls every 30 seconds. Watch `docker compose logs -f bridge`.

**The certificate isn't issued.** Check the domain's A record and that port 80 is open: `docker compose logs gateway`.

**Infuse can't connect to WebDAV.** Check the WebDAV port in your firewall and the login/password; with a domain, make sure HTTPS is enabled in Infuse.

## 🛠 Development

```bash
pip install -r requirements.txt ruff
ruff check .
python -m unittest discover -s tests -t .
```

Release: a `vX.Y.Z` git tag makes CI publish `ghcr.io/eonyushkin-commits/torrserver-infuse-bridge:X.Y.Z`.

## 📄 License

The project is distributed under the license in the [LICENSE](LICENSE) file.
