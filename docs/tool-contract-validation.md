# 工具合同修复与离线验证

本次业务源码仅修改 `tool_specs.py`、`mcp_server.py`、`calendar_operations.py`。
保留现有注册、业务分发、附件下载和禁用工具集合；`tool_params.py` 未接入或修改。

## 公开参数与调用入口

- 补充 `respond_to_event.response`、`create_draft.mode`、`find_message.aqs/folder`、`list_flagged_messages.folder/status`。
- `find_message.query` 可省略，默认空串，只做主题包含搜索，可结合结构化过滤。AQS 不能混用非空 query 或任何结构化过滤（包括显式 false）。
- 回复、回复全部、转发草稿必须给 `reply_to`；默认新建模式不接受原邮件引用。
- 对照现有邮件、日历、标记模块：分页长度 1–100，偏移量非负且无实现层上限；正文长度 1–100000；批量 ID 数组 1–50 项，不接受空 ID 或重复 ID。
- JSON schema 的 default 仅是公开默认值，运行时依赖业务方法默认参数，不注入默认字段，避免改变确认快照。
- 入口在现有审计中间件之后、业务处理之前校验原始 arguments，分发层也校验 params。未知字段、显式 null、错误类型和非法操作组合返回 `INVALID_PARAMS`，不回显输入；拒绝结果经过原审计链路，再由已有 MCP 结果封装层设置错误标志。注册处理函数不再暴露可被传入的 `_tool` 默认参数。
- 当前注册工具仍为 28 个。人员、任务、可用性等缺失模块的业务上下限无法核实，保留原有声明。

## 写操作三种请求

以下字段均放在 `arguments.params` 中：

| 路径 | 参数 |
| --- | --- |
| 预览 | lanid、name、必需业务参数，可选 idempotency_key |
| 确认 | lanid、name、与预览一致的业务参数、operation_id、confirm_token |
| 仅查询 | lanid、name、operation_id |

确认仍允许携带 idempotency_key；执行绑定于预览记录。仅查询不接受业务字段、令牌或幂等键。

所有回执查询、确认和重放先通过 OA 校验，再校验记录的 mailbox 和 tool。
归属不符返回 `OPERATION_SCOPE_MISMATCH`，不返回记录内容；仅查询和已有回执重放无需连接 Exchange。
已完成操作的确认重放也要求令牌、业务参数一致。保留过期预览拒绝、幂等和不自动重试行为；同一幂等键在不同邮箱仍相互独立。

## 日历限制

更新、响应、取消均拒绝周期主事件，只接受明确的 Single、Occurrence 或 Exception；类型缺失时拒绝操作。
更新和取消要求员工邮箱与组织者邮箱匹配。响应拒绝组织者，并要求员工出现在必需、可选或资源参会人列表中。
这些检查在预览和确认时都执行，发生在任何写入前。
保留发送开关、通知参数，以及无参会人个人预约移至已删除项目的行为。
身份匹配使用 Exchange 返回的邮箱地址，不新增别名解析能力；缺少可匹配地址时拒绝操作。

## 测试与边界

在项目目录执行：

```powershell
python -B -m unittest discover -s tests -v
```

本地验证使用现有临时虚拟环境中的 FastMCP 2.14.7、MCP 1.29.0、exchangelib 5.6.0。
2026-09-22：54 个测试通过，其中原有附件测试 23 个，新增合同/日历测试 31 个。
测试使用实际 FastMCP 注册、MCP Client 内存传输、分发器、SQLite 回执存储，以及真实邮件/日历业务方法；Exchange 写方法均被替换为测试桩。
已覆盖参数/default/枚举、AQS、公开 schema 的实际拒绝行为、预览/确认/查询、跨工具/邮箱拒绝、幂等/过期/不重复执行、日历角色及发送行为；包括建立 Exchange 连接期间预览过期时仍不得执行。

完整 `import mcp_server` 仍因缺少 `utils` 失败；同时缺少 `utils/audit.py`、`utils/lanid_email.py`、`availability_operations.py`、`mirror.py`、`oof_operations.py`、`people_operations.py`、`task_operations.py`。
服务器测试仅在导入期间替换 OA、审计、Outlook 客户端依赖，没有补造生产模块。
因此未验证真实 OA/审计、完整 OutlookClient 组合、真实 Exchange 行为和部署的 SSE/内网通信；不能视为完整集成测试通过。
没有真实邮件发送或日历写入，也没有部署或 Git 提交。

内网下一步：在模块完整的环境先运行上述离线测试，再启动服务核对 tools/list 和无写入的非法参数拒绝、原操作号回执查询；真实业务写入另行安排。

## 官方资料补核（2026-09-22）

