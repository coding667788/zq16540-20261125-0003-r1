# Crashfence

Crashfence 是一个**完全离线、仅使用 Python 3.12 标准库**的确定性安全模拟器：三台互相隔离的模拟节点共同保管一份模拟策略，两个观察员轮流担任协调者。它不连接任何真实服务，也不访问网络；除显式传入的本地模拟账本目录外，不读写其他状态。

它同时提供：

1. `simulate SCENARIO [STEPS]`：执行确定的消息传递、落盘、部分帧写入、崩溃和重启步骤，输出每一步后所有节点与读者的安全状态。
2. `check TRACE`：补全离线追踪中的缺失条目，枚举所有合法历史并投影授权结果；无合法历史时给出最短矛盾前缀，多个历史导致不同授权时给出最小证据，并区分“最后一帧不完整”和“已提交日志损坏”。
3. 真实 `subprocess.Popen` 启动、杀死短生命周期子进程的 unittest，在六个可标记落盘阶段逐一杀进程并恢复。
4. 独立串行参考模型与调度枚举，包含最多 12 个事件的交错调度、两协调者租期相接，以及“同令牌、同摘要”重复投递边界。

## 快速开始

在仓库根目录执行：

```bash
python -m unittest discover -s tests -v
```

重新生成合成夹具：

```bash
python -m crashfence fixtures fixtures
```

运行场景：

```bash
python -m crashfence simulate fixtures/scenario_revocation_priority.json
python -m crashfence simulate fixtures/scenario_coordinator_transition.json --compact-state
```

检查离线追踪：

```bash
python -m crashfence check fixtures/trace_ambiguous.json
python -m crashfence check fixtures/trace_no_history.json
python -m crashfence check fixtures/trace_committed_corruption.json
```

如果要求对本项目也使用 Python 隔离模式 `-I`，请通过根目录启动器运行（`-I` 不会把当前目录或脚本目录加入 `sys.path`，启动器显式插入仓库根）：

```bash
python -I run_crashfence.py check fixtures/trace_torn_frame.json
```

所有 CLI 输入和输出均为 UTF-8 JSON。路径参数使用 `-` 时，JSON 从标准输入读取。

默认情况下，只要输入被成功解析并生成 JSON 报告，进程退出码就是 `0`；分析结论请看 JSON 的 `status`。输入/格式错误退出 `2`。如果 CI 需要把分析结论当失败，可加 `--strict`：

| 命令 | `--strict` 退出码 | 含义 |
|---|---:|---|
| `simulate` | `3` | 报告状态为 `fatal_violation` |
| `check` | `4` | 不存在合法历史 |
| `check` | `5` | 多个合法历史导致不同授权结论 |
| 全部 | `2` | 输入/JSON/CLI 格式错误 |

## 安全模型

### 物理节点与双槽日志

默认有三个不同物理节点：`n1`、`n2`、`n3`。节点之间没有共享磁盘；跨节点影响只能通过场景中的消息或显式写入步骤发生。

每个节点有两个持久槽：`s0`、`s1`。模拟状态中每个槽保存若干长度前缀/摘要记录；真实文件账本中对应：

```text
<ledger>/nodes/<node>/slot-0.log
<ledger>/nodes/<node>/slot-0.seal.json
<ledger>/nodes/<node>/slot-1.log
<ledger>/nodes/<node>/slot-1.seal.json
```

完整日志帧的核心字段恰好包括题目要求的四项，并附加自摘要：

```json
{
  "version": 1,
  "proposal": { "...": "提案、授权或撤销内容，以及 owner token 引用" },
  "participants": ["n1", "n2", "n3"],
  "prev_digest": "上一已提交帧摘要或 GENESIS 摘要",
  "digest": "SHA-256(canonical_json({version,proposal,participants,prev_digest}))"
}
```

规范化 JSON 使用 UTF-8、排序键、无多余空白，因此相同输入必然得到相同摘要。

### 拥有者令牌与相接租期

令牌格式：

