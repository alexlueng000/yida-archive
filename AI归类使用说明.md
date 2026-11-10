# AI文档归类使用说明

2026-10-08 首期改造。只识别现有业务类别，沿用去重、敏感目录、稳定编号和最新版输出规则。

## 范围

- PDF：整份原文件发送至 PDF 模型，本地读取文字层核验引用，不做本地 OCR。存在没有文字层的页面时，模型分类结果需人工确认；加密或损坏文件跳过。
- Word：支持DOCX段落、表格、页眉页脚；旧版DOC使用系统 `antiword` 提取正文，未安装时保留规则建议或提示转换为DOCX。
- Excel：支持XLSX和XLS工作表文字，不执行宏、公式或外部链接。
- 图片：明确文件名规则优先；未知的PNG/JPEG/BMP/TIFF/WebP单帧图片使用本地OCR，提取文字后调用分类模型。超过3000万像素、多帧或无足够文字的图片保留待处理。
- 混合材料、不确定类型、草稿模板、与强规则冲突：跳过并记录原因；人工指定分类优先。

超过48,000字符、120页/工作表、64MB、或单表10,000行/200列的文件整体跳过，不截断后自动归档。DOCX中的图片不读取。确认门槛默认0.9；有可核实证据且评分达到0.75的文件可以先归类，标记 `suggested`，分类理由注明待复核。低于0.75仍待归类。模型评分不代表经校准的实际正确率。

证据核验忽略空白、全半角、下划线、零宽字符及直弯引号差异，兼容 `第1页`、`Page 1`、`p.1`。多条引用中至少一条通过核验即可使用；无效引用剔除并记录数量，不因多余引用出错否决全部证据。没有任何有效证据、类型不在白名单或内容完整性校验失败仍不会接受模型分类。

PDF技术失败时，与其他文档一样可以按既有文件名建议归类，明确标注未经正文核验。人工指定结果优先；不通过放宽数值、否定词或随意改页码来通过证据核验。

## 配置

`config.yaml`已开启AI，模型为`qwen3.8-omni-flash`。凭证相对路径以配置文件目录为准，支持百炼GBK/UTF-8键值CSV导出。读取`apiKey`和`openAiCompatible`，不输出密钥。环境变量`ARCHIVE_AI_API_KEY`可覆盖CSV密钥。其他配置文件需添加同样的`ai`配置，参考`config.example.yaml`。

```yaml
ai:
  enabled: true
  credentials_file: ../主账号空间-apiKey-7509302.csv
  model: qwen3.8-omni-flash
  pdf_model: qwen3.8-flash
  threshold: 0.9
  suggestion_threshold: 0.75
  timeout: 60
  retries: 2
  max_calls: 100
  max_chars: 48000
  max_units: 120
```

PDF 原文件、文件名、来源流程和附件字段会发送到 CSV 指定的模型接口；其他支持的文档发送提取出的文字。PDF 默认使用 `qwen3.8-flash`，可通过 `pdf_model` 修改为百炼支持 PDF 理解的模型。百炼 PDF 解析另按页收费，且会产生模型输入费用；使用前请核对当前价格和地域支持。每轮最多100次模型请求（含重试），最多重试2次。技术失败按 `failure_policy` 处理；扫描页无法核验时进入人工确认。设 `enabled: false` 恢复纯规则分类。

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

重跑整个项目（包括已自动归类文件）使用：

```sh
python tools/reclassify_pending.py --project 项目号 --all --workers 3
python tools/reclassify_pending.py --project 项目号 --all --write --workers 3
```

第一条预览，第二条保存并备份原项目。`--all` 会让规则已确认的文件也经过正文识别/证据重验，人工指定分类仍保留；省略时只处理未归类文件。已有模型缓存按新规则重新校验，避免重复付费；不代表每份文件都发起新的模型请求。运行期间暂停其他归档写入任务。

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

Linux服务器如需处理旧版DOC，另安装系统工具：`sudo apt-get install antiword`。图片OCR复用已有RapidOCR依赖。本轮证据核验和评分调整会重新验证现有原始模型缓存，不必清空缓存或重复付费识别。新增图片或DOC正文识别可能产生新的模型调用。
