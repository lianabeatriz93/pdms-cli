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
