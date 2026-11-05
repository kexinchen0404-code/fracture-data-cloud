# 跨项目科研云数据源

目标：多个 Codex 项目共用同一份云端资料目录和结构化结果；大型原始文件在云端处理与归档。

## 三个部分

| 部分 | 名称 | 用途 |
| --- | --- | --- |
| Turso 云数据库 | research-catalog，research-data 数据组 | 数据集、文件索引、按项目标识保存的小型 JSON 结果 |
| Codex MCP 入口 | research_data | 在同一台电脑的多个 Codex 项目中读取和写入这个库 |
| 大文件归档 | Zenodo，待完成迁移试跑 | 保存公开科研原始文件和稳定成果，不把约 1 TB 原始文件塞进数据库 |

[打开云数据库](https://app.turso.tech/kexinchen0404-code/databases/research-catalog)

Turso 使用 Free 方案，账号的数据库总免费存储为 5 GB，受平台当前额度约束。数据库与原始文件归档分别计算容量。

## 当前实现

- catalog_schema.sql 建立 datasets、files、project_results 三张表。
- sources.csv 已核实 WA 36588 的 3 个来源文件：完井修订报告、MWDGR MD、MWDGR TVD。
- 文件目录初始状态为 catalogued，精确字节数、SHA-256 和归档链接为空；这不代表原始文件已迁移。
- research_data_mcp.py 提供 list_datasets、list_files、write_result、read_result、list_results 五个工具。
- 结果用 project_id + result_key 标识，避免不同项目的同名结果互相覆盖。这是命名区分；同一用户可以明确指定其他项目来读取共享结果。
- 单条结构化结果上限 64 KB；大文件应保存在归档服务，仅把链接与校验值写入数据库。

## Codex 中使用

这台电脑已安装全局 research_data MCP 入口。数据库专用读写凭据保存后，重启 Codex 或新开聊天，确认 MCP 服务器列表中 research_data 可用。

例如：

> 使用 research_data 列出云端数据集，再读取 bcer-wa36588-pilot 的文件目录。

> 使用 research_data，把本项目的处理结果保存到 project_id=water-model-a，result_key=run-001。

> 从 research_data 读取 project_id=water-model-a、result_key=run-001 的结果。

这是同一 Codex 主机的共享连接。另一台电脑或 Codex 云端环境需要单独配置连接与授权，不会自动继承本机凭据。

## 凭据处理

官方 Turso MCP 的 OAuth 登录目前被此 Codex 客户端的授权来源校验阻止，因此使用只连接 research-catalog 的轻量 STDIO 连接器。

本机只运行少量连接代码，不复制数据库文件或原始资料。连接器通过 HTTPS 查询云数据库；不提供任意 SQL 或删除工具。

数据库读写令牌只用于 research-catalog。由用户在私密输入提示中运行 Save-Token.ps1 保存为 Windows 当前用户加密的 token.xml；密钥不进入仓库、命令参数或聊天。换电脑或 Windows 用户后必须重新授权。

## BCER 大文件迁移状态

bcer_pilot.py 是受大小限制的 FTPS 到 Zenodo 草稿试点，默认 3 个文件、合计 100 MB，单次最多 5 GB；逐块传输并核对大小和校验值。仅在 Zenodo 验证匹配后清理云端临时副本。

2026-11-05 的云端探测确认：BCER FTPS 缺少 R13 中间证书，补齐 [Let's Encrypt 官方 R13](https://letsencrypt.org/certs/2024/r13.pem) 后又报告服务器证书已过期。证书校验保持开启。当前 FTPS 迁移尚未跑通，需要改用证书有效的 HTTPS 下载通道或由源站修复证书；不要关闭 TLS 校验。

Zenodo 登录已完成，但上传令牌尚未配置。原始文件还未完成云端上传；约 1 TB 全量迁移尚未开始。应先完成三文件端到端试跑、核对再发布许可，再扩展规模。

Codespaces 已创建并停止运行，避免空闲计算继续消耗额度。恢复后先同步本仓库最新代码。仅使用免费额度，不添加付费资源。Codespaces 不是 1 TB 持久存储。

## 检查

```bash
python3 -m unittest discover -v
```

GitHub Actions 在代码变更后运行小型离线测试。FTPS 联通探测仅在手动触发工作流时运行，其失败与源码单元检查分开显示。

## 官方参考

- [Codex MCP 与共享配置](https://learn.chatgpt.com/docs/extend/mcp)
- [Turso 官方 Codex 连接说明](https://github.com/tursodatabase/turso-mcp/blob/main/codex/README.md)
- [Turso 免费额度](https://turso.tech/pricing)
- [BCER eLibrary](https://www.bc-er.ca/energy-professionals/online-systems/elibrary/)
- [Zenodo 存储额度](https://help.zenodo.org/docs/deposit/manage-quota/)
