# 发票到款核对 CLI (irec)

多命令结构的发票与银行流水核对工具。支持本地会话持久化、配置文件、自动/人工匹配、挂起/撤销、操作历史记录与核对报告导出。

---
## 特性

- **多命令 CLI**：基于 Click，结构化子命令（init / config / session / imp / match / manual / list / report 等）
- **本地会话**：JSON 持久化，关闭重启不变，多会话切换
- **配置文件**：金额容差、日期容差、客户名别名、匹配策略
- **智能导入**：
  - UTF-8/GBK/GB18030 自动编码检测
  - 中文/英文表头自动识别（发票号 / 客户名称 / 金额 / 开票日期 等）
  - **非法金额/日期行会精确到行号报错**
  - **同一文件重复导入会被去重拦截**（按内容+文件名哈希）
- **自动匹配引擎**：
  - 规则1：一对一（客户名 + 金额精确 + 日期容差）
  - 规则2：多对一（同客户多发票合并匹配一笔流水）
  - 规则3：一对多（同客户多流水合并匹配一张发票）
  - **超出容差的一对多关系保持未解决**（不强制匹配）
- **人工复核**：人工匹配、挂起、取消挂起、撤销
- **完整历史**：所有操作写入不可变历史链
- **报告导出**：JSON 总览 + CSV 多表（汇总、匹配明细、未匹配、撤销记录）
- **会话快照**：
  - 一键导出当前会话（发票、流水、匹配、挂起、撤销、历史、配置）为 ZIP 快照包
  - SHA-256 内容完整性校验，版本号兼容性检查
  - 导入为新会话：覆盖/拒绝/自动重命名 三种冲突处理
  - 可选恢复快照中的配置到当前工作目录
  - 导入/导出均写入操作历史，重启后数据完全一致
  - 重复导入来源检测（快照来源 vs 当前会话已导入CSV对比）
- **审计包（Audit Package）**：
  - 结账后一键产出完整归档包，比快照更完整，适合交接和复盘：
  - 包含：会话摘要、关键配置快照、匹配明细 CSV、撤销和挂起原因 CSV、来源文件指纹、JSONL 操作日志、完整 JSON/CSV 报告、完整会话数据
  - 不是单纯压缩包：导入时智能识别同名会话冲突、配置漂移、缺少依赖配置、重复导入来源
  - 三种冲突处理：拒绝（reject）、覆盖（overwrite）、另存新副本（auto-rename）
  - 操作日志回放：将审计包中的历史操作追加到目标会话，只写历史不还原数据
  - 所有导入/导出/回放动作全部写入操作历史
  - 归档内容 SHA-256 哈希校验，版本兼容性检查

---

## 安装

```bash
cd zyx-00129
pip install -e .
```

安装后提供命令 `irec`：

```bash
irec --help
```

---

## 项目结构

```
zyx-00129/
├── pyproject.toml
├── README.md
├── samples/
│   ├── invoices_good.csv         # 正常发票样例（8行）
│   ├── transactions_good.csv     # 正常流水样例（10行）
│   ├── invoices_bad.csv          # 非法数据样例（含错误行）
│   ├── invoices_regression.csv   # 边界回归：拼多多 2 张发票（6000+4000）
│   ├── transactions_regression.csv # 边界回归：拼多多 6 笔流水（两条链路）
│   ├── REGRESSION_NOTES.txt      # 边界回归数据设计说明
│   └── RULES.txt                 # 默认规则说明
└── invoice_reconcile/
    ├── __init__.py
    ├── __main__.py              # python -m invoice_reconcile 入口
    ├── cli.py                   # 多命令 CLI 主入口
    ├── config.py                # 配置文件管理
    ├── session.py               # 会话持久化 & 数据模型
    ├── importer.py              # CSV 导入 & 校验 & 去重
    ├── matcher.py               # 自动匹配引擎
    ├── manual.py                # 人工匹配/挂起/撤销
    └── reporter.py              # 核对报告构建与导出
```

---

## 默认规则（.irec_config.json）

```json
{
  "amount_tolerance": 0.01,
  "date_tolerance_days": 3,
  "customer_name_aliases": {},
  "match_strategy": "amount_date_name",
  "session_dir": ".irec_sessions",
  "default_session": "default"
}
```

- 金额容差：±0.01 元
- 日期容差：±3 天
- 客户名别名：通过 `irec config alias <标准名> <别名1> <别名2>` 配置

---

## 命令速览

