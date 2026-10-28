# 合同审批附件自动归类

> **阶段目录归类：** 进入某个阶段目录后，在文件数量旁点击“归类”。程序会读取该阶段目录及其子目录的附件，按所属项目号去重、分类并自动保存至 `归档结果/采购单位/项目号/分类`，未识别文件进入 `未归类`。原文件保留；重复点击不会重复导入。页面显示后台进度和结果，刷新后可继续查看，并可点击“查看归档结果”进入项目。项目根目录不再提供一次归类全部阶段的按钮。

> **2026-10-26：新增原目录浏览。** 网页左侧点击“原目录浏览”，即可打开本机已下载的项目附件目录，逐级查看每次上传及其中的文件；支持路径导航、当前目录搜索、PDF/图片/文本查看和原文件下载。项目详情页也可点击“浏览原目录”直接定位该项目。此功能只读取 `archive-*/每次上传` 下的原始资料。

> **2026-10-23：项目档案应用已上线。** 启动 `tools/launch.ps1` 后打开 `http://127.0.0.1:8765`。首页支持新建、搜索和打开项目；在项目内上传文件后，程序自动分析、按内容去重并保存，不再需要确认保密选项或点击保存。所有新归档文件均按普通文件保存，目录为 `归档结果/采购单位/项目号/分类/文件`，无法识别的文件保存到 `未归类`。页面支持按分类查看、搜索、下载单个文件和打包下载项目。项目记录和原始上传保存在 `.cache/web-projects`，重启服务后自动恢复；保存失败会保留原件，点击“重新保存”即可重试。旧测试会话会自动导入独立项目。下文保密选项和公共/加密分区属于旧版命令行归档说明，不适用于新版网页应用。

> **2026-10-15 需求V1.5第一阶段已对齐。** 当前采用28类固定编号、默认待归类、采购单位/项目目录和项目级保密标记。请优先阅读《归档使用说明》的“V1.5规则升级”。下方早期说明中的上传顺序编号、29类、按文件类型划分敏感目录及默认跳过均已被替代。

> **2026-10-08：增加AI正文分类，模型为 `qwen3.8-omni-flash`。** 读取 [AI归类使用说明.md](AI归类使用说明.md) 了解凭证、支持格式、缓存和启动方式。图片不发送到模型，扫描PDF不做OCR。

> **2026-09-29 更新：请先阅读 [归档使用说明.md](归档使用说明.md)。** 当前按 UUID 或内容哈希去重，按上传时间编号，确认版本后只输出终版，总目录为“项目号_文件分类”，网页可直接保存到文件夹。下文为旧版说明，其中四层去重、44 个文件基线及旧目录和命名规则已被新说明替代。

把宜搭流程里上传的附件，按规则去重、分类、统一命名，落到服务器的目录里。

针对的问题：同一个项目在「合同审批」「货款支付」等流程里反复上传同一份文件，
平台里看是几十条附件，实际只有十几份不同的文件；而且同一份文件在不同环节
被改过内容，光靠文件名分不出来。

> 这份 README 讲**怎么用**。想知道**为什么这么做**——架构、关键取舍、
> 踩过的坑、已知边界和未决问题——看 [`项目说明.md`](项目说明.md)。

## 两种模式

### 模式一：只要每次上传了哪些文件（不去重、不归类）

```bash
python run.py --config config.yaml --online --files           # 只出清单
python run.py --config config.yaml --online --files --write   # 顺便下载
```

一次上传 = 一条流程单据。输出一条单据一节的清单，
`--write` 时下载到 `<out_dir>/每次上传/<序号>-<时间>-<流程>-<件数>件-<标题>/`，
文件直接放在单据目录中，保留原始文件名，不再额外建立“附件”等字段子目录。同一单据内出现重名文件时自动加序号，避免覆盖。

件数故意放在流程名和标题之间：同一个项目在同一流程里往往有多次提交，
宜搭的标题模板会把它们生成得一模一样（实测有个项目的「合同审批」提交了 7 次，
标题全同，附件数从 3 到 29 不等）。只靠时间分不清哪次是哪次，件数扫一眼就能看出区别。

