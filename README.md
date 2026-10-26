# EWS MCP

面向内网的 Exchange 邮件 MCP 服务，通过 OA 按员工 LANID 唯一解析有效邮箱，以固定服务账号通过 DELEGATE 访问对应邮箱。

- 公开 30 个工具（见 [tool_specs.py](tool_specs.py)）；`arguments` 为扁平对象，必填 `lanid` 及工具所需业务参数。邮箱仅取自 OA 按 LANID 查询的唯一结果，仍检查结果唯一性和邮箱有效性；兼容字段 `name` 可省略，传入时忽略，不用于验证或授权。
- 写操作先预览，再携原业务参数、`operation_id` 和 `confirm_token` 确认，可选 `idempotency_key`。结果未知时用原 `operation_id` 查回执，不自动重试。
- `send_draft` 最多等待 30 秒；确认发送仍无最终结果时返回 `processing` 和原操作号，后台继续处理，调用方查询回执且勿重发。预览超时与调用示例见 [发送等待上限](docs/send-response-wait.md)。
- `EWS_MCP_SEND_ENABLED` 默认关闭，限制真实发送、会议通知及 OOF 执行；预览仍可用。
- `prepare_attachment_download` 返回内网 HTTP 签名链接，供 AI 执行器 GET 下载；响应不含附件字节。
- `prepare_attachment_upload` 授权执行器 HTTP PUT 上传，`attach_draft_attachment` 经预览和确认附加到现有草稿，支持普通附件和 PNG/JPEG CID。附加后须重新预览发送；旧发送预览拒绝草稿变化。配置、执行器及恢复说明见 [附件上传](docs/attachment-uploads.md)。
- `check_availability` 返回各邮箱的忙闲区间，由调用方 Agent 选择公共时段；`interval_minutes` 为采样间隔（默认 30，范围 5–1440），不是会议时长。参数与迁移说明见 [忙闲查询](docs/availability.md)。
- `list_tasks` 默认先筛选未完成任务，再按到期日降序分页；用返回的 `next_offset` 取后续页。参数、空结果和分页边界见 [任务列表](docs/task-listing.md)。
- 审计日志的 `input_arguments` 保存校验前的实际参数，保留字段、类型及嵌套结构，失败调用也记录。正文、附件及令牌等敏感值脱敏，超长输入标记截断（上限 16 KiB）。
- 错误与审计日志按日期分别落盘，保留最近30个自然日；宿主机目录、权限和控制台降噪见 [日志说明](docs/logging.md)。

接入、验收及回滚见 [内网接入说明](docs/intranet-integration.md)，变量见 [.env.example](.env.example)。服务账号用 `OUTLOOK_ADMIN_LANID`；`EWS_MCP_DATA_DIR` 保存回执及附件缓存。

来源、镜像移除及历史测试边界见 [源码恢复记录](docs/source-restoration.md) 和接入说明；网页取码见 [爬虫历史交付入口](docs/github-crawler-handoff.md)。凭据、照片及运行数据不入库。

用目标环境 Python 验证；内网 OA 和 Exchange 另行联调：

```sh
python -B -c "import mcp_server"
python -m pip check
python -B -m unittest discover -s tests -v
```

当前测试与兼容变化见 [版本验证](docs/release-validation.md)；功能说明见 [附件下载](docs/attachment-downloads.md)。[工具合同验证](docs/tool-contract-validation.md) 与 [描述核对](docs/tool-description-review.md) 保留历史记录。
