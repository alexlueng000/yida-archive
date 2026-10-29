# Ubuntu 部署与运行

夜间任务默认每天北京时间 02:00 拉取前一天审批通过并完结的流程附件，先保存按单据
组织的原件，再按 `{采购单位}/{项目号}/公共/{阶段}/` 归档。网页可选择日期手动拉取；
定时任务与手动任务使用同一把进程锁，重叠时后启动的任务等待前一个任务结束。
可在网页「每日自动拉取」中修改每天的时间，时间按北京时间解释并保存在服务器。

## 一、装一次

```bash
sudo useradd -r -m -d /opt/yida-archive -s /usr/sbin/nologin yida
sudo mkdir -p /opt/yida-archive
sudo rsync -a --exclude venv --exclude .work --exclude archive ./ /opt/yida-archive/
sudo chown -R yida:yida /opt/yida-archive
cd /opt/yida-archive

sudo -u yida python3 -m venv venv
sudo -u yida ./venv/bin/pip install -r requirements.txt
```

凭证放 `/opt/yida-archive/.env`，四行，不要加引号：

```
APPKEY=...
APPSECRET=...
SYSTEMTOKEN=...
USERID=...
```

同一个文件里再加一行 AI 密钥（模型地址和名称在 `config.yaml` 的 `ai` 段）：

```
ARCHIVE_AI_API_KEY=...
```

权限收紧到这个文件的属主能读就行：

```bash
sudo chown yida:yida /opt/yida-archive/.env
sudo chmod 600 /opt/yida-archive/.env
```

## 二、先演练，不落盘

```bash
sudo -u yida ./venv/bin/python -m yida_archive.nightly --date yesterday --limit 0
```

只打印落盘方案，不下载附件或写入归档目录；项目元数据缓存可能更新。

## 三、定时

```bash
sudo cp deploy/yida-archive-web.service deploy/yida-archive-nightly.service deploy/yida-archive-nightly.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now yida-archive-web.service
sudo systemctl enable --now yida-archive-nightly.timer
systemctl list-timers yida-archive-nightly.timer     # 看下次触发时间
```

定时器每分钟唤醒检查，程序以北京时间判断是否到达网页配置的执行时间；
这不依赖服务器系统时区。以下命令核对定时服务是否运行：

```bash
systemctl cat yida-archive-nightly.timer
systemctl status yida-archive-nightly.timer
```

设置文件为 `.cache/nightly-schedule.json`，没有文件时默认为 02:00。每天的执行
状态保存在 `.cache/nightly-schedule-state.json`。当天成功或失败后不会再次自动
运行同一天；失败可查日志后在网页手动重跑。已启动的任务异常中断后会在下次
唤醒时重试原日期。修改时间后，同一天已经运行过的任务不会因修改而再次运行。
首次启用时，如果当天的配置时间已经过去且没有当日执行记录，定时器会尽快
拉取前一天的数据。更新旧版服务时需重新复制 service 和 timer、执行 daemon-reload，
然后重启 timer 和网页服务；保留服务器现有的 `.env`、`config.yaml`、`.cache`、
`.work`、`archive-*` 和 `归档结果`，不要用 `rsync --delete` 覆盖这些数据。

## 四、手动触发

在网页的「归类项目」页选择审批日期，点击「开始拉取」；页面可查看后台运行记录。
网页只监听服务器的 `127.0.0.1:8765`；使用 SSH 隧道访问，或通过具备身份认证的
反向代理提供访问。示例：

```bash
ssh -L 8765:127.0.0.1:8765 用户名@服务器
```

保持 SSH 连接后，在本机打开 `http://127.0.0.1:8765`。

补跑某一天（最常用）：

```bash
sudo -u yida ./venv/bin/python -m yida_archive.nightly --date 2026-10-27 --write
```

只跑一个项目，验证效果：

```bash
sudo -u yida ./venv/bin/python -m yida_archive.nightly --date 2026-10-27 --project 26NKD-FW010 --write
```

走 systemd 跑一次（和定时任务完全同一条命令、同一套日志）：

```bash
sudo systemctl start yida-archive-nightly.service
journalctl -u yida-archive-nightly.service -f       # 跟着看
```

其他参数：

| 参数 | 用途 |
| --- | --- |
| `--hours-back 4` | 时间窗往前多留 4 小时，补上跨零点提交的单据 |
| `--limit 0` | 明细全打印，排查时用 |
| `--refresh-meta` | 忽略采购单位缓存，重新回查合同审批 |
| `--out /data/archive` | 临时换落盘根目录 |

## 五、看结果

- 落盘根目录由 `config.yaml` 的 `storage.out_dir` 决定，默认 `./归档结果`，和网页
  「归类项目」读的是同一个根目录。跑完打开网页就能在项目列表里看到这些项目，
  文件、类型、未归类理由都在项目详情里；换过目录的话网页上是看不到的。
- 每个项目目录下有两份自检文件：`archive-清单.csv`、`archive-结果.md`
- 日志：`journalctl -u yida-archive-nightly.service --since today`
- 网页手动任务日志：项目目录 `.cache/date-extractions/` 下的对应 `.log` 文件。
- 定时器的 `Persistent=true` 会在开机后补触发一次；如果服务器停机超过一天，
  它不会逐日自动补齐所有漏掉的日期，需要在网页逐日补跑。
- 项目号缺失、认不出归属的单据，落在 `{out_dir}/_未归组项目/{提交日期}/{流程}/{单据标题}/`，
  保留原文件名，等人认领

## 六、常见问题

**回查不到采购单位**：报告里会出现「回查不到合同的项目」。这类项目按
`未指定采购单位` 归档，不影响附件。合同审批补录后加 `--refresh-meta` 重跑一次就好。

**AI 识别全部失败**：检查 `.env` 里的 `ARCHIVE_AI_API_KEY` 是否配好。
没有配置时按关键词规则归类，附件照样归档，只是分类准确度低一些。

**某一分钟单据超过 10 条**：接口单次最多回 10 条，同分钟内过量会直接报错停下，
提示需要人工处理，不会悄悄少拿。用 `--hours-back` 换个时间窗重跑。

**重复跑会不会重复存档**：不会。程序按内容哈希去重，同一文件多次出现只留一份，
新版本覆盖旧版本并在清单里记录。
