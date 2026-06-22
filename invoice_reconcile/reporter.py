import csv
import json
import os
from datetime import datetime
from typing import Dict, List, Any, Optional

from .session import Session, SessionManager
from .config import Config


def build_report(session: Session, config: Config) -> Dict[str, Any]:
    sm = SessionManager(config.session_dir)
    summary = sm.status_summary(session)

    matched_details = []
    for match in session.matches.values():
        if match.reversed:
            continue
        invs = [session.invoices[i] for i in match.invoice_ids if i in session.invoices]
        txns = [session.transactions[t] for t in match.transaction_ids if t in session.transactions]
        inv_sum = sum(i.amount for i in invs)
        txn_sum = sum(t.amount for t in txns)
        matched_details.append({
            "match_id": match.id,
            "match_type": match.match_type,
            "matched_at": match.matched_at,
            "notes": match.notes,
            "invoices": [
                {
                    "id": i.id,
                    "invoice_no": i.invoice_no,
                    "customer_name": i.customer_name,
                    "amount": i.amount,
                    "invoice_date": i.invoice_date,
                }
                for i in invs
            ],
            "transactions": [
                {
                    "id": t.id,
                    "txn_id": t.txn_id,
                    "counterparty": t.counterparty,
                    "amount": t.amount,
                    "txn_date": t.txn_date,
                }
                for t in txns
            ],
            "invoice_amount_sum": round(inv_sum, 2),
            "transaction_amount_sum": round(txn_sum, 2),
            "diff_amount": round(abs(inv_sum - txn_sum), 2),
        })

    reversed_details = []
    for match in session.matches.values():
        if not match.reversed:
            continue
        invs = [session.invoices[i] for i in match.invoice_ids if i in session.invoices]
        txns = [session.transactions[t] for t in match.transaction_ids if t in session.transactions]
        reversed_details.append({
            "match_id": match.id,
            "match_type": match.match_type,
            "matched_at": match.matched_at,
            "reversed_at": match.reversed_at,
            "reversed_reason": match.reversed_reason,
            "invoice_nos": [i.invoice_no for i in invs],
            "txn_ids": [t.txn_id for t in txns],
        })

    unmatched_invoices = [
        {
            "id": i.id,
            "invoice_no": i.invoice_no,
            "customer_name": i.customer_name,
            "amount": i.amount,
            "invoice_date": i.invoice_date,
            "suspended": i.suspended,
            "suspend_reason": i.suspend_reason,
            "source_file": i.source_file,
            "source_row": i.source_row,
        }
        for i in session.invoices.values()
        if i.status == "unmatched"
    ]

    unmatched_transactions = [
        {
            "id": t.id,
            "txn_id": t.txn_id,
            "counterparty": t.counterparty,
            "amount": t.amount,
            "txn_date": t.txn_date,
            "suspended": t.suspended,
            "suspend_reason": t.suspend_reason,
            "source_file": t.source_file,
            "source_row": t.source_row,
        }
        for t in session.transactions.values()
        if t.status == "unmatched"
    ]

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "config": {
            "amount_tolerance": config.amount_tolerance,
            "date_tolerance_days": config.date_tolerance_days,
            "customer_name_aliases": config.customer_name_aliases,
            "match_strategy": config.match_strategy,
        },
        "matches": matched_details,
        "reversed_matches": reversed_details,
        "unmatched_invoices": unmatched_invoices,
        "unmatched_transactions": unmatched_transactions,
        "history_count": len(session.history),
    }


