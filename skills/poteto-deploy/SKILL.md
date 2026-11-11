---
name: poteto-deploy
description: 用 Poteto CLI 把现有应用目录预检并发布到 Poteto 的 HTTPS 预览。适用于用户指定 Poteto 的静态站、Node 应用、Dockerfile 项目，以及已绑定预览数据库或卷的动态应用。
---

# 用 Poteto 发布预览

目标是交付一个已通过检查的 HTTPS 预览链接。使用用户选定的应用目录和 Poteto 项目；不要为了发布重排项目，也不要改动生产发布、域名或数据资源。

## 安装与凭据

如果没有可用的 Poteto 0.3.0 或更新版本，从官方地址安装。需要 Python 3.10+，不需要 sudo。安装到用户路径，或为任务设置独立的 `POTETO_INSTALL_DIR`，用该目录下的 `poteto` 执行后续命令。

```sh
curl -fsSL https://app-5ac4322ae76637bc9f813f2a.poteto.best/install.sh | sh
```

默认可执行文件是 `$HOME/.local/bin/poteto`。运行 `poteto --version` 确认可用。

- 用户可用 `poteto login` 在浏览器完成 IF.Link 登录。使用指定的凭据文件时，通过 `POTETO_TOKEN_FILE` 传入路径。不要索要或输出 token 内容，不要将凭据复制到上传目录。
- 用 `poteto doctor` 核对项目和权限。仅接受有目标项目预览权限的凭据；不要为自动化改用管理员凭据或自行扩大权限。使用项目级手动 token 时，部署命令加 `--project "$PROJECT_ID"`。IF.Link 登录凭据已绑定项目时可省略。

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