落盘时会把每个目录和文件的**修改时间盖成该单据的提交时间**。不盖的话，
资源管理器「修改日期」那一列显示的是下载时间，26 个单据目录全挤在同一天——
想按时间找某一次提交，点那一列表头排序等于没排，只能一个个读目录名。
盖过之后，点那一列就等于按提交顺序排。没有提交时间的单据保持原样，
不盖成 1970。

命名规则改过、或早先下下来的目录时间是下载时间，都不用重下，就地治理即可：

```bash
# 改名
py tools/rename_by_instance.py --snapshot snapshots/xxx.json --out-dir archive-xxx          # 预演
py tools/rename_by_instance.py --snapshot snapshots/xxx.json --out-dir archive-xxx --write  # 真改
# 盖时间（两个开关可以一起给，先改名再盖章）
py tools/rename_by_instance.py --snapshot snapshots/xxx.json --out-dir archive-xxx --stamp-mtime
```

工具靠快照里的单据顺序把目录按两位序号对回去，所以快照必须是当初下载那次
生成的；对不上就整体不动并报出来，不会张冠李戴。重新下当然也可以，
但几百个附件重下一遍没必要。

跨单据重复的文件会在各自单据下各出现一次——这个模式要的就是「每次上传了什么」，
不做去重。

### 模式二：完整去重归类

1. **取数** —— 按项目号把配置好的流程实例全拉下来（分页拉全，不是只拉第一页）
2. **去重** —— 见下方「四层去重」
3. **算内容哈希** —— 把文件真下载下来算 SHA-256，同内容不同名的也能合并
4. **归类** —— 按规则表把文件分到 5 个大类 29 个类型里
5. **命名** —— 统一成 `类型序号-子序号-类型-项目号.ext`
6. **落盘** —— 公共 / 加密两套目录，按采购单位和项目分开放
7. **出报告** —— CSV 清单 + Markdown 报告，含待人工清单和异常提示

## 四层去重

| 层 | 判据 | 处理 |
| --- | --- | --- |
| L1 | `fileUuid` 相同 | 平台层面就是同一个物理文件，合并 |
| L2 | 文件名 + 大小相同 | 重新上传的，合并 |
| L3 | 文件名相同、大小不同 | 内容改过，**不合并**，作为版本命名并报出来 |
| L4 | 文件名不同、大小相同 | 疑似改名副本，**不合并**，报出来等哈希确认 |

L1 + L2 用并查集合并成组。第 3 步哈希算完后会再合并一次，
这时 L4 那批（内容真一样的改名副本）会被自动并掉。

注意：**`fileUuid` 不能单独当去重键**。重新上传同一份文件，平台会发一个新的
uuid；只有「选择已有文件」才保留原 uuid。所以 uuid 相同一定是同一份，
uuid 不同不等于不是同一份 —— 这就是为什么还要 L2 和内容哈希。

## 本地网页测试（不连宜搭）

不想在服务器上试的时候，起一个本地网页，把文件拖进去直接看结果：

```bash
py web\server.py
# 浏览器打开 http://127.0.0.1:8765
```

页面上能做的事：

- 打开就是**一个上传框**：选文件或直接拖进去，会自动建一条「上传记录」。
  一条上传记录 = 宜搭里的一条单据；传几次就是几次提交，
  去重是跨这几次一起算的 —— 和线上「同一个项目反复传」的场景一致。
  再传一批就点「+ 新建一次上传」，然后在新的框里传。
- 每次上传能改**附件字段名**。字段名会参与关键词匹配（比如字段叫「装箱单」，
  文件名里没有关键词也能归到箱单类）。
- 点「重传一次」把这一批文件原样复制成新的一次提交：内容一样、uuid 全新，
  正好用来验证去重是真在看内容，而不是靠 uuid 蒙对。
- 点「开始分析」看结果：去重省下多少次、哪个文件被合并了、分到哪个目录、
  哪些要人工看、哪些进加密目录。
- 点「载入示例数据」直接看快照里的真实数据（8 次上传、95 个附件位），
  不用先准备文件。预期就是 95 -> 44 -> 28 归类 / 16 待人工 / 6 加密。
- 「下载结果 zip」把归档好的文件按目录结构打包，附 `清单.csv` 和 `结果.md`。

