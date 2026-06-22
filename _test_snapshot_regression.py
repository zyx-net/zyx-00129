# -*- coding: utf-8 -*-
"""
核对快照（Snapshot）功能回归测试
覆盖 4 大类场景：
  A. 导出 → 导入往返（数据一致性校验）
  B. 跨重启恢复（重新加载会话不变）
  C. 冲突提示（重名会话分支）
  D. 覆盖与拒绝覆盖 + 自动重命名 + 配置缺失提示
"""
import sys
import os
import json
import subprocess
import shutil
import tempfile
import zipfile

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
    ".irec_config.json",
    ".irec_state.json",
    "snap_irec_report",
    "snap_irec_report.json",
    "snap_restored_report",
    "snap_restored_report.json",
]
for p in CLEAN_PATHS:
    if os.path.isfile(p):
        os.remove(p)
    elif os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)

SESS_SRC = "snap_source"
SESS_RESTORED = "snap_restored"
SESS_CONFLICT = "snap_conflict"

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
     "-n", "差额300元为手续费，财务确认入账【快照测试】"])
run(["manual", "suspend-inv", "INV-2026-005", "-r", "合同金额争议，等待商务确认【快照】"])

# 找到刚刚的人工匹配 match_id 并撤销，再重新匹配，以便测试撤销记录
from invoice_reconcile.config import Config
from invoice_reconcile.session import SessionManager
cfg = Config.load()
sm = SessionManager(cfg.session_dir)
sess = sm.load(SESS_SRC)

# 找到小米那组匹配记录（发票 INV-2026-008）然后撤销
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
run(["manual", "reverse", xiaomi_match_id, "-r", "演示撤销【快照测试】"])
# 重新匹配回来
run(["manual", "match", "-i", "INV-2026-008", "-t", "TXN20260620009",
     "-n", "重新匹配：差额300手续费【快照】"])

# 再次加载获取最新状态
del cfg, sm, sess
cfg = Config.load()
sm = SessionManager(cfg.session_dir)
sess = sm.load(SESS_SRC)
summary_src = sm.status_summary(sess)

# 记录关键状态信息，用于后续导入后比对
inv_by_no_src = {i.invoice_no: i for i in sess.invoices.values()}
txn_by_no_src = {t.txn_id: t for t in sess.transactions.values()}
matches_src = {m.id: m for m in sess.matches.values()}
active_matches_src = [m for m in matches_src.values() if not m.reversed]
reversed_matches_src = [m for m in matches_src.values() if m.reversed]
history_count_src = len(sess.history)

# 导出报告，记录报表汇总数字
run(["report", "-f", "both", "-o", "snap_irec_report"])
with open("snap_irec_report.json", "r", encoding="utf-8") as f:
    rpt_src = json.load(f)

# 注意：export 会在源会话中增加 snapshot_export 历史记录
# 但我们还没 export，先记当前历史数，之后断言使用 export 后再 compare
history_count_before_export = history_count_src

print()
print("=" * 70)
print("[A] 导出 → 导入往返测试")
print("=" * 70)

# A1: 导出快照
res, out = run(["snapshot", "export", "-n", "A类回归测试-完整会话", "-o", "snap_A_full.irecsnap"])
assert_true("快照已导出" in out, "A1: export 命令输出成功标记")

snap_dir = os.path.join(os.getcwd(), ".irec_snapshots")
snap_path = os.path.join(snap_dir, "snap_A_full.irecsnap")
assert_true(os.path.exists(snap_path), "A1: 快照文件生成在 .irec_snapshots/ 下")

# A2: 检查归档结构（zip + snapshot.json）
with zipfile.ZipFile(snap_path, "r") as zf:
    names = zf.namelist()
    assert_true("snapshot.json" in names, "A2: 快照归档内包含 snapshot.json")
    with zf.open("snapshot.json") as f:
        snap_json = json.load(f)
    assert_true("metadata" in snap_json, "A2: 包含 metadata")
    assert_true("session" in snap_json, "A2: 包含 session")
    assert_true("config" in snap_json, "A2: 包含 config")
    assert_true("content_hash" in snap_json, "A2: 包含 content_hash")
    assert_true(snap_json["metadata"]["snapshot_version"], "A2: metadata 中含有版本号")
    assert_true(snap_json["metadata"]["original_session_name"] == SESS_SRC, "A2: metadata 中含有原会话名")

# A3: snapshot list / info 正常显示
res, out = run(["snapshot", "list"])
assert_true("snap_A_full.irecsnap" in out, "A3: snapshot list 显示导出的文件")

res, out = run(["snapshot", "info", "snap_A_full.irecsnap"])
assert_true("版本兼容性" in out and "✓ 兼容" in out, "A3: snapshot info 版本兼容标记 ✓")
assert_true("完整性哈希" in out and "✓ 通过" in out, "A3: snapshot info 完整性校验 ✓")
assert_true("配置完整性" in out and "✓ 完整" in out, "A3: snapshot info 配置完整 ✓")

