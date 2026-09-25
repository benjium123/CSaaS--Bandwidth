# Deploy: Billing v2 (bundles, hard stop, alerts, console, Telnyx recon, fax)

Branch `b1-billing-v2` = live `origin/main` (51332c2) + the lost local-main commits
(Telnyx ownership gate, SignalWire sip-dial) + billing v2. Migrations 0064-0067.
Fast-forward of origin/main (no conflicts). Automated pushes/deploys were blocked by the
auto-mode classifier, so these steps are run by the operator.

## 1. Push + merge
```bash
cd C:/Users/omer_/csaas
git push origin b1-billing-v2 b0-sync-fixes
# open a PR b1-billing-v2 -> main on GitHub, review, merge (fast-forward)
git checkout main && git pull --ff-only
```

## 2. Box: back up the DB and set the prepaid default
```bash
ssh root@144.126.152.175
docker exec csaas-db-1 pg_dump -U csaas csaas | gzip > /opt/csaas/backups/pre-billing-v2-$(date +%F-%H%M).sql.gz
cp /opt/csaas/.env /opt/csaas/.env.bak-billingv2
sed -i 's/^TELEPHONY_PREPAID_DEFAULT=.*/TELEPHONY_PREPAID_DEFAULT=true/' /opt/csaas/.env
grep -q '^TELEPHONY_PREPAID_DEFAULT=' /opt/csaas/.env || echo 'TELEPHONY_PREPAID_DEFAULT=true' >> /opt/csaas/.env
```

## 3. Deploy (runs alembic 0064-0067; dry-run on a clone of the live DB passed)
```bash
bash deploy/deploy.sh          # from C:/Users/omer_/csaas on main
```
Migration 0065 switches ALL orgs to prepaid, billed from the migration instant.
**Right after**, give the launch credit, or inbound calls to those orgs are declined:
```bash
ssh root@144.126.152.175 "docker exec -i csaas-api-1 python scripts/launch_credit.py"          # dry run
ssh root@144.126.152.175 "docker exec -i csaas-api-1 python scripts/launch_credit.py --apply"  # $20 each
```
(Or per org from Ops -> Console -> org -> Credit adjustment. To exempt an org, Console ->
org -> Prepaid off.)

## 4. Fax
```bash
ssh root@144.126.152.175 "docker exec -i csaas-api-1 python scripts/telnyx_fax_setup.py --outbound-voice-profile-id 3051281139210650991"          # dry run
ssh root@144.126.152.175 "docker exec -i csaas-api-1 python scripts/telnyx_fax_setup.py --apply --outbound-voice-profile-id 3051281139210650991"
# put the printed TELNYX_FAX_CONNECTION_ID=... into /opt/csaas/.env, then:
ssh root@144.126.152.175 "cd /opt/csaas && docker compose --env-file .env -f deploy/docker-compose.prod.yml up -d api"
```
Then in the app: Fax -> Fax lines -> switch a csaas Telnyx number to fax mode (that
number stops taking voice calls; texting keeps working).

## 5. Low-balance emails (Resend)
Add to /opt/csaas/.env: `RESEND_API_KEY=...`, `RESEND_FROM=billing@ringlite.io` (verify
ringlite.io in Resend), restart api. Without it, bell/popup alerts still work; emails do not.

## 6. Verify
- `docker inspect csaas-agent-1 --format '{{.RestartCount}}'` stops climbing (call monitor
  moved to health port 8082).
- `curl -s https://ringlite.io/status`
- Ops -> Console loads; prices list shows SMS $0.015, MMS $0.035, call $0.011/min, fax $0.10.
- Settings -> Billing shows the Message bundles card; buy 1 SMS bundle (+1,000 texts) and
  5 bundles ($48, +5,000 texts, 20% discount recorded in Console -> Payments).
- api logs after the next hourly tick: `billing_thresholds_refreshed`, `telnyx_recon`.

## Rollback
Redeploy 51332c2 and restore the dump from step 2 (migrations 0064-0067 are additive
except 0065's one-way prepaid switch; restoring the dump undoes it).
