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
