[Русский](README.md) | [English](README.en.md)

# 🎬 TorrServer to Infuse Bridge

Stream torrents in **Infuse** (Apple TV, iOS, macOS) without downloading them to disk. The project connects **TorrServer** and Infuse via auto-generated `.strm` files and WebDAV: add a torrent to TorrServer, and a minute later the movie shows up in your Infuse library with a poster and description.

## ✨ Features

- Fully containerized: the host only needs Docker, git, and curl.
- One-script install and update via `install.sh` — no manual Git or Compose work.
- Prebuilt `parser` image from [GHCR](https://ghcr.io/eonyushkin-commits/torrserver-infuse-bridge) — no building on the server.
- Smart file naming via [guessit](https://github.com/guessit-io/guessit): Infuse groups TV shows by season out of the box, quality tags (`1080p`, `WEB-DL`) are ignored, Cyrillic titles are supported.
- Atomic `.strm` writes (temp file + `os.replace()`) — Infuse never sees an empty or half-written file.
- WebDAV and TorrServer are protected with HTTP Basic Auth; TorrServer is never exposed directly.
- Code updates never touch your data in `./ts` and `./strm_library`.

## ⚙️ How it works

A stack of four containers:

| Service | Container | Purpose |
|---|---|---|
| **TorrServer** | `torrserver` | Torrent source and HTTP streaming |
| **Nginx proxy** | `torr-proxy` | Basic Auth in front of TorrServer; Infuse pulls the stream through it |
| **Parser** | `strm-parser` | Polls TorrServer and generates `.strm` files |
| **WebDAV** | `webdav-infuse` | Serves the `.strm` library to Infuse |

Data flow:

1. You add a torrent to TorrServer.
2. The parser picks it up on the next poll and creates `.strm` files in `./strm_library`.
3. WebDAV serves the library to Infuse.
4. Infuse indexes the files, fetches artwork, and plays the stream through the Nginx proxy.

## 📋 Requirements

- A server (VPS or home) with `git`, `docker`, `curl`, and Docker Compose v2 (or a compatible `docker-compose`).
- The install script must run as `root` or via `sudo`.

## 🚀 Quick start

```bash
curl -O https://raw.githubusercontent.com/eonyushkin-commits/torrserver-infuse-bridge/main/install.sh
chmod +x install.sh
sudo ./install.sh
```

The script clones the repository into `/opt/torrserver-infuse-bridge`, interactively asks for parameters, creates `.env`, `.htpasswd`, and `nginx.conf`, pulls the `parser` image from GHCR, and starts the containers.

### Install parameters

| Parameter | Default | Description |
|---|---|---|
| `WEBDAV_PORT` | `8080` | External WebDAV port |
| `WEBDAV_USER` | `admin` | Login for WebDAV and the TorrServer proxy |
| `WEBDAV_PASSWORD` | — | Password (shared by WebDAV and the proxy) |
| `HOST_IP` | — | External IP or domain of the server |
| `TORR_PORT` | random from `10000–60000` | External TorrServer port (proxy port) |

> ⚠️ **`HOST_IP` is the most important parameter.** This address is written inside the generated `.strm` files. If it's wrong (e.g. you leave `127.0.0.1` on a remote VPS), Infuse will load the library fine but playback will fail.

Values are stored in `.env`. There is also a `TORR_HOST` variable (default `torrserver`) — the internal TorrServer hostname on the Docker network; you only need to change it when running the `parser` outside compose or in a non-standard network.

## 🍏 Connecting Infuse

1. Infuse → **Settings** → **Add Files** → **Other...**
2. Protocol: **WebDAV**.
3. **Address** — your server's IP or domain (without `http://`), **Username** and **Password** — the values from the install.
4. Under **Advanced**, set the **Port** to your `WEBDAV_PORT` (default `8080`).
5. Tap **Save** and add the folder to Favorites (⭐).

Infuse will scan the library, fetch posters, and group TV shows by season. New torrents appear with a short delay — after the parser's next pass.

## 🔄 Updating

Updating is just re-running `install.sh`:

```bash
cd /opt/torrserver-infuse-bridge
sudo ./install.sh
```

The script stops the containers, updates the code (`git fetch --all && git reset --hard origin/<current branch>`), recreates `.env` and configs if needed, pulls the latest `parser` image, and brings the stack back up. The `./ts` and `./strm_library` directories are left untouched.

To update only the `parser` image without reinstalling:

```bash
cd /opt/torrserver-infuse-bridge
docker compose pull parser
docker compose up -d parser
```

> The TorrServer image is pinned to a specific version (`MatriX.141`) in `docker-compose.yml` — newer versions arrive with repository updates, not automatically.

## 🛠 Administration

Run all commands from `/opt/torrserver-infuse-bridge`:

```bash
docker compose ps              # container status
docker logs -f strm-parser     # parser logs
docker compose down            # stop
docker compose up -d           # start
```

Main URLs after installation (both behind Basic Auth):

- TorrServer UI: `http://HOST_IP:TORR_PORT`
- WebDAV: `http://HOST_IP:WEBDAV_PORT`

## 📁 Project layout

```
/opt/torrserver-infuse-bridge
├── install.sh          # install, update, reconfigure (idempotent)
├── .env                # environment parameters (mode 600, not in Git)
├── .htpasswd           # bcrypt password hash for Basic Auth (not in Git)
├── nginx.conf          # proxy config in front of TorrServer (generated)
├── docker-compose.yml  # container definitions
├── strm_library/       # .strm library served to Infuse
└── ts/                 # TorrServer data (config, database, cache)
```

The `torr_to_strm.py` script lives in the repository but runs only inside the `parser` container (the image is built in CI and published to GHCR).

## 🔐 Security

- `.env` is created with mode `600`; `.env` and `.htpasswd` are never committed to Git.
- TorrServer is not exposed directly: external access goes only through the Nginx proxy with Basic Auth.
- `.strm` files contain URLs with credentials (`http://user:password@host:port/...`), but are themselves only reachable via WebDAV protected by the same credentials.

**Be aware:** the stack runs over plain HTTP — both Basic Auth and the credentials embedded in stream URLs travel over the network unencrypted. For access over the internet, it is recommended to:

- put a TLS-terminating reverse proxy in front of the services (Caddy, Traefik, Nginx + Let's Encrypt), or
- restrict access to the ports with a firewall by source IP (ufw, Security Groups), or
- connect via a VPN such as WireGuard.

And in any case — use a strong password.

## 🩺 Troubleshooting

**The library shows up in Infuse, but playback fails.** Almost always a wrong `HOST_IP` in `.env` (the address inside the `.strm` files). Fix the value and re-run `install.sh` — the files will be regenerated.

**A new torrent doesn't appear in Infuse.** Check the parser logs: `docker logs -f strm-parser`. Make sure the `torrserver` container is healthy (`docker compose ps`).

**Infuse can't connect to WebDAV.** Verify that `WEBDAV_PORT` is open in your firewall and that the login/password match the values from the install.

## 📄 License

The project is distributed under the license in the [LICENSE](LICENSE) file.
