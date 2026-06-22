from typing import List, Dict, Any, Optional
from datetime import datetime

from .session import Session, SessionManager, Invoice, BankTransaction, MatchRecord, _uuid, _now_iso
from .matcher import _create_match
from .closeout import check_session_closed


def _find_invoice(session: Session, identifier: str) -> Optional[Invoice]:
    """按ID或发票号查找发票"""
    if identifier in session.invoices:
        return session.invoices[identifier]
    for inv in session.invoices.values():
        if inv.invoice_no == identifier:
            return inv
    return None


def _find_transaction(session: Session, identifier: str) -> Optional[BankTransaction]:
    """按ID或交易编号查找流水"""
    if identifier in session.transactions:
        return session.transactions[identifier]
    for txn in session.transactions.values():
        if txn.txn_id == identifier:
            return txn
    return None


def manual_match(session: Session, sm: SessionManager,
                 invoice_ids: List[str], transaction_ids: List[str],
                 notes: str = "") -> Dict[str, Any]:
    """人工匹配发票和流水"""
    closed_check = check_session_closed(session, "人工匹配")
    if not closed_check["allowed"]:
        return {"success": False, "error": closed_check["error"], "match": None}

    invoices = []
    not_found = []
    for iid in invoice_ids:
        inv = _find_invoice(session, iid)
        if inv is None:
            not_found.append(f"发票 {iid}")
        elif inv.status == "matched" and not _is_matched_reversed(session, inv.id, "invoice"):
            return {
                "success": False,
                "error": f"发票 {inv.invoice_no} (ID:{inv.id}) 已匹配且未撤销，请先撤销原匹配",
                "match": None,
            }
        elif inv.suspended:
            return {
                "success": False,
                "error": f"发票 {inv.invoice_no} (ID:{inv.id}) 已挂起，请先取消挂起",
                "match": None,
            }
        else:
            invoices.append(inv)

    transactions = []
    for tid in transaction_ids:
        txn = _find_transaction(session, tid)
        if txn is None:
            not_found.append(f"流水 {tid}")
        elif txn.status == "matched" and not _is_matched_reversed(session, txn.id, "transaction"):
            return {
                "success": False,
                "error": f"流水 {txn.txn_id} (ID:{txn.id}) 已匹配且未撤销，请先撤销原匹配",
                "match": None,
            }
        elif txn.suspended:
            return {
                "success": False,
                "error": f"流水 {txn.txn_id} (ID:{txn.id}) 已挂起，请先取消挂起",
                "match": None,
            }
        else:
            transactions.append(txn)

    if not_found:
        return {"success": False, "error": "未找到以下记录: " + ", ".join(not_found), "match": None}
    if not invoices or not transactions:
        return {"success": False, "error": "发票和流水都不能为空", "match": None}

    inv_sum = sum(i.amount for i in invoices)
    txn_sum = sum(t.amount for t in transactions)

    match = _create_match(session, sm, invoices, transactions, "manual", notes or f"人工匹配: {len(invoices)}张发票-{len(transactions)}笔流水, 发票合计{inv_sum:.2f}, 流水合计{txn_sum:.2f}, 差额{abs(inv_sum-txn_sum):.2f}")
    sm.add_history(
        session, "manual_match",
        match_id=match.id,
        invoice_ids=[i.id for i in invoices],
        transaction_ids=[t.id for t in transactions],
        invoice_nos=[i.invoice_no for i in invoices],
        txn_ids=[t.txn_id for t in transactions],
        invoice_amount_sum=round(inv_sum, 2),
        transaction_amount_sum=round(txn_sum, 2),
        diff_amount=round(abs(inv_sum - txn_sum), 2),
        notes=notes,
    )
    return {"success": True, "match": match, "error": None}


def _is_matched_reversed(session: Session, obj_id: str, obj_type: str) -> bool:
    """检查对象所在的所有匹配是否都已被撤销"""
    for m in session.matches.values():
        ids_list = m.invoice_ids if obj_type == "invoice" else m.transaction_ids
        if obj_id in ids_list and not m.reversed:
            return False
    return True


def suspend_invoice(session: Session, sm: SessionManager, invoice_id: str, reason: str = "") -> Dict[str, Any]:
    closed_check = check_session_closed(session, "挂起发票")
    if not closed_check["allowed"]:
        return {"success": False, "error": closed_check["error"]}

    inv = _find_invoice(session, invoice_id)
    if inv is None:
        return {"success": False, "error": f"未找到发票: {invoice_id}"}
    if inv.status == "matched" and not _is_matched_reversed(session, inv.id, "invoice"):
        return {"success": False, "error": f"发票 {inv.invoice_no} 已匹配，请先撤销匹配后再挂起"}
    inv.suspended = True
    inv.suspend_reason = reason or "人工挂起"
    sm.add_history(
        session, "suspend_invoice",
        invoice_id=inv.id,
        invoice_no=inv.invoice_no,
        reason=inv.suspend_reason,
    )
    return {"success": True, "invoice": inv}