# A4: 导入快照为新会话（不重名，无冲突）
run(["snapshot", "import", "snap_A_full.irecsnap", "--as", SESS_RESTORED, "--switch"])

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
assert_true(summary2["history_count"] >= history_count_before_export + 1,
            f"A5: 导入会话历史数 >= 原历史数+1 (export动作+import动作): "
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
run(["report", "-f", "both", "-o", "snap_restored_report"])
with open("snap_restored_report.json", "r", encoding="utf-8") as f:
    rpt_dst = json.load(f)

sum_src = rpt_src["summary"]
sum_dst = rpt_dst["summary"]
for key1 in ["invoices", "transactions", "matches"]:
    for key2 in sum_src[key1].keys():
        assert_true(sum_src[key1][key2] == sum_dst[key1][key2],
                    f"A10: 报表汇总一致 {key1}.{key2}: src={sum_src[key1][key2]} dst={sum_dst[key1][key2]}")

# A11: 历史日志中包含 snapshot_export 和 snapshot_import
# 重新加载源会话（因为 CLI 子进程中 export 后已经 save 了）
sm_src = SessionManager(cfg.session_dir)
sess_src_reloaded = sm_src.load(SESS_SRC)
history_actions_src = [h.action for h in sess_src_reloaded.history]
history_actions_dst = [h.action for h in sess2.history]
assert_true("snapshot_export" in history_actions_src, "A11: 原会话历史中有 snapshot_export")
assert_true("snapshot_import" in history_actions_dst, "A11: 恢复会话历史中有 snapshot_import")

print()
print("=" * 70)
print("[B] 跨重启恢复测试（重新加载会话不变）")
print("=" * 70)

# 模拟重启：销毁所有 Python 对象，重新加载所有配置与会话
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
            f"B2: 重启后未匹配流水列表不变")

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

# B4: 历史记录包含 snapshot_import（重启后仍存在）
hist_r = [h.action for h in sess_r.history]
assert_true("snapshot_import" in hist_r, "B4: 重启后历史仍包含 snapshot_import")

print()
print("=" * 70)
print("[C] 冲突提示测试（重名会话分支）")
print("=" * 70)

# C1: 创建一个冲突会话（与源会话同名 snap_source），导入时使用默认 ask 模式的 reject 分支
run(["session", "create", SESS_CONFLICT])

# 使用 --reject 应失败（被拒绝）
res, out = run(
    ["snapshot", "import", "snap_A_full.irecsnap", "--as", SESS_CONFLICT, "--reject"],
    expect_fail=True,
)
assert_true("已存在" in out or "reject" in out.lower(),
            "C1: --reject 模式下重名导入被拒绝并给出明确提示")
# 会话仍存在且是空的
sess_c1 = sm_r.load(SESS_CONFLICT)
assert_true(len(sess_c1.invoices) == 0, "C1: --reject 模式下原会话数据未被改动（仍为空）")

# C2: 使用 --auto-rename 自动重命名
res, out = run(["snapshot", "import", "snap_A_full.irecsnap",
                "--as", SESS_CONFLICT, "--auto-rename"])
assert_true("因重名已自动重命名" in out, "C2: --auto-rename 模式下显示自动重命名提示")

# 查找重命名后的会话（应为 snap_conflict_restored 或 snap_conflict_restored1）
all_sessions = sm_r.list_sessions()
renamed_names = [s["name"] for s in all_sessions
                 if s["name"].startswith(SESS_CONFLICT) and s["name"] != SESS_CONFLICT]
assert_true(len(renamed_names) >= 1, f"C2: 存在被重命名的新会话 {renamed_names}")
# 加载其中一个，确认里面有数据
sess_c2 = sm_r.load(renamed_names[0])
assert_true(len(sess_c2.invoices) > 0, f"C2: 重命名后的会话中含有发票数据（{len(sess_c2.invoices)}条）")

# C3: check-conflicts 命令
res, out = run(["snapshot", "check-conflicts", "snap_A_full.irecsnap",
                "-s", SESS_RESTORED])
# 同一个源导入的两个会话应该有重复导入来源（同两个CSV）
assert_true("重复导入来源" in out or "未发现重复" in out,
            "C3: check-conflicts 命令正常输出（发现重复或未发现都算正常）")

print()
print("=" * 70)
print("[D] 覆盖与拒绝覆盖 + 配置缺失提示")
print("=" * 70)

# D1: --overwrite 覆盖已有会话
# 检查覆盖前会话是空（snap_conflict）还是非空（如果 snap_conflict 仍在且空）
sess_before = sm_r.load(SESS_CONFLICT)
inv_before = len(sess_before.invoices)
run(["snapshot", "import", "snap_A_full.irecsnap", "--as", SESS_CONFLICT, "--overwrite",
     "--apply-config"])
