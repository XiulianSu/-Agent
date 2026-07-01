自用Agent

2026.07.02 新增：按照语义进行重要性打标，保留关键prompt

后续计划，完成下面记忆系统的设计

1. Layer 1：在S08里面已经解决，按照语义重要性进行打标，保留对任务要求有改进的 prompt
	 - 只在当前会话里面有效
	 - 不会对跨对话 Memory产生影响
2. Layer 2：删除原版里面的`project`记忆部分，之保留`user`、`feedback`、`reference memory`
	 - `user`：用来保留个人偏好
	 - `feedback`：用来保留行为约束
	 - `reference`：用来保留具体项目的入口
3. `project` Memory 只有用户在创建文件夹的时候才会创建此记忆，并且支持跨文件夹的记忆调用
  - 参考结构：
```
~/.aoe/memory/
├── global/           # Layer 2 全局记忆
│   ├── MEMORY.md
│   └── *.md
└── projects/         # Layer 3 项目记忆
    ├── auth-refactor/ # 每个 project 有自己的记忆
    │   ├── MEMORY.md
    │   └── *.md
    └── rust-side-project/
        ├── MEMORY.md
        └── *.md
```
