[Russian](README.md) | [English](README.en.md)

# 🎬 TorrServer to Infuse Bridge

An automated `.strm` + WebDAV bridge between **TorrServer** and **Infuse** player (Apple TV, iOS, Mac) for streaming torrents without downloading files to disk.

## ⚙️ Architecture and How It Works

The project launches several services in Docker:

- **TorrServer** — torrent source and HTTP streaming server.
- **TorrServer proxy (Nginx)** — HTTP proxy in front of TorrServer with Basic Auth, through which Infuse accesses streams.
- **Parser** — service that polls TorrServer and generates `.strm` files.
- **WebDAV** — publishes the directory of `.strm` files for Infuse connection.

How it works:

1. You add a torrent to TorrServer.
2. Parser discovers it during the next poll.
3. Parser creates `.strm` files in the project library.
4. WebDAV publishes this library for Infuse.
5. Infuse indexes the files and plays streams through TorrServer proxy.

## ✨ Features

- Fully containerized launch via Docker.
- Installation and updates through a single `install.sh` (no manual Git and Compose work).
- Automatic `.strm` file generation for Infuse with atomic writes (no risk of empty files during Infuse reads).
- Background TorrServer polling without cron or Python dependencies on the host.
- The `parser` service image is published to [GitHub Container Registry (GHCR)](https://ghcr.io/eonyushkin-commits/torrserver-infuse-bridge) and pulled automatically — no build step required on the server.
- Public library via WebDAV with HTTP Basic Auth.
- Configuration and data storage inside the project directory (`.env`, TorrServer database, `.strm` library).
- Safe code updates without data loss in `./ts` and `./strm_library`.

## 📋 Requirements

The server must have installed:

- `git`
- `docker`
- `curl`
- Docker Compose v2 or compatible `docker-compose`

The installation script must run as `root` or via `sudo`.

## 🚀 Installation

Run on your server:

```bash
curl -O https://raw.githubusercontent.com/eonyushkin-commits/torrserver-infuse-bridge/refs/heads/main/install.sh
chmod +x install.sh
sudo ./install.sh
```

The installation script:

- clones the repository to `/opt/`;
- prompts for WebDAV and TorrServer parameters;
- creates `.env` with specified values;
- generates `.htpasswd` and `nginx.conf` config for TorrServer proxy;
- pulls the ready-built `parser` image from GHCR and starts all containers via Docker Compose.

## 🔄 Updates

Re-running `install.sh` is the standard way to update the project and change parameters:

```bash
cd /opt/torrserver-infuse-bridge
sudo ./install.sh
```

If the `/opt/torrserver-infuse-bridge` directory is a git repository, the script:

- stops current containers;
- determines the current branch (`git rev-parse --abbrev-ref HEAD`);
- executes `git fetch --all && git reset --hard "origin/$CURRENT_BRANCH"` to update code;
- recreates `.env` and configs if necessary;
- pulls the latest `parser` image from GHCR and brings up containers, **without touching directories** `./ts` and `./strm_library`.

To manually update only the `parser` image without a full reinstall:

```bash
cd /opt/torrserver-infuse-bridge
docker compose pull parser
docker compose up -d parser
```

## 🧩 Installation Parameters

During installation, the script interactively prompts for:

- `WEBDAV_PORT` — external WebDAV port, default `8080`;
- `WEBDAV_USER` — WebDAV and TorrServer proxy login, default `admin`;
- `WEBDAV_PASSWORD` — WebDAV and TorrServer proxy password;
- `HOST_IP` — external server IP address (or domain name).  
  **Important:** this address is written into generated `.strm` files.  
  If specified incorrectly (e.g., leaving `127.0.0.1` when installing on remote VPS), Infuse will successfully load the media library via WebDAV but won't be able to start video playback;
- `TORR_PORT` — external TorrServer port (proxy container port). By default, the script suggests a random available port in the range `10000–60000`, but you can specify any other free port.

These values are saved to `.env` file, which is used when launching services.

## 📁 Project Structure

After installation, the project operates from the directory:

```bash
/opt/torrserver-infuse-bridge
```

Main working directories and files:

- `install.sh` — installation, updates, and reconfiguration (idempotent deployment script);
- `.env` — environment parameters (WebDAV/TorrServer port, login/password, external IP/domain);
- `.htpasswd` — file with bcrypt password hash for HTTP Basic Auth (generated automatically, not stored in Git);
- `nginx.conf` — Nginx proxy configuration for TorrServer (generated automatically);
- `./strm_library` — library of `.strm` files for Infuse;
- `./ts` — TorrServer data (configuration, database, cache);
- `docker-compose.yml` — project containers description.

> `torr_to_strm.py` is part of the repository and is packaged into the `parser` Docker image published on GHCR. On the server it is present as part of the repository clone, but runs exclusively inside the container.

## 🍏 Connecting in Infuse

1. Open Infuse → **Settings** → **Add Files** → **Other...**.
2. Select **WebDAV** protocol.
3. Fill in the data using values you entered when running `install.sh`:
   - **Address:** your VPS IP or domain name (without `http://`).
   - **Username:** your login (specified during installation, default `admin`).
   - **Password:** your password (specified during hidden input).
4. In the **Advanced** section, specify:
   - **Port:** WebDAV port (specified during installation, default `8080`).
5. Click **Save** and add the folder to Favorites (⭐).

Infuse will automatically scan files, fetch posters, descriptions, and group series by seasons. New movies will appear in Infuse with a slight delay after adding to TorrServer and parser processing.

## 🛠 Administration

Check container status:

```bash
cd /opt/torrserver-infuse-bridge
docker compose ps
```

View parser logs:

```bash
docker logs -f strm-parser
```

Stop services:

```bash
cd /opt/torrserver-infuse-bridge
docker compose down
```

Start services again:

```bash
cd /opt/torrserver-infuse-bridge
docker compose up -d
```

## 🌐 Useful URLs

After installation, two main addresses are typically used:

- TorrServer UI (via Nginx proxy): `http://HOST_IP:TORR_PORT`
- WebDAV URL: `http://HOST_IP:WEBDAV_PORT`

Both services are protected by HTTP Basic Auth with login/password set during installation.

## 🔐 Security

- `.env` file is created with `600` permissions and is not committed to the repository.
- `.htpasswd` file contains bcrypt password hash, is generated by `httpd:alpine` container, and is also not stored in Git.
- Access to WebDAV and TorrServer proxy is protected by login and password set during installation.
- TorrServer is not exposed directly to the outside: external connections go through Nginx proxy with Basic Auth.
- `.strm` files contain URLs like `http://user:password@host:port/...`, but are only accessible via WebDAV, which itself requires the same credentials.
- Using a strong password is recommended, and if necessary, restrict external port access with a firewall (ufw, Security Groups, etc.).

## 📝 Notes

- The project is oriented toward fully containerized launch without manual Python environment installation on the host.
- The library for Infuse is published from the local `./strm_library` directory, TorrServer data from `./ts`.
- Correct media library display in Infuse depends on how parser forms `.strm` files and element names; file names are formatted for Infuse convenience.
- Parser uses error-resistant polling logic and atomic `.strm` writes via temporary files, so Infuse doesn’t encounter empty or partially written files.
- The project is especially convenient for VPS scenarios where TorrServer, `.strm` generator, Nginx proxy, and WebDAV work as a unified stack.
