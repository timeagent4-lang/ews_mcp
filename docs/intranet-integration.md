# 内网完整项目接入说明

## 交付定位

这是用于合入完整项目的源码快照。由于原目录没有历史提交，首次提交包含当前全部可用源码，不能把整份快照当作相对生产版本的补丁。
照片未包含在交付中。不要用本目录整体替换内网项目，不要删除内网已有但本目录缺少的模块。

本地修复与离线验证已完成；真实 OA、审计、完整客户端组合、Exchange 权限及最终部署的 HTTP 下载尚待内网验收。
之前内网 GET 成功仅证明当时链路可行，不代表本次最终版本已经验收。

## 文件与合并范围

| 文件 | 本次累计变更，内网接入要求 |
| --- | --- |
| `attachment_downloads.py` | 新增。签名链接、SQLite 下载索引、文件缓存配额、清理、HTTP GET 路由及日志脱敏。 |
| `config.py` | 增加下载配置；服务身份字段统一为 `lanid`，读取 `OUTLOOK_ADMIN_LANID`。保留内网其他配置。 |
| `mail_operations.py` | 增加 `prepare_attachment_download`，复用授权范围及流式导出；保留旧底层 save 行为；修复分页异常元素及空会话处理。 |
| `mcp_server.py` | 注册下载路由及生命周期；校验公开 schema；修复回执身份、工具归属、邮箱标准化及审计入口；保留已有业务分发。 |
| `tool_specs.py` | 增加下载工具；对齐参数、约束、默认值及描述；写操作区分预览、确认、查询；保持禁用集合。 |
| `calendar_operations.py` | 更新、响应、取消的周期主事件及角色限制，在写入前校验；保留通知发送开关及个人预约删除行为。 |
| `tool_params.py` | 附件阶段仅新增 `PrepareAttachmentDownloadParams`；仍不接入运行调用链。后续合同修复未依赖此文件。 |
| `tests/`、`docs/` | 合入回归测试及说明；现有测试保留。 |

其余源码为已有参考快照，本次没有为缺失模块补造实现。
`confirmation.py`、`outlook_client.py`、`write_operations.py`、`flag_operations.py`、`status_operations.py`、`oa_lookup.py`、`tool_support.py` 以及 `requirements.txt` 不应因为打包在本次首次提交中就盲目覆盖内网版本，先比较实际差异及调用接口。

## 配置与数据

1. 服务账号变量使用 `OUTLOOK_ADMIN_LANID`，同时保留现有 `OUTLOOK_ADMIN_PASSWORD`、`OUTLOOK_SERVER`（或原有 `outlook_host`）。如果部署模板仍用旧错误拼写，迁移变量名并重建容器；不要修改账号值或回传凭据。
2. 保留 OA 的真实地址、鉴权和姓名匹配实现。本副本 `oa_lookup.py` 默认地址为脱敏占位值，不能代替内网配置，也不能代替 `utils.lanid_email`。
3. 保留 `EWS_MCP_DATA_DIR` 持久卷及已有 `operations.db`、`mirror.db`；新增的 `attachment_downloads.db` 与原数据库分开。不要删除历史回执或把测试数据库复制进生产。
4. 部署目标为单实例和服务端本地持久化文件系统。已有多实例环境先核实路由和文件共享问题，本方案不直接保证多实例独立磁盘可用。
5. 如此前试运行已经产生邮箱含大写或首尾空格的回执，先离线核查同邮箱／同幂等键是否重复，评估后再制定迁移。当前修复规范化后续请求，不自动改写历史记录；不要直接批量 UPDATE 或清空回执。

启用下载时配置如下，示例占位值必须换成内网实际配置：

```env
EWS_MCP_DOWNLOAD_ENABLED=true
EWS_MCP_PUBLIC_BASE_URL=http://实际宿主机内网地址:7712
EWS_MCP_DOWNLOAD_SECRET=使用独立随机密钥替换
EWS_MCP_DOWNLOAD_TTL_SECONDS=600
EWS_MCP_DOWNLOAD_RETENTION_SECONDS=86400
EWS_MCP_DOWNLOAD_MAX_BYTES=5242880
EWS_MCP_DOWNLOAD_CACHE_MAX_BYTES=1073741824
EWS_MCP_DOWNLOAD_CACHE_MAX_FILES=1000
EWS_MCP_DOWNLOAD_CLEANUP_SECONDS=300
```

密钥由运维通过既有密钥管理方式注入，可用 `python -c "import secrets; print(secrets.token_hex(32))"` 在受控环境生成，输出不得粘贴回对话或提交 Git；已有有效密钥则保留，不要每次启动重新生成。
容器监听应为 `EWS_MCP_HOST=0.0.0.0`、`EWS_MCP_PORT=7805`；保持宿主机 `7712:7805` 映射。AI 员工访问宿主机 7712，不要求访问容器内部地址 7805。
有反向代理时保留既有认证边界，并确认 MCP 路径、`/healthz` 和 `/downloads/` 转发正确。
下载 URL 是持有即可下载的短期凭证，网关和工具审计不得记录完整签名 URL。
TTL、容量、清理等默认值完整；原 exports 目录中的未索引旧文件不会自动迁移、删除或计入新配额。

## 版本与本地验证边界