```json
{
  "token_id": "tb",
  "epoch": 2,
  "coordinator": "obs-b",
  "lease_id": "lease-b",
  "not_before": 10,
  "expires_at": 20
}
```

租期是半开区间：

```text
not_before <= t < expires_at
```

因此旧令牌 `[0,10)` 与新令牌 `[10,20)` 可以在时刻 `10` 相接，不要求空隙，也不允许重叠。时刻 `10` 旧令牌失效、新令牌有效。

节点重启扫描两个槽中的完整 owner-token 帧，只承认最后一个完整且摘要正确的拥有者令牌。若更新令牌只写入部分帧，恢复结果是 `last_frame_incomplete`，并继续使用上一个完整令牌的 epoch。节点见过更高完整 epoch 后，会拒绝更低令牌的写入。

### 法定人数、提交标记和读者

一项变更只有同时满足以下条件才算提交：

1. 同一帧摘要在至少两个**不同 physical_id 的物理节点**上完整、校验正确地落盘；
2. 帧版本和 `prev_digest` 仍接续当前已提交日志尖端；
3. 有效拥有者令牌和活动租期通过 fence 检查；
4. 发布持久提交标记。

提交后状态版本递增，提交标记包含 `marker_id/version/proposal_id/frame_digest/physical_nodes`。

读者对象声明自己的供应上游：

```json
{"id": "r1", "upstream": "up-r1", "possession_tokens": ["可选初始令牌"]}
```

读快照绑定：

- 一个已提交版本和帧摘要；
- 一个上游 ID；
- 一个占有确认令牌 `confirmation_token`。

`reader_read` 仅在快照上游等于读者上游，且读者实际持有该快照令牌时返回可见策略。否则返回安全的不可见结果和原因：

- `missing_confirmation_token`
- `wrong_upstream`
- `unknown_snapshot`

未提交帧、部分帧和节点内部候选状态不会进入读快照。

### 撤销优先

授权请求和撤销请求都有确定性的 `conflict_key`；也可以用撤销中的 `target_auth_id` 精确指向授权。

只要某个冲突撤销尚未变成 `aborted/rejected`，即使授权请求更早、甚至已经在两个节点形成法定持久副本，授权的提交标记也会被拒绝并置为 `aborted/revocation_priority`。撤销提交时，所有冲突的未提交授权变为 `superseded`。已经发布提交标记的授权属于历史事实，只能由之后的撤销新版本撤销，不会被回溯改写。

### 幂等回执与不确定响应

- 相同 `receipt_id`、相同载荷的重复回执返回 `duplicate: true`、`effect_applied: false`；消费计数始终为 1，只增加重复投递计数。
- 相同 `token + frame fields` 必然产生相同摘要；重复完整帧不会第二次加入该节点的法定节点集合。
- `record_response` 可以记录 `uncertain/failure/retry`。若试图在提案没有已提交标记时记录 `outcome: "success"`，该步骤被拒绝并产生 `FAKE_SUCCESS_WITHOUT_COMMIT` 致命项。不确定结果在投影中保持 uncertain/pending，不会被伪装成 granted/success。

## `simulate` 输入格式

顶层 SCENARIO 是 UTF-8 JSON 对象：

```json
{
  "schema": "crashfence.scenario.v1",
  "name": "可选名称",
  "initial_clock": 0,
  "nodes": ["n1", "n2", "n3"],
  "observers": ["obs-a", "obs-b"],
  "readers": [
    {"id": "r1", "upstream": "up-r1", "possession_tokens": []}
  ],
  "initial_policy": {"policy_id": "simulated-policy"},
  "steps": [
    {"op": "advance_clock", "to": 10},
    {"op": "grant_owner_token", "token_id": "t1", "token": {"...": "..."}}
  ]
}
```

`nodes` 也支持对象形式以显式指定不同物理标识：

```json
{"id": "logical-a", "physical_id": "rack-1-node-1"}
```

STEPS 参数可选；若提供 JSON 数组，则替换 SCENARIO 中的 `steps`，若提供对象，则与 SCENARIO 合并。

