# -*- coding: utf-8 -*-
"""
审计包（Audit Package）功能回归测试
覆盖 7 大类场景：
  A. 导出 → 导入往返（数据一致性校验）
  B. 跨重启恢复（重新加载会话不变）
  C. 冲突提示（重名会话分支：拒绝/另存新副本/覆盖）
  D. 配置漂移 + 配置缺失 + 版本不兼容 + 重复来源检测
  E. 日志回放（replay）
  F. list / info / delete 命令
  G. 归档内容验证（摘要、配置、明细、撤销挂起、指纹、日志、报告、会话数据）
"""
import sys
import os
import json
import subprocess
import shutil
import tempfile
import zipfile
import re

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


def run(args, expect_fail=False, stdin_input=None):
    global _cli_failed
    full = [sys.executable, "-m", "invoice_reconcile"] + list(args)
    print(f"\n$ irec {' '.join(args)}" + (" [expect_fail]" if expect_fail else ""))
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


def file_contains(path, text):
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        return text in f.read()


# ============================================================
# 准备工作：清理旧状态 & 初始化
# ============================================================
CLEAN_PATHS = [
    ".irec_sessions",
    ".irec_snapshots",
    ".irec_audits",
    ".irec_config.json",
    ".irec_state.json",
    "audit_irec_report",
    "audit_irec_report.json",
    "audit_restored_report",
    "audit_restored_report.json",
]
for p in CLEAN_PATHS:
    if os.path.isfile(p):
        os.remove(p)
    elif os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)

SESS_SRC = "audit_source"
SESS_RESTORED = "audit_restored"
SESS_CONFLICT = "audit_conflict"
SESS_REPLAY = "audit_replay_target"

print("=" * 70)
print("准备阶段：初始化配置并创建源会话")
print("=" * 70)

run(["init"])
for canonical, aliases in [
    ("华为技术有限公司", ["华为"]),
    ("阿里巴巴集团", ["阿里"]),
    ("腾讯科技", ["腾讯"]),
    ("字节跳动", ["字节"]),
    ("百度在线", ["百度"]),
    ("京东集团", ["京东"]),
    ("美团点评", ["美团"]),
    ("小米科技", ["小米"]),
]:
    run(["config", "alias", canonical] + aliases)

run(["session", "create", SESS_SRC])
run(["session", "switch", SESS_SRC])

run(["imp", "invoice", "samples/invoices_good.csv"])
run(["imp", "txn", "samples/transactions_good.csv"])
run(["match"])

run(["manual", "match", "-i", "INV-2026-008", "-t", "TXN20260620009",
     "-n", "差额300元为手续费，财务确认入账【审计测试】"])
run(["manual", "suspend-inv", "INV-2026-005", "-r", "合同金额争议，等待商务确认【审计】"])

from invoice_reconcile.config import Config
from invoice_reconcile.session import SessionManager

cfg = Config.load()
sm = SessionManager(cfg.session_dir)
sess = sm.load(SESS_SRC)

inv8_id = None
for inv in sess.invoices.values():
    if inv.invoice_no == "INV-2026-008":
        inv8_id = inv.id
        break
xiaomi_match_id = None
for m in sess.matches.values():
    if inv8_id in m.invoice_ids and not m.reversed and m.match_type == "manual":
        xiaomi_match_id = m.id
        break
assert_true(xiaomi_match_id is not None, "找到小米人工匹配记录用于测试撤销")
run(["manual", "reverse", xiaomi_match_id, "-r", "演示撤销【审计测试】"])
run(["manual", "match", "-i", "INV-2026-008", "-t", "TXN20260620009",
     "-n", "重新匹配：差额300手续费【审计】"])

del cfg, sm, sess
cfg = Config.load()
sm = SessionManager(cfg.session_dir)
sess = sm.load(SESS_SRC)
summary_src = sm.status_summary(sess)

inv_by_no_src = {i.invoice_no: i for i in sess.invoices.values()}
txn_by_no_src = {t.txn_id: t for t in sess.transactions.values()}
matches_src = {m.id: m for m in sess.matches.values()}
active_matches_src = [m for m in matches_src.values() if not m.reversed]
reversed_matches_src = [m for m in matches_src.values() if m.reversed]
history_count_src = len(sess.history)

run(["report", "-f", "both", "-o", "audit_irec_report"])
with open("audit_irec_report.json", "r", encoding="utf-8") as f:
    rpt_src = json.load(f)

history_count_before_export = history_count_src

print()
print("=" * 70)
print("[A] 导出 → 导入往返测试")
print("=" * 70)

# A1: 导出审计包
res, out = run(["audit", "export", "-n", "A类回归测试-完整审计包",
                "--operator", "tester_01", "-o", "audit_A_full.irecaudit"])
assert_true("审计包已导出" in out, "A1: export 命令输出成功标记")

audit_dir = os.path.join(os.getcwd(), ".irec_audits")
audit_path = os.path.join(audit_dir, "audit_A_full.irecaudit")
assert_true(os.path.exists(audit_path), "A1: 审计包文件生成在 .irec_audits/ 下")

# A2: 检查归档结构（zip 内文件齐全）
with zipfile.ZipFile(audit_path, "r") as zf:
    names = zf.namelist()
    required_files = [
        "audit_manifest.json",
        "session_summary.json",
        "config_snapshot.json",
        "match_details.csv",
        "reversal_suspension_records.csv",
        "source_fingerprints.json",
        "operation_log.jsonl",
        "full_report/report.json",
        "full_report/summary.csv",
        "full_report/matches.csv",
        "full_report/unmatched_invoices.csv",
        "full_report/unmatched_transactions.csv",
        "full_report/reversed_matches.csv",
        "session.json",
    ]
    for rf in required_files:
        assert_true(rf in names, f"A2: 归档内包含 {rf}")

    with zf.open("audit_manifest.json") as f:
        manifest_json = json.load(f)
    assert_true("metadata" in manifest_json, "A2: manifest 包含 metadata")
    assert_true("session" in manifest_json, "A2: manifest 包含 session")
    assert_true("config" in manifest_json, "A2: manifest 包含 config")
    assert_true("source_fingerprints" in manifest_json, "A2: manifest 包含 source_fingerprints")
    assert_true("content_hash" in manifest_json, "A2: manifest 包含 content_hash")
    assert_true(manifest_json["metadata"]["audit_version"], "A2: metadata 中含有版本号")
    assert_true(manifest_json["metadata"]["original_session_name"] == SESS_SRC,
                "A2: metadata 中含有原会话名")
    assert_true(manifest_json["metadata"]["operator"] == "tester_01",
                "A2: metadata 中含有操作人")

    with zf.open("session_summary.json") as f:
        summary_audit = json.load(f)
    assert_true(summary_audit["invoices"]["total"] == summary_src["invoices"]["total"],
                "A2: 归档内会话摘要发票数与源一致")

    with zf.open("config_snapshot.json") as f:
        cfg_audit = json.load(f)
    assert_true("amount_tolerance" in cfg_audit, "A2: 配置快照包含 amount_tolerance")
    assert_true("customer_name_aliases" in cfg_audit, "A2: 配置快照包含 customer_name_aliases")

    with zf.open("source_fingerprints.json") as f:
        fps = json.load(f)
    assert_true(len(fps) == 2, f"A2: 来源指纹共 2 个文件，实际 {len(fps)}")

    with zf.open("operation_log.jsonl") as f:
        oplog_lines = [l.decode("utf-8").strip() for l in f if l.strip()]
    assert_true(len(oplog_lines) > 0, "A2: 操作日志不为空")
    first_op = json.loads(oplog_lines[0])
    assert_true("action" in first_op and "details" in first_op and "timestamp" in first_op,
                "A2: 操作日志每条包含 action/details/timestamp")

    with zf.open("match_details.csv") as f:
        content = f.read().decode("utf-8-sig")
    assert_true("匹配ID" in content and "撤销原因" in content,
                "A2: 匹配明细表包含表头关键字")

    with zf.open("reversal_suspension_records.csv") as f:
        rev_content = f.read().decode("utf-8-sig")
    assert_true("撤销匹配" in rev_content and "挂起发票" in rev_content,
                "A2: 撤销挂起记录表包含两类记录")