```
irec init                          # 初始化（创建配置 + 默认会话）
irec config show                   # 查看配置
irec config alias 华为技术有限公司 华为
irec config set days_tol 5

irec session list                  # 列出所有会话
irec session create may_2026       # 创建新会话
irec session switch default        # 切换会话
irec session delete old --yes

irec status                        # 当前会话状态（核心面板）

irec imp invoice samples/invoices_good.csv       # 导入发票
irec imp txn samples/transactions_good.csv       # 导入流水

irec match                         # 执行自动匹配
irec match --dry-run               # 预览

irec manual match -i INV-001 -t TXN001 -n "备注"
irec manual suspend-inv INV-007 -r "合同争议"
irec manual unsuspend-inv INV-007
irec manual reverse <match_id> -r "撤销原因"

irec show unmatched                # 查看待复核列表
irec show unmatched --with-suspended
irec show matches                  # 有效匹配
irec show matches --all            # 含已撤销
irec show history -n 20
irec show history --action reverse_match

irec report -o irec_report         # 导出 JSON+CSV 报告
irec report -f json -o rpt.json

irec snapshot list                   # 列出所有快照（存储在 .irec_snapshots/）
irec snapshot info <快照文件>        # 查看快照详情：版本、完整性、配置、数据量
irec snapshot export -o may_2026_backup -n "月末备份"  # 导出当前会话为快照包
irec snapshot import may_2026_backup.irecsnap --as may_2026_restored --switch  # 导入为新会话并切换
irec snapshot import may_2026_backup.irecsnap --overwrite          # 同名会话已存在时覆盖
irec snapshot import may_2026_backup.irecsnap --auto-rename        # 同名时自动加 _restored 后缀
irec snapshot import may_2026_backup.irecsnap --apply-config       # 同时恢复快照中的配置
irec snapshot delete <快照文件> --yes                              # 删除快照
irec snapshot check-conflicts <快照文件> -s <现有会话>              # 对比重复导入来源

irec audit list                        # 列出所有审计包（存储在 .irec_audits/）
irec audit info <审计包文件>        # 查看审计包详情：版本、完整性、配置漂移、指纹、数据量
irec audit export -o may_2026_audit -n "月末审计归档" --operator 张三  # 导出当前会话为审计包
irec audit import may_2026_audit.irecaudit --as may_2026_restored --switch  # 导入为新会话并切换
irec audit import may_2026_audit.irecaudit --reject          # 同名会话已存在时拒绝
irec audit import may_2026_audit.irecaudit --auto-rename        # 同名时自动加 _restored 后缀
irec audit import may_2026_audit.irecaudit --overwrite        # 同名时强制覆盖
irec audit import may_2026_audit.irecaudit --apply-config       # 同时恢复审计包中的配置
irec audit replay may_2026_audit.irecaudit -s <目标会话>   # 只回放操作日志到指定会话
irec audit delete <审计包文件> --yes                              # 删除审计包
irec audit check-sources <审计包文件> -s <现有会话>              # 对比重复导入来源
```

---

## 验收命令链（完整流程）

以下命令链覆盖所有验收要点：**自动匹配 1 笔 + 人工匹配 1 笔 + 非法行报错 + 重复导入拦截 + 一对多超出容差保持未解决 + 会话重启持久化验证 + 一对多多候选边界不漏判**。

### 步骤 1：初始化 & 配置别名

```bash
# 清理旧状态（首次运行时）
# Remove-Item -Recurse -Force .irec_sessions,.irec_config.json,.irec_state.json,irec_report* -ErrorAction SilentlyContinue

pip install -e .

irec init
# => 配置初始化，默认会话创建

# 设置客户名别名（样例数据中流水使用的是简称）
irec config alias "华为技术有限公司" "华为"
irec config alias "阿里巴巴集团" "阿里" "Alibaba"
irec config alias "腾讯科技" "腾讯"
irec config alias "字节跳动" "字节" "ByteDance"
irec config alias "百度在线" "百度"
irec config alias "京东集团" "京东"
irec config alias "美团点评" "美团"
irec config alias "小米科技" "小米"

irec config show
irec status
```

### 步骤 2：导入发票 & 流水（验证非法行报错）

```bash
# 先尝试导入非法发票 -> 应带行号报错
irec imp invoice samples/invoices_bad.csv
# => 报错示例：第2行: 无法识别的金额格式... / 第5行: 无法识别的日期格式...

# 导入正常数据
irec imp invoice samples/invoices_good.csv
# => 导入 8 张发票
irec imp txn samples/transactions_good.csv
# => 导入 10 笔流水

irec status
```

### 步骤 3：重复导入拦截