页面中间的竖线可以**拖动调整左右两栏的宽度**。

- 拖那条线改左栏宽度，范围 260~900px，右边至少留 460px
- **双击那条线复位**；聚焦后方向键也能调（Shift 加大步长，Home 复位）
- 宽度记在浏览器里，刷新后不弹回去
- 窗口窄到 900px 以下自动变单栏，手柄隐藏

几点说明：

- 只监听 `127.0.0.1`，数据全在内存，关掉进程就没了。
- 上传走 JSON + base64，单个文件上限 64MB、总量 512MB。这是测试用的，
  不是生产上传通道。
- 示例数据只有文件名和大小、没有文件内容，所以去重只走 uuid 和名字/大小
  两层，zip 里也只有两份清单。想看内容哈希那层，得自己拖真文件进去。

跑完页面的自动化测试：

```bash
py tests\test_web.py     # 32 个测试，起真 HTTP 服务，不需要浏览器
```

## 部署

```bash
# 1. 装依赖（Python 3.8+）
pip install -r requirements.txt


# 2. 复制配置，改项目号和采购单位
cp config.example.yaml config.yaml

# 3. 选取数方式（见下）

# 4. 先演练：不下载、不落盘，只看清单
python run.py --config config.yaml --online --files

# 5. 确认没问题再落盘
python run.py --config config.yaml --online --files --write
```

### 取数方式：auth_mode

| 值 | 取数 | 下载附件 |
| --- | --- | --- |
| `token`（默认） | 直连宜搭接口 | 可以 |
| `cli` | 走 `openyida` 命令，复用它的登录态 | **不行** |

`cli` 模式适合服务器上已经装了 OpenYida 并登录过的情况，不用再单独配凭证给这个程序。
但要下载文件仍然得自己配凭证——**OpenYida 没有附件下载命令**，附件地址不带认证会直接
跳钉钉登录页（实测 302 到 `login.dingtalk.com`）。cli 模式加 `--write` 会明确报错提示。

### 登录凭证

凭证走环境变量，不要写进配置文件。两种二选一，都配了优先用 token：

| 环境变量 | 是什么 | 怎么拿 |
| --- | --- | --- |
| `YIDA_ACCESS_TOKEN` | Bearer token | 宜搭接口返回的 `access_token`，约 2 小时过期 |
| `YIDA_COOKIE` | 整条 cookie 头的值 | 浏览器 F12 → Network → 任一 `aliwork` 请求 → Request Headers → 复制 `cookie` 那一行的值 |

**先看一眼你的浏览器到底发哪个。** F12 → Network → 点一条发往 `aliwork.com`
的请求 → Request Headers。请求头按字母排序，`Accept-Language` 下面如果**直接
跳到 `cookie`**，说明这一格没有 `Authorization`，你这台机器就得用 `YIDA_COOKIE`；
有 `Authorization: Bearer ...` 就抄 `Bearer ` 后面那串用 `YIDA_ACCESS_TOKEN`。

（实测 2026-09-23：本机浏览器是 cookie 认证，没有 Authorization 头。
只配 token 的话一个文件也下不下来。）

**csrf 不需要。** `YIDA_CSRF_TOKEN` 可以留空——实测带与不带，接口返回一模一样。
（本机 cookie 里那个 `tianshu_csrf_token` 和请求 URL 里的 `_csrf_token` 是同一个值，
看着很像必需，其实不是。）

```bash
# Linux / Git Bash —— 值里有分号和空格，必须用引号
export YIDA_ACCESS_TOKEN='xxxx'
export YIDA_COOKIE='corp_id=xxx; account=xxx; ...'
```

```powershell
# PowerShell —— 同样要引号
$env:YIDA_ACCESS_TOKEN = "xxxx"
$env:YIDA_COOKIE = "corp_id=xxx; account=xxx; ..."
```

```cmd
:: cmd.exe —— 不要加引号，加了引号会把引号本身也塞进值里
set YIDA_ACCESS_TOKEN=xxxx
set YIDA_COOKIE=corp_id=xxx; account=xxx; ...
```

