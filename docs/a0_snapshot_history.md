# A0 历史快照采集

A0 是一个只写历史、一次一时刻的全市场现价快照采集器。它服务于宪章
§3、§5.3 和 §6：未来的 A1 可以读取按时刻的证据，但当前采集器不读快照，
也不接 Pipeline、AI、券商或真实交易。

## 输出

一次成功调用在注入的项目根目录下写入：

```text
data/snapshots/YYYY-MM-DD/HHMM.csv
data/snapshots/YYYY-MM-DD/HHMM.json
data/snapshots/YYYY-MM-DD/manifest.json
```

CSV 每行保留 `source`、provider 原始 `quote_date`/`quote_time` 和采集器的
`fetched_at`。缺少报价字段保持为空，不用本机时间或收盘价填充。JSON 侧车
记录行数、覆盖符号、缺失符号和三个边界布尔值：`pipeline_input=false`、
`ai_input=false`、`latest_overwrite=false`。

默认时刻表是两个交易时段的五分钟起始标签：`09:30..11:25` 和
`13:00..14:55`，共 48 个 regular slots。`15:00` 不属于 regular slots；
调用方显式传 `--include-close` 时，它会成为第 49 个可记录的 close slot。
`manifest.json` 始终按实际时刻表计算 `expected_slots`、`completed_slots` 和
`missing_slots`，不把缺失当完成，也不为了补齐而回写旧时刻。

provider 抛错或返回空数据时，不生成该时刻的 CSV／侧车；每次实际尝试分别
写入 `data/snapshots/YYYY-MM-DD/attempts/HHMM-000001.json` 等递增文件。
记录尝试开始、结束时间、来源（确有返回时）、失败原因、警告和运行版本。
同一时刻再次尝试不会覆盖先前记录。日摘要的 `slot_attempts` 保留这些历史，
`missing_slot_reasons` 解释每张缺失快照是无已记录尝试、provider 失败、空返回还是
档案损坏；失败尝试不计作完成。同一当前时刻后来成功时仍保留失败记录。
读取不完整的尝试文件会显示在 `invalid_attempts`；写入失败返回
`archive_write_failed`，保留原始 provider 原因与 `attempt_evidence_write_failed`。

每次实际采集在调用 provider 前读取运行代码所在 checkout 的 Git HEAD 和
脏文件数，写入侧车或失败尝试的 `code_version`（`git_head`、`dirty_count`、
`captured_at`、`status`、`reason_code`）。注入的 `--root` 只决定数据存放位置，
不会被误当成代码仓库。`manifest.json` 的 `slot_code_versions` 从各张原始
侧车读取版本，失败尝试则各自保留版本。读取或重复调用旧快照时不查询今天的
Git 来补写证据；当时未记录的字段明确为 `unknown`，版本记录时间 `captured_at` 留空。
Git 不可用或超时时也明确标 `unknown` 并写原因，不能声称该次运行已有完整
版本证据；它只影响版本证据说明，不改变报价完整率或阻止采集。

## 时间证据与标签

`attempt_started_at` 是进入采集尝试、记录运行版本前的本机时间；
`fetched_at` 是整轮 provider 调用返回并解析返回结构后的本机时间。
成功返回中的 `attempt_completed_at` 与 `fetched_at` 相同；归档 JSON 用
`fetched_at` 保存完成证据。它们不是每个 HTTP 请求或每条股票的响应时刻。
`stage_timings` 中的 `provider_fetch.duration_ms` 使用单调时钟计时，表示耗时。

报价的 `quote_date`、`quote_time` 保留 provider 原文。只有时分秒的来源按
既有规则锚定归档交易日，这不等于 provider 提供并核实了报价日期。
时间判断使用采集**完成时间**，不会把正常抓取期间出现的新报价误作未来报价。

| 标签 | 当前含义 |
|---|---|
| `future` | 报价超过记录的完成时间 5 秒；恰好 +5 秒允许，+6 秒拒绝 |
| `stale` | 报价日期早于归档交易日 |
| `missing` / `unparseable` | 来源没有给时间 / 给出的时间不能解析 |
| `usable` | 当日且未超完成时间边界；不保证报价满足某个日内最大年龄 |

`future` 说明记录的两个时钟不一致；没有当时可靠的对时证据，原因保持
`unknown`，不能断定来源钟快或本机钟慢。不得为消除标签扩大 5 秒容差。
例如开始 10:00:00、完成 10:00:30、报价 10:00:20 正常；报价 10:00:36
仍应标 `future`。这些例子是确定性说明，不是伪造的历史报价。

若整轮在下一档才完成，保留原始报价和请求档位，明确写入
`fetch_completed_outside_requested_slot`，不计作请求档位的正常完成。
历史不足、主机中断和缺档分别保留，不补写旧档案来消除警告。

## 运行边界

```powershell
python scripts/collect_market_snapshots.py --dry-run true --network false --json true
python scripts/collect_market_snapshots.py --enable-network --json true
```

默认不联网；`--enable-network` 是显式授权且只执行这一轮 provider 请求。
命令不会安装、启动或修改计划任务。非交易日、非时刻表内调用返回
`closed`，离线调用返回 `offline`，两者都不调用 provider、不创建文件。
完整的重复 slot 返回 `already_exists`；重复的部分覆盖仍返回 `partial`。
历史 CSV 和侧车不会覆盖。只有一份文件时返回 `archive_incomplete`，内容或
哈希不匹配时返回 `archive_invalid`，保留原文件供审查，退出码非零。

侧车固定当时的名册列表、名册哈希、CSV 哈希和真实抓取完成时间。完整率逐份
检查 schema、日期、时刻、行数、来源和哈希，不因当前名册变化而改写历史。
provider 返回但不在本地名册中的股票也保留；覆盖率只以两者交集计算。
缺股票、缺名册或降级日历均有可见标签并返回 `partial`，不把部分覆盖算作
`completed_slots`。日历优先读取本地 CSV，其次本地 legacy YAML；仍缺失时
明确标为 `calendar_unavailable`，工作日推定采集不计作已核实完整快照。

`dry-run` 不调用 provider。交易日的首次联网试采需要单独授权，成功的离线
测试不证明真实接口覆盖、限流或自然调度已经通过。

首次接入真实 provider 时，先在交易日做一次显式干跑，检查行数、来源、抓取
时间、限流和 `missing_slots`，再由所有者单独授权计划任务安装。安装动作不在
A0 代码范围内。