### 常用步骤

| `op` 及别名 | 作用 |
|---|---|
| `advance_clock` / `set_clock` | 设置确定性逻辑时钟 |
| `grant_owner_token` / `create_owner_token` | 登记并计算 owner token 摘要 |
| `persist_owner_token` | 将 owner token 作为特殊帧写入节点双槽；可 `partial_bytes` |
| `request_authorization` / `request_auth` | 创建授权提案 |
| `request_revocation` / `request_revoke` | 创建撤销提案，可给 `conflict_key/target_auth_id` |
| `write_frame` / `persist_frame` / `frame` | 在某物理节点写日志帧；支持 `slot/partial_bytes/corrupt/token/at` |
| `publish_commit_marker` / `commit` | 校验法定人数、fence、日志链接和撤销优先级后提交 |
| `crash_node` / `crash` | 节点宕机；易失状态丢失，显式持久记录保留 |
| `restart_node` / `restart` / `recover_node` | 重启并扫描双槽，输出恢复分类和最后完整 owner epoch |
| `inject_corruption` / `corrupt` | 注入已提交/未提交帧损坏，供追踪和恢复测试 |
| `send_message` / `send` | 发送显式消息并进入确定性消息表 |
| `deliver_message` / `deliver` | 按 ID 投递消息；可执行消息内嵌步骤，重复投递返回 duplicate |
| `publish_read_snapshot` | 仅允许为已提交版本创建上游读快照和确认令牌 |
| `grant_read_token` / `deliver_read_token` | 给读者发放占有确认令牌 |
| `reader_read` / `read` | 按上游和令牌检查返回安全读视图 |
| `redeem_receipt` / `receipt` | 幂等回执/消费 |
| `record_response` / `client_response` | 记录 success/uncertain/failure/retry；禁止伪成功 |
| `assert` / `observe` | 离线观察断言：授权结论、读者可见性、恢复分类、提交版本 |

无法识别的 `op`、缺少必填字段或引用不存在对象会产生 `rejected/STEP_ERROR/UNKNOWN_OP`，不会导致 CLI 堆栈泄漏；该步骤仍带当时的安全状态投影。

### 模拟输出

输出报告结构为：

```json
{
  "schema": "crashfence.simulation.v1",
  "status": "safe | assertion_failed | fatal_violation",
  "step_count": 15,
  "steps": [
    {
      "index": 1,
      "op": "...",
      "accepted": true,
      "outcome": "success | rejected | uncertain | assertion_failed",
      "events": [],
      "safe_state": {
        "clock": 0,
        "committed_version": 0,
        "nodes": { "n1": { "slots": {}, "recovery": {}, "up": true } },
        "readers": { "r1": { "held_tokens": [], "last_view": null } },
        "proposals": {},
        "policy": {},
        "authorization_projection": {}
      }
    }
  ],
  "final_state": {},
  "authorization_projection": {},
  "violations": [],
  "fatal": null
}
```

使用 `--compact-state` 可删除每一步内联的 `safe_state`，只保留最终状态。

## `check TRACE` 输入格式

TRACE 是 UTF-8 JSON 对象。最简单的形式是 `events` 数组；缺失条目写成 `null`、字符串 `"?"` 或 `{"missing": true}`：

```json
{
  "schema": "crashfence.trace.v1",
  "max_events": 12,
  "readers": [],
  "events": [
    {"op": "grant_owner_token", "...": "..."},
    {"op": "request_authorization", "proposal_id": "a1"},
    null,
    {"op": "publish_commit_marker", "proposal": "a1"}
  ],
  "choices": {
    "2": [
      {"op": "write_frame", "proposal": "a1", "node": "n2", "token": "t1"},
      {"op": "advance_clock", "to": 5}
    ]
  }
}
```

也支持：

