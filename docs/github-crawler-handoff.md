# GitHub 爬虫交付入口

本页用于只能通过网页爬虫读取 GitHub 的内网 AI。无需 git clone、下载 ZIP 或 GitHub API 凭据。

## 读取顺序与范围

1. 先读 [完整接入说明](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/docs/intranet-integration.md)，特别是其中的文件合并范围、真实依赖检查和验收步骤。
2. 按下表逐个读取完整文本。优先使用 Raw 直链；若爬虫不支持 raw.githubusercontent.com，则使用 GitHub 文件页面，只提取源码正文，不包含导航、行号和页面控件。
3. 必须读取完整文件。遇到页面截断、只有摘要、跳转登录或抓取失败时，报告具体路径，不能凭摘要或记忆补写源码。
4. 7 个主要源码文件逐项合并到内网完整项目。其他源码仅用于依赖对照，不能因为本仓库含有它们就覆盖生产文件；不得删除本仓库缺少的内网模块。
5. 本地来源提交为 `2ec6da0c8166e2937dc013767f6f1ee6c99a5af3`，对应 29 文件快照的 Git tree 为 `267d17ff73d906abb2b27025fe997394c5c48dad`。远程源码提交为 `b2275acc54786b98671cd3384576d84219fc5e28`，下表全部链接固定到该版本。由于经 GitHub API 创建提交，远程与本地提交号不同，但源码 tree 完全一致。

## 文件清单

| 路径 | 用途 | GitHub 页面 | Raw 原文 |
| --- | --- | --- | --- |
| `.gitignore` | 忽略规则 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/.gitignore) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/.gitignore) |
| `README.md` | 说明 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/README.md) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/README.md) |
| `attachment_downloads.py` | 主要源码合并 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/attachment_downloads.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/attachment_downloads.py) |
| `calendar_operations.py` | 主要源码合并 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/calendar_operations.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/calendar_operations.py) |
| `config.py` | 主要源码合并 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/config.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/config.py) |
| `confirmation.py` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/confirmation.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/confirmation.py) |
| `docs/attachment-download-plan.md` | 说明 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/docs/attachment-download-plan.md) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/docs/attachment-download-plan.md) |
| `docs/attachment-downloads.md` | 说明 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/docs/attachment-downloads.md) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/docs/attachment-downloads.md) |
| `docs/intranet-integration.md` | 说明 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/docs/intranet-integration.md) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/docs/intranet-integration.md) |
| `docs/tool-contract-validation.md` | 说明 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/docs/tool-contract-validation.md) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/docs/tool-contract-validation.md) |
| `docs/tool-description-review.md` | 说明 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/docs/tool-description-review.md) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/docs/tool-description-review.md) |
| `flag_operations.py` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/flag_operations.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/flag_operations.py) |
| `mail_operations.py` | 主要源码合并 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/mail_operations.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/mail_operations.py) |
| `mcp_server.py` | 主要源码合并 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/mcp_server.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/mcp_server.py) |
| `oa_lookup.py` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/oa_lookup.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/oa_lookup.py) |
| `outlook_client.py` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/outlook_client.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/outlook_client.py) |
| `requirements.txt` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/requirements.txt) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/requirements.txt) |
| `status_operations.py` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/status_operations.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/status_operations.py) |
| `tests/test_attachment_downloads.py` | 离线回归测试 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_attachment_downloads.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_attachment_downloads.py) |
| `tests/test_attachment_http.py` | 离线回归测试 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_attachment_http.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_attachment_http.py) |
| `tests/test_attachment_mail.py` | 离线回归测试 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_attachment_mail.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_attachment_mail.py) |
| `tests/test_calendar_guards.py` | 离线回归测试 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_calendar_guards.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_calendar_guards.py) |
| `tests/test_config.py` | 离线回归测试 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_config.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_config.py) |
| `tests/test_mail_read_results.py` | 离线回归测试 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_mail_read_results.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_mail_read_results.py) |
| `tests/test_tool_contracts.py` | 离线回归测试 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_tool_contracts.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tests/test_tool_contracts.py) |
| `tool_params.py` | 主要源码合并 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tool_params.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tool_params.py) |
| `tool_specs.py` | 主要源码合并 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tool_specs.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tool_specs.py) |
| `tool_support.py` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/tool_support.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/tool_support.py) |
| `write_operations.py` | 既有接口／依赖对照 | [页面](https://github.com/timeagent4-lang/ews_mcp/blob/b2275acc54786b98671cd3384576d84219fc5e28/write_operations.py) | [原文](https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/b2275acc54786b98671cd3384576d84219fc5e28/write_operations.py) |

## 给内网 AI 的执行约束

按完整接入说明完成逐文件比较和集成，不整目录覆盖，不改变会议通知副本行为。配置字段使用 lanid、服务账号环境变量使用 OUTLOOK_ADMIN_LANID；附件下载访问宿主机 7712 映射到容器 7805。照片、凭据、数据库和真实附件不在交付中。

爬虫只负责读取源码。如果你没有文件写入、终端或部署能力，请输出逐文件合并内容和运维操作清单，明确标为待执行，交给有执行能力的内网人员或工具；不得声称已经改好文件、执行测试或部署。

有执行能力时，运行交付的 69 项离线测试及内网已有测试，并单独验证真实模块导入、OA、审计和附件 GET；只通过带替代对象的离线测试不足以证明生产可用。不得真实发送邮件或写入日历，生产切换按既有授权流程执行。

回传版本、获取完整性、实际修改、实际测试、未执行和阻塞项。不得回传真实附件正文、凭据、确认令牌或完整有效下载链接。
