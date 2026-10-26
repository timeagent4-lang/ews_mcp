# 附件上传与草稿附加

当前版本公开30个工具。所有 MCP `arguments` 为扁平对象，`lanid` 必填，`name` 可选且忽略。每次通过 OA 查询唯一有效邮箱，沿用固定账号 DELEGATE。上传默认关闭，不由发送或下载开关代替。

## 调用流程

1. Agent 在自己的执行环境生成文件，计算精确字节数与 SHA-256。
2. 调用 `prepare_attachment_upload`，获得临时 PUT 地址与凭据，不连接 Exchange。
3. 执行器 PUT 原始字节；核对HTTP成功响应的ready、大小和摘要。
4. 调用 `attach_draft_attachment` 预览；确认文件、目标草稿与现有附件清单后，保持原业务参数并加 `operation_id`、`confirm_token` 确认。
5. 若是inline图片，用成功回执中的实际 `content_id` 更新HTML。最后重新调用 `send_draft` 预览和确认。

仅支持普通 Message 草稿，目标必须在当前员工 Drafts；不改原正文、收件人或旧附件。普通附件透传原字节；inline仅接受完整单帧PNG/JPEG，最大1600万像素、单边8192，实际解码验证并保留原始字节。SVG、动画PNG和损坏图片不能作为inline。

准备上传的 MCP 调用：

```json
{
  "name": "prepare_attachment_upload",
  "arguments": {
    "lanid": "employee_lanid",
    "filename": "report.pdf",
    "size": 12345,
    "sha256": "替换为执行器计算的64位十六进制SHA256",
    "content_type": "application/pdf"
  }
}
```

`structuredContent.results` 和文本JSON返回 `upload_id`、`upload_url`、`upload_token`、`upload_expires_at`、文件元数据及 `limits`。路径部分会从filename移除，服务端磁盘文件名由随机ID生成。临时token只交执行器使用，不展示给用户、写邮件或工单；token不在URL、普通操作回执和审计日志中。HTTP阶段凭据是针对指定上传的短期bearer，不声称绑定持有者的网络身份；附加仍重新校验OA LANID和邮箱归属。

执行器 curl 示例（变量从本次准备响应取得）：

```sh
curl --fail-with-body --request PUT \
  --header "Authorization: Bearer $UPLOAD_TOKEN" \
  --upload-file "$ATTACHMENT_FILE" "$UPLOAD_URL"
```

Python标准库流式示例，不依赖requests，不打印token或文件内容：

```python
import hashlib
import http.client
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

path = Path(os.environ["ATTACHMENT_FILE"])
digest = hashlib.sha256()
with path.open("rb") as source:
    for chunk in iter(lambda: source.read(65536), b""):
        digest.update(chunk)
# 把 path.name、path.stat().st_size、digest.hexdigest() 用于申请上传。
# 申请后如文件变化，请重新计算并重新申请。
url = urlsplit(os.environ["UPLOAD_URL"])
client_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
connection = client_type(url.hostname, url.port, timeout=120)
try:
    with path.open("rb") as source:
        connection.request("PUT", url.path, body=source, headers={
            "Authorization": "Bearer " + os.environ["UPLOAD_TOKEN"],
            "Content-Length": str(path.stat().st_size),
            "Content-Type": "application/octet-stream",
        })
    response = connection.getresponse()
    payload = json.loads(response.read())
    if response.status != 200:
        raise RuntimeError(f"上传HTTP {response.status}: {payload.get('error_code')}")
    if payload["sha256"] != digest.hexdigest() or payload["size"] != path.stat().st_size:
        raise RuntimeError("上传摘要或大小不符")
finally:
    connection.close()
```

上传成功后预览：

```json
{
  "name": "attach_draft_attachment",
  "arguments": {
    "lanid": "employee_lanid",
    "draft_id": "当前员工已有草稿ID",
    "upload_id": "本次返回的32位上传ID",
    "is_inline": false
  }
}
```

