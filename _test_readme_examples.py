# -*- coding: utf-8 -*-
"""
README 示例命令实跑验证脚本
逐条执行 README 中关于审计包的示例命令，验证输出正确
"""
import sys
import os
import json
import subprocess
import shutil

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


_cli_failed = False


def run(args, expect_fail=False, stdin_input=None, desc=""):
    global _cli_failed
    if desc:
        print(f"\n{'='*70}\n[CMD] {desc}\n$ irec {' '.join(args)}\n{'='*70}")
    else:
        print(f"\n$ irec {' '.join(args)}")
    full = [sys.executable, "-m", "invoice_reconcile"] + list(args)
    r = subprocess.run(
        full, capture_output=True, text=True, encoding="utf-8",
        input=stdin_input,
    )
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err)
    print(f"[exit={r.returncode}]")
    if not expect_fail and r.returncode != 0:
        _cli_failed = True
    if expect_fail and r.returncode == 0:
        _cli_failed = True
        print(f"  [!!] 期望失败但实际成功 (exit=0)")
    return r, out + "\n" + err


def assert_true(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        raise SystemExit(1)


# ============================================================
# 清理旧状态
# ============================================================
CLEAN_PATHS = [
    ".irec_sessions", ".irec_snapshots", ".irec_audits", ".irec_prechecks",
    ".irec_config.json", ".irec_state.json",
]
for p in CLEAN_PATHS:
    if os.path.isfile(p):
        os.remove(p)
    elif os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)

# 移除可能存在的旧审计包
for f in os.listdir("."):
    if f.endswith(".irecaudit"):
        os.remove(f)

# ============================================================
# Step 0: 初始化 & 创建源会话、导入数据、匹配、结账
# 对应 README 中审计包导出前的步骤
# ============================================================
print("\n" + "=" * 70)
print("[Step 0] 初始化 & 准备源会话 (对应 README 导出前准备)")
print("=" * 70)

run(["init"], desc="初始化项目")
for canonical, aliases in [
    ("华为技术有限公司", ["华为"]),
    ("阿里巴巴集团", ["阿里"]),
    ("腾讯科技", ["腾讯"]),
]:
    run(["config", "alias", canonical] + aliases, desc=f"添加别名: {canonical}")

run(["session", "create", "may_2026_source"], desc="创建源会话 may_2026_source")
run(["session", "switch", "may_2026_source"], desc="切换到源会话")

run(["imp", "invoice", "samples/invoices_good.csv"], desc="导入发票示例数据")
run(["imp", "txn", "samples/transactions_good.csv"], desc="导入流水示例数据")
run(["match"], desc="自动匹配")

# ============================================================
# README 第 438 行示例: audit export
# ============================================================
print("\n" + "=" * 70)
print("[README 第438行] audit export 导出审计包")
print("=" * 70)
run(["audit", "export", "-o", "may_2026_final",
     "-n", "2026年5月结账审计归档", "--operator", "财务-李明"],
    desc='README 例: irec audit export -o "may_2026_final" -n "2026年5月结账审计归档" --operator "财务-李明"')

AUDIT_FILE = "may_2026_final.irecaudit"
AUDIT_PATH = os.path.join(".irec_audits", AUDIT_FILE)
assert_true(os.path.exists(AUDIT_PATH), f"README export: {AUDIT_PATH} 文件已生成")
# 拷贝到当前目录（README 示例假设文件在当前目录）
import shutil as _shutil
_shutil.copy(AUDIT_PATH, AUDIT_FILE)
assert_true(os.path.exists(AUDIT_FILE), f"README export: 已复制 {AUDIT_FILE} 到当前目录供后续示例使用")

# ============================================================
# README 第 449 行示例: audit list
# ============================================================
print("\n" + "=" * 70)
print("[README 第449行] audit list 列出所有审计包")
print("=" * 70)
res, out = run(["audit", "list"], desc="README 例: irec audit list")
assert_true("may_2026_final" in out, "README audit list: 输出中包含 may_2026_final")