上一轮主要依据项目源码、已安装的锁定版 SDK 源码和离线测试，并未完成逐项官方文档核对。本轮补核如下；只更新此验证记录，没有修改业务源码或重新执行测试。

| 核对事项 | 官方／维护者资料及结论 |
| --- | --- |
| MCP 输入验证 | [MCP Tools 规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)要求服务端校验工具输入并实施访问控制；仅公开 schema 不等于执行校验。 |
| FastMCP 校验模式 | [维护者文档](https://github.com/prefecthq/fastmcp/blob/main/docs/servers/tools.mdx)区分默认宽松模式与 strict_input_validation。另核对本地 FastMCP 2.14.7 的 settings.py、server/server.py、tools/tool.py：默认 strict 为 false，函数签名校验不等于自定义嵌套 schema 校验；本项目入口补校验有必要。版本结论以本地锁定版源码为准。 |
| 三种请求、默认值 | [JSON Schema oneOf](https://json-schema.org/understanding-json-schema/reference/combining)要求恰好一个分支成立；[default 注解](https://json-schema.org/understanding-json-schema/reference/annotations)本身不填充遗漏值。当前 schema 分支和不注入默认字段的做法与此一致。 |
| 搜索 | [exchangelib 维护者文档](https://ecederstrand.github.io/exchangelib/)明确 subject__contains 为主题包含搜索，QueryString 通过 filter 的位置参数传入且不可结合其他过滤。 |
| 周期类型 | [Microsoft CalendarItemType](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/calendaritemtype)列出 Single、Occurrence、Exception、RecurringMaster。拒绝主事件是本项目约束，不是 EWS 不支持周期操作。 |
| 响应角色 | [Microsoft AcceptItemType](https://learn.microsoft.com/en-us/dotnet/api/exchangewebservices.acceptitemtype?view=exchange-ews-proxy)明确组织者不能接受自己的会议；参会人邮箱精确匹配为本项目的额外检查，不代表覆盖所有别名和通讯组场景。 |
| 取消与删除 | [Microsoft 删除预约／取消会议文档](https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/how-to-delete-appointments-and-cancel-meetings-by-using-ews-in-exchange)区分有参会人的会议和无参会人的预约，后者不需要发送通知。当前两条路径符合此区分。 |

OA 身份映射、operation_id／confirm_token、SQLite 回执、幂等键和附件签名链接均属本项目协议，不能称为 Microsoft 或 MCP 原生保证。官方资料不能替代部署版本、权限和实际服务器行为的联调。

后续已按用户要求修复搜索分页异常元素处理（包括第 limit+1 项），以及空会话误报文件夹不可访问的问题，详见下方复核记录。新增 9 个回归测试，当前全部 63 个离线测试通过。缺失模块和内网联调仍未完成。会议通知副本位置已按用户决定保持现状，不再作为待修复事项；工具描述仅说明员工 Sent 不能作为通知送达证明。

后续逐项文案复核见 [工具描述复核记录](tool-description-review.md)，包含补修文案和读取问题修复记录。

## 提交前审计发现的两项修复（2026-09-22）

- OA 身份解析后，邮箱统一去除首尾空格并转为小写，与原 `OutlookConfig.from_service_env` 规则一致；查询回执、确认、幂等查询和新建预览均使用此值。无有效邮箱时在查询回执前拒绝，不需要为回执查询连接 Exchange。
- 原始参数校验移入 FastMCP 中间件，注册在既有审计中间件之后；删除外层错误封装中的提前返回。参数错误、未知工具和禁用工具的拒绝结果均经过审计中间件和原低层处理器，仍不会进入 OA 或业务执行。
- 新增回归测试覆盖邮箱大小写／空格变化下的回执重放、新预览邮箱标准化、确认不重复执行、无效 OA 邮箱提前拒绝，以及非法调用经过两层审计入口且保留结构化错误。修复前这些测试已实际失败。
- 修复后全量运行 `python -B -m unittest discover -s tests -v`，67 项离线测试全部通过（原有 63 项，新增 4 项）。
- 审计入口测试使用实际 FastMCP 2.14.7／MCP 1.29.0 调用链，但以记录型测试替代对象代替缺失的 `utils.audit`；证明入口和结果传递，不证明真实审计日志存储、脱敏和去重正确。
- 本轮未修改数据库历史记录；没有部署或真实邮件／日历写入。

后续按用户确认修正拍照还原疑似拼写错误：服务配置字段统一为 `lanid`，环境变量统一为 `OUTLOOK_ADMIN_LANID`，与 `OutlookClient` 读取字段一致。部署时使用该环境变量名；不保留错误拼写作为别名。新增配置测试通过真实配置构造 SDK Credentials，不连接 Exchange。
修正后全量离线测试 69 项通过，其中新增配置测试 2 项；完整服务集成仍受上述缺失模块限制。
