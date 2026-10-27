# Pruebas realizadas

27 de octubre de 2026. Pruebas locales en Docker Desktop Linux amd64, Docker 29.9.2 y Compose 5.6.1. Ejecutor reproducible: `tests/Dockerfile`.

| Área | Resultado |
|---|---|
| Pruebas pequeñas | 62 casos: controles, formularios, confirmaciones, errores, modo sin color, cambio de tamaño, arranque sin Python y salida CLI |
| Interfaz completa | Recorridos por teclado y capturas de terminal; instalación, verificación y restauración reales de Nextcloud 35.0.2 desde el asistente |
| Instalación | Creación del administrador desde la web; instalación de Nextcloud 35.0.2 comprobada |
| Acceso | WebDAV PUT/GET/PROPFIND, comparticiones, dominios permitidos, HTTP y HTTPS local simultáneos |
| Certificados y rutas | Certificado local verificado, login v2, CardDAV/CalDAV y consulta de WebFinger |
| Actualizaciones | Nextcloud `33.0.9 → 33.0.10 → 34.0.5`; MariaDB `11.4.12 → 11.4.13` por separado |
| Respaldos | SQL, aplicación y certificados; tamaños, SHA-256 y gzip verificados; documentos excluidos |
| Restauración | SQL/aplicación en volúmenes nuevos; documentos y comparticiones conservados |
| Fallos | Configuración revertida, ensayo fallido bloquea el despliegue, importación SQL interrumpida recuperable y diagnósticos conservados |
| Persistencia | Carpetas Linux con bind, generaciones anteriores conservadas y arranque bloqueado si falta la ruta de datos |
| Gestor y cron | Gestor instalado independiente, bloqueo de operaciones simultáneas, diagnóstico, exportación de imagen y conectividad de cron |
| Aislamiento | Sin salida externa en el ensayo; limpieza de sus recursos y contenedores previos conservados |

Se usaron MariaDB 11.4.12/11.4.13/11.8.9, Redis 7.4.12 y Caddy 2.11.7. La pasada completa terminó con `FULL DOCKER ACCEPTANCE COMPLETE`.

## Caso de cambio LTS

Las versiones reales de Nextcloud consultadas admiten MariaDB 11.4: intentar cambiarla de LTS se rechazó.

Para probar el caso futuro se **simuló únicamente un requisito de Nextcloud**. La migración SQL `11.4 → 11.8` fue real: ensayo aislado, importación en un volumen nuevo y conservación de documentos. Una interrupción provocada permitió volver al motor y al mismo volumen SQL originales. Esa simulación existe solo en las pruebas.

## Reproducir

Las pruebas Docker requieren al menos 20 GiB libres para imágenes y copias temporales. Crean y eliminan sus propios recursos; conservan las imágenes en caché.

Los ensayos de navegación usan un backend controlado; `--ui-only` prueba la interfaz con Docker real y conserva documentos y comparticiones al restaurar.

```bash
python3 -m pip install -r tests/requirements.txt
python3 -m unittest discover -s tests -v
shellcheck start.sh tests/run-integration.sh
bash tests/run-integration.sh
bash tests/run-integration.sh --lts-only
bash tests/run-integration.sh --ui-only
```

En Windows con Docker Desktop Linux:

```powershell
.\tests\run-integration.ps1
.\tests\run-integration.ps1 -SkipBuild -LtsOnly
```

Comprobaciones adicionales, con el ejecutor ya construido:

```bash
docker run --rm -v "$PWD:/workspace:ro" -v /var/run/docker.sock:/var/run/docker.sock ndm-test-runner:local python3 tests/proxy_routes.py
docker run --rm -v "$PWD:/workspace:ro" -v /var/run/docker.sock:/var/run/docker.sock ndm-test-runner:local python3 tests/cron_network.py
```

## Pendiente en la VM

Instalación de Python/Docker en Ubuntu limpia, disco físico y montaje, reinicio, systemd, DNS/firewall, emisión y renovación pública de certificados, túnel manual y clientes desktop/móvil reales. HTTPS local con CA privada no valida esos pasos. Arm64 sin probar. Las pruebas unitarias remotas pasaron en GitHub Actions; la suite Docker remota no se ejecutó.
