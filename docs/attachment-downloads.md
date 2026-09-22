# 原始邮件附件下载

## 行为与接口

`get_attachment` 继续提供附件清单、元数据及受限文本。
新增 `prepare_attachment_download` 沿用身份校验、邮箱范围和附件归属检查，
把文件流式保存到已有的 `EWS_MCP_DATA_DIR/exports/<邮箱>/`，返回短期签名链接。
GET 下载读取这个缓存文件，不再访问 Exchange，不把文件内容放入 MCP 响应。

调用示例（替换为真实身份及该邮件返回的 ID）：

```json
{
  "params": {
    "lanid": "员工LANID",
    "name": "员工姓名",
    "message_id": "邮件ID",
    "attachment_id": "附件ID"
  }
}
```

成功响应的 `structuredContent.results` 包含：

```json
{
  "download_id": "随机标识",
  "download_url": "http://宿主机内网IP:7712/downloads/随机标识?expires=到期时间戳&token=签名",
  "filename": "报表.xlsx",
  "content_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "size": 12345,
  "sha256": "文件SHA-256",
  "expires_at": "UTC到期时间"
}
```

相同 JSON 也放在 `content` 文本中，兼容仅将文本交给模型的客户端。
AI 执行器通过 HTTP GET 下载到自己的任务目录，核对大小和 SHA-256，再调用
文档/表格工具。原始字节不进入模型上下文；随后读取或提取的内容可能进入上下文。

目前只支持 `FileAttachment`，包括 PDF、Office、图片、文本等原始文件，
不提供格式转换；转发邮件等 `ItemAttachment` 返回 `ATTACHMENT_NOT_SUPPORTED`。
零字节文件可以下载。文件名按 UTF-8 Content-Disposition 返回，路径分隔符及控制字符会被去除。

## 部署配置

以下文件清单仅针对附件阶段。接入本次累计全部改动时，以 [内网完整项目接入说明](intranet-integration.md) 的合并清单为准，其中还包含日历、工具合同、审计入口和配置字段修复。

先将这次变更的 5 个现有源码文件和新增的 `attachment_downloads.py` 一同发布：
`config.py`、`mail_operations.py`、`mcp_server.py`、`tool_specs.py`、`tool_params.py`。
不用新增第三方依赖。默认禁用链接功能，但工具始终出现在列表中；禁用时调用明确报错。

在现有容器配置中追加，保留原有 OA、Exchange、数据目录及其他设置：

```env
EWS_MCP_DOWNLOAD_ENABLED=true
EWS_MCP_PUBLIC_BASE_URL=http://实际宿主机内网IP:7712
EWS_MCP_DOWNLOAD_SECRET=替换为至少32字节的随机密钥
EWS_MCP_DOWNLOAD_TTL_SECONDS=600
EWS_MCP_DOWNLOAD_RETENTION_SECONDS=86400
EWS_MCP_DOWNLOAD_MAX_BYTES=5242880
EWS_MCP_DOWNLOAD_CACHE_MAX_BYTES=1073741824
EWS_MCP_DOWNLOAD_CACHE_MAX_FILES=1000
EWS_MCP_DOWNLOAD_CLEANUP_SECONDS=300
```

生成签名密钥：

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

密钥配置在服务器，AI 员工只拿到短期链接。不要将密钥提交到公开仓库。
已有映射 `7712:7805` 保持不变；容器继续监听 `0.0.0.0:7805`。
如有反向代理路径前缀，PUBLIC_BASE_URL 包含该前缀，代理需转发 `/downloads/`。
已有内网 HTTPS 入口时优先使用它；HTTP 链接和文件只能经由可信内网链路。

数据目录必须为服务账号独占的绝对路径，并有可写持久卷。
新增 `attachment_downloads.db` 保存下载元数据和配额；不要删除仍有有效链接的索引。
文件仍在原 exports 目录，每个文件使用随机磁盘文件名，原文件名保存于索引。
既有未索引的导出文件不自动迁移、不自动删除，也不计入新下载缓存配额；
首次上线请单独核对旧文件占用，避免把新缓存限额当成整个磁盘的占用上限。

更改容器环境变量后重建容器，单纯 `docker restart` 不会更新环境变量。
应用源码打包于镜像时需要重新构建镜像。重连 MCP 后工具总数为 28。

## 凭证、过期与清理

- 链接是 bearer 凭证，持有者可下载指定缓存文件；不宣称绑定 GET 请求者身份。
- 签发链接前沿用现有 OA/邮箱/邮件/附件检查。OA 姓名匹配不替代外层调用者认证，
  现有平台/网关的授权边界必须保留。
