# EWS MCP

面向内网的 Exchange 邮件 MCP 服务，通过 OA 校验员工身份，以固定服务账号通过 DELEGATE 访问对应邮箱。

- 公开 28 个工具（见 [tool_specs.py](tool_specs.py)）；`arguments` 为扁平对象，必填 `lanid`、`name`。
- 写操作先预览，再携原业务参数、`operation_id` 和 `confirm_token` 确认，可选 `idempotency_key`。结果未知时用原 `operation_id` 查回执，不自动重试。
- `EWS_MCP_SEND_ENABLED` 默认关闭，限制真实发送、会议通知及 OOF 执行；预览仍可用。
- `prepare_attachment_download` 返回内网 HTTP 签名链接，供 AI 执行器 GET 下载；响应不含附件字节。
- `check_availability` 返回各邮箱的忙闲区间，由调用方 Agent 选择公共时段；`interval_minutes` 为采样间隔（默认 30，范围 5–1440），不是会议时长。参数与迁移说明见 [忙闲查询](docs/availability.md)。
- `list_tasks` 默认先筛选未完成任务，再按到期日降序分页；用返回的 `next_offset` 取后续页。参数、空结果和分页边界见 [任务列表](docs/task-listing.md)。

接入、验收及回滚见 [内网接入说明](docs/intranet-integration.md)，变量见 [.env.example](.env.example)。服务账号用 `OUTLOOK_ADMIN_LANID`；`EWS_MCP_DATA_DIR` 保存回执及附件缓存。

来源、镜像移除及历史测试边界见 [源码恢复记录](docs/source-restoration.md) 和接入说明；网页取码见 [爬虫历史交付入口](docs/github-crawler-handoff.md)。凭据、照片及运行数据不入库。

用目标环境 Python 验证；内网 OA 和 Exchange 另行联调：

```sh
python -B -c "import mcp_server"
python -m pip check
python -B -m unittest discover -s tests -v
```

当前测试与兼容变化见 [版本验证](docs/release-validation.md)；功能说明见 [附件下载](docs/attachment-downloads.md)。[工具合同验证](docs/tool-contract-validation.md) 与 [描述核对](docs/tool-description-review.md) 保留历史记录。
