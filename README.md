# pdms-cli

`pdms` runs PDMS services on your machine, in the terminal or from a web page (`pdms ui`). Pick a development user
and a database, and it installs what changed and starts `uvicorn` with `DEV_*`, `DEVELOPMENT_MODE`, `LOGGING_LEVEL`
and `DB_PG_CONNECTION_STR` set. Around that it gives you:

- services in the background, with their logs, status and outdated-code warnings;
- **stacks** (groups of services) and a **proxy** that serves every route on one port, local first;
- the PDMS **frontend**, local **SQS/SNS events** and **emails** that never reach real people;
- a local copy of the database, test databases, Flyway migrations and VS Code debugging.

## Install

```sh
# macOS / Linux
curl -LsSf https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://github.com/lianabeatriz93/pdms-cli/releases/latest/download/install.ps1 | iex"
```

The installer sets up [uv](https://docs.astral.sh/uv/) if needed (it brings its own Python), installs the latest
`pdms`, puts it on the `PATH` and offers to add it to the app menu. `PDMS_VERSION=0.4.1` installs a given version.

| | |
| --- | --- |
| Update | `pdms self-update` (`--check` only looks). pdms also tells you, at most once a day, when there is a new one |
| Go back | `pdms self-update --list`, then `--version 0.4.11` (or Settings → **Versions** in pdms ui). pdms stops offering the version you left; a newer one, yes |
| Betas | Settings → **Versions: Beta** (`update_channel = "beta"`): each version before it reaches everyone |
| A problem | `pdms bug-report` (or **Bug report** in Doctor): a file with versions, doctor, services and logs, without passwords |
| Version | `pdms --version` |
| Uninstall | `uv tool uninstall pdms-cli` (the configuration in `~/.config/pdms` stays) |
| Tab completion | `pdms --install-completion`, then a new terminal: `pdms run lead-tp-<Tab>`, `pdms up <Tab>`… |

**Needs:** Linux, macOS or Windows with Python 3.10+ (tested on the three), and [Poetry](https://python-poetry.org/)
for the services. Docker for local events, the local database and migrations; Node 22–24 and yarn for the frontend;
the AWS CLI for the AWS profile. `pdms doctor` checks all of it and says how to fix what is missing.

## First steps

```bash
pdms setup     # guided: import a teammate's configuration, then add whatever is still missing
pdms ui        # the web interface (or pdms, for the interactive menu in the terminal)
```

`pdms setup` runs by itself the first time. It offers to import a file exported with `pdms config export` (it looks
in the current folder, `~/Downloads` and your home), then asks only for what is missing: the PDMS checkout, the
migrations repo, the databases and their passwords, the development users (importable from a database's `pdms_user`
table), a stack, and finally runs `pdms doctor`. Every step can be skipped and done later.

A usual day:

```bash
pdms up tp            # the services of the stack tp, in the background
pdms proxy -b         # http://localhost:28800: local services first, the dev API for the rest
pdms front -b         # the web app on https://localhost:3000, pointed at the proxy
pdms logs --all       # every log interleaved (Ctrl+C to leave)
```

## pdms ui

```bash
pdms ui               # http://127.0.0.1:8765 in the browser
pdms ui --window      # the same page in a window of its own, with a tray icon
pdms ui --install     # pdms in the app menu; --at-login opens it in the tray when you log in
```

The page and the CLI read and change the same files, so both can be used at once. Its screens:

| Screen | What you do there |
| --- | --- |
| **Home** | Start or stop your **setup** (a stack, the proxy, the frontend and local events, savable under a name); what needs attention; services running old code after a pull, with **Reinstall and restart** |
| **Services** | Start, restart (another user or database), stop, debug and follow the logs of each service; a failed one shows its traceback, each line opening VS Code; **Save as stack** |
| **Stacks** | Start, restart and stop stacks; create, edit, rename them, or make one **from the running services** |
| **Requests** | The proxy's requests live: status, time, database time and queries (**Slow**, **N+1**, **Errors**); a request's trace through services, events and emails, its queries with the line that ran them, **Copy as curl**, **Replay**, its logs |
| **Events** | Local queues and their messages, sending events, event types, SNS publishes and **Emails** |
| **Data** | Each database's round trip and tunnel, the local copy and its snapshots, the test databases, migrations, the AWS profile |
| **Tests** | The services and packages your branch touches, run on the test databases, failures linked to VS Code |
| **Doctor** | The checks of `pdms doctor`, with a button where pdms can fix it; **Bug report** saves a file to send with a problem |
| **Settings** | Repos, databases, users, defaults, export and import |

**Ctrl K** finds and runs any action; `/` jumps to the screen's filter. The status bar shows the branch, user,
database and its round trip, proxy and events. The page follows pdms's language and offers updates (**Update and
restart**). It only listens on `127.0.0.1` and only opens with the link (and token) that `pdms ui` prints.

The window uses [pywebview](https://pywebview.flowrl.com/) (the `desktop` extra). Windows and macOS install it by
themselves the first time; on Linux it needs Qt (~210 MB), so `pdms ui --window` prints the command to add it.
Closing the window keeps pdms ui in the tray; **Quit** stops it.

## Running services

```bash
pdms run                                 # inside a service folder: asks user, DB, port, foreground/background
pdms run lead-tp-create -b               # by name from anywhere (a unique part of the name is enough)
pdms run lead-tp-create -u supervisor -d local -p 28101 -n   # -n: skip the install, -i: force it
pdms ps                                  # status, URL, user, DB and uptime of each background service
pdms logs lead-tp-create                 # live; -p the previous run, --all every service, --stack tp
pdms urls lead-tp-list -f health         # endpoints of a running service
pdms open lead-tp-create                 # its Swagger /docs
pdms restart lead-tp-create -u agent     # same port; another user (-d another database)
pdms stop                                # pick which; --all stops everything, the proxy included
```

Each background instance is `service@port`, logs to `~/.local/state/pdms/logs/` and is stopped with its uvicorn
reloader. pdms asks before using a database marked as **protected**. `pdms ps` asks every service whether it answers:

| Status | Meaning |
| --- | --- |
| `● ok` | It answers |
| `… starting` | Running, not answering yet |
| `⚠ error` | The code failed to load; the exception is shown. Saving the fix reloads it |
| `✗ stopped` | The process ended; its log stays until `pdms ps --clean` |

- **Smart install.** Services use `backend/common/*` as a copy (`develop = false`), so `pdms` runs
  `poetry lock && poetry install` before starting, but only when the service's `pyproject.toml`/`poetry.lock` or
  the code of those libraries changed since the last install. When only their code changed (the usual case after a
  `git pull`), it skips poetry and reinstalls just those libraries with `pip`; `-i` forces the whole install.
  `pdms up` installs several services at once (2 on a PC with 4 cores or less, or under 12 GB of memory; 3
  otherwise), each with its output in its install log.
- **Outdated code.** `pdms ps` (and pdms ui) says when a running service uses an old copy of `common/` or an old
  lock: `pdms restart` reinstalls it.
- **Outside pdms.** A service still running after pdms lost track of it is listed as *outside pdms*; `pdms adopt`
  manages it again (`--stop` stops it).
- **Light on the machine.** A service starts with its virtualenv's Python (`python -m uvicorn`), without
  `poetry run` in between, and `--reload` watches only the folders with its code: not its `.venv` (thousands of
  files that uvicorn would otherwise check four times a second, about two thirds of a CPU core per service) nor
  `tests`. A change to `main.py` alone needs `pdms restart`.

### Python of the services

Poetry keeps using a service's `.venv` after its project moves to another Python (`python = ">=3.12,<3.14"`), so it
would keep running on the old one. When pdms is about to install a service whose virtualenv runs a Python its project
no longer accepts, it says so and asks: recreate it on the lowest installed Python the project accepts (the one the
Lambdas run) and install again, or keep it this time. `pdms doctor` counts the services still on the old one and
says which Python pdms would use, or which to install (`uv python install 3.12`).

### Slow databases

PDMS services query the database synchronously inside `async` endpoints, so one slow query holds the whole service.
Services started by pdms (never in AWS) get, through a `sitecustomize` on their `PYTHONPATH`:

- **requests in parallel** (`parallel_requests`; `pdms run --no-parallel` for one run): each request in its own thread;
- **connections opened at start** (`warm_connections = 2`);
- **database time per request** (`query_stats`): an `x-pdms-db` header on every answer, the queries of each request in
  pdms ui, and a log line for a request that spent a second in the database or ran the same query five times
  (`14 queries in 5.23 s · the same query 9 times … (lead_common/repository.py:212)`). Only the SQL is kept, never its
  values.

They apply when a service starts. None makes a query faster: the round trip to the database still costs what it costs.

### On demand

On a computer with little memory, services can wait **asleep** until they are used: each one takes ~150 MB awake and
nothing asleep.

```bash
pdms run lead-place-get -b --on-demand   # registered asleep, on its port
pdms up tp --on-demand                   # the whole stack (or on_demand = "on" in the stack)
pdms proxy -b                            # the proxy is what wakes them
```

- The **first request** through the proxy to one of its routes starts it (a few seconds) and waits for it; the next
  ones go straight to it, and requests that arrive while it starts wait for the same start.
- After **`idle_minutes`** (15) without requests through the proxy, and none in progress, the proxy stops it again.
- `pdms ps` and pdms ui show it as **asleep**. `pdms restart` (or **Start now**) starts it right away, and it still
  sleeps when idle; `--always-on` (or **Always on** in Services) keeps it running. `pdms stop` and `pdms down` forget it.
- Only through the proxy: calling its port directly (`localhost:28101/docs`) while it is asleep finds nothing.
  SQS consumers are never on demand, since they read their queue all the time.
- `on_demand = true` in the defaults (Settings → **Services on demand**) makes it the default for `pdms run -b`,
  `pdms up` and pdms ui; a stack can say `on_demand = "on"` or `"off"` whatever the default.

## Stacks

```bash
pdms stack add                    # wizard: name, services, a fixed user/DB or "ask when starting"
pdms stack add fix-1234 --running # the services of the repo running now, as they run
pdms stack rename fix-1234 tp-fix
pdms up tp                        # starts what is not running, each on a free port (-u / -d to choose)
pdms up --setup Prospecting       # the stack of a setup saved in pdms ui
pdms restart --stack tp lead-place-get -u tp-provider --remember   # that service always with that user
pdms down tp
```

A stack made from the running services takes the user and database most of them use, and keeps the others' as
exceptions. Exceptions (`--remember`, or "Remember in the stack" in pdms ui) live in the stack's `overrides`:

```toml
[stacks.tp]
services = ["lead/lead-place-get", "lead/lead-tp-update"]
user = "supervisor"
db = "web-dev"

[stacks.tp.overrides."lead/lead-place-get"]
user = "tp-provider"
```

## Proxy

```bash
pdms proxy -b              # http://localhost:28800 in the background (pdms logs proxy, pdms stop proxy)
pdms proxy --as agent      # act as another user on local services, without restarting them
pdms proxy routes -f tp    # which service handles each route, and where it goes now
```

- **Routes** come from the repo's Terraform (`api_rsc_*.tf`), matched like API Gateway.
- A request goes to the **running local service** of the current repo; anything else to the **remote API** (read from
  `frontend/.env`, `--remote URL` for another, `--no-remote` to answer 503). It answers CORS preflights itself.
- `--as USER` sends the `X-Dev-*` headers, only to local services.
- It points the frontend at itself in `frontend/.env.local` (git-ignored) and puts the file back when it stops.
- `http://localhost:28800/docs` is one Swagger UI for every local service.

## Frontend

```bash
pdms front -b              # yarn dev in <repo>/frontend: https://localhost:3000
pdms front -b --build      # a production build (yarn build) served with vite preview; --rebuild forces it
```

It checks Node and yarn and runs `yarn install` when `node_modules` is out of date. The `@alivi` packages come from
AWS CodeArtifact, with a token that lasts 12 hours: when npm no longer accepts it, pdms logs in by itself before
`yarn install`, with a profile of the account `frontend/codeartifact-login.sh` names (`pdms aws codeartifact` does
it by hand). A build keeps the API URL it
was made with, so pdms builds again only when that, `yarn.lock` or the code changed.

### Opening the local frontend from the known front

With the pdms extension in Chrome, Edge, Brave, Chromium or Firefox, going to `https://dev-pdm.alivi.com/...` (also `qa-pdm`
and `uat-pdm`, never production unless you add it) opens `https://localhost:3000/...` with the same path and query.
Links in emails and redirects from the backend then land in your local app.

```bash
pdms browser install       # writes the extension and opens the browser's extensions page (or Settings → Browser)
pdms browser redirect on   # or the "Known front" switch on the Frontend tile; --host to choose the fronts
pdms browser               # the redirect, and the browsers where the extension is connected
```

You load the extension once: turn on Developer mode on the extensions page, click **Load unpacked** and choose the
folder pdms prints. Firefox only takes extensions Mozilla signed, so there pdms opens the signed file that comes with
each release and Firefox asks whether to add it. When pdms brings a newer version, Settings → Browser offers to
update it. It asks pdms ui every half a minute what to do, so it only redirects while pdms ui and the
frontend run. Otherwise the known front opens as always. Its icon in the toolbar says which: **ON** (redirecting), **OFF** (the
redirect is off), **…** (on, but the frontend is not running) or **!** (pdms ui is not open). Click it to see where it
redirects, turn the redirect on or off, or check right away. After a pdms update it reloads its own files. The local app
keeps its own login, so you sign in there once.

## Databases, tests and migrations

```bash
pdms db add                       # wizard; `protected = true` asks before a service uses it
pdms db test                      # connection and round trip of every database
pdms user import -d local -s ana  # development users from a database's pdms_user table
```

**pdms's own Postgres** (Docker `pdms-postgres`, port 5440, data in a volume) holds two things:

```bash
pdms db local up                         # start it, with 4 empty test databases
pdms db local refresh                    # copy pdm_template_dev (only read) as the alias pdms-local
pdms db local snapshot save fresh        # keep the copy as it is…
pdms db local snapshot restore fresh     # …and come back to it in a second
```

- The **local copy** (`pdms-local`) runs services without web-dev's slow round trips: schema, Flyway history,
  reference data and the repo's pending migrations.
- The **test databases** (`pdms_test_1`…) are the only ones tests ever run on: PDMS's integration tests drop every
  table of their database, so a database with data (web-dev, `pdms_sync`) would be wiped. `pdms test` refuses any
  other.

```bash
pdms test lead-tp-create                 # poetry run pytest on pdms_test_1 (smart install first)
pdms test lead-tp-create -- -k create -x # arguments after -- go to pytest
pdms test core -d pdms_test_2            # a package too, on another test database
pdms migrate -d web-dev                  # Flyway info (validate too); read-only
pdms migrate migrate -d pdms-local       # apply pending migrations, only on a local database
```

Tests run with `DEVELOPMENT_MODE` off, as deployed (`--dev-mode` to turn it on). `pdms migrate` runs the pipeline's
Flyway image and arguments from the `pdms-db-migrations` checkout found next to the repo (`--migrations PATH` for
another), so only Docker is needed. pdms never downloads a Docker image without asking.

**Migrations of the checkout.** Flyway applies the working tree of `pdms-db-migrations`, uncommitted files included,
and pdms says which those are. On local databases only (never web-dev or a shared one):

- `pdms db local refresh` copies the template, then asks before applying what your checkout has that the template
  does not, and offers to prepare the test databases from the new copy.
- `pdms db local prepare-tests` (Data → **Prepare from the local copy**) makes test databases copies of the local
  copy, with its schema and Flyway history, and applies what they lack. The tests' own fixtures still drop and make
  their tables again when they run.
- Starting services (`pdms run`, `up`, `restart`, pdms ui) on a local database that lacks migrations asks whether to
  apply them first. Data and `pdms doctor` show what each local database lacks, and migrations that changed in the
  checkout after they ran: Flyway refuses those until `flyway repair` (Data → **Repair and apply**); for the local
  copy, making it again is cleaner.
- Migrations that write to `backup_data` find it: pdms creates it empty when a local database lacks it.

**Test views.** When you change a materialized view (`R__MV_<view>.sql`, committed or not) and start a service that
reads it against web-dev (`pdms run`, `up`, `restart`, pdms ui), pdms asks whether to try your version: a test copy
made from your SQL beside the real view, named with the ticket of the migrations repo's branch
(`pdms_report_lead_view_pdmp627`), always with its data. That service then reads the copy through a rewrite of its
queries, with no change to its code; the deployed ones keep the real view. When your user cannot create views there
(web-dev's usual one), you get the SQL to run in a database client, and pdms checks the copy is there before starting.

A service reads a view when its code, or its local packages' (report-common), names it; a `report-lead-*` service
is about the lead view, a general one (`report-download`) about all. Once your change is deployed or undone, Data →
Test views and `pdms doctor` point to the copy left over, with its `DROP`.

```bash
pdms view                                # the views you changed and their test copies
pdms view refresh pdms_report_lead_view  # today's data in the copy (a snapshot of when it was made)
pdms view drop pdms_report_lead_view     # back to the real view; the copy goes (or you get the DROP to run)
pdms view create / use / stop / sql      # the same steps by hand
```

The copy's SQL ends granting `SELECT` to the user the services connect as (`pdm-writer` on web-dev), and pdms checks
that user can read it before starting. Refreshing or dropping a copy takes its owner: made from a database client as
another user, pdms gives you the SQL.

## AWS, events and emails

### AWS profile

```bash
pdms aws profile pdm-dev   # the profile services use (--none: leave AWS as the terminal has it)
pdms aws login             # aws sso login for it
pdms aws status            # the profile, its session and what was read from its Lambdas
```

Services get `AWS_PROFILE` and, from that account's Lambdas (read-only, once a day), only the bucket variables and
`EMAIL_SENDER`; the rest of a Lambda's environment is never kept. Services take their credentials from the AWS CLI
(`credential_process`), since the botocore they pin cannot refresh an SSO session: they keep working for the whole
login.

### Local events (SQS)

```bash
pdms events map -f email   # event type → queue → consumer, read from the repo's Terraform and common/event
pdms events up             # ElasticMQ in Docker with every queue (:9324), plus the broker
pdms run email-notify -b   # a consumer: pdms polls its queue and calls its Lambda handler
pdms events send email-notify -b '{"to_emails": ["a@x.com"]}'   # through the broker (--template shows the fields)
pdms events peek email-send-sqs-queue.fifo                       # without consuming; purge to empty it
pdms events down
```

While local events run, every service pdms starts publishes to the local broker, which routes each event to its
queue as in AWS. Nothing in `.env` changes: a `sitecustomize` sends the SQS clients to ElasticMQ (other AWS clients are
not touched). The `events` setting chooses: `auto` (local while ElasticMQ runs), `local` or `aws`. A failed message is
retried after 5 s and dropped after 3 attempts, since there is no dead-letter queue locally.

**SNS.** With local events, whatever a service publishes to any topic stays in the local queue `pdms-sns` and in a
readable log (`pdms logs sns`, Events → SNS), answered as AWS would.

### Emails

```bash
pdms config email-to you@alivi.com   # emails go only to you, from the next start of each service
pdms config email-to ""              # back to nobody: they only show in pdms ui
pdms logs emails
```

Whatever a service run by pdms sends through SES never reaches its recipients. By default it never leaves the
machine: it shows in Events → Emails (headers, text, HTML in a sandboxed frame) and in the trace of its request. With
an address it goes through the real SES only to you, with `[to: <recipients>]` before the subject. Bulk sends are
never sent.

## Debugging in VS Code

```bash
pdms debug lead-tp-create -u supervisor -d local -p 8090
```

Adds a configuration to the repo's `.vscode/launch.json` (the service's Python, no `--reload`): Run and Debug → F5.
The variables go to an env file in pdms's state folder (permissions `0600`), never into `launch.json`. Breakpoints in
`common/` go in the copy under `.venv/lib/python3.*/site-packages/`. In pdms ui, **Debug** does all of this and opens
VS Code. `pdms env -u supervisor -d local` prints the variables (`eval "$(pdms env …)"`, `--dotenv`).

## WSL

Inside WSL pdms runs as on Linux (keep the repos in the Linux home: on `/mnt/c` Poetry, yarn and `--reload` are much
slower), and what reaches the desktop goes to Windows: `pdms ui` and `pdms open` open the Windows browser, and
`pdms browser install` finds the Windows browsers and writes the extension to a folder they can open
(`AppData\Local\pdms`). WSL2 forwards `127.0.0.1`, so pdms ui, the proxy and the web app answer as usual. The web
app's certificate (mkcert) is trusted inside WSL only: `pdms browser trust` adds it to Windows. `pdms ui --install`
and `--at-login` add pdms to Windows' Start menu and to its Startup folder: a small script starts pdms ui inside WSL
with no window, and keeps WSL running while it does; it opens pdms ui in a window of its own. With WSLg (Windows
11) that window, also `pdms ui --window`, is pywebview's as on Linux; without WSLg's display or pywebview, Windows' Edge
(or Chrome) opens the page as an app (no tabs, no address bar). `pdms doctor` has a WSL section.

## Configuration

| | Linux / macOS | Windows |
| --- | --- | --- |
| Configuration (`PDMS_CONFIG`) | `~/.config/pdms/config.toml` | `%USERPROFILE%\.config\pdms\config.toml` |
| State: instances, logs, caches (`XDG_STATE_HOME`) | `~/.local/state/pdms/` | `%USERPROFILE%\.local\state\pdms\` |

The configuration holds database passwords: it is written with permissions `0600`; never commit it. Edit it with
`pdms config` (or Settings in pdms ui). The defaults and what they do:

```toml
[defaults]
language = "en"              # or "es" (pdms config language es; PDMS_LANG=es for one command)
port = 28100                 # first port tried for a service (the next free one when busy)
proxy_port = 28800
logging_level = "DEBUG"
reload = true
install = true
smart_install = true         # install only when something changed
events = "auto"              # auto | local | aws
events_port = 9324
parallel_requests = true     # see "Slow databases"
warm_connections = 2
query_stats = true
aws_profile = "pdm-dev"      # pdms aws profile
email_to = ""                # pdms config email-to
update_check = true
notify = true                # desktop notifications from pdms ui
ui_at_login = false
db_timeout = 15              # seconds for pdms db test
proxy_timeout = 300          # seconds the proxy waits before a 502
on_demand = false            # see "On demand"
idle_minutes = 15
front_redirect = false       # pdms browser redirect on
front_redirect_hosts = ["dev-pdm.alivi.com", "qa-pdm.alivi.com", "uat-pdm.alivi.com"]

[defaults.env]               # extra variables for every service

[users.supervisor]
user_id = "00000000-0000-0000-0000-000000000001"
username = "supervisor@example.com"
roles = "TPR.Supervisor"

[dbs.local]
host = "localhost"
port = 5432
database = "pdm"
user = "postgres"
password = "..."
protected = false

[stacks.tp]
services = ["lead/lead-tp-list", "lead/lead-tp-details"]
user = ""                    # empty: ask when starting
db = ""
on_demand = ""               # "on", "off" or "": as the default
```

**Repos.** pdms works with several PDMS checkouts; the **current** one is where services, stacks and migrations come
from (`pdms repo list | add | use | edit | remove`). Run pdms inside another checkout and it offers to switch. Stacks
store paths relative to the backend, so they work with any repo.

**Sharing with the team.**

```bash
pdms config export team.toml --no-secrets   # users, databases, stacks, defaults; no passwords
pdms config import team.toml                # shows what is new or changed and asks what to overwrite
```

`--only users,stacks` picks sections; `--secrets` includes passwords (don't commit that file). Importing keeps the
passwords you already have and backs up the configuration first. Repos are never exported: their paths are yours.

## Commands

| Command | What it does |
| --- | --- |
| `pdms` / `pdms ui` | Interactive menu / web interface |
| `pdms setup` / `pdms doctor` | Guided setup / check everything pdms needs |
| `pdms run`, `debug`, `env`, `services` | Run a service, debug it in VS Code, print its variables, list the repo's services |
| `pdms ps`, `logs`, `urls`, `open`, `restart`, `stop`, `adopt` | Background services |
| `pdms stack`, `up`, `down` | Stacks (`list`, `add`, `rename`, `edit`, `remove`) |
| `pdms proxy`, `front` | Local API gateway, the web app |
| `pdms browser` | Extension that opens the local web app from the known front (`install`, `redirect`, `status`) |
| `pdms events` | Local SQS (`map`, `up`, `status`, `send`, `peek`, `purge`, `down`) |
| `pdms aws` | AWS profile (`profile`, `login`, `status`, `read`) |
| `pdms db`, `user`, `repo`, `config` | Databases (and `db local`), development users, repos, settings |
| `pdms view` | Test copies of materialized views (`create`, `use`, `stop`, `drop`, `sql`) |
| `pdms test`, `migrate` | Tests on a test database, Flyway |
| `pdms self-update` | Update pdms (`--list`, `--version` to go back) |
| `pdms bug-report` | A report to send with a problem, without passwords |

`pdms <command> --help` shows every option.

## Development

```bash
git clone git@github.com:lianabeatriz93/pdms-cli.git ~/Code/Alivi/pdms-cli && cd ~/Code/Alivi/pdms-cli
uv sync && uv run pytest
uvx ruff check src tests                  # errors and likely bugs only, as the CI runs it
PDMS_E2E=1 uv run pytest tests/e2e        # pdms ui in a browser (Playwright's Chromium, else your Google Chrome)
uv tool install -e '.[desktop]' --force   # use the checkout as `pdms` (again when dependencies change)
```

With the editable install, a change to the page shows when you reload it, and a Python change when pdms ui (or the
service) restarts.

- `actions/` does things without asking; a decision it needs is raised for the caller. The terminal
  (`commands/`, one module per group; `cli.py` only wires them) and pdms ui both call it as `actions.<name>`. One
  module per area (services, stacks, proxy, settings…); between them, functions are called through their module
  (`services.plan_service`), so replacing `actions.<name>` in a test reaches every caller (`tests/test_actions_package.py`).
- `ui/`: the server (`server.py`: who may ask, replies and streams; `api.py`: what each route does, as tables of
  methods; `jobs.py`, `state.py`) and the page in `static/`, plain ES modules with no build step (`js/`, one per
  screen). `tests/test_ui_api.py` fails when the page calls a route `api.py` does not have.
- `sqs_patch/`: what pdms loads into the services it runs (`sitecustomize.py` and its modules).
- Every text is written in English inside `_()` (`i18n.py`) or `t()` (`static/i18n.js`) and translated to Spanish
  there; the tests fail when one is missing.

**CI** runs once per commit of a pull request and on every push to `main`: Linux with Python 3.10 and 3.12, macOS and
Windows with 3.12, plus ruff and pdms ui in Chromium (`tests/e2e`: the real server and page, with what would start
processes answered by the test). `gh workflow run ci.yml --ref <branch>` runs all three systems with both Pythons; a
`bump:` commit only runs ruff, since its tag's release runs the tests. The installers (macOS runners can queue for long) only when
what they try changed, on pull requests and on `main`: `installer/`, the package in `pyproject.toml` (not the dev
dependencies nor `uv.lock`), the app menu entries or their own job (`.github/installers_changed.py` decides).
Markdown-only changes run nothing.

### Releasing

Every release is a stable **patch** (`0.4.1` → `0.4.2`); the minor goes up only for something significant.

```bash
git checkout main && git pull --ff-only
uvx --from commitizen cz bump --increment PATCH --files-only --yes   # MINOR for a significant one
git commit -am "bump: version 0.4.1 → 0.4.2"
git tag -a v0.4.2 -m v0.4.2
git push origin main v0.4.2
```

Before tagging, on a build of `main` (`uv tool install -e '.[desktop]' --force`), a few minutes by hand of what
the tests cannot see:

- [ ] `pdms ui --window` opens, and the screens the release changed work (start a service twice in a row too);
- [ ] Linux, plus Windows or WSL whenever the release touches the window, the installers, updating or paths;
- [ ] the CI of `main` is green (`gh workflow run ci.yml` for all three systems with both Pythons, when it touched
      something system-specific).

A change that is hard to try here goes first as a **beta** for whoever is on that channel:
`cz bump --increment PATCH --prerelease beta --files-only --yes` makes `0.4.13b0` (the next one, `0.4.13b1`), committed,
tagged and pushed the same way. GitHub marks it as a pre-release, so the installers and the stable channel never get
it; once it works for them, `cz bump --increment PATCH --files-only --yes` makes the stable `0.4.13`.

`--files-only` updates `pyproject.toml`, `uv.lock` and `CHANGELOG.md` without committing or tagging, since a plain
`cz bump` makes a lightweight tag that `git push --follow-tags` leaves behind. The tag runs `release.yml`: it checks
the version, runs the tests and publishes the wheel, both installers and the changelog as a GitHub release.

The browser extension for Firefox is signed in the same workflow, as an unlisted add-on on addons.mozilla.org, with
the `AMO_JWT_ISSUER` and `AMO_JWT_SECRET` secrets of the repo. Mozilla signs each version once: change `version` in
`browser_extension/manifest.json` whenever the extension changes, and releases in between reuse the signed file of an
earlier release.
