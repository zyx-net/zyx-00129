from typing import Dict, List, Any, Optional
from dataclasses import asdict
import json
from pathlib import Path

from .session import Session, SessionManager, CloseRecord, _uuid, _now_iso


def check_close_ready(session: Session) -> Dict[str, Any]:
    """检查会话是否可以关账，返回检查结果"""
    issues = []
    warnings = []

    inv_unmatched = sum(1 for i in session.invoices.values() if i.status == "unmatched" and not i.suspended)
    txn_unmatched = sum(1 for t in session.transactions.values() if t.status == "unmatched" and not t.suspended)
    inv_suspended = sum(1 for i in session.invoices.values() if i.suspended)
    txn_suspended = sum(1 for t in session.transactions.values() if t.suspended)
    reversed_unreviewed = sum(1 for m in session.matches.values() if m.reversed and not m.notes.startswith("[已复核]"))

    if inv_unmatched > 0:
        issues.append(f"存在 {inv_unmatched} 张未匹配发票")
    if txn_unmatched > 0:
        issues.append(f"存在 {txn_unmatched} 笔未匹配流水")
    if inv_suspended > 0:
        issues.append(f"存在 {inv_suspended} 张挂起发票")
    if txn_suspended > 0:
        issues.append(f"存在 {txn_suspended} 笔挂起流水")
    if reversed_unreviewed > 0:
        issues.append(f"存在 {reversed_unreviewed} 条已撤销未复核的匹配记录")

    if session.is_closed:
        warnings.append("会话已处于关账状态")

    return {
        "can_close": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "stats": {
            "invoices_unmatched": inv_unmatched,
            "transactions_unmatched": txn_unmatched,
            "invoices_suspended": inv_suspended,
            "transactions_suspended": txn_suspended,
            "reversed_unreviewed": reversed_unreviewed,
        },
    }


def _build_close_summary(session: Session, sm: SessionManager) -> Dict[str, Any]:
    """构建关账时的汇总快照"""
    base_summary = sm.status_summary(session)

    invoice_summary = {
        "total": base_summary["invoices"]["total"],
        "matched": base_summary["invoices"]["matched"],
        "unmatched": base_summary["invoices"]["unmatched"],
        "suspended": base_summary["invoices"]["suspended"],
        "amount_total": base_summary["invoices"]["amount_total"],
        "amount_matched": base_summary["invoices"]["amount_matched"],
        "by_customer": {},
    }
    for inv in session.invoices.values():
        cust = inv.customer_name
        if cust not in invoice_summary["by_customer"]:
            invoice_summary["by_customer"][cust] = {"count": 0, "amount": 0.0, "matched_count": 0, "matched_amount": 0.0}
        invoice_summary["by_customer"][cust]["count"] += 1
        invoice_summary["by_customer"][cust]["amount"] += inv.amount
        if inv.status == "matched":
            invoice_summary["by_customer"][cust]["matched_count"] += 1
            invoice_summary["by_customer"][cust]["matched_amount"] += inv.amount

    transaction_summary = {
        "total": base_summary["transactions"]["total"],
        "matched": base_summary["transactions"]["matched"],
        "unmatched": base_summary["transactions"]["unmatched"],
        "suspended": base_summary["transactions"]["suspended"],
        "amount_total": base_summary["transactions"]["amount_total"],
        "amount_matched": base_summary["transactions"]["amount_matched"],
        "by_counterparty": {},
    }
    for txn in session.transactions.values():
        cp = txn.counterparty
        if cp not in transaction_summary["by_counterparty"]:
            transaction_summary["by_counterparty"][cp] = {"count": 0, "amount": 0.0, "matched_count": 0, "matched_amount": 0.0}
        transaction_summary["by_counterparty"][cp]["count"] += 1
        transaction_summary["by_counterparty"][cp]["amount"] += txn.amount
        if txn.status == "matched":
            transaction_summary["by_counterparty"][cp]["matched_count"] += 1
            transaction_summary["by_counterparty"][cp]["matched_amount"] += txn.amount

    match_summary = {
        "total": base_summary["matches"]["total"],
        "active": base_summary["matches"]["active"],
        "reversed": base_summary["matches"]["reversed"],
        "by_type": {},
    }
    for m in session.matches.values():
        mt = m.match_type
        if mt not in match_summary["by_type"]:
            match_summary["by_type"][mt] = {"active": 0, "reversed": 0}
        if m.reversed:
            match_summary["by_type"][mt]["reversed"] += 1
        else:
            match_summary["by_type"][mt]["active"] += 1

    unmatched_details = {
        "invoices": [
            {
                "id": i.id,
                "invoice_no": i.invoice_no,
                "customer_name": i.customer_name,
                "amount": round(i.amount, 2),
                "invoice_date": i.invoice_date,
                "suspended": i.suspended,
                "suspend_reason": i.suspend_reason,
            }
            for i in session.invoices.values()
            if i.status == "unmatched"
        ],
        "transactions": [
            {
                "id": t.id,
                "txn_id": t.txn_id,
                "counterparty": t.counterparty,
                "amount": round(t.amount, 2),
                "txn_date": t.txn_date,
                "suspended": t.suspended,
                "suspend_reason": t.suspend_reason,
            }
            for t in session.transactions.values()
            if t.status == "unmatched"
        ],
    }

    return {
        "base_summary": base_summary,
        "invoices": invoice_summary,
        "transactions": transaction_summary,
        "matches": match_summary,
        "unmatched_details": unmatched_details,
        "imported_files": list(session.imported_files.values()),
        "history_count": len(session.history),
    }


