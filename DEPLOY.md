# Deploy updates to EC2

Assumes the repo is already cloned on EC2 and the systemd service is named `boogle`.
Replace paths, key filename, and `PUBLIC_IP` with your own values.

## On your Mac

From the repository root, review and publish your changes:

```bash
git status
git add <files-you-changed>
git commit -m "Describe your changes"
git push
ssh -i ~/Downloads/boogle.pem ubuntu@PUBLIC_IP
```

Do not commit `.venv/`, `boogle.db`, passwords, or your `.pem` key.

## On EC2

```bash
cd /absolute/path/to/PA1
git status
git pull --ff-only
cd starter
source .venv/bin/activate
```

If `git status` shows server-side edits, resolve them before pulling. If the pull fails, stop and resolve the error before restarting.

Only if dependencies changed, install them using your project's dependency file. If you have no dependency file, install the newly required packages explicitly:

```bash
python3 -m pip install <new-package>
```

Restart and check the service:

```bash
sudo systemctl restart boogle
sudo systemctl status boogle --no-pager
sudo journalctl -u boogle -n 50 --no-pager
curl -i http://127.0.0.1:8000/
```

Open `http://PUBLIC_IP:8000/` in your browser and verify searching and history.

## Commands to avoid during normal updates

- Don't run `python3 app.py` or a second `gunicorn` while the service is running; the service already owns port 8000.
- Don't run `gunicorn app:app` from the repo root; `app.py` is in `starter`.
- Don't recreate the virtual environment or reinstall all packages for a code-only update.
- Don't delete or overwrite `starter/boogle.db`; it contains your stored history and visits.
- Don't use `git reset --hard` or `git clean -fd` to fix a failed pull; they can discard server files or edits.
- Don't run `sudo systemctl daemon-reload` for ordinary Python changes. Use it only after editing the service file, then restart the service.

Database schema changes need a migration; restarting won't alter existing tables automatically.

## If you have not configured systemd

Stop your existing foreground Gunicorn with Ctrl+C, pull the updates, then run from `starter` with the virtual environment activated:

```bash
gunicorn --workers 1 --bind 0.0.0.0:8000 app:app
```

This foreground process does not reliably stay running after you disconnect from SSH. Configure the `boogle` service for persistent hosting.
