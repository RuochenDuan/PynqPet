# LLM 本地控制 Command 设计

## 1. 设计目标

本文档用于定义大模型控制本地硬件和本地功能时使用的 command 与 args 格式。

当前只考虑 LLM 调用本地功能，不考虑需要上传给服务端或大模型的内容。

## 2. 统一消息格式

LLM 下发本地控制指令时，统一使用以下结构：

```json
{
  "command": "command_name",
  "args": {}
}
```

字段说明：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `command` | string | 需要执行的本地功能命令 |
| `args` | object | 命令参数，不同 command 对应不同参数 |

## 3. 当前 Command 总览

| 功能 | command | 说明 |
| --- | --- | --- |
| 切换界面 | `ui.switch_screen` | 切换到指定本地界面 |
| OLED 显示内容 | `oled.display` | 在 OLED 上显示文字、表情、提示语、LLM 回复或互动反馈 |
| 拍摄图片 | `camera.capture` | 调用本地摄像头完成拍照动作 |
| 播报当前时间 | `time.speak_current` | 读取本地 RTC 时间并进行语音播报 |
| 播报当前温湿度环境 | `environment.speak_current` | 读取当前温湿度并播报环境信息 |
| 设置 Todo | `todo.manage` | 创建、查询、修改、完成、删除待办事项 |
| 改变宠物状态 | `pet.update_status` | 以增量方式修改宠物状态 |

当前 command 列表：

```text
ui.switch_screen
oled.display
camera.capture
time.speak_current
environment.speak_current
todo.manage
pet.update_status
```

## 4. Command 详细设计

## 4.1 切换界面

command：

```text
ui.switch_screen
```

用途：

- 切换本地 OLED 界面。
- 可用于进入主页、对话页、拍照页、Todo 页、宠物状态页等。

args：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `screen_id` | string | 是 | 目标界面 ID |
| `reason` | string | 否 | 切换原因，便于本地记录或调试 |

示例：

```json
{
  "command": "ui.switch_screen",
  "args": {
    "screen_id": "home_screen",
    "reason": "返回主界面"
  }
}
```

建议界面 ID：

```text
home_screen
main_menu_screen
pet_status_screen
interaction_feedback_screen
voice_interaction_screen
todo_list_screen
todo_detail_screen
todo_confirm_screen
reminder_popup_screen
environment_screen
camera_capture_screen
settings_screen
error_status_screen
```

## 4.2 OLED 显示内容

command：

```text
oled.display
```

用途：

- 在 OLED 上显示文字、表情、提示语、LLM 回复或互动反馈。

args：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `content_type` | string | 是 | 显示内容类型 |
| `text` | string | 否 | 需要显示的文字 |

`content_type` 可选值建议：

```text
text
```

示例：

```json
{
  "command": "oled.display",
  "args": {
    "content_type": "text",
    "text": "主人，今天也要加油！"
  }
}
```

## 4.3 拍摄图片

command：

```text
camera.capture
```

用途：

- 调用本地摄像头完成拍照动作。

args：无

示例：

```json
{
  "command": "camera.capture",
  "args": {}
}
```

## 4.4 播报当前时间

command：

```text
time.speak_current
```

用途：

- 读取本地 RTC 当前时间。
- 将当前时间转换成适合播报的中文文本。
- 调用本地 TTS 模块播报当前时间。

args：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `show_on_oled` | bool | 否 | 是否同时在 OLED 上显示当前时间 |

示例：

```json
{
  "command": "time.speak_current",
  "args": {
    "show_on_oled": true
  }
}
```

说明：

- LLM 不需要提供具体时间，当前时间由本地 RTC 读取。
- 本地逻辑负责生成播报文本，例如“现在是上午九点三十分”。
- 如果 RTC 读取失败，本地可切换到错误提示界面或调用 `oled.display` 显示错误。

## 4.5 播报当前温湿度环境

command：

```text
environment.speak_current
```

用途：

- 读取当前温度和湿度。
- 根据温湿度生成简短环境评价。
- 调用本地 TTS 模块播报当前温湿度和环境状态。

args：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `show_on_oled` | bool | 否 | 是否同时在 OLED 上显示温湿度信息 |

示例：

