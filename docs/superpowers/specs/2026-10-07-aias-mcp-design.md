# aias_mcp 改造设计

日期：2026-10-07。基于 `timeagent4-lang/ews_mcp` 的 `c4a2bd70c2cc9412ff46a9f04a1b24e7c9667dcd`，本地项目为 `D:\code\aias_mcp`。

## 目标与范围

在现有 Python / FastMCP / exchangelib 结构内直接改造，优先复用邮件与日历业务逻辑。相比重新搭建服务，这样更便于保留原有邮件格式、作用域和 Exchange 行为；只增加一层接口适配则无法去掉旧确认与持久化依赖，因此不采用。

- 保留邮件查询、读取、附件读取和下载、草稿、发送、回复、转发、分类、已读状态、移动，以及现有日历功能。
- 开放邮件和草稿删除，统一移到 Deleted Items，不提供永久删除。
- 重点扩展目标用户邮箱内的联系人：发现已有文件夹、列表、搜索、详情、新建和编辑。不新增联系人删除或文件夹创建工具。
- 不加入空闲时间查询、自动回复、任务、本地镜像；清除这些模块的导入、继承、初始化与公开工具。
- 移除 OA 身份校验、共享目录的 OA 授权、二次确认、预览令牌、防重复提交、执行数据库和回执查询。

## 接入与凭据

沿用 Streamable HTTP `/mcp?key=...` 和现有服务 API key 校验。保留固定的 Exchange 服务器配置，不开放用户指定 EWS 服务器地址。所有工具的公开参数均包含必填 `mailbox`、可选 `username` 和 `password`，不使用旧的 `arguments.params` 嵌套结构。

- 提供非空密码：使用个人凭据；`username` 省略时默认使用 `mailbox`，允许显式传入 Exchange 所需登录名。
- 未提供密码，或显式传 `null`/空字符串：使用服务端配置的公邮凭据，以 DELEGATE 方式操作 `mailbox`；此模式忽略 `username`。全空格密码仍是提供了密码，原样用于个人认证。
- 个人登录失败明确返回认证错误，不自动切换到公邮。
- MCP 信任调用者传入的目标邮箱，由 Exchange 执行权限检查；不增加新的用户归属校验。
- 用户密码不落盘、不入库、不进日志，不新增凭据保存接口。每次调用创建独立的目标邮箱 Account，连接库的内存池按凭据隔离。
- 凭据字段只参与连接初始化，不传入邮件、联系人等业务函数。密码不做字符串 trim。

公邮代理发信时，From 使用目标用户邮箱，允许收件人看到“公邮代用户发送”。具体 Sender 由 Exchange 权限决定；缺少发送权限时返回实际错误，不改为从公邮自身邮箱发送。保留草稿创建与发送两个业务步骤，工具不再要求用户确认。保留服务端部署发送开关，实际提供发送能力的部署设置 `EWS_MCP_SEND_ENABLED=true`。

## 联系人文件夹与字段

以绑定目标邮箱的新鲜 Root 和 MsgFolderRoot 调用 `FolderCollection.find_folders(depth="Deep")`，分页读取名称、类型、ID、父 ID 等轻量元数据。用这些元数据自行拼路径，避免访问会触发全树缓存的 `walk/parent/absolute`。筛选普通 `IPF.Contact` 文件夹，排除明确的系统联系人缓存或元数据目录。

复审补充：SDK 会根据名称猜测 PersonMetadata 类，不能据此排除同名自定义目录。Exchange 2016+ 每次发现或自定义目录访问用一次绑定目标邮箱的 `personmetadata` distinguished 查询获取实际 ID，按 ID 排除系统目录；默认 Contacts 无额外查询。仅 FolderNotFound/ItemNotFound 代表明确缺失，其他错误保留，空响应不等于缺失。旧版不发送不支持的查询，普通目录保持可用；原来无法可靠分类的歧义目录维持限制，发现结果明确列为 `unclassified_folders`，访问返回分类不支持错误，不冒充可靠识别。

