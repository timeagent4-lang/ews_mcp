# aias_mcp

基于 [ews_mcp](https://github.com/timeagent4-lang/ews_mcp) 2026-09-30 提交改造的 Exchange MCP，使用 Python、FastMCP 和 exchangelib。提供邮件、日历及目标邮箱自定义文件夹的联系人操作。

工具直接执行，移除了 OA 身份查询、二次确认、防重复提交、执行记录和回执查询。服务日志记录请求结果。邮件和草稿删除仅移入 Deleted Items。

## 启动

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
# 填写 .env 后启动
& .\.venv\Scripts\python.exe mcp_server.py
```

客户端连接 Streamable HTTP `http://服务地址:7805/mcp?key=服务密钥`。`EWS_MCP_API_KEY` 必填；`OUTLOOK_SERVER` 为固定 Exchange 地址。`EWS_MCP_SEND_ENABLED=true` 开启发信和会议通知。`/healthz` 返回存活状态与工具数量。

容器参考 `docker-compose.yml`，宿主机端口为 7712。默认 Compose 网络可接入新平台；镜像使用非 root 用户，日志最多保留 3 个 10 MiB 文件。内网 pip 配置可在构建时通过 BuildKit `pip_conf` secret 挂载，密钥不进入镜像。

## 凭据与结果

所有工具参数均为顶层 JSON，无 `params` 包装：

```json
{"mailbox":"user@example.com","username":"DOMAIN\\user","password":"个人密码","folder":"inbox","limit":10}
```

上例用于 `find_message`。`mailbox` 必填；提供个人密码时 `username` 省略默认邮箱。密码不做 trim；全空格也是个人密码。未传密码或传 `null`／`""` 使用服务端 `OUTLOOK_ADMIN_LANID`／`OUTLOOK_ADMIN_PASSWORD` 公邮凭据，以 DELEGATE 访问目标邮箱；此时忽略 username。个人认证失败不会切换公邮。

每次请求创建目标 Account；用户密码只在请求和 SDK 内存连接池中使用，不保存到磁盘、数据库或日志。公邮代理发送时 From 为目标邮箱，允许收件人看到“公邮代用户发送”；权限由 Exchange 检查，拒绝时不会改用公邮自身发件。

成功与失败都返回 `ok/status/results/request_id`，文本内容与 `structuredContent` 一致。`status` 为 `success/failed/partial/unknown`，仅完整成功 `ok=true`；其余 MCP `isError=true`。错误包含 `error_code/message`，有真实 Exchange 原因时带脱敏后的 `exchange_code/exchange_message`。批量结果提供逐项状态和成功／失败／未知计数。

写入提交后的超时、断连或响应解析失败可能返回 `unknown`。服务不自动重发，请先查询邮箱实际结果。联系人资料保存后照片失败会保留已成功的联系人 ID，并返回 `partial` 或 `unknown`。

## 29 个工具

| 类别 | 工具 |
|---|---|
| 邮件读取（8） | `list_folders`、`get_mailbox_overview`、`find_message`、`get_message`、`get_thread`、`get_attachment`、`prepare_attachment_download`、`list_flagged_messages` |
| 邮件写入（8） | `create_draft`、`update_draft`、`send_draft`、`delete_draft`、`update_messages`、`move_messages`、`delete_messages`、`set_message_flag` |
| 联系人（6） | `list_contact_folders`、`list_contacts`、`find_people`、`get_contact`、`create_contact`、`update_contact` |
| 日历（6） | `list_events`、`get_event`、`create_event`、`update_event`、`respond_to_event`、`cancel_event` |
| 状态（1） | `get_server_status` |

`create_draft` 支持 new/reply/reply_all/forward；保存后用 `send_draft` 发送。日历保留组织者／参与者和重复主项限制。空闲时间、自动回复、任务、本地镜像均未注册。完整参数约束由 `tool_specs.py` 的公开 schema 定义，可通过 MCP `tools/list` 获取。

## 联系人

先用 `list_contact_folders` 获取已有文件夹 ID；支持嵌套和同名文件夹，展示路径，后续按 ID 操作。自定义文件夹会用轻量父 ID 查询核对目标邮箱归属。工具不创建文件夹。`folder_id` 省略使用默认 Contacts，仅查询所选文件夹；`find_people` 未指定文件夹可选 auto/contacts/gal，指定文件夹只查其中联系人。GAL 只读。

Exchange 2016 或更新版本使用一次轻量 `personmetadata` 查询取得系统文件夹实际 ID，再按 ID 排除；同名自定义文件夹可用，重命名的系统文件夹仍会被排除。该查询的权限、网络及其他错误原样进入现有错误处理，只有明确的目录不存在错误才按缺失处理。默认 Contacts 不增加该查询。

旧版 Exchange 不支持这个系统标识，因此维持对无法可靠分类目录的限制：发现结果中的 `unclassified_folders` 会说明原因，按 ID 使用这些目录返回 `CONTACT_FOLDER_CLASSIFICATION_UNSUPPORTED`；其他普通自定义目录和默认 Contacts 仍可使用。[微软版本行为说明](https://learn.microsoft.com/en-us/openspecs/exchange_server_protocols/ms-oxwscdata/e5ec9563-0491-486d-9bec-50585fa2a2c6)

GAL 详情和搜索在没有 Body 备注时补充返回目录只读 Notes；私人联系人新增和编辑的 `notes` 继续存入 Body。

`create_contact`／`update_contact` 使用 `contact` 对象。新增必填 `display_name`，其余未传为空；编辑未传保留，明确空值才清空，递归应用于标签和地址子字段。文本和日期用 `null`／`""` 清空，列表用 `[]`，索引对象用 `{}`／`null`；display_name 不能清空。

| 资料 | 字段 |
|---|---|
| 姓名 | display_name、given_name、middle_name、surname、initials、nickname、generation、file_as、file_as_mapping |
| 组织 | company_name、companies、department、job_title、office、manager、assistant_name、profession、business_homepage、mileage |
| 家庭与日期 | spouse_name、children、birthday、wedding_anniversary（日期 YYYY-MM-DD） |
| 联系方式 | email_addresses（EmailAddress1–3）、phone_numbers（官方 19 标签）、im_addresses（ImAddress1–3） |
| 地址 | physical_addresses（Business/Home/Other；street/city/state/country/zipcode）、postal_address_index |
| 其他 | categories、notes、photo |

创建示例：

```json
{
  "mailbox":"user@example.com",
  "folder_id":"已有文件夹ID",
  "contact":{
    "display_name":"张三",
    "email_addresses":{"EmailAddress1":"zhang@example.com"},
    "phone_numbers":{"MobilePhone":"13800000000","BusinessPhone":"010-12345678"},
    "physical_addresses":{"Business":{"street":"示例路1号","city":"北京","zipcode":"100000"}},
    "company_name":"示例公司",
    "notes":"沟通备注"
  }
}
```

只改手机号并清除工作地址中的邮编：

```json
{"mailbox":"user@example.com","folder_id":"已有文件夹ID","contact_id":"联系人ID","contact":{"phone_numbers":{"MobilePhone":"13900000000"},"physical_addresses":{"Business":{"zipcode":""}}}}
```

备注映射为纯文本正文；照片参数为 `{"filename":"photo.jpg","content_type":"image/jpeg","content_base64":"..."}`，以照片附件处理。`photo=null` 清除照片并保留其他附件，空字符串／空对象无效。查询返回照片元数据。`postal_address_index` 清空会设为 Exchange 的 `"None"`；系统只读字段和自定义 MAPI 字段不作为可编辑资料。

## 日志与附件下载

每次工具请求都有同一个临时请求号，开始和结束各一条单行 JSON。记录工具、目标邮箱、凭据模式、对象 ID、修改字段名、结果状态、耗时和错误码；失败可附安全诊断位置。日志不记录密码、账号、API key、完整参数、正文、联系人值或原搜索文本。请求号仅关联服务日志。

原附件下载是可选能力，继续使用临时文件、签名链接和下载元数据索引，见 [附件下载](docs/attachment-downloads.md)。这份索引不保存工具执行记录或用户密码。

## 验证

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试包含真实 FastMCP／MCP 入口、HTTP 鉴权、附件下载和真实 SDK 模型，Exchange 网络边界使用模拟。实际 Exchange 权限、自定义文件夹现场发现、代理发件显示和新平台调用仍需使用部署环境联调；离线测试不代表现场验收。

设计依据及验收范围见 [改造设计](docs/superpowers/specs/2026-10-07-aias-mcp-design.md)。
