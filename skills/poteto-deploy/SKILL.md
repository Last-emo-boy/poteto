---
name: poteto-deploy
description: 用 Poteto CLI 把现有应用目录预检并发布到 Poteto 的 HTTPS 预览。适用于用户指定 Poteto 的静态站、Node 应用、Dockerfile 项目，以及已绑定预览数据库或卷的动态应用。
---

# 用 Poteto 发布预览

目标是交付一个已通过检查的 HTTPS 预览链接。使用用户选定的应用目录和 Poteto 项目；不要为了发布重排项目，也不要改动生产发布、域名或数据资源。

## 安装与凭据

如果没有可用的 Poteto 0.16.1 或更新版本，从官方地址安装。需要 Python 3.10+，不需要管理员权限。安装到用户路径，或为任务设置独立的 `POTETO_INSTALL_DIR`，用该目录下的 `poteto` 执行后续命令。

```sh
curl -fsSL https://poteto.iflink.tech/install.sh | sh
```

Windows 使用 PowerShell 7+：

```powershell
irm https://poteto.iflink.tech/install.ps1 | iex
```

默认可执行文件：macOS/Linux 为 `$HOME/.local/bin/poteto`；Windows 为 `$env:LOCALAPPDATA\Programs\Poteto\bin\poteto.cmd`，安装器会添加用户 PATH。运行 `poteto --version` 确认可用。

- 用户可用 `poteto login` 在浏览器完成 IF.Link 登录。使用指定的凭据文件时，通过 `POTETO_TOKEN_FILE` 传入路径。不要索要或输出 token 内容，不要将凭据复制到上传目录。
- 用 `poteto doctor` 核对项目和权限。仅接受有目标项目预览权限的凭据；不要为自动化改用管理员凭据或自行扩大权限。使用项目级手动 token 时，部署命令加 `--project "$PROJECT_ID"`。IF.Link 登录凭据已绑定项目时可省略。
- 重复使用同一目录时，可运行 `poteto link "$APP_DIR" --project "$PROJECT_ID"` 保存项目与 API 地址；`.poteto/` 不保存 token，也不会上传。`poteto unlink "$APP_DIR"` 只解除本地绑定，不删除服务器项目。

## 预检 → 预览 → 验证

```sh
poteto plan "$APP_DIR" --preview --json
poteto deploy "$APP_DIR" --local-build --project "$PROJECT_ID" --preview --url-only
```

先确认 `plan` 返回 `ok: true`，检查 `runtime`、`public_directory` 和构建/启动命令；如果需要 `--config`，在两个命令上用同一个文件。确认是应用目录或成品目录，而不是包含其他项目或私有文件的上层目录。本地预检不执行构建，也不需要登录。

**默认把构建放在用户电脑或独立 CI，不放在 Poteto 发布服务器上。** `--local-build` 在客户端构建后自动检查成品的服务端权限、运行额度和数据绑定，检查通过才上传；失败不会自动改走服务器编译。静态项目使用本地 npm，Node/Dockerfile 使用当前 Docker context 构建 `linux/amd64` 镜像。确认该 context 不是发布服务器；只构建可信项目。动态镜像需要 Docker Engine API 1.48+、Buildx 和项目已有镜像权限，没有这些条件时先说明缺项，不擅自改用服务器构建。

**静态站优先在本机或 CI 构建。** 对可信的 npm 项目，`poteto build` 会按锁文件安装依赖并执行项目的构建脚本。Vite 需要时在构建命令上加 `--preset vite`；其他静态 npm 项目可在 `poteto.json` 指定 `build_command` 和 `output_directory`。构建和预检不需要登录，不要把私密配置编进浏览器资产。

```sh
poteto build "$APP_DIR"
poteto plan "$APP_DIR" --prebuilt --remote --project "$PROJECT_ID" --preview --json
poteto deploy "$APP_DIR" --prebuilt --project "$PROJECT_ID" --preview --url-only
```

已有静态成品时，也可以直接部署含 `index.html` 的成品目录，例如 `dist/`。

