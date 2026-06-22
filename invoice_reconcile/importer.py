import csv
import os
from datetime import datetime
from typing import List, Dict, Tuple, Any
from decimal import Decimal, InvalidOperation

from .session import Session, SessionManager, Invoice, BankTransaction, _uuid
from .closeout import check_session_closed


DATE_FORMATS = [
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
]

INVOICE_COLUMN_ALIASES = {
    "invoice_no": ["发票号", "发票编号", "发票号码", "invoice_no", "invoice_number", "no", "编号", "票号"],
    "customer_name": ["客户名称", "客户", "购货方", "购方名称", "customer_name", "customer", "client", "buyer", "购买方"],
    "amount": ["金额", "价税合计", "含税金额", "总金额", "amount", "total", "total_amount", "价税合计金额"],
    "invoice_date": ["开票日期", "日期", "填开日期", "invoice_date", "date", "issue_date"],
}

TXN_COLUMN_ALIASES = {
    "txn_id": ["交易编号", "流水号", "交易号", "交易流水号", "txn_id", "transaction_id", "id", "no", "编号"],
    "counterparty": ["对方单位", "对方户名", "对方名称", "付款方", "收款方", "counterparty", "payer", "payee", "name", "名称", "对方账户名"],
    "amount": ["金额", "交易金额", "发生额", "amount", "transaction_amount", "total"],
    "txn_date": ["交易日期", "日期", "记账日期", "到账日期", "txn_date", "date", "transaction_date", "入账日期"],
}


class ImportError(Exception):
    def __init__(self, message: str, errors: List[str] = None):
        super().__init__(message)
        self.errors = errors or []


def _parse_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("日期为空")
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"无法识别的日期格式: {value}")


def _parse_amount(value: str) -> float:
    value = (value or "").strip()
    if not value:
        raise ValueError("金额为空")
    cleaned = value.replace(",", "").replace("¥", "").replace("￥", "").replace(" ", "")
    try:
        d = Decimal(cleaned)
        return float(round(d, 2))
    except (InvalidOperation, ValueError):
        raise ValueError(f"无法识别的金额格式: {value}")


def _map_headers(headers: List[str], aliases: Dict[str, List[str]]) -> Dict[str, str]:
    mapping = {}
    header_lookup = {h.strip().lower(): h for h in headers}
    for canonical, alias_list in aliases.items():
        for alias in alias_list:
            key = alias.strip().lower()
            if key in header_lookup:
                mapping[canonical] = header_lookup[key]
                break
    return mapping


def _detect_encoding(filepath: str) -> str:
    for enc in ["utf-8-sig", "utf-8", "gbk", "gb18030"]:
        try:
            with open(filepath, "r", encoding=enc) as f:
                f.read(4096)
            return enc
        except UnicodeDecodeError:
            continue
    return "utf-8"