# A3: audit list / info 正常显示
res, out = run(["audit", "list"])
assert_true("audit_A_full.irecaudit" in out, "A3: audit list 显示导出的文件")

res, out = run(["audit", "info", "audit_A_full.irecaudit"])
assert_true("版本兼容性" in out and "兼容" in out, "A3: audit info 版本兼容标记 ✓")
assert_true("完整性哈希" in out and "通过" in out, "A3: audit info 完整性校验 ✓")
assert_true("配置完整性" in out and "完整" in out, "A3: audit info 配置完整 ✓")
assert_true("配置漂移" in out and "一致" in out, "A3: audit info 配置漂移 ✓")
assert_true("来源文件数" in out, "A3: audit info 显示来源文件数")

# A4: 导入审计包为新会话（不重名，无冲突）
run(["audit", "import", "audit_A_full.irecaudit", "--as", SESS_RESTORED, "--switch"])

# A5: 加载新会话，验证核心数据一致性
cfg2 = Config.load()
sm2 = SessionManager(cfg2.session_dir)
assert_true(sm2.exists(SESS_RESTORED), "A5: 导入会话文件存在")

sess2 = sm2.load(SESS_RESTORED)
summary2 = sm2.status_summary(sess2)

assert_true(summary2["invoices"]["total"] == summary_src["invoices"]["total"],
            f"A5: 发票总数一致 before={summary_src['invoices']['total']} after={summary2['invoices']['total']}")
assert_true(summary2["transactions"]["total"] == summary_src["transactions"]["total"],
            f"A5: 流水总数一致 before={summary_src['transactions']['total']} after={summary2['transactions']['total']}")
assert_true(summary2["matches"]["active"] == summary_src["matches"]["active"],
            f"A5: 活动匹配数一致 before={summary_src['matches']['active']} after={summary2['matches']['active']}")
assert_true(summary2["matches"]["reversed"] == summary_src["matches"]["reversed"],
            f"A5: 撤销匹配数一致 before={summary_src['matches']['reversed']} after={summary2['matches']['reversed']}")
assert_true(summary2["invoices"]["suspended"] == summary_src["invoices"]["suspended"],
            f"A5: 挂起发票数一致 before={summary_src['invoices']['suspended']} after={summary2['invoices']['suspended']}")
assert_true(summary2["history_count"] >= history_count_before_export + 2,
            f"A5: 导入会话历史数 >= 原历史数+2 (export动作+import动作): "
            f"before_export={history_count_before_export} after_import={summary2['history_count']}")

# A6: 未匹配列表内容完全一致
unmatched_invs_src = sorted(
    [i.invoice_no for i in sess.invoices.values() if i.status == "unmatched"]
)
unmatched_invs_dst = sorted(
    [i.invoice_no for i in sess2.invoices.values() if i.status == "unmatched"]
)
assert_true(unmatched_invs_src == unmatched_invs_dst,
            f"A6: 未匹配发票列表一致 before={unmatched_invs_src} after={unmatched_invs_dst}")

unmatched_txns_src = sorted(
    [t.txn_id for t in sess.transactions.values() if t.status == "unmatched"]
)
unmatched_txns_dst = sorted(
    [t.txn_id for t in sess2.transactions.values() if t.status == "unmatched"]
)
assert_true(unmatched_txns_src == unmatched_txns_dst,
            f"A6: 未匹配流水列表一致 before={unmatched_txns_src} after={unmatched_txns_dst}")

# A7: 人工备注存在（小米匹配的备注）
inv8_dst = None
for inv in sess2.invoices.values():
    if inv.invoice_no == "INV-2026-008":
        inv8_dst = inv
        break
assert_true(inv8_dst is not None, "A7: 恢复会话中存在 INV-2026-008")

xiaomi_active_match = None
for m in sess2.matches.values():
    if inv8_dst.id in m.invoice_ids and not m.reversed:
        xiaomi_active_match = m
        break
assert_true(xiaomi_active_match is not None, "A7: 恢复会话中小米发票匹配活动记录存在")
assert_true("差额300手续费" in xiaomi_active_match.notes,
            f"A7: 人工备注被正确恢复 备注={xiaomi_active_match.notes!r}")

# A8: 挂起原因被正确恢复
inv5_dst = None
for inv in sess2.invoices.values():
    if inv.invoice_no == "INV-2026-005":
        inv5_dst = inv
        break
assert_true(inv5_dst is not None and inv5_dst.suspended, "A8: INV-2026-005 挂起状态被恢复")
assert_true("合同金额争议" in inv5_dst.suspend_reason,
            f"A8: 挂起原因被正确恢复 reason={inv5_dst.suspend_reason!r}")

# A9: 撤销记录被正确恢复
rev_count_dst = sum(1 for m in sess2.matches.values() if m.reversed)
rev_count_src = sum(1 for m in sess.matches.values() if m.reversed)
assert_true(rev_count_src == rev_count_dst == 1,
            f"A9: 撤销记录数一致 src={rev_count_src} dst={rev_count_dst}")
rev_match = [m for m in sess2.matches.values() if m.reversed][0]
assert_true("演示撤销" in rev_match.reversed_reason,
            f"A9: 撤销原因被正确恢复 reason={rev_match.reversed_reason!r}")

# A10: 报表汇总完全一致（导出恢复后的报告）
run(["session", "switch", SESS_RESTORED])
run(["report", "-f", "both", "-o", "audit_restored_report"])
with open("audit_restored_report.json", "r", encoding="utf-8") as f:
    rpt_dst = json.load(f)

sum_src = rpt_src["summary"]
sum_dst = rpt_dst["summary"]
for key1 in ["invoices", "transactions", "matches"]:
    for key2 in sum_src[key1].keys():
        assert_true(sum_src[key1][key2] == sum_dst[key1][key2],
                    f"A10: 报表汇总一致 {key1}.{key2}: src={sum_src[key1][key2]} dst={sum_dst[key1][key2]}")

# A11: 历史日志中包含 audit_export 和 audit_import
sm_src = SessionManager(cfg.session_dir)
sess_src_reloaded = sm_src.load(SESS_SRC)
history_actions_src = [h.action for h in sess_src_reloaded.history]
history_actions_dst = [h.action for h in sess2.history]
assert_true("audit_export" in history_actions_src, "A11: 原会话历史中有 audit_export")
assert_true("audit_import" in history_actions_dst, "A11: 恢复会话历史中有 audit_import")

# 检查 import 历史详情里是否包含冲突模式、配置漂移、重复来源等信息
import_entries = [h for h in sess2.history if h.action == "audit_import"]
assert_true(len(import_entries) == 1, "A11: 恢复会话历史中恰好 1 条 audit_import")
imp_details = import_entries[0].details
assert_true("source_audit_id" in imp_details, "A11: audit_import 详情含 source_audit_id")
assert_true("conflict_mode" in imp_details, "A11: audit_import 详情含 conflict_mode")
assert_true("apply_config" in imp_details, "A11: audit_import 详情含 apply_config")

print()
print("=" * 70)
print("[B] 跨重启恢复测试（重新加载会话不变）")
print("=" * 70)

del cfg, sm, sess, cfg2, sm2, sess2, inv8_dst, inv5_dst, xiaomi_active_match, rev_match
import gc
gc.collect()

cfg_r = Config.load()
sm_r = SessionManager(cfg_r.session_dir)
sess_r = sm_r.load(SESS_RESTORED)
summary_r = sm_r.status_summary(sess_r)

