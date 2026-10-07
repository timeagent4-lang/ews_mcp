# aias_mcp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 交付已确认的 29 个工具，支持每次调用选择个人或公邮代理凭据、直接执行邮件与日历操作以及自定义文件夹中的完整联系人编辑。

**Architecture:** 复用业务 mixin，替换入口身份与确认状态机。`tool_support.py` 提供脱敏、错误分类和写入结果确定性；`utils/audit.py` 提供请求上下文及两条 JSON 日志；联系人字段单独放在 `contact_fields.py`。保留附件临时下载存储。

**Tech Stack:** Python 3.12、FastMCP 2.14.7、MCP 1.29.0、exchangelib 5.6.0、unittest、JSON Schema。

工作目录固定为 `D:\code\aias_mcp`，复用用户指定的独立克隆与 `codex/aias-mcp-adaptation` 分支。所有测试命令使用 `.venv\Scripts\python.exe`，不访问实际邮箱。

## 1. 环境与公共错误接口（主代理）

文件：`config.py`、`outlook_client.py`、`tool_support.py`、`tests/test_config.py`、新增 `tests/test_errors.py`。

- [x] 在项目 `.venv` 安装保留依赖，记录原有测试失败原因；移除已删除模块专用依赖。
- [x] 先运行凭据选择和错误分类失败测试：`& .\.venv\Scripts\python.exe -m unittest tests.test_config tests.test_errors -v`。
- [x] 实现 `OutlookConfig.from_tool_params(mailbox, username=None, password=None)`，固定服务器、公邮 DELEGATE、个人失败不降级，密码不 trim，配置 repr 隐藏密码。

```python
personal = password is not None and password != ""
login = (username or mailbox).strip() if personal else service_login
```

- [x] 清除客户端四类缺失导入、镜像初始化和 OA 时区依赖；每次调用新 Account，保留凭据隔离的协议池。
- [x] 公共接口 `error_details(exc, *, submitted=False)` 返回安全业务错误字段；`ToolOperationError(code, message, *, status="failed", results=None)` 携带确定状态和已成功部分。直接 ResponseMessageError 保留 Exchange code；提交后的不确定网络或解析错误为 unknown。
- [x] `sanitize` 同时替换请求上下文中的密码、服务密码、API key，再做现有邮箱/令牌脱敏；不得输出原异常链。
- [x] 增加认证、配置、网络、冲突、unknown、短密码脱敏用例，运行上述测试至通过。

## 2. 联系人（联系人代理）

文件：`people_operations.py`、新增 `contact_fields.py`、新增 `tests/test_contacts.py`。不编辑入口或工具 schema。

- [x] 先编写默认/自定义文件夹、全面字段、编辑合并与清空测试，运行 `& .\.venv\Scripts\python.exe -m unittest tests.test_contacts -v` 看到功能缺失失败。
- [x] 增加 `list_contact_folders`、`list_contacts`、`update_contact`；所有业务函数不接收凭据、不使用 confirm 参数。
- [x] 新 Root/MsgFolderRoot + Deep FindFolder 轻量分页发现文件夹，自行拼路径；GetFolder 直接定位、检查类型与父文件夹，普通 IPF.Contact 过滤系统目录；自定义文件夹用轻量父 ID 查询上溯到目标 MsgFolderRoot，拒绝跨邮箱 ID，祖先 Exchange 错误原样保留。
- [x] `contact_fields.py` 提供 `CONTACT_SCHEMA`，与 `build_contact_values(contact, existing=None)`、`serialize_contact(item)` 共用官方可写字段、枚举与标签；日期 YYYY-MM-DD、notes 映射 body、photo 使用 FileAttachment。

