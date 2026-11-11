# iPhone 购买跟进 Agent Demo

基于 Python 和 LangChain 的命令行助手，帮助用户澄清换机需求、比较模拟商品，并从购买确认持续跟进到正确收货。采用单进程 CLI、单 Agent 和 SQLite，所有商品、订单、支付与物流均为模拟数据。

## 项目功能

- **需求澄清与商品比较**：收集用途、预算、地区、容量、成色及日期要求，区分普通价格和附条件优惠，展示候选及不匹配原因。
- **人工确认购买**：使用 LangChain `create_agent` 调用工具，通过原生人工中断展示具体商品、卖家、费用和配送条件，用户批准后才模拟下单。
- **条件变化处理**：修改预算或报价变化后，旧建议及受影响的确认失效；下单前再次校验库存和费用。
- **未知订单核验**：模拟订单创建后响应超时，只查询原操作，不重复购买；核验受次数和时间限制。
- **持续跟进与通知**：程序运行时，即使没有新聊天，也按约定频率检查；关键变化通知，重复事实或无变化保持静默。
- **任务控制**：支持暂停、恢复、结束和自动到期；恢复不补发暂停期间提醒，迟到事实不自动重启任务。
- **结果核对**：待支付、支付成功或物流送达均不算目标完成；需要订单证据与用户正确收货反馈一致。
- **可重放验收**：提供正常、调整、控制三条场景，以及原始需求 12 个案例对应的业务测试和证据。

支付、取消、退货和退款由测试用户注入模拟事件，Agent 只读取结果。每个数据目录只运行一个进程，通知输出到终端；程序退出后不再自动跟进。

## 启动方法

### 1. 安装依赖

需要 Python 3.11+，当前验证环境为 Windows / Python 3.12。以下命令在项目根目录的 PowerShell 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### 2. 配置模型

编辑 `.env`：

```dotenv
OPENAI_API_KEY=填写模型服务密钥
MODEL_NAME=填写支持工具调用的模型名称
OPENAI_BASE_URL=
MODEL_TIMEOUT_SECONDS=30
AGENT_RECURSION_LIMIT=16
```

模型需要支持 tool calling。`OPENAI_BASE_URL` 可留空，使用兼容服务时按提供方说明填写。未配置密钥和模型名称时，交互程序会明确报错；离线测试及场景重放无需模型配置。

### 3. 启动交互

```powershell
.\.venv\Scripts\python.exe -X utf8 -m order_demo.cli --user-id demo --task-id t1 --data-dir data/demo
```

输入“想换一台适合我的 iPhone”，按助手追问补充条件并授权跟进。演示时可约定每 10 秒检查一次，并分别给出期望收货日期与跟进截止日期。

比较后明确选择商品。终端显示待确认的购买快照和操作编号时，输入 `/approve <操作编号>` 批准或 `/reject <操作编号>` 拒绝。

### 4. 常用命令与模拟反馈

| 命令 | 功能 |
| --- | --- |
| 普通文本 | 与 Agent 对话，澄清需求、比较和选择商品 |
| `/status` | 查看任务与当前订单操作 |
| `/requirements <JSON>` | 精确修改需求；金额单位为整数分，日期为 Unix 秒 |
| `/approve <操作编号>`、`/reject <操作编号>` | 批准或拒绝当前购买确认 |
| `/pause`、`/resume`、`/end` | 暂停、恢复或结束任务 |
| `/advance 10` | 将演示时钟额外推进 10 秒，触发到期检查 |
| `/event <类型> <JSON>` | 注入测试用户或模拟来源的事实 |
| `/evidence` | 查看事件与业务证据 |
| Ctrl+C | 退出程序 |

订单创建后，依次在 Demo 终端输入以下模拟反馈。`$operation` 会自动替换为当前操作编号；它是 Demo 命令参数，不是在 PowerShell 中执行的变量：

```text
/event paid {"operation_id":"$operation","event_id":"paid-1"}
/advance 10
/event delivered {"operation_id":"$operation","event_id":"delivered-1"}
/advance 10
/event receipt {"operation_id":"$operation","storage_gb":256,"correct":true,"event_id":"receipt-1"}
/status
```

收货容量应与实际选择一致。其他事件包括 `offer`、`shipping`、`timeout_next_order`、`source_failure`、`cancelled`、`returned`、`refund_requested`、`refund_processing`、`refunded`。同一事件编号重复输入会去重，异载荷冲突会标记待核验。

## 测试方法

### 1. 运行全部自动化测试

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
```

当前记录为 **39 项通过、1 项在线模型测试跳过**，详细结果见 [验收记录](docs/验收记录.md)。

| 测试文件 | 覆盖内容 |
| --- | --- |
| `tests/test_acceptance.py` | 原始需求的 12 个验收案例，以及授权、控制、去重和冲突边界 |
| `tests/test_agent.py` | 真实 LangChain 工具调用、人工中断、确认恢复和检查点；模型为离线测试替身 |
| `tests/test_cli.py` | CLI 控制命令、时间推进及三条场景重放 |
| `tests/test_live_model.py` | 可选的真实模型正常购买流程，默认跳过 |

仅运行原始需求验收案例：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_acceptance.py -v
```

### 2. 重放三条演示路径

无需密钥。每次重放使用新的数据目录，以下用时间戳避免复用历史状态：

```powershell
$replayRun = Get-Date -Format "yyyyMMdd-HHmmss-fff"
.\.venv\Scripts\python.exe -X utf8 -m order_demo.cli --replay fixtures/scenarios/normal.json --data-dir "data/replay-$replayRun/normal"
.\.venv\Scripts\python.exe -X utf8 -m order_demo.cli --replay fixtures/scenarios/adjustment.json --data-dir "data/replay-$replayRun/adjustment"
.\.venv\Scripts\python.exe -X utf8 -m order_demo.cli --replay fixtures/scenarios/control.json --data-dir "data/replay-$replayRun/control"
```

| 场景 | 主要验证 |
| --- | --- |
| `normal.json` | 澄清、报价、确认、待支付、物流和正确收货 |
| `adjustment.json` | 预算修改、旧确认失效、下单超时和原订单核验 |
| `control.json` | 暂停静默、恢复不补发、结束后迟到事实 |

结果保存在各数据目录的 `evidence.json`，本次已生成的证据位于 `docs/evidence/`。三条场景共 34 个断言。

重放标记为 `OFFLINE_RULE_REPLAY`，用于验证业务状态和副作用；Agent 集成测试使用离线模型替身，两者均不代表在线模型测试通过。

### 3. 运行真实模型测试

先完成 `.env` 配置，再显式启用：

```powershell
$env:RUN_LIVE_MODEL="1"
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_live_model.py -v
Remove-Item Env:RUN_LIVE_MODEL
```

该测试会实际调用模型服务，验证商品查询、人工确认、模拟下单及正确收货流程。当前本地尚未配置模型，因此在线测试未执行。

### 4. 导出测试报告

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --junitxml=docs/evidence/pytest.xml
```

更多材料：[实现方案](实现方案.md)、[验收记录](docs/验收记录.md)、[修改说明](docs/修改说明.md)、[AI 协作记录说明](docs/ai-records/README.md)。