确认时复制原业务参数，仅添加返回的操作号和确认令牌。成功回执包含 `draft_id`、`attachment_id`、最新已知 `changekey`、filename、size、content_type、sha256、is_inline、content_id；没有文件字节或服务器路径。inline成功后才使用 `<img src="cid:实际content_id">`，不要猜CID或把预览当作已附加成功。

查询回执只传 `lanid` 与原 `operation_id`，不带草稿ID、upload_id或confirm_token。结果为unknown时禁止自动重新附加或发送；回执不依赖暂存文件仍存在。send_draft仍最多等待30秒，processing表示后台可能继续，详见[发送等待](send-response-wait.md)。

## 上传与幂等

- 要求唯一Authorization和精确Content-Length。流式统计实际字节和摘要；不把正文/Base64传入MCP。
- 同一上传只接收一次；ready或正在接收时重复PUT固定返回409，绝不覆盖。若成功响应丢失，可用原upload_id申请附加预览确认ready；这不会写Exchange。
- 内容超限、长度/摘要不符、中断或超时均不发布ready，重新prepare取得新上传。
- 一上传同时只允许一个有效附加预览。首次确认后永久绑定原操作；明确未写入失败仅原操作、原截止时间内可重试。过期需重新prepare，不能换操作号绕过消费绑定。
- 同幂等键必须保持同一文件/草稿/inline参数，跨工具或邮箱不能复用。确认比较封存文件和草稿版本，不能仅靠upload_id或幂等键判断内容未变。
- 草稿附加、编辑、发送按真实草稿ID协调。本服务观察到预览后正文、收件人、附件或版本变化会拒绝旧确认；该机制不能冻结Outlook外部编辑的整个竞争窗口。旧版本缺少安全快照的未执行草稿预览要求重新预览。

## 配置与容量

```env
EWS_MCP_UPLOAD_ENABLED=true
EWS_MCP_PUBLIC_BASE_URL=http://实际宿主机内网地址:7712
EWS_MCP_UPLOAD_MAX_BYTES=10485760
EWS_MCP_UPLOAD_CACHE_MAX_BYTES=1073741824
EWS_MCP_UPLOAD_CACHE_MAX_FILES=1000
EWS_MCP_UPLOAD_TTL_SECONDS=600
EWS_MCP_UPLOAD_RETENTION_SECONDS=86400
EWS_MCP_UPLOAD_CLEANUP_SECONDS=300
EWS_MCP_UPLOAD_DRAFT_MAX_BYTES=20971520
EWS_MCP_UPLOAD_DRAFT_MAX_ATTACHMENTS=20
EWS_MCP_PREVIEW_TTL_SECONDS=1800
```

使用现有端口映射7712:7805；代理需转发PUT和`/uploads/`，若有前缀则放入PUBLIC_BASE_URL。核实代理的请求体及超时限制。上传无需下载secret或MCP主密钥；发送开关关闭仍可附加草稿。

单文件默认10MiB，草稿总附件20MiB/20个（含旧附件）；已有附件大小不明时拒绝新增。总缓存默认1GiB/1000个，零字节文件也占一个名额。包括prepared预留、receiving、ready、有效引用、quarantine以及删除失败文件。

接收最多4个并发，总时限120秒，无进展30秒；SDK附加最多2个并发。SDK会将文件字节编码进EWS请求，不能把HTTP流式落盘理解为全程恒定内存。上述限额不等于Exchange/网关的已验收上限。

存储操作在线程中执行，发布ready前再次检查单调时钟截止时间。超时或取消后先等待当前不可中断的文件/SQLite操作结束，再作废和清理，避免后台继续发布文件；因此磁盘阻塞可能延迟HTTP错误返回或服务退出，但不会占住事件循环。退出时同样等待正在执行的清理线程完成。

## 持久化、清理和恢复