def close_session(
    session: Session,
    sm: SessionManager,
    closed_by: str,
    notes: str = "",
    force: bool = False,
    force_reason: str = "",
) -> Dict[str, Any]:
    """执行关账操作"""
    if session.is_closed:
        return {
            "success": False,
            "error": "会话已处于关账状态，如需重新关账请先解账",
            "record": None,
        }

    check_result = check_close_ready(session)

    if not force and not check_result["can_close"]:
        return {
            "success": False,
            "error": "关账检查未通过，存在待处理项：\n  - " + "\n  - ".join(check_result["issues"]) +
                     "\n如需强制关账，请使用 --force 参数并说明原因",
            "check_result": check_result,
            "record": None,
        }

    summary = _build_close_summary(session, sm)
    summary["force"] = force
    summary["force_reason"] = force_reason
    summary["check_result"] = check_result

    record = CloseRecord(
        id=f"close_{_uuid()}",
        closed_at=_now_iso(),
        closed_by=closed_by,
        notes=notes,
        summary=summary,
    )

    session.close_records.append(record)
    session.is_closed = True

    sm.add_history(
        session, "close_session",
        close_id=record.id,
        closed_by=closed_by,
        notes=notes,
        force=force,
        force_reason=force_reason,
        check_result=check_result,
        summary_snapshot=summary,
    )

    return {
        "success": True,
        "record": record,
        "check_result": check_result,
        "force_used": force,
    }


def unclose_session(
    session: Session,
    sm: SessionManager,
    unclosed_by: str,
    reason: str,
) -> Dict[str, Any]:
    """执行解账操作"""
    if not session.is_closed:
        return {
            "success": False,
            "error": "会话未处于关账状态，无需解账",
            "record": None,
        }

    if not session.close_records:
        return {
            "success": False,
            "error": "未找到关账记录",
            "record": None,
        }

    latest = session.close_records[-1]
    if latest.is_unclosed:
        return {
            "success": False,
            "error": "最新关账记录已处于解账状态",
            "record": None,
        }

    latest.is_unclosed = True
    latest.unclosed_at = _now_iso()
    latest.unclosed_by = unclosed_by
    latest.unclose_reason = reason

    session.is_closed = False

    sm.add_history(
        session, "unclose_session",
        close_id=latest.id,
        unclosed_by=unclosed_by,
        reason=reason,
        closed_at=latest.closed_at,
        closed_by=latest.closed_by,
    )

    return {
        "success": True,
        "record": latest,
    }


def get_close_records(session: Session) -> List[Dict[str, Any]]:
    """获取所有关账记录"""
    return [cr.to_dict() for cr in session.close_records]


def export_close_summary(
    session: Session,
    output_path: str,
    close_id: Optional[str] = None,
) -> Dict[str, Any]:
    """导出关账摘要为JSON文件"""
    if close_id:
        record = next((cr for cr in session.close_records if cr.id == close_id), None)
        if not record:
            return {"success": False, "error": f"未找到关账记录: {close_id}"}
        summary_data = record.to_dict()
    else:
        if not session.close_records:
            return {"success": False, "error": "会话没有关账记录"}
        summary_data = {
            "session_name": session.name,
            "session_id": session.session_id,
            "is_currently_closed": session.is_closed,
            "close_records": [cr.to_dict() for cr in session.close_records],
        }

    path = Path(output_path)
    if not path.name.endswith(".json"):
        path = path.with_suffix(".json")

    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary_data, f, ensure_ascii=False, indent=2)

    return {
        "success": True,
        "output_path": str(path),
        "close_count": len(session.close_records),
    }


def check_session_closed(session: Session, action: str) -> Dict[str, Any]:
    """检查会话是否已关账，用于在执行操作前拦截"""
    if session.is_closed:
        return {
            "allowed": False,
            "error": f"会话已关账，禁止执行「{action}」操作。如需继续，请先执行 `irec unclose` 解账并说明原因。",
        }
    return {"allowed": True}
