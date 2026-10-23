# Deployment

There are two editions of the website. They use the same templates, engine and data.

| | Static edition (GitHub Pages) | Server edition (Flask + workers) |
|---|---|---|
| Hosting | GitHub Pages, free, no server | Any host that runs containers or Python processes |
| Freshness | Daily snapshot after each PSX close | Live; manual "Refresh latest data" |
| Stock pages, engine, Learn page | ✅ every tracked symbol | ✅ |
| PSX news search | Pre-built for the tracked watchlist | Live search of every listed company |
| Ops pages, refresh, forecasts, models | ✗ | ✅ (`/ops`, admin token) |

## Static edition on GitHub Pages

`.github/workflows/pages.yml` rebuilds and publishes the site every weekday at 18:45 PKT. It also runs on
pushes to `main` that touch the app, and manually ("Run workflow"). Each run:

1. Starts a throwaway PostgreSQL and Redis on the runner.
2. Starts the pinned psxdata-api (`services/psxdata-api/run.sh`).
3. Downloads full daily-bar history, applies `config/corporate_actions.yaml` and runs the quality checks.
4. Runs `flask --app wsgi site export`. This renders every page under the base path and writes the data
   files (`data/engine/*.json`, `data/diagnostics.json`, `data/symbols.json`, `data/news/*.json`).
5. Fails if any page errors, any engine self-check fails, or any internal link is broken.
6. Deploys only if every step succeeded. A failed run leaves the previous site online.

No secret is needed. The app's `SECRET_KEY` and `ADMIN_API_TOKEN` are random for each run and masked in the logs.

### One-time setup

```bash
git init -b main && git add -A && git commit -m "PSX Intelligence"
gh auth login                       # your GitHub account
gh repo create psx-intelligence --public --source . --push
gh api -X POST "repos/{owner}/{repo}/pages" -f build_type=workflow   # Pages source: GitHub Actions
gh workflow run "Publish website"   # first build (~5 minutes)
```

The site appears at `https://<user>.github.io/psx-intelligence/`.

- **Repository name:** if you use another name, nothing changes. The base path defaults to `/<repository name>`.
- **Root address:** for a custom domain or a `<user>.github.io` repository, set the repository variable
  `SITE_BASE_PATH_EMPTY=true`.
- **Watchlist:** to change the stocks without editing code, set the repository variable `PSXDATA_SYMBOLS`.
  Research the corporate actions first (see "Adding a symbol" in `docs/operations.md`).

### Build locally (preview, or if PSX blocks GitHub's servers)

```bash
.venv/bin/flask --app wsgi site export --out build/site --base-path /psx-intelligence
mkdir -p /tmp/preview && ln -sfn "$PWD/build/site" /tmp/preview/psx-intelligence
python3 -m http.server 8090 --directory /tmp/preview      # open http://127.0.0.1:8090/psx-intelligence/
```

If the PSX Data Portal refuses GitHub's runners, the build fails and the last good site stays online.
To publish anyway, use a local build:

1. Build locally with the command above.
2. Push `build/site` to a `gh-pages` branch.
3. Set Pages → Source to "Deploy from a branch".

### Things to know

- **Public repository.** GitHub Pages on a free account needs a public repository, so the code becomes public.
  `.env`, `.local/`, `build/`, `logs/`, `artifacts/` and the licensed `data/reference/` are git-ignored and
  never committed.
- **Data terms.** The site republishes prices derived from the PSX Data Portal, whose terms restrict
  automated access and redistribution, and Google News headlines (titles and links only). Publishing
  them on a public website goes beyond private research use. Check the terms, or get permission, before
  you rely on it.
- **Schedule pauses.** GitHub pauses scheduled workflows in public repositories after 60 days without
  activity. Re-enable it from the Actions tab, or push a commit.
- **News sentiment.** The CI build uses `lexicon_v1`, because FinBERT needs ~1.7 GB of ML dependencies.
  Headlines with a stored FinBERT tone exist only in the server edition's database.

## Server edition

`compose.yaml` runs PostgreSQL, Redis, psxdata-api, migrations, the web app (gunicorn), a worker and the
scheduler:

```bash
cp .env.example .env   # set SECRET_KEY, ADMIN_API_TOKEN, POSTGRES_PASSWORD
docker compose up --build
```

Before exposing it publicly:

- Put it behind TLS with a reverse proxy, and set `TRUSTED_PROXY_COUNT=1`.
- Keep `APP_ENV=production`. This turns on HSTS and `/ops` basic auth, and requires strong secrets.
- Review the rate limits (`NEWS_SEARCH_*`).
