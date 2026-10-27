# EWS MCP 内网合入说明（2026-10-27）

这是开发侧合入任务，应用端 AI 的验证报告只能作为联调证据，不能替代服务源码和日志核对。

- 仓库：https://github.com/timeagent4-lang/ews_mcp
- 交付分支：`codex/attachment-upload-20261026`
- 本轮父提交：`0feaa92d1c6ed49cb1cedd26f1af387238894d00`
- 目标代码：以交接提示词提供的固定提交 SHA 为准，读取该提交的本说明和源码，不从摘要猜写。
- 交付范围：下列 31 个代码、测试和当前说明文件，以及本说明。工具仍为 30 个。

## 合入步骤

1. 先记录内网当前分支、提交和未提交改动，备份代码及运行数据；在独立集成分支逐文件合并，不直接整目录覆盖。
2. 能用 Git 时，取固定目标提交，对照其父提交检查本轮差异，合并或 cherry-pick 到内网集成分支。存在内网独有修复时逐项处理，不能用外网文件直接覆盖。
3. 若只能通过 Firecrawl 读取，先读本说明，再读取固定提交下的完整源码；优先 Raw，失败再读 GitHub 文件正文。遇到截断或获取失败，报告具体文件，不根据摘要补写。
4. 不覆盖真实 `.env`、凭据、私有部署差异、日志、`operations.db`、附件暂存与缓存。不清空数据库，不批量删占用，不关闭幂等保护。
5. 保留内网已有的附件回执放宽与提交阶段修复，比较行为后合入，不重复退回旧实现；本轮无需升级依赖。

固定提交文件网址模板（将 SHA 换成交接提示词指定值）：

```text
https://github.com/timeagent4-lang/ews_mcp/blob/SHA/相对路径
https://raw.githubusercontent.com/timeagent4-lang/ews_mcp/SHA/相对路径
```

## 本轮合同

- LANID 仍由应用从登录信息取得并传参。参数 message 说明具体缺失字段、类型、范围或确认组合；不增加 missing_fields，不回显敏感值。额外参数使用统一拒绝提示，不增加公开参数。
- 完整 structuredContent 同时序列化为 JSON TextContent；移除重复 ok，保留 MCP isError。调用方需刷新工具 schema，读取结构化字段或同内容 JSON。
- pending/confirmed/executing/processing 不带 error_code，isError=false；completed 无错误；failed/partial/unknown 有具体错误码。普通读取不增加写生命周期 status。参数、权限、令牌拒绝只表示本次请求失败，不把原操作伪造为 failed。
- 执行返回、查询和重复确认使用一致的持久结果，保留错误类型、Exchange 原因和已有 request_id。不存在操作为 OPERATION_NOT_FOUND，区别于执行结果 unknown。
- 联系人和线程查询区分完整空结果、部分完成和全部来源失败。部分来源成功保留结果及覆盖信息，partial=true、isError=false，审计为 incomplete；查询未完成的来源不留下残留记录。
- send_draft 保留 30 秒等待上限与原后台任务继续一次。尚无结果时返回 processing，无错误码，message 为“邮箱服务不稳定 请五分钟后查看结果”。它不证明发送已开始或成功。
- 原发送任务活跃时保留草稿占用；任务真正结束后，即使 unknown 也释放发送占用，重启释放遗留发送占用。同一 unknown 操作号仍不可重发。实际结果交给调用方五分钟后查询员工已发送邮件核实，不自动重发或增加服务端核对任务；未找到副本不证明未发送。
- update_draft/send_draft/attach_draft_attachment 区分 SDK 写调用前失败与提交后不明确异常。附件 SDK 正常返回即成功，不再强制重读或通过返回 Size/回执完整性把成功改成 unknown；保留提交前版本及上传完整性检查。附件 unknown 隔离继续保留，不能套用发送释放规则。
- create_contact 的 email 可省略，提供时校验。增加 department/mobile_phone/office/notes；notes 保存至原生 Contact.body，手机使用 MobilePhone。预览、保存、回执及详情一致；不增加联系人工具或个人文件夹枚举。
- 已知年 1 的 last_response_time 占位值返回 null，保留 response_type；有效历史时间出现秒级偏移时转 UTC 保持同一时刻；全天日期不变。
- create_draft 的 reply/reply_all 支持 MeetingRequest 普通文字回复，原生保存回复草稿，不改变参会状态。RSVP 仍走 respond_to_event 和日历事件 ID；其他会议类型及会议邀请转发范围不扩大。
- 工具和参数描述已精简，当前使用文档同步；不要恢复已移除的查询入口或能力宣称。

## 文件清单

运行代码：

```text
calendar_operations.py
confirmation.py
mail_operations.py
mail_replies.py
mcp_server.py
people_operations.py
tool_specs.py
tool_support.py
utils/audit.py
utils/tool_results.py
write_operations.py
```

测试：

```text
tests/test_attachment_uploads.py
tests/test_audit_arguments.py
tests/test_calendar_datetime.py
tests/test_contact_fields.py
tests/test_draft_attachments.py
tests/test_flag_confirmation.py
tests/test_mail_read_results.py
tests/test_meeting_replies.py
tests/test_people_boundaries.py
tests/test_send_response_wait.py
tests/test_tool_concurrency.py
tests/test_tool_contracts.py
```

当前说明：

```text
README.md
docs/attachment-uploads.md
docs/html-drafts.md
docs/intranet-integration.md
docs/logging.md
docs/review/ews-mail-mcp-playbook/SKILL.md
docs/send-response-wait.md
docs/tool-contract-validation.md
```

## 测试与回报

发布前当前源码 272 项离线测试通过，依赖检查无冲突；独立代码审查无阻塞项。运行源码及全部测试与父提交叠加本清单后的内容逐文件核对。没有访问真实 OA/Exchange、发信、部署或验证内网镜像。

内网先跑原有基线，逐模块合并后执行：

```sh
python -B -m unittest discover -s tests
python -m pip check
```

测试使用临时数据目录及 OA/Exchange 网络替身，不读取生产数据库。重点核对 30 秒后原后台任务继续、同操作不重复发送、活跃占用与结束释放、附件正常返回无额外重读、联系人字段、日期和会议回复不触发 RSVP。刷新调用方 schema，并验证 JSON TextContent/structuredContent/isError 的读取。

数据库落库故障后的占用回收不在本轮扩展范围，继续保守处理；不要因此批量清库。真实 OA/Exchange 权限、超时、投递和 Outlook 显示需使用获准对象另行验收，未获明确授权不发真实邮件或会议响应。

完成后回报固定目标提交、实际合入文件、保留的内网差异、冲突处理、测试结果、是否部署和剩余验收项。只有网页读取能力时，明确说明尚未写入文件、运行测试或部署，不声称“已同步”。
