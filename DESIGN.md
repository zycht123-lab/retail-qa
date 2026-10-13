# 案例②实现研究与设计决策

## 来源与目标

依据 [Notion「中日云岗位面试案例概要」中的案例②](https://app.notion.com/p/671ab5f90c4a4e99af1449f77753c080)。原案例计划约 40 个指标、30 个参数化模板、400 道评测题，数据层是 Databricks Gold Delta 和 SQL Warehouse；所有规模、准确率与故障故事都是模拟设定。本项目先实现可验证的最小闭环，包含 3 个指标、3 个模板、合成数据和本地审查命令。它不把计划中的规模或结果写成已完成。

## 固定流程与模型权限

1. **理解问题。** 模型输出 action、指标能力、站点和期间。程序检查能力 ID、枚举、请求人可见站点、明确站点与模型输出是否一致。缺口走澄清；当前库存、订单状态等不属于本地快照的问题拒绝假答。
2. **模板优先。** 指标定义与 SQL 模板在程序中登记版本。模型只选择能力 ID 和槽位；SQL 由后端固定代码生成，日期按业务时区换算，值经参数绑定。模板路径不需要分析师逐条审 SQL。
3. **长尾草稿。** 仅当模型判断语义明确且模板不覆盖时生成一条 SELECT 和参数。程序在当前请求人的授权数据视图上执行 `EXPLAIN QUERY PLAN`，检查表、列、函数和 SQL 形态；此阶段不取数。草稿保存 request_id、提问人、问题、SQL、参数、理由、内容摘要和创建时间。
4. **分析师审核。** `review` 显示完整 SQL 与参数；`revise` 更新后重新预校验、保留修订摘要并仍处于待审；`approve` 再检查摘要、状态、24 小时有效期，执行并保存结果。修改后不能沿用旧批准。
5. **按请求人范围执行。** PoC 根据演示身份从合成库复制可见站点的记录到本次查询专用内存 SQLite。查询只面对该数据副本，且使用 SQLite authorizer、只读模式、函数白名单、最大返回行数与指令预算。结果只能由原提问人或分析师通过 CLI 查看。
6. **有限开放式探索。** 先执行一个批准的期间对比；每得到一次结果，模型只能在剩余批准指标中选择下一项，最多两次。输出客观同期变化，并提示不能从相关性直接断言因果。
7. **独立模型复核。** 本地默认 Qwen 路由、GLM 写长尾 SQL 草稿、TokenHub 混元复核开放式探索的答案；已审核模板的结果由程序计算并直接返回。Judge 接收原问题、批准的指标口径、固定 SQL、绑定参数、结果和探索答案草稿；输出结构化 `pass/issues/evidence/suggested_next_step`。未通过时不向提问者展示探索答案。Notion 原设计指定 Bedrock Claude Sonnet 4 做 Judge，当前尚未配置该模型；PoC 也未实现自动补查/重规划循环。

模型可决定**路由**及受限的下一步，不能授予自己数据权限、改写已批准模板或批准自己生成的 SQL。SQLite authorizer 是本地演示的第二道检查，不是 Databricks 的权限替代。

## 已查证的生产接入边界

- [Databricks SQL Connector for Python](https://docs.databricks.com/aws/en/dev-tools/python-sql-connector) 3.0+ 支持原生参数化查询。生产模板与长尾查询需要在真实 SQL Warehouse 上使用原生参数，避免拼接用户值；表和字段应来自后端批准的 catalog，不由模型任意指定。
- [Unity Catalog access control](https://docs.databricks.com/aws/en/data-governance/unity-catalog/access-control/) 将对象权限与行列控制分开。真实系统应以可信映射后的用户身份执行，并配置相应 grants、ABAC 或按表行列策略；共享服务主体不自动代表终端用户。
- [Databricks Statement Execution API](https://docs.databricks.com/aws/en/dev-tools/sql-execution-tutorial) 对动态 SQL 和异步结果有明确边界。大查询需超时、取消、并发与结果量控制；`SELECT` 开头和 `LIMIT` 都不足以单独证明费用或数据范围可控。

当前没有 Databricks Workspace 地址、Warehouse HTTP Path、数据 schema 或真实身份提供方，因此没有连接真实数据。若接入这些服务，先把 `retail_qa/data.py` 的本地执行层替换为 Query Service，保留能力目录和审批状态机；再加入服务端验证的用户身份、SQL AST／成本校验、Unity Catalog 权限测试、结果取回鉴权及审计。真实 Key 或 token 不应放进模型提示词。

## 评测建议

先准备确定性题、需要澄清题、长尾审批题、越权题和无数据题，分开计算路由、结果、拒答和审批轨迹。程序断言至少包括：模板问题不生成 SQL；未经分析师确认的长尾 SQL 不执行；权限不随模型输出扩大；修改后旧摘要不能批准；未知表和危险函数被拒绝；不同用户结果不串站点。当前 `tests/test_flow.py` 覆盖了其中可离线验证的核心路径。真实准确率、时延和费用必须在接入数据后实测。
