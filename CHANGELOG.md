## v0.4.0 (2026-10-04)

### Feat

- **aws**: services use the chosen AWS profile and the buckets pdms reads from that account's Lambdas
- **ui**: Data shows how each database travels, its last hour and its migrations, and Home offers the local copy
- **data**: every database measured while Data is open, migrations applied on local ones, restore around the services
- **db**: pdms asks before downloading a Docker image, and Doctor lists the ones it needs
- **ui**: a Data screen with the connections, the local copy, its snapshots and the test databases
- **db**: a local copy of pdm_template_dev in pdms's Postgres, with snapshots
- **ui**: All logs in one click, every log of the dock on one connection
- **ui**: a log dock under every screen, and a request's events in its trace
- **trace**: a request is followed through the services, their logs and their events
- **proxy**: a request shows in Requests as soon as it arrives, with its time counting
- **ui**: SNS publishes show their date as well as their time
- **ui**: a Changed directly tab in Tests, apart from what a changed package drags in
- **test**: choose development mode for a test run, asked when the service's .env turns it on
- **ui**: a Tests screen with what your changes touch, failures linked to the line, and Re-run failed
- **test**: tests only on a local database, results kept per project, and what the branch touches
- **ui**: setups with a name on Home, switching between them in one click
- **ui**: Home says which running services run old code, and reinstalls and restarts them
- **ui**: pick several services to restart or stop, and restart part of a stack as another user
- **restart**: several services at once, and part of a stack with another user it can remember
- **proxy**: a request's trace says how long it waited for the service and the way back
- **ui**: a status bar with the round trip to the database, and the tunnels and the line in Doctor
- **proxy**: Requests shows the database time and the queries of each request
- **run**: each request says how long it spent in the database and on which queries

### Fix

- **test**: tests run only on pdms's own test databases, never on one that keeps data
- **logs**: the mark of an appended log is plain ASCII, and Windows does not cut logs in use
- **logs**: logs stay small, pdms's health checks stay out of them, and All logs shows only new lines
- **test**: tests run with development mode off, whatever the service's .env says
- **ui**: Tests loads when the page opens on it, and finds what changed in a fraction of a second
- **ui**: only the content scrolls, so the sidebar and the title bar never scroll away
- **ui**: say how to fix a page that cannot load its scripts

### Refactor

- **ui**: the page's script as one ES module per screen
- **cli**: one module per command group in pdms_cli.commands

## v0.3.4 (2026-10-03)

### Feat

- **run**: requests in parallel and connections opened at start, for slow databases

## v0.3.3 (2026-10-03)

### Feat

- **ui**: Events says if Docker and its port are ready, and Look is picked from cards
- **ui**: Ctrl K to search or run any action, and / to filter the current view
- **ui**: desktop notifications and a Recent panel on Home
- **proxy**: each request kept with its detail, Copy as curl and Replay
- **ui**: tracebacks open the code in VS Code, and Debug in VS Code from a service row

## v0.3.2 (2026-10-02)

### Feat

- **ui**: Stacks as chips in two columns, Events explained while off, Doctor problems first
- **ui**: Services grouped by stack, quick filters, and row actions as icons
- **ui**: Home puts what needs attention first, with Start everything as joined steps
- **ui**: icons and live state in the sidebar, narrow windows, and light or dark at will

### Fix

- **instances**: a service busy with a slow request shows as busy, not starting

## v0.3.1 (2026-10-02)

### Feat

- find the services that run outside pdms, and adopt or stop them

### Fix

- **instances**: adopting on Windows keeps the log the service still writes
- **ui**: readable filled buttons in dark mode, and the local SNS off in grey
- **frontend**: warn when the frontend calls a local API nobody answers on
- **instances**: a clock adjustment no longer makes running services look stopped

## v0.3.0 (2026-10-02)

### Feat

- services from port 28100 and the proxy on 28800 by default
- **ui**: the Flyway migrations of a database, pending or applied
- **ui**: Repos in Settings and a Doctor screen
- **ui**: pdms in the app menu with a tray icon, and updates from pdms ui
- **ui**: Home with Start everything, pdms front for the web app, and renaming users
- **proxy**: configurable timeout, 300 s by default
- **ui**: the pdms ui window shows the pdms icon

