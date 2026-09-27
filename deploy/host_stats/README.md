# Per-app (docker) usage for the ops console Server page

The api container runs with no extra privileges and **never** gets access to the Docker
socket. A small host-side systemd timer instead writes a one-shot `docker stats` snapshot to
`/opt/csaas/run/host_stats.json`, which is bind-mounted READ-ONLY into the api container
(`backend/app/services/server_stats.py` reads it). Whole-host CPU / memory / disk / load come
straight from `/proc` and `/`, so only the per-container table needs this file.

## Install

Copy the script to `/opt/csaas/bin` and the units to `/etc/systemd/system`:

```bash
sudo install -d -m 0755 /opt/csaas/bin /opt/csaas/run
sudo install -m 0755 csaas-host-stats.sh /opt/csaas/bin/csaas-host-stats.sh
sudo install -m 0644 csaas-host-stats.service /etc/systemd/system/csaas-host-stats.service
sudo install -m 0644 csaas-host-stats.timer /etc/systemd/system/csaas-host-stats.timer
sudo systemctl daemon-reload
sudo systemctl enable --now csaas-host-stats.timer
```

Verify it is running and producing output:

```bash
systemctl list-timers csaas-host-stats.timer
head -n 1 /opt/csaas/run/host_stats.json
```

## Mount the file into the api container

Add this volume line to the api service in `docker-compose.yml`:

```yaml
      - /opt/csaas/run/host_stats.json:/opt/csaas/run/host_stats.json:ro
```

If the file may not exist yet (the timer has not run for the first time), bind the DIRECTORY
instead so Docker creates the mount point:

```yaml
      - /opt/csaas/run:/opt/csaas/run:ro
```

Point the backend elsewhere with the `SERVER_STATS_HOST_FILE` environment variable if you keep
the file at a different path.

## Do not

**Never** mount `/var/run/docker.sock` into the api container. The whole point of this host job
is that the api never talks to the Docker daemon (see the constraints on
`backend/app/services/server_stats.py`).
