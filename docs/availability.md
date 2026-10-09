# 忙闲查询调用说明

`check_availability` 只查询 EWS Free/Busy 并返回每个邮箱的状态区间，不读取完整日程、不创建或预订会议。调用方 Agent 根据这些区间、用户要求的会议时长和其他条件选择公共时段，再决定后续动作。

本文描述当前源码分支的接口；真实 Exchange、OA 和最终部署仍需内网验收。旧部署或调用方草稿中的 `duration`、`suggested_slot`、`mutually_free_slot_possible` 不能与新接口混用。

## 参数

`arguments` 为扁平对象，仍须包含 `lanid`、`name`。

| 参数 | 约束与含义 |
| --- | --- |
| `start` | 必填，查询起点；时间解析后统一为 `Asia/Shanghai`。 |
| `end` | 必填，查询终点，必须晚于 `start`。 |
| `attendees` | 必填，1–100 个邮箱；去首尾空格、转小写并去重，每个邮箱只查询和返回一次。 |
| `interval_minutes` | 可选，整数 5–1440，默认 30；EWS 忙闲采样间隔，不是期望会议时长。 |

例如查询 09:00–10:30、按 30 分钟采样：

```json
{
  "name": "check_availability",
  "arguments": {
    "lanid": "test.employee",
    "name": "测试员工",
    "start": "2026-10-12T09:00:00+08:00",
    "end": "2026-10-12T10:30:00+08:00",
    "attendees": ["alice@example.com", "bob@example.com"],
    "interval_minutes": 30
  }
}
```

## 返回与解释

业务结果示例：

```json
{
  "window_start": "2026-10-12T09:00:00+08:00",
  "window_end": "2026-10-12T10:30:00+08:00",
  "timezone": "Asia/Shanghai",
  "interval_minutes": 30,
  "attendees": [
    {
      "email": "alice@example.com",
      "status": "busy",
      "intervals": [
        {"start": "2026-10-12T09:00:00+08:00", "end": "2026-10-12T09:30:00+08:00", "status": "free"},
        {"start": "2026-10-12T09:30:00+08:00", "end": "2026-10-12T10:00:00+08:00", "status": "busy"},
        {"start": "2026-10-12T10:00:00+08:00", "end": "2026-10-12T10:30:00+08:00", "status": "free"}
      ]
    },
    {
      "email": "bob@example.com",
      "status": "unknown",
      "intervals": [
        {"start": "2026-10-12T09:00:00+08:00", "end": "2026-10-12T10:00:00+08:00", "status": "free"},
        {"start": "2026-10-12T10:00:00+08:00", "end": "2026-10-12T10:30:00+08:00", "status": "unknown"}
      ]
    }
  ]
}
```

每个 `intervals` 元素表示 `[start, end)` 区间。相邻同状态区间合并，末尾裁到实际 `window_end`；区间不会越过查询窗口。状态保留 EWS 采样含义：

| EWS merged 字符 | 区间 `status` | 含义 |
| --- | --- | --- |
| `0` | `free` | 该采样区间为空闲。 |
| `1` | `tentative` | 暂定。 |
| `2` | `busy` | 忙碌。 |
| `3` | `oof` | 外出／不在办公室。 |
| `4` | `unknown` | NoData，不能视为空闲。 |

一个采样区间包含多种状态时，EWS 返回数字最高的分类，因此忙碌区间并不代表其中每一分钟都忙。接口不补造区间内的事件起止时间；需要更细粒度时可减小 `interval_minutes`。相邻 `free` 区间可用于调用方核对连续空闲时长；最后不足一个采样间隔的区间仍只覆盖实际窗口，不能据此声称满足更长会议时长。

每人的顶层 `status` 是整窗摘要：存在任何 `unknown` 则为 `unknown`；否则存在 `tentative`、`busy` 或 `oof` 则为 `busy`；其余为 `free`。摘要为 `busy` 或 `unknown` 时，仍须查看具体区间，不能把整窗解释为始终忙碌或始终未知。

单人响应缺失或异常、merged 格式非法、长度与窗口及采样间隔不符时，该人整窗返回 `unknown`；响应人数与请求人数不符时，全员整窗返回 `unknown`。有效 merged 中的 NoData 只映射对应区间。整次 EWS 调用失败返回 `AVAILABILITY_UNAVAILABLE`，不伪装为一次成功查询。

## 调用方迁移与内网验收

旧 `duration` 参数已移除，不能直接把“会议时长”改名后继续使用；调用方应分别保留用户要求的会议时长和查询采样间隔。工具不再返回 `suggested_slot`、`mutually_free_slot_possible` 或候选公共时段，由 Agent 对所有相关参会人的区间取交集、核对完整时长并决定安排。

接入时刷新 MCP tools/list 和调用方 schema，同步旧字段依赖；现有调用方 skill 草稿须在确认部署版本后再同步，不能将源码分支合同写成生产已验收行为。历史恢复问题见 [源码恢复记录](source-restoration.md)，其中旧返回字段及缺陷是历史记录。

内网使用获准测试邮箱核对已知空闲、暂定、忙碌、OOF 与 NoData，验证跨邮箱混合状态、重复邮箱去重、最后不足一个采样间隔的窗口和带时区时间。EWS 请求使用上海 `Bias=-480`，并包含完整 `StandardTime`、`DaylightTime` 结构；需核对实际 Exchange 返回时间与 Outlook 一致。此次联调只读取忙闲，不安排会议。

## 官方依据

- [MergedFreeBusy](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/mergedfreebusy)：每个字符代表一个采样区间，分类为 0–4，混合状态使用最高数字。
- [MergedFreeBusyIntervalInMinutes](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/mergedfreebusyintervalinminutes)：默认 30 分钟，最小 5 分钟，最大 1440 分钟。
- [TimeZone (Availability)](https://learn.microsoft.com/en-us/exchange/client-developer/web-service-reference/timezone-availability) 与 [SerializableTimeZone](https://learn.microsoft.com/en-us/openspecs/exchange_server_protocols/ms-oxwavls/42cb0025-b1b4-4994-8ad2-e956fa9d33d2)：请求时区及完整子元素要求。
- [TIME_ZONE_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/timezoneapi/ns-timezoneapi-time_zone_information)：偏移按 `UTC = local time + bias` 计算，UTC+8 对应 `-480` 分钟。

邮箱规范化、区间合并、摘要优先级、异常响应转未知及调用方选时段属于本项目合同；官方文档不代替真实部署验收。
