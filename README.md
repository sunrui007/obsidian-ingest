# obsidian-ingest

把链接、文章和本地文件整理为 Markdown，按正文主题保存到配置的 Obsidian 知识库。适用于 Codex、OpenClaw 等能执行本地 Python、读取技能说明的助手；Obsidian 无需运行。

对助手说“收藏这个链接”“把这篇文章存起来”“保存这个 PDF”即可触发。仅要求阅读、总结或翻译不会自动入库；指定其他保存位置时遵从你的要求。

## 安装与配置

需要 Python 3.10+。将整个目录安装为宿主技能，在技能目录中安装依赖：

```sh
python -m pip install -r requirements.txt
```

也可安装到独立虚拟环境，或用 `python -m pip install --target .deps -r requirements.txt` 安装到技能目录；脚本会自动加载 `.deps`。源码不包含依赖包、语音模型或知识库内容。

OpenClaw 可使用 `~/.openclaw/skills/obsidian-ingest`，或工作区的 `skills/obsidian-ingest`。目录规则见 [OpenClaw 官方技能文档](https://docs.openclaw.ai/zh-CN/tools/skills)。容器或远程宿主需要能够访问实际挂载的知识库路径。

`config.json` 中的 `./vault` 只是便携占位路径，使用前改为**已经存在的知识库目录**。在技能目录中执行，例如：

```powershell
# Windows
python scripts/ingest.py config set --vault-path "D:\Notes\Obsidian"
```

```sh
# Linux / macOS
python3 scripts/ingest.py config set --vault-path "$HOME/Notes/Obsidian"
```

再检查配置与运行环境：

```sh
python scripts/ingest.py config show
python scripts/ingest.py doctor
```

修改配置不会搬迁旧笔记或附件。相对路径以配置文件所在目录为基准。若不想把本机路径写进受版本控制的模板，复制 `config.json` 为 `config.local.json`，在以上命令的子命令之前加 `--config config.local.json`，并让宿主设置环境变量 `OBSIDIAN_INGEST_CONFIG` 指向该文件。

## 能处理什么

- 文章网页、微信公众号：提取可访问的正文，保留标题、层级、表格、代码和图片引用。
- Markdown、文本、HTML、Word `.docx`、PDF、图片：提取正文及附件；扫描页和图片使用 OCR。
- 视频链接、本地视频和音频：优先使用字幕，否则尝试下载可访问媒体并本地转写，保留时间戳。
- 由宿主助手阅读正文并选择主题分类；写入前检查目录、附件与完成状态，支持来源和内容去重、同名保护及写后核验。

完整指令见 [SKILL.md](SKILL.md)，命令、配置项和格式恢复流程见 [references/workflow.md](references/workflow.md)。

## 实际限制

网页和视频平台可能要求登录、验证码或限制访问；获取失败时需要宿主浏览器或用户提供的真实文件，不能用标题、简介代替正文。远程图片保留为 URL 时不等于离线保存。旧 `.doc` 需要先转换；复杂 PDF、公式、多栏布局和视频画面需要额外核对。语音转写模型首次使用时可能需要下载。

**图片或扫描页中的表格必须实际看图核验后才能入库。** 规则网格可以生成 Markdown 草稿，能确认的合并单元格使用 HTML；无边框或复杂结构会保留文字并提示复核。OCR 可能把高置信度数字识别错，放大重识别也不保证准确。`complete --method vision` 只记录宿主已经完成的视觉核验，本身不调用视觉模型；不能用它跳过看图。

去重仅覆盖本技能记录过的导入；未建立索引的历史笔记、手工移动的笔记需要另行核对。原文件和既有笔记不会被替换。

## 测试

```sh
python -m unittest discover -s tests -v
```

测试使用临时目录和随附图片，不访问真实知识库。缺少可选依赖或中文字体时，部分集成测试会跳过；通过这些测试不代表任意输入都能完整识别。
