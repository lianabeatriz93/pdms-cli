## v0.2.1a0 (2026-09-28)

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
