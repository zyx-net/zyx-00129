# -*- coding: utf-8 -*-
"""
关账（Closeout）功能回归测试
确保关账功能不破坏现有核心功能，覆盖：
  A. 未关账会话的正常操作不受影响
  B. 关账后操作限制正确生效
  C. 解账后操作恢复正常
  D. 关账不影响其他会话
  E. 历史记录正确记录关账/解账动作
  F. 现有快照、审计包功能不受关账影响
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
    return r, out + "\n" + err


def assert_true(cond, msg):
    if not cond:
        print(f"[FAIL] {msg}")
        sys.exit(1)
    print(f"[PASS] {msg}")


def cleanup_session(name):
    cfg = __import__("invoice_reconcile.config", fromlist=["Config"]).Config.load()
    sess_dir = cfg.session_dir
    sess_file = os.path.join(sess_dir, f"{name}.json")
    if os.path.exists(sess_file):
        os.remove(sess_file)


SESSION_NORMAL = "reg_close_normal"
SESSION_CLOSED = "reg_close_closed"
SESSION_OTHER = "reg_close_other"


# ============================================================
# 测试 A: 未关账会话的正常操作不受影响
# ============================================================
print("\n" + "=" * 70)
print("测试 A: 未关账会话的正常操作不受影响")
print("=" * 70)

cleanup_session(SESSION_NORMAL)
run(["session", "create", SESSION_NORMAL])
run(["session", "switch", SESSION_NORMAL])

# 导入发票
r, out = run(["imp", "invoice", "samples/invoices_good.csv", "--session", SESSION_NORMAL])
assert_true(r.returncode == 0, "未关账会话导入发票应正常")

# 导入流水
r, out = run(["imp", "txn", "samples/transactions_good.csv", "--session", SESSION_NORMAL])
assert_true(r.returncode == 0, "未关账会话导入流水应正常")

# 自动匹配
r, out = run(["match", "--session", SESSION_NORMAL])
assert_true(r.returncode == 0, "未关账会话自动匹配应正常")

# 检查匹配结果
from invoice_reconcile.config import Config
from invoice_reconcile.session import SessionManager

cfg = Config.load()
sm = SessionManager(cfg.session_dir)
sess = sm.load(SESSION_NORMAL)
assert_true(len(sess.matches) > 0, "未关账会话匹配结果应正常")

# 关账检查
r, out = run(["close", "check", "--session", SESSION_NORMAL])
# 有未匹配项，返回非零是正常的
assert_true("关账前置检查" in out or "待处理" in out or "未匹配" in out,
            "关账检查命令应正常执行")

# status 命令
r, out = run(["status", "--session", SESSION_NORMAL])
assert_true(r.returncode == 0, "未关账会话 status 命令应正常")
assert_true("未关账" in out, "未关账会话状态应显示为未关账")

# history 命令
r, out = run(["show", "history", "--session", SESSION_NORMAL])
assert_true(r.returncode == 0, "未关账会话 history 命令应正常")


# ============================================================
# 测试 B: 关账后操作限制正确生效
# ============================================================
print("\n" + "=" * 70)
print("测试 B: 关账后操作限制正确生效")
print("=" * 70)

cleanup_session(SESSION_CLOSED)
run(["session", "create", SESSION_CLOSED])
run(["session", "switch", SESSION_CLOSED])
run(["imp", "invoice", "samples/invoices_good.csv", "--session", SESSION_CLOSED])
run(["imp", "txn", "samples/transactions_good.csv", "--session", SESSION_CLOSED])
run(["match", "--session", SESSION_CLOSED])

# 强制关账
r, out = run(["close", "do", "--by", "财务-测试", "--notes", "回归测试关账",
              "--force", "--force-reason", "回归测试强制关账",
              "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "关账应成功")

# 验证关账状态
sess_closed = sm.load(SESSION_CLOSED)
assert_true(sess_closed.is_closed == True, "关账后 is_closed 应为 True")
assert_true(len(sess_closed.close_records) == 1, "关账后应有 1 条关账记录")

# 尝试导入发票（应被拒绝）
r, out = run(["imp", "invoice", "samples/invoices_good.csv", "--session", SESSION_CLOSED], expect_fail=True)
assert_true(r.returncode != 0, "关账后导入发票应被拒绝")
assert_true("会话已关账" in out or "关账" in out, "拒绝原因应包含关账相关提示")

# 尝试导入流水（应被拒绝）
r, out = run(["imp", "txn", "samples/transactions_good.csv", "--session", SESSION_CLOSED], expect_fail=True)
assert_true(r.returncode != 0, "关账后导入流水应被拒绝")

# 尝试自动匹配（应被拒绝）
r, out = run(["match", "--session", SESSION_CLOSED], expect_fail=True)
assert_true(r.returncode != 0, "关账后自动匹配应被拒绝")

# status 命令应显示关账状态
r, out = run(["status", "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "关账后 status 命令应正常执行")
assert_true("已关账" in out, "关账后状态应显示为已关账")
assert_true("关账后限制" in out, "关账后应显示限制提示")

# close list 命令
r, out = run(["close", "list", "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "关账后 close list 命令应正常执行")


# ============================================================
# 测试 C: 解账后操作恢复正常
# ============================================================
print("\n" + "=" * 70)
print("测试 C: 解账后操作恢复正常")
print("=" * 70)

# 解账
r, out = run(["unclose", "--by", "财务-测试", "--reason", "回归测试解账",
              "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "解账应成功")

# 验证解账状态
sess_unclosed = sm.load(SESSION_CLOSED)
assert_true(sess_unclosed.is_closed == False, "解账后 is_closed 应为 False")
assert_true(sess_unclosed.close_records[-1].is_unclosed == True, "解账后最新关账记录应标记为已解账")

# 解账后应可以正常操作
r, out = run(["imp", "invoice", "samples/invoices_regression.csv", "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "解账后导入发票应恢复正常")

r, out = run(["match", "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "解账后自动匹配应恢复正常")

# status 命令应显示未关账状态
r, out = run(["status", "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "解账后 status 命令应正常执行")
assert_true("未关账" in out, "解账后状态应显示为未关账")


# ============================================================
# 测试 D: 关账不影响其他会话
# ============================================================
print("\n" + "=" * 70)
print("测试 D: 关账不影响其他会话")
print("=" * 70)

# 重新关账 SESSION_CLOSED
run(["close", "do", "--by", "财务-测试", "--notes", "重新关账",
     "--force", "--force-reason", "测试跨会话隔离",
     "--session", SESSION_CLOSED])

# 创建另一个会话，不关账
cleanup_session(SESSION_OTHER)
run(["session", "create", SESSION_OTHER])
run(["session", "switch", SESSION_OTHER])

# 验证另一个会话未关账
sess_other = sm.load(SESSION_OTHER)
assert_true(sess_other.is_closed == False, "其他会话 is_closed 应为 False")
assert_true(len(sess_other.close_records) == 0, "其他会话应无关账记录")

# 另一个会话应可以正常操作
r, out = run(["imp", "invoice", "samples/invoices_good.csv", "--session", SESSION_OTHER])
assert_true(r.returncode == 0, "其他会话导入发票应正常")

r, out = run(["imp", "txn", "samples/transactions_good.csv", "--session", SESSION_OTHER])
assert_true(r.returncode == 0, "其他会话导入流水应正常")

r, out = run(["match", "--session", SESSION_OTHER])
assert_true(r.returncode == 0, "其他会话自动匹配应正常")

# 验证第一个会话仍处于关账状态
sess_closed2 = sm.load(SESSION_CLOSED)
assert_true(sess_closed2.is_closed == True, "第一个会话仍应处于关账状态")


# ============================================================
# 测试 E: 历史记录正确记录关账/解账动作
# ============================================================
print("\n" + "=" * 70)
print("测试 E: 历史记录正确记录关账/解账动作")
print("=" * 70)

from invoice_reconcile.manual import list_history

history_data = list_history(sess_closed2, 20)
action_types = [h["action"] for h in history_data]

assert_true("close_session" in action_types, "历史记录应包含 close_session")
assert_true("unclose_session" in action_types, "历史记录应包含 unclose_session")

# 检查关账历史记录的详细信息
close_entries = [h for h in history_data if h["action"] == "close_session"]
assert_true(len(close_entries) >= 2, "应有至少 2 条关账记录")
assert_true(close_entries[0]["details"]["closed_by"] == "财务-测试",
            "关账历史记录应包含正确的关账人")

# 检查解账历史记录的详细信息
unclose_entries = [h for h in history_data if h["action"] == "unclose_session"]
assert_true(len(unclose_entries) >= 1, "应有至少 1 条解账记录")
assert_true(unclose_entries[0]["details"]["unclosed_by"] == "财务-测试",
            "解账历史记录应包含正确的解账人")
assert_true(unclose_entries[0]["details"]["reason"] == "回归测试解账",
            "解账历史记录应包含正确的解账原因")


# ============================================================
# 测试 F: 现有快照、审计包功能不受关账影响
# ============================================================
print("\n" + "=" * 70)
print("测试 F: 现有快照、审计包功能不受关账影响")
print("=" * 70)

# 导出快照
SNAP_FILE = "reg_close_test.irecsnap"
if os.path.exists(os.path.join(".irec_snapshots", SNAP_FILE)):
    os.remove(os.path.join(".irec_snapshots", SNAP_FILE))

r, out = run(["snapshot", "export", "-o", "reg_close_test", "--notes", "回归测试快照",
              "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "关账会话导出快照应正常")
assert_true(os.path.exists(os.path.join(".irec_snapshots", SNAP_FILE)), "快照文件应存在")

# 导入快照
SNAP_RESTORE = "reg_close_snap_restore"
cleanup_session(SNAP_RESTORE)
r, out = run(["snapshot", "import", os.path.join(".irec_snapshots", SNAP_FILE),
              "--as", SNAP_RESTORE, "--overwrite"])
assert_true(r.returncode == 0, "导入包含关账记录的快照应正常")

# 验证导入后的会话状态
sess_restored = sm.load(SNAP_RESTORE)
assert_true(sess_restored.is_closed == True, "导入的会话应保持关账状态")
assert_true(len(sess_restored.close_records) >= 1, "导入的会话应有关账记录")

# 导出审计包
AUDIT_FILE = "reg_close_test.irecaudit"
if os.path.exists(os.path.join(".irec_audits", AUDIT_FILE)):
    os.remove(os.path.join(".irec_audits", AUDIT_FILE))

r, out = run(["audit", "export", "-o", "reg_close_test", "--notes", "回归测试审计包",
              "--operator", "财务-测试", "--session", SESSION_CLOSED])
assert_true(r.returncode == 0, "关账会话导出审计包应正常")
assert_true(os.path.exists(os.path.join(".irec_audits", AUDIT_FILE)), "审计包文件应存在")

# 导入审计包
AUDIT_RESTORE = "reg_close_audit_restore"
cleanup_session(AUDIT_RESTORE)
r, out = run(["audit", "import", os.path.join(".irec_audits", AUDIT_FILE),
              "--as", AUDIT_RESTORE, "--overwrite"])
assert_true(r.returncode == 0, "导入包含关账记录的审计包应正常")

# 验证导入后的会话状态
sess_audit = sm.load(AUDIT_RESTORE)
assert_true(sess_audit.is_closed == True, "导入的审计包会话应保持关账状态")
assert_true(len(sess_audit.close_records) >= 1, "导入的审计包会话应有关账记录")


# ============================================================
# 清理测试数据
# ============================================================
print("\n" + "=" * 70)
print("清理测试数据")
print("=" * 70)

cleanup_session(SESSION_NORMAL)
cleanup_session(SESSION_CLOSED)
cleanup_session(SESSION_OTHER)
cleanup_session(SNAP_RESTORE)
cleanup_session(AUDIT_RESTORE)

snap_path = os.path.join(".irec_snapshots", SNAP_FILE)
if os.path.exists(snap_path):
    os.remove(snap_path)

audit_path = os.path.join(".irec_audits", AUDIT_FILE)
if os.path.exists(audit_path):
    os.remove(audit_path)

print("清理完成")


# ============================================================
# 最终检查
# ============================================================
print("\n" + "=" * 70)
if _cli_failed:
    print("[FAILED] 存在 CLI 子进程非预期退出码，参见上文 [exit=...] 标记")
    sys.exit(1)
else:
    print("[PASSED] 所有关账回归测试通过！")
    print("=" * 70)