cookie 那一行的值整条原样抄，里面的分号不用管，那是 cookie 自己的分隔符。
值里那些 `%3A`、`%2B` 是它本来的样子，不用解码——cmd 对查不到的 `%...%`
会原样保留，不会吃掉。

### 一个很坑的地方：Referer

**请求必须带 `Referer` 头。** 少了它，接口返回 **HTTP 200 + 空响应**——
不报错、不跳转、不说原因，就是 0 字节。带着 cookie 也一样。

（实测 2026-09-24：用 curl 复现时，只有 cookie + csrf 是 0 字节，
加上 `Referer` 立刻变成 116KB 的 JSON。值本身无所谓，
连 `https://example.com/` 都能过，但头必须在。）

程序默认用应用工作台地址（`{base_url}/{appType}/workbench`），
一般不用管。要改就配 `yida.referer`。

排查这类问题用 `py probe_auth.py`，它会告诉你卡在哪一步。

## Windows 上测试

能测，三种方式任选，都不依赖服务器：

```powershell
cd D:\work\yida-archive

# 1) 完全不连网：用自带快照跑通去重和归类
py run.py --config config.yaml --snapshot fixtures\B22026LD001.json --dry-run

# 2) 只要每次上传的清单
py run.py --config config.yaml --snapshot fixtures\B22026LD001.json --files

# 3) 连真实宜搭（需要 openyida 登录过，或配了 token）
py run.py --config config.yaml --online --files
```

```powershell
# 单元测试，不需要网络
py tests\test_pipeline.py     # 48 个
py tests\test_download.py     # 21 个，用本地假服务器验下载落盘、跳转和两种凭证
py tests\test_web.py          # 32 个，起真 HTTP 服务验网页接口
```

两个 Windows 上会踩的坑，都已处理：

| 坑 | 现象 | 处理 |
| --- | --- | --- |
| 用 `python` 而不是 `py` | `ModuleNotFoundError: No module named 'yaml'` | PATH 上的 `python` 可能是没装依赖的那个。用 `py`，它指向装了依赖的版本 |
| 中文显示成乱码 | `���Ҿ귑����` | 控制台默认 GBK。程序启动时自动切到 UTF-8。写出来的文件本来就没问题 |