- 顶层 `"candidates": [ ... ]`：所有缺失槽共用的候选事件字母表；
- `choices` 为数组，按下标对应缺失槽；
- 稀疏格式 `"entries": [{"seq": 0, "event": {...}}, {"seq": 3, "event": {...}}], "length": 4`；
- `initial` 或 `scenario`：所有补全历史共享的初始节点、读者和策略；
- `history_limit`：保护极大搜索；默认枚举上限 10000，夹具和参考测试均保持在 12 事件以内。

如果没有提供候选，`tracecheck.default_candidates()` 给出一个小型有限默认字母表。对于严肃的离线追踪，建议显式提供每个缺失位置的 `choices`，这样“所有合法历史”的域是明确、可审计的。

### 合法性和投影

每个补全都调用同一个确定性状态机。以下补全视为不合法历史：

- 含无法识别操作；
- 把未提交/不存在的操作记录为 `success`；
- 任一 `assert/observe` 与该历史的状态投影不一致。

被 fence 拒绝、法定人数不足、撤销优先级拒绝、重复消息/回执等属于**合法执行中的安全拒绝**，不会把该历史删除。

每个合法历史输出：

- 补全后的事件序列和事件摘要；
- 提交版本；
- 授权与读者投影摘要；
- 各节点恢复分类；
- 投影摘要。

报告 `status`：

| 状态 | 含义 |
|---|---|
| `unique_history` | 只有一个合法历史 |
| `unique_authorization_projection` | 多个合法历史，但授权/读者结论相同 |
| `ambiguous_authorization` | 多个历史产生不同授权投影；见 `minimal_authorization_evidence` |
| `no_legal_history` | 所有补全都矛盾；见 `shortest_contradictory_prefix` |

`shortest_contradictory_prefix` 给出从第一个事件起最短的无合法补全前缀，包括固定观察项和第一条断言/不可能事件错误。

`minimal_authorization_evidence` 从最短前缀开始寻找授权分歧，并给出：

- 前缀长度；
- 第一个造成分歧的缺失槽；
- 两个候选事件；
- 两个完整见证事件序列；
- 两个不同授权投影。

### 损坏分类

恢复分类按以下优先级：

1. `committed_log_corruption`：提交标记引用的帧缺失、长度/校验/摘要错误，或本地提交标记本身不可信；
2. `last_frame_incomplete`：最新槽记录没有完整 seal/长度/校验，通常是最后一帧部分写入，可安全丢弃或幂等重试；
3. `uncommitted_frame_corruption`：未提交完整帧损坏，可从法定副本重建；
4. `clean`。

关键区别是：**最后一帧不全可以截断/重写；已提交日志损坏绝不能静默截断成“干净日志”**，必须保留证据并从其他法定物理节点恢复。

夹具示例：

- `fixtures/trace_torn_frame.json`
- `fixtures/trace_committed_corruption.json`
- `fixtures/trace_wrong_corruption_expectation.json`

最后一个夹具错误地把已提交损坏断言成 `last_frame_incomplete`，`check` 会返回无历史和最短矛盾前缀，错误中同时给出 expected/actual。

## 六个真实杀进程恢复阶段

`crashfence/durable.py` 实现真实本地双槽文件账本，`crashfence/worker.py` 是短生命周期子进程入口。六个可标记阶段为：

| 阶段 | 标记名 | 杀死并恢复后的安全状态 |
|---:|---|---|
| 1 | `owner_token_durable` | 最后完整 owner token 存在；尚无帧/提交 |
| 2 | `partial_frame_durable` | n1 最后一帧只有 fsync 过的前缀，分类 `last_frame_incomplete` |
| 3 | `first_node_frame_sealed` | n1 帧和 SHA-256/length seal 完整，恢复分类 `single_node_durable_waiting_for_quorum`，但只有一个物理节点 |
| 4 | `second_quorum_node_frame_sealed` | n1/n2 两个不同物理节点完整，恢复分类 `quorum_durable_waiting_for_commit_marker`，但无提交标记，读者仍不可见 |
| 5 | `commit_marker_durable` | 提交标记持久化，变更已提交，但读者确认令牌尚不存在 |
| 6 | `read_confirmation_token_durable` | 令牌落盘；令牌持有者才可读 |

