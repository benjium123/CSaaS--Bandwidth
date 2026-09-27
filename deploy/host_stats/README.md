# Host stats job (per-app CPU / memory for the ops console "Server" page)

The api container has no docker socket and never gets one. Instead, a host systemd timer runs
`docker stats --no-stream` every 15 s and atomically writes NDJSON to
`/opt/csaas/var/host_stats/host_stats.json`. `deploy/docker-compose.prod.yml` bind-mounts that
directory READ-ONLY into the api container at `/app/var/host_stats`, where
`backend/app/services/server_stats.py` reads it (override with `SERVER_STATS_HOST_FILE`).

Paths are chosen to survive `deploy.sh`: its `rsync --delete` wipes everything under
`/opt/csaas` except `var/`, `.env`, `backups/` and a few rendered files. The output lives under
`var/`; the script lives in `/usr/local/sbin`.

## Install (once, as root on the box)

```bash
install -d -m 0755 /opt/csaas/var/host_stats
install -m 0755 csaas-host-stats.sh /usr/local/sbin/csaas-host-stats.sh
install -m 0644 csaas-host-stats.service /etc/systemd/system/csaas-host-stats.service
install -m 0644 csaas-host-stats.timer /etc/systemd/system/csaas-host-stats.timer
sed -i 's/\r$//' /usr/local/sbin/csaas-host-stats.sh /etc/systemd/system/csaas-host-stats.*
systemctl daemon-reload && systemctl enable --now csaas-host-stats.timer
```

Verify:

```bash
systemctl list-timers csaas-host-stats.timer
head -n 1 /opt/csaas/var/host_stats/host_stats.json
```

## Do not

**Never** mount `/var/run/docker.sock` into the api container. `docker stats` lists every
container on the box (other apps included); that is intended: the page is labelled
"Whole server (shared with other apps)".