如果 `py` 也不行，指定解释器全路径：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python314\python.exe" run.py --config config.yaml --files
```

装依赖：

```powershell
py -m pip install -r requirements.txt
```

## 不用 token 先验证逻辑

仓库里带了一份快照 `fixtures/B22026LD001.json`，
是之前从线上拉下来的真实数据（8 条单据、95 次附件上传）。
不连网就能把去重和归类跑通：

```bash
python run.py --config config.yaml --snapshot fixtures/B22026LD001.json --dry-run
python tests/test_pipeline.py      # 48 个单元测试，不需要网络
```

预期结果：95 次上传 -> 44 个文件 -> 28 个归类、16 个待人工。
和之前人工核过的结果一致。

## 常用参数

| 参数 | 作用 |
| --- | --- |
| `--snapshot PATH` | 用本地快照，不连宜搭 |
| `--online` | 连宜搭取数 |
| `--files` | 只按「每次上传」列文件，不去重不归类 |
| `--survey` | 盘点每个项目有多少单据和附件，用来挑样本；不需要配 `scope.project_no` |
| `--survey-limit N` | 盘点最多列 N 行，0（默认）全列 |
| `--survey-md PATH` | 把推荐名单存成 Markdown 文档 |
| `--survey-json PATH` | 把盘点原始统计存成 JSON，以后换口径重算不用再翻一遍 |
| `--survey-top N` | Markdown 里对照表列前 N 名，默认 20 |
| `--dry-run` | 只出方案和报告，不下载不落盘（默认） |
| `--write` | 真下载并落盘 |
| `--dump-snapshot PATH` | 把线上拉到的数据存成快照，以后可离线复跑 |
| `--no-hash` | 跳过下载算哈希 |
| `--discover` | 只列出各表单实际出现的附件字段 id |
| `--prefix NAME` | 输出文件名前缀 |

## 部署到新项目时的顺序

1. `python run.py --config config.yaml --online --discover`
   —— 先看清楚每个表单里有哪几个附件字段、字段 id 是什么，别靠猜
2. 把字段 id 填进配置的 `field_labels`
3. `--dump-snapshot` 存一份快照，固定住这轮数据
4. 反复 `--dry-run` 调规则，直到待人工清单可以接受
5. 最后 `--write`

## 配置要点

- `scope.project_no` / `scope.purchaser` —— 决定归档到谁的目录下
- `scope.forms[].project_field` —— 该项目号落在哪个字段，
  子表用 `子表字段id.列字段id` 写法
- `classification.rules` —— 不写就用内置的 29 类规则表；写了就以配置为准
- `classification.field_hints` —— 字段线索，比关键词优先。
  默认关闭，先看关键词能分到什么程度，确认业务含义后再打开
- `classification.unmatched_policy` —— `skip` 不存储 / `manual` 进待人工
- `storage.structure` —— 落盘结构，默认 `{root}/{purchaser}/{project}/{folder}`

## 输出的文件

| 文件 | 内容 |
| --- | --- |
| `<out_dir>/<prefix>-每次上传.csv` | `--files` 模式：每次上传的文件清单 |
| `<out_dir>/<prefix>-每次上传.md` | `--files` 模式：一条单据一节，给人读 |
| `<out_dir>/每次上传/` | `--files --write` 时下载的文件目录 |
| `<out_dir>/<prefix>-清单.csv` | 归类模式：每个文件一行，含归档目录、新名、原名、上传次数、哈希 |
| `<out_dir>/<prefix>-结果.md` | 归类模式：汇总、分类明细、待人工、异常提示 |
| `<out_dir>/<prefix>-落盘清单.tsv` | 归类模式 `--write` 才有，用来和源文件对账 |

报告里会单独列两类需要人看的情况：
同名不同内容（版本）、大小相同但文件名不同（疑似改名副本）。

## 目录结构

```
yida-archive/
├─ run.py                 入口
├─ 项目说明.md            项目文档：架构、设计取舍、已知边界
├─ config.example.yaml    配置模板
├─ config.pay.yaml        只取货款支付的配置
├─ config.25JZBTPFB157-306.yaml  盘点挑出来的大样本项目（207 个附件位）
├─ config.24JZBTPFB157-401.yaml  第二个样本（194 个附件位，两个流程均衡）
├─ requirements.txt
├─ fixtures/              离线快照样本
├─ web/                   本地网页测试
│  ├─ server.py           服务（只用标准库，不额外装东西）
│  └─ static/index.html   页面（单文件，不引外部资源）
├─ tests/                 单元测试
└─ yida_archive/
   ├─ config.py           配置加载 + 内置 29 类规则表
   ├─ models.py           数据结构
   ├─ extract.py          从 formData 里递归抽附件（含子表）
   ├─ fetch_yida.py       直连宜搭接口取数 + 下载
   ├─ fetch_cli.py        走 openyida 命令取数（复用登录态）
   ├─ fetch_json.py       读/写本地快照
   ├─ discover.py         探附件字段 id
   ├─ survey.py           盘点每个项目的单据数和附件数
   ├─ by_instance.py      按「每次上传」列文件，落盘时把时间盖成提交时间
   ├─ dedupe.py           四层去重
   ├─ classify.py         归类规则
   ├─ naming.py           统一命名
   ├─ archive.py          下载 / 哈希 / 落盘
   ├─ report.py           CSV + Markdown 报告
   └─ pipeline.py         串起来
```

## 挑一个「附件多、流程走完」的项目

不知道归档哪个项目好、或者想找个数据量大的项目试跑时，先盘一遍：

```bash
py run.py --config config.yaml --online --survey
```

它**不按项目号过滤**，把配置里每个表单翻全，再在本地按项目号聚合，输出一张
按附件数从多到少的表（项目号 / 单据数 / 附件位 / 流程覆盖 / 时间范围 / 状态），
末尾单独列出「每个流程都走过、且单据全部已完成」的项目。

几个要点：

- **不需要配 `scope.project_no`**，也不需要凭证——只读，`auth_mode: cli` 复用
  openyida 登录态就够。
- 单据多的时候会慢（实测本应用 12186 条单据，两个表单翻全要几分钟）。
  用 `--survey-limit 20` 只看前几名。
- 「流程覆盖」是 `走过的流程数/配置的流程数`。只覆盖一部分的项目，说明另一个
  流程里的单据没填这个项目号，或者根本还没走到那一步。
- 「已完成」是按平台的 `instanceStatus` 判的。**已终止的不算走完**，所以附件位
  最多的那个项目未必出现在推荐列表里。
- 盘点的范围**只限配置里写了的那几个表单**。应用里还有别的表单的话，
  它们的数据不在这张表里，「全流程」也就只能在这个范围内说。

要一份能存下来、能发给别人看的名单就加输出参数：

```bash
py run.py --config config.yaml --online --survey \
  --survey-md 项目推荐名单.md --survey-json archive/盘点-原始统计.json