def import_invoices(session: Session, sm: SessionManager, filepath: str) -> Dict[str, Any]:
    closed_check = check_session_closed(session, "导入发票")
    if not closed_check["allowed"]:
        raise ImportError(closed_check["error"])

    filepath = os.path.abspath(filepath)
    if not os.path.exists(filepath):
        raise ImportError(f"文件不存在: {filepath}")

    if sm.is_file_imported(session, filepath):
        fh = sm.file_hash(filepath)
        raise ImportError(
            f"文件已导入，不能重复入账。\n"
            f"  文件: {os.path.basename(filepath)}\n"
            f"  标识: {fh[:16]}...\n"
            f"  首次导入: {session.imported_files.get(fh, 'unknown')}"
        )

    encoding = _detect_encoding(filepath)
    errors: List[str] = []
    imported: List[Invoice] = []

    with open(filepath, "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        raise ImportError("CSV 文件为空")

    headers = rows[0]
    mapping = _map_headers(headers, INVOICE_COLUMN_ALIASES)

    required = ["invoice_no", "customer_name", "amount", "invoice_date"]
    missing = [c for c in required if c not in mapping]
    if missing:
        hint = ", ".join(missing)
        alias_hint = {m: INVOICE_COLUMN_ALIASES[m] for m in missing}
        raise ImportError(
            f"缺少必要列: {hint}\n"
            f"  已检测表头: {headers}\n"
            f"  需要的列及别名: {alias_hint}"
        )

    for idx, row in enumerate(rows[1:], start=2):
        line_num = idx
        try:
            if not any(cell.strip() for cell in row):
                continue
            while len(row) < len(headers):
                row.append("")
            row_map = dict(zip(headers, row))
            invoice_no = row_map.get(mapping["invoice_no"], "").strip()
            customer_name = row_map.get(mapping["customer_name"], "").strip()
            raw_amount = row_map.get(mapping["amount"], "")
            raw_date = row_map.get(mapping["invoice_date"], "")

            if not invoice_no:
                raise ValueError("发票号为空")
            if not customer_name:
                raise ValueError("客户名称为空")
            amount = _parse_amount(raw_amount)
            if amount <= 0:
                raise ValueError(f"金额必须为正数: {raw_amount}")
            invoice_date = _parse_date(raw_date)

            inv = Invoice(
                id=_uuid(),
                invoice_no=invoice_no,
                customer_name=customer_name,
                amount=amount,
                invoice_date=invoice_date,
                source_file=os.path.basename(filepath),
                source_row=line_num,
            )
            imported.append(inv)

        except ValueError as e:
            errors.append(f"第{line_num}行: {str(e)} | 行内容: {row}")

    if errors:
        raise ImportError(f"导入失败，共 {len(errors)} 处错误:", errors=errors)

    for inv in imported:
        session.invoices[inv.id] = inv
    sm.mark_file_imported(session, filepath)
    sm.add_history(
        session, "import_invoices",
        file=os.path.basename(filepath),
        count=len(imported),
        encoding=encoding,
    )
    return {
        "count": len(imported),
        "invoices": imported,
        "errors": [],
    }


def import_transactions(session: Session, sm: SessionManager, filepath: str) -> Dict[str, Any]:
    closed_check = check_session_closed(session, "导入流水")
    if not closed_check["allowed"]:
        raise ImportError(closed_check["error"])

    filepath = os.path.abspath(filepath)
    if not os.path.exists(filepath):
        raise ImportError(f"文件不存在: {filepath}")

    if sm.is_file_imported(session, filepath):
        fh = sm.file_hash(filepath)
        raise ImportError(
            f"文件已导入，不能重复入账。\n"
            f"  文件: {os.path.basename(filepath)}\n"
            f"  标识: {fh[:16]}...\n"
            f"  首次导入: {session.imported_files.get(fh, 'unknown')}"
        )

    encoding = _detect_encoding(filepath)
    errors: List[str] = []
    imported: List[BankTransaction] = []

    with open(filepath, "r", encoding=encoding, newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)

    if not rows:
        raise ImportError("CSV 文件为空")

    headers = rows[0]
    mapping = _map_headers(headers, TXN_COLUMN_ALIASES)

    required = ["txn_id", "counterparty", "amount", "txn_date"]
    missing = [c for c in required if c not in mapping]
    if missing:
        hint = ", ".join(missing)
        alias_hint = {m: TXN_COLUMN_ALIASES[m] for m in missing}
        raise ImportError(
            f"缺少必要列: {hint}\n"
            f"  已检测表头: {headers}\n"
            f"  需要的列及别名: {alias_hint}"
        )

    for idx, row in enumerate(rows[1:], start=2):
        line_num = idx
        try:
            if not any(cell.strip() for cell in row):
                continue
            while len(row) < len(headers):
                row.append("")
            row_map = dict(zip(headers, row))
            txn_id = row_map.get(mapping["txn_id"], "").strip()
            counterparty = row_map.get(mapping["counterparty"], "").strip()
            raw_amount = row_map.get(mapping["amount"], "")
            raw_date = row_map.get(mapping["txn_date"], "")

            if not txn_id:
                raise ValueError("交易编号为空")
            if not counterparty:
                raise ValueError("对方单位为空")
            amount = _parse_amount(raw_amount)
            if amount <= 0:
                raise ValueError(f"金额必须为正数: {raw_amount}")
            txn_date = _parse_date(raw_date)

            txn = BankTransaction(
                id=_uuid(),
                txn_id=txn_id,
                counterparty=counterparty,
                amount=amount,
                txn_date=txn_date,
                source_file=os.path.basename(filepath),
                source_row=line_num,
            )
            imported.append(txn)

        except ValueError as e:
            errors.append(f"第{line_num}行: {str(e)} | 行内容: {row}")

    if errors:
        raise ImportError(f"导入失败，共 {len(errors)} 处错误:", errors=errors)

    for txn in imported:
        session.transactions[txn.id] = txn
    sm.mark_file_imported(session, filepath)
    sm.add_history(
        session, "import_transactions",
        file=os.path.basename(filepath),
        count=len(imported),
        encoding=encoding,
    )
    return {
        "count": len(imported),
        "transactions": imported,
        "errors": [],
    }
