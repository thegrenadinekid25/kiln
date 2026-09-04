# kiln

**Purpose**: Public, free, read-only map of Earth's hottest ground — a most-recent
satellite land-surface temperature view plus an all-time archive accumulated from
the pipeline's own record (satellite-only by decision 2026-08-31). No accounts, no
client writes. A Tortoise studio product.

**Names**: public name is "The Hottest Place in the World"
(thehottestplaceintheworld.bytortoise.com, decided 2026-09-01). "Kiln" is the
internal codename only — repo, Troth project, Vercel project, bucket names. Do not
use Kiln in user-facing copy.

## Architecture

- `web/` — Vite + React + TS + Zustand + CSS Modules, maplibre-gl with a free
  no-token basemap. Read-only Supabase fetches (anon SELECT). Bun toolchain.
- `ingest/` — Python 3.12 pipeline (the portfolio's JS-default is deliberately
  broken here: HDF4 satellite swath parsing needs numpy/pyhdf). Downloads NASA
  LANCE NRT MODIS LST granules (MOD11_L2 Terra + MYD11_L2 Aqua), applies QC
  bitmasks, computes daytime max LST per 1-degree tile, upserts hot tiles
  (>= 40 C, plus global top 10).
- `supabase/` — migrations + seed for the `kiln` schema in the SHARED tortoise
  Supabase project (`wdvguesfxcxxatzpirvy`, schema-per-app rule). Tables:
  `record_holders` (curated, cited), `lst_readings` (daily tile maxima),
  `ingest_runs` (staleness bookkeeping). RLS: public SELECT only; the pipeline
  writes with service_role. The schema is exposed to the Data API additively via
  Management API PATCH — NEVER `supabase config push` against this shared project.
- `.github/workflows/ingest.yml` — daily cron (09:00 UTC), secrets
  `EARTHDATA_TOKEN` + `SUPABASE_SERVICE_KEY`.

## Data honesty rules

- SATELLITE-ONLY (decision 2026-08-31): everything Kiln shows is derived from the
  same MODIS land-surface-temperature lineage as the live layer. Station
  air-temperature records (Death Valley, Dallol) are deliberately out of scope;
  do not reintroduce them.
- Every record carries a citation one tap away. No record ships without a citable
  source (NASA-published or peer-reviewed satellite LST).
- Timestamps visible on every live reading; cloud gaps shown as gaps, stale data
  marked stale, never presented as fresh.

## Design

