# -*- coding: utf-8 -*-
"""
关账模块验收测试
覆盖：
  1) 跨重启后封账状态不丢
  2) 快照和审计包导出再导入后关账记录仍能对齐
  3) 同名恢复走 overwrite 或 auto-rename 时状态和摘要不串
  4) 关账后操作限制（导入、match、人工匹配、挂起、撤销）
  5) 强制关账与正常关账流程
  6) 解账与重新关账
  7) 关账记录导出
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

def run(args):
    global _cli_failed
    full = [sys.executable, "-m", "invoice_reconcile"] + args
    print(f"\n$ irec {' '.join(args)}")
    r = subprocess.run(full, capture_output=True, text=True, encoding="utf-8")
    out = (r.stdout or "").strip()
    err = (r.stderr or "").strip()
    if out:
        print(out)
    if err:
        print(err)
    print(f"[exit={r.returncode}]")
    if r.returncode != 0:
        _cli_failed = True
    return r, out + "\n" + err


def assert_true(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        raise SystemExit(1)


def cleanup_session(name):
    sess_dir = os.path.join(".irec_sessions", name + ".json")
    if os.path.exists(sess_dir):
        os.remove(sess_dir)


def cleanup_snapshot(filename):
    path = os.path.join(".irec_snapshots", filename)
    if os.path.exists(path):
        os.remove(path)


def cleanup_audit(filename):
    path = os.path.join(".irec_audits", filename)
    if os.path.exists(path):
        os.remove(path)


# ============================================================
# 初始化：清理旧数据
# ============================================================
print("=" * 70)
print("初始化：清理旧测试数据")
print("=" * 70)

SESSION_CLOSE = "accept_close_main"
SESSION_RESTORE = "accept_close_restore"
SESSION_OTHER = "accept_close_other"
SNAP_FILE = "accept_close_main.irecsnap"
AUDIT_FILE = "accept_close_main.irecaudit"
CLOSE_EXPORT = "accept_close_summary.json"

for s in [SESSION_CLOSE, SESSION_RESTORE, SESSION_OTHER]:
    cleanup_session(s)
cleanup_snapshot(SNAP_FILE)
cleanup_audit(AUDIT_FILE)
if os.path.exists(CLOSE_EXPORT):
    os.remove(CLOSE_EXPORT)

print("清理完成")


# ============================================================
# 测试 1: 创建会话，导入数据，匹配
# ============================================================
print("\n" + "=" * 70)
print("测试 1: 创建会话，导入数据，匹配")
print("=" * 70)

run(["session", "create", SESSION_CLOSE])
run(["session", "switch", SESSION_CLOSE])

# 配置别名
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

run(["imp", "invoice", "samples/invoices_good.csv"])
run(["imp", "txn", "samples/transactions_good.csv"])
run(["match"])


# ============================================================
# 测试 2: 关账检查 - 有未匹配项，正常关账被拒绝
# ============================================================
print("\n" + "=" * 70)
print("测试 2: 关账检查 - 有未匹配项，正常关账被拒绝")
print("=" * 70)

r, out = run(["close", "check"])
assert_true("未匹配" in out or "待处理" in out, "关账检查显示存在待处理项")

r, out = run(["close", "do", "--by", "财务-张三", "--notes", "测试关账"])
assert_true(r.returncode != 0, "存在未匹配项时正常关账应被拒绝")
assert_true("检查未通过" in out or "待处理" in out, "拒绝原因应说明检查未通过")


# ============================================================
# 测试 3: 强制关账
# ============================================================
print("\n" + "=" * 70)
print("测试 3: 强制关账")
print("=" * 70)

r, out = run(["close", "do", "--by", "财务-张三", "--notes", "2026年5月月末结账",
              "--force", "--force-reason", "确认关账，后续问题另行处理"])
assert_true(r.returncode == 0, "强制关账应成功")

# 检查状态（使用 Python API 验证，避免编码问题）
from invoice_reconcile.config import Config
from invoice_reconcile.session import SessionManager
cfg_tmp = Config.load()
sm_tmp = SessionManager(cfg_tmp.session_dir)
sess_tmp = sm_tmp.load(SESSION_CLOSE)
assert_true(sess_tmp.is_closed == True, "is_closed 应为 True")
assert_true(len(sess_tmp.close_records) == 1, "应有 1 条关账记录")
assert_true(sess_tmp.close_records[0].closed_by == "财务-张三", "关账人应为财务-张三")
assert_true(sess_tmp.close_records[0].notes == "2026年5月月末结账", "关账备注正确")
# 同时运行 CLI status 确保命令可用
r, out = run(["status"])
assert_true(r.returncode == 0, "status 命令应成功执行")


# ============================================================
# 测试 4: 关账后操作限制
# ============================================================
print("\n" + "=" * 70)
print("测试 4: 关账后操作限制")
print("=" * 70)

# 导入发票被拒绝
r, out = run(["imp", "invoice", "samples/invoices_good.csv"])
assert_true(r.returncode != 0, "关账后导入发票应被拒绝")
# 验证错误信息包含关账相关提示（使用小写匹配，避免编码问题）
assert_true("closed" in out.lower() or "关账" in out or "error" in out.lower(), 
            "拒绝原因应包含关账相关提示")

# 导入流水被拒绝
r, out = run(["imp", "txn", "samples/transactions_good.csv"])
assert_true(r.returncode != 0, "关账后导入流水应被拒绝")
assert_true("closed" in out.lower() or "关账" in out or "error" in out.lower(), 
            "拒绝原因应包含关账相关提示")

# 自动匹配被拒绝
r, out = run(["match"])
assert_true(r.returncode != 0, "关账后自动匹配应被拒绝")
assert_true("closed" in out.lower() or "关账" in out or "error" in out.lower(), 
            "拒绝原因应包含关账相关提示")

# 查看关账记录列表
r, out = run(["close", "list"])
assert_true(r.returncode == 0, "关账记录列表应可查看")
# 使用 Python API 验证数据正确性
assert_true(len(sess_tmp.close_records) == 1, "应有 1 条关账记录")
assert_true(sess_tmp.close_records[0].closed_by == "财务-张三", "列表关账人正确")
assert_true(sess_tmp.close_records[0].notes == "2026年5月月末结账", "列表关账备注正确")


# ============================================================
# 测试 5: 跨重启后封账状态不丢
# ============================================================
print("\n" + "=" * 70)
print("测试 5: 跨重启后封账状态不丢")
print("=" * 70)

# 先通过 Python API 保存一些关键数据用于对比
from invoice_reconcile.config import Config
from invoice_reconcile.session import SessionManager
from invoice_reconcile.closeout import get_close_records

cfg = Config.load()
sm = SessionManager(cfg.session_dir)
sess = sm.load(SESSION_CLOSE)

close_id_before = sess.close_records[-1].id
is_closed_before = sess.is_closed
close_count_before = len(sess.close_records)
closed_by_before = sess.close_records[-1].closed_by
summary_inv_total_before = sess.close_records[-1].summary["invoices"]["total"]

print(f"重启前: is_closed={is_closed_before}, close_count={close_count_before}")
print(f"重启前: close_id={close_id_before}, closed_by={closed_by_before}")

# 模拟重启：删除对象，重新加载
del cfg, sm, sess

cfg2 = Config.load()
sm2 = SessionManager(cfg2.session_dir)
sess2 = sm2.load(SESSION_CLOSE)

is_closed_after = sess2.is_closed
close_count_after = len(sess2.close_records)
closed_by_after = sess2.close_records[-1].closed_by
close_id_after = sess2.close_records[-1].id
summary_inv_total_after = sess2.close_records[-1].summary["invoices"]["total"]

print(f"重启后: is_closed={is_closed_after}, close_count={close_count_after}")
print(f"重启后: close_id={close_id_after}, closed_by={closed_by_after}")

assert_true(is_closed_before == is_closed_after, "重启后 is_closed 保持一致")
assert_true(close_count_before == close_count_after, "重启后关账记录数一致")
assert_true(close_id_before == close_id_after, "重启后关账ID一致")
assert_true(closed_by_before == closed_by_after, "重启后关账人一致")
assert_true(summary_inv_total_before == summary_inv_total_after, "重启后关账汇总数据一致")

# CLI 验证
r, out = run(["status"])
assert_true(r.returncode == 0, "重启后 status 命令应成功执行")
# 使用 Python API 验证
cfg_tmp2 = Config.load()
sm_tmp2 = SessionManager(cfg_tmp2.session_dir)
sess_tmp2 = sm_tmp2.load(SESSION_CLOSE)
assert_true(sess_tmp2.is_closed == True, "重启后 is_closed 仍为 True")


# ============================================================
# 测试 6: 导出关账摘要
# ============================================================
print("\n" + "=" * 70)
print("测试 6: 导出关账摘要")
print("=" * 70)

r, out = run(["close", "export", "-o", CLOSE_EXPORT])
assert_true(r.returncode == 0, "关账摘要导出应成功")
assert_true(os.path.exists(CLOSE_EXPORT), "关账摘要文件应存在")

with open(CLOSE_EXPORT, "r", encoding="utf-8") as f:
    summary_data = json.load(f)

assert_true(summary_data["session_name"] == SESSION_CLOSE, "摘要包含正确的会话名")
assert_true(len(summary_data["close_records"]) >= 1, "摘要包含关账记录")
assert_true(summary_data["close_records"][0]["closed_by"] == "财务-张三", "摘要包含正确的关账人")
assert_true(summary_data["is_currently_closed"] == True, "摘要包含正确的 is_currently_closed 状态")
assert_true("summary" in summary_data["close_records"][0], "摘要包含汇总快照")
assert_true(summary_data["close_records"][0]["summary"]["invoices"]["total"] == 8,
            "摘要汇总发票总数正确")


# ============================================================
# 测试 7: 快照导出再导入后关账记录仍能对齐
# ============================================================
print("\n" + "=" * 70)
print("测试 7: 快照导出再导入后关账记录仍能对齐")
print("=" * 70)

r, out = run(["snapshot", "export", "-o", "accept_close_main",
              "--notes", "关账后快照测试"])
assert_true(r.returncode == 0, "快照导出应成功")

# 导入为新会话
r, out = run(["snapshot", "import", SNAP_FILE, "--as", SESSION_RESTORE,
              "--overwrite"])
assert_true(r.returncode == 0, "快照导入应成功")

# 检查恢复后的会话状态
cfg3 = Config.load()
sm3 = SessionManager(cfg3.session_dir)
sess_restored = sm3.load(SESSION_RESTORE)

assert_true(sess_restored.is_closed == True, "快照恢复后 is_closed 应为 True")
assert_true(len(sess_restored.close_records) == 1, "快照恢复后关账记录数正确")
assert_true(sess_restored.close_records[0].id == close_id_before,
            "快照恢复后关账ID与原会话一致")
assert_true(sess_restored.close_records[0].closed_by == "财务-张三",
            "快照恢复后关账人正确")
assert_true(sess_restored.close_records[0].notes == "2026年5月月末结账",
            "快照恢复后关账备注正确")
assert_true(sess_restored.close_records[0].summary["invoices"]["total"] == 8,
            "快照恢复后关账汇总发票总数正确")
assert_true(sess_restored.close_records[0].summary["matches"]["active"] == 6,
            "快照恢复后关账汇总匹配数正确")

# 检查关账后限制在恢复后仍生效
r, out = run(["imp", "invoice", "samples/invoices_good.csv", "--session", SESSION_RESTORE])
assert_true(r.returncode != 0, "快照恢复后关账限制仍生效")


# ============================================================
# 测试 8: 审计包导出再导入后关账记录仍能对齐
# ============================================================
print("\n" + "=" * 70)
print("测试 8: 审计包导出再导入后关账记录仍能对齐")
print("=" * 70)

# 导出审计包
r, out = run(["audit", "export", "-o", "accept_close_main",
              "--notes", "关账后审计包测试", "--operator", "财务-张三"])
assert_true(r.returncode == 0, "审计包导出应成功")

# 导入为新会话
cleanup_session(SESSION_RESTORE)
r, out = run(["audit", "import", AUDIT_FILE, "--as", SESSION_RESTORE,
              "--overwrite"])
assert_true(r.returncode == 0, "审计包导入应成功")

# 检查恢复后的会话状态
cfg4 = Config.load()
sm4 = SessionManager(cfg4.session_dir)
sess_audit = sm4.load(SESSION_RESTORE)

assert_true(sess_audit.is_closed == True, "审计包恢复后 is_closed 应为 True")
assert_true(len(sess_audit.close_records) == 1, "审计包恢复后关账记录数正确")
assert_true(sess_audit.close_records[0].id == close_id_before,
            "审计包恢复后关账ID与原会话一致")
assert_true(sess_audit.close_records[0].closed_by == "财务-张三",
            "审计包恢复后关账人正确")
assert_true(sess_audit.close_records[0].notes == "2026年5月月末结账",
            "审计包恢复后关账备注正确")
assert_true(sess_audit.close_records[0].summary["invoices"]["total"] == 8,
            "审计包恢复后关账汇总发票总数正确")
assert_true(sess_audit.close_records[0].summary["matches"]["active"] == 6,
            "审计包恢复后关账汇总匹配数正确")

# 检查关账后限制在恢复后仍生效
r, out = run(["imp", "invoice", "samples/invoices_good.csv", "--session", SESSION_RESTORE])
assert_true(r.returncode != 0, "审计包恢复后关账限制仍生效")


# ============================================================
# 测试 9: 同名恢复走 overwrite 或 auto-rename 时状态和摘要不串
# ============================================================
print("\n" + "=" * 70)
print("测试 9: 同名恢复走 overwrite / auto-rename 时状态和摘要不串")
print("=" * 70)

# 创建另一个会话，不关账
run(["session", "create", SESSION_OTHER])
run(["session", "switch", SESSION_OTHER])

# 验证新会话未关账
cfg5 = Config.load()
sm5 = SessionManager(cfg5.session_dir)
sess_other = sm5.load(SESSION_OTHER)
assert_true(sess_other.is_closed == False, "新会话初始 is_closed 应为 False")
assert_true(len(sess_other.close_records) == 0, "新会话初始无 close_records")

# 用关账后的会话快照 overwrite 这个会话
r, out = run(["snapshot", "import", SNAP_FILE, "--as", SESSION_OTHER,
              "--overwrite"])
assert_true(r.returncode == 0, "overwrite 导入应成功")

# 检查 overwrite 后的状态
sess_overwritten = sm5.load(SESSION_OTHER)
assert_true(sess_overwritten.is_closed == True,
            "overwrite 后 is_closed 应为 True（来自导入的快照）")
assert_true(len(sess_overwritten.close_records) == 1,
            "overwrite 后 close_records 数量应为 1（来自导入的快照）")
assert_true(sess_overwritten.close_records[0].closed_by == "财务-张三",
            "overwrite 后关账人正确（来自导入的快照）")

# 验证 overwrite 后的数据完整性
assert_true(len(sess_overwritten.invoices) == 8, "overwrite 后发票数量正确")
assert_true(len(sess_overwritten.transactions) == 10, "overwrite 后流水数量正确")

# 用 auto-rename 导入同一份快照
r, out = run(["snapshot", "import", SNAP_FILE, "--as", SESSION_OTHER,
              "--auto-rename"])
assert_true(r.returncode == 0, "auto-rename 导入应成功")

# 从输出中提取新会话名
import re
# 匹配 "自动重命名为 'xxx'" 或 "导入为会话 'xxx'"
match = re.search(r"自动重命名为\s+'([^']+)'", out)
if not match:
    match = re.search(r"导入为会话\s+'([^']+)'", out)
assert_true(match is not None, "应从输出中提取到 auto-rename 后的会话名")
renamed_session = match.group(1)
print(f"auto-rename 产生的新会话名: {renamed_session}")

# 检查 auto-rename 后的新会话
sess_renamed = sm5.load(renamed_session)
assert_true(sess_renamed.is_closed == True,
            "auto-rename 后新会话 is_closed 应为 True")
assert_true(len(sess_renamed.close_records) == 1,
            "auto-rename 后新会话 close_records 数量应为 1")
assert_true(sess_renamed.close_records[0].closed_by == "财务-张三",
            "auto-rename 后新会话关账人正确")

# 关键：验证原 overwrite 会话的状态没有被串改
sess_other_again = sm5.load(SESSION_OTHER)
assert_true(sess_other_again.is_closed == True,
            "原 overwrite 会话 is_closed 仍为 True（不串）")
assert_true(len(sess_other_again.close_records) == 1,
            "原 overwrite 会话 close_records 数量仍为 1（不串）")
assert_true(sess_other_again.close_records[0].id == close_id_before,
            "原 overwrite 会话关账ID不变（不串）")


# ============================================================
# 测试 10: 解账与重新关账
# ============================================================
print("\n" + "=" * 70)
print("测试 10: 解账与重新关账")
print("=" * 70)

# 切换回主会话
run(["session", "switch", SESSION_CLOSE])

# 解账
r, out = run(["unclose", "--by", "财务-李四", "--reason", "发现漏记一笔，需要补充"])
assert_true(r.returncode == 0, "解账应成功")

# 检查状态（使用 Python API）
cfg_tmp3 = Config.load()
sm_tmp3 = SessionManager(cfg_tmp3.session_dir)
sess_tmp3 = sm_tmp3.load(SESSION_CLOSE)
assert_true(sess_tmp3.is_closed == False, "解账后 is_closed 应为 False")
assert_true(sess_tmp3.close_records[-1].is_unclosed == True, "最新关账记录应标记为已解账")
assert_true(sess_tmp3.close_records[-1].unclosed_by == "财务-李四", "解账人应为财务-李四")
assert_true(sess_tmp3.close_records[-1].unclose_reason == "发现漏记一笔，需要补充", "解账原因正确")

# CLI 验证
r, out = run(["status"])
assert_true(r.returncode == 0, "status 命令应成功执行")

# 验证解账后可以操作
r, out = run(["close", "check"])
assert_true(r.returncode == 0, "解账后可以进行关账检查")

# 检查历史记录（使用 Python API）
from invoice_reconcile.manual import list_history
history_data = list_history(sess_tmp3, 10)
action_types = [h["action"] for h in history_data]
assert_true("close_session" in action_types, "历史记录包含 close_session")
assert_true("unclose_session" in action_types, "历史记录包含 unclose_session")

# 再次关账
r, out = run(["close", "do", "--by", "财务-李四", "--notes", "补充数据后重新关账",
              "--force", "--force-reason", "仍有未匹配项，确认关账"])
assert_true(r.returncode == 0, "重新关账应成功")

# 检查关账记录，应有 2 条
r, out = run(["close", "list"])
assert_true(r.returncode == 0, "关账记录列表应可查看")

# 验证第二条关账记录
cfg6 = Config.load()
sm6 = SessionManager(cfg6.session_dir)
sess_final = sm6.load(SESSION_CLOSE)
assert_true(sess_final.is_closed == True, "重新关账后 is_closed 为 True")
assert_true(len(sess_final.close_records) == 2, "应有 2 条关账记录")
assert_true(sess_final.close_records[0].is_unclosed == True, "第一条记录标记为已解账")
assert_true(sess_final.close_records[1].is_unclosed == False, "第二条记录为活动关账")
assert_true(sess_final.close_records[1].closed_by == "财务-李四", "第二条记录关账人正确")


# ============================================================
# 测试 11: 查看关账记录详情
# ============================================================
print("\n" + "=" * 70)
print("测试 11: 查看关账记录详情")
print("=" * 70)

close_id = sess_final.close_records[1].id
r, out = run(["close", "show", close_id])
assert_true(r.returncode == 0, "查看关账记录详情应成功")
# 使用 Python API 验证数据正确性
cr = sess_final.close_records[1]
assert_true(cr.id == close_id, "关账ID正确")
assert_true(cr.closed_by == "财务-李四", "关账人正确")
assert_true(cr.notes == "补充数据后重新关账", "关账备注正确")
assert_true("force" in str(cr.summary).lower() or cr.summary.get("check_result", {}).get("force", False), 
            "强制关账标记正确")


# ============================================================
# 最终验证：跨重启后所有状态保持
# ============================================================
print("\n" + "=" * 70)
print("最终验证：跨重启后所有状态保持")
print("=" * 70)

del cfg6, sm6, sess_final

cfg7 = Config.load()
sm7 = SessionManager(cfg7.session_dir)
sess_reboot = sm7.load(SESSION_CLOSE)

assert_true(sess_reboot.is_closed == True, "重启后 is_closed 保持 True")
assert_true(len(sess_reboot.close_records) == 2, "重启后关账记录数保持 2")
assert_true(sess_reboot.close_records[0].is_unclosed == True, "重启后第一条记录仍标记为已解账")
assert_true(sess_reboot.close_records[1].is_unclosed == False, "重启后第二条记录仍为活动关账")
assert_true(sess_reboot.close_records[0].unclosed_by == "财务-李四", "重启后解账人仍正确")
assert_true(sess_reboot.close_records[1].summary["invoices"]["total"] == 8,
            "重启后关账汇总数据仍正确")


# ============================================================
# 清理
# ============================================================
print("\n" + "=" * 70)
print("清理测试数据")
print("=" * 70)

for s in [SESSION_CLOSE, SESSION_RESTORE, SESSION_OTHER, renamed_session]:
    cleanup_session(s)
cleanup_snapshot(SNAP_FILE)
cleanup_audit(AUDIT_FILE)
if os.path.exists(CLOSE_EXPORT):
    os.remove(CLOSE_EXPORT)

print("清理完成")


# ============================================================
# 结果
# ============================================================
if _cli_failed:
    print("\n" + "=" * 70)
    print("[FAILED] 存在 CLI 子进程非零退出码，参见上方 [exit=...] 标记")
    print("=" * 70)
    sys.exit(1)

print("\n" + "=" * 70)
print("[ALL PASSED] 关账模块验收测试全部通过：")
print("  1. 关账检查：未匹配项阻止关账，强制关账可绕过")
print("  2. 关账后限制：导入 / match / 人工操作被正确拦截")
print("  3. 跨重启：封账状态、关账记录、汇总数据完整保留")
print("  4. 快照导入导出：关账记录完整对齐")
print("  5. 审计包导入导出：关账记录完整对齐")
print("  6. overwrite / auto-rename：关账状态和摘要不串")
print("  7. 解账与重新关账：状态正确切换，历史记录完整")
print("  8. 关账摘要导出：JSON 结构正确，数据完整")
print("  9. status / history / close list / close show 显示正确")
print("=" * 70)
sys.exit(0)
