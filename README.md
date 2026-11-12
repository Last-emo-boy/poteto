# Poteto

Poteto publishes an application directory to an HTTPS preview. Use it from a terminal or give an agent a project-scoped preview token. No framework-specific directory layout is required.

```sh
poteto plan . --preview --json
poteto deploy . --preview --url-only
```

`plan` checks the upload locally without authenticating, building, or creating a deployment. It reports the runtime, build and start commands, and which directory will be public. Upload a ready-made static directory, use the npm runtime for a Node app, or bring a Dockerfile. Keep non-default build settings in `poteto.json`.

## Check the server before uploading

```sh
poteto plan . --remote --preview --json
poteto plan . --prebuilt --remote --preview --json
```

Add `--remote` after login to check the selected project's publishing permission, runtime capacity, upload limits, storage budget and database/volume bindings. It sends configuration and byte counts, not application files, and creates no deployment. Use `--project PROJECT_ID` for a manual token. The check is a snapshot: deployment repeats admission checks, and generated files or normalized image storage are checked when available. Without `--remote`, `plan` remains offline and does not read credentials.

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

## Own several applications, delegate one at a time

With personal workspaces enabled, use a separate owner credential on your computer:

```sh
poteto login --production --manage-workspace --output /secure/owner.json
export POTETO_TOKEN_FILE=/secure/owner.json
poteto projects --create my-app
poteto projects
poteto token create-preview --project PROJECT_ID --name my-agent --output /secure/agent.json
```

Give the Agent the private `agent.json` file through `POTETO_TOKEN_FILE`, not the owner credential or a token pasted into its prompt. The file binds the API and project automatically. The preview token cannot publish production, manage environment variables or data, create projects, or issue more tokens. For an explicitly approved production pipeline, use `token create-production` instead; it grants deployment operations for that project, including rollback and non-current release cleanup, but not environment, data or token management. `token list` shows metadata; `token revoke TOKEN_ID` revokes a selected credential. Issued tokens expire no later than the owner credential (IF.Link logins last seven days), even if a longer lifetime is requested. Logging out revokes that login, not independently issued Agent tokens.

For direct work on another owned project, use `poteto login --project PROJECT_ID --output /secure/project.json`; add `--production` or `--production --manage-project` only for the permissions you need. The usual login still selects the original project. If you deleted that project, workspace login can list or create a replacement without creating another account; choose an active project explicitly for a deployment login.

New workspace accounts allow five active projects with one concurrent deployment. All projects share the same account storage and runtime budget; additional projects do not reserve additional CPU or memory. Operators enable `personal_workspaces: true` alongside `personal_previews` and `personal_production` in the publisher runtime configuration. Existing accounts keep their stored limits. To adjust a specific account, controller root can run `python -m publisher.admin set-project-limit ACCOUNT_NAME --confirm-account ACCOUNT_ID --projects 5`. This changes only its project count; legacy linked projects retain operator-managed authorization.

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

`plan --remote`, `deploy`, `ls`, `env`, `database`, `volume`, `promote`, `rollback`, and `project status` can use the linked project. Explicit `--project` takes precedence. API selection is `--url`, then `POTETO_URL` / legacy `PAAS_URL`, then the directory link, then the distribution default. A link from another API is never used as the selected project's default, and a login file cannot authenticate a different API. For a manual token, confirm a linked non-default API with `--url` or `POTETO_URL`; project files alone cannot redirect that token to another server.

Use `poteto deploy . --prod` only when you intend a production deployment and have production permission. It is mutually exclusive with `--preview`. Existing manifest defaults are preserved; linking a directory makes otherwise unspecified deployments previews. `inspect`, `status`, and `logs` accept a deployment ID or an actual Poteto preview URL. `inspect` also resolves a production application URL to its current deployment.

`POTETO_TOKEN` can supply a token through the environment; the existing `PAAS_TOKEN`, `PAAS_TOKEN_FILE`, and `PAAS_URL` variables remain supported. Never put a token in a command argument.

Agents can follow the [Poteto preview skill](skills/poteto-deploy/SKILL.md) for installation, preflight, publishing, and verification.

Use `poteto logs DEPLOYMENT_ID --follow` to watch a build as it runs, including a deployment started with `--no-wait`. Build output goes to stderr; the final deployment remains JSON on stdout.