```

Markdown 里三节：推荐项目（按流程拆开列，能直接看出「都走过」是走过哪几条）、
附件最多的 N 个项目（**对照用，不等于推荐**，带一列「推荐：是/否」说明为什么落榜）、
口径与边界。JSON 存的是原始统计，换口径重算时不用再联网翻一遍。

**推荐名单可能很长**——本应用 3489 个项目号里有 2835 个满足「两个流程都走过
且全部已完成」。附件多的排前面，直接看前几十行就够了。要控制篇幅，
表格行数用 `--survey-limit N`、对照表用 `--survey-top N`。

盘完把选中的项目号填进 `config.yaml` 的 `scope.project_no` 就能跑了。
仓库里的 `config.25JZBTPFB157-306.yaml` 就是这么选出来的一个样本。

## 只下某一个流程

`config.pay.yaml` 是现成的例子：只取货款支付，不下合同审批。
产物落到 `./archive-pay`，不会和别的混在一起。要换成别的流程，
改 `scope.forms` 里那一条就行（`form_uuid` 和 `project_field` 别写错，
写错会静默返回 0 条）。

```bash
# 先看清单，确认单据数对不对（这一步可以走 cli，不用配凭证）
python run.py --config config.pay.yaml --online --files

# 确认了再真下
python run.py --config config.pay.yaml --online --files --write
```

## 已知边界

- **下载附件必须要有可用的登录凭证。** OpenYida（v2026.9.22-1，195 个命令）
  没有附件下载命令；附件地址不带认证会 302 到 `login.dingtalk.com`（实测）。
  所以 `auth_mode: cli` 只能取数、不能下载，要下载得配 `auth_mode: token`
  并把凭证放进环境变量（token 或 cookie 都行）。
- **凭证不一定是 Bearer。** 浏览器发往 `front.aliwork.com` 的请求里
  **没有 `Authorization` 头**，靠 cookie 认证（实测 2026-09-23）。
  程序两种都支持，但只配 token 的话，在这种机器上一个文件也下不下来，
  所以配之前先看 F12 确认是哪种。
- 凭证无效或过期时，程序**不会**把登录页当成附件存下来。
  早期版本会：`requests` 默认跟着 302 跳转，拿到登录页的 200 HTML 后
  原样写进 `xxx.pdf`，日志还报「下载成功」。现在跳转到已知登录域
  （`login.dingtalk.com` 等）、以及 200 的 HTML 都会被判成失败，
  记进报告，不留 `.part` 残留文件。附件本身是 `.html` 的不受影响。
- **跳转不能一律当失败。** 修上面那个 bug 时矫枉过正过一版：把**所有** 3xx
  都当认证失败，结果有登录态时一个文件也下不到——附件地址本来就是中转，
  真文件在后面的对象存储上（实测：302 跟过去才是 `application/pdf`）。
  现在只把已知登录域当失败，其余跳转照跟，最多 5 跳。
- 附件必须能下载才能算内容哈希。如果 token 没有文件读取权限，
  下载会失败并记进报告，不会中断整批。
- 归类靠关键词规则。规则外的文件不会硬塞，一律进待人工清单。
- 文件名里的版本号**不做猜测**（名字里的数字多半是日期或复制标记）。
  只有「原名完全相同但内容不同」这种有证据的情况才标 `-v1/-v2`。
- 一键从服务器上读全部流程实例，取决于 token 的可见范围；
  取不到的表单会在日志里报出来，不会静默跳过。
