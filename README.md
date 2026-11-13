# Antigravity → Codex（WSL）

让 Antigravity 中的 Gemini 负责创意、设计、拆任务和验收，通过 MCP 调用 WSL 中独立安装的 Codex CLI，完成实现、修复与测试。

这是一套个人协作工具，非 Google 或 OpenAI 官方项目。仓库只包含程序、安装脚本与协作规则；每台电脑都需要自行登录 Codex。

```text
你 → Antigravity / Gemini → codex-worker MCP → WSL / Codex CLI → 项目文件
                          ← 进度、报告、验证结果 ←
```

<a id="deploy-with-codex"></a>

## 让 Codex 帮你部署

在另一台电脑本地运行的 Codex 桌面端或 CLI 中，复制发送下面这段完整提示词即可：

```text
请把这个项目部署到这台电脑：
https://github.com/wanqianquexiaobai/antigravity-codex-bridge

先检查本机的 WSL 和 Antigravity 环境，再按照 README 实际完成安装、配置和连接验证。使用独立的 Codex 执行环境，保留已有的 Codex 登录和其他 MCP 配置。

需要我登录账号或完成系统授权时，提示我操作。完成后确认 Antigravity 能调用 codex-worker，并说明验证结果。
```

Codex 的账号登录仍需要你在浏览器里完成一次。也可以按照下面的步骤手动安装。

## 准备条件

- Windows 上已有 WSL 2，并安装 Debian 或 Ubuntu；使用普通 Linux 用户。
- 已安装并登录 Antigravity，能使用 Gemini。
- 具备可以登录 Codex CLI 的账号，并能访问依赖下载站点和模型服务。
- 安装命令在 WSL 终端执行，不是在 PowerShell 中执行。

在 WSL 内准备系统工具：

```bash
sudo apt update
sudo apt install -y curl ca-certificates git ripgrep tar util-linux
```

## 在新电脑上安装

```bash
mkdir -p ~/projects
cd ~/projects
git clone https://github.com/wanqianquexiaobai/antigravity-codex-bridge.git
cd antigravity-codex-bridge
bash install.sh
```

安装脚本会下载独立的 uv、Python、MCP SDK 和 Codex CLI，生成本机路径，并合并 Windows 与 WSL 两侧的 Antigravity 配置。默认安装在 `~/.local/share/antigravity-codex`（设置了 `XDG_DATA_HOME` 时使用该目录），不会替换日常使用的 Codex 配置，也不会复制旧电脑的登录信息。现有配置修改前会备份。

如果 Windows 用户目录无法自动识别，手动指定：

```bash
bash install.sh --windows-home /mnt/c/Users/YOUR_WINDOWS_NAME
```

如果 Antigravity 确实只在 WSL 内运行：

```bash
bash install.sh --wsl-only
```

完成安装后，运行终端末尾打印的登录命令。默认位置为：

```bash
~/.local/share/antigravity-codex/bin/codex-worker login --device-auth
```

按提示在浏览器中完成登录。不要把 `auth.json`、登录目录或一次性登录码提交到仓库。

## 验证连接

重启 Antigravity，在新对话中说：

> 请调用 codex-worker 的 codex_health，报告 authenticated 和 environment。

预期 `authenticated` 为 `true`，`environment` 显示当前 WSL 发行版，例如 `WSL Debian` 或 `WSL Ubuntu-24.04`。

也可以从仓库目录检查实际 MCP 连接；这个检查只检查程序和登录，不会提交模型任务：

```bash
~/.local/share/antigravity-codex/venv/bin/python scripts/doctor.py
```

## 日常使用

在 Antigravity 中打开项目，直接描述需求即可。安装器添加了始终生效的分工规则：软件实现、修复和代码审查优先交给 Codex；纯创意讨论、方案交流和普通问答由 Gemini 完成。也可以使用 `/codex-delegate` 显式调用。

分工依赖模型遵循规则，并非强制转发所有请求。如果 Gemini 忘记委派，提醒它“把实现交给 Codex，按既定流程跟进并验收”。不需要提前打开一个 Codex CLI 窗口。

Gemini 必须把目标、上下文、文件路径、限制和验收标准写入任务。它的聊天记录和图片不会自动传给 Codex。Codex 返回后，Gemini 应检查实际测试证据；需要调整时继续同一个 Codex 会话。

| MCP 工具 | 用途 |
| --- | --- |
| `codex_health` | 检查安装、登录与允许使用的项目范围 |
| `codex_submit` | 提交任务，立即获得 `job_id` |
| `codex_status` | 查询进度，每次最多等待 30 秒 |
| `codex_result` | 获取报告，长报告支持分页 |
| `codex_continue` | 续接指定任务的原会话，得到新的 `job_id` |
| `codex_cancel` | 请求取消，已写入的内容保留 |
| `codex_list_jobs` | 连接恢复后找回任务 |

## 工作范围与限制

