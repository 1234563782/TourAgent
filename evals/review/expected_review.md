# 全量测评预期草稿（待人工审核）

88 条逐轮预期见 `expected_drafts.jsonl`；本表仅供审核导航，不是 gold，也未执行真实 Agent。
历史观察及夹具期望是来源建议，政策金额、故障注入与初态须人工核实。

| ID | 轮次 | 首轮目标草稿 | 重复 / 待确认 |
| --- | ---: | --- | --- |
| `history-012a4101392a9780d7f7` | 6 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-0b8cfbe4b416f38491e6` | 1 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-18798db82be13cb38475` | 9 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-1bab37903c1367cd2f3c` | 1 | 区分已提交事实与本轮拟更正事实，状态以持久化快照为准 | needs_review |
| `history-24a8e809e3401e4f672b` | 12 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-3a329f2d4929d2fa6022` | 3 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-65ffe5c148e48956c3fa` | 1 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-731f22e2562849dc929c` | 11 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-8eb7936ee13c94758d6d` | 3 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-acc2d4a42938e58efc99` | 2 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-d511bdb14cc73543d9db` | 11 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-e29efcfc9d15cc7d23cd` | 7 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-e3ffafbb1ff999d6c004` | 1 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `gap-ambiguous-target` | 2 | 保留两趟出差分别去南京和苏州的意图 | revise_before_trial |
| `gap-location-conflict` | 1 | 识别广东与南京冲突并澄清 | review_existing_draft |
| `gap-memory-delete` | 2 | 持久化本人飞机靠窗偏好 | review_existing_draft |
| `gap-memory-scope` | 3 | 保存本人长期高铁靠窗偏好 | review_existing_draft |
| `gap-review-rejection` | 1 | 终止且rag.status=insufficient | revise_before_trial |
| `gap-season-missing-date` | 1 | 追问入住或费用日期 | review_existing_draft |
| `gap-shared-room` | 1 | 解释附表口径为元/人·天 | revise_before_trial |
| `gap-stale-result` | 3 | 保存明确给出的南京和2026-10-01 | revise_before_trial |
| `history-029ea45150d278c1c3d0` | 7 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-0392cc95f9da193823bf` | 1 | 拒绝无授权审批、支付或订票，保留普通帮助边界 | needs_review; 同输入：history-cbc7908ecfe35691e085 |
| `history-04fd3061683077deb3af` | 1 | 识别天气查询并保留用户指定地点与日期 | needs_review; 同输入：history-0f963aedc7b82ea12460 |
| `history-092e275ca246fd681599` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-1cb553af42c3ee76ab27 |
| `history-0a67477b805624cf7c65` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-0f963aedc7b82ea12460` | 1 | 识别天气查询并保留用户指定地点与日期 | needs_review; 同输入：history-04fd3061683077deb3af |
| `history-136da2fab7c580ed5f44` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-977f9715fb6f79b56570 |
| `history-16a8f294367d52ecad3c` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-1b713ebf7a8af400e64a` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-ddb1a7be9cdebce77167 |
| `history-1cb553af42c3ee76ab27` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-092e275ca246fd681599 |
| `history-1d7cfba8ee4ffd8b7769` | 2 | 区分本人的持久偏好与本次偏好、他人偏好及假设 | needs_review; 同输入：history-4a45fa86aae90985c162 |
| `history-200448d07bcb1e16e5f5` | 1 | 保存本人长期飞机座位靠过道偏好 | review_existing_draft |
| `history-29d1e6bbb9e5e6b3aea2` | 1 | 识别天气查询并保留用户指定地点与日期 | needs_review |
| `history-29f0c1b5bd6a36038296` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-f78483bb433972bde0f1 |
| `history-2b2920e6c0764b8bcfb7` | 5 | 只处理本轮所指酒店事项，保留明确给出的城市和日期条件 | needs_review |
| `history-2c5f4e50187d7d484bcd` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-2f16b84929debdd47fef` | 2 | 区分本人的持久偏好与本次偏好、他人偏好及假设 | needs_review; 同输入：history-c280dcf0041a77b66f7c |
| `history-301f9185bfedc42c110f` | 1 | 仅删除酒店偏好 | review_existing_draft |
| `history-3494ba7cf949c3945ff8` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-c304274f31395ebc9b81 |
| `history-36ef300004f45d492206` | 2 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-399d2f11cff2b45fd105` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-4789a386bc6b60ef6527` | 6 | 区分已提交事实与本轮拟更正事实，状态以持久化快照为准 | needs_review |
| `history-4a45fa86aae90985c162` | 2 | 区分本人的持久偏好与本次偏好、他人偏好及假设 | needs_review; 同输入：history-1d7cfba8ee4ffd8b7769 |
| `history-4aebe845a4fb97e4bd15` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-4dcf4fce49b6744e97bf` | 6 | 区分已提交事实与本轮拟更正事实，状态以持久化快照为准 | needs_review |
| `history-502fa4b627459dfbaeca` | 2 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-513f891563ccf9ba233d` | 21 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-553317cbd14e4232e3bc` | 3 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-566aa2cae3caad4f35cd` | 1 | 酒店偏好变更为带健身房 | review_existing_draft |
| `history-5aaf4ec52b11dd2db986` | 1 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-6336dc6f0abfea58ef16` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-a1a10e5749d21b5aaede |
| `history-643fbdb40819d95f6f9c` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-9204b9fa582e71fbf0c1 |
| `history-68a55c1e712b4c837cae` | 1 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-6c012f57955c6de4d105` | 1 | 保存本人长期酒店隔音偏好 | review_existing_draft |
| `history-7262fd28e290249b955c` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-726f06e575336a8faeae` | 2 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-72a135050a1d7877ee3c` | 1 | 按本轮显式目标识别事项，缺少执行条件时提出定向追问 | needs_review; 同输入：history-aef5b12c351346307d4e |
| `history-7c909afdc6d4488bf150` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-7e837a70f7afe20800cd` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-f6e7265a59f808e955a2 |
| `history-821ed9bdbae34902b5c1` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-869be6499dcd9989dc0e` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-8c031c85412adb4ead9e` | 2 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-cbe372401ccebb8897a1 |
| `history-9204b9fa582e71fbf0c1` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-643fbdb40819d95f6f9c |
| `history-95a8292fcad360bce832` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-cfeb7d704d22c01e44f9 |
| `history-977f9715fb6f79b56570` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-136da2fab7c580ed5f44 |
| `history-9a1646fa9ad0e25d138e` | 1 | 识别为假设且用户否认实际保存意图 | review_existing_draft |
| `history-a1a10e5749d21b5aaede` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-6336dc6f0abfea58ef16 |
| `history-a808397e0db5b32bbd18` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-a8692962dc0a5639b3c3` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-aef5b12c351346307d4e` | 1 | 按本轮显式目标识别事项，缺少执行条件时提出定向追问 | needs_review; 同输入：history-72a135050a1d7877ee3c |
| `history-b26953dd8953f84f979f` | 1 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-bff28e5010d60214bf29` | 1 | 保留同事为陈述主体 | review_existing_draft |
| `history-c10484d4e37482db7956` | 14 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-c280dcf0041a77b66f7c` | 2 | 区分本人的持久偏好与本次偏好、他人偏好及假设 | needs_review; 同输入：history-2f16b84929debdd47fef |
| `history-c304274f31395ebc9b81` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-3494ba7cf949c3945ff8 |
| `history-cbc7908ecfe35691e085` | 1 | 拒绝无授权审批、支付或订票，保留普通帮助边界 | needs_review; 同输入：history-0392cc95f9da193823bf |
| `history-cbe372401ccebb8897a1` | 2 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-8c031c85412adb4ead9e |
| `history-cbf2d14c5bfd6b4cd4fa` | 17 | 按本轮明确的费用日期和地点检索适用版本的政策条款；解释与证据一致 | needs_review |
| `history-cfeb7d704d22c01e44f9` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-95a8292fcad360bce832 |
| `history-d1586ff8ac51f36f8aa0` | 2 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |
| `history-d79ac751f0e036456409` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-e16c738155f6c6b66708 |
| `history-ddb1a7be9cdebce77167` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-1b713ebf7a8af400e64a |
| `history-e16c738155f6c6b66708` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-d79ac751f0e036456409 |
| `history-e61f086d67a7a01796c1` | 1 | 将靠窗限定为本次高铁意愿 | review_existing_draft |
| `history-f6e7265a59f808e955a2` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-7e837a70f7afe20800cd |
| `history-f78483bb433972bde0f1` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review; 同输入：history-29f0c1b5bd6a36038296 |
| `history-ff3ced21fbe9b0f36cb6` | 1 | 将本轮事实、假设与未知条件分别处理，只有证据充分才给确定结论 | needs_review |

## 审核顺序

1. 优先核对 4 条 `revise_before_trial` 的前置条件和政策原文。
2. 对 16 组相同输入核实来源环境后选择代表例，避免重复样例扭曲准确率。
3. 逐轮确认 `must/must_not`、观察点、来源夹具字段与政策版本，再手动审批。
4. 批准后才能编写回放适配器并试跑；不得将此文件直接上传当金标。
