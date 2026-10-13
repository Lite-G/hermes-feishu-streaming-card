# 开发分支的模块边界与兼容入口

本页记录 2026-10-13 重构分支已经建立的边界，不代表全部计划已经完成，也不代表公开版本已包含这些变化。总进度见[执行计划](../plans/2026-10-13-refactor-upgrade.md)。

| 职责 | 实现所有者 | 调用方保留什么 |
| --- | --- | --- |
| 终态 PATCH、有限重试、一次补发的顺序 | `terminal_delivery`；结果类型在 `delivery` | server 冻结目标，注入 I/O，核对 generation 后提交回执/通知/检查点 |
| 事件路由、reply alias、独立通知归属 | `event_rules` | server 在原锁域读取状态并写入 alias；纯规则只返回键或变更计划 |
| native terminal admission、重复终态与持久 fence 衔接 | `native_admission` | 只依赖原始事件、owner 快照与四方法 store port；server 保留 policy、cache 提交、HTTP/指标与 repair 调度 |
| 选择值与表单解析 | `interaction_input` | server/SDK 继续负责身份、过期、claim 和原 Hermes resolver |
| 过期审批窗口的重展资格 | `interaction_rules` | server 先在原锁内验证对象/token/chat/kind 并 expire，再按资格旋转窗口 token；重展不执行 resolver |
| task 状态投影、颜色语义 | `card_view` | `presentation` 修改已知显示字段；不改执行或授权状态 |
| 正文的完整来源与显示表示 | `content_plan`、`native_tables`、`text` | renderer 组合卡片并由共享 serializer 裁决最终容量 |
| 命令卡纯构造 | `command_cards` | hook_runtime 保留 producer、callback 注册、adapter identity 与命令执行 |
| turn-local observer 序号、terminal barrier 和有界 worker | `turn_events` | PluginRuntime 保留 producer 归属、registry/map 与锁顺序；允许的 producer 名称不是排他所有权证明 |
| 只读 CLI 格式化与本地诊断导出 | `cli_reports`、`support_report` | CLI 执行诊断并决定退出码；报告默认不上传 |
| 已加载阅读默认值与下次加载比较 | `config_snapshot` | process 验证现有受管进程，server 只读取 factory 已加载值 |
| 解释器路径身份 | `runtime_ownership.python_executable_identity` | server、CLI、runner、maintenance 共用同一算法 |
| Base delivery 源码家族验证 | `install/base_contracts`、`install/ast_contracts` | patcher 传入原 renderer 生成的 owned blocks，再按原事务应用 |
| portable 安装文件事务与 evidence | `install/file_transaction` | CLI 与 decomposed installer 共用；自动恢复仍要求原有更强目录绑定 |
| POSIX 目录句柄、原子写入和事务回滚 | `install/posix_transaction` | CLI 选择安装/恢复计划并传入原有 pre-commit/validate callbacks；底层模块不导入 CLI |
| 受理事件观测耗时 | `observations` | session 只记录已接受事件；checkpoint 不恢复计时；不推导模型速率 |

## 兼容名称与清退条件

以下是导入别名或窄接线，实际逻辑只有一份。新代码直接导入实现所有者；不要复制旧实现，或让业务模块反向导入入口文件。

| 入口 | 当前消费者与保留理由 | 清退条件 |
| --- | --- | --- |
| `cli._format_*`、`_doctor_exit_code` | CLI 现有 dispatch/格式化调用与针对公开输出的测试；不是新公共扩展接口 | 相关命令整体迁移时同步改调用方和故障注入点，不为改名单独增加转发层 |
| `server` 的旧 event-rule 名称 | 现有协调流程及 HTTP 回归定位；新纯规则不依赖 server | 调用方迁到真实所有者后逐个移除未用别名，不能绕过锁内写入 |
| `server._native_handoff_ack_capable` 与 identity/metadata 导入名 | 现有 handoff begin 调用和契约测试；窄 wrapper 只提取 verifier 是否可用 | 调用迁移时保留原认证入口，不能把 capability 布尔值当作签名通过证明 |
| `server.python_executable_identity` | 既有外部导入及 `test_server` 的直接消费者 | 暂保兼容 reexport；新内部调用已直达 runtime_ownership |
| `presentation` 的 task view 名称 | 既有导入保持可用；renderer/reading/server 已直达 card_view | 确认支持范围内无导入消费者并有迁移说明后移除；不得保留两份算法 |
| `hook_runtime` 的 command builder 名称 | 现有 adapter/命令调用；注入 hook 的稳定入口不改 | 完成 producer/callback 梳理后分开审核，不能根据私有前缀认定可删 |
| `hermes_plugin_runtime.ObserverEvent`、`TurnEventCoordinator` | 既有调用和测试的稳定 import；对象直接 reexport，无 subclass/wrapper | 新独立消费者从 turn_events 导入；已有导入路径在支持周期内保留 |
| `cli` 的 file_transaction helpers | 安装/恢复计划继续使用 snapshot/read/hash/evidence；portable writer 已共享 | 已清退没有产品或测试消费者的四个导入；其余随具体消费者迁移，不保留两份实现 |
| `patcher._find_exact_base_patch_locations` | 把两个现有 renderer 的精确 owned fragments 传给纯验证器 | 当前是必要接线，不是双实现；改变生成语义必须重新证明逐字恢复 |

迁移故障注入时必须证明异常仍触及真实 I/O，不能因为 patch 了一个无人调用的旧别名就宣布测试通过。已确认的迁移回归保留首次失败与修复记录。

POSIX writer、bound helpers 和 capability 检查的产品消费者已全部改为 `posix_transaction` 模块访问，避免固定导入别名让故障注入漏掉部分调用。旧 CLI 私有名称没有产品外部消费者，未新增兼容转发层；CLI 的公开 `main` 及命令参数保持原入口。

## 仍需继续整理

server 的交互 claim/resolver、handoff repair 编排和部分状态提交仍在协调器；hook producer/SDK 接线、CLI 的完整生命周期事务以及 Gateway/cron 来源家族尚未完成全部拆分。移动端真机、五人上手试用、性能比较与公开包验收分别留在计划中，不用本页的结构说明代替。

native admission 的有 generation fence 与终态去重仍先于 policy；无 generation 的 legacy lifecycle 准备仍在 policy 之后。返回值分开保存 restore 时的 cache 更新和 expire 后的 selected record，后续拒绝也不能丢弃已发生的 cache 更新。四次 store 操作沿用各自的原子机制，不被描述为一个新的跨调用事务。
