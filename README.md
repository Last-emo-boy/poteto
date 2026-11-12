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

Run `poteto login` with your IF.Link account, or point `POTETO_TOKEN_FILE` at a project-scoped token file. On hosted Poteto, a first login creates a personal preview project unless the account is already linked. CLI login grants preview access to that project, not production access. Keep credentials out of prompts and upload directories. Windows login credentials use `%LOCALAPPDATA%\Poteto\token.json`; existing `~/.config/poteto/token.json` credentials remain usable.

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

`deploy`, `ls`, `env`, `database`, `volume`, `promote`, `rollback`, and `project status` can use the linked project. Explicit `--project` takes precedence. API selection is `--url`, then `POTETO_URL` / legacy `PAAS_URL`, then the directory link, then the distribution default. A link from another API is never used as the selected project's default, and a login file cannot authenticate a different API. For a manual token, confirm a linked non-default API with `--url` or `POTETO_URL`; project files alone cannot redirect that token to another server.

Use `poteto deploy . --prod` only when you intend a production deployment and have production permission. It is mutually exclusive with `--preview`. Existing manifest defaults are preserved; linking a directory makes otherwise unspecified deployments previews. `inspect`, `status`, and `logs` accept a deployment ID or an actual Poteto preview URL. `inspect` also resolves a production application URL to its current deployment.

`POTETO_TOKEN` can supply a token through the environment; the existing `PAAS_TOKEN`, `PAAS_TOKEN_FILE`, and `PAAS_URL` variables remain supported. Never put a token in a command argument.

Agents can follow the [Poteto preview skill](skills/poteto-deploy/SKILL.md) for installation, preflight, publishing, and verification.

Use `poteto logs DEPLOYMENT_ID --follow` to watch a build as it runs, including a deployment started with `--no-wait`. Build output goes to stderr; the final deployment remains JSON on stdout.

## Runtime variables and redeploys

```sh
poteto env list --target preview
poteto env set API_KEY --file /secure/api-key.txt --target preview
poteto env rm OLD_KEY --target preview
poteto redeploy https://preview-DEPLOYMENT_ID.page.iflink.tech --url-only
```

`env set` updates one variable without replacing the others. Use a value file outside the upload directory, `--stdin`, or the hidden interactive prompt; values are never accepted as command arguments or returned by the API. New `env` subcommands default to preview. The existing `poteto env --file variables.json` still replaces the full production environment unless `--target preview` is specified. Variable management requires `env:write`; an agent's preview-only token cannot edit variables.

Changes apply to the runtime of new deployments. `redeploy` rebuilds the original uploaded source using the current runtime environment and keeps its build configuration and resource bindings. It creates a new deployment, leaving the original untouched. It defaults to preview; production requires `--prod` and production permission. A database or volume binding must belong to the selected stage. A configured migration command runs again, so it must be repeatable and backward-compatible.

The CLI prints a request ID to stderr. If a request is interrupted, repeat with `--request-id THE_SAME_ID` to resume the same candidate rather than creating another. `--no-wait` returns the queued candidate; `inspect` and `logs` can observe it later. `retry` remains the command for resuming a failed candidate in place.

## Framework presets

```sh
poteto plan . --preset nextjs --preview --json
poteto deploy . --preset nextjs --preview
poteto deploy . --preset vite --preview
```

Or add `"preset": "nextjs"` / `"preset": "vite"` to `poteto.json`. Use the same preset for preflight and deployment. Explicit manifest settings override preset defaults; a selected preset requires its matching runtime. The preset field is consumed locally, not sent as an extra server parameter.

- **Next.js:** npm build + npm start, Node runtime, 512 MiB with an explicit preset. Existing Next.js projects with both scripts are detected automatically without changing their previous resource default. This targets the Node server and server-side rendering; static exports can be built separately and uploaded from `out/`.
- **Vite:** npm build + static `dist/` hosting. Set `output_directory` if your Vite configuration uses a different output folder. Production never runs the Vite development or preview server.

Both require `package-lock.json`. Runtime variables are available to server-side code; build-time public variables such as `VITE_*` and `NEXT_PUBLIC_*` are not injected into the isolated builder. Build those assets with their intended public configuration before uploading them, or define that build configuration in the application's Dockerfile.

## Platform source

- `publisher/`: API, CLI, OAuth, deployments, PostgreSQL and volume binding, and project backup and import.
- `execution/`: the VM builder, optional native Docker builder, and tenant runtime/restore services.
- `infra/`: service and networking templates; `fixtures/`: runnable sample applications.

The hosted platform's credentials, routing configuration, and operational state are not part of this repository. For a separate installation, configure your own domains, VM access, and edge routing. The VM launcher templates expect an x86-64 Linux host with KVM. The execution host can set `npm_registry` in its `config.json` to a credential-free HTTPS npm mirror; leaving it unset preserves npm's default registry.