### Fix

- **ui**: pdms ui opened from the menu finds pyenv, nvm and the other shell tools
- **cli**: never crash on a character the output encoding cannot write
- **ui**: menu entries run this pdms on Windows, and the tests pass in CI

### Refactor

- **repos**: non-interactive actions for repos, and pdms repo edit

## v0.2.5 (2026-10-01)

### Feat

- **ui**: pdms ui in the language of pdms, switching live
- **ui**: pdms ui --window installs pywebview by itself on Windows and macOS
- **ui**: roles to tick, settings export/import and users from a database
- **ui**: settings screen for databases, users and defaults, and an icon
- **ui**: start the SQS consumers and the broker from the events screen
- **ui**: start a single service from pdms ui
- **ui**: filter the services and stacks of pdms ui
- **ui**: open pdms ui in a window of its own with --window
- **ui**: coloured JSON for event messages and wider stack cards
- **ui**: local events screen in pdms ui
- **ui**: start, stop and follow the proxy from pdms ui
- **events**: follow the local SNS in pdms ui and with pdms logs sns
- **ui**: start, stop and edit stacks from pdms ui
- **ui**: stop, restart and follow the logs of services from pdms ui
- **ui**: add the pdms ui web server with live state
- **proxy**: run the proxy in the background and start it through actions

### Fix

- **install**: keep poetry out of the virtualenv pdms runs in
- **ui**: open the JSON nested in the strings of event messages
- **events**: keep SQS consumers ready in pdms ps and pdms ui
- **events**: open JSON sent as a string in the local SNS log
- **events**: always show the local SNS queue in pdms events status
- **events**: keep every SNS publish in one local queue with local events
- **proxy**: skip the reverse DNS lookup when binding the proxy
- **proxy**: recognize the background proxy by its port, not the spawned pid

### Refactor

- **config**: non-interactive actions for databases, users and defaults
- **actions**: plan, start, stop, save and remove stacks through actions
- **actions**: move starting, stopping and restarting services into actions

## v0.2.4 (2026-09-30)

### Feat

- **proxy**: put frontend/.env.local back as it was when the proxy stops
- **stack**: pick the stack's services from the whole repo in one list

### Fix

- **proxy**: offer the next free port when the default one is in use
- **cli**: pass every Typer parameter when calling commands directly
- **install**: reinstall copied path dependencies after poetry install

## v0.2.3 (2026-09-29)

### Feat

- **setup**: guided setup that imports a shared configuration first

### Fix

- **migrate**: download the Flyway image with retries
- **migrate**: Flyway instead of the frozen Alembic migrations

## v0.2.2 (2026-09-29)

### Feat

- PDMS banner when the interactive menu opens
- **events**: send, peek and purge events; debug consumers
- **events**: run the broker and event consumers locally
- **events**: services publish to the local broker
- **events**: event map of the repo and a local ElasticMQ

### Fix

- editable installs report the checkout's version
- **events**: UTF-8 logs for the poller and background instances on Windows
- **release**: merge pre-release entries into the final version's changelog

## v0.2.1 (2026-09-28)

### Feat

- **doctor**: check the environment and the configuration
- **ps**: warn when a running instance has outdated installed code
- **update**: tell when a new pdms version is available
- **release**: alpha pre-releases between significant versions

## v0.2.0 (2026-09-28)

### Feat

- **release**: versioned releases with installers for macOS, Linux and Windows
- support Windows and macOS
- **user**: read the role mapping from the current repo
- **proxy**: local API gateway on a single port
- **urls**: show the endpoints of running services
- **repos**: several PDMS checkouts with a current one
- pdms test and pdms migrate
- **user**: import development users from the pdms_user table
- **install**: skip poetry lock && poetry install when nothing changed
- **logs**: keep the previous run's log on restart
- **logs**: follow several instances at once
- **completion**: tab completion for services, instances and config entries
- **open**: open a background service in the browser
- **logs**: follow several instances at once
- **config**: export and import the configuration
- **i18n**: English by default with optional Spanish
- interactive pdms CLI to run PDMS services locally

### Fix

- **installer**: run native commands safely under Windows PowerShell 5.1
- **logs**: strip Windows line endings when following logs
