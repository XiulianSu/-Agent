# 自用 Agent

基于 OpenRouter 的终端编码 Agent（S08 模块化版本）。

## 功能

- **工具调用**：bash、读写文件、glob、todo、子 Agent、技能加载
- **上下文压缩**：四层压缩管线（tool result budget → snip → micro compact → 自动/响应式摘要）
- **语义保留**：按重要性打分，压缩时保留关键 prompt 与约束
- **Hook 系统**：权限拦截、日志、会话统计
- **技能加载**：扫描 `skills/` 目录，按需 `load_skill` 注入完整 SKILL.md

## 项目结构

```
├── main.py              # 入口：Agent 循环、压缩、子 Agent、技能扫描
├── tools.py             # 工具实现与 OpenAI function schema
├── display.py           # 终端输出与 ANSI 样式
├── state.py             # 会话状态（消息、todo、计数器）
├── memory.py            # 三层内存压缩管线（纯函数）
├── long_term_memory.py  # 长期记忆（开发中，尚未接入 main）
├── requirements.txt
└── .env.example
```

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入 OPENROUTER_API_KEY

python main.py
```

在目标工作目录下运行；Agent 会将当前目录作为工作区。可选：在工作目录创建 `skills/<name>/SKILL.md` 以启用技能加载。

## 记忆系统规划

1. **Layer 1（已实现）**：会话内语义打标，压缩时保留关键 prompt
2. **Layer 2（规划中）**：跨会话 `user` / `feedback` / `reference` 记忆
3. **Layer 3（规划中）**：按项目文件夹的 `project` 记忆

参考结构：

```
~/.aoe/memory/
├── global/
│   ├── MEMORY.md
│   └── *.md
└── projects/
    ├── auth-refactor/
    │   ├── MEMORY.md
    │   └── *.md
    └── rust-side-project/
        ├── MEMORY.md
        └── *.md
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `OPENROUTER_API_KEY` | OpenRouter API 密钥 | 必填 |
| `OPENROUTER_MODEL` | 模型 ID | `moonshotai/kimi-k2.6` |
| `OPENROUTER_TIMEOUT` | 请求超时（秒） | `120` |
| `OPENROUTER_TRUST_ENV` | 是否使用系统代理 | `1` |
