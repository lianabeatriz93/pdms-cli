# pdms-cli

Interactive CLI to run PDMS services locally. Pick a development user and a database, and `pdms` runs
`poetry lock && poetry install` and starts `uvicorn main:app` with `DEV_*`, `DEVELOPMENT_MODE`, `LOGGING_LEVEL` and
`DB_PG_CONNECTION_STR` already set. It can run several services in the background, show their logs, detect broken
reloads, start groups of services together and generate VS Code debug configurations.

## Installation

macOS / Linux:

```sh
curl -LsSf https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.sh | sh
```

Windows (PowerShell):

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.ps1 | iex"
```

The installer sets up [uv](https://docs.astral.sh/uv/) if it is missing (uv also provides a suitable Python), installs
the latest release of `pdms`, adds it to the `PATH` and checks that it starts. Open a new terminal afterwards if it
says so. Both installers are also attached to every [release](https://github.com/lianabeatriz93/pdms-cli/releases),
and accept `PDMS_VERSION=0.2.0` to install a specific version.

Without the installer, with uv already installed:

```sh
uv tool install https://github.com/lianabeatriz93/pdms-cli/releases/download/v0.2.0/pdms_cli-0.2.0-py3-none-any.whl
```

| | |
| --- | --- |
| Check the version | `pdms --version` |
| Update | `pdms self-update` (`--check` only tells whether there is a new one; `--version X` for a specific one) |
| Pre-releases | Installers: `PDMS_PRERELEASE=1` (e.g. `curl ... \| PDMS_PRERELEASE=1 sh`, or `$env:PDMS_PRERELEASE = "1"` on Windows). `pdms self-update --pre`; once you run an alpha, `self-update` keeps following alphas |
| Uninstall | `uv tool uninstall pdms-cli` (the configuration in `~/.config/pdms` is kept) |

`pdms` tells you when a new version is published: it checks GitHub at most once a day, in the background while a
command runs, and shows `⬆ New pdms version available: … · update with: pdms self-update` when the command ends (or
when the menu opens), at most once a day. Stable installs are told about stable releases, alpha installs about alphas
too. It never checks without an interactive terminal, in CI, from an editable checkout, with
`PDMS_NO_UPDATE_CHECK=1`, or when disabled in `pdms config` (`update_check = false`).

### Platforms

Linux, macOS and Windows, with Python 3.10+ (tested in CI on the three systems with Python 3.10 and 3.12).
Running services also needs [Poetry](https://python-poetry.org/docs/#installation) in `PATH`, as PDMS uses it.

| | Linux / macOS | Windows |
| --- | --- | --- |
| Configuration | `~/.config/pdms/config.toml` | `%USERPROFILE%\.config\pdms\config.toml` |
| State (instances, logs, caches) | `~/.local/state/pdms/` | `%USERPROFILE%\.local\state\pdms\` |

`PDMS_CONFIG` and `XDG_STATE_HOME` override those locations. Background services are detached from the terminal and
stopped together with their child processes (the uvicorn reloader and its workers) on every system.

`pdms doctor` checks all of this at once: pdms and its Python, Poetry and a Python 3.10/3.11 for the services, the
configuration file and its permissions, every database connection (`--no-db` to skip them), the current repo and
what the proxy needs, the ports, background instances and tab completion. Each problem comes with a hint on how to
fix it; the exit code is 1 when something is broken.

First steps:

```bash
pdms repo add ~/Code/Alivi/pdms   # register your PDMS checkout (done automatically the first time you run pdms inside it)
pdms config          # settings: language, default port, install...
pdms db add          # wizard: alias, host, port, database, user, password
pdms user add        # wizard: DEV_USER_ID, DEV_USERNAME, DEV_ROLES...
pdms                 # interactive menu
```

### Tab completion

```bash
pdms --install-completion    # then open a new terminal
```

Completes service names, instances, users, databases, stacks and languages, e.g. `pdms run lead-tp-<Tab>`,
`pdms logs <Tab>`, `pdms up <Tab>`, `pdms run -u <Tab>`.

## Repos (several PDMS checkouts)

`pdms` can work with several checkouts of PDMS (e.g. `~/Code/Alivi/pdms` and `~/Code/Alivi/pdms_v2`). One of them is
the **current repo**: its backend is where services are listed, stacks are resolved and migrations run.

```bash
pdms repo list                       # ● marks the current one; also shows how many instances run from each
pdms repo add ~/Code/Alivi/pdms_v2   # alias defaults to the folder name
pdms repo use pdms_v2
pdms repo edit pdms_v2 --migrations ~/Code/Alivi/pdms-db-migrations-v2 --remote https://<id>.execute-api.us-east-1.amazonaws.com/dev
pdms repo remove pdms                # only forgets it; nothing is deleted from disk
```

When you run `pdms` inside a registered or unregistered PDMS repo that is not the current one, it asks whether to
**switch** to it, use it **only for this command**, or **never ask again** in that repo. Without an interactive
terminal it uses the folder's repo for that command only. The first repo you use becomes the current one
automatically (an old `backend_path` setting is migrated to a repo).

Instances from different repos can run at the same time (`pdms ps` has a **Repo** column). When you switch repos and
instances of the previous one are running, `pdms` asks whether to keep them, stop them, or **restart them from the
new repo** with the same user, DB and port (services that do not exist in the new repo are left running).
Stacks store paths relative to the backend, so they work with any repo. Repos are not included in
`pdms config export`, since their paths are machine-specific.

## Language

English is the default. Spanish is also available:

```bash
pdms config language es    # or: pdms config language (asks)
pdms config language en
PDMS_LANG=es pdms ps       # one-off override
```

It is stored as `language` in the `[defaults]` section of the config.

### Importing users from a database

```bash
pdms user import -d local                 # search, then pick users with the space bar
pdms user import -d web -s ana -r TPR.Agent
pdms user import -d local --inactive -y   # every match, inactive ones included, without asking
```

Reads `public.pdms_user` (read-only) and creates a profile per user: `DEV_USER_ID` = `entity_id`, `DEV_USERNAME` =
`username`, names, and `DEV_ROLES` converted from the internal names stored in the table
(`TRANSPORTATION_PR_SUPERVISOR`) to the external ones the services expect (`TPR.Supervisor`). The mapping is read
from the current repo (`MAP_INTERNAL_ROLES` in `backend/common/core/core/settings.py` and the `UserRoleEnum` /
`UserRolePPEnum` enums, parsed without importing them), so new or renamed roles are picked up automatically; a
built-in copy is used only if the repo cannot be read. Aliases come from the email (`jane.doe@example.com` → `jane-doe`); users already imported
(same `DEV_USER_ID`) are updated in place instead of duplicated.

## Running a service

```bash
cd ~/Code/Alivi/pdms/backend/lead/lead-tp-create
pdms run                                  # asks for user, DB, port and foreground/background
pdms run -u supervisor -d local -p 28101 -n
pdms run lead-tp-create -b                # by name (or lead/lead-tp-create) from anywhere
pdms run -C backend/lead/lead-tp-create
```

`pdms run` finds the service by walking up from the current folder. Outside a service, it lists the services of the
current repo's backend folder (or below the current folder) with autocompletion; a unique partial name is enough.
It remembers the last user and DB, proposes the next free port, and asks for confirmation before using a database
marked as protected.

Options of `pdms run [SERVICE]`: `-u/--user`, `-d/--db`, `-p/--port`, `--host`, `-b/--background` / `-f/--foreground`,
`-i/--install` / `-n/--no-install` (force / skip the install), `--reload/--no-reload`, `-y/--yes` (no confirmation for protected DBs),
`-C/--path`, `--parallel/--no-parallel` (see below).

### Slow databases

PDMS services query the database synchronously inside `async` endpoints, so while one query waits, the whole service
waits: through a slow line or a tunnel, the requests of a page queue up behind each other. `pdms` changes two things
when it starts a service, without touching the repo (a `sitecustomize` on the service's `PYTHONPATH`, like local
events; see `src/pdms_cli/sqs_patch/pdms_parallel.py`):

- **Requests in parallel** (`parallel_requests = true`, or `pdms run --no-parallel` for one run): each request runs
  in its own thread, so a slow query only holds up its own request. The log says `[pdms] requests run in parallel`;
  `pdms ui` tags a service started without it as "one request at a time".
- **Connections opened at start** (`warm_connections = 2`, 0 to turn it off): each service opens that many database
  connections as soon as it starts, so its first requests don't wait for them (the log says how long they took).

Neither makes a query faster: each one still costs what the line takes to go there and back.

To see where the time goes, **database time per request** (`query_stats = true`) measures what each request asked
the database (see `src/pdms_cli/sqs_patch/pdms_queries.py`): every answer carries an `x-pdms-db` header (`queries=14;
time=5.231; connect=0.000; transactions=2`, visible in the browser's developer tools), pdms proxy keeps the queries
with the request for `pdms ui`, and the service's log gets a line when a request spent a second or more in the
database or ran the same query five times or more, usually once per row of a list:

```
[pdms] GET /lead/tp/details/8812: 14 queries in 5.23 s · the same query 9 times, 3.31 s (lead_common/repository.py:212)
```

Only the SQL text is kept, never the values sent with it.

The three apply when a service starts (restart it after changing them), and only to services started by `pdms` (CLI,
`pdms ui` or its Debug in VS Code), never in AWS.

### Smart install

Services depend on the `backend/common/*` libraries with `develop = false`, so a change in `common/` does not reach
the service until it is reinstalled. That is why `pdms` runs `poetry lock && poetry install` before starting, but only
when something that affects the install changed since the last successful one. The fingerprint covers:

- the service's `pyproject.toml` and `poetry.lock`;
- recursively, the whole source tree of its path dependencies installed as a copy (`develop = false`);
- only the `pyproject.toml` of editable path dependencies (`develop = true`), since their code is picked up live.

`-i` forces the install, `-n` skips it. `pdms config` can turn the smart check off (`smart_install = false`, always
install) or disable the install altogether (`install = false`). Fingerprints live in `~/.local/state/pdms/installs.json`.

## Background services

```bash
pdms services tp-                  # services of the current repo and the ports they are running on
pdms run lead-tp-create -b
pdms run lead-tp-details -b -p 28101
pdms ps                            # status, URL, user, DB and uptime of each instance
pdms logs lead-tp-create           # live console (Ctrl+C to exit); --no-follow to print and exit
pdms logs --all                    # every running instance merged, prefixed and colored per instance
pdms logs --stack tp               # the instances of a stack; or several: pdms logs lead-tp-list lead-tp-details
pdms logs lead-tp-create -p        # log of the previous run (kept as <log>.1 on every restart)
pdms urls                          # endpoints of every running instance: method, full URL and summary
pdms urls lead-tp-list -f health   # one instance, only paths containing 'health'
pdms open lead-tp-create           # Swagger (/docs) in the browser; --path /redoc for another page
pdms restart lead-tp-create        # same user, DB and port
pdms restart lead-tp-create -u agent   # switch user (-d for DB, -c to pick interactively)
pdms restart lead-tp-create lead-place-get util-state   # several, one after the other
pdms stop                          # pick which ones to stop; pdms stop --all (the proxy included)
```

When a background service starts, `pdms` also prints its endpoints (read live from its `/openapi.json`).

Each instance is identified as `service@port`, runs in its own process group (stopping it also stops the uvicorn
reloader) and writes its output to `~/.local/state/pdms/logs/<service@port>.log`. The registry lives in
`~/.local/state/pdms/instances.json`. Stopped instances keep their log until `pdms ps --clean`. When there are
background services, the interactive menu shows them on entry.

`pdms ps` sends an HTTP request to each instance, because with `--reload` uvicorn stays alive even when the app
fails to load:

| Status | Meaning |
| --- | --- |
| `● ok` | The app responds |
| `… starting` | Process alive, not responding yet |
| `⚠ error` | The last load failed; the exception is shown (e.g. `ModuleNotFoundError: ...`). Saving the fix reloads it |
| `✗ stopped` | The process exited; its log is kept |

**Services running outside pdms.** When pdms loses track of a background service that is still running (its
registry entry was forgotten while the process kept going), `pdms ps`, `pdms doctor` and pdms ui list it as
*outside pdms*: services and SQS consumers of a registered repo whose pdms exited, with the user and database of
the config they run as. `pdms adopt` (or `--all`, or the **Adopt** button) registers them again with their log, so
`pdms logs`, `stop` and `restart` work; `pdms adopt --stop` stops them instead. A foreground `pdms run` or a
debugger session is never listed, since something still holds it.

`pdms ps` also warns when the **installed code of a running instance is outdated**: `--reload` picks up changes of the
service and of editable (`develop = true`) libraries, but not of the `common/` libraries installed as a copy
(`develop = false`), nor a new `poetry.lock`. When any of them changed since the instance started, it says which ones
and suggests `pdms restart <instance>`, which reinstalls them automatically (smart install).

## Proxy: one port for every service

```bash
pdms up tp                 # start locally what you are working on
pdms proxy                 # http://localhost:28800 → local instances first, the remote API (dev) for the rest
pdms proxy --as agent      # act as another user on local services, without restarting them
pdms proxy -b              # in the background: pdms ps shows it, pdms logs proxy, pdms stop proxy
pdms proxy routes -f tp    # which service handles each route and where it would go now
```

- **Routes** come from the repo's Terraform (`infra/infra_auto/environments/dev/api_rsc_*.tf`, `--env` for others):
  every `METHOD /api/v1/...` is mapped to its service, preferring literal segments over `{params}` like API Gateway.
  They are cached until a `.tf` file changes.
- **Local first:** a request goes to the running instance of its service from the current repo (ports from
  `pdms ps`). Instances of the same service running from another repo are not used, to avoid mixing versions.
- **Remote fallback:** anything else is forwarded unchanged (headers, `Authorization`, body) to the repo's remote API,
  read once from `frontend/.env` (`VITE_APP_API_URL` + stage) and saved per repo; `--remote URL` sets another one and
  `--no-remote` answers `503` with the command to start the missing service. An API Gateway stage prefix
  (`/dev/api/v1/...`) is accepted too.
- **CORS** preflights are answered by the proxy (in AWS, API Gateway does it; the services have no CORS middleware).
- **`--as USER`** adds the `X-Dev-*` headers that services in `DEVELOPMENT_MODE` use instead of their `DEV_*`
  variables. They are only sent to local services, never to the remote API.
- **Frontend:** `pdms proxy` offers to write `VITE_APP_API_URL=http://localhost:28800` and
  `VITE_APP_API_URL_VERSION=api/v1` into `frontend/.env.local` (git-ignored, other lines are kept). Restart `yarn dev`.
  When the proxy stops, the file is put back as it was (if the proxy was killed, the next `pdms proxy` does it);
  values edited by hand in the meantime are left alone.
- **Docs:** `http://localhost:28800/docs` is a Swagger UI with a selector for every local service (live specs from
  their `/openapi.json`) plus an "All local services" view; "Try it out" goes through the proxy. Each service's own
  `http://localhost:<port>/docs` keeps working.
- Every request is logged with its status, target and time. Every response carries an `X-Pdms-Target` header.
- **Background:** `pdms proxy -b` keeps it running without a terminal (asked if you give neither `-b` nor `-f`).
  It shows up in `pdms ps` as `proxy@<port>`, its requests go to a log file (`pdms logs proxy`) and
  `pdms stop proxy` (or `pdms stop --all`) stops it and puts `frontend/.env.local` back.

## Frontend: `pdms front`

```bash
pdms front -b              # yarn dev in <repo>/frontend, in the background: https://localhost:3000
pdms front -b --build      # build it as in production (yarn build) and serve the build (vite preview)
pdms front --rebuild       # build again even if nothing changed
pdms logs frontend         # its output; pdms stop frontend stops it
```

It runs the PDMS web app of the current repo. It checks Node (22 to 24, as the frontend's `package.json` asks) and
yarn, and runs `yarn install` first when `node_modules` does not match `package.json` and `yarn.lock`
(`--install`/`--no-install` to force or skip it). The `@alivi` packages come from AWS CodeArtifact: if the install
cannot download them, log in with `./codeartifact-login.sh` (in `frontend/`).

- **dev** (`yarn dev`) reloads as you change the code. Vite also restarts by itself when `.env.local` changes, so
  starting or stopping the proxy (which points `.env.local` to it) is picked up without restarting it.
- **build** (`--build`) builds into `frontend/dist` (tsc + vite build, minified as in production) and serves it on the
  same port. The API URL is fixed when it is built, so pdms remembers what each build was made from (the API URL,
  `yarn.lock`, the code) and builds again only when one of them changed. `pdms ui` warns when a running build points
  to an API that is no longer the current one (for example after starting the proxy) and offers **Rebuild**.
- It listens on 3000, where logging in to the app works; if that port is in use it asks before taking another one.
- It shows up in `pdms ps` as `frontend (dev)` or `frontend (build)`; `pdms stop --all` stops it too. `pdms doctor`
  checks Node, yarn, `node_modules` and the port.

## Web interface: `pdms ui` (preview)

```bash
pdms ui                    # opens http://127.0.0.1:8765 in the browser; Ctrl+C to stop it
pdms ui --no-browser -p 9000
pdms ui --window           # the same page in a window of its own
pdms ui --install          # pdms in the app menu (Start menu, ~/Applications); --uninstall removes it
pdms ui --at-login         # open it in the tray when you log in (--not-at-login to stop)
pdms logs ui               # what pdms ui writes when opened from the app menu
```

A page with the background services, the stacks, the proxy, the frontend, the local events and the settings, updated
live from the same files the CLI uses, so both can be used at the same time.

It opens on **Home**: a card for each piece (services, proxy, frontend, events) with its own buttons, the stacks with
how many of their services run, and what needs attention (failed services and jobs, a build that points to an old
API, local events that are off). **Start my setup** keeps the stack, the proxy, the frontend (dev or build) and the
local events you want, saved in the configuration as you change it; **Start everything** starts what is not running
yet, in this order: the local events, the stack, the proxy (pointing the frontend to it) and the frontend. It asks
before a protected database, like the other screens. **Stop everything** stops the frontend, the proxy, every
background service and the local ElasticMQ, after listing them. The title bar shows the current user with its name
and roles.

**Setups** with a name: **Save as a setup** keeps the current choices under a name, and a click on another saved
setup switches to it: it stops what the current one runs and the new one does not use (its stack when it is another
one, the proxy and the frontend when the new one goes without them; the local events stay), after listing it, and
then starts the new one. The start button carries the setup's name, Ctrl K can switch too, and `pdms up --setup NAME`
starts a setup's stack from the terminal.

**What changed**: pdms ui checks every 30 seconds which running services run old code (a library of `common/` or
their own dependencies changed since they started, after a pull or a branch switch). Home says so with **Reinstall
and restart N**, and lists them with the services of the setup that will install on their next start and the
commits since the services started.

From the services screen (which also has a row for the local SNS, see
[SNS](#sns-one-local-topic-for-everything)) you can follow a service's logs live (also the
previous run's and the install's), open its Swagger docs, stop it, forget the stopped ones, and restart it with the
same or another user and database: it asks before a protected database and installs only if something changed, like
`pdms restart`. **Start service** starts any service of the current repo (like `pdms run -b`), on the port you choose
or the next free one, and **Start frontend** the web app in dev or build mode (like `pdms front -b`), with its own row,
its install and build logs, and **Rebuild**. The stacks screen starts (like `pdms up`) and stops (`pdms down`) a stack, shows which of its
services run, and creates, edits and deletes stacks with a searchable list of the repo's services. **Restart…** on a
stack restarts all its running services or the ones you pick, with their own users and databases or another one for
all of them; "Remember in the stack" keeps it for those services, their chips say so, and the stack editor lists these
exceptions to remove them. In the services screen a checkbox on each row (and "Select all" per stack) picks several
to restart or stop together; they restart one after the other, each row showing its progress. The proxy screen
starts the proxy in the background (like `pdms proxy -b`: it asks for another port when the one you chose is in use)
and stops it, and follows its requests live (the sidebar calls it **Requests**) with where each one went, how long
it took and, for local services, how much of it was database and how many queries it ran ([database time per
request](#slow-databases)); **Slow**, **N+1** and **Errors** filter them. A request's **Trace** puts the proxy, the
service, opening connections and each query on one time line, and **Queries** lists them with the service line that
ran each one (it opens VS Code there), pointing out the same query run again and again. A click on a request shows what it sent and
what came back (headers, with `Authorization` and cookies hidden, and bodies up to 64 KB), with **Copy as curl**,
**Replay** (sent through the proxy again, as it came) and **Open the log here** (the local service's log with its lines
marked). The background proxy keeps them in `proxy-requests.jsonl` in the state folder, readable only by you and never
more than a few MB. A request shows as soon as it reaches the proxy, with its time counting, until it is answered.

Each request is followed through the services and events: the background proxy sends it with `X-Request-Id`, and the
services pdms starts end each line they log for it with `#<id>`, pass the id on in their SQS messages (with local
events: the broker and the consumers carry it along) and write each SQS send, SNS publish and consumer run to
`trace.jsonl` in the state folder. The request's **Trace** adds them to its time line, and **Logs of this request**
opens the log dock with the proxy, the service and those consumers, only that request's lines. The log dock sits under
every screen: several logs interleaved by time, each with its colour, filtered by level, text or request, hidden to its
bar when not needed; **All logs** follows every running service, the proxy, the local SNS and the frontend, only
their new lines. While pdms ui runs, a log past 8 MB moves to `<log>.1` and starts again (its first lines stay), and
logs of instances gone for 7 days are deleted; pdms's own check that a service answers (every few seconds,
`/__pdms_health`) never reaches the service's code or its log. Its routes tab shows where each route goes now, like
`pdms proxy routes`. A proxy
started in a terminal shows there too, but its requests stay in that terminal. The events screen starts (like
`pdms events up`, the broker included) and stops the local ElasticMQ, lists every queue with its waiting messages and
consumer, shows a queue's messages without consuming them, purges them, and sends events (with the fields of the
event class filled in, through the broker or straight to the queue) or messages to a queue. It also lists the event
types with their queues and consumers, and every SNS publish, filtered by topic or text, with its attributes and
message. The settings screen adds, edits, deletes and filters the databases (with a connection test, also for a
database not saved yet) and the users, like `pdms db` and `pdms user` (a user can be renamed: its stacks follow), and edits the defaults of `pdms config defaults`.
A user's `DEV_ROLES` are ticked from the roles of the current repo (also in `pdms user add/edit`), and users can be
imported from the `pdms_user` table of a database, like `pdms user import`. **Export…** and **Import…** do what
`pdms config export` and `pdms config import` do: choose the sections, the passwords only if asked, and on import see
what is new or changed and which of your entries to overwrite (the previous file is kept as a backup).
Deleting a database or a user that a stack uses makes that stack ask for one when it starts.
Each database has **Migrations**: the Flyway migrations of the current repo's migrations checkout against it
(applied, pending, failed, a repeatable that changed and runs again, or applied but not in this checkout), read from
its `flyway_schema_history` and the migration files, without Docker; only to look, `pdms migrate` applies them.
Its first tab, **Repos**, adds (checking the folder as you type it; **Browse…** in the window), edits (name,
migrations repo, remote API for the proxy) and removes repos, like `pdms repo add/edit/remove`, and **Use** makes one
the current repo: when services of the old one are running it asks, like `pdms repo use`, whether to keep them, stop
them or restart them from the new repo with the same user, database and port. The repo chip in the title bar lists the
repos to switch to.

**From the page to the code.** A service that failed to load shows its traceback under its row (Home offers
**See the error**); a line of a file of a registered repo opens VS Code at that line. **Debug** (the bug icon) stops
the instance, writes its `.vscode/launch.json` configuration like `pdms debug` (same user, database and port, without
`--reload`) and opens VS Code on the repo: F5 starts it under the debugger. VS Code's `code` command has to be
installed (in VS Code: Shell Command: Install 'code' command in PATH). A service that started but does not answer for
a moment shows as **busy**, not starting: the PDMS services call the database synchronously inside `async`
endpoints, so a slow request keeps everything else to that service waiting until it ends. **Recent**, on Home, lists
what failed, stopped, loaded again or finished while pdms ui runs, and **Desktop notifications** (Settings →
Defaults, on by default) tell you when a service fails to load or stops by itself, also with pdms ui only in the tray.
**Ctrl K** (⌘K on macOS), or the search box in the title bar, finds and runs any action (restart or debug a
service, open its logs, start a stack, go to a screen...); `/` jumps to the filter of the screen you are on.

**Status bar.** At the bottom: the branch of the current repo, the user, the current database with what one round
trip to it costs, the tunnel it goes through (a database whose host name points to this machine, like the SSM
tunnels of devo-cli), the proxy, the local events and the pdms version. pdms ui measures the databases in use (the
current one and those of the running services) once a minute with `select 1` on a connection it keeps open, so the
number is what every query pays at least; the database opens the last hour of each one, **Measure now** and **Line
check** (one connection to the internet, to tell a slow line from a slow way to the database). Home says when a
database takes 300 ms or more per round trip, does not answer, or its tunnel is down.

**Doctor** (in the sidebar) runs the checks of `pdms doctor`, grouped by section, with a filter, "Problems only" and,
if asked, the database connections. Where pdms ui can fix something the hint is a button (edit that database, set the
repo's migrations, forget stopped services...); otherwise it shows the command, ready to copy. **Copy report** copies
them all as text. They run when pdms ui starts and every 15 minutes, so the sidebar shows how many warnings and
problems there are, and Home's "Needs attention" lists the problems. Its **Network** section (also in `pdms doctor`)
checks that something listens where each tunnel of a database should be, and how long one connection to the internet
takes; with the database connections, each one shows its round trip, and a warning from 100 ms on. `pdms db test`
shows the round trip too.

The page uses the language of pdms (`pdms config language`, or `PDMS_LANG`) and changes with it while it is open.

**Updates.** When a new pdms is published (the same daily check as the CLI), the title bar shows **⬆ <version>** and
Home lists it; it opens the release notes and **Update and restart**, which does what `pdms self-update` does and then
restarts pdms ui on the same port, so the page or the window comes back by itself. Services, the frontend and the
local events keep running; the background proxy runs on pdms's own code, so it is restarted with the new version
unless you untick it. On Windows, where a running pdms cannot replace its own files, a small PowerShell helper updates
it once pdms ui has closed and then opens it again (`pdms self-update` on Windows uses the same helper, in a window of
its own). Settings → Defaults shows the version and has **Check now**. If pdms was updated from the terminal, the
chip offers to restart pdms ui with the new version.

**In the system.** `pdms ui --install` adds pdms to the app menu with its icon: a `.desktop` file in
`~/.local/share/applications` on Linux, a Start menu shortcut on Windows (to `pdmsw.exe`, the same pdms without a
console window) and `~/Applications/pdms.app` on macOS. The installers offer it at the end (`PDMS_MENU=1` or `0` to
answer beforehand). The entry opens `pdms ui --window` (in the browser when the desktop extra is not installed), and
opening it again shows the pdms ui that is already open instead of starting another one. With the window there is a
tray icon (Open, Open in the browser, Quit): closing the window keeps pdms ui in the tray, and **Quit** stops it;
without a tray, closing the window stops it. "Open pdms ui when I log in" (Settings → Defaults, or
`pdms ui --at-login`) starts it in the tray at login. Opened from the menu there is no terminal, so its output goes
to `pdms logs ui`, and an error that stops it shows as a notification.

It only listens on `127.0.0.1`, and only the link `pdms ui` prints opens it: that link carries a random token for the
session, which the browser keeps as a cookie. Requests from other web pages are rejected, and a database password
only reaches the page when you click its eye to show it (in the table it hides again after 30 seconds).

The window (`--window`) uses [pywebview](https://pywebview.flowrl.com/), which comes with the optional `desktop`
extra: Edge WebView2 on Windows and WebKit on macOS (both part of the system), Qt on Linux (installed with the extra,
about 210 MB to download; its tray icon is Qt's, pystray's on Windows and macOS). On Windows and macOS `pdms ui --window` installs pywebview by itself the first time
(into pdms's own environment, with `uv pip`); on Linux add it to an install with `uv tool install --force 'pdms-cli[desktop] @ <wheel URL of the release>'`
(`pdms ui --window` prints the exact command), or `uv tool install -e '.[desktop]' --force` from a checkout;
`pdms self-update` keeps it.

## Events (local SQS)

PDMS services publish SQS events to the **broker** queue (`SQS_EVENT_BROKER_URL`); the `broker-sqs-event` Lambda
routes each one by its `type` to a destination queue, consumed by an event Lambda (`*-ev`). `pdms` reads that whole
map from the current repo — nothing has to be registered by hand:

| What | Where it comes from |
| --- | --- |
| Queues (name, FIFO, visibility timeout) | `aws_sqs_queue` in the repo's Terraform, resolving `var.*` defaults |
| Consumer of each queue and its handler | `aws_lambda_event_source_mapping` → the Lambda's `lambda_path` / `handler` |
| Queue of each event type | `EVENT_ROUTE_DEST` + `EventType` in `backend/common/event` and the broker Lambda's environment in Terraform |
| Queues of events without Terraform yet | extra queues in `infra/local_sqs/elasticmq.conf` |

```bash
pdms events map -f email   # event type → queue → consumer (and queues the broker does not route)
pdms events up             # local ElasticMQ (Docker) with every queue, at http://localhost:9324
pdms events status         # messages waiting / in flight per queue (--all for empty ones)
pdms events down
```

`pdms events up` generates the ElasticMQ configuration from that map and recreates the container only when the
queues change. The port is `events_port` in the configuration (9324 by default). Docker is required; `pdms doctor`
checks it.

### Publishing to the local broker

While `pdms events up` is running, every service started with `pdms run`, `pdms up`, `pdms restart` or `pdms debug`
publishes its events to the **local broker** — no `.env` to edit:

- `SQS_EVENT_BROKER_URL` points to the local broker queue, and the broker's destination variables
  (`SQS_EMAIL_NOTIFY`, …) to their local queues;
- a `sitecustomize.py` shipped with pdms is put on `PYTHONPATH`: Python loads it in every process of the service,
  including the ones uvicorn `--reload` spawns, and it sends the SQS clients created by botocore (1.29 in PDMS, which
  predates `AWS_ENDPOINT_URL_SQS`) to ElasticMQ with dummy credentials. Other AWS clients (S3, …) are not touched.

The `events` setting chooses the behaviour (`--events` overrides it per command): `auto` (default: local while the
local ElasticMQ runs, otherwise the service's own configuration), `local` (always; offers to start ElasticMQ) or
`aws`. The summary of every run shows where the service publishes, and `pdms events status` lists the instances
publishing locally. Queues are FIFO with content-based deduplication, like in AWS: the same message body sent twice
within 5 minutes is stored once.

### Broker and consumers

`pdms events up` also runs the **broker** (`broker-sqs-event`, with the last used user/DB; `--no-broker` to skip it),
so published events are routed to their queues exactly as in AWS. Event Lambdas run like any other service:

```bash
pdms run email-notify -b        # detected as the consumer of email-send-sqs-queue.fifo: no port, no uvicorn
pdms ps                         # email-notify@sqs · sqs ← email-send-sqs-queue.fifo
pdms logs --all                 # received / processed / failed messages of every consumer, next to the services
pdms events down                # stops the consumers, the broker and ElasticMQ
```

A service is a consumer when the repo's Terraform maps a queue to it. pdms then runs its own poller with the
service's Python: it long-polls the local queue and calls the Lambda `handler` from Terraform with
`{"Records": [...]}` and a Lambda-like context, deletes processed messages and honours `batchItemFailures`. A failed
message is retried after 5 seconds (not the queue's visibility timeout) and dropped with a warning after 3 attempts,
since there is no dead-letter queue locally. Consumers work with `ps`, `logs`, `stop`, `restart`, stacks and the
outdated-code warning; they always use the local ElasticMQ (and publish to the local broker), offering to start it.

### Sending and inspecting events

```bash
pdms events send email-notify --template             # the event's fields, from its class in backend/common/event
pdms events send email-notify -b '{"to_emails": ["a@x.com"]}'   # through the broker, like a publishing service
pdms events send email-notify --direct -f event.json # straight to its queue, skipping the broker
pdms events send demo-sqs-queue.fifo -b '{"raw": 1}' # a raw message to any queue
pdms events peek email-send-sqs-queue.fifo           # messages waiting, without consuming them
pdms events purge email-send-sqs-queue.fifo          # or --all
pdms debug email-notify                              # VS Code configuration running the consumer under the debugger
```

`send` with an event type builds the same message a service publishes (common/event's `Event` as JSON: `event_id`,
`type`, `app_context` and the given fields) and, by default, sends it to the broker queue. Every message gets its own
deduplication id, so sending the same test body twice is not swallowed by the FIFO deduplication. It warns when
nothing is consuming the target queue. `peek` counts as a receive in SQS, but the local poller counts its own
attempts, so peeking does not make a failing message be dropped sooner. The interactive menu has an **Events** entry
with all of this.

### SNS: one local topic for everything

Some consumers publish onwards to SNS topics that other systems subscribe to (`account-publish-ev` and the
`credential-*-publish-ev` ones). With local events nothing of that reaches AWS: whatever any service publishes, to
any topic, is kept in one local queue, `pdms-sns`, and in a readable log, and the service gets the same answer AWS
would give.

```bash
pdms logs sns                 # live: time, service → topic, group id and attributes, then the message
pdms events peek pdms-sns     # the same messages as JSON, without consuming them
pdms events purge pdms-sns
```

`pdms ui` shows it as an `sns` row in Services (with when something was last published) whose **Logs** follow that
log live, like any service, and in more detail in Events → SNS (filter by topic, attributes); `pdms events status`
always lists it.

Each service also gets its topics' ARNs from its Lambda's Terraform (`SNS_ACCOUNT_PUBLISH_ARN =
aws_sns_topic.sns_account_topic.arn` becomes `arn:aws:sns:us-east-1:000000000000:sns-account-publish.fifo`), so
`peek` shows which topic each message was for; a publish without a topic still lands there as `(no TopicArn)`.
SNS calls other than publishing go to the local ElasticMQ, which rejects them, instead of the real AWS.

## Stacks: several services at once

```bash
pdms stack add          # wizard: name, services, fixed user/DB or "ask when starting"
pdms stack list
pdms up tp              # starts the services that are not running, each on a free port
pdms up tp -u agent -d web
pdms up --setup Prospecting             # the stack of a setup saved in pdms ui's Home (it becomes the current one)
pdms restart --stack tp                 # its running services, one after the other
pdms restart --stack tp lead-place-get lead-tp-update -u tp-provider --remember
pdms down tp
```

A stack can start some of its services with another user or database than the rest: `pdms restart --stack` with
`-u`/`-d` and `--remember` (or "Remember in the stack" in pdms ui) keeps that exception for those services, and
`pdms up` respects it. In the configuration it is the stack's `overrides`:

```toml
[stacks.tp]
services = ["lead/lead-place-get", "lead/lead-tp-update", "util/util-state-list"]
user = "supervisor"
db = "web-dev"

[stacks.tp.overrides."lead/lead-place-get"]
user = "tp-provider"
```

Renaming or deleting a user or a database updates the exceptions; a service removed from the stack loses its own.

## Debugging in VS Code

```bash
pdms debug lead-tp-create -u supervisor -d local -p 8090
```

Adds (or updates) the configuration `pdms: lead-tp-create · supervisor @ local :8090` in the repo's
`.vscode/launch.json`, using the service's virtualenv python and no `--reload` so breakpoints work. Then:
Run and Debug (Ctrl+Shift+D) → pick the configuration → F5.

The variables (including the password) are **not** written to `launch.json` but to an `envFile` in
`~/.local/state/pdms/env/<service>.env` (permissions `0600`), because `.vscode` is not in the repo's `.gitignore`.
If `launch.json` had comments they are lost when it is rewritten; a copy is kept as `launch.json.bak`.

The `backend/common/*` libraries are installed as a copy (`develop = false`): to stop inside them, set the breakpoint
in the copy under `.venv/lib/python3.*/site-packages/...` or step in with F11 from the service.

`pdms env -u supervisor -d local` prints the variables (`eval "$(pdms env ...)"`, or `--dotenv` for `.env` format).

## Tests and migrations

```bash
pdms test lead-tp-create                 # poetry run pytest in the service (smart install first), on pdms_test_1
pdms test lead-tp-create -- -k create -x # extra arguments go to pytest
pdms test core -d pdms_test_2 -u supervisor  # a package too; another test database, and a user's DEV_*
pdms db local up                         # pdms's own Postgres (Docker, :5440) with 4 empty test databases
pdms db local status                     # its databases and their size
pdms db local tests 6 --recreate pdms_test_1  # have 6 of them; recreate one empty
pdms db local refresh                    # copy pdm_template_dev (web-dev's server, read only) as alias pdms-local
pdms db local snapshot save fresh        # keep the copy as it is…
pdms db local snapshot restore fresh     # …and come back to it in a second
pdms test lead-tp-create --dev-mode      # DEVELOPMENT_MODE on (no token check); off by default, as deployed
pdms migrate -d web-dev                  # flyway info: applied and pending migrations (read-only)
pdms migrate validate -d web-dev         # flyway validate, like the pipeline (pending ones are not errors)
pdms migrate migrate -d local            # apply them — only against a local database
```

Tests only ever run on **pdms's test databases** (`pdms_test_1` … in pdms's own Postgres, the `pdms-postgres`
container on port 5440 with its data in the `pdms-postgres-data` volume): the integration tests drop and create every
table of the database they get, so a database that keeps data — web-dev, or a local one like `pdms_sync` — would be
wiped. pdms always sets `DB_PG_CONNECTION_STR` to one of them, so a service's `.env` is never used, and refuses any
other database. Without them, `pdms test` offers to start that Postgres and create them (`pdms db local up`).

The same Postgres keeps a **local copy** to run services on without the slow round trips of web-dev:
`pdms db local refresh` copies `pdm_template_dev` (the schema, its Flyway history and the reference data) from the
server of the `web-dev` alias — only read, with `pg_dump` from the Postgres image, through the same tunnel as the
services — into its `pdms` database, builds the `configuration` schema from the repo's migrations (the dev user cannot
read it), applies the repo's pending migrations and registers the alias `pdms-local`. Snapshots keep it and bring it
back in about a second. Flyway's `migrate` from pdms holds a session lock (`postgresql.transactional.lock=false`): with
a transaction lock, a migration with `CREATE INDEX CONCURRENTLY` waits for Flyway's own lock forever. Tests run
with `DEVELOPMENT_MODE` off (a service's `.env` usually turns it on, and with it the token is not checked, so tests
of unauthorized requests fail): when the `.env` turns it on, `pdms test` asks, `--dev-mode` / `--no-dev-mode`
answer it, and the Tests screen has a Development mode checkbox; each result says how it ran. Each run keeps
its JUnit report and result for **pdms ui → Tests**, which lists the services and packages your branch touches
(its diff against main plus uncommitted files, including changes to local packages such as `common/core`), runs
them with Re-run failed, and links each failure to its line in VS Code. Services share tables, so each test
database runs one project at a time: tick more of them to run more at once.

`pdms migrate` runs [Flyway](https://flywaydb.org/) from the `pdms-db-migrations` repo (PDMP-467; the Alembic
migrations in `backend/common/sync-database` are frozen) with the same Docker image and arguments as the
CodePipeline (`flyway-postgres:11.20.2-1`, `-configFiles=flyway.toml,placeholder.toml -environment=local`), so no
Flyway install is needed — only Docker. `info` and `validate` work against any database; `migrate` only against a
local one (`localhost`, not protected), since shared databases are migrated by the pipeline; other Flyway commands
(`clean`, `repair`, …) are not allowed. The database password is passed to the container as an environment
variable, never on the command line.

The migrations checkout is found automatically and remembered per PDMS repo: the current folder if it is a Flyway
repo, the one saved for the repo, or a sibling of the PDMS checkout (`~/Code/Alivi/pdms-db-migrations`; with several,
the one whose suffix matches, e.g. `pdms_v2` ↔ `pdms-db-migrations-v2`). `--migrations PATH` sets another one.
`pdms doctor` and `pdms repo list` show it.

## Getting started: `pdms setup`

The first time you run `pdms` (with no configuration yet) it offers a guided setup. You can run it again at any time
with `pdms setup` (or **Settings → Guided setup**) to complete whatever is still missing.

1. **Import** — it first asks whether you have a configuration to import, such as the one a teammate exported with
   `pdms config export`. It suggests the `pdms-config*.toml` files it finds in the current folder, `~/Downloads` and
   your home, or you can pick another file. The import works as `pdms config import`.
2. **Then only what is missing**, whether the import happened or not:
   - the **language**, on a first setup when the import did not bring the defaults;
   - the **PDMS repo**: the checkout you are in, or a folder you enter;
   - the **migrations repo** (pdms-db-migrations), found next to the PDMS repo when possible;
   - **databases**: add one if there are none, and type the **passwords** that imported databases do not carry
     (exports leave them out by default), with an optional connection test;
   - **development users**: import them from the `pdms_user` table of a database, or add one by hand;
   - optionally a **stack**, and a review of the other defaults.
3. Finally it offers to run `pdms doctor`.

Steps that are already configured are just listed with a ✓, and every step can be skipped (it tells you the command to
do it later).

## Command reference

| Command | What it does |
| --- | --- |
| `pdms` | Interactive menu |
| `pdms setup` | Guided setup: import a shared configuration, then configure what is missing |
| `pdms run` / `pdms debug` / `pdms env` | Run a service / create a VS Code debug configuration / print a profile's variables |
| `pdms services` | List the services of the current repo |
| `pdms repo` | Repos menu (`list`, `add`, `use`, `edit`, `remove`) |
| `pdms test` / `pdms migrate` | Run a service's tests on a pdms test database / Flyway `info`, `validate` (and `migrate` locally) |
| `pdms ps` / `logs` / `urls` / `open` / `stop` / `restart` | Manage background instances |
| `pdms up` / `pdms down` | Start / stop a stack |
| `pdms proxy` | Local API gateway (`routes` to inspect the mapping) |
| `pdms front` | The PDMS web app: `yarn dev`, or a production build with `--build` |
| `pdms ui` | Web interface with the services, the proxy and the events, live (preview); `--window` in a window of its own, `--install` in the app menu |
| `pdms events` | Local SQS: map, ElasticMQ and broker (`map`, `up`, `status`, `send`, `peek`, `purge`, `down`) |
| `pdms stack` | Stacks menu (`list`, `add`, `edit`, `remove`) |
| `pdms db` | Databases menu (`list`, `add`, `edit`, `remove`, `test`) |
| `pdms user` | Users menu (`list`, `add`, `edit`, `rename`, `remove`, `import`) |
| `pdms config` | Settings (`defaults`, `language`, `export`, `import`, `path`, `edit`) |
| `pdms doctor` | Check the environment and the configuration |

`pdms <command> --help` shows every option.

## Configuration

Stored in `~/.config/pdms/config.toml` (or wherever `PDMS_CONFIG` points) with permissions `0600`, since it holds
the database passwords. Never commit it. Up to 0.2.5 services started on 8080 and the proxy on 8000, ports many other
programs use; a configuration still on 8080 moves to the new defaults by itself.

```toml
[defaults]
language = "en"                           # "en" or "es"
host = "0.0.0.0"
port = 28100                              # first port tried for a service (the next free one if busy)
proxy_port = 28800                        # pdms proxy (or pdms proxy -p)
logging_level = "DEBUG"
reload = true
install = true
smart_install = true                      # skip the install when nothing changed
update_check = true                       # tell when a new pdms version is out
ui_at_login = false                       # open pdms ui in the tray at login (pdms ui --at-login; an import keeps yours)
notify = true                             # pdms ui tells the desktop when a service fails to load or stops by itself
banner = true                             # big PDMS banner when the menu opens (or PDMS_NO_BANNER=1)
events = "auto"                           # auto | local | aws: where services publish SQS events
events_port = 9324                        # local ElasticMQ (pdms events up)
db_timeout = 15                           # seconds for `pdms db test` (or `pdms db test -t 30`)
proxy_timeout = 300                       # seconds the proxy waits for an answer before a 502 (or `pdms proxy -t 600`)
parallel_requests = true                  # each request of a service in its own thread (see "Slow databases")
warm_connections = 2                      # database connections each service opens as it starts (0 = when needed)