## Resume an interrupted upload

CLI 0.12.0 and an updated server transfer artifacts in 2 MiB parts, with committed offsets and a complete-file integrity check before queueing the deployment. A dropped connection does not require uploading completed parts again.

```sh
poteto deploy . --local-build --preview
# If interrupted, in the same directory:
poteto deploy . --resume DEPLOYMENT_ID
```

The CLI saves the original artifact and its API/project/target in `.poteto/` with restricted file permissions, without credentials. `--resume` sends those same bytes; it does not rebuild or read modified application files. Omit build/configuration/target flags when resuming, and keep the original credential or another credential authorized for that project. Add `--project PROJECT_ID` for a manual token. The local pending copy is removed after the server confirms queueing. Unsubmitted server uploads expire after 24 hours and count toward storage quotas. The original whole-archive endpoint remains available for older clients; a new client uses it only when the server does not advertise resumable uploads.

## See reservations and clean up previews

```sh
poteto usage
poteto usage --json
poteto prune --older-than-days 7 --keep 3
```

`usage` shows the account's artifact-storage, runtime and concurrent-deployment budgets, plus accessible projects and the selected project's visible release reservations. These are the same reservations used by admission checks, not measured CPU or RSS; sleeping apps still reserve capacity. Database/volume disk space is separate from artifact storage. Project-scoped credentials never list another project's releases, and preview credentials do not list production releases.

`prune` is read-only by default. It protects production, every release referenced by a promotion/rollback operation, data-bound previews, active deployments, and the newest three ready previews. Only previews older than seven days are selected by default. It reports the reservations that deletion would release, not a promise about physical disk savings.

After reviewing the exact candidate IDs, use the printed command, for example:

```sh
poteto prune --older-than-days 7 --keep 3 --yes --ids PREVIEW_ID_1 PREVIEW_ID_2
```

Both `--yes` and `--ids` are required. The server rechecks every selected ID before queueing any deletion. It never adds new candidates to your selection. Cleanup uses the existing durable deletion jobs; repeat the same IDs after an interrupted request to observe pending jobs, retry failed cleanup, or confirm an already-removed release. Use `--no-wait --json` to retain the job IDs without waiting.

Ordinary Agent preview tokens remain deployment-only. Personal users can explicitly request preview cleanup during IF.Link login with `poteto login --manage-previews` (log out first, or choose a separate `--output` file). This grants `previews:manage` only for the bound project, not production, environment variables, token issuance or database/volume deletion. Existing management credentials with `deployments:write` also work. For manual tokens, add `--project PROJECT_ID` to these commands.

### Automatically retire old stateless previews

Retention is off for every project until its owner enables it. Inspect the proposed policy first:

```sh
poteto retention status --project PROJECT_ID
poteto retention set --project PROJECT_ID --older-than-days 7 --keep 3
poteto retention set --project PROJECT_ID --older-than-days 7 --keep 3 --yes
poteto retention disable --project PROJECT_ID
```

Without `--yes`, `set` is a read-only preview. Enabling requires both `projects:write` and `deployments:write`, available through an explicitly authorized project-management or workspace login; Agent deployment tokens and preview-cleanup tokens cannot change this persistent policy. The main worker checks enabled projects every minute and removes at most one eligible preview per project per interval, using the same production, rollback-history and data-binding protections as `prune`. Keep at least one ready preview; active uploads and projects with pending operations are left alone. Busy projects are reconsidered later.

These removals permanently delete the selected preview's runtime and artifact; there is no recovery bin. Persistent databases and volumes are never included. `status --json` shows the current candidates and recent job results. Disabling or tightening a policy takes effect before a queued job selects its release. A deletion already begun finishes through the existing crash-recovery path. Failed deletion remains visible and requires an explicit `prune --yes --ids ...` retry; the scheduler does not loop over it. Policies remain project settings after the enabling login expires. Project bundles do not copy retention settings; a new destination keeps its default disabled policy.

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

## Let a preview call external APIs

Default Agent tokens still deploy only. Request the permissions you need explicitly during login, keeping the resulting credential outside the application directory:

```sh
poteto login --manage-env --allow-web
poteto env set API_KEY --file /secure/api-key.txt --target preview
```

