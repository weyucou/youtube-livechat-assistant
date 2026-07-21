# jawed (youtube-livechat-assistant) — deploy & smoke runbook

Deploy procedure for the jawed Flask API (flask-smorest) to AWS Lambda via Zappa,
plus the deployed-environment smoke test. Addresses #18.

> ## ⚠️ Deploy blocker — read first
>
> jawed persists state in **SQLite files under a relative `data/` directory**
> (`jawed/definitions.py`: `DATA_DIR = "data"`, `MASTER_DB_NAME = "master.db"`;
> `jawed/database.py`: `sqlite3.connect(...)`). **This does not work on Lambda:**
> the deployment package is a **read-only** filesystem, so opening/creating the
> master DB fails, and even `/tmp` (the only writable path) is **ephemeral and
> per-instance** — data is lost on cold start and not shared across concurrent
> invocations. Any write path (`POST /auth/register`, `POST /channels/`) will 500,
> and a channel registered on one instance is invisible to the next.
>
> **A real deployment is blocked until state moves off local SQLite** — that is the
> intent of #17 (provision `jawed-state` / `jawed-events` S3 buckets) and the
> DynamoDB direction. The steps below document how the Zappa deploy itself works so
> the procedure is ready the moment persistence is fixed; the smoke test's
> `accepting-requests` check will fail loudly against the current SQLite build,
> which is the signal that #17 is still outstanding.

## Region & account note

`zappa_settings.json` targets **`ap-northeast-1`** (both `dev` and `prod`). This is a
**personal / monkut** deployment, not the weyucou org dev account — the `weyucou-dev-agent`
profile (us-west-2, account `610714125210`) **cannot** deploy this. Whoever deploys must
have credentials for the account that owns the `youtube-livechat-assistant-zappa` bucket in
`ap-northeast-1`.

## Prerequisites

- AWS credentials for the target account/region (`ap-northeast-1`), a role with the Zappa
  permissions (Lambda, API Gateway, IAM, S3, CloudFormation) — see #18.
- The Zappa deploy bucket `youtube-livechat-assistant-zappa` (Zappa creates it on first
  deploy if the caller has `s3:CreateBucket`).
- `uv` and Python 3.14.
- Runtime configuration (set as Lambda env via Zappa `environment_variables` or
  `aws lambda update-function-configuration`; **never commit real values**):

  | Variable | Purpose |
  |----------|---------|
  | `JWT_SECRET` | Signs/verifies the JWT the API issues (`@jwt_required`); the app cannot authenticate anyone without it |
  | `LOG_LEVEL` | `INFO` (dev) / `WARNING` (prod) — already set in `zappa_settings.json` |
  | `REGION` | AWS region for runtime SDK calls |
  | YouTube API key / OAuth client id+secret | Live-chat polling + channel OAuth (see `docs/onboarding.md`) |

## Deploy

```bash
uv sync --extra api --no-dev      # --no-dev keeps the package under Lambda's 250 MB unzipped limit
uv run zappa deploy dev           # first deploy for the stage
uv run zappa update dev           # subsequent deploys
```

Set the runtime secrets (not tracked in `zappa_settings.json`):

```bash
aws lambda update-function-configuration \
  --region ap-northeast-1 \
  --function-name youtube-livechat-assistant-dev \
  --environment "Variables={JWT_SECRET=<secret>,REGION=ap-northeast-1,LOG_LEVEL=INFO}"
```

Zappa prints the API Gateway invoke URL on success; capture it for the smoke test.
`GET /openapi/spec/yaml/` serves the live OpenAPI spec once deployed.

## Verify (smoke test)

```bash
API_URL=$(uv run zappa status dev --json | python -c "import sys,json;print(json.load(sys.stdin)['API Gateway URL'])")
python validation/smoke_test.py --api-url "$API_URL"
```

Checks DNS/TLS, `GET /health`, the anonymous `401` boundary on `GET /channels/`, and
`GET /channels/<unknown>/accepting-requests` → `404` (**not** `500`). That last check
exercises a master-DB read on Lambda: while the SQLite blocker above is unresolved it
returns `500`, which is the smoke test telling you persistence is not yet Lambda-safe.

## Rollback / teardown

```bash
uv run zappa rollback dev -n 1     # revert to the previous deployed version
uv run zappa undeploy dev          # remove the Lambda + API Gateway for the stage
```

The Zappa deploy bucket and any state buckets (#17) are not removed by `undeploy` —
empty and delete them separately if tearing the stage down completely.