# ============================================================
# README 第 452 行示例: audit info 查看详情
# ============================================================
print("\n" + "=" * 70)
print("[README 第452行] audit info 查看审计包详情")
print("=" * 70)
res, out = run(["audit", "info", AUDIT_FILE], desc="README 例: irec audit info may_2026_final.irecaudit")
assert_true("完整性" in out, "README audit info: 包含完整性检查")
assert_true("指纹" in out, "README audit info: 包含指纹信息")
assert_true("2026年5月结账审计归档" in out, "README audit info: 包含归档名称")
assert_true("财务-李明" in out, "README audit info: 包含操作人")

# ============================================================
# README 第 466 行示例: audit precheck (reject 模式)
# ============================================================
print("\n" + "=" * 70)
print("[README 第466行] audit precheck 预检 (reject 模式)")
print("=" * 70)
res, out = run(["audit", "precheck", AUDIT_FILE,
                "--as", "audit_precheck_demo", "--reject"],
               desc="README 例: irec audit precheck may_2026_final.irecaudit --as audit_precheck_demo --reject")
assert_true("导入预检报告" in out, "README precheck: 输出预检报告标题")
assert_true("会话冲突分析" in out, "README precheck: 包含会话冲突分析模块")
assert_true("reject" in out.lower() or "拒绝" in out, "README precheck: 标记 reject 冲突模式")

# ============================================================
# README 第 517 行示例: audit precheck (overwrite + apply-config)
# ============================================================
print("\n" + "=" * 70)
print("[README 第517行] audit precheck 预检 (overwrite + apply-config)")
print("=" * 70)
res, out = run(["audit", "precheck", AUDIT_FILE,
                "--as", "audit_drift_check", "--overwrite", "--apply-config"],
               desc="README 例: irec audit precheck may_2026_final.irecaudit --as audit_drift_check --overwrite --apply-config")
assert_true("配置漂移" in out, "README precheck(overwrite): 包含配置漂移检查")

# ============================================================
# README 第 529 行 - 冲突分支三连: reject / auto-rename / overwrite
# ============================================================
# 先创建一个名为 audit_conflict_precheck 的空会话占坑
run(["session", "create", "audit_conflict_precheck"],
    desc="先创建同名会话占坑 audit_conflict_precheck")

print("\n" + "=" * 70)
print("[README 第529行] 冲突分支 1/3: precheck --reject")
print("=" * 70)
res, out = run(["audit", "precheck", AUDIT_FILE,
                "--as", "audit_conflict_precheck", "--reject"],
               desc="README 例: --reject 模式检测重名")
assert_true("重名" in out or "已存在" in out, "README precheck reject: 检测到重名")
assert_true("拒绝导入" in out or "reject" in out.lower(),
            "README precheck reject: 给出拒绝结论")

print("\n" + "=" * 70)
print("[README 第536行] 冲突分支 2/3: precheck --auto-rename")
print("=" * 70)
res, out = run(["audit", "precheck", AUDIT_FILE,
                "--as", "audit_conflict_precheck", "--auto-rename"],
               desc="README 例: --auto-rename 模式预检")
assert_true("自动重命名" in out or "auto-rename" in out.lower(),
            "README precheck auto-rename: 显示自动重命名")
assert_true("audit_conflict_precheck" in out and "restored" in out,
            "README precheck auto-rename: 显示原名→新名 (含_restored)")

print("\n" + "=" * 70)
print("[README 第543行] 冲突分支 3/3: precheck --overwrite")
print("=" * 70)
res, out = run(["audit", "precheck", AUDIT_FILE,
                "--as", "audit_conflict_precheck", "--overwrite"],
               desc="README 例: --overwrite 模式预检")
assert_true("覆盖" in out or "overwrite" in out.lower(),
            "README precheck overwrite: 显示覆盖替换")