```json
{
  "command": "environment.speak_current",
  "args": {
    "show_on_oled": true
  }
}
```

说明：

- LLM 不需要提供温湿度数值，温湿度由本地传感器读取。
- 本地逻辑负责生成播报文本，例如“当前温度二十六度，湿度百分之五十，环境比较舒适”。
- 温湿度是否影响宠物状态，应由本地宠物状态逻辑统一处理。
- 如果传感器读取失败，本地可切换到错误提示界面或调用 `oled.display` 显示错误。

## 4.6 设置 Todo

command：

```text
todo.manage
```

用途：

- 创建、查询、修改、完成、删除待办事项。

args：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `action` | string | 是 | Todo 操作类型 |
| `todo_id` | string | 否 | Todo ID，修改、完成、删除指定任务时使用 |
| `title` | string | 否 | 任务标题或任务内容 |
| `remind_time` | string | 否 | 提醒时间 |
| `status` | string | 否 | 任务状态 |

`action` 可选值建议：

```text
create
query
update
complete
delete
```

`status` 可选值建议：

```text
pending
completed
reminded
```

创建任务示例：

```json
{
  "command": "todo.manage",
  "args": {
    "action": "create",
    "title": "明天上午交硬件课设报告",
    "remind_time": "2026-06-03 09:00",
    "status": "pending"
  }
}
```

更新任务示例：

```json
{
  "command": "todo.manage",
  "args": {
    "action": "update",
    "title": "后天上午交硬件课设报告",
    "remind_time": "2026-06-04 09:00"
  }
}
```

完成任务示例：

```json
{
  "command": "todo.manage",
  "args": {
    "action": "complete",
    "title": "xxx"
  }
}
```

删除任务示例：

```json
{
  "command": "todo.manage",
  "args": {
    "action": "delete",
    "title": "xxx"
  }
}
```

说明：

- To-Do 任务只负责任务管理、提醒和界面反馈。
- 根据当前宠物状态设计，To-Do 不直接改变宠物基础状态。

## 4.7 改变宠物状态

command：

```text
pet.update_status
```

用途：

- 以增量方式修改宠物状态。
- 不直接设置完整状态值，由本地状态管理模块在原有数值基础上进行加减。

基础状态字段参考：

| 状态 | 字段名 | 含义 |
| --- | --- | --- |
| 心情值 | `mood` | 表示宠物开心程度 |
| 饱食度 | `satiety` | 数值越高表示越饱 |
| 精力值 | `energy` | 表示宠物活跃程度 |
| 亲密度 | `affinity` | 表示用户与宠物关系 |
| 健康度 | `health` | 表示宠物综合健康状态 |

args：

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `mood_delta` | int | 否 | 心情值变化量，正数表示心情提升，负数表示心情下降 |
| `satiety_delta` | int | 否 | 饱食度变化量，正数表示更饱，负数表示更饿 |
| `energy_delta` | int | 否 | 精力值变化量，正数表示恢复精力，负数表示消耗精力 |
| `affinity_delta` | int | 否 | 亲密度变化量，正数表示亲密度提升，负数表示下降 |
| `health_delta` | int | 否 | 健康度变化量，正数表示健康改善，负数表示健康下降 |
| `status_text` | string | 否 | 当前状态描述，适合 OLED 显示的短文本 |

示例：

```json
{
  "command": "pet.update_status",
  "args": {
    "mood_delta": 5,
    "satiety_delta": 0,
    "energy_delta": -1,
    "affinity_delta": 2,
    "health_delta": 0,
    "status_text": "今天很开心"
  }
}
```

约束：

- 所有 delta 字段均为可选，未传入时按 0 处理。
- 本地状态管理模块需要将更新后的状态值限制在 0 到 100。
- `status_text` 应尽量简短，适合 OLED 小屏显示。
- 派生状态、表情和主界面显示建议由本地状态管理模块统一计算。

## 5. 备注

- 本文档中的 command 名称和 args 字段用于当前阶段的软件接口设计。
- 后续如果接入 WebSocket，可将该结构放入 `response.behavior` 的 `command` 与 `args` 字段中。
- 本地执行 command 后，建议返回执行结果，包括成功、失败、错误原因和耗时等信息。
