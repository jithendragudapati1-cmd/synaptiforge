# Publishing Synaptiforge

## Prepared, not yet publicly deployed

The repository contains a Gunicorn production launcher, native-Python Render blueprint, and optional Docker/Compose configuration. No hosting account, paid service, or domain has been created. Docker is not installed in the development environment, so the container image has not yet been built here. The recommended Render path uses its native Python runtime, so it does not require a local Docker build. The Python launcher and its processes are tested separately.

## Suggested first host

For a small beta, use one native Python web service with persistent disk and sufficient memory for four Python training workers. The supplied `render.yaml` is an optional starting point using a paid standard service and a 5 GB disk. Review current pricing and resource availability before applying it.

Render's free web services do not support persistent disks. Its default filesystem is ephemeral: uploaded data and SQLite accounts would be lost across redeploys without a persistent disk. See [persistent disks](https://render.com/docs/disks), [Docker hosting](https://render.com/docs/docker), and [free-service limits](https://render.com/docs/free).

The web server, training worker, email worker, and backup worker run inside one service so they can share the same local SQLite files and volume. Do not place these on separate hosts or scale the service to multiple instances. Scaling requires a shared database, object storage, and a distributed task queue.

## Before creating a public service

1. Put **only source code** in the hosting source repository; `.env`, `.env.production`, `data/`, `models/`, and `backups/` are private and must be excluded.
2. Choose the final HTTPS domain or the host-provided HTTPS subdomain.
3. Generate a strong random `SECRET_KEY`, at least 32 characters. Keep the same value across redeploys so sessions and encrypted email queues remain valid.
4. Enter Google OAuth and email credentials in the host's secret settings. Do not upload the local `.env` file.
5. Set `APP_BASE_URL` to the final HTTPS origin, without a trailing slash. This controls email links and OAuth callback construction. On Render, the app can use its assigned `RENDER_EXTERNAL_URL` automatically when `APP_BASE_URL` is not set.
6. Register `https://YOUR-DOMAIN/auth/google/callback` in the Google OAuth web client's authorized redirect URIs. Keep the localhost callback separately for local development. Add appropriate consent-screen test users or complete Google's publication requirements before broader access.
7. Set `COOKIE_SECURE=true`, `SYNAPTIFORGE_ENV=production`, and an absolute `SYNAPTIFORGE_STORAGE_DIR` on a mounted persistent disk. Ensure the service process can write that mounted directory.
8. Start with `ALLOW_DEMOS=false`; add a short beta notice and support contact for actual users.

The app refuses production startup without HTTPS configuration, secure cookies, a persistent-storage path, or a sufficiently long signing key. Configure TLS at the hosting platform or reverse proxy, which forwards requests to the local production port. The application does not terminate TLS itself.

## Docker Compose on your own server

Prepare `.env.production` locally on the server from `.env.production.example`, filling the real domain and credentials. Then:

```bash
docker compose up --build -d
```

Compose binds the app to `127.0.0.1:8080` and uses a named persistent volume. Put an HTTPS reverse proxy in front of that port. Do not remove the named volume during upgrades. Gunicorn replaces Flask's development server; see [Flask production guidance](https://flask.palletsprojects.com/en/stable/deploying/).

The container runs as an unprivileged user. If a hosting platform creates the mounted directory with different ownership, fix the mount permissions through the host's volume setup before starting the app.

## Storage and backups

The persistent directory contains account SQLite data, encrypted email queues, the job queue, account-scoped datasets/models/checkpoints, and worker lock files. Back up the persistent volume and signing key securely; these contain private user data and secrets. Use a consistent SQLite backup or briefly stop services before taking a whole-volume snapshot. Never put this backup in a public source repository.

Local accounts are not automatically copied to a new host. Decide whether to start with fresh accounts or perform a private migration of `data/` and `models/` into the mounted directory. If migrating the encrypted email queue, preserve its signing secret too. This decision is separate from uploading source code.

## Health and resource behavior

`GET /healthz` is public and exposes only service status and training availability. The production supervisor exits if the web, training, email, or backup process fails so the host can restart the service. The coordinator serializes jobs, limits runtime, supports cancellation, and can adopt a surviving training child after a coordinator restart. A stopped job with a saved checkpoint can resume; one stopped before its first checkpoint fails cleanly.