ATLAS PLATE theme (decision 2026-09-02, this product's own theme, not shared with sibling products): mid-tone
sage plate ground (#9AA396, basemap patched to match in Map.tsx), light card
surfaces (#EFEEE6), heat sienna as the ONLY saturated color family, Survey Sheet
type (Archivo Narrow condensed-caps display / Archivo UI / IBM Plex Mono numerals).
Heat ramp #C9B896->#6E3410 lives in THREE places that must change together:
web/src/tokens.css, LiveLayer.tsx step expression, ingest tile_png.py palette.
Heat never gets alarmist emergency-red. Read `~/projects-local/USABILITY-CANON.md`
before touching any surface.

## Infrastructure cost

Expected monthly cost: $0 (shared Supabase schema, GitHub Actions cron on the
private repo's free minutes, Vercel free static hosting).

## Troth: structural map of this codebase

Troth is a structural map of the codebase, not a project tracker. Specs are areas of the
code — they persist as long as that area exists. Before creating a new spec, find where
the work belongs in the existing structure. File bugs and features in the spec that owns
the affected code; find before you create. Foundations (constitution, design bible,
tokens) cross-cut every spec automatically — they are not specs.

## Session opener

At the start of a work session on this project (not for one-off questions), orient with the CLI:

- `troth briefing` — last decision, current drift, what changed recently
- `troth next` — ready tasks in priority order
- `troth foundations show` — constitutions, design bible, tokens (worth pulling before design/UI work)

(In claude.ai web conversations only, the `start_session` MCP tool composes all three in one call.)

## Troth Task Management

### MANDATORY: Always Update Task Status

When you finish ANY troth task, you MUST run one of these commands:

```
troth task done <name> --notes "Changed: <files>. Decisions: <choices>. Watch: <notes>."
troth task fail <name> --notes "Blocked by: <cause>. Tried: <approaches>. Next: <suggestion>."
troth task cancel <name> --notes "why this task is no longer needed"
```

**This is not optional.** Every task must end with a status update.

### Work Loop

1. `troth next` -- see ready tasks
2. `troth task start <name>` -- claim the task
3. `troth task show <name>` -- read objective, acceptance criteria, file scope
4. Do the work (only modify files listed in file_scope)
5. Run all verification commands from the task
6. `troth task done <name> --notes "..."` or `troth task fail <name> --notes "..."`
7. `troth next` -- repeat

For multi-task execution: `troth execute --spec <name>` auto-plans and runs conflict-free batches. Use `--dry-run` to preview.

### Key Rules

- **file_scope**: Only modify files listed in the task.
- **verification**: All verification commands must exit 0 before marking done.
- **--notes are mandatory**: Include what changed, decisions made, and watch-outs.
- **guardrails**: Prohibitions. Violating a guardrail = task failure.

### Parallel Execution (MANDATORY)

**NEVER dispatch parallel agents without `troth next --parallel N` first.** This checks file scope overlaps and dependency chains.

- Only tasks returned by `troth next --parallel` are safe to run simultaneously.
- Tasks with unmet dependencies MUST wait.
- After each batch completes, run `troth next --parallel N` again.

### Requests (Intent Tracking)

Before starting work on a bug, idea, or initiative, create a request to track intent:

1. `troth request create "<title>" --description "<why>"` -- capture the intent
2. `troth request link <id> --spec <name>` or `--task <spec/task>` -- link work items
3. `troth request resolve <id> --outcome resolved --learning "<what we learned>"`
4. `troth request search "<query>"` -- check for existing requests before creating duplicates

Requests sit above specs and tasks. A single request may spawn multiple specs/tasks, or be resolved by existing work.

### Spec Hygiene

Before creating a spec, run `troth spec list`. Every spec must own files no other spec owns.
- Enhancement to owned directory -> task on owning spec, not a new spec
- Cross-cutting concern -> bundle or finding, not a spec
- Future idea with no code -> `troth idea`, not a spec
- Max 2 new top-level specs per session

### Acceptance Criteria

When creating tasks, write **structured acceptance criteria** for anything
mechanically verifiable. Format: subject (file/endpoint/table), property
(contains_string/file_exists/http_status), expected (value), method (shell
command that checks it). `troth test` runs these mechanically — no interpretation.

Plain English criteria are for things requiring human judgment only.

**Production criteria**: Tasks with migrations or deployments MUST have
production criteria (environment: "production"). These verify the deployed
state — table exists on prod DB, endpoint returns 200, page loads.
- `local-verified` = code is correct (local tests pass)
- `passed` = works in production (production criteria also pass)

Run `troth test <task>` for local, `troth test <task> --staging` after staging deploy,
`troth test <task> --production` after production deploy.

Run `troth guide` for full SAP format reference and examples.

### Environment Pipeline

**NEVER deploy directly to production.** All changes must go through staging first:

1. `troth deploy --staging` — deploy to staging, verify health
2. `troth test --staging --wave` — run all pending staging criteria
3. `troth promote` — deploy to production, run production criteria, mark tasks passed

**Three environments**: local (dev machine), staging (preview deploy), production (live).
- Local: typecheck, unit tests, build, file checks
- Staging: Playwright browser tests, API integration, E2E tests
- Production: health checks, smoke tests, migration verification

Playwright tests run against **staging**, not localhost or production.

After a wave of tasks completes: deploy-staging -> verify-staging -> promote -> verify-production.

Configure environments in `.troth/environments.json`. Run `troth env list` to check config.

### CLI Quick Reference

```
troth next                          ready tasks (sequential)
troth next --parallel N             conflict-free tasks for parallel dispatch
troth tasks <spec>                  list tasks for a spec
troth task show/start/done/fail     task lifecycle
troth spec list/show/new            spec management
troth execute --spec <name>         auto-execute a spec's tasks
troth orchestrate --spec <name>     full lifecycle: plan, execute, verify
troth status                        all task statuses
troth decompose <spec>              decompose spec into tasks
troth bug/idea <message>            file findings
troth request create/list/show      request lifecycle
troth request link/resolve/search   link work, resolve, search
troth learnings                     learnings from resolved requests
troth deploy --staging              deploy to staging
troth test --staging --wave         batch staging verification
troth promote                       staging -> production pipeline
troth env list                      show configured environments
troth env check                     ping health endpoints
```

Run `troth help` or `troth <command> --help` for full details on any command.

### IMPORTANT: Always Use the CLI, Never MCP

**Claude Code must ALWAYS use `troth` CLI commands via the shell.** Do NOT use Troth MCP tools (e.g. `mcp__troth__get_briefing`, `mcp__troth__file_bug`, `mcp__troth__list_specs`, `mcp__troth__create_task`). The MCP tools are for claude.ai web conversations only.
