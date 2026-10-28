# AGENTS.md

## What this is
Single-file, zero-dependency server (`app/server.py`, ~822 lines, Python stdlib only) wrapping `yt-dlp` (which shells out to `ffmpeg` for stream merging/transcoding — server.py itself never calls ffmpeg) behind a local web UI. Frontend in `app/static/` is vanilla HTML/CSS/JS — no build step, no bundler, no venv, no pip/npm packages. "Zero external dependencies" is an explicit project constraint (README badge); do not add pip dependencies.

## Commands
- Tests (fast, no network, no packages): `python3 -m unittest discover -s tests -p "test_*.py" -v`. CI matrix is 3.10–3.12; code runs fine on newer (3.14 verified).
- Run server: `python3 app/server.py` → http://127.0.0.1:5050 (flags: `--host`, `--port`, `--downloads-dir`). Cwd doesn't matter; static dir is resolved from `__file__`.
- Docker: `docker compose up -d` → port 5050, downloads land in `./downloads` (compose mounts `./downloads:/downloads`). Container runs as uid 1000; the `--host 0.0.0.0` binding comes from the Dockerfile `ENTRYPOINT`, not compose.

## Runtime prerequisites
- `yt-dlp` + `ffmpeg` must be installed on the host for inspect/download to work. The server starts fine without them and only fails at request time. Binary resolution (`find_yt_dlp()`): hardcoded paths first (`/opt/homebrew/bin`, `/usr/local/bin`, `~/.local/bin`), then `PATH`.

## Testing the API manually (curl gotchas)
- Every request passes `_is_request_authorized()`: `Host` must be `127.0.0.1`/`localhost` (loopback-only by default) with the correct port; `Origin`, if sent, must be a local origin with the same port. Anything else → 403. `Origin` and `Sec-Fetch-Site` are only *sent by browsers* — plain curl with no `Origin` header bypasses that layer by design.
- **Bind-host rule**: the `--host` value is never auto-whitelisted (`resolve_allowed_hostnames()` in `app/server.py`). Exposing beyond loopback requires `--allow-hosts <names>` (comma-separated) and prints a startup warning; LAN clients otherwise get 403. For Docker LAN access, add opt-in args via compose `command:` — the `ENTRYPOINT` alone is no longer enough.
- All `POST /api/*` require `Content-Type: application/json` → otherwise 415. Curl example: `-H 'Content-Type: application/json' -d '{"url": "..."}'`.

## State model
- All state is in-memory module globals: `TASKS`, `TASKS_LOCK`, `PORT`, `ALLOWED_HOSTNAMES`, `YT_DLP_BIN`. No persistence — restarting the server wipes tasks. Downloads run in daemon threads executing yt-dlp subprocesses; progress comes from parsing yt-dlp's `PROGRESS:` stdout template via regexes in `run_download_task`.
- `app/static/app.js` polls `GET /api/tasks` every 1.2 s and renders task dict fields directly: changing task fields requires updating both sides. The `process` key is stripped from `/api/tasks` responses.
- CORS reflects only verified local `Origin` (no wildcard) — keep UI calls same-origin.

## Tests
- `inspect_url` and `run_download_task` (incl. the `PROGRESS:` regex parser) are covered by mocked-subprocess tests: `TestInspectUrlSubprocess` fakes `subprocess.run`, `TestRunDownloadTaskSubprocess` fakes `Popen` with canned `PROGRESS:`/`ERROR:` lines — no network, no yt-dlp install needed. Real CDN behavior (format availability, timeout flags) is still only verifiable manually against a running server.
- Tests mutate module globals and restore them in `finally` (`TestTaskRegistryBoundedStore` sets `MAX_TASKS = 5`). Handler tests construct `MediaHubHandler.__new__(server.MediaHubHandler)` with a mock headers dict, and `TestSecurityDefenses` pins `server.PORT = 5050`. Follow this pattern for new tests.

## Repo hygiene
- `docs/` and `career_transformation_plan.local.md` are git-**ignored** personal notes (AI chat transcripts, yt-dlp cheatsheets, career/meme drafts), not project documentation. Don't treat them as sources of truth and don't try to commit them. Same for `*.local.md`, `*.plan.md`, `private_docs/`, `.notes/`.
- Git history was deliberately rebuilt (orphan branch) to purge an earlier 2-commit chain containing personal transcripts; the old chain is unrecoverable. Never "restore" deleted files (e.g. `docs/video_downloader`) into git.
- Origin: `https://github.com/DevHacksHitman/hundjager.git` (public); profile repo: `DevHacksHitman/DevHacksHitman` (public, README only). Commit style is conventional: `type(scope): summary` — e.g. `feat(core):`, `test(server):`, `docs(readme):`. Push `main` only; never `--mirror` or `--tags`; never push from a Kilo worktree-pool worktree.
- Commit email: always the GitHub noreply (`218126545+DevHacksHitman@users.noreply.github.com`, set repo-local) — never the personal gmail; history was rewritten to purge it, and GitHub blocks email-exposing pushes.
- Pushes are performed by the orchestrator, not implementers (local-only briefing: `private_docs/handoff-sprint2.md` + `private_docs/backlog.md`, git-ignored and reference-only).
- Sensitivity scans must cover ALL refs and worktree HEADs — `git log -p --all`, not plain `git log -p`, and re-verify from a fresh clone after any push.
- Pre-push sensitivity gate lives in `githooks/pre-push` (blocking, safe-fails on scan error). After ANY fresh clone: `git config core.hooksPath githooks`, or protection is lost. Modifying its pattern list: every needle must break up its own literal with a character class (e.g. `youpo[r]n`, `sl[u]t`) — a plain literal makes the hook's own source match the scan and every push self-blocks forever (found live on the first push).

## Invariants (regression-tested; don't break)
- **Loopback-only allowlist**: the bind host is never auto-added to `ALLOWED_HOSTNAMES`; `resolve_allowed_hostnames()` is the single decision point and `TestAllowlistResolution` guards it. Any change must keep the explicit `--allow-hosts` opt-in + startup warning.
- **Cancel always wins**: the worker's spawn transition (status → `downloading` + process store) is atomic under `TASKS_LOCK`, `/api/cancel` decides under the same lock, and a cancelled task cannot be flipped to `downloading`/`finished` afterward. `TestRunDownloadTaskSubprocess` guards this — never re-split those lock sections.

Done criterion: unittest suite green. There is no lint/typecheck config.