```bash
irec imp invoice samples/invoices_good.csv
# => 错误：文件已导入，不能重复入账（带文件哈希标识）
irec imp txn samples/transactions_good.csv
# => 错误：文件已导入，不能重复入账
```

### 步骤 4：执行自动匹配

```bash
irec match --dry-run       # 预览
irec match                 # 正式执行
irec status
irec show matches
# 预期自动匹配成功的 1 对 1 关系：
#   INV-2026-001 (华为 12500, 6/1)  <-> TXN20260601001 (华为 12500, 6/2, 日期差1)
#   INV-2026-002 (阿里 8750.5, 6/5)  <-> TXN20260605002 (阿里 8750.5, 6/5, 日期差0)
#   INV-2026-003 (腾讯 33200, 6/8)   <-> TXN20260608003 (腾讯 33200, 6/7, 日期差1)
#   INV-2026-004 (字节 5600.75, 6/10)<-> TXN20260610004 (字节 5600.75, 6/12, 日期差2)
#   INV-2026-006 (京东 4200, 6/15)   <-> TXN20260612006 (京东 4200, 6/16, 日期差1)
#   INV-2026-007 (美团 7800, 6/18)   <-> TXN20260618007 + TXN20260618008 (3000+4800=7800, 多对一)

irec show unmatched
# 预期未匹配（需人工判断的项）：
#   INV-2026-005 (百度 18900, 6/12)  vs TXN20260612005 (百度 9400, 6/13)  -> 金额差很大
#   INV-2026-008 (小米 9200, 6/20)   vs TXN20260620009 (小米 9500, 6/21)  -> 金额差300>0.01容差
#   TXN20260622010 (网易 20000, 6/22) -> 无对应发票
# 超出容差的一对多关系（如 9400 + 9400+？ 凑不到18900）将保持未解决状态
```

### 步骤 4.5：边界场景回归（多候选一对多不漏判）

```bash
# 先配置拼多多别名（如果要单独跑这一步）
# irec config alias "拼多多科技有限公司" "拼多多" "PDD"

# 导入边界回归数据（两组场景，详细设计见 samples/REGRESSION_NOTES.txt）
irec imp invoice samples/invoices_regression.csv
# => 2 张发票：INV-2026-009(6000@6/15) + INV-2026-010(4000@6/10)

irec imp txn samples/transactions_regression.csv
# => 6 笔流水（按组合设计顺序）：
#     T1=1000@5/1  T2=5000@5/1   (首组合 6000 但日期超 ±3 容差 44 天)
#     T3=2000@6/14 T4=4000@6/16  (次组合 6000 日期 1 天内 合格)
#     T5=1500@5/1  T6=2500@5/1   (唯一组合 4000 但日期超容差)

irec match
# 关键断言（请对照 samples/REGRESSION_NOTES.txt）：
#   ✓ 链路 A：INV-2026-009 匹配上的流水应是 T3+T4（日期合格组合）
#     而不是先扫到的 T1+T2（超容差组合）。
#     修复前：T1+T2 金额命中但日期被外层丢弃后不再继续，整组留在待复核
#     修复后：日期校验下沉到子集和搜索，T1+T2 被过滤 → 继续遍历 → 命中 T3+T4
#
#   ✓ 链路 B：INV-2026-010 对应的 T5+T6 金额合计 4000 但所有候选日期都超容差，
#     应全部保留在未匹配列表（irec show unmatched），且状态为 "unmatched"。
#     这和 INV-2026-005 / INV-2026-008 的"普通金额不符"未匹配是两类根因，
#     前者是日期容差失败、后者是金额根本凑不到目标值——两者都留在未匹配，
#     但报表应能区分金额 vs 日期根因（可看 matches.csv 的匹配注释，
#     或用 reporter.json 中 unmatched_* 字段人工核对，避免把边界误判为普通金额不符）。

irec show unmatched
# 预期边界回归相关的未匹配条目（共 5 条：1 张发票 + 4 笔流水）：
#   INV-2026-010 拼多多 4000 (日期容差失败的唯一候选，不是"金额凑不出")
#   TXN20260601011/TXN20260601012 拼多多 1000+5000 (链路A里被过滤掉的不合格组合)
#   TXN20260601015/TXN20260601016 拼多多 1500+2500 (链路B里日期全不合格的组合)

# 或直接运行一次性自动化回归脚本（包含 import→match→report→断言→重启持久化）：
python _test_boundary_regression.py
# => 结束时输出 [ALL PASSED] 边界场景回归通过 且脚本 exit 0；任何子进程非零退出码均视为失败

### 步骤 5：人工匹配 1 笔 + 挂起 + 撤销演示

```bash
# 人工匹配 小米 发票 INV-2026-008 与 流水 TXN20260620009（差额300，会计确认是手续费）
irec manual match -i INV-2026-008 -t TXN20260620009 -n "差额300元为手续费，财务确认入账"
# => 完成第 2 笔要求的人工匹配

