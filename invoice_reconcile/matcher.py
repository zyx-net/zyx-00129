from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any, Optional

from .session import Session, SessionManager, Invoice, BankTransaction, MatchRecord, _uuid
from .config import Config
from .closeout import check_session_closed


def _parse_date_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def _dates_close(d1: str, d2: str, days: int) -> bool:
    a = _parse_date_iso(d1)
    b = _parse_date_iso(d2)
    return abs((a - b).days) <= days


def _amounts_close(a: float, b: float, tol: float) -> bool:
    return abs(round(a, 2) - round(b, 2)) <= tol + 1e-9


def auto_match(session: Session, sm: SessionManager, config: Config) -> Dict[str, Any]:
    closed_check = check_session_closed(session, "自动匹配")
    if not closed_check["allowed"]:
        return {
            "success": False,
            "error": closed_check["error"],
            "matches_count": 0,
            "invoices_matched": 0,
            "transactions_matched": 0,
            "matches": [],
        }

    tol_amount = config.amount_tolerance
    tol_days = config.date_tolerance_days

    unmatched_invs = [inv for inv in session.invoices.values()
                      if inv.status == "unmatched" and not inv.suspended]
    unmatched_txns = [txn for txn in session.transactions.values()
                      if txn.status == "unmatched" and not txn.suspended]

    inv_by_customer: Dict[str, List[Invoice]] = {}
    txn_by_customer: Dict[str, List[BankTransaction]] = {}

    for inv in unmatched_invs:
        key = config.resolve_customer_name(inv.customer_name).lower()
        inv_by_customer.setdefault(key, []).append(inv)
    for txn in unmatched_txns:
        key = config.resolve_customer_name(txn.counterparty).lower()
        txn_by_customer.setdefault(key, []).append(txn)

    used_invoice_ids: set = set()
    used_txn_ids: set = set()
    matches_made: List[MatchRecord] = []

    # --- 规则1: 一对一精确匹配 (客户名 + 金额精确 + 日期在容差内) ---
    for cust, cust_invs in inv_by_customer.items():
        cust_txns = txn_by_customer.get(cust, [])
        if not cust_txns:
            continue
        for inv in list(cust_invs):
            if inv.id in used_invoice_ids:
                continue
            for txn in list(cust_txns):
                if txn.id in used_txn_ids:
                    continue
                if (_amounts_close(inv.amount, txn.amount, tol_amount)
                        and _dates_close(inv.invoice_date, txn.txn_date, tol_days)):
                    match = _create_match(session, sm, [inv], [txn], "auto",
                                          f"一对一匹配: 客户={cust}, 金额差={abs(inv.amount-txn.amount):.4f}, 日期差={abs(_parse_date_iso(inv.invoice_date)-_parse_date_iso(txn.txn_date)).days}天")
                    matches_made.append(match)
                    used_invoice_ids.add(inv.id)
                    used_txn_ids.add(txn.id)
                    inv.status = "matched"
                    txn.status = "matched"
                    cust_invs.remove(inv)
                    cust_txns.remove(txn)
                    break

    # --- 规则2: 一对多精确匹配 (同客户多发票合并 = 一笔流水, 总金额精确, 所有日期在容差内) ---
    for cust, cust_invs in inv_by_customer.items():
        cust_txns = txn_by_customer.get(cust, [])
        remaining_invs = [i for i in cust_invs if i.id not in used_invoice_ids]
        remaining_txns = [t for t in cust_txns if t.id not in used_txn_ids]
        if len(remaining_invs) < 2 or not remaining_txns:
            continue
        result = _find_exact_subset_sum(
            remaining_invs, remaining_txns,
            key_amount=lambda i: i.amount,
            tol=tol_amount,
            max_items=min(len(remaining_invs), 8),
            # 日期过滤：每一张发票的开票日期必须与流水日期在容差之内
            combination_filter=lambda group, t: all(
                _dates_close(i.invoice_date, t.txn_date, tol_days) for i in group
            ),
        )
        if result:
            invs_group, txn = result
            sum_amount = sum(i.amount for i in invs_group)
            match = _create_match(session, sm, list(invs_group), [txn], "auto",
                                  f"多对一匹配: {len(invs_group)}张发票合并, 总金额={sum_amount:.2f}, 流水金额={txn.amount:.2f}, 差额={abs(sum_amount-txn.amount):.4f}")
            matches_made.append(match)
            for i in invs_group:
                i.status = "matched"
                used_invoice_ids.add(i.id)
            txn.status = "matched"
            used_txn_ids.add(txn.id)

    # --- 规则3: 一对多精确匹配 (同客户多流水合并 = 一张发票, 总金额精确) ---
    for cust, cust_invs in inv_by_customer.items():
        cust_txns = txn_by_customer.get(cust, [])
        remaining_invs = [i for i in cust_invs if i.id not in used_invoice_ids]
        remaining_txns = [t for t in cust_txns if t.id not in used_txn_ids]
        if not remaining_invs or len(remaining_txns) < 2:
            continue
        result = _find_exact_subset_sum(
            remaining_txns, remaining_invs,
            key_amount=lambda t: t.amount,
            tol=tol_amount,
            max_items=min(len(remaining_txns), 8),
            # 日期过滤：每一笔流水的交易日期必须与发票日期在容差之内
            combination_filter=lambda group, i: all(
                _dates_close(i.invoice_date, t.txn_date, tol_days) for t in group
            ),
        )
        if result:
            txns_group, inv = result
            sum_amount = sum(t.amount for t in txns_group)
            match = _create_match(session, sm, [inv], list(txns_group), "auto",
                                  f"一对多匹配: {len(txns_group)}笔流水合并, 总金额={sum_amount:.2f}, 发票金额={inv.amount:.2f}, 差额={abs(sum_amount-inv.amount):.4f}")
            matches_made.append(match)
            for t in txns_group:
                t.status = "matched"
                used_txn_ids.add(t.id)
            inv.status = "matched"
            used_invoice_ids.add(inv.id)

    # --- 超出容差的一对多不匹配，保持未解决 ---

    sm.add_history(
        session, "auto_match",
        matched_count=len(matches_made),
        invoice_ids_used=sorted(used_invoice_ids),
        transaction_ids_used=sorted(used_txn_ids),
        config={
            "amount_tolerance": tol_amount,
            "date_tolerance_days": tol_days,
        },
    )

    return {
        "matches_count": len(matches_made),
        "matches": matches_made,
        "invoices_matched": len(used_invoice_ids),
        "transactions_matched": len(used_txn_ids),
    }


