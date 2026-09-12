# obsidian-ingest

把链接、文章和本地文件整理为 Markdown，按正文主题保存到配置的 Obsidian 知识库。适用于能运行本地 Python 的 Codex、OpenClaw 等助手。

## 目录

```text
skills/obsidian-ingest/  可独立安装的技能
docs/usage.md           安装、配置与使用说明
tests/                  回归测试及图片样本
```

## 安装入口

**只复制 [skills/obsidian-ingest](skills/obsidian-ingest) 目录到宿主的技能目录**，不要把整个仓库作为技能安装。技能运行所需的脚本、配置、依赖清单和 `references/workflow.md` 都在该目录内；`docs/` 与 `tests/` 不参与运行。

需要 Python 3.10+，首次使用前安装依赖并配置已存在的知识库路径。具体步骤见 [使用说明](docs/usage.md)，宿主执行规则见 [SKILL.md](skills/obsidian-ingest/SKILL.md)。图片或扫描页的表格必须实际看图核验后才能入库。

开发测试从仓库根目录运行，命令与测试依赖见 [测试说明](docs/usage.md#测试)。
