# Working on socialq

```bash
pip install -e ".[dev]"

docker run -d --name socialq-pg \
  -e POSTGRES_USER=socialq -e POSTGRES_PASSWORD=socialq -e POSTGRES_DB=socialq \
  -p 55432:5432 postgres:16-alpine

python -m pytest
```

Tests run against that container (override with `TEST_DATABASE_URL`) and create
a throwaway `socialq_test` database. They use a real Postgres deliberately: the
claim query is `FOR UPDATE SKIP LOCKED` and no in-memory substitute reproduces
it.

Migrations are numbered `.sql` files in `socialq/migrations/`, applied in order
by `socialq migrate`. Add, never edit.

## Commands

The runtime only supplies a clock. Every job is a CLI command, so the host is
interchangeable:

```
socialq worker      # claim due work, publish, record        (§6)
socialq reconcile   # resolve in_flight, refresh tokens      (§7, §8.3)
socialq prune       # delete published media from R2         (§9)
socialq doctor      # check everything a publish needs, without publishing
socialq status      # what is in the queue right now
socialq migrate     # apply pending migrations
```

`doctor` is the one to run after any deploy. It catches the failures that
otherwise surface as a post silently not going out: an unreadable secret, an
expiring token, R2 credentials that were never set.

## Deploying

### Appwrite (current)

Free through the GitHub Student Pack, 1-minute cron, no card required.

```bash
npm install -g appwrite-cli
appwrite client --endpoint "$APPWRITE_ENDPOINT" \
  --project-id "$APPWRITE_PROJECT_ID" --key "$APPWRITE_API_KEY"

./scripts/deploy_appwrite.sh          # all four functions
./scripts/deploy_appwrite.sh worker   # just one
```

Then prove it end to end from inside the runtime, where the environment differs
from a laptop:

```bash
appwrite functions create-execution --function-id doctor --async false
```

Schedules: `worker` every minute, `reconcile` every ten, `prune` daily at
04:00 UTC, `doctor` manual.

Two things that will bite:

- Appwrite caps **synchronous** executions at 30 seconds regardless of the
  function's configured timeout. Scheduled runs are asynchronous and get the
  full timeout, so testing a real publish by hand needs `--async true`.
- Function **variable IDs are unique per project, not per function**, which is
  why the deploy script prefixes them with the function name.

`modal_app.py` is kept for the day a card is available — Modal is the same
commands on a different scheduler, at 1-minute granularity and $0 within its
free credits.

### Secrets

Credentials are not environment variables in production. They live encrypted in
the `secrets` table — §8.2.1 says `api_key_ref` points at a store rather than
holding the secret, and §8.3's token refresh must be able to write the new
token back, which the environment cannot.

```bash
socialq keygen              # once: generate SOCIALQ_SECRET_KEY
socialq secrets --push      # copy credential secrets from .env into the store
socialq secrets             # list what is stored, and prove it decrypts
```

`SOCIALQ_SECRET_KEY` belongs in the runtime's own secret manager and **never**
in the database it protects — that separation is the entire point. Losing it
means re-authorising every account.

## Accounts

```bash
python scripts/register_account.py --platform instagram \
  --handle @deployedunsafe --token-ref DEPLOYEDUNSAFE_IG_TOKEN
```

Verifies the credential against the platform before writing anything. Accounts
are registered **disabled** unless `--enable` is passed: §14 warms new accounts
by hand for about a week, because an account that suddenly starts posting on a
schedule is the shape platforms throttle. An established account can go live
immediately.

## Environment

| variable | needed by |
|---|---|
| `DATABASE_URL` | everything |
| `SOCIALQ_SECRET_KEY` | the encrypted secret store |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | media upload |
| `R2_PUBLIC_BASE_URL` | media URLs — a custom domain, never `r2.dev` (§9) |
| `APPWRITE_ENDPOINT`, `APPWRITE_PROJECT_ID`, `APPWRITE_API_KEY` | deploying |
| `<CREDENTIAL>_TOKEN` | seeding the secret store, once |

Platform tokens are only needed in the environment for the initial
`socialq secrets --push`. After that the store is the source of truth, and a
refreshed token is written there rather than back to `.env`.
