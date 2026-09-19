# 离线日线修复候选

`scripts/prepare_daily_repair_candidates.py` 将损坏日线的原件、来源证据、候选和验证清单写入一个**新的仓库外目录**。
它不替换原件，不联网，不触发收盘 Pipeline，也不安装或执行计划任务。
它服务于 Research Only / Paper Account Only 的数据修复准备阶段。

原件中独立的纯 NUL 记录可以隔离，其他有效历史保留原始字节。字段内 NUL、一般列数错误、
未知或混合复权口径、日期/股票代码冲突及无效 OHLCV 均不能通过清洗掩盖。
原件一直标记为损坏；候选通过校验不代表原件通过校验。

补数只接受显式提供的本地原始价格 CSV，表头与现有 13 列日线协议一致。补数帧在选择日期前按
`classify_frame_price_basis()` 分类，缺 `adjust` 不从目录、文件名或 provider 猜口径。
已有日期发生价格冲突则拒绝；不覆盖历史，不把估值快照的 close 编造成完整 OHLC。
只有覆盖所请求区间的可信逐日日历才用于证明完整性；不使用工作日猜测补日历。
当前工具不将未核验的停牌或缺行情当成已解释缺口。

最小调用如下。`daily_input`、`calendar.csv` 和 `repair_review` 均由使用者显式指定；
`repair_review` 必须尚不存在，并在所有源码仓库及输入目录之外。

```powershell
python -B scripts/prepare_daily_repair_candidates.py --input-dir <daily_input> --symbols 002633 --start-date 2026-09-07 --target-date 2026-09-10 --calendar <calendar.csv> --output-dir <repair_review>
```

批量选择已有复核清单中的 `daily_history_column_mismatch:<symbol>.csv` 条目：

```powershell
python -B scripts/prepare_daily_repair_candidates.py --input-dir <daily_input> --review-json <daily_refresh_review.json> --start-date 2026-09-07 --target-date 2026-09-10 --calendar <calendar.csv> --expected-hashes <original_sha256.json> --output-dir <repair_review>
```

复核清单的 `target_date` 必须与命令一致。`original_sha256.json` 为
`{"002633": "64位SHA256", "002634": "64位SHA256"}`，必须覆盖本次所有股票。
没有提供 hash 清单时，程序记录读入时观察到的 hash；提供与否都会在单只生成后、整包结束前
复查原件、补数和日历。输入改变会撤销该项的候选就绪状态，已经写出的候选作为失效证据保留，
不再从 `candidate_path` 引用。这是分时读取的离线观察，不能代替日后应用时在排他锁内再次核对 hash；
每个包都保留 `requires_locked_hash_recheck=true`。

如已经获得经过审查的真实缺失日数据，增加 `--supplemental-dir <local_evidence>`。
该目录每只股票一个 `<symbol>.csv`；原始补数和日历证据会随候选保存在输出包中。
缺少补数输入时只能检查和隔离损坏，不能声称补齐日期。

| 单文件状态 | 含义 |
|---|---|
| `blocked` | 有无法安全处理的输入问题，没有可应用的候选；最终复查前已落盘的候选只留作失效证据。 |
| `structural_candidate_only` | 可以生成结构候选，但缺失日期或日历证据仍未解决。 |
| `candidate_ready` | 该候选通过指定区间的检查，仍未应用到真实目录。 |

输出包的 `manifest.json` 汇总每只股票，`manifests/` 保存单文件详情，`originals/` 保存原件，
`candidates/` 保存可生成的候选，`evidence/` 保存提供的补数与日历。
清单包含原件及候选 SHA256、隔离记录偏移/长度/hash、保留历史校验、新增日期和未补日期。
`canonical_write=false`、`network_effective=false`、`applied=false` 始终表示尚未应用。
处理中意外失败会尝试留下 `aborted` 终态；中断的包不能作为已完成交付。

退出码：`0` 表示所有候选为 `candidate_ready`；`3` 表示仍有缺口或部分被阻断；
`2` 表示全部被阻断或参数/文件错误；其他异常退出需检查清单的 `aborted` 状态。
退出码 `0` 也不表示真实行情目录已经修复。

正式应用应由现有唯一 writer 在原有 Pipeline 排他锁内完成：重新核对原件、候选、补数和日历的 hash，
验证补数 provenance 与完整性，保留可恢复原件，再原子替换并复验。这个应用步骤尚未接入本工具，
需要路径所有者单独实施和验收。不能另起开机自动修复 writer 与收盘任务争写 `data/daily`。

新增测试先单独运行；共享 `run_tests.cmd`、`run_tests.sh` 和 CI 入口由现有所有者接线。

```powershell
python -B -m pytest -q -o addopts= -p no:cacheprovider tests/test_daily_repair_candidates.py tests/test_daily_repair_candidate_cli.py tests/test_benchmark_target_freshness.py tests/test_round10_partE_benchmark_incremental_update.py tests/test_sina_symbol_mapping_302.py
```

## 收盘链的只读恢复预览

维护者可以先检查一个已有 checkpoint 哪些阶段可复用、哪些阶段必须重新验证：

```powershell
python -B scripts/run_data_close_pipeline.py --target-date YYYY-MM-DD --resume-run-id <existing_run> --enable-network --recovery-plan --json true
```

`--recovery-plan` 在取 writer 锁、启动 provider、写报告之前返回，只读取 checkpoint 和当前输入/输出哈希。
此处 `--enable-network` 用于与原运行的配置比较，不会在预览中发起请求。目标日或配置/预算不匹配、
checkpoint 缺失时明确 blocked，不猜测旧配置，也不自动重跑全部阶段。阶段版本和文件变化分别列在
`invalid_stages`、`stages_to_run`；正常旧阶段列在 `reused_stages`。实际恢复仍需当前 writer 权限，
使用新的 `--run-id` 与同一个 `--resume-run-id`，在原 Pipeline OS 锁及 child lease 内运行。
每次准备复用下一阶段之前会再核当前文件哈希；上游重跑改变了输出时，下游必须重新执行。
已验证的 health_alert 阶段会复用，不因恢复自动获得新的推送权限。

每次调用有独立 `attempt_id`，开始、进度和终态保存在
`results/data_close_pipeline/attempts/<attempt_id>/manifest.json`。历史 `runs/<run_id>/manifest.json`
只创建一次；重入不覆盖原 manifest 或原 checkpoint。`latest` 是最近尝试，`invocation` 记录这次调用的
实际起止时间和状态；兼容字段可以保留被复用执行的原始时间，不能据此把复用说成新执行。
`last_successful` 保留最近满足证明要求的执行；失败或健康复用都不把旧成功证据重写成新的执行。

universe 降级会记录当前阶段版本、真实接受名册及其接受/恢复证据的路径和哈希，并把输入/输出证明
写到 checkpoint 实际消费的字段。缺证明的旧 checkpoint 仍拒收，不给历史记录补造证明。
基准的 `target_date`、`target_covered`、`freshness_status`、逐指数结果和原因逐层保留；
`appended=0` 不等于目标日已覆盖。空响应但本地目标日已核验时保留 current，缺目标日则明确降级；
基准降级不增加一条阻断整条日线研究链的硬门。
