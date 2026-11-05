# BCER 科研数据云端试跑

本仓库只保存脚本、来源清单和操作说明。BCER 原始文件在 GitHub Codespaces 中分批下载，并上传到你的 Zenodo 未发布草稿；本机不接收原始数据。

当前样本：WA 36588 的完井修订报告、MWDGR MD 与 TVD 两份 LAS，共 3 个文件。网站显示的大小经过取整；程序以传输服务器返回的精确字节数校验。

## 状态

- 已核实源站目录和这 3 个文件名；这不代表已经下载或解析文件内容。
- 默认最多 3 个文件、合计 100 MB；本试点程序的单次上限为 5 GB。
- 使用显式 FTPS，验证服务器证书，并对数据通道加密。是否可以从 Codespaces 连接源站，需要实际探测确认。
- 逐块下载与上传；记录精确大小、SHA-256、MD5、来源路径与时间。
- 原始文件仅在 Zenodo 返回匹配的大小与 MD5 后，才移除云端临时副本；失败时保留云端副本或 .part。
- 已存在同名但内容不同的 Zenodo 原始文件时停止，不覆盖。
- 没有自动发布功能。先核对 BCER 资料的再发布许可、数据说明和署名，再在 Zenodo 审阅发布。
- 本试点用于验证链路，不是已验证的 1 TB 批量迁移工具；尚未实现跨中断续传、全量调度和 PDF/LAS 解析。

## 1. 云端运行环境

在本仓库选择 Code → Codespaces → 创建 2 核环境。先检查免费额度和账单预算；若出现付费要求，停止，不添加付费资源。

GitHub Free 的 Codespaces 额度有限，不能视为持续运行的免费服务器。GitHub Actions 在这里只做短时测试和 TLS 探测，不用来批量抓取原始数据。

## 2. 在 Codespaces Secrets 中配置

打开 https://github.com/settings/codespaces ，在 Secrets 中为本仓库添加：

| 名称 | 值 |
| --- | --- |
| BCER_USERNAME | 你的 BCER 用户名 |
| BCER_PASSWORD | 你的 BCER 密码 |
| ZENODO_TOKEN | 你的 Zenodo 上传令牌 |

Zenodo 令牌在 https://zenodo.org/account/settings/applications/tokens/new/ 创建，仅勾选 deposit:write。不要勾选 deposit:actions；此流程不需要发布权限。不要把令牌或密码放在仓库、CSV、命令参数、聊天或日志中。设置 Secrets 后，需重启已有 Codespace 使其载入。

## 3. 先检查，再创建草稿

在 Codespaces 的 Linux 终端运行（Python 3.9+，无第三方依赖）：

```bash
python3 -m unittest -v test_bcer_pilot.py
python3 bcer_pilot.py --probe
python3 bcer_pilot.py --create-draft
```

最后一条命令打印未发布草稿 ID；每次执行都会新建草稿，成功后不要重复创建。

## 4. 三文件试跑

把下面的数字替换为刚才打印的草稿 ID：

```bash
python3 bcer_pilot.py --draft-id 123456
```

程序上传原始文件和 pilot_manifest.json。完成后在 Zenodo 草稿查看文件数量、大小和清单；草稿未发布，不代表已经公开归档或取得正式 DOI。

相同内容再次运行时会重新下载核对，但跳过重复上传；单个失败文件目前会从头重新传输。大规模处理前再完善续传与调度。

## 官方说明

- [BCER eLibrary：公开、免费访问的井资料入口](https://www.bc-er.ca/energy-professionals/online-systems/elibrary/)
- [Zenodo 存储额度](https://help.zenodo.org/docs/deposit/manage-quota/)
- [Zenodo 上传 API](https://developers.zenodo.org/)
- [GitHub 免费额度](https://docs.github.com/en/billing/reference/product-usage-included)

## FTPS 证书兼容说明

2026-11-05 的云端诊断发现 BCER FTPS 服务未完整发送 R13 中间证书。本项目附带 [Let's Encrypt 官方 R13](https://letsencrypt.org/certs/2024/r13.pem)，仅用于此客户端补齐证书链；仍验证受信任根、域名和有效期，不修改系统证书设置。该中间证书有效期至 2027-03-12；源站换证后若验证失败，应重新核查完整证书链，不可关闭校验。
