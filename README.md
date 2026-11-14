# Ampuly Engine / 安普里引擎

> **源码可见、附条件商用。** 安普里原创部分适用 [LICENSE](LICENSE)，
> 上游及第三方权利继续保留，见 [NOTICE.md](NOTICE.md)。本项目不宣称采用 OSI
> 批准的开源许可；此源码快照面向开发者评估，不是经过完整验收的安装器。

Ampuly Engine 是面向项目任务与业务协作的 Python 引擎，包含 Agent 执行、工具调用、
审批、项目数据和浏览器控制台源码。早期基础来自 HKUDS/OpenOPC，来源与第三方许可
见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本快照提供通用引擎源码。私有 VI 业务流、客户项目、运行数据库、API 密钥、
开发过程文档、交互记录和内部 Git 历史不属于本次发布范围。旧安装器、浏览器二进制、
Python 环境与已构建的前端也没有随源码提供。

## 开发环境准备

以下命令供开发者在**新建检出目录**中手动执行，不是发布准备工作已经执行的记录。
使用 Python 3.12 和支持 Vite 7 的 Node.js / npm；项目声明的最低 Python 版本为 3.10。
依赖安装会联网下载软件。请先审阅 `pyproject.toml`、前端 `package.json` 与
`package-lock.json`，再决定是否安装。

Windows PowerShell 示例：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[superones-runtime]"
Push-Location ampuly/plugins/office_ui/frontend_src
npm ci
npm run build
Pop-Location
```

`superones-runtime` 是既有可选依赖组的兼容名称，包含 aiohttp、协议依赖以及 Codex
SDK/CLI 等软件。安装此依赖组不代表登录账号、授权外部 Agent 或启用真实模型。
只审阅后端源码时可不安装该依赖组。当前源码未提供完整冻结的 Python 依赖锁，
这些命令也不构成可复现离线安装器的承诺。

## 使用新的隔离数据目录初始化

以下环境变量仅影响当前 PowerShell 进程及其子进程，不修改系统环境设置。请在新
检出目录执行，确保 `.ampuly` 尚未承载任何已有业务数据。

```powershell
$env:AMPULY_HOME = Join-Path (Get-Location) '.ampuly'
$env:AMPULY_WORKPLACE_ROOT = Join-Path $env:AMPULY_HOME 'workplace'
$env:APPDATA = Join-Path $env:AMPULY_HOME 'user-config'
$env:LOCALAPPDATA = Join-Path $env:AMPULY_HOME 'user-local'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $env:AMPULY_HOME 'browsers'
.\.venv\Scripts\ampuly.exe init --no-external-agent-preflight --no-trust-external-agents
```

初始化会建立本地配置、技能和记忆文件，并在隔离的 APPDATA 下记录本项目的配置信任。
上述选项关闭外部 Agent 的初始化预检和自动信任。不要使用其他项目或正式部署的 HOME，
也不要复制真实账号、浏览器会话或生产配置作为示例。

模型凭据应由使用者在自己的本地配置或明确支持的环境变量中提供。模板中的凭据保持
空值；不要把填写后的配置、`.env`、浏览器状态或数据库提交到 Git。

## 启动本地控制台

确认选择的端口空闲后，在同一个 PowerShell 会话中执行：

```powershell
.\.venv\Scripts\ampuly.exe ui --host 127.0.0.1 --port 8875
```

在浏览器访问 `http://127.0.0.1:8875/`；前台进程使用 Ctrl+C 停止。首次使用按页面
提示创建自己的本地账号。不要在完成账号设置与网络访问评估前绑定公网接口。

当前 UI 启动代码会在 aiohttp 缺失时尝试安装依赖，在前端缺失或过期时尝试 npm
安装和构建。上面的显式准备步骤用于提前完成这些动作；源码验证不能将 `ampuly ui`
当成无副作用检查。浏览器自动化另需兼容的 Playwright 浏览器，须由使用者明确安装；
该步骤不在此源码快照中自动执行。

## 源码与样本

- `ampuly/`：通用引擎、工具与控制台源代码。
- `config/`：去除凭据的初始化模板。
- `skills/core/`：通用技能文本。
- `distribution/prompts/talent/general-default-employee.md`：新编写的单一通用角色模板；未打包第三方角色库。
- `examples/isolation/`：新编写的无业务合成样本，仅用于只读隔离规划。
- `licenses/`：额外第三方许可全文。

业务流程由使用者自行创建或按单独授权导入。此稿不包含预置的私有 VI 业务流程。
安全边界和报告方式见 [SECURITY.md](SECURITY.md)。本次仅完成发布路径边界测试、
源码静态检查和隔离的 CLI 加载检查，未进行完整系统回归或生产验收。离线前端构建
完成了源码转换，但在压缩输出阶段遇到本机 esbuild 临时文件访问错误，完整构建
仍未验证通过；wheel 与安装器也未验证。开发者应在自己的隔离环境完成构建与评估。

## 商用许可与开发赞助

适用作品的商用需要书面确认：共享方案免软件许可费，提交有权提供的脱敏业务流
设计，仅供安普里内部研究和产品改进；保密方案无需提交设计，按引擎直接交付产生
的实收收入扣除退款及代收税费后计收 3%。两种方案均按明确约定申报必要收入汇总，
不向公众公开；完整适用边界以 [LICENSE](LICENSE) 为准。

本地控制台账号与商业授权账号是不同用途。商业账号、设计上传、收入申报和支付
服务尚未提供，此源码没有为上述计划新增后台采集或自动扣款。安装软件或赞助均
不自动取得商用授权，也不改变第三方许可证已经授予的权利。

[支持 Ampuly 开发](SPONSORSHIP.zh.md) 是独立、自愿的赞助安排；目前未开通收款渠道。

## 可选联网功能

配置并调用模型、消息渠道、浏览器代理等功能会向所选服务发送相应输入。可选
Puly 功能需要使用者提供密钥，并向其 TypeSafe 服务发送任务及交互状态；这不是
上述商业申报服务。请先审阅功能实现与服务方政策，再提供凭据和业务内容。
真实 Agent 集成测试有单独环境开关及密钥要求，不属于默认离线检查。
