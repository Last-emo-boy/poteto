---
name: poteto-deploy
description: 用 Poteto CLI 把现有应用目录预检并发布到 Poteto 的 HTTPS 预览。适用于用户指定 Poteto 的静态站、Node 应用、Dockerfile 项目，以及已绑定预览数据库或卷的动态应用。
---

# 用 Poteto 发布预览

目标是交付一个已通过检查的 HTTPS 预览链接。使用用户选定的应用目录和 Poteto 项目；不要为了发布重排项目，也不要改动生产发布、域名或数据资源。

## 安装与凭据

如果没有可用的 Poteto 0.6.0 或更新版本，从官方地址安装。需要 Python 3.10+，不需要管理员权限。安装到用户路径，或为任务设置独立的 `POTETO_INSTALL_DIR`，用该目录下的 `poteto` 执行后续命令。

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
poteto deploy "$APP_DIR" --project "$PROJECT_ID" --preview --url-only
```

先确认 `plan` 返回 `ok: true`，检查 `runtime`、`public_directory` 和构建/启动命令；如果需要 `--config`，在两个命令上用同一个文件。确认是应用的目录或成品目录，而不是包含其他项目或私有文件的上层目录。预检不执行构建，也不需要登录。

- 静态成品直接传包含 `index.html` 的目录，例如 `dist/`。如果 Vite 或其他前端构建不能明确识别，先构建再传成品，或在 `poteto.json` 明确配置 `build_command` 与 `output_directory`。
- Node 源码构建需要 npm 锁文件。动态应用须监听 `0.0.0.0` 上平台提供的 `PORT`；非 npm 构建可以使用 Dockerfile。预览数据库或卷由项目所有者预先创建和授权，沿用已有绑定。
- 预检失败时按 JSON 的 `error.code` 和 `error.message` 处理；不要绕过预检或改用生产发布。上传后失败时用 `poteto deployments --project "$PROJECT_ID"` 和 `poteto logs <发布 ID>` 定位，不盲目重复创建发布。

只在 `deploy` 成功且返回的 HTTPS 地址可访问后，才交付预览链接。验证首页或应用实际配置的健康检查路径；验证共享数据时优先使用只读请求。不要把 token 或环境变量值放进回复。

后续用 `poteto inspect <预览 URL>` 查看同一个部署，用 `poteto logs <预览 URL>` 查看日志，加 `--follow` 可在构建过程中持续查看输出，用 `poteto ls` 列出当前项目发布。此技能始终使用 `--preview`；不得为了绕过预览权限改用 `--prod`。

Next.js / Vite 项目可在预检和部署时同时加 `--preset nextjs` / `--preset vite`，或在 `poteto.json` 指定 `preset`。保留已有目录和锁文件。环境变量操作需 `env:write`，不要为预览 Agent 扩权；运行时变量变更后可用 `poteto redeploy <预览 URL> --preview --url-only` 重建。请求中断时沿用 stderr 中的 `--request-id`，不要重新生成请求。