文件仅在独立uploads目录；operations.db新增attachment_uploads、operation_upload_refs、draft_claims和短期upload_readers表，只保存元数据和句柄，不存字节。保留旧回执、下载数据库和下载目录。

预览与引用、确认与执行占用、最终回执与资源释放分别在同一SQLite事务提交。正在接收/读取、有效预览、执行和unknown隔离保护的文件不能被清理。删除先标deleting、物理删除成功才释放配额。默认未引用ready保留24小时；有效预览可延长保护，不能只按24小时删除。上传启用需要正预览TTL。

重启仍将活跃业务操作转unknown；只有预览、未执行的引用可释放，中断receiving作废，已有有效prepared/ready可保留。执行中或可能已提交的操作保留草稿隔离与核查线索；不会自动重附加或发送。禁用上传后，服务仍清理已有过期上传缓存。只支持单实例及可靠本地文件系统，不能直接在共享网络盘或多实例上复制此部署。

### unknown的受控维护

先查询原回执。若仍executing且原进程已经终止，走正常启动恢复后再查询；维护对象必须已为unknown。停止本实例，备份数据库及文件卷；在获准邮箱核查真实附件ID/CID、大小/摘要或实际已发送结果。暂时未找到不能证明未提交，证据不足保持隔离。

有充分证据后，用一次本地维护事务执行以下步骤；不在服务运行时编辑数据库，不直接删除claim，不重放EWS请求：

1. `BEGIN IMMEDIATE`，读取原operation与对应mailbox/canonical_draft_id的claim，检查unknown、工具和owner均匹配且唯一。
2. 将原operation记为已核实completed或确定未提交failed；result_json保存原实际回执或安全错误码及核查证据摘要。不得记录文件正文或凭据。
3. 终结该操作的上传：成功为attached；确定未提交为failed并作废upload；将retained_until设为当前时间，结束对应execution/quarantine引用。
4. 若记failed，同时把原confirm_token替换为新的随机值并不再签发，撤销旧确认能力，避免维护后原确认继续重试。
5. 按原operation_id和owner条件解除对应draft claim，核对所有受影响行数；不匹配全部ROLLBACK，匹配才COMMIT。恢复服务后仅查询原回执；新动作需新上传和新预览。

该流程是获准管理员的离线数据维护，不新增MCP管理工具，也不允许定时自动解锁unknown。

## 错误与验收

| 位置 | 错误/状态 | 处理 |
| --- | --- | --- |
| MCP | UPLOAD_DISABLED / UPLOAD_CONFIG_INVALID | 核对独立开关、地址、TTL及限额 |
| MCP | UPLOAD_CACHE_FULL / UPLOAD_BUSY | 配额或SDK容量不足；不新建重复业务动作 |
| PUT | 403 / 410 | 凭据无效或过期，重新prepare |
| PUT | 411 / 400 / 413 / 422 | 缺长度、长度不符、超限、摘要不符；重新prepare |
| PUT | 408 / 429 / 503 | 接收超时、接收容量或存储不可用 |
| PUT | 409 | 正在接收或已经封存；不可覆盖 |
| MCP | DRAFT_CHANGED / PREVIEW_VERSION_REQUIRED | 重新读取并预览，不复用旧确认 |
| MCP | DRAFT_BUSY | 同草稿执行中或unknown隔离，查询原操作 |
| MCP | ATTACHMENT_FAILED_OR_UNKNOWN / ATTACHMENT_RECEIPT_INCOMPLETE / OPERATION_UNKNOWN | 可能已提交，禁止自动附加或发送 |

内网验收使用获准测试对象：从Agent真实执行环境上传一个PDF/表格和一个logo；附加后下载取回比对原字节与SHA-256；新建/回复/全部回复/转发分别检查原文和旧inline保留；最终重新预览发送，验证收件人及已发送副本的普通附件与CID显示。另验收PUT代理限制、过期、错误owner、并发确认和日志脱敏。离线SDK/XML/MCP测试通过不能代替这些结果。