`--manage-env` grants `previews:env` for the login's project, not production environment access. The environment API returns variable names, never values. Values are encrypted at rest and captured for new deployments; updating a variable does not change a running release. Deployment permission lets application code consume that release's variables, so give Agent deployment credentials only to code you trust with those values.

`--allow-web` grants `previews:web`. Opt a dynamic preview into networking in `poteto.json`:

```json
{"runtime":"node","outbound_network":"web"}
```

Then use `poteto deploy . --local-build --preview`. Web egress permits public IPv4 TCP ports 80/443 and the operator's DNS resolvers. Host access, private/reserved ranges, cloud metadata, arbitrary ports, IPv6 and cross-sandbox connections stay blocked. It is not a destination-domain allowlist: treat a web-enabled application as able to send its own data to public websites. Omitting the setting keeps the previous no-egress behavior. A web token does not grant environment editing or cleanup; the login options are independent. Missing platform support or permission fails preflight without creating a deployment or falling back to another runtime.

## Build and publish without compiling on the server

```sh
poteto plan . --preview --json
poteto deploy . --local-build --preview --url-only
```

`--local-build` builds on the client, checks the actual artifact against server permissions and quotas, then uploads only that artifact. Static projects use local npm; Node projects and Dockerfile projects use your Docker context to build a `linux/amd64` image. Dynamic projects need Docker Engine API 1.48+, Buildx and image support shown by `poteto doctor`; new personal projects have it, while legacy projects keep their `prebuilt_image_projects` opt-in. A failed local build or preflight creates no deployment and never falls back to server compilation. Ordinary `deploy` retains its existing source-build behavior.

Build trusted projects only. The image context follows Poteto's upload exclusions, including `.env*`, credentials, `.git`, `.poteto` and `node_modules`. Build output goes to stderr; `--url-only` keeps stdout suitable for automation. Temporary image tags are removed after packaging; Docker's build cache remains for the next build. Use a local Docker context or an independent CI runner to keep compilation off the publishing server.

For separate CI build and publish jobs:

## Build static sites locally or in CI

```sh
poteto build . --preset vite
poteto plan . --prebuilt --json
poteto deploy . --prebuilt --preview --url-only
```

`build` runs trusted npm projects' locked install and configured build locally, then packages only the static output into `.poteto/output.tar.gz`. Plain HTML can be packaged without npm. Pass custom `--config` or `--preset` to `build`, not to `deploy --prebuilt`. Copy the archive to the same relative path in a separate CI publish job if needed. Prebuilt deployments default to preview; production still requires `--prod` and a production-capable credential. This path is for static output, including a completed Next.js static export; SSR and APIs continue to use the existing runtime paths.

## Publish a prebuilt dynamic image

For a personal project, or a managed Node/Dockerfile project enabled in `prebuilt_image_projects`, build locally or in CI and publish the resulting image without server-side compilation:

```sh
poteto build .
poteto plan . --prebuilt-image .poteto/image.tar.gz --remote --preview --json
poteto deploy . --prebuilt-image .poteto/image.tar.gz --preview --url-only
```

Node builds share the hosted builder's locked npm install and runtime recipe. Use a multi-stage Dockerfile for lean framework-specific images such as Next.js standalone output. With an existing locally built image, packaging still works without rebuilding:

```sh
docker buildx build --platform linux/amd64 --load -t my-app:preview .
poteto build . --from-image my-app:preview
poteto plan . --prebuilt-image .poteto/image.tar.gz --json
poteto deploy . --prebuilt-image .poteto/image.tar.gz --preview --url-only
```

Use Docker Engine with API 1.48 or newer for platform-specific image export. Poteto accepts a single image (128 MiB compressed, 512 MiB expanded); build a lean final image. Use the image's entrypoint and listen on `0.0.0.0:$PORT` as UID 10001. Runtime settings such as the health check and database binding come from `poteto.json` or `--config`, not from build flags. Personal previews run in gVisor; managed projects retain their existing isolated tenant runtime. Existing source deployments are unchanged.

The [Next.js + PostgreSQL fixture](fixtures/next-postgres) uses standalone output, runtime-only database credentials, and an idempotent migration. Its image is built and exercised by the dedicated SSR acceptance workflow.

