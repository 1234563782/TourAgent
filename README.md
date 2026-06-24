# AI 多智能体差旅规划系统

> 让差旅更简单 🌏 — 基于 AgentScope 的多智能体协作框架，覆盖行程规划、偏好管理、知识问答、信息查询等全场景商旅需求

[![Python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![AgentScope](https://img.shields.io/badge/AgentScope-1.0.16-green)](https://github.com/agentscope-ai/agentscope)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

一个智能差旅规划系统，通过意图识别 + 多智能体协作，将自然语言需求转化为结构化的差旅方案。系统内置 6 个专业子智能体，同时具备记忆系统、RAG 知识库、熔断器、懒加载等工程化能力。

---

## 目录

- [核心特性](#核心特性)
- [系统架构](#系统架构)
- [智能体列表](#智能体列表)
- [快速开始](#快速开始)
- [使用示例](#使用示例)
- [项目结构](#项目结构)
- [配置说明](#配置说明)
- [技术栈](#技术栈)
- [许可证](#许可证)

---

## 核心特性

| 特性                   | 说明                                                                     |
| ---------------------- | ------------------------------------------------------------------------ |
| **意图识别**     | 两段式推理（推理过程 + JSON 决策），支持多意图识别、模糊消歧、Query 改写 |
| **多智能体协作** | 6 个专业子智能体，按优先级并行/顺序执行，去中心化调度                    |
| **记忆系统**     | 短期记忆（会话上下文）+ 长期记忆（用户偏好、历史行程、跨会话持久化）     |
| **RAG 知识库**   | Milvus Lite 向量检索 + BGE 中文嵌入，支持企业知识库问答                  |
| **插件化架构**   | 基于 `.claude/skills` 的懒加载机制，添加新 Agent 无需修改核心代码      |
| **工程健壮性**   | 熔断器 + 指数退避重试 + 可重试错误判断 + LLM 健康检查                    |
| **美观 CLI**     | 基于 Rich 的终端交互界面，支持 Markdown 渲染、表格、进度条               |

---

## 系统架构

```
用户输入
  │
  ▼
┌──────────────────────────────────────┐
│         IntentionAgent              │
│   意图识别 + 推理 + Query 改写        │
└──────────────┬───────────────────────┘
               │ agent_schedule (JSON)
               ▼
┌──────────────────────────────────────┐
│       OrchestrationAgent            │
│   按优先级调度，并行/顺序执行          │
└──────────────┬───────────────────────┘
               │
     ┌─────────┼─────────┐
     ▼         ▼         ▼
┌─────────┐ ┌─────────┐ ┌─────────┐
│Priority 1│ │Priority 1│ │Priority 1│  ← 并行执行（信息收集）
│ memory  │ │ event   │ │preference│
│ _query  │ │collection│ │         │
├─────────┤ ├─────────┤ ├─────────┤
│ rag     │ │ info    │ │         │
│knowledge│ │ _query  │ │         │
└────┬────┘ └────┬────┘ └────┬────┘
     │           │           │
     └───────────┼───────────┘
                 ▼
        ┌───────────────┐
        │  Priority 2   │  ← 顺序执行（依赖 Priority 1 结果）
        │  itinerary    │
        │  _planning    │
        └───────────────┘
                 │
                 ▼
         结果聚合 & 展示
```

### 记忆系统

```
┌────────────────────────────┐
│   短期记忆 (ShortTerm)       │
│   • 会话级对话历史 (10 轮)    │
│   • 自动淘汰旧消息            │
│   • 上下文理解 & 消歧         │
└────────────┬───────────────┘
             │
             ▼
┌────────────────────────────┐
│   长期记忆 (LongTerm)        │
│   • 用户偏好持久化            │
│   • 历史行程记录              │
│   • 跨会话 LLM 总结          │
│   • 智能筛选相关历史          │
└────────────────────────────┘
```

---

## 智能体列表

| 智能体                       | 功能       | 触发条件                                       |
| ---------------------------- | ---------- | ---------------------------------------------- |
| **event-collection**   | 事项收集   | 提取行程信息（出发地、目的地、日期、目的）     |
| **preference**         | 偏好管理   | 用户偏好设置（常驻地、交通、酒店、航司等）     |
| **itinerary-planning** | 行程规划   | 生成每日详细行程（景点、交通、餐食、注意事项） |
| **information-query**  | 信息查询   | 联网搜索（天气、目的地介绍、最新资讯）         |
| **rag-knowledge**      | 知识库问答 | 查询企业差旅标准、报销政策、预订指南等         |
| **memory-query**       | 记忆查询   | 查询用户历史行程、过往偏好、去过的城市         |

**优先级规则**：Priority 1 的 Agent（信息收集）并行执行，Priority 2 的 Agent（行程规划）依赖 Priority 1 的结果顺序执行。

---

## 快速开始

### 环境要求

- Python 3.9+
- pip

### 安装

```bash
# 克隆仓库
git clone https://github.com/shaojian0514/TourAgent.git
cd TourAgent

# 安装依赖
pip install -r requirements.txt
```

### 配置

编辑 `config.py`，填入你的 LLM API 信息：

```python
LLM_CONFIG = {
    "api_key": "your-api-key",          # 替换为你的 API Key
    "model_name": "your-model-name",    # 替换为模型名称
    "base_url": "https://api.openai.com/v1",  # OpenAI 兼容端点
    "temperature": 0.7,
    "max_tokens": 8192,
}
```

### 初始化知识库（可选）

```bash
cd .claude/skills/ask-question/script
python init_knowledge_base.py
```

### 启动

```bash
python cli.py
```

### 健康检查

```bash
python cli.py health
```

---

## 使用示例

### 规划行程

```
> 我要从上海去北京出差，2月28日出发，3月3日返回，喜欢住汉庭

🤖 调用智能体: 事项收集 ✓, 偏好管理 ✓

✓ 已收集行程信息
  • 出发地: 上海
  • 目的地: 北京
  • 出发日期: 2026-02-28
  • 返程日期: 2026-03-03

✈️  上海 → 北京 差旅计划
时长: 4天3晚

第 1 天
  08:00 - 从上海出发
  10:30 - 抵达北京首都机场
    🚇 地铁机场线
  12:00 - 入住汉庭酒店（北京朝阳区）
  ...

📌 注意事项
  • 建议提前预订酒店
  • 北京早晚温差大，注意保暖
```

### 查询知识库

```
> 北京的住宿标准是多少

根据公司差旅标准，北京地区住宿标准如下：
- 普通员工：500元/晚
- 部门经理：800元/晚
- 总监及以上：1200元/晚
参考来源：差旅管理制度 v2.3
```

### 查询历史

```
> 我上次去北京是什么时候

根据您的历史记录，您最近一次去北京是 2025年11月：
• 上海 → 北京 (2025-11-15) - 商务会议
• 上海 → 北京 (2025-09-02) - 客户拜访
```

### 命令列表

| 命令            | 说明                |
| --------------- | ------------------- |
| `help`        | 显示帮助信息        |
| `status`      | 查看当前状态和记忆  |
| `health`      | 检查 LLM 服务可用性 |
| `clear`       | 清空短期记忆        |
| `history`     | 查看历史行程        |
| `preferences` | 查看用户偏好        |
| `exit`        | 退出程序            |

---

## 项目结构

```
TourAgent/
├── cli.py                          # CLI 交互主入口（Rich 终端界面）
├── config.py                       # 全局配置（LLM / 系统 / RAG / 容错）
├── config_agentscope.py            # AgentScope 初始化
├── requirements.txt                # Python 依赖
│
├── agents/                         # 智能体模块
│   ├── intention_agent.py          #   意图识别智能体（推理 + 决策 + Query 改写）
│   ├── orchestration_agent.py      #   协调器智能体（调度 + 并行执行 + 结果聚合）
│   └── lazy_agent_registry.py      #   懒加载注册器（插件化发现机制）
│
├── context/                        # 记忆系统
│   ├── memory_manager.py           #   记忆管理器（统一 API）
│   ├── short_term_memory.py        #   短期记忆（会话级，10 轮对话）
│   └── long_term_memory.py         #   长期记忆（跨会话持久化）
│
├── utils/                          # 工具模块
│   ├── circuit_breaker.py          #   熔断器（Closed → Open → HalfOpen）
│   ├── llm_resilience.py           #   重试退避 + 可重试错误判断 + 健康检查
│   ├── skill_loader.py             #   Skill 描述加载器
│   └── json_parser.py              #   JSON 解析工具
│
├── .claude/skills/                 # 插件化 Skill 定义
│   ├── ask-question/               #   知识库问答（RAG + Milvus）
│   │   ├── SKILL.md
│   │   ├── data/documents/         #       企业知识文档（8 个 txt）
│   │   └── script/agent.py
│   ├── event-collection/           #   事项收集
│   ├── preference/                 #   偏好管理
│   ├── plan-trip/                  #   行程规划
│   ├── query-info/                 #   信息查询
│   └── memory-query/               #   记忆查询
│
├── data/                           # 数据目录
│   ├── memory/                     #   用户记忆持久化存储
│   └── models/                     #   嵌入模型（bge-small-zh-v1.5）
│
└── tests/                          # 测试套件
    ├── test_cli_qa.py
    ├── test_intention_agent.py
    ├── test_orchestration.py
    ├── test_memory_system.py
    └── results/                    #   测试结果
```

---

## 配置说明

### LLM 配置 (`config.py`)

```python
LLM_CONFIG = {
    "api_key": "API_KEY",          # API 密钥
    "model_name": "Model_Name",    # 模型名称
    "base_url": "...",             # API 端点（OpenAI 兼容）
    "temperature": 0.7,            # 生成温度
    "max_tokens": 8192,            # 最大 token 数
}
```

### 系统配置

```python
SYSTEM_CONFIG = {
    "enable_llm": True,            # 启用 LLM 模式
    "log_level": "INFO",           # 日志级别
    "max_retries": 3,              # 最大重试次数
    "timeout": 60,                 # 请求超时（秒）
}
```

### 容错配置

```python
RESILIENCE_CONFIG = {
    "max_retries": 3,                      # 单次请求最大重试
    "retry_base_delay_sec": 1.0,           # 重试退避基数
    "retry_max_delay_sec": 30.0,           # 重试退避上限
    "circuit_failure_threshold": 5,        # 连续失败多少次后熔断
    "circuit_recovery_timeout_sec": 60.0,  # 熔断后恢复时间
    "circuit_half_open_successes": 2,      # 半开状态下连续成功多少次关闭
    "health_check_timeout_sec": 10.0,      # 健康检查超时
}
```

---

## 技术栈

| 技术                                                            | 用途              |
| --------------------------------------------------------------- | ----------------- |
| [AgentScope](https://github.com/agentscope-ai/agentscope)          | 多智能体框架      |
| OpenAI 兼容 API                                                 | LLM 推理          |
| [Milvus Lite](https://milvus.io/)                                  | 向量知识库（RAG） |
| [BGE-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) | 中文嵌入模型      |
| [Rich](https://github.com/Textualize/rich)                         | 终端 UI           |
| [DuckDuckGo](https://duckduckgo.com/)                              | 网络搜索          |
| JSON 文件存储                                                   | 用户记忆持久化    |

---

## 许可证

MIT License