# B1: 会话 ID 和核心数字都完全一致
assert_true(summary_r["session_id"] == summary2["session_id"],
            "B1: 重启后 session_id 不变")
for key1 in ["invoices", "transactions", "matches"]:
    for key2 in summary_r[key1].keys():
        assert_true(summary_r[key1][key2] == summary2[key1][key2],
                    f"B1: 重启后 {key1}.{key2} 不变 重启前={summary2[key1][key2]} 重启后={summary_r[key1][key2]}")

# B2: 未匹配列表完全一致
unmatched_restart_inv = sorted([i.invoice_no for i in sess_r.invoices.values() if i.status == "unmatched"])
assert_true(unmatched_restart_inv == unmatched_invs_dst,
            f"B2: 重启后未匹配发票列表不变 before={unmatched_invs_dst} after={unmatched_restart_inv}")

unmatched_restart_txn = sorted([t.txn_id for t in sess_r.transactions.values() if t.status == "unmatched"])
assert_true(unmatched_restart_txn == unmatched_txns_dst,
            "B2: 重启后未匹配流水列表不变")

# B3: 挂起原因 / 人工备注 / 撤销记录重启后仍对
inv5_r = [i for i in sess_r.invoices.values() if i.invoice_no == "INV-2026-005"][0]
assert_true(inv5_r.suspended and "合同金额争议" in inv5_r.suspend_reason,
            f"B3: 重启后挂起原因仍正确: {inv5_r.suspend_reason!r}")

inv8_r = [i for i in sess_r.invoices.values() if i.invoice_no == "INV-2026-008"][0]
match_r = [m for m in sess_r.matches.values() if inv8_r.id in m.invoice_ids and not m.reversed][0]
assert_true("差额300手续费" in match_r.notes,
            f"B3: 重启后人工备注仍正确: {match_r.notes!r}")

rev_r = [m for m in sess_r.matches.values() if m.reversed][0]
assert_true("演示撤销" in rev_r.reversed_reason,
            f"B3: 重启后撤销原因仍正确: {rev_r.reversed_reason!r}")

# B4: 历史记录包含 audit_import（重启后仍存在）
hist_r = [h.action for h in sess_r.history]
assert_true("audit_import" in hist_r, "B4: 重启后历史仍包含 audit_import")

print()
print("=" * 70)
print("[C] 冲突提示测试（重名会话分支：拒绝/另存新副本/覆盖）")
print("=" * 70)

run(["session", "create", SESS_CONFLICT])

# C1: --reject 应失败（被拒绝）
res, out = run(
    ["audit", "import", "audit_A_full.irecaudit", "--as", SESS_CONFLICT, "--reject"],
    expect_fail=True,
)
assert_true("已存在" in out or "reject" in out.lower(),
            "C1: --reject 模式下重名导入被拒绝并给出明确提示")
sess_c1 = sm_r.load(SESS_CONFLICT)
assert_true(len(sess_c1.invoices) == 0, "C1: --reject 模式下原会话数据未被改动（仍为空）")

# C2: --auto-rename 自动重命名（另存新副本）
res, out = run(["audit", "import", "audit_A_full.irecaudit",
                "--as", SESS_CONFLICT, "--auto-rename"])
assert_true("因重名已自动重命名" in out, "C2: --auto-rename 模式下显示自动重命名提示")
assert_true("另存新副本" in out, "C2: --auto-rename 模式下显示另存新副本提示")

all_sessions = sm_r.list_sessions()
renamed_names = [s["name"] for s in all_sessions
                 if s["name"].startswith(SESS_CONFLICT) and s["name"] != SESS_CONFLICT]
assert_true(len(renamed_names) >= 1, f"C2: 存在被重命名的新会话 {renamed_names}")
sess_c2 = sm_r.load(renamed_names[0])
assert_true(len(sess_c2.invoices) > 0, f"C2: 重命名后的会话中含有发票数据（{len(sess_c2.invoices)}条）")

# C3: check-sources 命令
res, out = run(["audit", "check-sources", "audit_A_full.irecaudit",
                "-s", SESS_RESTORED])
assert_true("重复导入来源" in out or "未发现重复" in out,
            "C3: check-sources 命令正常输出")

print()
print("=" * 70)
print("[D] 配置漂移 + 配置缺失 + 版本不兼容 + 重复来源")
print("=" * 70)

# D1: 制造"配置漂移"：修改当前配置，再导入审计包检测漂移
run(["config", "set", "days_tol", "10"])
cfg_modified = Config.load()
assert_true(cfg_modified.date_tolerance_days == 10, "D1: 修改 days_tol 为 10 成功")

# 导入审计包，配置漂移应该只警告，不会导致失败
res, out = run(["audit", "import", "audit_A_full.irecaudit",
                "--as", "audit_drift_test"])
assert_true("配置漂移" in out, "D1: 导入时检测到配置漂移并给出警告")
assert_true("date_tolerance_days" in out, "D1: 导入警告中列出 date_tolerance_days 漂移项")
assert_true(res.returncode == 0, "D1: 配置漂移场景下导入仍然成功（仅警告）")

# audit info 也应该能检测到配置漂移
res2, out2 = run(["audit", "info", "audit_A_full.irecaudit"])
assert_true("配置漂移" in out2, "D1: audit info 检测到配置漂移")
assert_true("date_tolerance_days" in out2, "D1: audit info 列出 date_tolerance_days 漂移项")

# 恢复配置
run(["config", "set", "days_tol", "3"])

# D2: 制造"配置缺失"的审计包来测试提示
from invoice_reconcile.audit import _compute_audit_content_hash
audit_bad_cfg_path = os.path.join(audit_dir, "audit_D_badcfg.irecaudit")
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("audit_manifest.json") as f:
        d = json.load(f)