- 默认一次执行一个任务，单任务默认 30 分钟，可指定 1–120 分钟。
- 分析使用 `read-only`；已授权的代码修改使用 `workspace-write`。保留 Codex 自身的执行隔离，不使用完全跳过权限控制的选项。
- `succeeded` 仅表示模型完成这一轮，不能据此认定所有测试通过。
- 默认项目范围是 Linux 用户目录，以及能找到的 Windows `Documents` 目录。应传入其中的具体项目目录，不能直接把主目录、允许范围的根目录或应用配置目录作为工作区。
- 允许根目录用于校验工作目录，不是完整的文件读取隔离；实际执行权限仍由 Codex 控制。只向代理提供任务所需的信息。
- 使用同一账号时，多个 Codex 客户端可能共享账号额度。避免手动 Codex 和委派任务同时修改同一份文件；并行工作时使用不同工作副本。
- MCP 连接断开后，任务可继续在后台执行并被重新查询。电脑休眠、关机、WSL 停止会影响任务；不会自动重新执行中断任务。
- Node、Java、项目依赖等仍需根据具体项目安装。本工具只准备桥接服务与 Codex 的运行环境。

## 其他磁盘与运行工具

例如项目位于 `D:\Projects\my-app`，可以在安装时额外允许父目录：

```bash
bash install.sh --workspace-root /mnt/d/Projects
```

参数可重复使用；添加的目录必须已存在。已有安装也可以直接运行配置脚本，避免重新下载运行环境：

```bash
~/.local/share/antigravity-codex/venv/bin/python scripts/configure.py \
  --workspace-root /mnt/d/Projects
```

随后在 Antigravity 中重启 `codex-worker`。Windows 盘符路径和当前发行版的 WSL UNC 路径会转换为 Linux 路径。

执行环境禁用了登录 shell 和环境快照，并使用明确的 Linux `PATH`，防止调用到 Windows 的 Python 等程序。如需添加 nvm、项目专用 Node 或其他工具目录，修改安装目录下 `app/settings.json` 的 `execution_path`，同时修改 `codex-home/config.toml` 的 `shell_environment_policy.set.PATH`。这两个位置要保持一致。

## 配置位置与更新

以下路径均相对于安装目录：

| 路径 | 内容 |
| --- | --- |
| `bin/codex-worker` | 使用隔离环境的 Codex 命令入口 |
| `bin/start-mcp.sh` | 供 Antigravity 自动启动的 MCP 入口 |
| `app/settings.json` | 工作目录范围、运行路径等本机配置 |
| `codex-home/config.toml` | 独立 Codex 配置，可指定模型 |
| `codex-home/AGENTS.md` | Codex 的执行职责 |
| `state/` | 本机任务记录与日志 |
| `backups/`、`installation.json` | 配置修改前的备份、目标路径和文件摘要 |

Antigravity 全局配置目录为 `~/.gemini/config`；Windows 侧在对应 Windows 用户目录下。安装器只合并 `mcp_config.json` 中的 `codex-worker` 项，并安装 `skills/codex-delegate/SKILL.md` 和 `rules/codex-delegation.md`。

更新代码后可重新安装：

```bash
git pull --ff-only
bash install.sh
```

先结束正在执行的任务，并关闭 Antigravity 中的这个 MCP 服务，再更新。脚本会拒绝覆盖存在活动任务的环境。重复安装保留已有工作区列表、执行路径、登录文件与自定义 Codex 配置；分工规则和服务程序会随仓库更新。安装过程中发生配置写入错误，会恢复已改动的配置文件；下载的运行环境不属于该回滚范围。

发现已有不同的 `codex-worker` 项时，默认停止。检查后确实要替换，可加 `--replace-existing`；原内容会进入备份。停用时，在 Antigravity 禁用 `codex-worker`，并移除或停用 `codex-delegation` 规则即可，无需退出日常 Codex。

## 版本与验证

初始固定版本：Codex CLI `0.169.0`、Python `3.12.14`、uv `0.12.5`、MCP Python SDK `2.3.0`。Python 依赖固定在 `requirements.lock.txt`。它们是本项目验证所用的版本，并非声称一直是最新版本。

仓库包含 13 项服务与安装配置测试；不需要真实账号即可运行：

```bash
~/.local/share/antigravity-codex/venv/bin/python -m unittest discover -s tests -v
```

开发时在 WSL Debian 验证了真实 Codex 读取文件、修改示例项目、继续同一会话和运行项目测试；Windows → WSL 的 MCP 启动入口与 Antigravity 的健康检查也已验证。Ubuntu、不同 Windows 用户名、其他机器上的系统策略仍需在目标环境验证，不能把本机测试当作所有平台保证。

通用安装器还在全新目录中完成了独立下载安装；新环境的 13 项测试、7 个 MCP 工具发现和 Windows → WSL 启动检查通过。该新环境保持未登录状态，确认没有从原环境复制账号凭据。

## 官方参考

- [Antigravity MCP 配置](https://antigravity.google/docs/mcp)
- [Antigravity Skills](https://antigravity.google/docs/skills)
- [Antigravity Rules](https://antigravity.google/docs/rules)
- [Codex 非交互执行](https://learn.chatgpt.com/docs/non-interactive-mode)
- [uv 安装器环境变量](https://docs.astral.sh/uv/reference/environment/#uv_unmanaged_install)

本项目使用官方 Codex CLI 的非交互执行接口和官方 MCP Python SDK，自行提供 MCP 转接服务。