irec status
irec show matches

# 挂起百度发票（合同争议）
irec manual suspend-inv INV-2026-005 -r "合同金额争议，等待商务确认"
irec show unmatched --with-suspended

# 撤销刚才的人工匹配（演示撤销功能），然后再重新匹配上
# 先找到 match_id（在 list matches 中查看）
# 比如小米那组匹配的 ID 是 <match_id>：
# irec manual reverse <match_id> -r "演示撤销"
# 然后重新匹配回来：
# irec manual match -i INV-2026-008 -t TXN20260620009 -n "差额300元为手续费，财务确认入账"

irec show history -n 10
```

### 步骤 6：导出报告

```bash
irec report -o irec_report
# => 导出：
#   irec_report.json                  总报告 JSON
#   irec_report/summary.csv           汇总表
#   irec_report/matches.csv           匹配明细
#   irec_report/unmatched_invoices.csv  未匹配发票
#   irec_report/unmatched_transactions.csv 未匹配流水
#   irec_report/reversed_matches.csv  撤销记录

irec status
```

### 步骤 7：会话持久化验证（关闭再启动不变）

```bash
# 记录当前关键信息：
irec status
irec show history --action reverse_match
irec show unmatched

# 记录会话 ID、报表合计、撤销记录数、未匹配列表内容

# 模拟关闭再启动（会话数据在 .irec_sessions/default.json 中持久化）
# 这里实际不退出，直接重新读取验证：
irec session list
irec status
# 关键断言：
#   ✓ 会话编号 (session_id) 不变
#   ✓ 未匹配列表条数和内容不变
#   ✓ 报表合计 (invoices.amount_total / transactions.amount_total 等) 不变
#   ✓ 撤销记录（如果执行了 reverse）依然存在
#   ✓ 操作历史链完整

# 再次打开验证持久化
irec show matches
irec show history -n 30
```

### 步骤 7.5：会话快照 - 导出备份与导入恢复

```bash
# 记录当前状态快照前的关键数字（用于导入后比对）
irec status
irec show unmatched
irec show history --action reverse_match

# 导出当前会话为快照包（含发票、流水、匹配、挂起、撤销、历史、配置）
irec snapshot export -o "step7_backup.irecsnap" -n "README演示：步骤7完成后备份"
# => 输出：快照ID、版本号、SHA-256校验结果、数据量统计
#    快照文件生成在 .irec_snapshots/step7_backup.irecsnap

# 查看所有快照 & 详情（版本、完整性、配置、数据量）
irec snapshot list
irec snapshot info step7_backup.irecsnap
# => 关键标记：版本兼容性 ✓  完整性哈希 ✓  配置完整性 ✓

# 导入为新会话（不覆盖原 default），不恢复配置
irec snapshot import step7_backup.irecsnap --as "default_restored" --switch
# => 新会话ID + 来源快照ID；新会话数据量与原会话完全一致

# 切换回原会话，再演示冲突处理分支
irec session switch default

# 分支 A：同名会话已存在，默认 ask 模式会交互确认
# irec snapshot import step7_backup.irecsnap --as default
# => 提示冲突，要求确认是否覆盖（Y=覆盖，N=终止，改用 --as 或 --auto-rename）

# 分支 B：--reject 拒绝重名（安全默认）
irec snapshot import step7_backup.irecsnap --as default_restored --reject
# => Error: 会话 'default_restored' 已存在 (冲突模式: reject)

# 分支 C：--auto-rename 自动加后缀
irec snapshot import step7_backup.irecsnap --as default_restored --auto-rename
# => 自动重命名为 default_restored_restored（或 default_restored_restored2 等）

# 分支 D：--overwrite 强制覆盖 + --apply-config 恢复快照配置
irec snapshot import step7_backup.irecsnap --as default_restored --overwrite --apply-config
# => 显示 "已覆盖已存在的同名会话" + "配置已恢复"

# 检查重复导入来源（两个会话是否各自独立导入过相同CSV）
irec snapshot check-conflicts step7_backup.irecsnap -s default
# => 若两边都导入过 invoices_good.csv / transactions_good.csv，会提示发现 N 个重复来源