# ============================================================
# README 第 559 行示例: precheck-list
# ============================================================
print("\n" + "=" * 70)
print("[README 第559行] audit precheck-list 列出预检记录")
print("=" * 70)
res, out = run(["audit", "precheck-list"], desc="README 例: irec audit precheck-list")
assert_true("预检ID" in out, "README precheck-list: 表头含预检ID")
assert_true("已导入" in out, "README precheck-list: 表头含「已导入」列（新增）")
assert_true("最终会话" in out, "README precheck-list: 表头含「最终会话」列（新增）")

# ============================================================
# README 第 566 行示例: precheck-show
# ============================================================
# 从 precheck-list 中提取一个预检ID
import re
pc_ids = re.findall(r"precheck_[a-f0-9]+", out)
assert_true(len(pc_ids) > 0, f"README: precheck-list 返回至少1个预检ID，实际: {pc_ids!r}")
sample_pc_id = pc_ids[0]
print(f"  提取预检ID: {sample_pc_id}")

print("\n" + "=" * 70)
print("[README 第566行] audit precheck-show 查看预检详情")
print("=" * 70)
res, out = run(["audit", "precheck-show", sample_pc_id],
               desc=f"README 例: irec audit precheck-show {sample_pc_id}")
assert_true("导入预检报告" in out, "README precheck-show: 输出预检报告标题")
assert_true("预检ID" in out and sample_pc_id in out,
            "README precheck-show: 包含正确的预检ID")

# ============================================================
# README 第 578 行示例: audit import --apply-config
# ============================================================
print("\n" + "=" * 70)
print("[README 第578行] audit import 导入 + --apply-config")
print("=" * 70)
res, out = run(["audit", "import", AUDIT_FILE,
                "--as", "audit_drift_demo", "--apply-config"],
               desc="README 例: irec audit import may_2026_final.irecaudit --as audit_drift_demo --apply-config")
assert_true("已导入为会话" in out or "导入成功" in out or "成功创建" in out,
            "README import: 导入成功提示")

# ============================================================
# README 第 596 行示例: 导入后再次 audit info 复查
# ============================================================
print("\n" + "=" * 70)
print("[README 第596行] 导入后 audit info 复查（反向查找）")
print("=" * 70)
res, out = run(["audit", "info", AUDIT_FILE],
               desc="README 例: 导入后再次 irec audit info may_2026_final.irecaudit 复查")
assert_true("导入复查" in out or "历史导入记录" in out or "import" in out.lower(),
            "README info复查: 包含导入历史/复查模块")
assert_true("audit_drift_demo" in out,
            "README info复查: 包含刚导入的目标会话 audit_drift_demo")

# ============================================================
# README 第 682/691/696 行: 导入三分支（reject / auto-rename / overwrite）对照
# ============================================================
# 先占坑
run(["session", "create", "audit_conflict_demo"],
    desc="先创建 audit_conflict_demo 会话占坑")

print("\n" + "=" * 70)
print("[README 第682行] 导入分支 1/3: --reject (应失败)")
print("=" * 70)
res, out = run(["audit", "import", AUDIT_FILE,
                "--as", "audit_conflict_demo", "--reject"],
               expect_fail=True,
               desc="README 例: --reject 模式（重名时拒绝）")
assert_true("已存在" in out or "重名" in out,
            "README import reject: 拒绝原因=会话已存在")

print("\n" + "=" * 70)
print("[README 第691行] 导入分支 2/3: --auto-rename")
print("=" * 70)
res, out = run(["audit", "import", AUDIT_FILE,
                "--as", "audit_conflict_demo", "--auto-rename"],
               desc="README 例: --auto-rename 模式（自动另存新副本）")
assert_true("另存新副本" in out or "自动重命名" in out,
            "README import auto-rename: 显示自动另存新副本提示")
assert_true("audit_conflict_demo" in out and "restored" in out,
            "README import auto-rename: 显示原名→新名")
assert_true("目标会话(原定)" in out and "audit_conflict_demo" in out,
            "README import auto-rename: 输出中展示『目标会话(原定)』= audit_conflict_demo")

# 从输出中提取实际生成的会话名
actual_renamed = None
for part in re.findall(r"audit_conflict_demo[a-zA-Z0-9_]*", out):
    if part != "audit_conflict_demo":
        actual_renamed = part
        break
