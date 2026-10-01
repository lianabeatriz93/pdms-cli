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
