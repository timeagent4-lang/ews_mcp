# 任务列表调用说明

`list_tasks` 实时读取当前员工授权的 Tasks 文件夹，默认只返回未完成任务。先在 EWS 查询中筛选，再按到期日降序排列和分页，避免先截取 50 条再移除已完成项导致漏查后续未完成任务。它不包含邮件旗标、不读取本地镜像，也不执行写入；`create_task`、`update_task` 的行为不变。

本文描述当前源码分支的修复，真实 OA、Exchange 和最终部署仍需内网验收；不能沿用历史记录认定生产已支持这些分页语义。

## 参数

`arguments` 为扁平对象，必须包含 `lanid`、`name`。

| 参数 | 默认值 | 约束与含义 |
| --- | --- | --- |
| `folder` | `"tasks"` | 只接受 `"tasks"`。 |
| `incomplete_only` | `true` | 布尔值；`true` 只查询未完成任务，`false` 查询所有任务。 |
| `limit` | `50` | 整数 1–100，本页最多返回条数。 |
| `offset` | `0` | 非负整数，跳过筛选后结果中的条数。 |

未完成任务包括 `NotStarted`、`InProgress`、`WaitingOnOthers`、`Deferred`；不是只查询 `NotStarted`。整数参数不接受布尔值或字符串。直接 Python 调用签名为 `list_tasks(folder="tasks", incomplete_only=True, limit=50, offset=0)`，原第三个位置参数仍是 `limit`。

## 第一页与下一页

例如每页读取 2 条未完成任务，以下身份和 ID 均为占位示例：

```json
{
  "name": "list_tasks",
  "arguments": {
    "lanid": "example_lanid",
    "name": "示例姓名",
    "folder": "tasks",
    "incomplete_only": true,
    "limit": 2,
    "offset": 0
  }
}
```

MCP 结构化结果示例：

```json
{
  "results": [
    {"id": "task-a", "changekey": "key-a", "subject": "核对合同", "complete": false, "status": "InProgress", "start_date": "2026-10-09", "due_date": "2026-10-14", "percent_complete": 50},
    {"id": "task-b", "changekey": "key-b", "subject": "准备材料", "complete": false, "status": "NotStarted", "start_date": "2026-10-09", "due_date": "2026-10-13", "percent_complete": 0}
  ],
  "incomplete_only": true,
  "offset": 0,
  "limit": 2,
  "has_more": true,
  "next_offset": 2
}
```

保持筛选条件和 `limit`，将返回的 `next_offset` 作为下一次 `offset`：

```json
{
  "name": "list_tasks",
  "arguments": {
    "lanid": "example_lanid",
    "name": "示例姓名",
    "folder": "tasks",
    "incomplete_only": true,
    "limit": 2,
    "offset": 2
  }
}
```

若只剩一条任务，返回：

```json
{
  "results": [
    {"id": "task-c", "changekey": "key-c", "subject": "等待反馈", "complete": false, "status": "WaitingOnOthers", "start_date": "2026-10-09", "due_date": "2026-10-12", "percent_complete": 0}
  ],
  "incomplete_only": true,
  "offset": 2,
  "limit": 2,
  "has_more": false,
  "next_offset": null
}
```

返回元数据与 `results` 同层。直接调用业务方法时列表字段叫 `items`；MCP 包装器将 `items` 转为 `results`，其他字段保持不变。

## 空结果与结束条件

筛选后没有任务或 `offset` 已超出结果范围时，成功返回空列表，仍保留请求的筛选条件及分页参数。例如默认查询没有未完成任务：

```json
{
  "results": [],
  "incomplete_only": true,
  "offset": 0,
  "limit": 50,
  "has_more": false,
  "next_offset": null
}
```

默认查询为空仅表示此次查询没有未完成任务，不代表 Tasks 文件夹没有已完成任务；需要全部任务时显式传 `incomplete_only=false`。接口多取一条用于判断后续是否有结果，因此末页即使恰好返回 `limit` 条，也会返回 `has_more=false`、`next_offset=null`。调用方按 `next_offset` 继续，值为 `null` 时结束，不必再猜测偏移或额外查询空页。

## 内网验收与依据

使用获准测试邮箱中的现有任务只读验收：超过 50 条且混有已完成任务时，默认分页仍覆盖后续未完成项；核对不同未完成状态、`incomplete_only=false`、多页、空结果、100 条上限以及恰好满末页的结束元数据。分页偏移按筛选后的结果计算，不能使用未筛选 Tasks 列表的序号。同步调用方 schema 后再确认部署效果，不为此次验收创建或更新任务。

微软 [IsComplete](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/iscomplete) 定义任务是否完成的布尔属性；[IndexedPageItemView](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/indexedpageitemview) 定义按查找结果集的偏移分页。当前 exchangelib 5.6.0 的 `Task.status` 不支持搜索，代码使用可搜索的 `is_complete=False`，对应 EWS `task:IsComplete`，将筛选放到分页之前。`has_more`、`next_offset` 的多取一条计算和 100 条上限属于本项目合同，不是所有 EWS 列表接口共有的返回语义。