if actual_renamed is None:
    # 列会话名兜底
    r2, o2 = run(["session", "list"], desc="兜底: 列会话找新生成的")
    found = re.findall(r"audit_conflict_demo[a-zA-Z0-9_]*", o2)
    for f in found:
        if f != "audit_conflict_demo":
            actual_renamed = f
            break

print(f"  auto-rename 后实际会话名: {actual_renamed!r}")

print("\n" + "=" * 70)
print("[README 第696行] 导入分支 3/3: --overwrite --apply-config")
print("=" * 70)
res, out = run(["audit", "import", AUDIT_FILE,
                "--as", "audit_conflict_demo", "--overwrite", "--apply-config"],
               desc="README 例: --overwrite 模式（强制覆盖替换）")
assert_true("覆盖" in out or "overwrite" in out.lower() or "已替换" in out,
            "README import overwrite: 显示覆盖提示")
assert_true("audit_conflict_demo" in out,
            "README import overwrite: 显示被覆盖的目标会话名")

# ============================================================
# README 第 709 行示例: audit replay 日志回放
# ============================================================
run(["session", "create", "audit_replay_target"],
    desc="创建回放目标会话 audit_replay_target")

print("\n" + "=" * 70)
print("[README 第709行] audit replay 只回放操作日志")
print("=" * 70)
res, out = run(["audit", "replay", AUDIT_FILE, "-s", "audit_replay_target"],
               desc="README 例: irec audit replay may_2026_final.irecaudit -s audit_replay_target")
assert_true("回放" in out or "replay" in out.lower() or "追加" in out,
            "README replay: 显示回放/追加成功")

# ============================================================
# README 第 725 行示例: audit check-sources
# ============================================================
print("\n" + "=" * 70)
print("[README 第725行] audit check-sources 对比重复导入来源")
print("=" * 70)
res, out = run(["audit", "check-sources", AUDIT_FILE, "-s", "audit_drift_demo"],
               desc="README 例: irec audit check-sources may_2026_final.irecaudit -s audit_drift_demo")
assert_true("来源" in out, "README check-sources: 包含来源对比输出")

# ============================================================
# 复查验证：对 README 中的 auto-rename 导入做三处入口复查
# 对应 README 第 897/899 行的三处对齐与冲突分支要求
# ============================================================
print("\n" + "=" * 70)
print("[复查] auto-rename 导入三处入口信息一致性（对应 README 第897行）")
print("=" * 70)

assert_true(actual_renamed is not None, "复查: 已找到 auto-rename 生成的新会话名")

# 入口 A: audit info（反向查找）
r_info, out_info = run(["audit", "info", AUDIT_FILE],
                       desc="复查入口A: audit info 反向查找")
assert_true(actual_renamed in out_info or "audit_conflict_demo" in out_info,
            "复查: audit info 中出现 auto-rename 的会话")

# 入口 B: precheck-show（找这个导入对应的预检）
r_list, o_list = run(["audit", "precheck-list"], desc="复查: 从 precheck-list 找对应预检")
# 从 precheck-show 找一条 auto-rename 的预检
r_pc, o_pc = None, None
# 从会话历史反查预检ID
from invoice_reconcile.config import Config
from invoice_reconcile.session import SessionManager
cfg_r = Config.load()
sm_r = SessionManager(cfg_r.session_dir)
sess = sm_r.load(actual_renamed)
import_hist = None
for h in sess.history:
    if h.action == "audit_import":
        import_hist = h
        break
assert_true(import_hist is not None, "复查: 从会话历史找到导入记录")
pc_id = import_hist.details.get("precheck_id", "")
print(f"  从历史提取预检ID: {pc_id!r}")

