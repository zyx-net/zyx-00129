# -*- coding: utf-8 -*-
"""
一对多自动匹配边界场景回归测试
覆盖：
  1) 首个命中组合日期不合格但后续组合应成功匹配（INV-2026-009 6000 元场景）
  2) 一对多金额命中但全部候选组合超出日期容差时仍保持未解决（INV-2026-010 4000 元场景）
"""
import sys
import os
import json
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

os.chdir(os.path.dirname(os.path.abspath(__file__)))

SESSION_NAME = "regression_boundary"
# 清理旧的测试会话
import shutil
sess_dir = os.path.join(".irec_sessions", SESSION_NAME + ".json")
state = ".irec_state.json"
rpt_json = "regression_report.json"
rpt_dir = "regression_report"
for p in [sess_dir, rpt_json, rpt_dir]:
    if os.path.isfile(p):
        os.remove(p)
    elif os.path.isdir(p):
        shutil.rmtree(p, ignore_errors=True)


def run(args):
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
    return r, out + "\n" + err


def assert_true(cond, msg):
    if cond:
        print(f"[PASS] {msg}")
    else:
        print(f"[FAIL] {msg}")
        raise SystemExit(1)


# ---- 1. 创建会话
run(["session", "create", SESSION_NAME])
run(["session", "switch", SESSION_NAME])

# ---- 2. 配置客户名别名（原数据 + 拼多多）
for canonical, aliases in [
    ("华为技术有限公司", ["华为"]),
    ("阿里巴巴集团", ["阿里"]),
    ("腾讯科技", ["腾讯"]),
    ("字节跳动", ["字节"]),
    ("百度在线", ["百度"]),
    ("京东集团", ["京东"]),
    ("美团点评", ["美团"]),
    ("小米科技", ["小米"]),
    ("拼多多科技有限公司", ["拼多多", "PDD"]),
]:
    run(["config", "alias", canonical] + aliases)

# ---- 3. 导入数据
run(["imp", "invoice", "samples/invoices_good.csv"])
run(["imp", "txn", "samples/transactions_good.csv"])
run(["imp", "invoice", "samples/invoices_regression.csv"])
run(["imp", "txn", "samples/transactions_regression.csv"])

# ---- 4. 自动匹配
res_match, _ = run(["match"])

# ---- 5. 加载会话检查关键断言
from invoice_reconcile.config import Config
from invoice_reconcile.session import SessionManager

cfg = Config.load()
sm = SessionManager(cfg.session_dir)
sess = sm.load(SESSION_NAME)

print("\n" + "=" * 70)
print("开始断言检查")
print("=" * 70)

# 找发票对象
inv_by_no = {i.invoice_no: i for i in sess.invoices.values()}
txn_by_no = {t.txn_id: t for t in sess.transactions.values()}
txn_by_id = {t.id: t for t in sess.transactions.values()}

inv9 = inv_by_no.get("INV-2026-009")  # 场景1：6000元，应被匹配
inv10 = inv_by_no.get("INV-2026-010")  # 场景2：4000元，应未匹配
t11 = txn_by_no.get("TXN20260601011")   # 1000 @ 5/1 超容差，应未匹配（未在合格组合里）
t12 = txn_by_no.get("TXN20260601012")   # 5000 @ 5/1 超容差，应未匹配
t13 = txn_by_no.get("TXN20260601013")   # 2000 @ 6/14 合格，应被匹配
t14 = txn_by_no.get("TXN20260601014")   # 4000 @ 6/16 合格，应被匹配
t15 = txn_by_no.get("TXN20260601015")   # 1500 @ 5/1 超容差，应未匹配
t16 = txn_by_no.get("TXN20260601016")   # 2500 @ 5/1 超容差，应未匹配

# --- 场景1断言 ---
assert_true(inv9 is not None, "场景1：发票 INV-2026-009 存在")
assert_true(inv9.status == "matched",
            f"场景1：INV-2026-009 状态应为 matched，实际={inv9.status} → 后续合格日期组合(T3,T4)被命中")