def export_json(report: Dict[str, Any], filepath: str) -> None:
    filepath = os.path.abspath(filepath)
    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def export_csv(report: Dict[str, Any], output_dir: str) -> Dict[str, str]:
    output_dir = os.path.abspath(output_dir)
    os.makedirs(output_dir, exist_ok=True)
    files = {}

    # ---- 汇总表 ----
    sum_path = os.path.join(output_dir, "summary.csv")
    with open(sum_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        s = report["summary"]
        w.writerow(["项目", "数值"])
        w.writerow(["会话ID", s["session_id"]])
        w.writerow(["会话名称", s["name"]])
        w.writerow(["创建时间", s["created_at"]])
        w.writerow(["最后更新", s["updated_at"]])
        w.writerow(["发票总数", s["invoices"]["total"]])
        w.writerow(["发票匹配数", s["invoices"]["matched"]])
        w.writerow(["发票未匹配数", s["invoices"]["unmatched"]])
        w.writerow(["发票挂起数", s["invoices"]["suspended"]])
        w.writerow(["发票总额", s["invoices"]["amount_total"]])
        w.writerow(["发票已匹配总额", s["invoices"]["amount_matched"]])
        w.writerow(["流水总数", s["transactions"]["total"]])
        w.writerow(["流水匹配数", s["transactions"]["matched"]])
        w.writerow(["流水未匹配数", s["transactions"]["unmatched"]])
        w.writerow(["流水挂起数", s["transactions"]["suspended"]])
        w.writerow(["流水总额", s["transactions"]["amount_total"]])
        w.writerow(["流水已匹配总额", s["transactions"]["amount_matched"]])
        w.writerow(["活动匹配数", s["matches"]["active"]])
        w.writerow(["已撤销匹配数", s["matches"]["reversed"]])
        w.writerow(["历史记录数", s["history_count"]])
    files["summary"] = sum_path

    # ---- 匹配明细表 ----
    match_path = os.path.join(output_dir, "matches.csv")
    with open(match_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["匹配ID", "匹配类型", "匹配时间", "发票号", "发票客户",
                    "发票金额", "开票日期", "流水号", "流水对方", "流水金额",
                    "交易日期", "发票合计", "流水合计", "差额", "备注"])
        for m in report["matches"]:
            max_rows = max(len(m["invoices"]), len(m["transactions"]))
            for r in range(max_rows):
                inv = m["invoices"][r] if r < len(m["invoices"]) else {}
                txn = m["transactions"][r] if r < len(m["transactions"]) else {}
                w.writerow([
                    m["match_id"] if r == 0 else "",
                    m["match_type"] if r == 0 else "",
                    m["matched_at"] if r == 0 else "",
                    inv.get("invoice_no", ""),
                    inv.get("customer_name", ""),
                    inv.get("amount", ""),
                    inv.get("invoice_date", ""),
                    txn.get("txn_id", ""),
                    txn.get("counterparty", ""),
                    txn.get("amount", ""),
                    txn.get("txn_date", ""),
                    m["invoice_amount_sum"] if r == 0 else "",
                    m["transaction_amount_sum"] if r == 0 else "",
                    m["diff_amount"] if r == 0 else "",
                    m["notes"] if r == 0 else "",
                ])
    files["matches"] = match_path

    # ---- 未匹配发票 ----
    uinv_path = os.path.join(output_dir, "unmatched_invoices.csv")
    with open(uinv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ID", "发票号", "客户名称", "金额", "开票日期", "挂起", "挂起原因", "来源文件", "来源行号"])
        for i in report["unmatched_invoices"]:
            w.writerow([i["id"], i["invoice_no"], i["customer_name"], i["amount"],
                        i["invoice_date"], "是" if i["suspended"] else "否",
                        i["suspend_reason"], i["source_file"], i["source_row"]])
    files["unmatched_invoices"] = uinv_path

    # ---- 未匹配流水 ----
    utxn_path = os.path.join(output_dir, "unmatched_transactions.csv")
    with open(utxn_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ID", "流水号", "对方单位", "金额", "交易日期", "挂起", "挂起原因", "来源文件", "来源行号"])
        for t in report["unmatched_transactions"]:
            w.writerow([t["id"], t["txn_id"], t["counterparty"], t["amount"],
                        t["txn_date"], "是" if t["suspended"] else "否",
                        t["suspend_reason"], t["source_file"], t["source_row"]])
    files["unmatched_transactions"] = utxn_path

    # ---- 撤销记录 ----
    rev_path = os.path.join(output_dir, "reversed_matches.csv")
    with open(rev_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["匹配ID", "原匹配类型", "匹配时间", "撤销时间", "撤销原因", "涉及发票", "涉及流水"])
        for r in report["reversed_matches"]:
            w.writerow([r["match_id"], r["match_type"], r["matched_at"],
                        r["reversed_at"], r["reversed_reason"],
                        ", ".join(r["invoice_nos"]), ", ".join(r["txn_ids"])])
    files["reversed_matches"] = rev_path

    return files