The source/static upload limits remain 50/200 MiB. To budget retained images and rollback candidates for one account, an operator can use `paas-admin set-storage-limit ACCOUNT_NAME --confirm-account ACCOUNT_ID --mib 2048` on the controller. This does not change other accounts or their runtime limits; lowering it below current reservations is refused.

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

For a separate HTTPS origin behind a CDN, readiness can verify that origin without waiting for public DNS. Set `ingress_probe: "origin"`, `origin_probe_address` to its IP address and `origin_probe_ca` to an absolute PEM CA path in the backend configuration. The probe connects to port 443 with the deployment's hostname as SNI and Host, verifies its certificate and waits for the matching deployment header. Allow only that origin connection in the controller's egress policy. If the origin exposes a different application domain while retaining older internal routes, set `origin_probe_domain` to that external domain; this changes only the probe hostname, not project domains or published URLs. The default `public` mode continues to probe the public HTTPS endpoint.

In the IF.Link login configuration pointed to by `PAAS_OIDC_CONFIG_FILE`, set `"auto_provision": true` to create a personal preview project after the first verified sign-in. Existing `projects_by_subject` bindings retain their projects. Leave auto-provisioning off to require an explicit binding.

## Default personal previews without tenant VMs

On a provisioned executor, set `personal_previews: true` in both the publisher runtime configuration and the executor configuration. **New** IF.Link enrollments then receive an `isolated` personal project. Existing projects and releases retain their original mode. `poteto doctor` exposes the selected project's capabilities, and remote preflight returns the effective configuration.

Personal projects accept locally built static output and prebuilt `linux/amd64` preview images. With the native database provider below, images can also use preview PostgreSQL and migrations. They do not run source builds, allocate persistent volumes, import production bundles, or silently fall back to a VM. Images default to 256 MiB, 0.1 vCPU and idle sleep; explicitly supplied resources and sleep settings are preserved. Outbound access is off unless both the token and deployment opt into web egress. Personal production images use the explicit opt-in below; existing managed projects retain their production path.

The shared pool has 64 private ingress slots. Its default remains 2 GiB / 1.5 vCPU. To change it, set the same `sandbox_memory_limit_mib` and `sandbox_cpu_limit` in publisher and executor configurations, and apply matching `MemoryMax`, `MemorySwapMax=0` and `CPUQuota` values to `paas-sandbox.slice`. The executor checks effective cgroup limits and stored reservations before admitting a personal preview. Keep headroom for the controller, existing tenant VMs and the host; do not size this pool from currently free RAM alone. The controller's persistent loopback filter and executor egress policy must allow `23000–23063` and the wake proxy at `23100`.

To enable web egress on an existing installation, deploy `execution/sandbox_network.py` and the updated `paas-sandbox-network.service`, then set `sandbox_web: true` in both publisher and executor configurations. The network service reconciles its own nftables table from persisted sandbox receipts on restart. Set `sandbox_dns` in the executor to one to three reachable public IPv4 resolvers when the defaults are unsuitable. Web previews mount a platform-owned, read-only resolver file rather than using Docker’s loopback DNS; recreate web previews after changing that resolver list. Use `systemctl reload paas-sandbox-network.service` to reconcile changed rules without restarting the wake proxy. Disabling the switch and reloading that service withdraws web access; it does not grant a less isolated fallback or change application ingress. Existing offline receipts stay offline. Keep the older static `paas-sandbox.nft` only as a deny-all fallback, not as the web-enabled service's entry point.

A stopped preview retains its reservation so simultaneous wake-ups cannot overbook the pool. The per-project cleanup and usage commands help reclaim old reservations; a high project count does not imply that every project can run a large dynamic application concurrently.

### Publish a personal project to a stable production URL

When the platform enables personal production, request a separate production credential explicitly:

```sh
poteto login --production --output /secure/poteto-production.json
export POTETO_TOKEN_FILE=/secure/poteto-production.json
poteto deploy . --local-build --prod --url-only
```

The returned production URL stays the same across releases. Static output uses shared hosting; prebuilt images stay in gVisor without a tenant VM. A release must pass health checks before traffic switches. `poteto rollback RELEASE_ID` restores a retained healthy release without rebuilding it. It changes traffic, not database contents; migrations must remain compatible with the previous application version.

