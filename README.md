# pm-paper

面向代理研究与预测市场模拟执行的Python框架。把模型调用、角色提示词、公开市场读取、研究初筛和模拟执行拆成可组合模块，供其他项目直接调用或参考架构。

执行层提取自已有项目的模拟账本及逐档成交代码，保留决策冻结、事务、执行防重、费用、部分成交、正式结果和未知估值处理。模型服务通过应用程序接口（API）地址、模型名及密钥环境变量配置，也支持接入已有命令行代理。

## 安装与验证

Python 3.11及以上版本，运行模块使用Python标准库。

```bash
git clone https://github.com/jianyingzhihe/pm-paper.git
cd pm-paper
python -m pip install -e .
python -m unittest discover -s tests -q
python -m pm_paper demo
```

`demo`在临时目录动态生成合成资料，跑通买入、部分卖出及正式结算。输出中的`data_type`为`synthetic`，表示合成的软件验证输入。

## 接入自己的模型

复制默认配置到运行目录，设置模型服务地址、模型名和读取密钥的环境变量名称。

PowerShell示例

```powershell
New-Item -ItemType Directory -Force runtime | Out-Null
Copy-Item pm_paper/default_config.json runtime/project.json
$env:PM_PAPER_API_KEY = '填写自己的密钥'
```

Linux或macOS示例

```bash
mkdir -p runtime
cp pm_paper/default_config.json runtime/project.json
export PM_PAPER_API_KEY='填写自己的密钥'
```

默认配置使用DeepSeek的兼容消息接口。其他兼容服务修改`agent.base_url`和`agent.model`，密钥在运行时由`agent.api_key_env`指定的环境变量读取。

```json
{
  "type": "openai-compatible",
  "base_url": "https://api.deepseek.com/v1",
  "model": "deepseek-chat",
  "api_key_env": "PM_PAPER_API_KEY",
  "timeout_seconds": 120,
  "max_tokens": 4000
}
```

配置里的`role_overrides`可分别为研究、资料或复核角色设置模型服务和密钥环境变量。`review_roles`控制本轮参与复核的角色，默认包含`critic`独立反方复核。运行目录保留各次调用的请求、最终可见答复、结构化交付和实际错误。

## 开始一个研究周期

先取得公开目录，再按事件安排研究。

```bash
python -m pm_paper catalog --max-pages 1 --page-size 10 --output runtime/catalog.json
python -m pm_paper screen --catalog runtime/catalog.json --output runtime/screening.json
python -m pm_paper --config runtime/project.json cycle --catalog runtime/catalog.json --workspace runtime/project
```

目录输出的`complete`说明是否实际到达分页终点，`next_cursor`记录下一页位置。上例控制首次下载规模，实际覆盖范围见输出。持续全量采集可增大`--max-pages`并核对分页终点，也可用`--cursor`读取后续范围。

每轮按事件合并市场，结合参考价格档、反馈时间和资料标记排序，再按上次选择记录轮转。价格极端的市场保留专项研究去向，流动性、成交量和价差作为排序信息。当前目录超过36小时时保留实际错误并接续已有持仓复核。

公开数据默认从本机读取。已有公开资料主机时，可把`PM_PAPER_DATA_HOST`设置为自己安全远程连接的主机别名。该方式通过SSH安全远程连接及远端Python标准库读取相同公开接口，逐次保留请求与失败。连接配置由调用方的运行环境提供。

默认周期调用研究负责人、独立反方复核和项目判断汇总。角色根据实际输入形成具体动作及资料缺口。代码从公开原件补齐市场身份和完整规则，校验决定，再按调用方选择进入模拟执行。

API密钥提供模型调用能力。公开市场数据由代码读取，新闻、公告及其他领域资料可通过`--evidence`传入结构化原件或由调用方接入自己的资料工具。资料助手提示词用于整理实际提供或实际访问的材料。

## 复用执行层

### 使用自己的账本

