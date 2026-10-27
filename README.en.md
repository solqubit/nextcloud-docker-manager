# Nextcloud Docker Manager

Install and manage Nextcloud with Docker on a local Linux server or VPS. Includes MariaDB, Redis, cron and Caddy for HTTPS. English and Spanish interface.

Choose the language before dependency setup; it is remembered on subsequent launches. Arrow-key menus, forms and animated progress. Enter selects; Esc goes back.

[Español](README.md) · [Test results](docs/VALIDACION.es.md)

## Install

Ubuntu 24.04 or Debian 12/13, Python 3.11+, amd64 or arm64. The assistant checks Docker and Compose and offers to install Docker when missing.

```bash
git clone https://github.com/solqubit/nextcloud-docker-manager.git
cd nextcloud-docker-manager
sudo bash start.sh --lang en
```

1. Choose local server or VPS, version, IP, ports and data directory. The latest and previous officially maintained majors are offered. The default ports are 80/443.
2. Open the displayed local URL and create the administrator in the web installer.
3. Select **Check web installation** in the manager.
4. Add a domain when you want external access.

On a VPS, first open the private installer through SSH:

```bash
ssh -L 8080:127.0.0.1:80 user@VPS_IP
# Open http://127.0.0.1:8080 on your PC
```

After installation: `sudo bash /opt/nextcloud-docker-manager/start.sh --lang en`.

## Domain and HTTPS

Use **Domains and access** to add or remove hosts and IP addresses and change ports. Keep the local address to retain local access.

- **Cloudflare Tunnel:** install cloudflared manually, route the hostname to `http://LOCAL_IP:HTTP_PORT`, and add that domain in tunnel mode in the manager.
- **Direct VPS:** point IPv4 DNS to the server, add the domain and publish TCP 80/443; allow these ports in the firewall too.

Caddy requests and renews the certificate. HTTP-01 validation must reach the proxy, and the tunnel must preserve the hostname. Local HTTP and public HTTPS can coexist. Optional local HTTPS requires trusting the CA exported from the menu.

## Data and backups

Default directory: `/opt/nextcloud-docker-manager`. Choose another disk for data during installation.

```text
├── start.sh             # Open the manager
├── current/             # Active configuration
├── backups/             # SQL, application and certificates
├── logs/                # Diagnostics
└── data/<id>/
    ├── app/             # Nextcloud and configuration
    ├── db/              # MariaDB
    ├── data/            # Documents, versions, trash and appdata
    ├── proxy_data/      # Certificates
    └── proxy_config/
```

Backups contain SQL, application state and certificates. **They exclude `data/<id>/data/`: documents, versions, trash and appdata.** They contain credentials: keep them private. For another manager directory, start with `sudo bash start.sh --state-dir /another/path`.

## Updates and recovery

- **Nextcloud:** patches within the same branch; consecutive major versions, starting from the current branch's latest patch.
- **MariaDB:** keeps `11.4.x`. Changing LTS is allowed only when required by the target Nextcloud.
- **Redis and Caddy:** independent updates.
- **Maintenance:** checks services, cron, certificates, space, official support, updates and backups. Warns near end of support and marks expired branches as obsolete. Daily scheduling does not apply updates.

Before updating, the manager enables maintenance, backs up SQL/application state, and rehearses the change with actual SQL in isolated containers without documents. This takes time and free Docker storage.

For a failure, use **Recover → Resume** or **Recover → Restore**. Current documents remain in place: older SQL may no longer match them. These backups do not recover deleted files.

To update the manager from a downloaded and reviewed checkout:

```bash
sudo bash /opt/nextcloud-docker-manager/start.sh self-update --source /path/to/checkout
```

Use the menu or `--help`. Arrow keys select, Enter accepts and Esc cancels; `--plain` enables the simple interface.

Docker checks passed. Pending VM checks: host bootstrap, systemd and domain/tunnel with public HTTPS. [Results and reproduction](docs/VALIDACION.es.md).

[MIT](LICENSE). Terminal interface adapted from GLPI Docker Manager.