# 导入后验证：状态、未匹配、备注、挂起、撤销、报表汇总全部对得上
irec session switch default_restored
irec status                     # 会话ID变化，但核心数字=导出前
irec show unmatched             # 未匹配列表内容、顺序完全一致
irec show matches --all         # 活动匹配 + 已撤销匹配数量和明细完全一致
# 人工备注仍在：差额300元为手续费
# 挂起原因仍在：合同金额争议
# 撤销记录仍在：演示撤销
irec show history --action snapshot_import   # 新会话历史包含 snapshot_import
irec show history --action snapshot_export   # （若从导入的会话看，也有原会话的 snapshot_export）

# 重启后数据不变
irec session list
irec status -s default_restored
# => session_id、所有合计数字、未匹配列表内容均与导入完成后一致
```

### 步骤 7.8：审计包 - 结账归档与交接复盘（完整链路）

```bash
# 先切换回 default 会话，确保有完整的业务数据
irec session switch default

# ======================
# 1. 导出审计包（结账归档）
# ======================

# 记录归档前的关键状态，用于归档后比对
irec status > audit_before_status.txt
irec show unmatched > audit_before_unmatched.txt
irec show matches > audit_before_matches.txt
irec show history -n 20 > audit_before_history.txt

# 导出审计包（比快照更完整，包含业务可直接查阅的 CSV 明细）
irec audit export -o "may_2026_final" -n "2026年5月结账审计归档" --operator "财务-李明"
# => 输出：
#   审计包 ID、版本号、SHA-256 完整性哈希
#   归档内容：14 个文件（摘要、配置、明细、撤销挂起、指纹、日志、报告、会话）
#   发票/流水/匹配/撤销/挂起/历史/文件数 统计
#   文件位置：.irec_audits/may_2026_final.irecaudit

# ======================
# 2. 查看审计包信息
# ======================

irec audit list
# => 列出所有审计包，含审计包ID、原会话、创建时间、数据量

irec audit info may_2026_final.irecaudit
# => 显示完整的审计包信息：
#   ✓ 版本兼容性（当前支持 v1.0）
#   ✓ 完整性哈希校验
#   ✓ 配置完整性
#   ⚠ 配置漂移（若当前工作目录配置与审计包不同，会逐项列出）
#   来源文件指纹（2 个，带哈希、文件名、导入时间、记录数）
#   三大核心统计（发票/流水/匹配）

# ======================
# 3. 配置漂移场景演示
# ======================

# 先修改当前配置，制造漂移
irec config set days_tol 10

# 导入审计包，应检测到配置漂移并警告，但导入仍然成功
irec audit import may_2026_final.irecaudit --as "audit_drift_demo"
# => 输出：
#   [提示] 配置漂移检测：共 X 项与当前工作目录配置不同
#          - date_tolerance_days: 审计包=3, 当前=10
#          加 --apply-config 可覆盖为审计包中的配置
#   [OK] 审计包已导入为会话 'audit_drift_demo'
#   新会话ID / 来源审计包ID / 原会话
#   配置漂移项：date_tolerance_days
#   重复来源文件：X 个
#   状态面板（导入后的会话数据）

# 恢复配置
irec config set days_tol 3

# ======================
# 4. 冲突处理三分支演示
# ======================

# 先创建一个同名空会话，用于制造冲突
irec session create audit_conflict_demo

# 分支 A：--reject 拒绝重名导入
irec audit import may_2026_final.irecaudit --as "audit_conflict_demo" --reject
# => Error: 会话 'audit_conflict_demo' 已存在 (冲突模式: reject).
#           可使用 --overwrite 覆盖或指定 --as <新名称> 另存新副本

# 验证原会话未被改动（仍为空）
irec status -s audit_conflict_demo
# => 发票 0, 流水 0, 匹配 0

# 分支 B：--auto-rename 另存新副本
irec audit import may_2026_final.irecaudit --as "audit_conflict_demo" --auto-rename
# => [OK] 审计包已导入为会话 'audit_conflict_demo_restored'
#   [!!] 因重名已自动重命名为 'audit_conflict_demo_restored'（另存新副本）

# 分支 C：--overwrite 强制覆盖
irec audit import may_2026_final.irecaudit --as "audit_conflict_demo" --overwrite --apply-config
# => [OK] 审计包已导入为会话 'audit_conflict_demo'
#   [!!] 已覆盖已存在的同名会话
#   [OK] 配置已恢复为审计包中的配置

# ======================
# 5. 日志回放演示
# ======================

# 创建一个新的空会话
irec init audit_replay_target

