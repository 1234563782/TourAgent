# 真实模型记忆试跑：2026-09-21

## 执行与结果

- 使用项目现有模型配置，未修改密钥、模型或依赖。
- 入口：`scripts/verify_preference_dialogues.py`。
- 数据：`tests/preference_dialogue_cases.json`，7 条已有夹具，各执行一次。
- 使用真实 LangGraph 和真实模型、临时 TravelMemory、InMemorySaver；供应商禁用，不写正式数据库，关闭 Langfuse 上报。
- 脚本将模型超时限制为最多 30 秒、slot_max_attempts 设为 1、recursion_limit 设为 20；不等同于生产默认配置。
- 成功运行退出码 0：7/7 通过脚本的确定性检查。
- 证据：`.verification/preference-dialogues-0b721b4534bc48ba9253b9adf1bd3081/results.json`。
- 首次沙箱内连接失败，停止后续用例；经权限流程在沙箱外重跑。首次证据保留于 `.verification/preference-dialogues-6c8178ef974c4c02a95f895cfe780010/results.json`，属于环境受阻，不作为模型语义失败。

| 场景 | 观察结果 | 确定性检查 |
| --- | --- | --- |
| explicit_add | 保存飞机靠过道偏好 | 通过 |
| other_person | 未将同事偏好保存为本人偏好 | 通过 |
| temporary | 本次高铁靠窗未写入长期记忆 | 通过 |
| hypothetical | 假设表达未写入长期记忆 | 通过 |
| candidate | 自动保存酒店隔音偏好 | 通过 |
| explicit_update | 酒店改为带健身房，保留座位靠过道 | 通过 |
| explicit_delete | 删除酒店偏好，保留座位靠过道 | 通过 |

## 评分范围

当前脚本核对记忆记录数量、指定内容子串、部分记录类型，及中断、槽位错误和工具错误状态。不是完整语义评分：未校验所有来源字段、短期槽位是否真正保留、跨轮及跨用户隔离、持久化、模型稳定性或回答质量。未采集完整调用次数、token、费用和延迟。

本次是未完成人工审核的开发试跑，不改写审核决定；不能把结果解释为 88 条候选通过或整个 Agent 准确率 100%。

## 回答质量观察

- `explicit_delete`：删除正确，但回复“已记住你的偏好。”，没有准确确认删除。记录为表达待改进项，不修改既有状态检查结果。
- `hypothetical`：没有误存，但解释暴露工具名称及内部流程；是否计为失败需要先明确面向用户的回答规范。
- `temporary`：回复声称已记录本次需求；当前结果只证明长期记忆为空，不能据此确认短期状态已保存。

## 下一步

1. 将逐轮 state/revision、记忆前后快照和调用指标接入回放记录，补齐短期状态断言。
2. 补齐旧结果失效、歧义目标场景的可验证前置条件，未触发时单独记录。
3. 继续政策及多轮开发试跑；人工审核完成前不发布正式准确率或将草稿转为 gold。