sess_d1 = sm_r.load(SESS_CONFLICT)
assert_true(len(sess_d1.invoices) > inv_before,
            f"D1: --overwrite 模式下会话数据被替换（之前{inv_before}张，之后{len(sess_d1.invoices)}张）")
history_d1 = [h.action for h in sess_d1.history]
assert_true("snapshot_import" in history_d1,
            "D1: 覆盖后新会话历史中仍有 snapshot_import 记录")

# D2: 配置被 --apply-config 恢复（快照里的配置覆盖了当前配置）
cfg_after = Config.load()
assert_true("华为" in cfg_after.customer_name_aliases.get("华为技术有限公司", []),
            "D2: --apply-config 后别名配置仍存在（快照中的配置已应用）")

# D3: 制造一个"配置缺失"的快照来测试提示
#   直接写一个 snapshot.json 把 config 字段删掉再 zip，并重新计算 hash
snap_bad_cfg_path = os.path.join(snap_dir, "snap_D_badcfg.irecsnap")
with zipfile.ZipFile(snap_path, "r") as zf:
    with zf.open("snapshot.json") as f:
        d = json.load(f)
# 删 config 字段
d.pop("config", None)
# 重新计算 hash（没有 config）
from invoice_reconcile.snapshot import _compute_content_hash
d["content_hash"] = _compute_content_hash(d["session"], {}, {
    k: v for k, v in d["metadata"].items() if k != "created_at" and k != "snapshot_id"
})
with tempfile.TemporaryDirectory() as tmpdir:
    jp = os.path.join(tmpdir, "snapshot.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    with zipfile.ZipFile(snap_bad_cfg_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(jp, arcname="snapshot.json")

res, out = run(["snapshot", "import", "snap_D_badcfg.irecsnap", "--as", "snap_D_dest",
                "--auto-rename"])
assert_true("缺少配置项" in out or "缺少 config" in out or "缺失" in out,
            "D3: 导入缺少配置的快照时会给出缺失配置提示")

# D4: 制造"版本不兼容"快照，应被拒绝导入
snap_bad_ver_path = os.path.join(snap_dir, "snap_D_badver.irecsnap")
with zipfile.ZipFile(snap_path, "r") as zf:
    with zf.open("snapshot.json") as f:
        d = json.load(f)
d["metadata"]["snapshot_version"] = "99.0"  # 主版本号高到不兼容
# 重新计算 hash（版本变了会影响 hash，需要重新算以免 hash 校验触发确认）
d["content_hash"] = _compute_content_hash(d["session"], d.get("config", {}), {
    k: v for k, v in d["metadata"].items() if k != "created_at" and k != "snapshot_id"
})
with tempfile.TemporaryDirectory() as tmpdir:
    jp = os.path.join(tmpdir, "snapshot.json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    with zipfile.ZipFile(snap_bad_ver_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(jp, arcname="snapshot.json")

res, out = run(["snapshot", "import", "snap_D_badver.irecsnap", "--as", "snap_D_badver_dest"],
               expect_fail=True)
assert_true("版本不兼容" in out or "主版本" in out,
            "D4: 版本不兼容快照导入被明确拒绝（含原因说明）")

# D5: snapshot delete 命令正常
res, out = run(["snapshot", "delete", "snap_D_badver.irecsnap", "--yes"])
assert_true("快照已删除" in out, "D5: snapshot delete 成功输出标记")
assert_true(not os.path.exists(os.path.join(snap_dir, "snap_D_badver.irecsnap")),
            "D5: 快照文件被实际删除")

# D6: 三选一的冲突模式参数互斥校验
res, out = run(["snapshot", "import", "snap_A_full.irecsnap", "--overwrite", "--reject",
                "--as", "snap_should_fail"], expect_fail=True)
assert_true("三选一" in out or "不能同时" in out,
            "D6: 同时指定 --overwrite 和 --reject 被参数校验拦截")


# ============================================================
# 最终汇总
# ============================================================
if _cli_failed:
    print("\n" + "=" * 70)
    print("[FAILED] 存在 CLI 子进程非零退出码，参见上方 [exit=...] 标记")
    print("=" * 70)
    sys.exit(1)

print("\n" + "=" * 70)
print("[ALL PASSED] 快照功能回归测试全部通过：")
print("  [A] 导出→导入往返：发票/流水/匹配/撤销/挂起/备注/历史全部一致")
print("  [B] 跨重启恢复：会话ID、核心数字、未匹配列表、备注、挂起、撤销全部不变")
print("  [C] 冲突提示：--reject 拒绝、--auto-rename 重命名、check-conflicts 来源检测")
print("  [D] 覆盖/拒绝/配置缺失/版本不兼容/删除/参数互斥 全部分支正确")
print("=" * 70)
sys.exit(0)
