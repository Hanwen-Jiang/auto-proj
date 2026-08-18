# auto-proj

本仓库是 Sub2API 原生 A6 Channel 经营模块的设计与协议工作区。旧的独立 A6 Trader 已冻结归档，
新工作以 `docs/sub2api-a6-channel-implementation-prompt.md` 为唯一实现总纲。

## 从这里开始

1. 先核对 [冻结清单](docs/a6-channel-blueprint/FROZEN-MANIFEST.md) 与 [Canonical 终稿](docs/A6API_Canonical_Design_FrozenCandidate_SSEIntegrated.md) 的哈希，确认实现基线没有漂移。
2. 从 [分系统实施总纲](docs/a6-channel-blueprint/README.md) 选择一个有明确依赖和验收边界的纵向工作包；按版本交付时从 [R0-R4 横向总纲](docs/a6-channel-blueprint/delivery-slices/README.md) 选择目标切片。
3. 阅读 [实现 Prompt](docs/sub2api-a6-channel-implementation-prompt.md)，确认该工作包对应的数据模型、状态机、经济门和迁移范围。
4. 以 [A6 API Reference](docs/a6-api-reference.md) 实现 Typed Client，不从旧 Trader 代码反推协议。
5. 以 [错误分类合同](errorlist.md) 实现 attempt、账号、路由、Listing 和平台 incident 的分层处置。
6. 需要迁移旧行为时先查 [旧 Trader 逻辑说明](docs/legacy-trader-logic.md)，再进入冻结源码定位实现。

## 活跃目录

| 路径 | 作用 |
| --- | --- |
| `docs/A6API_Canonical_Design_FrozenCandidate_SSEIntegrated.md` | 已冻结、可散列校验的完整设计终稿 |
| `docs/a6-channel-blueprint/` | 面向人和实现 Agent 的分系统三联稿、图谱及需求追踪 |
| `docs/a6-channel-blueprint/delivery-slices/` | R0-R4 横向渐进交付切片，不改变冻结语义 |
| `docs/sub2api-a6-channel-implementation-prompt.md` | 新模块实现合同与验收口径 |
| `docs/a6-api-reference.md` | A6 控制面和请求面协议 |
| `api-docs/` | 已脱敏的协议抓包与响应样本 |
| `errorlist.md` | 统一错误分类输入 |
| `resources/corpora/` | Supplier、Channel、Listing 和 Token 公共文本语料 |
| `docs/legacy-trader-logic.md` | 旧系统领域、控制器、状态和迁移说明 |
| `archive/legacy-trader/` | 冻结的旧 Python Trader 源码、测试、迁移和部署定义 |

## 本机实现工作区

- `E:\jhw\a6-auto` 是设计与协议 Git 仓库；它不直接承载 Sub2API 源码。
- `E:\jhw\a6-auto\.local\sub2api-base\backend` 是当前 Sub2API 实现主工作区，也是独立 Git 仓库，集成分支为 `integration`。
- `E:\jhw\a6-auto\.local\worktrees\` 和 `E:\jhw\a6-auto\.local\sub2api-base\worktrees\` 仅承载子任务 worktree。
- 外层仓库按规则忽略整个 `.local/`；因此外层提交不会包含实现仓库的 commit，二者必须分别核对和交付。
- `ssh us_cloud` 是验收后的部署目标，不是日常编码工作区；未完成 release 不得被误报为已上传或已发布。

## 工作边界

- 活跃协议以根目录文档为准；归档代码和归档设计稿仅提供迁移依据。
- 新 A6 Channel 逻辑直接落入 Sub2API，不继续扩展归档 Python Trader。
- A6 外部事实优先级为：实测响应、已脱敏抓包、活跃 API Reference、归档代码。
- 新接口先补 `docs/a6-api-reference.md`，再实现 Typed Client 和写后读测试。
- 公共展示文本只能从 `resources/corpora/` 选择。
- Cookie、完整 Token、Key、代理、钱包清单、HAR 和运行输出只留在本机 `.local/`，不进入 Git。

## 归档策略

旧 Trader 在整理前包含 91 个 Python 模块、49 个脚本、123 个测试文件、68 个数据库迁移，以及
43 个 service 和 26 个 timer。它们整体保存在 `archive/legacy-trader/`，原始使用说明保存在
`archive/legacy-trader/LEGACY_README.md`。

归档是只读参考面。需要迁移逻辑时：

1. 从实现 Prompt 的目标行为出发。
2. 在旧逻辑说明中找到对应控制器和事实表。
3. 只提取不变量、协议读回和失败补偿，不照搬旧进程边界。
4. 在 Sub2API 的数据库、调度器、计费链和管理端内重新落地。

## 当前状态

仓库当前只维护设计、协议、语料和旧实现归档。Sub2API A6 Channel 功能的编码、迁移和发布状态
以实现 Prompt 的验收清单及后续提交为准。