**动态应用分步构建时：** `poteto build "$APP_DIR"` 可构建 Node 或 Dockerfile 项目；已有可信镜像时用 `poteto build "$APP_DIR" --from-image "$IMAGE"`，不会重建。对同一个应用目录，在 `plan` 和 `deploy` 上都传 `--prebuilt-image "$APP_DIR/.poteto/image.tar.gz"`，预检加 `--remote --project "$PROJECT_ID"`，并继续用 `--preview`。镜像上限为压缩 128 MiB、展开 512 MiB；Next.js 等框架优先用多阶段 Dockerfile 和 standalone 产物。不要把凭据、运行时密钥或数据库内容打进镜像。先以 `poteto doctor` / 远端预检确认项目能力；新版新注册个人项目可默认接收镜像，旧托管项目仍按原开通策略。未开通时说明缺项，不反复提交。

远端预检的 `runtime_driver: "gvisor"` 表示平台将直接启动隔离容器，不使用构建 VM 或租户 VM。新个人项目默认可用，镜像至少 256 MiB；支持原生数据库的平台还可绑定预览 PostgreSQL 并执行迁移，持久卷仍需托管项目。旧项目仍需平台开通。新个人项目默认休眠，旧项目需开通 `sandbox_sleep_projects`；`sleep: true` 会由 Sablier 在空闲后停止、访问时等待健康检查再唤醒。沿用应用的实际需求，不为选用此通道删除数据绑定。配额满或隔离启动失败时处理错误，不退回无隔离运行。

- Node 源码构建需要 npm 锁文件。动态应用须监听 `0.0.0.0` 上平台提供的 `PORT`；非 npm 构建可以使用 Dockerfile。预览数据库或卷由项目所有者预先创建和授权，沿用已有绑定。
- CLI 0.12.0 配合新版服务端会自动分片上传，并在当前应用的 `.poteto/` 保存不含凭据的待上传成品。若上传中断，在同一目录执行 `poteto deploy . --project "$PROJECT_ID" --resume <发布 ID>`，沿用输出中的 ID；不要重新构建、添加 `--local-build`、更换目标或创建另一个发布。收到服务端已入队确认后，本地待上传副本会自动删除。
- 预检失败时按 JSON 的 `error.code` 和 `error.message` 处理；不要绕过预检或改用生产发布。上传后失败时用 `poteto deployments --project "$PROJECT_ID"` 和 `poteto logs <发布 ID>` 定位，不盲目重复创建发布。

只在 `deploy` 成功且返回的 HTTPS 地址可访问后，才交付预览链接。验证首页或应用实际配置的健康检查路径；验证共享数据时优先使用只读请求。不要把 token 或环境变量值放进回复。

后续用 `poteto inspect <预览 URL>` 查看同一个部署，用 `poteto logs <预览 URL>` 查看日志，加 `--follow` 可在构建过程中持续查看输出，用 `poteto ls` 列出当前项目发布。此技能始终使用 `--preview`；不得为了绕过预览权限改用 `--prod`。

Next.js / Vite 项目可在预检和部署时同时加 `--preset nextjs` / `--preset vite`，或在 `poteto.json` 指定 `preset`。保留已有目录和锁文件。只有用户明确选择服务器源码构建时，才省略 `--local-build`。预览变量操作需用户已授予的 `previews:env` 或管理凭据 `env:write`，不要擅自扩权；`poteto redeploy <预览 URL> --preview --url-only` 使用已保存的构建方式，预构建镜像复用成品，源码发布会在服务器重建。请求中断时沿用 stderr 中的 `--request-id`，不要重新生成请求。

## 配额不足时先查看占用

`poteto usage --project "$PROJECT_ID" --json` 返回实际用于准入的空间、运行资源和并发额度预留，以及当前凭据可见的项目版本；不是实时 CPU/RSS，休眠仍保留唤醒额度。空间或资源不足时先据此定位，不重复创建部署。

`poteto prune --project "$PROJECT_ID" --json` 只预览清理候选，默认保留最新三个就绪预览和最近七天的版本；生产、曾用于发布/回滚的版本、绑定数据库或卷的预览不会入选。删除须取得用户对具体 ID 的授权，再用同一筛选条件执行 `poteto prune --project "$PROJECT_ID" --yes --ids ID1 ID2 --json`。中断后重复原 ID 列表，不扩大选择。部署专用 token 没有清理权限；不要擅自扩权或替用户启用 `login --manage-previews`，应由用户使用管理凭据执行。