# 将审计包中的操作日志回放到空会话（只追加历史，不还原业务数据）
irec audit replay may_2026_final.irecaudit -s audit_replay_target
# => [OK] 已回放 N 条操作日志到会话 'audit_replay_target'
#   动作类型分布：import_invoice / import_transaction / auto_match /
#                manual_match / suspend_invoice / audit_export 等

# 验证：只有历史被追加，业务数据为空
irec status -s audit_replay_target
# => 发票 0, 流水 0, 匹配 0 （业务数据为空）
irec show history -n 10 -s audit_replay_target
# => 可看到所有导入、匹配、挂起、撤销等操作历史

# ======================
# 6. 重复来源检查
# ======================

# 检查审计包与现有会话的导入来源是否有重复
irec audit check-sources may_2026_final.irecaudit -s default
# => 输出：
#   审计包来源文件（2 个）:
#     0e5c4d...  invoices_good.csv @ 2026-xx-xx  (发票8张, 流水0笔)
#     544b29...  transactions_good.csv @ 2026-xx-xx  (发票0张, 流水10笔)
#   目标会话来源文件（2 个）:
#     0e5c4d...  invoices_good.csv @ ...
#     544b29...  transactions_good.csv @ ...
#   重复导入来源: 2 个
#   仅在审计包中导入的文件: 0 个
#   仅在目标会话中导入的文件: 0 个

# ======================
# 7. 导入后验证（归档前后完全对齐）
# ======================

# 切换到导入的会话，验证所有数据与导出前一致
irec session switch audit_drift_demo

# 7.1 状态面板对比
irec status
# => 发票总数 8 / 已匹配 7 / 未匹配 0 / 挂起 1
#    流水总数 10 / 已匹配 8 / 未匹配 2 / 挂起 0
#    匹配活动 7 / 已撤销 1 / 历史操作 N / 已导入文件 2
#    所有合计金额与导出前完全一致

# 7.2 未匹配列表对比
irec show unmatched
# => 与导出前的 audit_before_unmatched.txt 内容完全一致

# 7.3 匹配明细对比
irec show matches --all
# => 活动匹配 + 已撤销匹配，数量和明细与导出前完全一致
#    人工备注仍在："差额300元为手续费，财务确认入账"
#    挂起原因仍在："合同金额争议，等待商务确认"
#    撤销记录仍在

# 7.4 历史记录对比
irec show history -n 20
# => 包含完整的操作历史链，新增 audit_import 记录
#    audit_import 记录包含：source_audit_id、conflict_mode、
#    apply_config、config_drift_detected、duplicate_source_files 等详情

# ======================
# 8. 跨重启恢复验证
# ======================

# 记录导入后的关键信息
irec status > audit_after_status.txt
irec show unmatched > audit_after_unmatched.txt
irec show matches --all > audit_after_matches.txt

# 模拟重启（重新读取会话数据）
irec session list
irec status -s audit_drift_demo
# => session_id、所有合计数字、未匹配列表、匹配明细、历史记录
#    均与导入完成时完全一致

# 验证归档前后状态一致
# （手动比对 audit_before_status.txt vs audit_after_status.txt 的核心数字）

# 清理演示会话
irec session switch default
irec session delete audit_drift_demo --yes
irec session delete audit_conflict_demo --yes
irec session delete audit_conflict_demo_restored --yes
irec session delete audit_replay_target --yes
```

### 步骤 8：一键自动化回归

```bash
# 原边界场景回归（自动匹配链路A/B + 重启持久化）
python _test_boundary_regression.py
# => [ALL PASSED] 边界场景回归通过，exit 0

# 快照功能回归（A往返 / B重启 / C冲突 / D覆盖·配置·版本 全部分支）
python _test_snapshot_regression.py
# => [ALL PASSED] 快照功能回归测试全部通过，exit 0
# 覆盖子断言：
#   导出→导入 发票/流水/匹配/撤销/挂起/备注/历史 全部一致
#   重启：session_id/核心数字/未匹配列表/备注/挂起/撤销 全部不变
#   冲突：--reject 拒绝、--auto-rename 重命名、check-conflicts 来源检测
#   覆盖：--overwrite 覆盖 + --apply-config 配置恢复
#   配置缺失提示 + 版本不兼容拒绝 + 删除 + 参数互斥校验  全部分支正确

