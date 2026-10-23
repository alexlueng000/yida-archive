# AI文档归类使用说明

2026-10-08 首期改造。只识别现有业务类别，沿用去重、敏感目录、稳定编号和最新版输出规则。

## 范围

- PDF：读取文字层；无正文扫描页、加密或损坏文件跳过，不做OCR。
- Word：支持DOCX段落、表格、页眉页脚；旧版DOC需先转为DOCX。
- Excel：支持XLSX和XLS工作表文字，不执行宏、公式或外部链接。
- 图片：不调用AI、不读取图像内容，继续使用原有文件名和字段分类规则。
- 混合材料、不确定类型、草稿模板、与强规则冲突：跳过并记录原因；人工指定分类优先。

超过48,000字符、120页/工作表、64MB、或单表10,000行/200列的文件整体跳过，不截断后自动归档。DOCX中的图片不读取。评分门槛默认0.9，同时校验类型白名单及原文证据；模型评分不代表经校准的实际正确率。

## 配置

`config.yaml`已开启AI，模型为`qwen3.8-omni-flash`。凭证相对路径以配置文件目录为准，支持百炼GBK/UTF-8键值CSV导出。读取`apiKey`和`openAiCompatible`，不输出密钥。环境变量`ARCHIVE_AI_API_KEY`可覆盖CSV密钥。其他配置文件需添加同样的`ai`配置，参考`config.example.yaml`。

```yaml
ai:
  enabled: true
  credentials_file: ../主账号空间-apiKey-7509302.csv
  model: qwen3.8-omni-flash
  threshold: 0.9
  timeout: 60
  retries: 2
  max_calls: 100
  max_chars: 48000
  max_units: 120
```

正文、文件名、来源流程和附件字段会发送到CSV指定的模型接口，只发送文字。每轮最多100次模型请求（含重试），最多重试2次。失败不会退回文件名推测后自动归档。设`enabled: false`恢复纯规则分类。

## 使用

```powershell
cd D:\work\yida-archive
powershell -NoProfile -ExecutionPolicy Bypass -File tools/launch.ps1 web/server.py
```

打开 http://127.0.0.1:8765，上传后开始分析。网页默认加载项目`config.yaml`，可通过`--config`更换。分析完成展示分类依据和原文证据，汇总AI成功、失败和缓存复用数量；命令行显示逐文件进度，网页等待整批结果。

本地目录预览及保存：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File tools/launch.ps1 run.py --config config.yaml --local-dir "archive/每次上传" --output "归档结果/AI测试" --dry-run
powershell -NoProfile -ExecutionPolicy Bypass -File tools/launch.ps1 run.py --config config.yaml --local-dir "archive/每次上传" --output "归档结果/AI测试" --write
```

本地dry-run不提交归档和编号，但会调用AI并生成缓存及报告。在线dry-run和纯元数据快照没有正文，不进行AI分类；在线正式归档须可下载附件的宜搭凭证及`--write`。`--no-hash`不会替代正文下载。

## 缓存与历史

识别结果位于`work_dir/ai`，默认`.work/ai`。缓存包含内容哈希、模型、接口地址、分类白名单、文件名、来源和提示词版本。预览、ZIP、保存复用结果；缓存仍需通过正文证据和当前评分门槛校验。失败请求不持久缓存，损坏缓存重新识别。

CSV新增AI状态、模型、证据、Token用量及缓存命中列；Markdown增加正文识别记录。缓存命中时显示的是原始请求用量，本次不再请求模型。缓存含少量原文证据，按业务数据管理。

历史未重传附件继续保留；相同内容重传且AI失败或改判时，保留原有归档类别并标记`history_preserved`，不自动迁移或删除旧附件。新内容修订沿用原版本规则。

## 检查

```powershell
# 用合成装箱单文字检查模型，不上传业务文件
powershell -NoProfile -ExecutionPolicy Bypass -File tools/launch.ps1 tools/check_ai.py --config config.yaml
# 离线回归，不调用真实模型
powershell -NoProfile -ExecutionPolicy Bypass -File tools/launch.ps1 -Entry '-m' unittest discover -s tests
```

其他机器安装更新后的`requirements.txt`。当前启动脚本使用Codex自带PDF、DOCX、XLSX依赖，XLS依赖已安装在项目venv包目录。
