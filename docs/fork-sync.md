# Syncing this fork with upstream

Runbook for keeping the `self-host` branch of this fork on top of
`upstream/main` (`mutonby/openshorts`) and pushing it to `origin`.

Remotes this assumes:

| Remote     | Points to                  |
|------------|----------------------------|
| `origin`   | this fork                  |
| `upstream` | `mutonby/openshorts`       |

## 1. Rebase onto upstream

```bash
git fetch upstream
git rebase upstream/main
```

Resolve any conflicts, `git add` the files and `git rebase --continue`.

## 2. See what changed

`ORIG_HEAD` is the branch as it was before the rebase:

```bash
git diff --stat ORIG_HEAD HEAD
```

## 3. Refresh the containers

Decide from the files in that diff:

| Files changed                                   | Action                                        |
|-------------------------------------------------|-----------------------------------------------|
| `Dockerfile`, `requirements.txt`                | `docker compose up -d --build backend`        |
| `dashboard/package.json`, `dashboard/Dockerfile`| `docker compose up -d --build frontend`       |
| `render-service/`                               | `docker compose up -d --build renderer`       |
| `watcher/`                                      | rebuild the watcher with its compose file     |
| only `*.py`                                     | `docker compose restart backend`              |
| only `dashboard/src/`                           | nothing: Vite hot-reloads (refresh the tab)   |
| only docs / tests                               | nothing                                       |

Why: the backend mounts the repo at `/app`, but `serve.py` runs uvicorn
without `--reload`, so Python changes need a restart. The frontend mounts
`dashboard/` and runs the Vite dev server with HMR. The renderer bakes its
code into the image.

Check that no job is running before restarting the backend: a restart
drains the queue (drain-cancelled jobs resume, but it is better not to cut
one mid-render).

## 4. Check before pushing

Smoke-test one short job, then run what CI runs:

```bash
pytest tests/ -v
```

```bash
cd dashboard && npm run build && npm run lint
```

Make sure only intended files are staged. Local-only files (`.atl/`, `.nv/`,
`.env*`, `docker-compose.override.yml`) must never be committed: the repo is
public. To hide them from `git status` without touching `.gitignore`, list
them in `.git/info/exclude` (local, never pushed).

## 5. Confirm nothing is lost

After a rebase `git status` shows the branch as both ahead and behind
`origin/self-host`: the "behind" commits are the pre-rebase copies of your
own commits. Confirm they match the rebased ones:

```bash
git range-diff @{u}...HEAD
```

Every one of your commits should show `=` (or `!` if you resolved a
conflict in it). Commits only on the right side (`-: ------- > N:`) are the
new upstream ones. A commit only on the left side would be lost by the
push: stop and investigate.

## 6. Push

The rebase rewrote history, so the push must be forced. Always with lease:

```bash
git push --force-with-lease origin self-host
```

`--force-with-lease` refuses to overwrite the remote if it holds commits you
have not fetched; plain `--force` would silently discard them.

## 7. After pushing

If GitHub Actions is enabled on the fork, wait for a green run. With two
remotes, `gh` needs to know which repo to use: run `gh repo set-default`
once and pick the fork.

```bash
gh run watch $(gh run list -b self-host -L1 --json databaseId -q ".[0].databaseId") --exit-status
```

On any other machine with `self-host` checked out, do **not** `git pull`
(it would merge the old and new history). Check it has nothing unpushed,
then:

```bash
git fetch origin && git reset --hard origin/self-host
```