unittest 对每个阶段执行：

1. `subprocess.Popen([sys.executable, "-I", worker.py, "run", "--crash-at", k, ...])`；
2. 等待真实 ready 标记文件；
3. 断言子进程仍在等待；
4. 调用 `proc.kill()` 并 `communicate(timeout=10)`；
5. 再启动一个真实 `-I` 子进程执行 `recover`；
6. 断言该阶段对应的恢复分类、提交状态和读者可见性。

测试只在仓库 `.runtime-tmp/unittest-subprocess/` 下创建临时账本，并在清理时删除。另一个测试在完整提交后向 n1 槽追加损坏字节，验证恢复为 `committed_log_corruption` 而不是 torn tail。

手动运行 worker：

```bash
python -I crashfence/worker.py run --root .runtime-tmp/demo --crash-at 2 \
  --ready-file .runtime-tmp/ready.json
# 进程会停在阶段 2；在另一个终端或测试中杀死它，然后：
python -I crashfence/worker.py recover --root .runtime-tmp/demo
```

不带 `--crash-at` 时，worker 会一次走完六个阶段并退出。

## 独立串行参考与调度枚举

`crashfence/reference.py` 不导入富状态机，只用紧凑字典重新陈述安全关键规则，包括：

- 令牌租期和节点旧 epoch fence；
- 同节点同摘要重复写不增加法定物理节点数；
- 至少两个物理节点才有 quorum；
- 冲突撤销优先；
- 相同 receipt ID 不重复消费；
- 读者必须持有快照令牌；
- 未提交操作不能记录 success。

测试包含：

1. **7 事件边界计划的全部唯一排列**：两个相同的“同令牌、同摘要”写事件使唯一调度数为 `7! / 2 = 2520`。每个调度都检查参考不变量，并映射回富模型，逐一比较 `auth-1` 是否最终 granted。
2. **恰好 12 个事件的三流交错枚举**：控制流 2 个事件、客户端流 4 个事件、复制流 6 个事件；保持每流内部顺序，交错数为  
   `12! / (2! × 4! × 6!) = 13860`。
3. 规范 12 事件调度覆盖 obs-a→obs-b 的相接租期、重复回执、同令牌同摘要重复写、撤销提交和授权 superseded。
4. 租期边界测试直接验证 `t=9` 旧令牌有效、`t=10` 旧令牌失效而新令牌有效。

运行全部测试：

```bash
python -m unittest discover -s tests -v
```

普通 Windows CPU 上完整套件通常为数秒级；13860 个参考调度和 2520 个富模型差分排列均在标准库进程内完成。

## 目录结构

```text
crashfence/
  __init__.py
  __main__.py          # python -m crashfence
  cli.py               # UTF-8 JSON CLI
  canonical.py         # canonical JSON + SHA-256
  model.py             # 确定性场景状态机和安全投影
  tracecheck.py        # 缺失条目补全、历史枚举、最短矛盾/最小证据
  reference.py         # 独立串行参考和调度枚举
  durable.py           # 真实本地双槽文件账本与六阶段协议
  worker.py            # 可被真实启动/杀死的子进程入口
  fixtures.py          # 合成夹具定义和生成器
fixtures/              # 已生成的 UTF-8 JSON 合成夹具
tests/
  test_safety_model.py
  test_tracecheck.py
  test_reference_schedules.py
  test_subprocess_crash.py
run_crashfence.py      # python -I 时可用的仓库根启动器
```

## 非目标与边界

- 这是**模拟和离线检查工具**，不是共识服务、真实策略执行点或网络协议实现。
- 不打开 socket、不启动 HTTP 服务、不调用云 API；唯一子进程是本仓库自己的短期 worker。
- “物理节点”在模拟中由不同 `physical_id` 和独立账本目录表示；测试不使用多台机器。
- 枚举的“所有历史”限定为 TRACE 明确声明的候选事件域和 `max_events` 边界；输入格式使该域可审计，而不是声称枚举整个无界事件宇宙。