d.pop("config", None)
# 重新计算 hash
d["content_hash"] = _compute_audit_content_hash(
    d["session"], {}, d.get("source_fingerprints", {}),
    {k: v for k, v in d["metadata"].items() if k != "created_at" and k != "audit_id"},
    d.get("manifest", {}),
)
with tempfile.TemporaryDirectory() as tmpdir:
    jp = os.path.join(tmpdir, "audit_manifest.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    with zipfile.ZipFile(audit_bad_cfg_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(jp, arcname="audit_manifest.json")

res, out = run(["audit", "import", "audit_D_badcfg.irecaudit", "--as", "audit_D_dest",
                "--auto-rename"])
assert_true("缺少配置项" in out or "缺失" in out,
            "D2: 导入缺少配置的审计包时会给出缺失配置提示")

# D3: 制造"版本不兼容"审计包，应被拒绝导入
audit_bad_ver_path = os.path.join(audit_dir, "audit_D_badver.irecaudit")
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("audit_manifest.json") as f:
        d = json.load(f)
d["metadata"]["audit_version"] = "99.0"
d["content_hash"] = _compute_audit_content_hash(
    d["session"], d.get("config", {}), d.get("source_fingerprints", {}),
    {k: v for k, v in d["metadata"].items() if k != "created_at" and k != "audit_id"},
    d.get("manifest", {}),
)
with tempfile.TemporaryDirectory() as tmpdir:
    jp = os.path.join(tmpdir, "audit_manifest.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    with zipfile.ZipFile(audit_bad_ver_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(jp, arcname="audit_manifest.json")

res, out = run(["audit", "import", "audit_D_badver.irecaudit", "--as", "audit_D_badver_dest"],
               expect_fail=True)
assert_true("版本不兼容" in out or "主版本" in out,
            "D3: 版本不兼容审计包导入被明确拒绝（含原因说明）")

# D4: 重复来源检测
from invoice_reconcile.audit import AuditManager, PrecheckStore
audit_mgr = AuditManager()
analysis = audit_mgr.analyze("audit_A_full.irecaudit", current_config=cfg_modified)
dup_count = len(analysis.get("imported_file_hashes", []))
assert_true(dup_count == 2, f"D4: 审计包分析得到 2 个导入来源，实际 {dup_count}")

print()
print("=" * 70)
print("[E] 日志回放测试（replay）")
print("=" * 70)

# E1: 创建一个空会话作为回放目标
run(["session", "create", SESS_REPLAY])

# E2: 回放审计包的操作日志
res, out = run(["audit", "replay", "audit_A_full.irecaudit", "-s", SESS_REPLAY])
assert_true("操作日志已回放到会话" in out, "E2: replay 命令输出成功标记")

# E3: 检查回放后历史条目数
sess_replay = sm_r.load(SESS_REPLAY)
replay_history_count = len(sess_replay.history)
assert_true(replay_history_count > 0, f"E3: 回放后历史条目数 > 0，实际 {replay_history_count}")

# E4: 回放只追加历史，不还原业务数据（发票数仍为 0）
assert_true(len(sess_replay.invoices) == 0,
            f"E4: 回放仅追加历史记录，不还原业务数据（发票数仍为0，实际 {len(sess_replay.invoices)}）")

# E5: 检查历史动作类型
replay_actions = set(h.action for h in sess_replay.history)
assert_true("import_invoices" in replay_actions, "E5: 回放历史包含 import_invoices")
assert_true("auto_match" in replay_actions or "match" in str(replay_actions),
            "E5: 回放历史包含匹配动作")
assert_true("audit_export" in replay_actions, "E5: 回放历史包含 audit_export")

print()
print("=" * 70)
print("[F] list / info / delete 命令测试")
print("=" * 70)

# F1: list 列出全部
res, out = run(["audit", "list"])
assert_true("审计包存储目录" in out, "F1: audit list 显示存储目录")
assert_true("audit_A_full.irecaudit" in out, "F1: audit list 包含审计包 A")
assert_true("audit_D_badcfg.irecaudit" in out, "F1: audit list 包含审计包 badcfg")

# F2: info 详情
res, out = run(["audit", "info", "audit_A_full.irecaudit"])
assert_true("审计包ID" in out, "F2: audit info 显示审计包ID")
assert_true("完整性哈希" in out, "F2: audit info 显示完整性哈希")
assert_true("来源文件指纹" in out, "F2: audit info 显示来源文件指纹")

# F3: delete 命令正常
res, out = run(["audit", "delete", "audit_D_badver.irecaudit", "--yes"])
assert_true("审计包已删除" in out, "F3: audit delete 成功输出标记")
assert_true(not os.path.exists(os.path.join(audit_dir, "audit_D_badver.irecaudit")),
            "F3: 审计包文件被实际删除")

# F4: 三选一的冲突模式参数互斥校验
res, out = run(["audit", "import", "audit_A_full.irecaudit", "--overwrite", "--reject",
                "--as", "audit_should_fail"], expect_fail=True)
assert_true("三选一" in out or "不能同时" in out,
            "F4: 同时指定 --overwrite 和 --reject 被参数校验拦截")

print()
print("=" * 70)
print("[G] 归档内容字段完整性验证")
print("=" * 70)

# G1: 会话摘要里有完整的统计字段
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("session_summary.json") as f:
        summary = json.load(f)
assert_true("invoices" in summary and "transactions" in summary and "matches" in summary,
            "G1: 会话摘要包含三大核心统计")
for sub_key in ["total", "matched", "unmatched", "suspended"]:
    assert_true(sub_key in summary["invoices"], f"G1: 发票统计包含 {sub_key}")

# G2: 配置快照包含关键字段
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("config_snapshot.json") as f:
        cfg_snap = json.load(f)
assert_true("amount_tolerance" in cfg_snap, "G2: 配置包含 amount_tolerance")
assert_true("date_tolerance_days" in cfg_snap, "G2: 配置包含 date_tolerance_days")
assert_true("customer_name_aliases" in cfg_snap, "G2: 配置包含 customer_name_aliases")
assert_true("match_strategy" in cfg_snap, "G2: 配置包含 match_strategy")

# G3: 来源指纹包含文件哈希和记录数
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("source_fingerprints.json") as f:
        fps = json.load(f)
for fh, fp in fps.items():
    assert_true("file_hash" in fp, "G3: 指纹包含 file_hash")
    assert_true("file_label" in fp, "G3: 指纹包含 file_label")
    assert_true("invoice_count" in fp, "G3: 指纹包含 invoice_count")
    assert_true("transaction_count" in fp, "G3: 指纹包含 transaction_count")

# G4: 操作日志是有效 JSONL
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("operation_log.jsonl") as f:
        lines = [l.decode("utf-8").strip() for l in f if l.strip()]
for line in lines:
    entry = json.loads(line)
    assert_true("action" in entry, "G4: 每条日志包含 action")
    assert_true("timestamp" in entry, "G4: 每条日志包含 timestamp")
    assert_true("details" in entry, "G4: 每条日志包含 details")

# G5: 完整报告 JSON 结构正确
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("full_report/report.json") as f:
        rpt = json.load(f)
assert_true("summary" in rpt, "G5: 报告包含 summary")
assert_true("matches" in rpt, "G5: 报告包含 matches")
assert_true("unmatched_invoices" in rpt, "G5: 报告包含 unmatched_invoices")
assert_true("unmatched_transactions" in rpt, "G5: 报告包含 unmatched_transactions")
assert_true("reversed_matches" in rpt, "G5: 报告包含 reversed_matches")

# G6: 会话数据完整
with zipfile.ZipFile(audit_path, "r") as zf:
    with zf.open("session.json") as f:
        sess_data = json.load(f)
assert_true("session_id" in sess_data, "G6: 会话数据包含 session_id")
assert_true("invoices" in sess_data, "G6: 会话数据包含 invoices")
assert_true("transactions" in sess_data, "G6: 会话数据包含 transactions")
assert_true("matches" in sess_data, "G6: 会话数据包含 matches")
assert_true("history" in sess_data, "G6: 会话数据包含 history")
assert_true("imported_files" in sess_data, "G6: 会话数据包含 imported_files")


print()
print("=" * 70)
print("[H] 导入预检功能测试（不落库 / 持久化 / 跨重启）")
print("=" * 70)

# H0: 先清理旧的预检记录
run(["audit", "precheck-clear", "--yes"])

# H1: 导出后预检 - 无冲突场景
SESS_PRECHECK_NEW = "precheck_new_session"
res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--as", SESS_PRECHECK_NEW, "--reject"])
assert_true("导入预检报告" in out, "H1: precheck 命令输出预检报告标题")
assert_true("预检结论" in out and "可以导入" in out,
            "H1: 无冲突场景下预检结论为可以导入")
assert_true("会话冲突分析" in out, "H1: 包含会话冲突分析")
assert_true("版本与完整性检查" in out, "H1: 包含版本与完整性检查")
assert_true("配置检查" in out, "H1: 包含配置检查")
assert_true("重复导入来源检查" in out, "H1: 包含重复导入来源检查")
assert_true("数据概览" in out, "H1: 包含数据概览")
assert_true("目标会话已存在: 否" in out, "H1: 目标会话不存在")
assert_true("处理方式: 创建新会话" in out, "H1: 处理方式为创建新会话")
assert_true("版本兼容性" in out and "兼容" in out, "H1: 版本兼容性检查通过")
assert_true("完整性哈希" in out and "通过" in out, "H1: 完整性哈希校验通过")
assert_true(f"最终会话名: {SESS_PRECHECK_NEW}" in out, "H1: 最终会话名正确")

# H2: 验证预检不落库（会话未被创建）
assert_true(not sm_r.exists(SESS_PRECHECK_NEW),
            "H2: 预检不落库 - 目标会话未被创建")

# H3: 预检持久化验证 - precheck-list 和 precheck-show
res, out = run(["audit", "precheck-list"])
assert_true("预检记录存储目录" in out, "H3: precheck-list 输出存储目录")
assert_true("audit_A_full.irecaudit" in out, "H3: precheck-list 包含刚保存的预检记录")
assert_true(SESS_PRECHECK_NEW in out, "H3: precheck-list 显示目标会话名")

# 从 list 输出中提取预检 ID
precheck_id_h1 = None
for line in out.split("\n"):
    if "audit_A_full.irecaudit" in line and "precheck_" in line:
        parts = line.strip().split()
        if parts and parts[0].startswith("precheck_"):
            precheck_id_h1 = parts[0]
            break
assert_true(precheck_id_h1 is not None, f"H3: 从列表中提取到预检ID: {precheck_id_h1}")

# H4: precheck-show 查看详情
res, out = run(["audit", "precheck-show", precheck_id_h1])
assert_true("导入预检报告" in out, "H4: precheck-show 输出预检报告")
assert_true(precheck_id_h1 in out, "H4: precheck-show 显示正确的预检ID")
assert_true("audit_A_full.irecaudit" in out, "H4: precheck-show 显示审计包文件名")

# H5: 冲突分支 - reject 模式（会话已存在）
# 先创建一个冲突会话
SESS_PRECHECK_CONFLICT = "precheck_conflict"
run(["session", "create", SESS_PRECHECK_CONFLICT])

res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--as", SESS_PRECHECK_CONFLICT, "--reject"])
assert_true("无法导入" in out, "H5: reject 模式下冲突预检结论为无法导入")
assert_true("目标会话已存在: 是" in out, "H5: 检测到目标会话已存在")
assert_true("处理方式: 拒绝（reject）" in out, "H5: 处理方式显示为拒绝")
assert_true("错误" in out and "已存在" in out, "H5: 错误列表中包含会话已存在信息")

# H6: 冲突分支 - auto-rename 模式
res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--as", SESS_PRECHECK_CONFLICT, "--auto-rename"])
assert_true("可以导入" in out, "H6: auto-rename 模式下预检结论为可以导入")
assert_true("处理方式: 自动重命名（auto-rename）" in out, "H6: 处理方式为自动重命名")
assert_true("重命名为: precheck_conflict_restored" in out, "H6: 显示重命名后的名称")
assert_true("最终会话名: precheck_conflict_restored" in out, "H6: 最终会话名为重命名后")

# 验证仍未落库
assert_true(not sm_r.exists("precheck_conflict_restored"),
            "H6: auto-rename 预检也不落库")

# H7: 冲突分支 - overwrite 模式
res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--as", SESS_PRECHECK_CONFLICT, "--overwrite"])
assert_true("可以导入" in out, "H7: overwrite 模式下预检结论为可以导入")
assert_true("处理方式: 覆盖（overwrite）" in out, "H7: 处理方式为覆盖")
assert_true("现有会话数据将被替换" in out, "H7: 提示现有数据将被替换")

# H8: 配置漂移检测
run(["config", "set", "days_tol", "10"])
res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--as", SESS_PRECHECK_NEW + "_drift", "--reject"])
assert_true("配置漂移" in out, "H8: 预检检测到配置漂移")
assert_true("date_tolerance_days" in out, "H8: 列出 date_tolerance_days 漂移项")
assert_true("审计包" in out and "3" in out, "H8: 显示审计包中的值为 3")
assert_true("当前" in out and "10" in out, "H8: 显示当前值为 10")
assert_true("警告" in out, "H8: 配置漂移作为警告出现")
# 恢复配置
run(["config", "set", "days_tol", "3"])

# H9: 重复来源检查
res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--as", SESS_PRECHECK_NEW + "_dup", "--reject",
                "-c", "audit_restored"])
