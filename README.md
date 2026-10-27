# Nextcloud Docker Manager

Instala y administra Nextcloud con Docker en un servidor local o una VPS. Incluye MariaDB, Redis, cron y Caddy para HTTPS. Interfaz en español e inglés.

Primero eliges el idioma, antes de preparar dependencias; se recuerda en los siguientes arranques. Menús con flechas, formularios y progreso animado. Enter selecciona; Esc vuelve.

[English](README.en.md) · [Pruebas realizadas](docs/VALIDACION.es.md)

## Instalar

Ubuntu 24.04 o Debian 12/13, Python 3.11+ y arquitectura amd64 o arm64. El asistente comprueba Docker y Compose y ofrece instalar Docker si falta.

```bash
git clone https://github.com/solqubit/nextcloud-docker-manager.git
cd nextcloud-docker-manager
sudo bash start.sh
```

1. Elige servidor local o VPS, versión, IP, puertos y carpeta de datos. Se ofrecen la última rama y la anterior con soporte oficial.
2. Abre la URL local indicada y crea el administrador en la web de Nextcloud.
3. Selecciona **Comprobar instalación web** en el gestor.
4. Añade un dominio cuando quieras acceso externo.

En una VPS, abre primero la instalación privada mediante SSH:

```bash
ssh -L 8080:127.0.0.1:8080 usuario@IP_VPS
# Abre http://127.0.0.1:8080 en tu PC
```

Después de instalar: `sudo bash /opt/nextcloud-docker-manager/start.sh`.

## Dominio y HTTPS

En **Dominios y acceso** puedes agregar o quitar dominios e IP y ajustar los puertos. Conserva la dirección local para seguir entrando por ella.

- **Cloudflare Tunnel:** instala cloudflared manualmente, configura el hostname hacia `http://IP_LOCAL:PUERTO_HTTP` y añade ese dominio en modo túnel en el gestor.
- **VPS directa:** apunta el DNS IPv4 al servidor, añade el dominio y publica TCP 80/443; habilítalos también en el firewall.

Caddy solicita y renueva el certificado. La validación HTTP-01 debe llegar al proxy y el túnel debe conservar el hostname. HTTP local y HTTPS público pueden coexistir. El HTTPS local opcional requiere confiar en la CA exportada desde el menú.

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

Los respaldos guardan SQL, aplicación y certificados. **Excluyen `data/<id>/data/`: documentos, versiones, papelera y appdata.** Contienen credenciales: mantenlos privados. Para otra ruta del gestor, inicia con `sudo bash start.sh --state-dir /otra/ruta`.

## Actualizar y recuperar

- **Nextcloud:** parches dentro de la misma rama; versiones mayores consecutivas, empezando por el último parche de la actual.
- **MariaDB:** conserva `11.4.x`. Solo permite cambiar de LTS si el Nextcloud objetivo lo exige.
- **Redis y Caddy:** actualizaciones independientes.
- **Mantenimiento:** comprueba servicios, cron, certificados, espacio, soporte oficial, actualizaciones y respaldos. Avisa al acercarse el fin de soporte y marca la rama como obsoleta cuando termina. La programación diaria no aplica actualizaciones.

Antes de actualizar se activa mantenimiento, se respalda SQL/aplicación y se ensaya el cambio con SQL real en contenedores aislados, sin documentos. Requiere tiempo y espacio en Docker.

Si falla, usa **Recuperar → Continuar** o **Recuperar → Restaurar**. Los documentos actuales se conservan: SQL antiguo puede no coincidir con ellos. Estos respaldos no recuperan archivos borrados.

Para actualizar el gestor desde un repositorio descargado y revisado:

```bash
sudo bash /opt/nextcloud-docker-manager/start.sh self-update --source /ruta/al/repositorio
```

Usa el menú o `--help`. Flechas para seleccionar, Enter para aceptar y Esc para cancelar; `--plain` activa el modo sencillo.

Pruebas Docker completadas. Pendiente en la VM: arranque del host, systemd y dominio/túnel con HTTPS público. [Resultados y reproducción](docs/VALIDACION.es.md).

[MIT](LICENSE). Interfaz adaptada de GLPI Docker Manager.