def suspend_transaction(session: Session, sm: SessionManager, txn_id: str, reason: str = "") -> Dict[str, Any]:
    closed_check = check_session_closed(session, "挂起流水")
    if not closed_check["allowed"]:
        return {"success": False, "error": closed_check["error"]}

    txn = _find_transaction(session, txn_id)
    if txn is None:
        return {"success": False, "error": f"未找到流水: {txn_id}"}
    if txn.status == "matched" and not _is_matched_reversed(session, txn.id, "transaction"):
        return {"success": False, "error": f"流水 {txn.txn_id} 已匹配，请先撤销匹配后再挂起"}
    txn.suspended = True
    txn.suspend_reason = reason or "人工挂起"
    sm.add_history(
        session, "suspend_transaction",
        txn_id=txn.id,
        txn_no=txn.txn_id,
        reason=txn.suspend_reason,
    )
    return {"success": True, "transaction": txn}


def unsuspend_invoice(session: Session, sm: SessionManager, invoice_id: str) -> Dict[str, Any]:
    inv = _find_invoice(session, invoice_id)
    if inv is None:
        return {"success": False, "error": f"未找到发票: {invoice_id}"}
    if not inv.suspended:
        return {"success": False, "error": f"发票 {inv.invoice_no} 未挂起"}
    inv.suspended = False
    old_reason = inv.suspend_reason
    inv.suspend_reason = ""
    sm.add_history(
        session, "unsuspend_invoice",
        invoice_id=inv.id,
        invoice_no=inv.invoice_no,
        previous_reason=old_reason,
    )
    return {"success": True, "invoice": inv}


def unsuspend_transaction(session: Session, sm: SessionManager, txn_id: str) -> Dict[str, Any]:
    txn = _find_transaction(session, txn_id)
    if txn is None:
        return {"success": False, "error": f"未找到流水: {txn_id}"}
    if not txn.suspended:
        return {"success": False, "error": f"流水 {txn.txn_id} 未挂起"}
    txn.suspended = False
    old_reason = txn.suspend_reason
    txn.suspend_reason = ""
    sm.add_history(
        session, "unsuspend_transaction",
        txn_id=txn.id,
        txn_no=txn.txn_id,
        previous_reason=old_reason,
    )
    return {"success": True, "transaction": txn}


def reverse_match(session: Session, sm: SessionManager, match_id: str, reason: str = "") -> Dict[str, Any]:
    closed_check = check_session_closed(session, "撤销匹配")
    if not closed_check["allowed"]:
        return {"success": False, "error": closed_check["error"], "match": None}

    if match_id not in session.matches:
        return {"success": False, "error": f"未找到匹配记录: {match_id}"}
    match = session.matches[match_id]
    if match.reversed:
        return {"success": False, "error": f"匹配记录 {match_id} 已撤销，不能重复撤销"}

    match.reversed = True
    match.reversed_at = _now_iso()
    match.reversed_reason = reason or "人工撤销"

    for iid in match.invoice_ids:
        if iid in session.invoices:
            inv = session.invoices[iid]
            if _is_matched_reversed(session, iid, "invoice"):
                inv.status = "unmatched"

    for tid in match.transaction_ids:
        if tid in session.transactions:
            txn = session.transactions[tid]
            if _is_matched_reversed(session, tid, "transaction"):
                txn.status = "unmatched"

    sm.add_history(
        session, "reverse_match",
        match_id=match.id,
        match_type=match.match_type,
        invoice_ids=match.invoice_ids,
        transaction_ids=match.transaction_ids,
        reason=match.reversed_reason,
    )
    return {"success": True, "match": match}


def list_unmatched(session: Session, include_suspended: bool = False) -> Dict[str, List[Any]]:
    return {
        "invoices": [
            {
                "id": i.id,
                "invoice_no": i.invoice_no,
                "customer_name": i.customer_name,
                "amount": i.amount,
                "invoice_date": i.invoice_date,
                "status": i.status,
                "suspended": i.suspended,
                "suspend_reason": i.suspend_reason,
                "source_file": i.source_file,
                "source_row": i.source_row,
            }
            for i in session.invoices.values()
            if (i.status == "unmatched" and (include_suspended or not i.suspended))
        ],
        "transactions": [
            {
                "id": t.id,
                "txn_id": t.txn_id,
                "counterparty": t.counterparty,
                "amount": t.amount,
                "txn_date": t.txn_date,
                "status": t.status,
                "suspended": t.suspended,
                "suspend_reason": t.suspend_reason,
                "source_file": t.source_file,
                "source_row": t.source_row,
            }
            for t in session.transactions.values()
            if (t.status == "unmatched" and (include_suspended or not t.suspended))
        ],
    }


def list_history(session: Session, limit: int = 50, action_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    items = list(reversed(session.history))
    if action_filter:
        items = [h for h in items if h.action == action_filter]
    return [
        {
            "id": h.id,
            "action": h.action,
            "timestamp": h.timestamp,
            "details": h.details,
        }
        for h in items[:limit]
    ]