assert_true("重复导入来源检查" in out, "H9: 包含重复导入来源检查")
assert_true("重复来源" in out and "2 个" in out,
            "H9: 检测到 2 个重复导入来源")
assert_true("两边都导入过相同文件" in out, "H9: 提示两边都导入过相同文件")
assert_true("仅审计包" in out, "H9: 显示仅审计包的文件数")
assert_true("仅目标会话" in out, "H9: 显示仅目标会话的文件数")

# H10: 导入后复查 - 预检 ID 写入历史
res, out = run(["audit", "import", "audit_A_full.irecaudit",
                "--as", "audit_precheck_imported", "--switch"])
assert_true("预检ID" in out, "H10: 导入输出中显示预检ID")

# 从输出中提取预检ID
import_precheck_id = None
for line in out.split("\n"):
    if "预检ID" in line and "precheck_" in line:
        import_precheck_id = line.split(":", 1)[1].strip()
        break
assert_true(import_precheck_id is not None,
            f"H10: 从导入输出中提取到预检ID: {import_precheck_id}")

# 验证历史记录中包含预检信息
sess_imported = sm_r.load("audit_precheck_imported")
import_hist = [h for h in sess_imported.history if h.action == "audit_import"]
assert_true(len(import_hist) > 0, "H10: 导入会话历史中有 audit_import 记录")
imp_details = import_hist[-1].details
assert_true("precheck_id" in imp_details, "H10: 历史记录中包含 precheck_id")
assert_true(imp_details["precheck_id"] == import_precheck_id,
            "H10: 历史记录中的 precheck_id 与输出一致")
assert_true("precheck_summary" in imp_details, "H10: 历史记录中包含 precheck_summary")
assert_true("final_action" in imp_details, "H10: 历史记录中包含 final_action")

# H11: 跨重启一致性 - 预检记录持久化
# 先清理现有引用，模拟重启
del sm_r, cfg_r
import gc
gc.collect()

# 重新加载
cfg_restart = Config.load()
sm_restart = SessionManager(cfg_restart.session_dir)
precheck_store = PrecheckStore()

# 验证预检记录仍然存在
all_prechecks = precheck_store.list()
assert_true(len(all_prechecks) >= 5,
            f"H11: 跨重启后预检记录仍然存在（实际 {len(all_prechecks)} 条）")

# 验证之前保存的预检记录可以加载
pc = precheck_store.load(precheck_id_h1)
assert_true(pc is not None, "H11: 跨重启后可加载 H1 的预检记录")
assert_true(pc.audit_file == "audit_A_full.irecaudit",
            "H11: 加载的预检记录审计包名正确")
assert_true(pc.target_session_name == SESS_PRECHECK_NEW,
            "H11: 加载的预检记录目标会话名正确")
assert_true(pc.importable == True, "H11: 加载的预检记录可导入标记正确")

# H12: precheck-clear 清空功能
count_before = len(precheck_store.list())
assert_true(count_before >= 5, f"H12: 清空前有 {count_before} 条记录")

run(["audit", "precheck-clear", "--yes"])
count_after = len(precheck_store.list())
assert_true(count_after == 0, f"H12: 清空后有 {count_after} 条记录，应为 0")

# H13: 三选一参数互斥校验
res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--overwrite", "--reject", "--as", "should_fail"],
               expect_fail=True)
assert_true("三选一" in out or "不能同时" in out,
            "H13: 同时指定 --overwrite 和 --reject 被参数校验拦截")

# H14: 预检中包含可执行导入命令提示
res, out = run(["audit", "precheck", "audit_A_full.irecaudit",
                "--as", "cmd_hint_test", "--overwrite", "--apply-config"])
assert_true("执行导入:" in out, "H14: 预检结论中包含执行导入命令")
assert_true("cmd_hint_test" in out, "H14: 导入命令中包含目标会话名")
assert_true("--overwrite" in out, "H14: 导入命令中包含 --overwrite")
assert_true("--apply-config" in out, "H14: 导入命令中包含 --apply-config")

# H15: 导入后用 precheck-show 可以复查同一份结论
# 先做一次新导入
res, out = run(["audit", "import", "audit_A_full.irecaudit",
                "--as", "audit_review_test", "--auto-rename"])