独立预检返回按方向及金额的实际可买份额、含费成本、最低数量所需金额、退出收入及无法退出份额。调用方可把这些结果接到自己的资金和交易记录中。

```python
from pm_paper.execution import preflight
from pm_paper.files import read_json

item = read_json("runtime/order_item.json")
snapshot = read_json("runtime/snapshot.json")
result = preflight(item, snapshot, cash="1000")
```

逐档成交纯函数位于`pm_paper.fills`，公开读取位于`pm_paper.public`，完整账本位于`pm_paper.account`。

### 使用兼容的已有账本

```bash
python -m pm_paper account report --path runtime/account.sqlite --output runtime/report.json
python -m pm_paper account freeze --path runtime/account.sqlite --decision runtime/decision.json
python -m pm_paper account execute --path runtime/account.sqlite --decision-id ACTUAL_DECISION_ID --output runtime/execution.json
python -m pm_paper account review --path runtime/account.sqlite --output runtime/review.json
```

`execute`读取已冻结决定，再取新盘口执行。同一决定返回原执行结果。已有账本通过明确路径加载，格式兼容性由数据库标识和表结构检查。

为新项目明确建账时，可使用`account create --path runtime/account.sqlite --initial-cash 1000`。资金参数由调用方选择，创建命令要求目标文件尚不存在。

周期接入账户后，会先复核已有持仓，并在模型或新资料步骤失败后继续保存持仓报告。

```bash
python -m pm_paper --config runtime/project.json cycle --catalog runtime/catalog.json --workspace runtime/project --account runtime/account.sqlite --execute
```

## 角色提示词

| 文件 | 职责 |
|---|---|
| [项目接管](pm_paper/prompts/project_start.md) | 接收目标、配置、已有状态和接入方式 |
| [研究负责人](pm_paper/prompts/researcher.md) | 规则、方向、支持反证、概率及具体决定 |
| [资料助手](pm_paper/prompts/collector.md) | 日期、数字、公开资料、去重和缺口整理 |
| [独立反方复核](pm_paper/prompts/critic.md) | 核对具体错误、执行条件与反证 |
| [项目判断汇总](pm_paper/prompts/coordinator.md) | 核验意见并保存最终动作及处理依据 |
| [判断与执行复盘](pm_paper/prompts/retrospective.md) | 原判断、正式结果、含费损益及方法迭代 |

这些提示词可直接放进其他代理的系统提示，也可通过命令行单独调用。

```bash
python -m pm_paper --config runtime/project.json agent --role collector --context runtime/context.json --task "整理这批原件的日期、变化与关键缺口" --output-dir runtime/collector-task-001
```

## 工程处理

- 决定与执行历史采用追加记录，已冻结内容保留原摘要和真实时间。
- 同一项目使用独占周期锁，避免重叠日程重复调用模型和领取候选。
- 执行按盘口逐档、费用、数量精度、最小数量、限价和现金核算。
- 同账户对同一源端挂单深度保留消费记录，重复决定与重复报价分别检查。
- 未取得费用或完整报价时保留未知，报告分开显示可退出金额、无法退出份额及估值未知。
- 正式结算核对平台解决状态、订单簿服务关闭状态、条件、代币与明确胜者。取消、退款和均分进入待核验。
- 当前提取账本沿用300秒报价有效期与源端盘口时间检查，数量精度为0.01份，费用函数支持接口指数为1的形式。其他口径可通过明确版本继续适配。
- 概率未知使用`null`。数值点概率、方法依据、未来正式结果及校准状态分别留档。

## 继续接入

- [架构与运行机制](docs/架构与运行机制.md)
- [接口与数据约定](docs/接口与数据约定.md)
- [接入已有代理](docs/接入已有代理.md)
- [代码来源与适配](docs/来源与适配.json)
- [Python调用示例](examples/use_execution.py)

运行产物默认放在`runtime/`，资料原文、账户、行情快照和调用记录由各项目在自己的运行目录保存。仓库维护代码、提示词、配置模板、说明及合成测试。
