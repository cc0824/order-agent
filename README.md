# iPhone 购买跟进 Agent Demo

基于 Python、LangChain 和 Streamlit 的购买跟进演示。通过浏览器页面澄清需求、比较商品、确认模拟订单，并持续跟进到正确收货。全部商品、订单、支付与物流均为模拟数据。

## 项目功能

- **聊天与需求表单**：使用 LangChain `create_agent` 处理自然语言，只需明确预算即可查询。用途与偏好可选，其他条件预填默认值，并可随时修改。
- **商品比较与购买确认**：展示模拟商品、卖家、总费用和配送时间；选择商品后，通过 LangChain 原生人工中断等待批准或拒绝。批准只创建模拟订单，不会付款。
- **条件变化与下单保护**：修改预算或报价后旧确认失效；下单前再次核对费用、版本及库存。下单回执超时后只核验原操作，避免重复购买。
- **持续跟进与通知**：服务运行期间，即使没有新聊天，也按授权频率检查。关键变化显示在“通知与记录”，相同事实保持静默。
- **任务控制与完成核对**：页面按钮支持暂停、恢复和结束，截止时间自动生效。支付成功、物流送达都不直接判定完成，需要正确收货反馈与订单证据一致。
- **模拟验收与证据**：页面可注入支付、物流、收货及退款等模拟事实、推进演示时间，并下载任务证据。保留原需求 12 项业务验收及三条离线场景。

采用标准 Streamlit 组件，无需 Node.js 或单独构建前端。控制台聊天及斜杠命令入口已移除；终端仅用于启动服务、运行测试和非交互式场景重放。

## 启动方法

目录按模块划分，前后端通过 Python 接口协作，仍由同一个 Streamlit 进程启动：

```text
app.py                         # 启动入口
frontend/page.py               # 页面、表单与局部刷新
backend/                       # Agent、业务规则、模拟服务、持久化与后台检查
  config.py                    # 统一环境读取与项目路径
  replay.py                    # 非交互式场景验收
config/
  .env.example                 # 配置示例，Git 管理
  .env                         # 本机模型密钥，Git 忽略
  catalog.json                 # 初始商品，Git 管理
  defaults.json                # 默认购买条件与跟进参数，Git 管理
  simulated_external.json      # 页面实际使用的模拟商品与订单，Git 管理
  scenarios/                   # 三条验收场景，Git 管理
data/                          # 本机 SQLite 会话和检查点，Git 忽略
tests/                         # 自动化测试
```

`config/simulated_external.json` 已从旧 `data/web/` 迁入。页面修改价格、库存或创建模拟订单，会修改此文件并形成可审查的 Git 差异。`catalog.json` 用于初始化新模拟状态。默认商品以 `delivery_seconds` 保存相对配送时长，启动时换算成当前预计送达时间，订单中的确认快照保存固定时间。SQLite 保存的本机任务与聊天记录不随 Git 分发，提交模拟 JSON 不会恢复这些会话。配置中的相对路径均从项目根目录解析。

### 1. 安装依赖