新个人项目若显示 `preview_mode: "isolated"`，只接收本地构建的静态成品或预览镜像，不会退回服务器源码编译或租户 VM。镜像默认 256 MiB、0.1 vCPU、自动休眠；以远端预检的 `configuration` 为准，保留用户显式配置。数据库能力以 `capabilities.preview_database` 和远端预检为准；不支持持久卷，不要为进入通道删除真实需求。出站网络默认关闭；外部 API 需求按下节检查，PostgreSQL 需求按下节处理，持久卷沿用已支持的托管项目。


## 外部 API 和预览环境变量

`poteto doctor` 的 `capabilities.preview_environment` 与 `web_egress` 区分已有权限和平台支持。普通部署 token 不自动拥有这些权限。用户需要时可自行执行 `poteto login --manage-env --allow-web`；不代替用户扩大授权。

使用已有的预览环境权限，以目录外文件、标准输入或隐藏交互设置值，例如 `poteto env set API_KEY --file /secure/api-key.txt --target preview`。只检查返回的变量名，不读取、输出或编进镜像；变更仅用于新部署。获得部署权限的应用代码能使用对应运行时变量，因此不能把不可信代码与敏感运行时值混用。

只有需求包含调用外部服务、平台已启用且 token 有 `previews:web` 时，才在动态应用配置中设置 `"outbound_network": "web"`，然后重新预检。它允许公共 IPv4 HTTP/HTTPS（80/443）及指定 DNS，不允许内网、云元数据、任意端口、IPv6或其他沙箱。它不是域名白名单。缺少权限或网络能力时说明缺项，不改用管理 token、不移除真实需求后宣称完成。


## 原生预览数据库与迁移

Poteto 0.16.0 的 `capabilities.preview_database` 表示服务器支持个人项目的原生 PostgreSQL。创建、删除或备份数据需要用户明确授予 `previews:data`，可由用户执行 `poteto login --manage-data`；不要自行扩大 Agent 权限。已有数据库绑定可供部署 token 使用。创建时必须指定 `poteto database create --stage preview`，默认阶段仍是生产；用 `database list` 等待 `ready` 后，把返回 ID 写入 `poteto.json` 的 `database_id`。不要读取或复制平台生成的 `DATABASE_URL`。

迁移用 `migration_command`，例如 `node migrate.mjs`；先确认脚本和镜像中有对应依赖。平台在独立 gVisor 容器中执行，只有该数据库的网络权限，最长 300 秒。失败时查看日志并修复，不盲目重试或删除数据库；已提交的 SQL 不会自动回滚。新发布会执行新迁移，脚本应可重复且向后兼容。应用仍可休眠，数据库保留运行和数据。

删除单条预览不会删除数据库。数据库删除需要先移除所有绑定发布，再明确执行 `database delete ID --destroy-data`；必须取得用户对具体数据的删除授权。

## 加密备份与迁移

只有用户要求备份或迁移时，才执行这条路径。预览导出需要该项目的预览环境权限，有数据库时还需数据权限：

```sh
poteto export create "$PROJECT_ID" --deployment "$PREVIEW_ID" --recipient "$AGE_PUBLIC_KEY"
poteto export download "$EXPORT_ID" --output /secure/preview.zip.age
poteto import create "$EMPTY_PROJECT_ID" /secure/preview.zip.age --identity /secure/age-key.txt --preview
```

分片传输遇到短暂断线会先核对服务端已提交偏移再有限重试；仍失败时按错误中的 ID 执行 `import resume`，沿用同一加密包和身份文件，不重新创建导入。

接收方必须是已授权的空个人预览项目。身份私钥和加密包放在上传目录之外；不要输出私钥、环境值或数据库内容。导入会创建新数据库绑定，保留原数据并跳过已应用迁移。访问导入后的页面验证实际数据，同时确认源应用未改变；不能只凭任务完成就宣称恢复成功。

导入失败先检查状态。用户同意放弃该导入后，可用 `poteto import delete "$IMPORT_ID" --destroy-data` 清理仅由它创建的资源，保持目标项目可复用。中断后重试同一操作，不扩大删除范围。
