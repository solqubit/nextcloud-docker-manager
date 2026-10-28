# Nextcloud Docker Manager

Instala y administra Nextcloud con Docker en un servidor local o una VPS. Incluye MariaDB, Redis, cron y Caddy para HTTPS. Interfaz en español e inglés.

Primero eliges el idioma, antes de preparar dependencias; se recuerda en los siguientes arranques. Menús con flechas, formularios y progreso animado. Enter selecciona; Esc vuelve (en el menú principal, Esc lleva a «Salir» y una segunda pulsación cierra).

[English](README.en.md) · [Pruebas realizadas](docs/VALIDACION.es.md)

## Instalar

El gestor depende de requisitos técnicos, no del nombre de la distribución:

- Linux de 64 bits (amd64 o arm64) y acceso `sudo`.
- Python 3.11 o posterior (`python3`, `python3.11`, `python3.12`…). Si falta, `start.sh` ofrece instalarlo con apt, dnf, zypper o apk.
- Docker Engine local con el plugin Compose V2 (`docker compose`). Si falta, el asistente lo instala desde download.docker.com (Debian, Ubuntu y derivadas; Fedora, RHEL, Rocky, AlmaLinux) o con los paquetes de la distribución (openSUSE, Alpine). Si RHEL o derivadas ya tienen `runc` (Podman), el asistente se detiene en lugar de reemplazarlo. Docker instalado por Snap solo sirve con volúmenes Docker, no con carpetas del servidor.
- systemd, solo para programar las comprobaciones diarias.

Probado en Ubuntu 24.04 y Debian 12/13.

```bash
git clone https://github.com/solqubit/nextcloud-docker-manager.git
cd nextcloud-docker-manager
sudo bash start.sh
```

1. Elige servidor local o VPS, versión, IP, puertos y carpeta de datos. Se ofrecen la última rama y la anterior con soporte oficial. Los puertos predeterminados son 80/443.
2. Abre la URL local indicada y crea el administrador en la web de Nextcloud.
3. El gestor completa automáticamente la verificación inicial al respaldar, actualizar Nextcloud o sus componentes y aplicar configuraciones, siempre que ya hayas creado el administrador. **Comprobar instalación web** sigue disponible para ejecutarla directamente. En instalaciones nuevas con SQL de hasta 64 MiB completa índices y tipos MIME; con bases mayores los deja para mantenimiento planificado.
4. Añade un dominio cuando quieras acceso externo.

En una VPS, o en cualquier servidor con IP pública, la primera instalación escucha solo en 127.0.0.1 y se abre mediante SSH (el gestor rechaza una IP pública hasta que exista el administrador):

```bash
ssh -L 8080:127.0.0.1:80 usuario@IP_VPS
# Abre http://127.0.0.1:8080 en tu PC
```

Después de instalar: `sudo bash /opt/nextcloud-docker-manager/start.sh`.

## Dominio y HTTPS

En **Dominios y acceso** puedes agregar o quitar dominios e IP y ajustar los puertos. Conserva la dirección local para seguir entrando por ella.
El primer dominio de un túnel se elige como principal automáticamente si todavía usas una dirección local.

- **Cloudflare Tunnel:** instala cloudflared manualmente, configura el hostname hacia `http://IP_LOCAL:PUERTO_HTTP` y añade ese dominio en modo túnel en el gestor.
- **VPS directa:** apunta el DNS IPv4 al servidor, añade el dominio y publica TCP 80/443; habilítalos también en el firewall.

Caddy solicita y renueva el certificado. La validación HTTP-01 debe llegar al proxy y el túnel debe conservar el hostname. HTTP local y HTTPS público pueden coexistir. Las rutas HTTP sin cifrar (direcciones locales y origen del túnel) solo responden a clientes de redes privadas (LAN, Docker, Tailscale), aunque el proxy escuche en 0.0.0.0. El HTTPS local opcional requiere confiar en la CA exportada desde el menú.

## Datos y respaldos

Base predeterminada: `/opt/nextcloud-docker-manager`. Puedes elegir otro disco para los datos al instalar.

```text
├── start.sh             # Abrir el gestor
├── current/             # Configuración activa
├── backups/             # SQL, aplicación y certificados
├── logs/                # Diagnósticos
└── data/<id>/
    ├── app/             # Nextcloud y configuración
    ├── db/              # MariaDB
    ├── data/            # Documentos, versiones, papelera y appdata
    ├── proxy_data/      # Certificados
    └── proxy_config/
```

Los respaldos guardan SQL, aplicación y certificados. **Excluyen `data/<id>/data/`: documentos, versiones, papelera y appdata**, para que sean rápidos y no llenen el disco con instalaciones de cientos de GB; respalda esa carpeta con tu herramienta de archivos. Contienen credenciales: mantenlos privados. Los respaldos automáticos previos a una actualización conservan los 3 más recientes; los manuales se eliminan desde **Respaldos**. Para otra ruta del gestor, inicia con `sudo bash start.sh --state-dir /otra/ruta`.

## Actualizar y recuperar

- **Nextcloud:** parches dentro de la misma rama; versiones mayores consecutivas, empezando por el último parche de la actual.
- **MariaDB:** conserva `11.4.x`. Solo permite cambiar de LTS si el Nextcloud objetivo lo exige.
- **Redis y Caddy:** actualizaciones independientes.
- **Comprobaciones de salud:** servicios, cron, certificados, espacio, soporte oficial, actualizaciones disponibles e integridad del último respaldo. Avisa al acercarse el fin de soporte y marca la rama como obsoleta cuando termina. La programación diaria (temporizador systemd) no aplica actualizaciones y termina con error si encuentra problemas.

En **Actualizaciones** ves en una sola pantalla los parches, la siguiente versión mayor, la renovación de imagen y los componentes. Antes de actualizar se comprueba el espacio con Nextcloud en línea; después se activa mantenimiento, se respalda SQL/aplicación y se ensaya el cambio con SQL real en contenedores aislados, sin documentos. Si el respaldo o el ensayo fallan, el servicio se restablece sin cambios. Requiere tiempo y espacio en Docker.

Si se interrumpe (corte, Ctrl+C) o falla al aplicar, usa **Recuperar operación pendiente → Continuar** o **→ Restaurar**. Los documentos actuales se conservan: SQL antiguo puede no coincidir con ellos. Estos respaldos no recuperan archivos borrados.

Para actualizar el gestor desde un repositorio descargado y revisado:

```bash
sudo bash /opt/nextcloud-docker-manager/start.sh self-update --source /ruta/al/repositorio
```

Usa el menú o `--help`. Flechas para seleccionar, Enter para aceptar y Esc para cancelar; `--plain` activa el modo sencillo.

Pruebas Docker completadas. Pendiente en la VM: arranque del host, systemd y dominio/túnel con HTTPS público. [Resultados y reproducción](docs/VALIDACION.es.md).

[MIT](LICENSE). Interfaz adaptada de GLPI Docker Manager.