[defaults.env]
# extra variables injected on every run

[users.supervisor]
user_id = "00000000-0000-0000-0000-000000000001"
username = "supervisor@example.com"
first_name = "Jane"
last_name = "Doe"
roles = "TPR.Supervisor"

[dbs.local]
host = "localhost"
port = 5432
database = "pdm"
user = "postgres"
password = "..."
driver = "postgresql+psycopg2"
protected = false                         # true = ask for confirmation before using it

[stacks.tp]
services = ["lead/lead-tp-list", "lead/lead-tp-details"]
user = ""                                 # empty = ask when starting
db = ""

[setup]                                   # what Start everything starts (pdms ui, Home)
stack = "tp"
events = false
proxy = true
frontend = true
frontend_mode = "dev"                     # dev | build
```

### Export and import

Share the setup with the team or back it up:

```bash
pdms config export                         # asks which sections and whether to include passwords
pdms config export team.toml --no-secrets  # users, databases, stacks and defaults, without passwords
pdms config export backup.toml --secrets   # full backup, passwords included
pdms config export - --only users,stacks   # to stdout, only some sections
pdms config import team.toml               # shows new / changed / unchanged entries and asks what to overwrite
pdms config import team.toml --only dbs --overwrite -y
pdms config import backup.toml --replace   # the selected sections become exactly the file's content
```

- Sections: `defaults`, `users`, `dbs`, `stacks`. The runtime state (last user/DB) is never exported.
- Passwords are left out unless you choose to include them. Importing a database without password keeps the password
  you already have for that alias; new ones without password are listed so you can set them with `pdms db edit`.
- By default the import merges: new entries are added and, for entries that differ, you pick which ones to overwrite
  (without a terminal they are kept unless `--overwrite`). On a first setup, with no configuration yet, everything
  is imported.
- Before writing, the current configuration is copied to `config.toml.bak-<timestamp>`.
- Export files are written with permissions `0600`. Do not commit a file exported with `--secrets`.

The same options are available in the interactive menu under **Settings**.

## Development

```bash
git clone git@github.com:lianabeatriz93/pdms-cli.git ~/Code/Alivi/pdms-cli
cd ~/Code/Alivi/pdms-cli
uv sync                          # .venv with the dev dependencies
uv run pytest
uv tool install -e . --force     # use your checkout as the `pdms` command (re-run when dependencies change)
```

With the editable install, code changes apply immediately and `pdms self-update` tells you to use `git pull`.
If an update adds a dependency and the command fails, it says which one is missing and how to reinstall.

### Code layout

- `src/pdms_cli/actions.py`: what pdms does (start a service, a stack, the proxy...), without asking anything. The
  terminal and `pdms ui` both call it; a decision it cannot take is raised as an exception the caller answers.
- `src/pdms_cli/commands/`: the terminal commands, one module per group (`run.py`, `instances.py`, `proxy.py`...),
  with the shared helpers in `common.py`. `cli.py` is only the entrypoint: a new command also goes in its
  `COMMAND_ORDER`, the order `pdms --help` lists them in.
- `src/pdms_cli/ui/`: the `pdms ui` server (`server.py`, `jobs.py`, `state.py`) and its page in `static/`:
  `index.html`, `app.css`, `i18n.js` (the Spanish catalog) and `js/`, one ES module per screen (`home.js`,
  `services.js`, `proxy.js`...) plus `core.js` (shared helpers), `state.js` (the live state), `router.js` and
  `main.js` (wiring). No build step: the browser loads the modules as they are.
- Every text has its Spanish translation: `i18n.py` for the terminal and `static/i18n.js` for the page; the tests
  fail when one is missing.

CI (`.github/workflows/ci.yml`) runs once per commit of a pull request and on every push to `main`; a new commit to
a pull request cancels the run of the previous one, and changes to Markdown files alone run nothing. On `main` it runs
the tests on Linux, macOS and Windows with Python 3.10 and 3.12; pull requests run both Pythons on Linux and 3.12 on
macOS and Windows (their minutes cost more). The installers (`install.sh` / `install.ps1` on the three systems) run on
`main` and in pull requests that change `installer/`, `pyproject.toml`, `uv.lock` or the workflows.

### Releasing

Versions follow [Conventional Commits](https://www.conventionalcommits.org/) with
[commitizen](https://commitizen-tools.github.io/commitizen/) (`feat` → minor, `fix` → patch while below 1.0) and
[PEP 440](https://peps.python.org/pep-0440/).

Version numbers advance slowly: every release is a **stable patch release** (`0.2.1`, `0.2.2`, `0.2.3`, ...), and
the minor version only goes up when something significant ships:

```
0.2.1  →  0.2.2  →  0.2.3  →  ...  →  (something significant)  →  0.3.0
```

```bash
git checkout main && git pull
uvx --from commitizen cz bump --increment PATCH --dry-run   # preview: e.g. 0.2.1 → 0.2.2
uvx --from commitizen cz bump --increment PATCH
git push --follow-tags
```

`--increment PATCH` keeps commitizen from bumping the minor version for `feat:` commits. For a significant release:

```bash
uvx --from commitizen cz bump --increment MINOR   # e.g. 0.2.7 → 0.3.0
git push --follow-tags
```

`cz bump` updates `pyproject.toml`, `uv.lock` and `CHANGELOG.md`, commits and creates the `vX.Y.Z` tag.
(Pre-release versions such as `0.3.0a1` are still supported, published as GitHub pre-releases and installed with
`PDMS_PRERELEASE=1` / `pdms self-update --pre`, but they are not part of the regular process.)

Pushing the tag runs `.github/workflows/release.yml`: it checks that the tag matches the package version, runs the
tests, builds the wheel and sdist, and publishes a GitHub release with them, both installers and the changelog
section as release notes.


User-facing texts are written in English inside `_()` (from `pdms_cli.i18n`) and translated in the `ES` catalog of
[`src/pdms_cli/i18n.py`](src/pdms_cli/i18n.py). `tests/test_i18n.py` fails if a string has no Spanish translation,
if placeholders differ between languages, or if Spanish text is left outside the catalog.
