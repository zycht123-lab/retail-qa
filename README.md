# 企业问数 Agent：模板优先、长尾 SQL 审核

这是根据 Notion [「中日云岗位面试案例概要」中的案例②](https://app.notion.com/p/671ab5f90c4a4e99af1449f77753c080) 做的**本地可运行 PoC**。数据、用户身份和结果都是合成演示；它不是该虚构客户已经上线的系统。

首版实现了两条核心路径：

```text
问题 → 路由模型识别意图/指标/站点/时间
  ├─ 缺条件 → 追问，不生成 SQL
  ├─ 已审核的期间对比 → 后端套用参数化 SQL 模板 → 自动查询
  ├─ 明确的长尾问题 → SQL 模型生成 SELECT 草稿 → 程序预校验
  │                    → 分析师查看/修改/批准 → 按提问人的可见数据执行
  └─ 开放式下降分析 → 先查一个批准指标 → 根据结果最多选两次批准的后续指标
已查询的答案草稿 → 独立 Judge 模型复核 → 通过才展示答案
```

当前有净销售额、已付款订单数、访问次数三个指标，以及对应的三个期间对比能力。`yesterday` 和 `last_week` 按 `America/New_York` 业务时区转换为明确日期。合成数据按站点和业务日保存 60 天；美国站最近有一段**人为设置**的下降，便于演示多步分析。当前数据源另有库存数量字段供长尾分析，但它是日快照，不是实时库存。

## 启动

项目读取相邻 `agent-demo/config.toml` 中已有的智谱、百炼和 TokenHub 模型配置与 Key，**不会把 Key 提交到本仓库**。克隆到其他电脑时，需自行提供该配置，或修改本项目 [config.toml](config.toml) 的 `model_config` 指向自己的模型配置。[config.toml](config.toml) 的 `[models]` 分别配置 `router=qwen`、`sql=glm`、`judge=hy4`。其中路由模型也负责开放式分析的下一步选择。可以把每个角色改为已有的 `qwen`、`glm`、`hy4` 或 `deepseek`；命令行也可用 `--model`、`--sql-model`、`--judge-model` 临时覆盖。

Notion 案例②设想在 Bedrock 上用 Claude Sonnet 4 做提交前 Judge。本地演示先用你已开通的 TokenHub `hy4-preview` 承担该角色；这**不是**已接入 Claude。后续接入 Claude 时，要先在共享模型配置中加入其 API 地址与凭证，再将 `judge` 别名指向它。Judge 只复核已查询的答案，不能代替 SQL 程序校验或分析师批准。复核不通过时 CLI 返回 `needs_review`，不展示答案。

```bash
cd retail-qa-agent
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m retail_qa init
```

先试已审核模板：

```bash
.venv/bin/python -m retail_qa ask --user alice "美国站上周净销售额比前一周变化多少？"
```

再试长尾 SQL 审核：

```bash
.venv/bin/python -m retail_qa ask --user alice "美国站最近30天每天的净销售额和访问次数，按日期列出"
.venv/bin/python -m retail_qa pending
.venv/bin/python -m retail_qa review <上一条返回的request_id>
.venv/bin/python -m retail_qa approve <同一个request_id>
.venv/bin/python -m retail_qa result <同一个request_id> --user alice
```

`review` 会展示 SQL、参数和提问人。分析师也可以将修改后的 SQL 写入一个文件，再运行 `.venv/bin/python -m retail_qa revise <request_id> --sql-file 修改稿.sql`；如果参数也要调整，加 `--params-file 参数.json`。修改后状态仍为待审，必须再次 `review` 和 `approve`。CLI 的 `--user alice/bob/analyst` 仅用于模拟不同身份：Alice 只能看美国站、Bob 只能看日本站、analyst 可审草稿。

试开放式探索：

```bash
.venv/bin/python -m retail_qa ask --user alice "美国站上周净销售额为什么比前一周下降？"
```

它只报告批准指标中的同期变化，并明确提醒相关性不等于因果；没有足够证据时不会给出确定原因。
每次 `ask` 返回 `models_used`，可看到该问题实际调用了哪些模型，以及合计输入、输出和总 Token 用量。模板问题通常调用路由与 Judge 两个模型；长尾问题调用路由与 SQL 草稿模型，批准前不调用 Judge。SQL 审核与执行命令本身不调用模型。

## 设计与边界

| Notion 案例②中的要求 | PoC 落点 |
| --- | --- |
| 先识别意图、指标、条件 | `retail_qa/agent.py` 的路由请求；未明确则返回澄清 |
| 高频问题模板优先 | `retail_qa/catalog.py` 中版本化指标和固定 SQL 模板 |
| 长尾 SQL 审查 | 草稿存入 SQLite；分析师 `review`／`revise`／`approve`；批准内容绑定摘要 |
| 执行时控制数据范围 | 按演示请求人建立仅含获准站点数据的临时只读库；SQL authorizer 只允许指定表、列和函数 |
| 有限探索 | 每步结果返回后，模型只能从剩余的已审核能力中选下一步，最多三次模板查询 |
| 独立答案复核 | 已查询的答案草稿与固定 SQL、参数、结果交给 Judge；未通过则不返回答案 |
| 禁止把目标当成绩 | 所有业务数据、身份和验证结果都标注为模拟 |

SQL 草稿的预校验只执行 `EXPLAIN QUERY PLAN`，不会提前查询数据。批准时再次检查草稿摘要、请求状态和有效期，再使用参数绑定执行。长尾查询限定一条 `SELECT`、允许的表列与函数、最多 100 行，并设置 SQLite 运算预算。模板值参数由后端绑定，模型不能改写模板 SQL。

**生产接入还需要这些条件：** 真实登录与身份映射、请求人与分析师分权；Databricks SQL Warehouse 连接与按真实用户执行；Unity Catalog 的 grants 及必要的行列策略；真实指标目录、模板登记审批、SQL AST 校验与资源成本限制；跨进程的审批状态、审计和结果取回鉴权；评测集与真实数据对账。此 PoC 的 `--user` 参数和本地数据库文件不提供生产级安全隔离。未连接真实 Databricks，也未部署 AgentCore Runtime／Gateway／Policy。官方文档指出 Databricks SQL Connector 3.0+ 支持原生参数化查询，[Unity Catalog](https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/) 还需以真实执行身份配置相应的数据访问控制：[SQL Connector](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector)。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

测试使用假模型，不调用任何 LLM API。覆盖模板路径、越权站点、长尾审批、SQL 拒绝与篡改检测。

另有**真实模型路由评测**，与上述单元测试分开。`evals/router_cases.json` 预先标注 10 道含糊题和 3 道条件明确的对照题；脚本把问题真正发给 `[models].router` 对应的 API，仅检查路由决定，不访问数据库。它会消耗该模型的 Token，用于判断模型是否主动选择澄清、模板或探索：

```bash
.venv/bin/python -m evals.run_router_eval
```

逐题的原始决定和 Token 用量写到 `evals/latest_results.json`。当前评分只比较 `action`，澄清题还要求有非空追问；**追问是否问全了缺失条件需要另外人工检查**。少量题目上的通过率不能代表所有问法的准确率。要比较其他模型，可加 `--model glm`、`--model hy4` 或 `--model deepseek`。