A production login grants this project's deployment read/write operations, including promotion, rollback, non-current release cleanup and explicitly configured web egress. It does not grant environment editing, data management, account administration or token issuance. Add `--manage-project` only when you need to manage the project's environments, databases, encrypted backups or deletion. That credential can manage both stages. Preview-only login flags cannot be combined with production login; keep the preview Agent credential in its own file.

For persistent production data, use the management credential to create `poteto database create --stage production`, then bind that database in the production deployment configuration. Preview and production databases have different IDs and credentials. A data-bound preview cannot be promoted into production; publish with `--prod` and the production binding instead. Likewise, use a new production deployment when environment settings differ. Production images may sleep just like previews; set `sleep: false` explicitly if the application needs to remain running, within the same resource budget.

Full production backups use `poteto export create PROJECT_ID --recipient AGE_PUBLIC_KEY` without `--deployment`. Restore into an empty enabled personal project with `poteto import create PROJECT_ID BUNDLE --identity KEY` without `--preview`. The production bundle includes both stages' project environment and database resources; protect the recipient key accordingly. Native restore preserves the schema and skips its already-applied migration.

Deploy matching publisher and executor code, including `execution/sandbox_wake.py`, before enabling this path with `personal_production: true` in the publisher runtime configuration, alongside `personal_previews` and, for databases, `native_databases`. Existing managed projects, credentials and VM routes retain their original contract. Turning the flag off stops new personal production logins, image deployments, database allocation and imports; it does not revoke issued credentials or withdraw an already running route.

### Preview PostgreSQL without a tenant VM

With server support enabled, request data permission explicitly at login, then create a preview database:

```sh
poteto login --manage-data
poteto database create --stage preview
poteto database list
```

Wait for the database to become `ready`. Set its ID as `database_id` in `poteto.json`; the platform injects `DATABASE_URL` only into the runtime and migration. A `migration_command`, such as `node migrate.mjs`, runs before application startup in a separate, bounded gVisor container with access only to that database. It runs once per release, with a 300-second deadline. A failed migration prevents publication; already committed database changes are not automatically undone. Use repeatable, backward-compatible migrations.

Each database reserves 256 MiB, 0.25 vCPU and a 2 GiB disk. Applications can sleep while their database remains available. Deleting a preview does not delete its data; remove every bound release before explicitly running `poteto database delete DATABASE_ID --destroy-data`. Preview data permission is project-scoped and does not grant production access. A deployment-only token can deploy against an existing authorized binding but cannot create or delete databases.

For operators: deploy the native database modules and `paas-native-databases.service`, and set `native_databases: true` in both publisher and executor configuration. Set executor `native_database_image` to a cached, trusted Linux/amd64 PostgreSQL 16 image ID. Enable the recovery service so owned databases restart before the wake proxy after a host reboot. Database reservations share the sandbox pool; do not increase capacity merely to bypass admission. Existing VM-backed databases keep their driver and data.

### Move a preview and its database with an encrypted bundle

```sh
poteto export create PROJECT_ID --deployment PREVIEW_ID --recipient AGE_PUBLIC_KEY
poteto export download EXPORT_ID --output /secure/preview.zip.age
poteto import create EMPTY_PROJECT_ID /secure/preview.zip.age --identity /secure/age-key.txt --preview
```

The exporting credential needs preview environment permission and, for a bound database, preview data permission. Import into an empty personal preview project with those permissions. The encrypted bundle includes the image, preview environment and logical database dump, not production variables. Import creates a new database binding, restores its data and skips the already-applied migration. Keep the identity and bundle outside upload directories. Web egress still requires the importing credential's explicit permission.

If an import fails after allocating resources, inspect its status before `poteto import delete IMPORT_ID --destroy-data`. This removes only that import's resources and leaves the project reusable. Retry the same cleanup after interruption; do not manually delete unrelated data.

### Restore runtimes after a platform migration

Platform snapshots include sandbox receipts, wake routing, pool units/overrides and normalized build archives, while the controller disk contains application configuration and credentials. On the restored host, provision Docker, gVisor and the Caddy/Sablier images first. After restoring the executor state and the controller, stop its workers, enter the maintenance barrier, and queue reconstruction from controller root:

