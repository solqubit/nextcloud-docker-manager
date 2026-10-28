# Nextcloud Docker Manager

Install and manage Nextcloud with Docker on a local Linux server or VPS. Includes MariaDB, Redis, cron and Caddy for HTTPS. English and Spanish interface.

Choose the language before dependency setup; it is remembered on subsequent launches. Arrow-key menus, forms and animated progress. Enter selects; Esc goes back (on the main menu, Esc moves to «Exit» and a second press closes).

[Español](README.md) · [Test results](docs/VALIDACION.es.md)

## Install

The manager depends on technical requirements, not on the distribution name:

- 64-bit Linux (amd64 or arm64) and `sudo` access.
- Python 3.11 or later (`python3`, `python3.11`, `python3.12`…). When missing, `start.sh` offers to install it with apt, dnf, zypper or apk.
- A local Docker Engine with the Compose V2 plugin (`docker compose`). When missing, the assistant installs it from download.docker.com (Debian, Ubuntu and derivatives; Fedora, RHEL, Rocky, AlmaLinux) or from the distribution's packages (openSUSE, Alpine). If RHEL or a rebuild already has `runc` (Podman), the assistant stops instead of replacing it. Snap Docker only works with Docker volumes, not server folders.
- systemd, only for scheduling the daily checks.

Tested on Ubuntu 24.04 and Debian 12/13.

```bash
git clone https://github.com/solqubit/nextcloud-docker-manager.git
cd nextcloud-docker-manager
sudo bash start.sh --lang en
```

1. Choose local server or VPS, version, IP, ports and data directory. The latest and previous officially maintained majors are offered. The default ports are 80/443.
2. Open the displayed local URL and create the administrator in the web installer.
3. The manager automatically completes initial verification before backups, Nextcloud or component updates, and configuration changes, provided the administrator already exists. **Check web installation** remains available to run it directly. New installations with SQL up to 64 MiB complete indices and MIME repairs; larger databases defer them to planned maintenance.
4. Add a domain when you want external access.

After administrator creation, cron automatically sets a 300-second app store timeout. Custom values are preserved; checks, updates and restores also apply the default when it is missing.

On a VPS, or any server with a public IP, the first installation listens only on 127.0.0.1 and opens through SSH (the manager rejects a public IP until the administrator exists):

```bash
ssh -L 8080:127.0.0.1:80 user@VPS_IP
# Open http://127.0.0.1:8080 on your PC
```

After installation: `sudo bash /opt/nextcloud-docker-manager/start.sh --lang en`.

## Domain and HTTPS

Use **Domains and access** to add or remove hosts and IP addresses and change ports. Keep the local address to retain local access.
Each access type shows saved addresses first and offers adding another. The first public domain, tunnel or direct, becomes primary if no other public domain exists; adding more retains your choice.

- **Cloudflare Tunnel:** install cloudflared manually, route the hostname to `http://LOCAL_IP:HTTP_PORT`, and add that domain in tunnel mode in the manager.
- **Direct VPS:** point IPv4 DNS to the server, add the domain and publish TCP 80/443; allow these ports in the firewall too.

Caddy requests and renews the certificate. HTTP-01 validation must reach the proxy, and the tunnel must preserve the hostname. Local HTTP and public HTTPS can coexist. Unencrypted HTTP routes (local addresses and the tunnel origin) only answer clients on private networks (LAN, Docker, Tailscale), even when the proxy listens on 0.0.0.0. Optional local HTTPS requires trusting the CA exported from the menu.

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

Backups contain SQL, application state and certificates. **They exclude `data/<id>/data/`: documents, versions, trash and appdata**, so they stay fast and do not fill the disk on installations of hundreds of GB; back that folder up with your file backup tool. They contain credentials: keep them private. Automatic pre-update backups keep the 3 newest; manual ones are deleted from **Backups**. For another manager directory, start with `sudo bash start.sh --state-dir /another/path`.

## Updates and recovery

Use **Accounts → Reset password** on the main menu to select an account and enter a new password twice. The current password cannot be displayed. The manager does not log the new password; server-side encryption requires Nextcloud's specific recovery process to retain access to encrypted files. Restoring SQL also restores the passwords that existed in that backup.

- **Nextcloud:** patches within the same branch; consecutive major versions, starting from the current branch's latest patch.
- **MariaDB:** keeps `11.4.x`. Changing LTS is allowed only when required by the target Nextcloud.
- **Redis and Caddy:** independent updates.
- **Health checks:** services, cron, certificates, space, official support, available updates and the latest backup's integrity. Warns near end of support and marks expired branches as obsolete. The daily schedule (a systemd timer) never applies updates and exits with an error when it finds problems.

**Updates** shows patches, the next major, image refresh and components on one screen. Before updating, free space is checked while Nextcloud is online; then the manager enables maintenance, backs up SQL/application state, and rehearses the change with actual SQL in isolated containers without documents. If the backup or the rehearsal fails, the service is restored unchanged. This takes time and free Docker storage.

After an interruption (power loss, Ctrl+C) or a failure while applying, use **Recover pending operation → Resume** or **→ Restore**. Current documents remain in place: older SQL may no longer match them. These backups do not recover deleted files.

To update the manager from a downloaded and reviewed checkout:

```bash
sudo bash /opt/nextcloud-docker-manager/start.sh self-update --source /path/to/checkout
```

Use the menu or `--help`. Arrow keys select, Enter accepts and Esc cancels; `--plain` enables the simple interface.

Docker checks passed. Pending VM checks: host bootstrap, systemd and domain/tunnel with public HTTPS. [Results and reproduction](docs/VALIDACION.es.md).

[MIT](LICENSE). Terminal interface adapted from GLPI Docker Manager.
