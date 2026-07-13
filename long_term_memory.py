```
Persistent cross-session memory stored as Markdown files.
```

from pathlib import path

MEMORY_DIR = Path.cwd() / ".memory" # 设置目录
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md" # 创建索引文件
MEMORY_TYPES = {"user", "feedback", "project", "reference"} # 创建记忆分类

def configure_long_term_memory(memory_dir: Path) -> None:
    global MEMORY_DIR, MEMORY_INDEX
    MEMORY_DIR = memory_dir
    MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
    MEMORY_DIR.mkdir(parents=True,exist_ok=True)

def _parse_frontmatter(text: str) -> tuple[dict[str,str],str]:
    if not text.startswith("---"):
        return {},text

    parts = text.split("---",2)
    if len(parts) <3:
        return {},text

    meta = {}
    for line in pasrts[1].strip().split("\n"):
        if ":" in line:
            key, value = line.split(":",1)