```sh
python -m publisher.sandbox_restore --operation RESTORE_OPERATION_ID --all
python -m publisher.sandbox_restore --operation RESTORE_OPERATION_ID --all --apply
```

Use the same 24-character operation ID as the maintenance barrier, or select owned test releases with repeated `--deployment ID` instead of `--all`. Resume the workers and leave that barrier to process the existing deployment IDs through their normal readiness checks. Repeating the operation does not create duplicate reconstruction jobs. Review the returned job IDs and wait for readiness before restoring public traffic. These commands belong on the restored controller, never on an outbound-blocked audit clone connected to the live executor.

Snapshots containing native databases use `ovh-paas-platform/3` with logical PostgreSQL dumps. On the restored host, with executor admission stopped and its trusted PostgreSQL image available, run `python -m execution.platform_restore restore-native RESTORE_OPERATION_ID` before reconstructing applications. It preserves the restored controller's resource IDs and credentials and refuses to overwrite an existing database. Reconstruction skips migrations whose schema is already in the snapshot. Older snapshots without native databases retain their existing restore path.

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

The experimental gVisor path accepts stateless previews from trusted native builds or normalized prebuilt images. Install the [gVisor containerd shim](https://docs.docker.com/engine/daemon/alternative-runtimes/) on the executor, install the sandbox network unit, nftables rules and systemd slice from `infra/execution/`, and load the execution egress policy. If the controller VM has an additional host loopback filter, allow its service UID to reach `127.0.0.1:23000–23063` there as well. Use the same trusted project ID in the publisher's `sandbox_projects` list and the executor's `sandbox_projects` project-to-account map. Prebuilt projects also need the publisher's `prebuilt_image_projects` opt-in; they do not need a native BuildKit builder.

For an eligible image preview, use `sleep: false`, at least 256 MiB memory, and no database, volume or migration binding, then run `poteto deploy . --local-build --preview`. The server normalizes the uploaded image and starts a gVisor container without either a build VM or tenant VM. Remote preflight reports `runtime_driver: "gvisor"`. Admission is bounded by the supplied sandbox slice's total 2 GiB memory and 1.5 CPU envelope, as well as normal account/platform quotas.

Sandbox containers have loopback-only ingress and a read-only root filesystem. Outbound access is off by default; the explicit web permission described above is the only public-egress path. Offline, always-on previews have no host mounts. Web previews mount only the platform resolver file; sleeping previews additionally mount the fixed health-check executable. Legacy managed-project data bindings and production continue to use the existing runtime; personal preview PostgreSQL uses the native provider described above. Sleeping managed previews require the separate opt-in below. A sandbox preview that fails setup does not run unsandboxed. Existing releases retain their selected runtime.

Sleeping sandbox previews are a separate opt-in: install `infra/execution/paas-sandbox-wake.service` and its `sandbox-wake.compose.yml`, provision the existing Caddy/Sablier images and a Linux/amd64 build of `infra/execution/healthcheck.go` at `/opt/paas-execution/healthcheck`, then add the project to the publisher's `sandbox_sleep_projects` list and the executor's `sandbox_sleep_projects` project-to-account map. Regenerate the executor egress policy and, if present, update the controller VM's additional host loopback filter to allow only the private wake-proxy port `127.0.0.1:23100`. Preserve this rule in the filter's persistent configuration. Existing sleeping releases keep their original runtime unless redeployed.

With that opt-in, `sleep: true` keeps stateless previews on gVisor. Sablier stops them after two idle minutes and waits for the platform's HTTP health check before forwarding a new request; reading logs does not wake them. A sleeping container mounts the fixed, read-only platform health-check executable and, when web egress is enabled, the resolver file; neither exposes application host data. Its stopped processes release working memory, while its admission reservation remains held so concurrent wake-ups cannot overbook the sandbox pool. For managed projects, databases, persistent volumes and production remain on the existing runtime. Failed health checks do not fall back to an unsandboxed process.

## Develop

With Python 3.12 or newer and [uv](https://docs.astral.sh/uv/):

```sh
uv sync --locked
uv run poteto --help
uv run python -m unittest discover -s tests/publisher
```

Licensed under [MIT](LICENSE).