assert_true(t13.status == "matched", f"场景1：TXN20260601013(T3=2000合格日期) 应被匹配")
assert_true(t14.status == "matched", f"场景1：TXN20260601014(T4=4000合格日期) 应被匹配")
assert_true(t11.status == "unmatched",
            f"场景1：TXN20260601011(T1=1000超容差) 应未匹配(不在合格组合里)，实际={t11.status}")
assert_true(t12.status == "unmatched",
            f"场景1：TXN20260601012(T2=5000超容差) 应未匹配(不在合格组合里)，实际={t12.status}")

# --- 场景2断言 ---
assert_true(inv10 is not None, "场景2：发票 INV-2026-010 存在")
assert_true(inv10.status == "unmatched",
            f"场景2：INV-2026-010 应保持 unmatched(所有候选日期都超容差)，实际={inv10.status}")
assert_true(t15.status == "unmatched",
            f"场景2：TXN20260601015(T5=1500超容差) 应未匹配，实际={t15.status}")
assert_true(t16.status == "unmatched",
            f"场景2：TXN20260601016(T6=2500超容差) 应未匹配，实际={t16.status}")

# 找到INV-2026-009对应的匹配记录，验证匹配的是 T3+T4 而不是 T1+T2
print("\n  >> 细节验证：INV-2026-009 所在匹配记录里包含的流水号")
pdd_match_notes = []
for m in sess.matches.values():
    if m.reversed:
        continue
    if inv9.id in m.invoice_ids:
        txn_ids_in_match = m.transaction_ids
        txn_nos_in_match = [txn_by_id[i].txn_id for i in txn_ids_in_match if i in txn_by_id]
        pdd_match_notes = txn_nos_in_match
        print(f"     匹配ID={m.id}  流水={txn_nos_in_match}  备注={m.notes}")
        break
assert_true("TXN20260601013" in pdd_match_notes and "TXN20260601014" in pdd_match_notes,
            f"场景1：正确命中日期合格组合(T3+T4)，实际匹配={pdd_match_notes}")
assert_true("TXN20260601011" not in pdd_match_notes,
            f"场景1：超容差的(T1+T2)不应匹配，实际匹配={pdd_match_notes}")
assert_true("TXN20260601012" not in pdd_match_notes,
            f"场景1：超容差的(T1+T2)不应匹配，实际匹配={pdd_match_notes}")

# --- 报表 ---
print("\n" + "=" * 70)
print("CLI 报告导出检查")
print("=" * 70)
res_rpt, _ = run(["report", "-f", "both", "-o", rpt_dir[:-5] if rpt_dir.endswith("json") else rpt_dir])

assert_true(os.path.exists(rpt_json := (rpt_dir + ".json" if not rpt_dir.endswith("json") else rpt_dir)),
            "JSON 报告文件存在")
assert_true(os.path.isdir(rpt_dir), "CSV 报告目录存在")

# 加载JSON报告做结构断言
with open(rpt_json, "r", encoding="utf-8") as f:
    rpt = json.load(f)

print("\n  >> 报表匹配明细中的拼多多相关记录")
pdd_matches = []
for m in rpt["matches"]:
    custs = set(i["customer_name"] for i in m["invoices"])
    if any("拼多多" in c for c in custs):
        pdd_matches.append(m)
        print(f"     匹配ID={m['match_id']}  类型={m['match_type']}  发票={[i['invoice_no'] for i in m['invoices']]}  流水={[t['txn_id'] for t in m['transactions']]}")

assert_true(len(pdd_matches) >= 1, "报告中应存在至少1条拼多多匹配记录(场景1的6000元)")
# 报表里的未匹配列表断言
uinv_nos = {i["invoice_no"] for i in rpt["unmatched_invoices"] if "INV-2026-010" in i.get("invoice_no", "")}
assert_true("INV-2026-010" in uinv_nos,
            f"场景2：报表未匹配发票列表中应包含 INV-2026-010，实际={[i['invoice_no'] for i in rpt['unmatched_invoices']]}")
