# 发现

- company_id 并非只有名称：用于多张表的主外键、查询条件、checkpoint thread_id、遥测匿名引用、浏览器选中会话键和政策包摘要。
- 原 resources 已只有全局执行槽位；保留该设计，配额维度改 school/user。
- 新 thread_id 为 user:session；三张 checkpoint 表需同时改写，保留 namespace、parent_checkpoint_id、blob、pending writes。
- 多个历史组织标识合并可能产生账号/业务数据冲突，工具拒绝自动合并，不删除或挑选记录。
- policy bundle 删除字段会改变摘要；须显式转换文件并映射旧/新摘要，防止已审阅版本导入冲突。
- 仓库在本任务前已有多个未提交改动（含并发、UI、发票附件）；基于当前内容修改，不能用 HEAD 直接回滚。

## 2026-10-30 Golden dataset 初步证据
- benchmark_golden_dataset.json 仅三条，均指向第六条。run_ragas_eval.py 直接用 ground_truth 拼接 answer 和 contexts，未执行真实检索或回答；target_clauses 未参与该脚本评分。
- 现存 CSV 来自同类拼接数据，不能证明 Agent 质量。下一步核对政策原文的适用前提、席别和标注。

### 审查结论
- 静态校验通过：JSON 可解析、3 条必填字段和非空条款存在、ID/问题无重复；三题全部为第六条。通过项目 .venv Python 的 zipfile/XML 直接核对 DOCX 文本与交通表，不修改原文件。
- CASE-01 基本符合院士一类高铁商务座上限，但金标扩展到非高铁席别，应改为聚焦最高标准并允许低等级等价回答。
- CASE-02 二类通常经济舱正确，绝对不得措辞忽略第六条（一）确因工作需要随行一人的国内例外；建议限定无其他适用待遇/随行情形。
- CASE-03 提问未指定全列软席火车，金标却先无条件可以，存在条件不足；应保留夕发朝至、全列软席、普通软席三项前提，其他情况不能据此直接允许。
- target_clauses 仅为条号，无文档版本/稳定证据标识且不被 run_ragas_eval 使用；evals.dataset 的 JSONL loader 也不加载该 JSON。需为真检索/真生成产物、版本与审核状态建立独立契约。
- 缺少住宿独立身份、附表金额/旺季/地区、缺失证据、多轮 all_regions、引用连续性等覆盖；建议先修评分输入与条件标注，再扩题。仅是当前三题文件的覆盖判断，不代表项目其他评测无这些场景。
- 未运行 Ragas/API/数据库；现有 CSV 第三题 faithfulness=0.5 仅反映模拟样本的裁判输出，不能解释为 Agent 错误率。
- 查询错误记录：PowerShell 中 rg 对 business_travel/university* 路径报 os error 123，后续改为具体目录检索；无业务文件修改。

## 2026-10-30 30题版本复审
- 当前30题、22个不同条款，必填类型与ID/问题唯一性检查通过；已用项目venv标准库zipfile/XML阅读DOCX全文核对。
- CASE-14增加原文未规定的租车（自驾车）审批表名称；CASE-29漏掉因客观原因无法取得合作方有效证明这一替代说明的前提。
- CASE-25只保留租车费用分类与合同门槛，未保留考察/调研/测试监测、地理环境和当地条件限制、事前审批及合规票据的适用条件；作为报销结论需完善。
- CASE-03仍未说明全列软席就先回答可以；CASE-02仍绝对否定头等/公务舱而未限定普通二类待遇及例外。CASE-08应先区分会议期间与在途期间，避免不能与后文可领在途补助矛盾。
- CASE-28的600元算术正确，但特区标准不是本原文术语；计算题宜明确5个符合发放条件自然日。
- 评分脚本仍将金标同时构造回答和上下文，target_clauses不评分；CSV仍只有3行。当前30题缺少身份独立、住宿附表金额/旺季/地区、缺证据和多轮覆盖案例，以及版本/证据标识。
- 仅静态核查，未调用模型/数据库，未修改金标或评分脚本。工具编排曾因JS字符串语法失败（未执行命令），后改为简单PowerShell读写。

## 2026-10-30 31题版本与新版评分器
- 已修复：CASE-02/03条件、CASE-08期间区分、CASE-14无来源审批表、CASE-25题目前提、CASE-28自然日、CASE-29替代证明条件；新增CASE-03-CONTRAST。当前31题。
- run_ragas_eval不再把ground_truth传给生成器或构造上下文；真实DOCX解析和LLM调用替代模拟评分，此问题已修正。
- 仍非生产RAG端到端：脚本自建set(question)字符匹配Top3+最多5条扩展，ChatOpenAI单次回答；未运行PostgresPolicyStore或explain_policy的原文引用校验/Reviewer/补检索/partial等链路。
- 已执行：项目venv，AST抽取当前load_document_chunks和retrieve_real_contexts函数，调用真实parse_docx，无模型/DB/network。31题字段/唯一性通过，解析51片段，本进程29/31命中；CASE-07未召回第二十条，CASE-30未召回第三十六条。该结果不能当作生产RAG分数。
- closure_ids为set且任意迭代后截断至5，会让跨进程补充片段顺序不稳定；不保证条件闭包完整，也未披露未装入关联条款。any条号命中仅表示至少一条条款存在，不等于召回完整或正确档次/款项。
- faithfulness/context_recall/context_precision缺少最终答案正确性检查，需增加针对结论/条件/数值/身份边界的断言或独立正确性评价；同模型可用于初步评测但应记录并抽样人工复核。
- 脚本policy_key=hfut/version=2026/effective_from=2016-09-01与manifest hfut_travel/2026-project-v1/2026采用期不一致，未读取住宿附表。CSV仍为历史3题模拟结果。
- CASE-04第二人不得按一类标准乘坐应收窄为不能仅凭随行身份享受，同乘人员若本身一类需遵循本人待遇。CASE-05非特区表述未完整明示青海/新疆120，建议只答西藏或完整列举。
- 本次未修改业务、评分脚本或数据集。工具编排一次JS语法失败未执行命令，改用简化PowerShell定位。