# 审计包功能回归（A往返 / B重启 / C冲突 / D配置漂移·缺失·版本·重复来源 / E日志回放 / F list/info/delete / G内容完整性）
python _test_audit_regression.py
# => [ALL PASSED] 审计包功能回归测试全部通过，exit 0
# 覆盖子断言：
#   [A] 导出→导入往返：发票/流水/匹配/撤销/挂起/备注/历史/指纹 全部一致
#   [B] 跨重启恢复：会话ID、核心数字、未匹配列表、备注、挂起、撤销 全部不变
#   [C] 冲突提示：--reject 拒绝、--auto-rename 另存新副本、check-sources 来源检测
#   [D] 配置漂移/配置缺失/版本不兼容/重复来源 全部正确检测
#   [E] 日志回放：只追加历史，不还原业务数据，动作类型齐全
#   [F] list/info/delete 命令 + 参数互斥校验 全部正常
#   [G] 归档内容：摘要、配置、明细、撤销挂起、指纹、日志、报告、会话数据 完整
```

---

## 验收要点清单

| 验收项 | 验证命令 / 位置 | 期望结果 |
|--------|----------------|---------|
| 样例发票导入 | `irec imp invoice samples/invoices_good.csv` | 成功，导入 8 张 |
| 样例流水导入 | `irec imp txn samples/transactions_good.csv` | 成功，导入 10 笔 |
| 非法行带行号报错 | `irec imp invoice samples/invoices_bad.csv` | 报错信息形如 "第2行: ..." |
| 重复导入拦截 | 再次执行 `irec imp invoice samples/invoices_good.csv` | 提示已导入，不入账 |
| 自动匹配 ≥1 笔 | `irec match` 后 `irec show matches` | 至少一对一匹配（华为/阿里/腾讯/字节/京东/美团多对一） |
| 人工匹配 1 笔 | `irec manual match -i INV-2026-008 -t TXN20260620009` | 成功，状态刷新 |
| 超出容差一对多保持未解决 | `irec show unmatched` 中百度项 | INV-2026-005 金额 18900 不与 9400 强制多对一 |
| **链路A：首个命中组合日期不合格但后续组合应成功匹配** | `irec match` 导入 `*_regression.csv` 后查看 matches | INV-2026-009 应与 TXN...013+...014（日期合格）匹配，**不能**匹配 ...011+...012（日期超容差），也**不能**留在未匹配 |
| **链路B：一对多金额命中但全部候选组合超日期容差 → 保持未解决** | `irec show unmatched` + `irec report` 的 unmatched_* | INV-2026-010 及 ...015/...016 留在未匹配列表；注意与"普通金额凑不出"的 INV-2026-005/008 区分类别，验收时不能把该边界误判为普通金额不符 |
| 历史记录完整 | `irec show history` | 导入、匹配、挂起、撤销均有记录 |
| 会话重启持久化 | `irec status` 重启后对照 | session_id、合计、未处理、撤销记录、链路A/B 匹配结果、未匹配列表均不变 |
| 报告导出 | `irec report` | JSON + 5 张 CSV 生成，含链路A拼多多匹配明细、链路B拼多多未匹配条目 |
| **快照导出** | `irec snapshot export -o backup` | `.irec_snapshots/backup.irecsnap` 生成，含 metadata/session/config + SHA-256 完整性 hash |
| **快照导入-无冲突** | `irec snapshot import backup --as restored --switch` | 新会话创建成功，发票/流水/匹配/挂起/撤销/人工备注/历史数据量与内容完全一致；切换成功 |
| **快照导入-冲突--reject** | 同名会话存在时加 `--reject` | 报错被明确拒绝，原会话数据未被改动 |
| **快照导入-冲突--auto-rename** | 同名会话存在时加 `--auto-rename` | 自动加 `_restored` 后缀导入，原会话不变 |
| **快照导入-冲突--overwrite** | 同名会话存在时加 `--overwrite` | 原会话被覆盖，新数据正确加载；新历史含 snapshot_import |
| **快照导入-配置缺失** | 导入删除 config 字段的快照 | 输出提示列出缺失配置项名称，使用默认值填充，导入仍成功 |
| **快照导入-版本不兼容** | 导入主版本号 v99.0 的快照 | 明确报错：主版本不兼容 + 拒绝导入 |
| **快照导入-hash 校验** | 篡改快照内容后导入 | 弹出确认警告，用户取消则终止导入 |
| **快照 check-conflicts** | `irec snapshot check-conflicts backup -s 某会话` | 对比并列出双方都导入过的重复CSV来源哈希 |
| **导入/恢复写入历史** | `irec show history --action snapshot_import` & `snapshot_export` | 导出会话在原会话历史中记录 snapshot_export；导入会话在新会话历史中记录 snapshot_import（含来源快照ID、冲突模式、是否恢复配置等详情） |
| **跨重启一致性** | 导入后 `irec status` → 重启CLI → 再 `irec status` | 新会话 ID、发票/流水/匹配/撤销/挂起总数、未匹配列表内容与顺序、人工备注、挂起原因、撤销原因、报表汇总 全部与导入完成时一致 |
| **snapshot list / info / delete** | 三个命令分别执行 | list 列出所有快照含原会话与数据量；info 展示版本兼容✓/完整性✓/配置✓ 三级标记；delete --yes 物理删除文件 |
| **审计包导出** | `irec audit export -o may_2026_final -n "结账归档" --operator 张三` | `.irec_audits/may_2026_final.irecaudit` 生成，含 14 个文件（摘要/配置/明细CSV/撤销挂起CSV/指纹/JSONL日志/完整报告/会话数据）+ SHA-256 完整性 hash |
| **审计包导入-无冲突** | `irec audit import may_2026_final.irecaudit --as restored --switch` | 新会话创建成功，发票/流水/匹配/挂起/撤销/人工备注/历史/指纹 数据量与内容完全一致；切换成功 |
| **审计包导入-配置漂移** | 修改 `days_tol` 后导入，不加 `--reject` | 检测到配置漂移并警告（列出 date_tolerance_days 差异），但导入仍然成功（仅警告） |
| **审计包导入-冲突--reject** | 同名会话存在时加 `--reject` | 报错被明确拒绝，原会话数据未被改动 |
| **审计包导入-冲突--auto-rename** | 同名会话存在时加 `--auto-rename` | 自动加 `_restored` 后缀导入（另存新副本），原会话不变 |
| **审计包导入-冲突--overwrite** | 同名会话存在时加 `--overwrite` | 原会话被覆盖，新数据正确加载；新历史含 audit_import |
| **审计包导入-配置缺失** | 导入删除 config 字段的审计包 | 输出提示列出缺失配置项名称，使用默认值填充，导入仍成功 |
| **审计包导入-版本不兼容** | 导入主版本号 v99.0 的审计包 | 明确报错：主版本不兼容 + 拒绝导入 |
| **审计包导入-hash 校验** | 篡改审计包内容后导入 | 弹出确认警告，用户取消则终止导入 |
| **审计包 check-sources** | `irec audit check-sources may_2026_final.irecaudit -s 某会话` | 对比并列出双方都导入过的重复CSV来源哈希 |
| **审计包 replay 日志回放** | `irec audit replay may_2026_final.irecaudit -s 空会话` | 只追加操作历史（N条），不还原业务数据（发票/流水/匹配仍为0）；动作类型齐全（import/match/suspend/reverse等） |
| **导入/恢复/回写入历史** | `irec show history --action audit_import` & `audit_export` & `audit_replay` | 三个动作均有详细历史记录，audit_import 含 source_audit_id、conflict_mode、apply_config、config_drift、duplicate_sources 等详情 |
| **跨重启一致性** | 导入后 `irec status` → 重启CLI → 再 `irec status` | 新会话 ID、发票/流水/匹配/撤销/挂起总数、未匹配列表内容与顺序、人工备注、挂起原因、撤销原因、报表汇总 全部与导入完成时一致 |
| **audit list / info / delete** | 三个命令分别执行 | list 列出所有审计包含原会话与数据量；info 展示版本兼容✓/完整性✓/配置✓/漂移项/指纹 多级标记；delete --yes 物理删除文件 |
| **一键回归脚本** | `python _test_audit_regression.py` | 输出 `[ALL PASSED]` + exit 0，覆盖 [A]往返/[B]重启/[C]冲突/[D]漂移·缺失·版本·重复来源/[E]回放/[F]list/info/delete/[G]内容完整性 七大类 |
| **一键回归脚本** | `python _test_snapshot_regression.py` | 输出 `[ALL PASSED]` + exit 0，覆盖 A往返/B重启/C冲突/D覆盖 四大类 |

---

## 常见问题

**Q: 表头列名不完全一致怎么办？**
A: 查看 `importer.py` 中的 `INVOICE_COLUMN_ALIASES` 和 `TXN_COLUMN_ALIASES`，已覆盖常见中英文列名，可按需扩展。

**Q: 编码乱码？**
A: 已自动探测 utf-8-sig / utf-8 / gbk / gb18030，大多数 Excel 导出的 CSV 可正常处理。

**Q: 如何同时运行多个独立核对任务？**
A: `irec session create 2026Q2` 后 `irec session switch 2026Q2`，每个会话完全独立存储。