review_precheck_id = None
for line in out.split("\n"):
    if "预检ID" in line and "precheck_" in line:
        review_precheck_id = line.split(":", 1)[1].strip()
        break
assert_true(review_precheck_id is not None, "H15: 获取导入时的预检ID")

# 用 precheck-show 复查
res, out = run(["audit", "precheck-show", review_precheck_id])
assert_true("导入预检报告" in out, "H15: precheck-show 可以复查导入时的预检结论")
assert_true(review_precheck_id in out, "H15: 复查的预检ID正确")
assert_true("audit_A_full.irecaudit" in out, "H15: 复查的审计包正确")
assert_true("可以导入" in out, "H15: 复查的结论为可导入")


# ============================================================
# 组 I：三处复查入口对齐（audit info / precheck-show / show history）
# 要求：任一入口都能看到同一份预检结论、最终处理方式、冲突分支、配置漂移摘要
# ============================================================
print("\n--- 组 I：三处复查入口对齐 ---")

# I0: 先做一次带完整场景的导入（overwrite + 配置漂移 + 完整细节）
# 先创建一个会话作为 overwrite 的目标（先删再建保证干净）
run(["session", "delete", "review_target_sess", "--yes"], expect_fail=True)
run(["session", "create", "review_target_sess"])
# 用 irec config set 改配置制造漂移（用 irec config show 先查原值）
res, out = run(["config", "show"])
orig_days_tol = 3
for line in out.split("\n"):
    if "日期容差" in line or "days_tol" in line or "date_tolerance_days" in line:
        m = re.search(r"(\d+)", line)
        if m:
            orig_days_tol = int(m.group(1))
# 备份当前配置文件（如果有）- 直接修改 .irec_config.json 增加一个额外漂移项
cfg_backup_path = None
if os.path.exists(".irec_config.json"):
    cfg_backup_path = ".irec_config_test_backup.json"
    shutil.copy(".irec_config.json", cfg_backup_path)
    # 直接修改 JSON 增加 small_amount_threshold（增加一个漂移项）
    with open(".irec_config.json", "r", encoding="utf-8") as f:
        _cfg = json.load(f)
    _cfg["small_amount_threshold"] = 88.88
    with open(".irec_config.json", "w", encoding="utf-8") as f:
        json.dump(_cfg, f, ensure_ascii=False, indent=2)
# 再用 config set 设置 days_tol = 999（制造 2 个漂移项）
run(["config", "set", "days_tol", "999"])
# 导入 overwrite + 配置漂移
res, out = run(["audit", "import",
                "audit_A_full.irecaudit", "--as", "review_target_sess",
                "--overwrite", "--apply-config"])
import_precheck_id = None
for line in out.split("\n"):
    if "预检ID" in line and "precheck_" in line:
        import_precheck_id = line.split(":", 1)[1].strip()
        break
assert_true(import_precheck_id is not None, "I0: 获取 overwrite+漂移场景的预检ID")
# 恢复配置 - 直接用备份（包含所有原值，包括 small_amount_threshold 等）
if cfg_backup_path:
    shutil.move(cfg_backup_path, ".irec_config.json")
else:
    # 还原 set 过的 days_tol
    run(["config", "set", "days_tol", str(orig_days_tol)])

# I1: audit info 能看到「导入复查」区块（find_audit_imports 反向查找）
res, out = run(["audit", "info", "audit_A_full.irecaudit"])
assert_true("导入复查" in out, "I1: audit info 显示导入复查区块")
assert_true("review_target_sess" in out, "I1: 复查中显示目标会话名")
assert_true(import_precheck_id in out or "precheck_" in out,
            "I1: 复查中显示预检ID")
assert_true("overwrite" in out or "覆盖" in out,
            "I1: 复查中显示处理方式为覆盖")
assert_true("配置漂移" in out, "I1: 复查中显示配置漂移摘要")

# I2: precheck-show 能看到「实际导入结果」区块（预检回写字段）
res, out = run(["audit", "precheck-show", import_precheck_id])
assert_true("实际导入结果" in out, "I2: precheck-show 显示实际导入结果区块")
assert_true("✓ 是" in out, "I2: 是否已导入标记为是")
assert_true("review_target_sess" in out, "I2: 最终导入会话正确")
assert_true("覆盖" in out or "overwrite" in out.lower(),
            "I2: 最终处理方式为覆盖")
# 复查建议入口
assert_true("audit info" in out, "I2: 底部建议包含 audit info 复查入口")
assert_true("show history" in out, "I2: 底部建议包含 show history 复查入口")

# I3: show history --action audit_import 专用格式化（不是 json 截断）
res, out = run(["show", "history",
                "--action", "audit_import", "--session", "review_target_sess"])
assert_true("预检结论" in out, "I3: history 显示预检结论标签框")
assert_true("最终处理方式" in out, "I3: history 显示最终处理方式")
assert_true("冲突分支结果" in out, "I3: history 显示冲突分支结果")
assert_true("配置漂移摘要" in out, "I3: history 显示配置漂移摘要")
assert_true("其他入口复查" in out, "I3: history 包含其他入口跳转建议")
# 不应被 JSON 截断（内容不应出现 json.dumps 的 ... 截断标记）
# 同时应该包含预检ID
assert_true(import_precheck_id in out, "I3: history 中能看到预检ID")

# I4: 三处展示的 final_action / precheck_id 一致
# audit info 中的预检ID
res1, out1 = run(["audit", "info", "audit_A_full.irecaudit"])
# precheck-show 中的处理方式
res2, out2 = run(["audit", "precheck-show", import_precheck_id])
# history 中的预检ID和处理方式
res3, out3 = run(["show", "history",
                  "--action", "audit_import", "--session", "review_target_sess"])
assert_true(import_precheck_id in out1, "I4: audit info 中预检ID一致")
assert_true(import_precheck_id in out3, "I4: history 中预检ID一致")
# 三处都提到覆盖/overwrite
assert_true("覆盖" in out1 or "overwrite" in out1.lower(),
            "I4: audit info 处理方式=overwrite")
assert_true("覆盖" in out2 or "overwrite" in out2.lower(),
            "I4: precheck-show 处理方式=overwrite")
assert_true("覆盖" in out3 or "overwrite" in out3.lower(),
            "I4: history 处理方式=overwrite")

# I5: precheck-list 「已导入」和「最终会话」列展示正确
res, out = run(["audit", "precheck-list"])
assert_true("已导入" in out, "I5: precheck-list 新增「已导入」列")
assert_true("最终会话" in out, "I5: precheck-list 新增「最终会话」列")
# 刚导入的 review_target_sess 应该在 list 中出现（或者 ✓ 标记）
assert_true("review_target_sess" in out or "✓" in out,
            "I5: precheck-list 显示已导入的会话")


# ============================================================
# 组 J：跨重启一致性（预检、会话历史、反向查找）
# 要求：重开 CLI / 重加载后字段不丢失，三处内容不打架
# ============================================================
print("\n--- 组 J：跨重启一致性 ---")

# J0: 保存当前关键信息，用于「重启」后对比
# 预检记录（从 .irec_prechecks 直接读）
import_precheck_path = None
for f in os.listdir(".irec_prechecks"):
    if f == f"{import_precheck_id}.json":
        import_precheck_path = os.path.join(".irec_prechecks", f)
        break
assert_true(import_precheck_path is not None, "J0: 找到预检持久化文件")
with open(import_precheck_path, "r", encoding="utf-8") as f:
    precheck_before = json.load(f)
# 会话历史 - 先找 review_target_sess 的文件
session_path = None
for f in os.listdir(".irec_sessions"):
    if f.startswith("review_target_sess") and f.endswith(".json"):
        session_path = os.path.join(".irec_sessions", f)
        break
assert_true(session_path is not None, "J0: 找到会话持久化文件")
with open(session_path, "r", encoding="utf-8") as f:
    session_before = json.load(f)