Default beta limits are documented in README. Increase them only after measuring your host's available memory, disk, and training time. The Python training processes have math-library thread counts capped; four workers still need memory for separate Python processes.

## Final hosted check

- Sign up, verify email, and confirm a real inbox receives the messages.
- Log in with password and Google; link an existing account if needed.
- Reset a password; confirm the previous password and old sessions stop working.
- Train a generic sample, pause/resume, and cancel a running job.
- Select a saved model, download the input template, and download prediction CSV.
- Rename and delete an inactive job after confirmation.
- Repeat with two accounts: neither may see or access the other's jobs, checkpoints, model selection, or predictions.
- Restart/redeploy the host; verify accounts, model files, queue entries, and settings persist.

Only the real hosted environment can confirm DNS, TLS, disk permissions, OAuth callbacks, and external email delivery. Those checks remain part of deployment after a host is selected.

## Automatic encrypted backups

`run_local.py` and `run_production.py` include an automatic backup worker. It takes its first snapshot when training is idle, then approximately once daily. SQLite databases are copied through SQLite's online backup API and checked for integrity. Saved uploads, models, checkpoints, and account files are included only when the files remain stable during the snapshot. A busy or changing workspace is retried later.

Archives use authenticated AES-GCM encryption with a key derived from `BACKUP_ENCRYPTION_KEY`, falling back to `SECRET_KEY` or the local signing key. Keep that key privately and unchanged; losing it makes backups unreadable. The worker keeps up to seven snapshots for up to seven days, pruning more when disk space is limited. A snapshot is skipped if it would exceed `BACKUP_MAX_MB` (default 1024 MB) or safe free-space requirements. `backups/automatic/status.json` records the last result. This file is for the operator and is not public.

These automatic snapshots are on the same persistent disk. They help recover accidental deletion but cannot recover loss of the entire disk. Download an encrypted archive to a separate private location regularly. Do not upload accounts, archives, or signing keys to the source repository.

To restore a backup, stop the services and restore into a new empty directory:

```bash
python restore_backup.py /private/path/snapshot-....sfb /private/path/empty-restored-storage
```

The encryption key must match the original. The command verifies authenticity before extraction and refuses to overwrite an existing workspace. Inspect the restored data, then point `SYNAPTIFORGE_STORAGE_DIR` at it. Review deletion requests when restoring old account data.

## Privacy and support

Public `/privacy` and `/support` pages now describe stored data, essential cookies, service access, retention, deletion requests, and supported input types. Set `SUPPORT_EMAIL` to a monitored operator inbox; otherwise it uses the account-email sender. Production startup requires a valid contact address. Check these pages against the actual provider settings and operator practices when changing storage or email providers.

## Additional small-beta safeguards

Defaults cap the whole service at 50 accounts, 10 queued/running jobs, and 1 GB of live workspace storage. Jobs also need 256 MB of remaining free disk and reserve an additional 8 MB for generated artifacts. These supplement each user's existing quota; adjust the environment variables only after measuring the host. Existing accounts remain able to log in when the signup limit is reached.

Account settings provide a ZIP download of profile information and private workspace files, excluding password hashes and sign-in secrets. Account deletion requires the exact email address, the current password for password accounts, or a recent Google sign-in for Google-only accounts. Active training must stop first. Deletion revokes all sessions and removes live workspace files. Account IDs are never reused. A durable deletion registry lets interrupted cleanup retry and helps the restore tool remove previously deleted accounts from old snapshots.

Set `BACKUP_SECONDARY_DIR` to a dedicated cloud-synced folder or mounted external destination. Only encrypted archives and an encrypted deletion registry are copied there, with hashes checked locally. The background worker retries mirroring periodically and keeps the same snapshot retention as the primary directory. The computer's cloud-sync client must be running and signed in. Verify remote upload separately; a filesystem copy alone is not proof of off-server delivery.

Keep the signing/backup encryption key in a separate secure place, such as a password manager. The source package does not contain it. Losing the key makes the encrypted archives unreadable even if Drive still has them. When restoring after total local loss, configure the secondary folder and matching key so the latest deletion registry is available too. A restored workspace invalidates old sessions.

Public hosting remains a separate step: hosted HTTPS, Google callback registration, SMTP delivery, and persistence after a host restart must be checked before inviting testers. No paid hosting is authorized or created by the local preparation.
