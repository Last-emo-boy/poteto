# Poteto

Poteto publishes an application directory to an HTTPS preview. Use it from a terminal or give an agent a project-scoped preview token. No framework-specific directory layout is required.

```sh
poteto plan . --preview --json
poteto deploy . --preview --url-only
```

`plan` checks the upload locally without authenticating, building, or creating a deployment. It reports the runtime, build and start commands, and which directory will be public. Upload a ready-made static directory, use the npm runtime for a Node app, or bring a Dockerfile. Keep non-default build settings in `poteto.json`.

## CLI and Agent Skill

The [hosted installer](https://poteto.iflink.tech/) supports Windows, macOS and Linux with Python 3.10 or newer. No administrator access is needed.

macOS / Linux:

```sh
curl -fsSL https://poteto.iflink.tech/install.sh | sh
```

Windows, in PowerShell 7 or newer (`pwsh`):

```powershell
irm https://poteto.iflink.tech/install.ps1 | iex
```

Windows installs `poteto.cmd` in `%LOCALAPPDATA%\Programs\Poteto\bin` and adds it to your user PATH. Set `POTETO_INSTALL_DIR` for a custom location. To install without changing PATH, download and run `install.ps1 -NoPath`.

Run `poteto login` to use an approved IF.Link account, or point `POTETO_TOKEN_FILE` at a project-scoped token file. Keep credentials out of prompts and upload directories. Windows login credentials use `%LOCALAPPDATA%\Poteto\token.json`; existing `~/.config/poteto/token.json` credentials remain usable.

## Link a directory once

```sh
poteto login
poteto link --project PROJECT_ID
poteto plan . --preview --json
poteto deploy . --preview --url-only
poteto ls
poteto inspect https://preview-DEPLOYMENT_ID.page.iflink.tech
```

With an IF.Link login, `poteto link` can use the login's project without `--project`. A link stores only the project ID and API origin in `.poteto/project.json`. It is excluded from uploads and ignored by Git. Child directories inherit the link, except across a nested Git repository. `poteto unlink` removes only the local link, not the remote application.

`deploy`, `ls`, `env`, `database`, `volume`, `promote`, `rollback`, and `project status` can use the linked project. Explicit `--project` takes precedence. API selection is `--url`, then `POTETO_URL` / legacy `PAAS_URL`, then the directory link, then the distribution default. A link from another API is never used as the selected project's default, and a login file cannot authenticate a different API.

Use `poteto deploy . --prod` only when you intend a production deployment and have production permission. It is mutually exclusive with `--preview`. Existing manifest defaults are preserved; linking a directory makes otherwise unspecified deployments previews. `inspect`, `status`, and `logs` accept a deployment ID or an actual Poteto preview URL. `inspect` also resolves a production application URL to its current deployment.

`POTETO_TOKEN` can supply a token through the environment; the existing `PAAS_TOKEN`, `PAAS_TOKEN_FILE`, and `PAAS_URL` variables remain supported. Never put a token in a command argument.

Agents can follow the [Poteto preview skill](skills/poteto-deploy/SKILL.md) for installation, preflight, publishing, and verification.

## Platform source

- `publisher/`: API, CLI, OAuth, deployments, PostgreSQL and volume binding, and project backup and import.
- `execution/`: isolated VM build and runtime services, with restore and reconciliation tools.
- `infra/`: service and networking templates; `fixtures/`: runnable sample applications.

The hosted platform's credentials, routing configuration, and operational state are not part of this repository. For a separate installation, configure your own domains, VM access, and edge routing. The VM launcher templates expect an x86-64 Linux host with KVM.

## Develop

With Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run poteto --help
uv run python -m unittest discover -s tests/publisher
```

Licensed under [MIT](LICENSE).