# 找 audit_import history entry
audit_import_history_before = None
for h in session_before["history"]:
    if h["action"] == "audit_import" and h["details"].get("precheck_id") == import_precheck_id:
        audit_import_history_before = h
        break
assert_true(audit_import_history_before is not None,
            "J0: 找到导入历史记录持久化")

# J1: 预检持久化文件中包含所有 7 个导入回写字段
for fk in ["import_executed", "imported_at", "imported_session_name",
           "imported_session_id", "actual_final_action",
           "actual_conflict_mode", "config_drift_summary"]:
    assert_true(fk in precheck_before, f"J1: 预检持久化包含 {fk}")
assert_true(precheck_before["import_executed"] is True,
            "J1: import_executed=True")
assert_true(precheck_before["imported_session_name"] == "review_target_sess",
            "J1: imported_session_name 正确")
assert_true(precheck_before["actual_final_action"] == "overwrite",
            "J1: actual_final_action=overwrite")

# J2: 会话历史持久化中包含新增字段（final_action_reason / conflict_branch_result
#     / config_drift_full / import_timestamp / precheck_summary）
hd = audit_import_history_before["details"]
for fk in ["final_action_reason", "conflict_branch_result",
           "config_drift_full", "import_timestamp", "precheck_summary"]:
    assert_true(fk in hd, f"J2: 历史详情包含 {fk}")
assert_true(hd["final_action"] == "overwrite", "J2: final_action=overwrite")
assert_true(hd["conflict_branch_result"]["session_existed_before"] is True,
            "J2: conflict_branch_result 包含 session_existed_before")
assert_true(hd["conflict_branch_result"]["overwritten"] is True,
            "J2: conflict_branch_result 包含 overwritten=True")

# J3: find_audit_imports 反向查找结果（通过 audit info）与直接文件一致
res, out = run(["audit", "info", "audit_A_full.irecaudit"])
# 找同一份预检ID 和 处理方式
assert_true(import_precheck_id in out,
            "J3: 反向查找（audit info）返回的预检ID一致")
assert_true("review_target_sess" in out,
            "J3: 反向查找返回的会话名一致")
assert_true("覆盖" in out or "overwrite" in out.lower(),
            "J3: 反向查找返回的处理方式一致")

# J4: 跨重启验证 - 手动重新创建 PrecheckStore/SessionManager 加载并校验
from invoice_reconcile.config import Config as _CfgJ
from invoice_reconcile.session import SessionManager as _SmJ
from invoice_reconcile.audit import PrecheckStore as _PsJ
cfg_j = _CfgJ.load()
sm_j = _SmJ(cfg_j.session_dir)
ps_j = _PsJ()  # PrecheckStore 不传参数，默认当前工作目录
# 重载预检
precheck_reloaded = ps_j.load(import_precheck_id)
assert_true(precheck_reloaded is not None, "J4: 重启后能加载预检")
assert_true(precheck_reloaded.import_executed is True,
            "J4: 重启后 import_executed 不变")
assert_true(precheck_reloaded.actual_final_action == "overwrite",
            "J4: 重启后 actual_final_action 不变")
assert_true(precheck_reloaded.imported_session_name == "review_target_sess",
            "J4: 重启后 imported_session_name 不变")
# 重载会话
sess_j = sm_j.load("review_target_sess")
audit_import_j = None
for h in sess_j.history:
    if h.action == "audit_import" and h.details.get("precheck_id") == import_precheck_id:
        audit_import_j = h
        break
assert_true(audit_import_j is not None, "J4: 重启后会话历史存在")
assert_true(audit_import_j.details["final_action"] == "overwrite",
            "J4: 重启后 final_action 不变")
assert_true("conflict_branch_result" in audit_import_j.details,
            "J4: 重启后 conflict_branch_result 字段存在")
assert_true("config_drift_full" in audit_import_j.details,
            "J4: 重启后 config_drift_full 字段存在")


# ============================================================
# 组 K：导出后再导入链路
# 要求：再次导出的审计包中包含原导入历史，再导入后字段不丢失
# ============================================================
print("\n--- 组 K：导出后再导入链路 ---")

# K1: 将 review_target_sess（已导入 A 的）再次导出为 audit_B
res, out = run(["audit", "export",
                "--session", "review_target_sess",
                "-o", "audit_B_reexport.irecaudit"])
assert_true("完成" in out or "成功" in out or "Saved" in out or "已导出" in out or "[OK]" in out or res.returncode == 0,
            "K1: audit_B 重新导出成功")
# 导出的文件在 .irec_audits/ 目录
audit_b_path = os.path.join(".irec_audits", "audit_B_reexport.irecaudit")
assert_true(os.path.exists(audit_b_path),
            "K1: audit_B 文件存在（在 .irec_audits/ 目录）")

# K2: 检查 audit_B 中的 session.json 包含原始 audit_import 历史
with zipfile.ZipFile(audit_b_path, "r") as zf:
    with zf.open("session.json") as sf:
        sess_b = json.load(sf)
audit_import_in_b = [h for h in sess_b["history"]
                     if h["action"] == "audit_import"]
assert_true(len(audit_import_in_b) >= 1,
            "K2: audit_B 的 session.json 中含 audit_import 历史")
hd_k2 = audit_import_in_b[0]["details"]
# 检查字段完整性
for fk in ["final_action", "final_action_reason", "conflict_branch_result",
           "config_drift_full", "precheck_id", "precheck_summary",
           "import_timestamp"]:
    assert_true(fk in hd_k2, f"K2: 再导出的历史中包含 {fk}")
assert_true(hd_k2["final_action"] == "overwrite",
            "K2: 再导出的 final_action=overwrite")
assert_true(hd_k2["precheck_id"] == import_precheck_id,
            "K2: 再导出的 precheck_id 不变")

# K3: 再导入 audit_B 得到新会话，检查能正确解析
res, out = run(["audit", "import", audit_b_path,
                "--as", "reimport_sess_B"])
assert_true(res.returncode == 0 or "[OK]" in out or "已导入" in out or "会话" in out,
            "K3: audit_B 再导入成功")
# 新会话中应该有两次 audit_import 历史（一次来自 audit_B，一次是当前导入）
res, out = run(["show", "history",
                "--action", "audit_import", "--session", "reimport_sess_B"])
count_ai = out.count("audit_import")
assert_true(count_ai >= 2, "K3: 再导入后历史含至少 2 次 audit_import")

# K4: 再次导出的审计包能正常预检和 info 查看
res, out = run(["audit", "info", audit_b_path])
assert_true(res.returncode == 0 or "审计包信息" in out or "info" in out.lower(),
            "K4: audit_B info 正常（exit=0 或信息标记）")
res, out = run(["audit", "precheck", audit_b_path,
                "--as", "precheck_B_sess"])
assert_true(res.returncode == 0 or "导入预检报告" in out or "precheck" in out.lower(),
            "K4: audit_B precheck 正常")


# ============================================================
# 组 L：冲突分支 + 配置漂移完整覆盖
# 要求：reject/overwrite/auto-rename 三分支 + 配置漂移都能正确记录
# ============================================================
print("\n--- 组 L：冲突分支 + 配置漂移完整覆盖 ---")

# L1: auto-rename 分支 + 配置漂移
# 先创建会话再制造漂移（先删再建保证干净）
run(["session", "delete", "rename_sess_base", "--yes"], expect_fail=True)
run(["session", "create", "rename_sess_base"])
# 备份配置 + 直接修改 JSON 增加 small_amount_threshold + config set days_tol=555
cfg_backup_l1 = None
if os.path.exists(".irec_config.json"):
    cfg_backup_l1 = ".irec_config_l1_backup.json"
    shutil.copy(".irec_config.json", cfg_backup_l1)
    with open(".irec_config.json", "r", encoding="utf-8") as f:
        _cfg2 = json.load(f)
    _cfg2["small_amount_threshold"] = 55.55
    with open(".irec_config.json", "w", encoding="utf-8") as f:
        json.dump(_cfg2, f, ensure_ascii=False, indent=2)
