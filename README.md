# Devin Remediation Control Plane

> Event-driven automation that converts GitHub maintenance issues into Devin sessions, pull requests, and measurable remediation outcomes.

## Problem

Large engineering repositories accumulate maintenance and reliability issues
faster than humans can triage them. Traditional automation can create tickets,
notify on-call, and route reviews — but it cannot turn a ticket into a
validated pull request. This service uses Devin as the autonomous remediation
worker behind a thin policy + reporting layer:

- A GitHub issue tagged `devin-autofix` is the **request for work**.
- The service decides whether the issue is **eligible** and what **kind** of
  bug it is.
- Devin does the actual code change and opens the PR.
- The service tracks **what came in, what went out, and how long it took**.

## Architecture

```text
GitHub Issue Event
        |
        v
FastAPI Webhook / Simulation Endpoint
        |
        v
Policy + Issue Classification
        |
        v
Devin API Session
        |
        v
Devin opens PR in target repo
        |
        v
SQLite task tracking + /tasks + /metrics
```

Four layers, intentionally:

1. **Intake** — `/webhooks/github` and `/simulate/issue/{n}`
2. **Policy** — `devin-autofix` label gate + `classify_issue` heuristics
3. **Execution** — `devin_client.create_devin_session` against the v3 API
4. **Reporting** — `/tasks`, `/tasks/{id}/sync`, `/metrics`

## Target Repository

The service points at a copied Apache Superset repo:

<https://github.com/csanichar/cognition>

This repo holds the seeded bug issues that Devin will remediate. The control
plane never modifies that repo directly — Devin does, through its own GitHub
integration.

## Issues

The demo includes three seeded Superset bugs:

1. **Stale chart references crash dashboard import**
   `timed_refresh_immune_slices` / `expanded_slices` raise `KeyError` when an
   id_map omits a deleted chart id.
2. **Missing native filter dataset UUID raises raw KeyError**
   A native filter target whose `datasetUuid` is absent from `dataset_info`
   produces a bare `KeyError` instead of an import-specific error.
3. **DrillDetailPane cache limit can become NaN**
   `cachePageLimit` is computed from `SAMPLES_ROW_LIMIT` and becomes
   non-finite when the env value is undefined.

The classifier in `app/prompts.py` routes each issue to issue-specific
acceptance criteria that get embedded in the Devin prompt.

## Setup

1. **Create `.env`** by copying `.env.example` and filling in the secrets:
   ```bash
   cp .env.example .env
   ```
2. **Install dependencies** (Python 3.12+ recommended):
   ```bash
   pip install -r requirements.txt
   ```
3. **Run FastAPI** (see "Running Locally" below).
4. **Run ngrok** to expose port 8000 publicly:
   ```bash
   ngrok http 8000
   ```
5. **Configure the GitHub webhook** on `csanichar/cognition` to point at
   `https://YOUR_NGROK_URL/webhooks/github` for the `Issues` event, with the
   same secret you placed in `GITHUB_WEBHOOK_SECRET`.

## Environment Variables

| Variable                | Required | Purpose                                                                   |
|-------------------------|----------|---------------------------------------------------------------------------|
| `DEVIN_API_KEY`         | yes      | Devin **service user** token for the v3 API. Not a legacy personal key.   |
| `DEVIN_ORG_ID`          | yes      | Your Devin organization ID — used in the `/v3/organizations/{id}/...` URL.|
| `GITHUB_TOKEN`          | yes      | Read-only token for issue/PR fetches. Service never pushes code.          |
| `GITHUB_OWNER`          | yes      | Owner of the target repo (e.g. `csanichar`).                              |
| `GITHUB_REPO`           | yes      | Repo name (e.g. `cognition`).                                             |
| `GITHUB_WEBHOOK_SECRET` | optional | HMAC secret for `X-Hub-Signature-256`. **Required in production.**        |
| `TARGET_REPO_URL`       | yes      | Full clone URL passed to Devin in the session payload.                    |
| `TARGET_BASE_BRANCH`    | optional | Branch Devin should branch from and PR into. Falls back to `main`.        |

## Running Locally

```bash
uvicorn app.main:app --reload --port 8000
```

Then hit:

```bash
curl http://localhost:8000/
```

You should see `{"status":"ok","service":"devin-remediation-control-plane"}`.

## Docker

```bash
docker compose up --build
```

The compose file mounts `./data` so the SQLite database survives container
restarts.

## Triggering via Simulation

When you don't want to deal with ngrok:

```bash
curl -X POST http://localhost:8000/simulate/issue/ISSUE_NUMBER
```

This fetches the issue from GitHub, runs the same eligibility + classification
+ Devin pipeline as the webhook, and records the resulting task.

## Triggering via GitHub Webhook

Configure the webhook on the target repo:

- **Payload URL**: `https://YOUR_NGROK_URL/webhooks/github`
- **Content type**: `application/json`
- **Secret**: same value as `GITHUB_WEBHOOK_SECRET`
- **Events**: `Issues`

The endpoint listens for `opened`, `labeled`, and `edited` actions and only
proceeds when the issue carries the `devin-autofix` label.

## Observability

Endpoints:

```text
GET  /tasks
GET  /tasks/{task_id}
POST /tasks/{task_id}/sync
GET  /metrics
```

`/metrics` is the answer to:

> If I were an engineering leader, how would I know this is working?

The system shows whether eligible issues become Devin sessions, whether those
sessions produce PRs, which tasks are active or failed, and how long
issue-to-PR conversion takes. `by_issue_kind` slices the same numbers per
remediation workflow so you can see whether one workflow is failing more often
than the others.

## Demo Flow

1. Show a GitHub issue on `csanichar/cognition` labeled `devin-autofix`.
2. Trigger remediation, either with `curl -X POST .../simulate/issue/{id}` or
   by adding the label in the GitHub UI to fire the real webhook.
3. `GET /tasks` — task is created in `session_created` state.
4. The Devin session URL is recorded on the task; open it.
5. Wait for Devin to open the PR, then `POST /tasks/{id}/sync`.
6. `GET /tasks/{id}` — task is now `pr_opened` (or `completed`) with `pr_url`.
7. `GET /metrics` — dashboards show 1 eligible task, 1 session, 1 PR.

## Production Extensions

Out of scope for the take-home, but the obvious next steps:

- Real CI status integration (poll GitHub Checks API per PR).
- Slack notifications on task lifecycle events.
- Approval gates by risk type (e.g. importer changes require human ack).
- ACU budgets sized by issue severity, not a flat cap of 5.
- Stronger deduplication keyed on issue + workflow, not just active status.
- Database upgrade from SQLite to Postgres for multi-instance deployments.
- Webhook replay protection (delivery-id cache + timestamp check).
- Use `structured_output_required: true` and parse Devin's structured result.
- Ingest PR review comments to feed back into Devin sessions automatically.