本地测试使用 Python 3.13.13，核心包与 requirements 一致：FastMCP 2.14.7、MCP 1.29.0、exchangelib 5.6.0、pydantic 2.13.4。
其余已安装直接依赖也与声明一致。本地未安装 openai、anthropic、PyMySQL、pycryptodome、json-repair；本地 pip check 仅验证已安装包。
本地 Starlette 1.6.0、jsonschema 4.26.0、httpx 0.28.1、uvicorn 0.53.0 为测试记录，不是新增锁定要求。内网先记录实际版本，在隔离环境验证，不直接升级生产全部依赖。

67 项原合同／附件／日历等测试加 2 项配置测试，共 69 项通过。MCP 合同测试运行真实框架、分发器和 SQLite，但使用测试替代对象替代缺失的 OA、审计和 OutlookClient；不能用这些通过结果证明真实审计落库或完整项目可启动。
参数校验中间件必须位于真实审计中间件内侧，非法输入仍应产生审计结果。错误封装使用既有 FastMCP 内部处理器挂接点，版本不同需要重新核验。

## 可直接转交内网 AI 的提示词

请将本仓库交付包作为参考变更，合入内网现有完整 EWS MCP 项目。完成实现接入和隔离测试后报告结果。不要发送真实邮件、修改或删除真实邮件、创建／更新／响应／取消真实日历事件；不要因测试要求临时打开生产发送开关。生产切换按现有发布流程执行，未获发布授权时停在可发布产物和验证报告阶段。

请依次执行：

1. 阅读本文件和另外三份说明：`docs/attachment-downloads.md`、`docs/tool-contract-validation.md`、`docs/tool-description-review.md`。确认交付版本，以交付包来源 Git 提交号和自身 SHA-256 记录来源。
2. 记录内网当前 Git 状态、Python／直接和传递依赖版本、容器启动方式、端口映射及持久卷路径。备份代码和配置；数据库采用 SQLite backup API 或停止写入后的完整一致备份，不要只复制活跃 WAL 数据库的主文件。不输出密钥。
3. 在集成分支或隔离副本逐文件对比，按上表合入 6 个改动源码文件和 1 个新增模块。保留内网全部其他业务模块及并行改动；`tool_params.py` 只保留所述附件参数模型，不接入运行调用链。遇到实现差异先基于内网实际签名处理，不按照片快照覆盖。
4. 检查真实 `utils.audit`、`utils.lanid_email`、availability、mirror、oof、people、task 模块均存在且接口匹配；禁止复制 tests 中的 Mock 或临时替代模块到生产。仍缺少时报告阻塞，不能宣称完成。
5. 使用真实模块，在目标镜像或隔离运行环境执行 `python -B -c "import mcp_server"` 和 `python -m pip check`；运行 `python -B -m unittest discover -s tests -v` 及内网已有测试。交付测试应至少 69 项通过，真实模块导入检查必须独立执行，不能让测试桩掩盖缺失导入。若另需依赖，在隔离环境处理，不直接升级生产。
6. 校验 `OUTLOOK_ADMIN_LANID`、OA、下载配置、持久卷和 HTTP 映射。启动集成服务，从 AI 员工实际执行环境访问 `http://实际宿主机内网地址:7712/healthz`，预期状态 ok、工具数 28。刷新 MCP tools/list，确认新增工具和禁用集合。若内网原本有额外工具，列出差异并保留授权功能，不为凑数量删除工具。healthz 仅证明存活，不能代替 OA／Exchange 检查。
7. 用获准测试员工和已有测试邮件验证只读路径：AQS 单独查询、关键词结合结构化过滤、非法 AQS 混用、分页、空会话、正文参数。检查非法类型、未知字段、仅令牌没有操作号均被拒绝；失败输入不进入业务，日志及审计不泄露令牌。
8. 验证真实审计链：合法只读调用、非法参数、未知或禁用工具均有符合原审计约定的记录；无意外重复记录；工具输入输出中的确认令牌和下载签名已脱敏。回执查询仍经过真实 OA，且不建立 Exchange 连接。回执的预览／确认／幂等／跨邮箱跨工具拒绝、邮箱大小写和空格变化、日历角色与周期限制，在隔离测试中验证，写方法使用测试替代对象，不触发真实发送或日历写入；不要往生产回执库注入伪造记录。
9. 选已有获准 TXT 或 XLSX 附件：get_attachment 获取 ID，prepare_attachment_download 返回元数据和链接，AI 执行器 GET 保存到任务目录；比较字节数和 SHA-256，使用解析工具读取已知测试内容。附件原始字节不放入 MCP 响应。有效链接重复 GET 应一致，篡改签名应 403，过期应 410，重新准备后可下载；错配附件 ID 应拒绝。完整签名 URL、正文和凭据不回传。
10. 在隔离部署中核验相同持久卷和密钥下重启后未过期链接仍可下载、过期缓存定期清理、原 operations.db 回执保留。不要更改生产时钟或通过缩短生产保留期来测试。记录权限／配额／大小边界中实际测试及未测试的项目。
11. 生成发布与回滚步骤。保持会议通知副本现状、既有发送开关、确认及不自动重试语义；先确认数据兼容再发布。回滚优先恢复代码和配置，不能直接恢复旧 operations.db 而抹掉上线后已执行的回执，否则可能导致重复写入；不确定操作用原 operation_id 查询，不自动重试。

返回报告应包含：交付版本／合并后的提交号、修改文件及冲突处理、真实依赖版本、缺失模块情况、离线测试数、真实模块导入结果、healthz 和 tools/list、真实 OA／审计验证、附件 GET 状态与大小／哈希、数据库与持久卷检查、未执行项目、发布或阻塞状态。结果脱敏，不回传真实附件正文、员工身份明细、密码、密钥、确认令牌和完整有效下载链接。