```python
old = Contact(phone_numbers=[PhoneNumber(label="BusinessPhone", phone_number="old"), PhoneNumber(label="MobilePhone", phone_number="mobile")])
values = build_contact_values({"phone_numbers": {"MobilePhone": "new"}}, existing=old)
assert {p.label: p.phone_number for p in values["phone_numbers"]}["BusinessPhone"] == "old"
values = build_contact_values({"phone_numbers": {"MobilePhone": ""}}, existing=old)
assert "MobilePhone" not in {p.label for p in values["phone_numbers"]}
```

- [x] 新建缺省为空、编辑缺省保留，递归处理地址和索引字段；照片失败报告 partial/unknown 和已知联系人 ID，保留非照片附件。
- [x] find_people 指定 folder_id 仅搜索联系人；缺省保留 contacts/gal/auto，逐来源错误及全部失败准确报告。
- [x] 运行联系人测试并检查官方库签名；返回测试证据和具体集成接口给主代理。

## 3. 直接邮件和日历写入（邮件代理）

文件：`write_operations.py`、`calendar_operations.py`、`flag_operations.py`、`status_operations.py`、`tests/test_calendar_guards.py`、新增 `tests/test_direct_operations.py`。

- [x] 先新增直接执行、删除仅移动、提交前与后失败用例；更新旧预览测试为直接业务调用，运行 `& .\.venv\Scripts\python.exe -m unittest tests.test_direct_operations tests.test_calendar_guards -v`。
- [x] 删除 confirm、confirmation_id、预览摘要和额外 expected_version 机制；保留真实 EWS ChangeKey 与 NEVER_OVERWRITE 检查。
- [x] 邮件保存、发送、修改、移动、软删除、旗标和会议写入分别标记真实提交边界；批量逐项错误用 `error_details(exc, submitted=...)`。
- [x] send_draft 在提交前定位目标 Sent；From 保留目标邮箱，拒绝后不回退公邮发送；发送开关保留。
- [x] 保留 HTML/reply/forward、日历组织者/邀请/重复会议守卫，状态移除镜像字段。

```python
client.delete_messages([item_id])
message.account.bulk_delete.assert_called_once_with(ids=[message], delete_type="MoveToDeletedItems", send_meeting_cancellations="SendToNone")
message.delete.assert_not_called()
```

- [x] 运行邮件/日历及原附件和读取测试，返回批量状态字段约定。

## 4. 请求日志（日志代理）

文件：`utils/audit.py`、新增 `tests/test_audit.py`。不编辑入口或业务文件。

- [x] 先写开始/结束两条、同一 request_id、并发隔离、fallback 去重和短密码不泄漏测试；运行 `& .\.venv\Scripts\python.exe -m unittest tests.test_audit -v`。
- [x] 提供 `current_request_id()`、`request_secrets()`；ContextVar 只用于单次请求，低层 fallback 初始化并 finally reset，中间件复用同一上下文。
- [x] 白名单记录工具、mailbox、credential_mode、对象 ID、变更字段名、状态、耗时、错误码和批量计数；不记录参数、值、原搜索、原异常消息、登录账号和签名 URL。
- [x] 未知工具与 schema 拒绝同样两条 JSON 日志，正常返回和 fallback 无重复；日志每条单行。
- [x] 运行日志测试返回证据。

## 5. 注册与 MCP 集成（主代理）

文件：`tool_specs.py`、`mcp_server.py`、`tool_params.py`、`tests/test_tool_contracts.py`；清除 `confirmation.py`、`oa_lookup.py`、`team_directories.py`、`utils/lanid_email.py` 及不再使用的旧文档。

- [x] 先写真实 FastMCP Client 测试，替换 Exchange 边界而保留真实 schema/日志/低层处理器；检查恰好 29 工具。
- [x] 每个工具顶层必填 mailbox、可选 username/password；旧 lanid/name/确认/回执字段拒绝，联系人使用 contact 对象 + folder_id。
- [x] 单一直接 dispatcher：验证参数 → 配置/Account → 去除公共凭据 → 调用业务方法 → 包装统一结果。删除 OA 与执行持久化生命周期。