def _create_match(session: Session, sm: SessionManager,
                  invoices: List[Invoice], transactions: List[BankTransaction],
                  match_type: str, notes: str = "") -> MatchRecord:
    match = MatchRecord(
        id=_uuid(),
        invoice_ids=[i.id for i in invoices],
        transaction_ids=[t.id for t in transactions],
        match_type=match_type,
        notes=notes,
    )
    session.matches[match.id] = match
    for i in invoices:
        i.status = "matched"
    for t in transactions:
        t.status = "matched"
    return match


def _find_exact_subset_sum(items, targets, key_amount, tol, max_items, combination_filter=None):
    """在 items 中找一个子集，其金额和等于某个 target。
    combination_filter(subset, target) -> bool：
        提供时用于在金额命中后二次校验（如日期容差），不合格则 continue 继续寻找下一个组合，
        而不是直接返回，保证首个命中但日期不合格的组合不会阻断后续合格组合。
    找不到返回 None。
    """
    from itertools import combinations
    for r in range(2, max_items + 1):
        for subset in combinations(items, r):
            s = sum(key_amount(x) for x in subset)
            for t in targets:
                t_amt = t.amount if hasattr(t, "amount") else key_amount(t)
                if _amounts_close(s, t_amt, tol):
                    # 若有自定义过滤且不通过，继续找下一个组合
                    if combination_filter is not None and not combination_filter(list(subset), t):
                        continue
                    return (list(subset), t)
    return None