## 2026-10-30 生产RAG评分适配复审
- 当前31题/22条款结构和唯一性通过；CASE-04/05已修，上轮字符检索和set截断已删除，现调用PolicyStore.from_settings/create_model/explain_policy，无gold泄漏。此前独立检索29/31结论不适用于现脚本。
- AST抽取并执行当前extract_answer_text和check_target_clauses（无联网）：partial同时存在claims与limitations时仅返回claims；clarify.question被变为通用依据不足；条款元数据非目标但正文提到第六条也返回True；双目标只命中其一返回True。
- 建议使用统一render_policy或保留claims/limitations/next_step/question的专用评测结构，并独立保留引用校验指标。条款命中应基于文档版本/条款元数据和更细证据ID，分别定义any/all覆盖。
- 所有题强行policy_query other/general且facts/request_context缺省，复用了政策内核但非graph的真实路由/填槽/账号身份链路。CASE-28个人总额计算与ANSWER_PROMPT不执行金额计算的职责有偏差，宜移至规则核验或计算用例。
- docstring声称Direct correctness verification，但metrics仍只有faithfulness/context_recall/context_precision，无答案正确性评分/逐例期望状态。Answer Rate非正确率；所有其他状态被汇总为证据不足，掩盖unavailable/conflict/incomplete/version_changed。
- 当前CSV共3行，全answered，已含真实上下文与生成答案；其问题/参考答案均不与当前31题列表一致，不是本版全量验收。不可再称这份CSV为模拟，但无manifest也无法确认运行版本。
- 报表缺case_id、数据集哈希、政策版本摘要/配置和诊断；异常时未持久化已执行案例，judge异常仅打印可能零退出，NaN均值默认跳过亦需记录有效样本分母。
- 本轮仅离线结构、helper反例和产物校验，无新模型/DB调用，未修改题集/脚本/业务代码。搜索正则因括号转义失败已改SimpleMatch。

## 评测修复：失败基线
- tests.test_ragas_benchmark 的4项最小回归全部按预期失败：partial丢limitations、clarify丢question、正文提条号误命中、多目标只中一条误通过。
- 修复选择：评测专用完整回答投影（不把原文摘录混入待评分结论）；按元数据归一化条号并分别记录any/all；逐题结构化裁判检查结论和必要条件；保持政策内核业务代码不变。

## 2026-10-30 评测修复完成
- run_ragas_eval.py 重写为可离线导入的政策内核评测器；31题补显式policy_query/facts/expected_statuses/required_points/期望政策版本。CASE-28调整为青海日标准和自然日规则，避免要求政策解释内核计算个人金额；CASE-25补合规票据。
- 修复partial限制及下一步丢失、clarify提问丢失；条款命中仅检查归一化元数据，分别记录any/all/missing，正文提及不算召回。
- 独立结构化正确性裁判检查结论、逐项必要条件、实质错误，gold仅供评分；通过门槛同时检查预期状态、政策版本和全部目标条款。状态、未执行数量和有效/缺失评分分母分开报告；NaN导致评分阶段失败且保留可用分数。
- 每次输出新目录，raw.jsonl先于裁判落盘；错误时保留cases/scored/report/summary/manifest。记录数据集/runner/生产核心代码哈希、配置白名单和实际政策版本摘要；跨题发布变化终止，不混合评分。无API Key/连接串日志，保留原CSV不覆盖。
- 初始4项回归按预期失败；修复后21项专项+5项原Runner共26项通过。31题--validate-only、AST/空白、git diff --check通过；现有ragas 0.4.3/datasets 5.0.1导入及三指标加载通过，无安装依赖。
- 扩展test_eval_expected时27项中26通过、1错误：旧预期来源tests/test_slot_protocol.py的摘要已失配。该文件本轮未修改，不更新旧证据摘要以隐藏错误。
- 沙箱内真实模块导入受_ssl DLL错误影响，经用户批准沙箱外运行离线测试和可选依赖导入；所有外部模型/DB边界在测试中替换，未执行31题联网基准。
- 编排错误：一次JS引号及一次apply_patch同路径删建失败，均未改变目标；改用原地写入完成。