utxn_nos = {t["txn_id"] for t in rpt["unmatched_transactions"]}
for expected_remain in ["TXN20260601011", "TXN20260601012", "TXN20260601015", "TXN20260601016"]:
    assert_true(expected_remain in utxn_nos,
                f"场景2：报表未匹配流水列表中应含 {expected_remain}")

# ---- 6. 重启持久化验证 ----
print("\n" + "=" * 70)
print("重启持久化验证")
print("=" * 70)
sess_id_before = sess.session_id
summary_before = sm.status_summary(sess)
unmatched_inv_before = sorted(i.invoice_no for i in sess.invoices.values() if i.status == "unmatched")
unmatched_txn_before = sorted(t.txn_id for t in sess.transactions.values() if t.status == "unmatched")
reversed_count_before = sum(1 for m in sess.matches.values() if m.reversed)

# 重新加载（模拟重启）
del cfg, sm, sess
cfg2 = Config.load()
sm2 = SessionManager(cfg2.session_dir)
sess2 = sm2.load(SESSION_NAME)
summary_after = sm2.status_summary(sess2)

assert_true(sess_id_before == summary_after["session_id"], "重启后会话ID不变")
for k in ["inv_total", "inv_matched", "inv_unmatched", "inv_suspended",
          "inv_amount_total", "inv_amount_matched",
          "txn_total", "txn_matched", "txn_unmatched", "txn_amount_total", "txn_amount_matched",
          "matches_active", "matches_reversed", "history_count"]:
    mapk = {"inv_total": ("invoices", "total"), "inv_matched": ("invoices", "matched"),
            "inv_unmatched": ("invoices", "unmatched"), "inv_suspended": ("invoices", "suspended"),
            "inv_amount_total": ("invoices", "amount_total"), "inv_amount_matched": ("invoices", "amount_matched"),
            "txn_total": ("transactions", "total"), "txn_matched": ("transactions", "matched"),
            "txn_unmatched": ("transactions", "unmatched"),
            "txn_amount_total": ("transactions", "amount_total"), "txn_amount_matched": ("transactions", "amount_matched"),
            "matches_active": ("matches", "active"), "matches_reversed": ("matches", "reversed"),
            "history_count": ("history_count", None)}
    a, b = mapk[k]
    vb = summary_before[a] if b is None else summary_before[a][b]
    va = summary_after[a] if b is None else summary_after[a][b]
    assert_true(vb == va, f"重启一致 {k}: before={vb} after={va}")

unmatched_inv_after = sorted(i.invoice_no for i in sess2.invoices.values() if i.status == "unmatched")
unmatched_txn_after = sorted(t.txn_id for t in sess2.transactions.values() if t.status == "unmatched")
assert_true(unmatched_inv_before == unmatched_inv_after,
            f"重启一致 未匹配发票列表 before={unmatched_inv_before} after={unmatched_inv_after}")
assert_true(unmatched_txn_before == unmatched_txn_after,
            f"重启一致 未匹配流水列表 before={unmatched_txn_before} after={unmatched_txn_after}")

# 最后展示待复核列表
print("\n" + "=" * 70)
print("修复后待复核列表（show unmatched 输出）")
print("=" * 70)
run(["show", "unmatched"])

print("\n" + "=" * 70)
print("[ALL PASSED] 边界场景回归通过：")
print("  场景1：首个(T1+T2)超容差→继续遍历→命中(T3+T4)合格组合→自动核销成功")
print("  场景2：唯一(T5+T6)超容差→全部候选不合格→保持在待复核（报表也包含它们）")
print("  附加：会话重启持久化全部一致")
print("=" * 70)