run(["config", "set", "days_tol", "555"])
res, out = run(["audit", "import",
                "audit_A_full.irecaudit", "--as", "rename_sess_base",
                "--auto-rename", "--apply-config"])
assert_true(res.returncode == 0 or "[OK]" in out or "已导入" in out or "会话" in out or "另存" in out,
            "L1: auto-rename 导入成功")
assert_true("另存" in out or "新副本" in out or "rename" in out.lower(),
            "L1: 输出中提到 auto-rename")
# 从输出提取实际会话名
actual_rename_sess = None
rename_precheck_id = None
for line in out.split("\n"):
    # 两种格式都支持："审计包已导入为会话 'XXX'" 或 "导入会话 ... 完成"
    if ("导入" in line and "会话" in line) or ("导入为" in line):
        match = re.search(r"'([^']+)'", line)
        if match and "预检ID" not in line and "原会话" not in line:
            actual_rename_sess = match.group(1)
    if "自动重命名为" in line:
        match = re.search(r"'([^']+)'", line)
        if match:
            actual_rename_sess = match.group(1)
    if "预检ID" in line and "precheck_" in line:
        rename_precheck_id = line.split(":", 1)[1].strip()
assert_true(actual_rename_sess is not None, "L1: 获取重命名后的会话名")
assert_true(actual_rename_sess != "rename_sess_base",
            "L1: 实际会话名已被重命名（非原名）")
assert_true(rename_precheck_id is not None, "L1: 获取重命名预检ID")
# precheck-show 检查
res, out = run(["audit", "precheck-show", rename_precheck_id])
assert_true("自动重命名" in out or "auto-rename" in out.lower(),
            "L1: precheck-show 显示处理方式=auto-rename")
assert_true(actual_rename_sess in out,
            "L1: precheck-show 显示实际会话名")
# history 检查
res, out = run(["show", "history",
                "--action", "audit_import", "--session", actual_rename_sess])
assert_true("自动重命名" in out or "另存新副本" in out,
            "L1: history 显示冲突分支=auto-rename")
assert_true(rename_precheck_id in out,
            "L1: history 显示预检ID")
# 恢复配置
if cfg_backup_l1 and os.path.exists(cfg_backup_l1):
    shutil.move(cfg_backup_l1, ".irec_config.json")

# L2: reject 分支（测试预检记录，不执行导入）
res, out = run(["audit", "import",
                "audit_A_full.irecaudit", "--as", "rename_sess_base",
                "--reject"], expect_fail=True)
assert_true("拒绝" in out or "reject" in out.lower() or "已存在" in out,
            "L2: reject 分支被正确拒绝")
# 预检记录中 actual_final_action 应为 reject
reject_precheck_id = None
for line in out.split("\n"):
    if "预检ID" in line and "precheck_" in line:
        reject_precheck_id = line.split(":", 1)[1].strip()
        break
if reject_precheck_id:
    reject_pc_json = os.path.join(".irec_prechecks", f"{reject_precheck_id}.json")
    assert_true(os.path.exists(reject_pc_json), "L2: reject 预检记录 JSON 存在")
    with open(reject_pc_json, "r", encoding="utf-8") as f:
        rpc = json.load(f)
    assert_true(rpc.get("actual_final_action") == "reject",
                "L2: reject 预检 actual_final_action=reject")

# L3: final_action_reason 说明正确（三种场景），直接读 JSON 文件避免控制台乱码
def _load_precheck_json(pid):
    path = os.path.join(".irec_prechecks", f"{pid}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

# overwrite 场景
pc_overwrite = _load_precheck_json(import_precheck_id)
assert_true(pc_overwrite is not None, "L3: overwrite 预检 JSON 可加载")
assert_true(pc_overwrite.get("actual_final_action") == "overwrite"
            or "覆盖" in str(pc_overwrite.get("actual_final_action", "")),
            "L3: overwrite actual_final_action=overwrite")
assert_true(pc_overwrite.get("import_executed") == True,
            "L3: overwrite 场景 import_executed=True")
assert_true(pc_overwrite.get("actual_conflict_mode") == "overwrite",
            "L3: overwrite actual_conflict_mode=overwrite")
# 从会话历史取 final_action_reason
cfg_l3 = Config.load()
sm_l3 = SessionManager(cfg_l3.session_dir)
sess_overwrite = sm_l3.load(pc_overwrite.get("imported_session_name", ""))
reason_found = False
if sess_overwrite:
    for h in sess_overwrite.history:
        if h.action == "audit_import" and h.details.get("precheck_id") == import_precheck_id:
            if "final_action_reason" in h.details and h.details["final_action_reason"]:
                reason_found = True
                break
assert_true(reason_found, "L3: overwrite 场景会话历史含 final_action_reason")

# auto-rename 场景
pc_rename = _load_precheck_json(rename_precheck_id)
assert_true(pc_rename is not None, "L3: auto-rename 预检 JSON 可加载")
assert_true(pc_rename.get("actual_final_action") == "rename"
            or "重命名" in str(pc_rename.get("actual_final_action", ""))
            or "auto-rename" in str(pc_rename.get("actual_final_action", "")).lower(),
            "L3: auto-rename actual_final_action=rename")
assert_true(pc_rename.get("import_executed") == True,
            "L3: auto-rename 场景 import_executed=True")
# 从会话历史取 final_action_reason
reason_found_r = False
if actual_rename_sess:
    sess_rename = sm_l3.load(actual_rename_sess)
    if sess_rename:
        for h in sess_rename.history:
            if h.action == "audit_import" and h.details.get("precheck_id") == rename_precheck_id:
                if "final_action_reason" in h.details and h.details["final_action_reason"]:
                    reason_found_r = True
                    break
assert_true(reason_found_r, "L3: auto-rename 场景会话历史含 final_action_reason")


# ============================================================
# 清理新增文件
# ============================================================
if 'audit_b_path' in dir() and os.path.exists(audit_b_path):
    os.remove(audit_b_path)


# ============================================================
# 最终汇总
# ============================================================
if _cli_failed:
    print("\n" + "=" * 70)
    print("[FAILED] 存在 CLI 子进程非零退出码，参见上方 [exit=...] 标记")
    print("=" * 70)
    sys.exit(1)

print("\n" + "=" * 70)
print("[ALL PASSED] 审计包功能回归测试全部通过：")
print("  [A] 导出→导入往返：发票/流水/匹配/撤销/挂起/备注/历史/指纹 全部一致")
print("  [B] 跨重启恢复：会话ID、核心数字、未匹配列表、备注、挂起、撤销 全部不变")
print("  [C] 冲突提示：--reject 拒绝、--auto-rename 另存新副本、check-sources 来源检测")
print("  [D] 配置漂移/配置缺失/版本不兼容/重复来源 全部正确检测")
print("  [E] 日志回放：只追加历史，不还原业务数据，动作类型齐全")
print("  [F] list/info/delete 命令 + 参数互斥校验 全部正常")
print("  [G] 归档内容：摘要、配置、明细、撤销挂起、指纹、日志、报告、会话数据 完整")
print("  [H] 导入预检：不落库检测/三分支冲突/配置漂移/重复来源/持久化/跨重启/导入后复查")
print("  [I] 三处复查入口对齐：audit info / precheck-show / show history 信息一致")
print("  [J] 跨重启一致性：预检/会话/反向查找 字段不丢失")
print("  [K] 导出后再导入链路：历史字段完整、多次导入追溯清晰")
print("  [L] 冲突分支+配置漂移：overwrite/auto-rename/reject 三分支全覆盖")
print("=" * 70)
sys.exit(0)
