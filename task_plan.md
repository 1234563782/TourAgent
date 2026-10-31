# 酒店选择完整性修复计划

## 目标

梳理项目结构与技术栈，追踪主要业务流程，并基于代码、配置和可运行验证识别当前问题与风险。

## 本轮 2026-10-30 Token 优化审查

- [in_progress] 1. 定位模型调用、提示词与上下文构造
- [pending] 2. 量化重复调用、历史膨胀和输出浪费
- [pending] 3. 形成收益/风险/成本分级建议
- [pending] 4. 汇总证据与验证边界

## 阶段

- [in_progress] 1. 项目清单、工具链、入口与运行方式
- [pending] 2. 核心业务模块与调用流程
- [pending] 3. 测试/运行验证与问题复现
- [pending] 4. 风险分级、证据整理与总结

## 约束

- 保留用户已有改动，不执行破坏性 Git 操作。
- 交通类别与住宿档次通过政策配置映射；Jev 只做结构化识别，不作为最终政策判定器。
- 保留用户已有改动，不执行破坏性 Git 操作。
- 区分已观察事实、推测和待验证项。

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `rg` 复杂正则括号未闭合 | 1 | 改用多个固定字符串搜索 |

## 本轮追加事项

- [complete] 确认身份到住宿档次的映射
- [complete] 实现默认值、标准推导、候选推荐和结果展示调整
- [complete] 更新测试并执行验证

## 学生规划默认追加事项

- [complete] 学生推荐默认按三类交通和其他人员住宿
- [complete] 推荐结果增加相关活动条件提示
- [complete] 正式政策核验保留学生活动资格校验
- [complete] 完成相关回归验证

## 2026-10-18 追问全地区触发兜底诊断

- [in_progress] 1. 建立运行时证据：服务、代码版本、Langfuse Trace 与浏览器请求
- [pending] 2. 复现首问→补充北京→追问全地区链路并定位最小失败点
- [pending] 3. 验证 coverage 修复是否已加载，修复剩余运行时/模型契约问题
- [pending] 4. 回归测试、浏览器验证与结果记录

## Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| PowerShell 找不到全局 python | 1 | 改用 `TourAgent/.venv/Scripts/python.exe`；该错误不影响项目代码 |
| 直接访问 Git 仓库被 dubious ownership 拒绝 | 1 | 后续只使用 `git -c safe.directory=...` 的单次只读调用，不修改全局 Git 配置 |

## 2026-10-22 竞品功能基准与缺口分析

目标：基于当前 TourAgent 实现，检索同类差旅/商旅/旅行助手产品的公开能力，比较明显缺失、边界不合理和优先级。

- [complete] 1. 建立当前项目功能清单与产品边界
- [complete] 2. 检索官方竞品能力与公开产品文档
- [complete] 3. 建立对照矩阵并区分缺失/有意不做/不适用
- [complete] 4. 输出带证据和优先级的改进报告

约束：不调用真实付费预订、支付、审批或生产账号；只使用公开资料和当前仓库证据；不修改业务代码。

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| 系统未找到 `python`、`py` 或 `python3`，无法运行 planning skill 的 session-catchup 脚本 | 1-2 | 改为读取现有规划文件并手动追加本轮计划，保留历史内容 |


## 2026-10-22 意图两轴核对

- [complete] 对照当前执行器与 Rasa CALM，判断“三业务目标 + 操作轴 + 收窄 other”能否直接落地
- [pending] 用户尚未授权实现。若继续，先只把取消、恢复、重试从 other 的必建路径分开；保存和读旧结果保持可执行

## 2026-10-22 意图两轴落地

- [complete] 收窄 other：控制类操作离开 other，保存和读旧结果保留
- [complete] 补充路由提示与离线测试，90 项通过

## 2026-10-23 不确定意图

- [complete] 对照官方文档决定未决 Noul 的回退信号
- [complete] 落地 hint 三态并跑离线测试。tests.test_jev、tests.test_slots、tests.test_slot_protocol 共 91 项通过，未调用真实 JEV