```python
payload = {"ok": status == "success", "status": status, "results": results, "request_id": current_request_id()}
```

- [x] TextContent 与 structuredContent 一致；失败/partial/unknown 保留结构化字段且低层 isError=true。批量 success/failed/unknown 数量和多来源查询不伪成功。
- [x] 保留 /mcp?key=...、常量时间 key 校验、healthz 和签名附件下载路由；服务不加载执行库或四类缺失模块。
- [x] 运行真实 MCP 成功、schema 拒绝、认证/权限/冲突、partial、unknown、HTTP key 和下载测试。

## 6. 交付与审查（主代理 + 独立审查代理）

文件：`README.md`、`.env.example`、`requirements.txt`、`Dockerfile`、`docker-compose.yml`、本计划。

- [x] 文档列出 29 工具、顶层参数和联系人完整字段示例，说明直接执行/软删除/个人与公邮模式、日志、发送/下载配置；容器日志限制容量。
- [x] 执行 `& .\.venv\Scripts\python.exe -m unittest discover -s tests -v`、`& .\.venv\Scripts\python.exe -m compileall -q . -x '(\.venv|\.git)'`、`git diff --check`。
- [x] 先审需求覆盖，再审代码质量，修复发现的问题并运行针对性回归；核对无 OA、confirm、执行数据库和旧模块运行依赖。
- [x] 本地提交完成改造。交付已通过的离线证据及未进行实际 Exchange 联调的边界，不推送或部署。

## 验证证据（2026-10-07）

- 独立需求审查和代码复核通过，无未解决发现；复核另运行 88 项相关测试并检查真实 SDK 载荷。
- 全量离线测试 152 项通过，包括真实 MCP/HTTP 入口、真实 SDK 模型与 XML、联系人父链、递归编辑、照片、软删除、错误分类和日志脱敏。
- `compileall`、`pip check`、`docker compose config --no-env-resolution --quiet` 和 `git diff --check` 通过。
- 测试只替换 Exchange 网络边界，没有访问实际邮箱；尚需部署环境验证实际权限、代理发件显示、文件夹发现及新平台调用。
- 当前 Docker Linux 引擎未运行，未构建或启动容器镜像。

## 后续逻辑审查修复（分批验收）

2026-10-07 复审确认六个问题。按以下顺序修复，每批先补失败回归，再修改；相关测试、完整测试、编译和独立复核通过后单独提交，再进入下一批。

- [x] 第一批：同步 Exchange 调用卸载到工作线程；新增 3 项失败回归转绿，相关 35 项及完整 155 项通过，编译、依赖和差异检查通过。独立复核另验证真实 HTTP 取消和会话终止后后台调用完成且结束日志保留实际结果，无本批新发现。
- [x] 第二批：修复 CalendarView 排序后分页和真实下一页判断；会议响应及取消显式预解析目标 Sent。新增 6 项回归由失败转绿，完整 161 项、编译及差异检查通过；独立复核 61 项及四种原生 CreateItem XML 通过，无本批新发现。写入后 unknown、权限/发送限制和普通预约软删除保留。
- [x] 第三批：旗标截止日期回显改为读取保存后的邮件对象。新增 2 项多场景回归，旧日期保留场景由失败转绿；完整 163 项、编译及差异检查通过，独立 24 项相关测试通过，无本批新发现。
- [x] 第四批：补 GAL 目录 Notes，Exchange 2016+ 按实际系统 ID 过滤，保留同名自定义目录并修复复核中发现的重命名系统目录绕过。新增 13 项回归、调整原元数据查询夹具，完整 176 项、编译及差异检查通过；独立 41 项相关测试通过，无本批新阻断问题。默认 Contacts、父链、私人备注、照片和字段编辑规则保留。旧版不支持可靠系统标识，维持歧义目录限制并显式报告；兼容边界已写入 README 和设计。

用户后续授权：完成已知和复核中新发现的问题修复后，推送 `origin` 的当前改造分支。