新增 `list_contact_folders` 和 `list_contacts`，扩展现有 `find_people/get_contact/create_contact`，新增 `update_contact`。使用 `folder_id` 选择目标；省略时使用标准 Contacts。文件夹名称和路径用于展示，后续读写按 ID 定位。联系人搜索保留现有 GAL 读取能力；指定 `folder_id` 时只搜索该联系人文件夹，不混入 GAL；未指定时保留 `source=auto/contacts/gal` 的来源选择。GAL 不提供编辑操作。

按 ID 定位使用直接 GetFolder；读取或编辑联系人时检查 Contact 类型及其父文件夹。默认只查询指定文件夹中的联系人，不隐式扫描其子文件夹。

实施时核对官方 SOAP 后补充：原生 FolderId 自带邮箱上下文，新 Root 绑定只影响 SDK 对象，不能证明其属于传入 mailbox。自定义文件夹通过直接 GetFolder 的轻量父 ID 链，确认最终到达目标 MsgFolderRoot；不引入 OA 或调用者身份校验，不读取树缓存。

联系人资料统一放在 `contact` 对象中，公共凭据与 `folder_id/contact_id` 仍为工具顶层参数。支持以下标准可编辑资料：

| 类别 | 字段 |
|---|---|
| 姓名与显示 | `display_name/given_name/middle_name/surname/initials/nickname/generation/file_as/file_as_mapping` |
| 组织与工作 | `company_name/companies/department/job_title/office/manager/assistant_name/profession/business_homepage/mileage` |
| 家庭与日期 | `spouse_name/children/birthday/wedding_anniversary` |
| 邮箱 | `email_addresses`：`EmailAddress1/EmailAddress2/EmailAddress3` |
| 电话 | `phone_numbers`：官方全部 19 种标签，包含手机号、商务/家庭电话、传真等 |
| 地址 | `physical_addresses`：`Business/Home/Other`，每项支持 `street/city/state/country/zipcode`；另有 `postal_address_index` |
| 即时通讯 | `im_addresses`：`ImAddress1/ImAddress2/ImAddress3` |
| 其他资料 | `categories/notes/photo` |

新建时，未提供的选填资料为空；编辑时，未传字段保持原值，明确传空才清空。该规则递归应用于邮箱和电话的标签、地址类别及地址子字段。文本允许 `""` 或 `null` 清空，日期允许 `null` 或 `""` 清空，列表用 `[]` 清空；索引对象传 `{}` 或 `null` 清空整个对象，单个标签传空只清空该项。

显示名称新建时必填，编辑时可省略但不能清空。生日与纪念日使用 `YYYY-MM-DD`，不填不生成日期。`file_as_mapping` 传空时清除该选填字段；`postal_address_index` 清空为字符串 `"None"`，不能用删除字段的方式清空。非空枚举值必须属于官方允许值。Exchange 必须保留的内部状态属性使用其规定默认值，不作为联系人资料统一置空。

公开 `notes` 使用纯文本，映射到联系人实际 `body`。`photo` 使用文件名、媒体类型与 base64 内容，通过 `FileAttachment(is_contact_photo=True)` 添加或替换；`photo=null` 删除现有联系人照片，保留其他附件，`""/{}` 不是合法照片参数。系统 ID、ChangeKey、时间戳、来源、拼音等只读属性仅在查询时返回。非原生的自定义 MAPI 属性不在本次标准字段范围内。

GAL 读取补充：ResolveNames 可在只读 Notes 字段返回目录备注，仅在 Body 缺失时回落读取；Body 空字符串保留空。私人联系人备注写入规则不变。

联系人保存和照片附件处理属于多个 EWS 写入步骤。联系人已保存、照片明确失败时返回 `partial` 及已创建/更新的联系人 ID；后续附件提交结果不明时返回 `unknown`，同时保留已确定成功部分，不自动重做整个操作。

编辑保留 EWS ChangeKey 冲突检查，不增加版本管理系统；字段名、标签、日期和类型均有 schema 校验。修改一项不覆盖未传的其他资料。

## 返回结果与错误

所有业务工具统一返回 `ok/status/results`，状态为 `success/failed/partial/unknown`；`ok=true` 仅表示完整成功。错误包含业务 `error_code`、可读 `message` 和临时 `request_id`；存在真实 Exchange 原因时保留 `exchange_code/exchange_message`，说明经过脱敏。文本结果与结构化结果表达相同状态和必要错误信息，失败、部分失败及未知结果正确设置 MCP `isError=true`。