- 链接只含随机 ID、到期时间及签名，不含邮箱、磁盘路径或 Exchange 凭据。
- 有效期内允许重复 GET；第一版重试从文件开头下载，不提供 Range 断点续传。
- 过期后重新调用准备下载工具，会再次校验归属并从 Exchange 导出；当前不做请求间去重。
- 每次保存先检查附件声明大小，再累计实际流式字节，超限最多多读一个字节。
- 并发写入先按单文件最大值原子预留缓存容量，成功后按实际大小计费；
  因而空间不足以预留单文件上限时，小文件也可能暂时被拒绝。
- 文件完成后原子发布；失败清理临时文件并释放预留，定期清理仅处理索引中的过期文件。
- 写入中的预留使用一小时租期，有数据进展时续期，避免短缓存保留时间误删正在下载的文件；
  进程崩溃留下的部分文件在租期到期后由清理任务处理。
- 停用链接功能后，只要服务仍运行且存在下载索引，仍会清理此前生成的过期缓存。
- 同一数据目录、相同密钥下重启后有效链接仍可使用；更换密钥使所有旧链接失效。
- 单实例/本地文件系统是本次部署目标。多个实例独立磁盘会出现链接找不到文件，
  不应直接复制部署；需要另行设计共享存储/路由。SQLite 索引不应直接放到不支持可靠锁的网络盘。
- 服务内 Uvicorn 访问日志已对下载查询参数脱敏。网关访问日志及平台工具审计仍应对
  签名 URL 脱敏，不记录完整 token。当前目录缺少
  `utils.audit` 实现，需要内网核实其是否记录工具响应；不要将完整有效链接回传到聊天或工单。

## 错误与定位

| 位置 | 代码/状态 | 含义 |
|---|---|---|
| MCP | DOWNLOAD_DISABLED | 功能尚未启用 |
| MCP | DOWNLOAD_CONFIG_INVALID | 对外地址、密钥或容量/有效期配置错误 |
| MCP | ATTACHMENT_NOT_FOUND / ITEM_OUT_OF_SCOPE | 附件不属于该邮件，或邮件不在授权范围 |
| MCP | ATTACHMENT_NOT_SUPPORTED | 非文件类附件 |
| MCP | ATTACHMENT_TOO_LARGE | 声明大小或实际字节超过上限 |
| MCP | DOWNLOAD_CACHE_FULL | 文件数或容量配额不足，含写入中的预留 |
| GET | 403 | 下载凭证缺失、格式错误或被篡改 |
| GET | 410 | 链接过期，或缓存文件已失效/不可用 |
| GET | 404 | 功能禁用，或没有匹配到下载路由 |
| GET | 503 | 缓存服务暂时不可用，检查服务器配置、磁盘及索引权限 |

现有 `get_attachment(..., save=True)` 的底层 Python 调用仍返回 saved_path，
但该参数从未暴露在 MCP schema 中。远程调用使用新工具，不应使用服务器路径。
文本读取仍保持原 5 MiB / 20,000 字符限制，原文件下载上限可独立配置。

## 内网 AI 联调提示词

```text
请验证已部署的 prepare_attachment_download 工具。只使用获准的测试员工及测试邮件。
不要发送、修改或删除邮件，不改变服务器代码或配置。

1. 从 AI 员工实际执行环境访问宿主机7712的 /healthz，刷新工具列表，确认新工具存在。
2. 使用 get_attachment 列出一封测试邮件的附件，选一个已知内容的 TXT 或 XLSX。
3. 调用 prepare_attachment_download，记录返回字段，隐藏 URL token。
4. 在本次任务目录执行 HTTP GET 下载。记录 HTTP 状态、Content-Disposition、
   文件路径、大小、SHA-256，与工具响应比较。再用解析工具读取一个已知内容。
5. 使用同一有效链接重新 GET，确认哈希一致；修改签名一个字符，确认403。
6. 待 expires_at 之后，原链接应返回410；重新调用准备工具后，新链接可以下载。
7. 用另一封测试邮件的附件ID配上本邮件ID，确认拒绝且未返回链接。
8. 若测试邮箱有超限附件，确认返回 ATTACHMENT_TOO_LARGE，服务继续可用。
9. 报告实际工具调用、HTTP结果及文件校验。不回传真实附件正文、凭据或完整有效链接。
10. 请运维核对：数据目录持久化、GET日志及工具审计token脱敏、过期文件定期清理。

若失败，提供错误码和脱敏后的错误信息，并区分：OA/邮箱权限、工具注册、
链接配置、网络、GET凭证、缓存和AI文件解析问题。未执行项目标为未验证。
```

## 本地验证边界

测试命令：`python -B -m unittest discover -s tests -v`。
本地测试使用固定字节流代替 Exchange 网络，运行真实 FastMCP 工具结果及 SSE
应用的 HTTP 路由。当前项目副本缺少 utils、mirror 和若干 operation 模块，
不代表已验证整个生产 mcp_server 启动，也不替代真实邮箱、网关及审计联调。