In the IF.Link login configuration pointed to by `PAAS_OIDC_CONFIG_FILE`, set `"auto_provision": true` to create a personal preview project after the first verified sign-in. Existing `projects_by_subject` bindings retain their projects. Leave auto-provisioning off to require an explicit binding.

## Trusted native builds on Linux

Poteto defaults to a disposable VM builder (KVM or explicitly configured TCG). On a Linux/amd64 host with Docker and Buildx, an operator can enable the independent native driver for projects whose source, dependencies and uploaders are trusted. Tenant runtimes, data and routes still use their existing VM path. A login or scoped upload token does not make code trusted.

Create one dedicated BuildKit builder per trusted project, using the configuration directory under the executor state root (default shown). Choose a vetted BuildKit image for your registry with the driver option `image=...` if the default is not reachable.

```sh
STATE=/srv/ovh-paas-execution
PROJECT=YOUR_24_CHARACTER_PROJECT_ID
CFG="$STATE/native-docker"
sudo install -d -m 0700 "$CFG"
sudo env HOME="$CFG" BUILDX_CONFIG="$CFG/buildx" \
  docker --config "$CFG" --host unix:///var/run/docker.sock buildx create \
  --name "poteto-native-$PROJECT" --driver docker-container \
  --driver-opt memory=4g,memory-swap=4g,cpu-period=100000,cpu-quota=200000,restart-policy=no \
  --buildkitd-config infra/execution/native-buildkit.toml unix:///var/run/docker.sock
```

Merge `"native_build_projects": {"PROJECT_ID": "OWNING_ACCOUNT_ID"}` into the executor's existing `config.json`. The publisher passes project identity internally; there is no client-side builder switch. Existing jobs keep their driver, and image imports always stay on the VM path. BuildKit caches are scoped to the dedicated project builder and kept when it stops. The supplied configuration sets cache collection targets; leave disk headroom for active builds.

Remove the pair to disable native builds. A queued native job that loses authorization fails rather than switching drivers. After jobs drain, the operator can remove the dedicated builder and its cache without touching application data.

## VM-free previews

Prebuilt static sites can be served by one shared Nginx service instead of a VM per tenant. On the publisher host, create a world-traversable `static-releases` directory under the publisher state directory and start `infra/static-artifacts.compose.yml` on the existing Dokploy network. Set `"static_artifacts": ["PROJECT_ID"]` (or `true` for all projects) in `/etc/paas-publisher-runtime.json` and reload the publisher services. Upload an `index.html` site or point `output_directory` at a prebuilt `dist/` with no build command. Static releases still support preview, promotion, rollback and source-only encrypted export. Existing releases retain their runtime.

With the isolated executor connected, also set `"static_source_builds": true` to accept static source projects such as Vite on that lane. The disposable builder runs the build command and exports only the files under `output_directory`; no application image or tenant VM is needed to serve the release. The built files count toward the account's storage quota. Encrypted project exports include both the original source and the built files, so a destination with direct static hosting can restore the exact release without rebuilding it.

Install and enable `infra/paas-publisher-static-previews.service` alongside the updated publisher worker if a long build should not delay another project's prebuilt preview. This extra worker never claims source builds or production switches. The original worker can still serve prebuilt previews when the fast lane is off.

For static source builds, the executor can also prewarm the official Node base image. After the build queue drains, run `sudo systemd-run --unit=paas-builder-prewarm --property=WorkingDirectory=/opt/paas-execution --property=RuntimeMaxSec=900 /usr/bin/python3 -m execution.host prepare-builder-base docker.io/library/node:24-alpine@sha256:DIGEST` with a verified digest. This creates a node-local base from the clean guest and expires the selection after seven days. Each job still gets a new overlay; project files, dependencies and environment values are never written back to the base. Disable the selection by removing `builder-profile.json` from the execution state directory. Keep previously selected base files until the disposable builder has stopped; the cache can be recreated after restoring to another host.

The experimental gVisor path is limited to stateless previews of trusted native builds. Install the [gVisor containerd shim](https://docs.docker.com/engine/daemon/alternative-runtimes/) on the executor, install the sandbox network unit, nftables rules and systemd slice from `infra/execution/`, and load the execution egress policy. If the controller VM has an additional host loopback filter, allow its service UID to reach `127.0.0.1:23000–23031` there as well. Use the same trusted project ID in the publisher's `sandbox_projects` list and the executor's `sandbox_projects` project-to-account map. Sandbox containers have loopback-only ingress and no outbound access. Data bindings, sleep and production continue to use the existing runtime. A sandbox preview that fails setup does not run unsandboxed; previews do not automatically start after an executor reboot.

## Develop

With Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run poteto --help
uv run python -m unittest discover -s tests/publisher
```

Licensed under [MIT](LICENSE).
