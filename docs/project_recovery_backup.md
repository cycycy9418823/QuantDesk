# 项目恢复备份（P5，候选）

账本及执行证据继续使用原有 `paper_evidence_closure.v1` 备份。项目恢复材料使用独立 `project_recovery.v1` 包，包括明确选择的源码快照 Git bundle、QuantDesk 任务 XML、Python/系统/依赖名称版本清单，以及完整 `data/metadata/`。不包含环境变量、密钥、真实日线或第二份账本。

源码范围必须明确选择：默认 `full_history` 保存当前提交及可达历史，历史含受保护文件就拒收；`current_tree` 只保存当前提交的完整文件树，在仓外临时库构造独立的无父历史快照。二者仍要求源码已提交、工作树无源码改动。当前正式库的旧历史曾含 `config/llm_config.json`，因此完整历史模式仍会拦截。

`current_working_tree` 保存当次实际在用的维护源码，包括尚未提交的已跟踪改动、源码删除，以及未忽略的 `quantdesk/`、`scripts/`、`tests/`、`docs/`、`schemas/`、`templates/`、`examples/`、`app/`、`ai_trader/`、`design-system/`、`.github/` 和 `config/examples/` 下支持的源码/公开资源文件。已跟踪源码按原路径保存；未跟踪的根目录临时脚本、非上述维护目录的文件和生成文档不自动纳入。`data/` 资产、运行目录、缓存、私密配置、学习日志、密钥、被Git忽略的文件不作为源码读入；`data/metadata/` 仍通过独立的流式资产备份保存。已声明的本机配置 `config/local_data_store_override.yaml`、`config/project_recovery_backup.json`、`.codex/config.toml` 即使被旧库跟踪，也不作为工作源码读入，并列入 excluded_local_configuration_paths。其他当前索引受保护文件、链接或子模块依然拒收；full_history/current_tree 对本机配置的拒收仍保留。

新模式在仓外临时库按原字节建对象，不在正式库提交、写索引或导出旧历史，也不使用 Git clean filters。清单列出全部维护源码的路径、长度、SHA256、模式和快照指纹，真实 `git_dirty` 与 Git 状态摘要另列，不冒充已提交版本。打包前后复核文件集合、原提交、原始字节与状态；变化即失败，不发布半份包。当前工作源码总字节上限64 MiB；Windows保留索引执行模式，其他平台按当前执行位保存。

本候选示例选择 `current_working_tree` 且 `enabled=false`。三个模式都不提供私密内容；后两个模式**不提供原来的提交历史**。正式启用前由所有者和安装人确认范围。普通 `enabled` 旧配置的默认仍为 `full_history`，不自动替换既有范围。

快照的 `source.head` 是原提交，`source.bundle_head` 是恢复快照的独立提交，二者不同。`current_tree` 的 `source.tree` 与原提交文件树相同；`current_working_tree` 的 `source.base_tree` 是原提交文件树，`source.tree` 是实际字节快照，允许不同。恢复快照必须匹配 `source.tree` 和每项 `working_files` 的长度/SHA256。清单明确记录 `history_included=false`，不能把快照提交号当成正式仓库的新版本。

2026-11-01 只读清点：元数据 408 文件、7,726,722,882 字节，不能放入上限 128 MiB 的原账本包。新包按块读取与校验，不把全包读进内存；上限 16 GiB、5,000 个成员、源码 bundle 上限 64 MiB，超限或中途源文件变化则失败，不截断。JSON 元数据在检查敏感字段时单文件上限 4 MiB，超限明确失败。实际压缩体积与传输用时以首次真实运行收据为准。

正式启用由所有者确认、Claude 安装。新代码在本地 `config/project_recovery_backup.json` 不存在或 `enabled=false` 时保留原账本备份路径，不传送恢复包。安装人可从 `config/examples/project_recovery_backup.json` 复制到该本地路径，确认源码范围后将 enabled 改为 true；不需要修改计划任务。该配置不纳入源码 bundle。只有 enabled 的旧格式仍默认 full_history，不会自动删减历史范围。

恢复包启用后，先完成原账本备份，再打包上传恢复包并在远端逐文件校验。恢复包失败时，记录纸面账备份已成功、项目恢复未成功，整体退出非零且不清理旧备份。文件传输/校验最多 900 秒；原小包 120 秒时限不变。旧的 14 份保留规则不变，因此启用前应核对两端可用空间和 14 份完整包的预算；本候选不声称已验证真实空间、传输或定时运行。

`full_history`/`current_tree` 有未提交源码时仍拒收；`current_working_tree` 明确保存所选维护源码的未提交字节，不放宽受保护路径、链接/子模块、合并冲突或不完整 Git 历史检查。个人未跟踪学习日志不作为源码读入。任务 XML 只导出，不注册、不触发；含密钥参数的定义明确拒收。环境清单只记录公开版本字段，不导出环境变量、包源 URL 或认证配置。

## 离线打包与核验

解释器采用当前 Python。临时配置只需 `{"project_root":"源目录","network":false}`，不需要 SSH 密钥。

```powershell
python scripts/ops/push_backup_to_student.py --config <临时配置.json> --offline-recovery-pack <仓外目录>/project_recovery.tar.gz --source-scope current_working_tree
python quantdesk/project_recovery_backup.py --archive <恢复包> --expected-sha256 <打包回执哈希> --isolated-restore-root <全新仓外目录>
```

测试或非 Windows 离线演练可加 `--task-xml-dir <XML目录>`；文件名须以 QuantDesk 开头，内容为合法任务 XML。正式 Windows 运行自动只读导出 QuantDesk 前缀任务。不要通过这项参数省略未能导出的真实任务。

## 恢复一页说明

1. 在空白外部目录核验并展开恢复包。任何坏哈希、缺文件、重复路径、链接、路径越界或覆盖已有目录的请求都会失败。
2. 用 `git -c core.autocrlf=false clone <展开目录>/source/project.bundle <另一全新目录>` 恢复源码，核对 `git rev-parse HEAD` 与 manifest 的 source.bundle_head 相同，再核对 `git rev-parse HEAD^{tree}` 与 source.tree 相同。full_history 下 bundle_head 也等于原 source.head；两个快照模式只恢复文件树，旧历史不在包中。工作源码模式还须逐项核对 working_files 的长度和SHA256，不能只比提交号。当前项目的 `* -text` 属性保留原字节；其他项目若有 checkout filters/编码或换行转换，应在无检出的全新克隆中禁用相应转换再检出，并以原字节哈希确认。恢复包不自动执行克隆或程序。
3. 按 environment.json 安装对应 Python 与依赖，再对比需要的本地配置。清单不含密钥，需要所有者另行恢复受保护配置。
4. 将已校验的 data/metadata 放进恢复的源码目录。也可在第1步的展开目录执行 `git init`、`git fetch source/project.bundle HEAD`、`git checkout --detach FETCH_HEAD`，让源码与元数据处于同一全新恢复目录，再核对步骤2的提交和文件树。真实日线、快照、账本及其执行证据仍来自各自的数据备份，不能从源码 bundle 得到。
5. 先在隔离目录运行帮助/自检，核验后再由所有者指定正式恢复位置。tasks/ 只提供原任务定义；用户确认路径、账号和时间后另行安装，不自动恢复计划任务。

离线演练与实际灾备恢复不同：本轮交付按实测记录源码与元数据的恢复范围，不冒充全部行情资产恢复、全新系统环境重装、远端传输成功或用户验收。