当前验证环境为 Windows / Python 3.12。以下命令在项目根目录的 PowerShell 执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item config/.env.example config/.env
```

启动和离线验收需要的数据已集中到 `config/` 并由 Git 管理，克隆后即可使用，无需从旧 `fixtures/` 或 `data/` 补文件。真实模型密钥填写在本机 `config/.env`，该文件不提交。

### 2. 配置模型

编辑 `config/.env`：

```dotenv
OPENAI_API_KEY=填写模型服务密钥
MODEL_NAME=填写支持工具调用的模型名称
OPENAI_BASE_URL=
MODEL_TIMEOUT_SECONDS=30
AGENT_RECURSION_LIMIT=16
DEMO_DATA_DIR=data/web
DEMO_MOCK_FILE=config/simulated_external.json
DEMO_USER_ID=demo
DEMO_TASK_ID=t1
```

模型需支持 tool calling；兼容服务可配置 `OPENAI_BASE_URL`。缺少密钥或模型名称时，页面明确提示，聊天和购买确认不可用；需求表单、模拟事实和离线测试仍可使用，不会自动替换成预写答案。修改配置后需重启服务。

### 3. 启动页面

```powershell
.\.venv\Scripts\python.exe -m streamlit run app.py
```

在浏览器打开 [http://localhost:8501](http://localhost:8501)。消息、状态、候选及通知自动更新，聊天框和表单保留草稿，分为五个功能页签：

| 页签 | 使用方法 |
| --- | --- |
| 对话 | 发送后立即显示用户消息，等待时显示处理状态；按助手追问补充信息，选定商品后在对话下方核对购买快照并点击确认或拒绝 |
| 需求设置 | 只必填预算，其他条件在“更多选项”中修改；持续跟进需主动勾选授权；聊天修改需求后，点击“载入当前已生效需求”才会替换表单草稿；日期为香港时间 |
| 商品与订单 | 查询候选、选择商品，核对固定快照后批准或拒绝；可查询当前订单状态 |
| 模拟测试 | 下单后点击“载入当前订单的模拟反馈表单”，再注入支付、送达、收货反馈、报价变化或异常，也可推进时间 |
| 通知与记录 | 查看关键变化、事实与工具执行记录，下载 JSON 证据 |

可以直接输入“预算6000元，其他按默认条件”。默认值保存在 `config/defaults.json`：国行（CN）、全新、最低128GB、不限型号、7天内收货。预算始终由用户指定，用途和当前手机问题不必填写。持续跟进默认关闭，不影响主动查询、选购及手动查询订单；开启时一次授权默认方案（跟进7天、每600秒检查、页面通知）即可。期望收货时间与跟进截止时间独立保存，首次应用默认值时换算成固定日期，重启不顺延；修改配置只影响新任务或旧任务缺失项，已有用户决定保持原值。

正常演示：填写预算（如需后台跟进，勾选授权） → 查询商品 → 选择具体商品 → 等待人工确认按钮出现 → 确认模拟订单 → 在“模拟测试”提交“用户支付” → 提交“物流送达” → 按实际订单容量填写收货反馈并勾选一致。最后一步证据一致后任务才完成。支付、送达等外部事实由后续检查读取，也可点击“查询当前订单状态”主动核验。

购买确认同时显示在“对话”和“商品与订单”页签。选定商品后直接发起确认，无需重复输入口令；核对快照后点击“确认此模拟订单”才提交。等待确认期间输入“确认提交”只提示使用现有按钮，不会撤销当前有效确认或自动下单；修改预算、改选商品等新指令仍撤销旧确认。确认过期或条件变化后必须重新准备，不能复用旧快照。

地区筛选支持“中国”“中国大陆”“China”和 `CN` 等价；明确填写“不限”或 `ANY` 时不限制商品地区，预算、容量等条件仍生效。地区字段是模拟商品的销售地区/版本标签，Demo 未核验具体收货地址的配送覆盖，不能把地区标签不匹配解释为无法配送。已有会话保存的“中国”“不限”也兼容，无需清空记录。

成色筛选支持“全新”“全新机”与 `new` 等价，“二手”“二手机”与 `used` 等价，忽略英文大小写和首尾空格。新需求保存为统一标签；已有中文需求也可直接查询及载入表单。翻新等其他成色当前不支持，保存时会明确拒绝，不自动替换用户条件。

页面顶部可暂停、恢复或结束任务。模型请求由一个后台线程串行执行，页面保持可操作；请求期间提交的控制会在后续业务操作前检查，无法撤销已受理订单。恢复不会补发暂停期间提醒。

这是本地单任务 Demo，无账户认证。同一数据库目录或模拟状态文件只启动一个服务进程，多浏览器窗口共享同一任务。关闭浏览器后，只要 Streamlit 服务仍运行，授权检查继续；停止服务后不再跟进。在启动服务的终端按 Ctrl+C 停止。任务结束后，如需全新演示，在 `config/.env` 更换 `DEMO_TASK_ID`、`DEMO_DATA_DIR`，同时把 `DEMO_MOCK_FILE` 改为一个新的 JSON 路径（例如 `config/demo2-simulated_external.json`）后重启。缺失的模拟状态文件会从 `config/catalog.json` 初始化，旧数据仍保留。

## 测试方法

### 1. 自动化验收

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q
```

| 测试文件 | 覆盖内容 |
| --- | --- |
| `tests/test_acceptance.py` | 原始需求 12 个验收案例及授权、版本、幂等、控制、去重与完成证据 |
| `tests/test_agent.py` | 真实 LangChain 工具调用、原生人工中断、恢复与检查点，使用离线模型替身 |
| `tests/test_regions.py` | 中国与 CN 等价、明确不限、旧会话兼容、其他硬条件保留及地区拒绝证据 |
| `tests/test_conditions.py` | 全新/二手中英文匹配、旧会话兼容、真实成色冲突及预算容量约束 |
| `tests/test_defaults.py` | 仅预算查询、可配置默认值、用户条件优先、旧会话补缺、未授权不后台检查及购买确认 |
| `tests/test_runtime.py` | 页面操作队列、无聊天定时检查、购买确认、控制并发、旧表单拒绝及缺模型行为 |
| `tests/test_page.py` | Streamlit 页面渲染、需求表单、控制按钮及购买至正确收货的完整按钮流程 |
| `tests/test_replay.py` | 三条非交互式离线验收场景 |
| `tests/test_project_layout.py` | 配置目录、与启动位置无关的路径解析、共享 Mock 与数据库分离、测试数据隔离 |
| `tests/test_live_model.py` | 可选真实模型购买流程，默认跳过 |

仅运行原始需求验收：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_acceptance.py -v
```

### 2. 重放三条业务路径

无需模型密钥，每次使用新数据目录：

```powershell
$replayRun = Get-Date -Format "yyyyMMdd-HHmmss-fff"
.\.venv\Scripts\python.exe -X utf8 -m backend.replay --scenario config/scenarios/normal.json --data-dir "data/replay-$replayRun/normal"
.\.venv\Scripts\python.exe -X utf8 -m backend.replay --scenario config/scenarios/adjustment.json --data-dir "data/replay-$replayRun/adjustment"
.\.venv\Scripts\python.exe -X utf8 -m backend.replay --scenario config/scenarios/control.json --data-dir "data/replay-$replayRun/control"
```

三条场景分别验证正常购买、预算调整与下单回执超时、暂停恢复与结束后迟到事实，共 34 个断言。结果写入各目录的 `evidence.json`。重放仅为 `OFFLINE_RULE_REPLAY`，不是在线模型测试。

### 3. 可选真实模型测试

先配置 `config/.env`，再执行：

```powershell
$env:RUN_LIVE_MODEL="1"
.\.venv\Scripts\python.exe -X utf8 -m pytest tests/test_live_model.py -v
Remove-Item Env:RUN_LIVE_MODEL
```

这会实际调用模型服务。默认自动化测试会跳过此项，离线模型替身的通过结果不能代替在线验证。

导出机器可读报告：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest -q --junitxml=docs/evidence/pytest.xml
```