- 分开参数、配置、认证、网络、Exchange ResponseCode 和程序异常，不用异常名称后缀猜类型。
- 批量操作保留逐项结果、错误和成功/失败/未知数量。联系人多来源查询保留逐来源错误，全部来源失败不能返回“成功、空结果”。
- 区分提交前失败和实际写入提交后的超时、断连或响应解析失败；后者返回结果未知，不自动重发。发送、联系人新建、草稿保存、修改和删除均遵循该规则。
- 保留真实 Exchange 错误及包装异常的 cause；程序异常对客户端返回内部错误，安全诊断信息进入服务日志。
- 清理“原操作号查回执”“重新预览”“两阶段确认”等旧文案。请求号仅用于日志关联，不对应可查询的执行记录。
- HTTP 鉴权及 MCP 协议层错误继续遵循现有协议，不伪装成业务工具成功结果。

## 服务日志

改造现有审计中间件和 fallback，不引入日志数据库或独立日志系统。每次工具请求生成同一个临时请求号，记录开始与结束两条单行 JSON；fallback 不重复记录同一阶段。

记录工具、目标邮箱、个人/公邮代理模式、白名单对象 ID、修改字段名、状态、耗时、业务及 Exchange 错误码、批量数量。覆盖参数拒绝和未知工具调用，业务错误日志使用相同关联号。正常返回不打印全部结果。

不记录密码、登录账号、API key、完整参数、邮件正文、联系人资料值、搜索原文或带凭据的下载链接。错误输出不直接打印未清洗的异常文本及异常链；程序诊断可保留异常类型、文件位置和调用栈位置。应用日志由现有服务采集通道收集，参考容器配置补容量轮转；保留时长由部署环境管理。

附件下载仍使用临时文件、签名链接及既有生命周期管理；移除执行数据库不等于删除附件临时目录。

## 验收

1. 服务可启动并列出已保留工具；不依赖四类缺失模块、OA 或执行数据库；公开 schema 与实际参数一致。
2. 使用真实 MCP 入口验证成功、参数错误、认证失败、权限拒绝、版本冲突、部分失败、全部失败和提交后结果未知；不只依靠替换真实审计模块的旧测试。
3. 验证请求号贯穿返回与日志、状态与数量准确、fallback 不重复输出、密码及 API key 不进入日志或错误响应。
4. 验证默认/自定义/嵌套/同名联系人文件夹发现和定位、多项字段及编辑保留/清空规则、备注和照片映射。
5. 验证个人凭据与公邮代理、不同目标邮箱并发隔离；邮件发送保存到目标 Sent，删除只移到 Deleted Items。
6. Exchange 实际权限、代理发件显示及新平台工具调用完成联调后才报告现场验证通过。离线验证不能替代联调。

## 已核对的官方依据

- [exchangelib 5.6.0 FolderCollection / FindFolder](https://github.com/ecederstrand/exchangelib/blob/v5.6.0/exchangelib/folders/collections.py)
- [微软文件夹层级与 MsgFolderRoot](https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/how-to-work-with-folders-by-using-ews-in-exchange)
- [微软 ContactSchema](https://github.com/OfficeDev/ews-managed-api/blob/master/Core/ServiceObjects/Schemas/ContactSchema.cs)
- [exchangelib Contact / 备注和照片约束](https://github.com/ecederstrand/exchangelib/blob/v5.6.0/exchangelib/items/contact.py)
- [exchangelib 索引字段标签](https://github.com/ecederstrand/exchangelib/blob/v5.6.0/exchangelib/indexed_properties.py)
- [exchangelib 错误类型](https://github.com/ecederstrand/exchangelib/blob/v5.6.0/exchangelib/errors.py)
- [FastMCP 2.14.7 ToolResult](https://github.com/jlowin/fastmcp/blob/v2.14.7/src/fastmcp/tools/tool.py)
- [微软 Send As / Send on Behalf](https://learn.microsoft.com/en-us/exchange/recipients/mailbox-permissions)
- [微软显式和隐式代理访问／原生 ID 的邮箱上下文](https://learn.microsoft.com/en-us/exchange/client-developer/exchange-web-services/delegate-access-and-ews-in-exchange)