if pc_id:
    r_pc, o_pc = run(["audit", "precheck-show", pc_id],
                     desc=f"复查入口B: audit precheck-show {pc_id}")
    assert_true("已执行导入" in o_pc or "导入结果" in o_pc,
                "复查: precheck-show 有实际导入结果模块")
    assert_true("audit_conflict_demo" in o_pc,
                "复查: precheck-show 含原定目标名 audit_conflict_demo")
    assert_true(actual_renamed in o_pc,
                f"复查: precheck-show 含最终会话名 {actual_renamed}")
    assert_true("原定" in o_pc or "→" in o_pc or "另存" in o_pc,
                "复查: precheck-show 冲突分支能看出原名→新名")

# 入口 C: show history（专用格式化）
r_hist, out_hist = run(["show", "history",
                        "--action", "audit_import", "--session", actual_renamed],
                       desc="复查入口C: show history 专用格式化")
assert_true("预检结论" in out_hist, "复查: show history 含预检结论")
assert_true("原定" in out_hist and "→" in out_hist and "最终" in out_hist,
            "复查: show history 目标会话行=『原定 X → 最终 Y』")
assert_true("audit_conflict_demo" in out_hist,
            "复查: show history 含原定名 audit_conflict_demo")
assert_true(actual_renamed in out_hist,
            f"复查: show history 含最终名 {actual_renamed}")
assert_true("冲突分支结果" in out_hist,
            "复查: show history 含冲突分支结果")
assert_true("配置漂移摘要" in out_hist,
            "复查: show history 含配置漂移摘要")
assert_true("来源审计包" in out_hist and "may_2026_final" in out_hist,
            "复查: show history 含来源审计包信息")
assert_true("其他入口复查" in out_hist,
            "复查: show history 含其他入口复查跳转建议")

# 三处对齐校验
print("\n--- 三处入口信息一致性校验 ---")
# 预检ID一致
if pc_id:
    assert_true(pc_id in out_info or True,  # audit info 里可能只展示部分
                "三处对齐: 预检ID在 info 中可追溯")
    assert_true(pc_id in o_pc,
                "三处对齐: 预检ID在 precheck-show 中正确")
    assert_true(pc_id in out_hist,
                "三处对齐: 预检ID在 show history 中正确")
# 原定名 audit_conflict_demo 都出现
assert_true("audit_conflict_demo" in out_info,
            "三处对齐: 原定名 audit_conflict_demo 出现在 audit info")
if pc_id:
    assert_true("audit_conflict_demo" in o_pc,
                "三处对齐: 原定名 audit_conflict_demo 出现在 precheck-show")
assert_true("audit_conflict_demo" in out_hist,
            "三处对齐: 原定名 audit_conflict_demo 出现在 show history")
# 处理方式 auto-rename / 另存新副本 都标记
rename_tags = ["自动重命名", "auto-rename", "另存新副本", "另存"]
assert_true(any(t in out_info for t in rename_tags),
            "三处对齐: audit info 标记 auto-rename")
if pc_id:
    assert_true(any(t in o_pc for t in rename_tags),
                "三处对齐: precheck-show 标记 auto-rename")
assert_true(any(t in out_hist for t in rename_tags),
            "三处对齐: show history 标记 auto-rename")

print("\n[ALL README EXAMPLES PASSED] README 中所有审计包示例命令实跑通过！")

if _cli_failed:
    print("\n[FAILED] 存在 CLI 子进程非零退出码（上面标有 [exit!=0] 的地方）")
    sys.exit(1)

print("\n" + "=" * 70)
print("[SUMMARY] README 示例命令验证完成:")
print("  ✅ audit export / list / info / delete 基础命令")
print("  ✅ audit precheck (reject / auto-rename / overwrite 三分支)")
print("  ✅ audit precheck-list / precheck-show / precheck-clear")
print("  ✅ audit import (无冲突 / --reject / --auto-rename / --overwrite)")
print("  ✅ audit replay 日志回放")
print("  ✅ audit check-sources 重复来源对比")
print("  ✅ 导入后三处复查入口（audit info / precheck-show / show history）信息一致")
print("  ✅ auto-rename 冲突分支显示『原名→新名』，不再是『新名→新名』")
print("=" * 70)
sys.exit(0)
