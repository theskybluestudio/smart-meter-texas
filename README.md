# Smart Meter Texas Docker App

SQLite-only Smart Meter Texas interval retriever plus Streamlit dashboard.

This is a clean deployable tree carved out of the original workspace. It intentionally does **not** include local secrets, historical data, or logs.

## Services

- `web`: Streamlit dashboard, reading from local SQLite with CSV fallback.
- `scheduler`: daily Smart Meter Texas interval refresh into the shared SQLite volume.

Both services share Docker volumes mounted at:

- `/app/data`
- `/app/logs`

## Quick start

For local development, a `.env` file is the simplest option:

```bash
cp .env.example .env
# edit .env with SMT_USERNAME, SMT_PASSWORD, SMT_ESIID

docker compose up --build -d
```

For a VM or shared host, prefer secret files instead of plaintext values in `.env`:

```bash
mkdir -p secrets
printf '%s' 'your_username' > secrets/smt_username
printf '%s' 'your_password' > secrets/smt_password
printf '%s' 'your_esiid' > secrets/smt_esiid
chmod 600 secrets/smt_*

docker compose -f docker-compose.yml -f docker-compose.secrets.example.yml up --build -d
```

Open:

```text
http://localhost:8501
```

## Run a one-off refresh

```bash
docker compose run --rm scheduler smt-scheduler --once
```

Or directly:

```bash
docker compose run --rm scheduler smt-interval --overlap-days 7
```

## Configuration

Environment variables:

- `SMT_USERNAME` / `SMT_PASSWORD`: Smart Meter Texas login.
- `SMT_ESIID`: target ESIID; recommended even if the account only has one meter.
- `SMT_USERNAME_FILE` / `SMT_PASSWORD_FILE` / `SMT_ESIID_FILE`: optional file-based alternatives for deployments. If both are set, direct environment values win.
- `SMT_TIMEZONE`: default `America/Chicago`.
- `SMT_REFRESH_TIME`: daily scheduler time, default `11:00`.
- `SMT_OVERLAP_DAYS`: overlap refresh window, default `7`.
- `SMT_RUN_ON_START`: set `1` to run a refresh immediately when scheduler starts.

Dashboard config lives in `config.toml` inside the image and defaults to:

```toml
[data]
source = "sqlite"
fallback_sources = ["csv"]

[sqlite]
path = "data/smt_interval_usage_history.sqlite"
table = "interval_usage"
```

## Data outputs

The interval retriever writes:

- `data/smt_interval_usage_history.sqlite`
- `data/smt_interval_usage_history.csv`
- `data/smt_interval_sync_state.json`
- `logs/raw-payloads/*.json`

The SQLite table is `interval_usage`.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest -q
```

## Notes

- This uses the unofficial Smart Meter Texas JSON API and can break if SMT changes its API.
- No secrets should be committed. Use `.env` only for local development; use secret files or your deployment secret manager for VM deployments.
