import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any
from copy import deepcopy


def _uuid() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Invoice:
    id: str
    invoice_no: str
    customer_name: str
    amount: float
    invoice_date: str
    source_file: str
    source_row: int
    status: str = "unmatched"
    suspended: bool = False
    suspend_reason: str = ""
    created_at: str = field(default_factory=_now_iso)


@dataclass
class BankTransaction:
    id: str
    txn_id: str
    counterparty: str
    amount: float
    txn_date: str
    source_file: str
    source_row: int
    status: str = "unmatched"
    suspended: bool = False
    suspend_reason: str = ""
    created_at: str = field(default_factory=_now_iso)


@dataclass
class MatchRecord:
    id: str
    invoice_ids: List[str]
    transaction_ids: List[str]
    match_type: str
    matched_at: str = field(default_factory=_now_iso)
    notes: str = ""
    reversed: bool = False
    reversed_at: str = ""
    reversed_reason: str = ""


@dataclass
class HistoryEntry:
    id: str
    action: str
    timestamp: str = field(default_factory=_now_iso)
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    session_id: str
    name: str
    created_at: str
    updated_at: str
    invoices: Dict[str, Invoice] = field(default_factory=dict)
    transactions: Dict[str, BankTransaction] = field(default_factory=dict)
    matches: Dict[str, MatchRecord] = field(default_factory=dict)
    history: List[HistoryEntry] = field(default_factory=list)
    imported_files: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "name": self.name,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "invoices": {k: asdict(v) for k, v in self.invoices.items()},
            "transactions": {k: asdict(v) for k, v in self.transactions.items()},
            "matches": {k: asdict(v) for k, v in self.matches.items()},
            "history": [asdict(h) for h in self.history],
            "imported_files": self.imported_files,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        session = cls(
            session_id=data["session_id"],
            name=data["name"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )
        for k, v in data.get("invoices", {}).items():
            session.invoices[k] = Invoice(**v)
        for k, v in data.get("transactions", {}).items():
            session.transactions[k] = BankTransaction(**v)
        for k, v in data.get("matches", {}).items():
            session.matches[k] = MatchRecord(**v)
        for h in data.get("history", []):
            session.history.append(HistoryEntry(**h))
        session.imported_files = data.get("imported_files", {})
        return session


class SessionManager:
    def __init__(self, session_dir: str, default_session: str = "default"):
        self.session_dir = Path(session_dir).resolve()
        self.default_session_name = default_session
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def _session_path(self, name: str) -> Path:
        return self.session_dir / f"{name}.json"

    def create(self, name: Optional[str] = None) -> Session:
        name = name or self.default_session_name
        path = self._session_path(name)
        if path.exists():
            raise FileExistsError(f"会话 '{name}' 已存在，使用 load 加载或 switch 切换")
        session = Session(
            session_id=_uuid(),
            name=name,
            created_at=_now_iso(),
            updated_at=_now_iso(),
        )
        self._save(session)
        return session

    def load(self, name: Optional[str] = None) -> Session:
        name = name or self.default_session_name
        path = self._session_path(name)
        if not path.exists():
            raise FileNotFoundError(f"会话 '{name}' 不存在，使用 create 创建")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return Session.from_dict(data)

    def save(self, session: Session) -> None:
        session.updated_at = _now_iso()
        self._save(session)

    def _save(self, session: Session) -> None:
        path = self._session_path(session.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)

    def list_sessions(self) -> List[Dict[str, str]]:
        sessions = []
        for p in sorted(self.session_dir.glob("*.json")):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                sessions.append({
                    "name": p.stem,
                    "session_id": data.get("session_id", "?"),
                    "created_at": data.get("created_at", "?"),
                    "updated_at": data.get("updated_at", "?"),
                })
            except Exception:
                sessions.append({"name": p.stem, "session_id": "corrupt", "created_at": "-", "updated_at": "-"})
        return sessions

    def delete(self, name: str) -> None:
        path = self._session_path(name)
        if not path.exists():
            raise FileNotFoundError(f"会话 '{name}' 不存在")
        path.unlink()

    @staticmethod
    def file_hash(filepath: str) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        h.update(os.path.basename(filepath).encode("utf-8"))
        return h.hexdigest()

    def is_file_imported(self, session: Session, filepath: str) -> bool:
        fh = self.file_hash(filepath)
        return fh in session.imported_files

    def mark_file_imported(self, session: Session, filepath: str) -> None:
        fh = self.file_hash(filepath)
        session.imported_files[fh] = f"{os.path.basename(filepath)} @ {_now_iso()}"

    def add_history(self, session: Session, action: str, **details) -> HistoryEntry:
        entry = HistoryEntry(id=_uuid(), action=action, details=details)
        session.history.append(entry)
        return entry

    def status_summary(self, session: Session) -> Dict[str, Any]:
        inv_total = len(session.invoices)
        txn_total = len(session.transactions)
        inv_matched = sum(1 for i in session.invoices.values() if i.status == "matched")
        txn_matched = sum(1 for t in session.transactions.values() if t.status == "matched")
        inv_suspended = sum(1 for i in session.invoices.values() if i.suspended)
        txn_suspended = sum(1 for t in session.transactions.values() if t.suspended)
        inv_unmatched = inv_total - inv_matched - inv_suspended
        txn_unmatched = txn_total - txn_matched - txn_suspended
        active_matches = sum(1 for m in session.matches.values() if not m.reversed)
        reversed_matches = sum(1 for m in session.matches.values() if m.reversed)
        inv_amount_total = sum(i.amount for i in session.invoices.values())
        txn_amount_total = sum(t.amount for t in session.transactions.values())
        inv_amount_matched = sum(i.amount for i in session.invoices.values() if i.status == "matched")
        txn_amount_matched = sum(t.amount for t in session.transactions.values() if t.status == "matched")
        return {
            "session_id": session.session_id,
            "name": session.name,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "invoices": {
                "total": inv_total,
                "matched": inv_matched,
                "unmatched": inv_unmatched,
                "suspended": inv_suspended,
                "amount_total": round(inv_amount_total, 2),
                "amount_matched": round(inv_amount_matched, 2),
            },
            "transactions": {
                "total": txn_total,
                "matched": txn_matched,
                "unmatched": txn_unmatched,
                "suspended": txn_suspended,
                "amount_total": round(txn_amount_total, 2),
                "amount_matched": round(txn_amount_matched, 2),
            },
            "matches": {
                "active": active_matches,
                "reversed": reversed_matches,
                "total": len(session.matches),
            },
            "history_count": len(session.history),
            "imported_files": len(session.imported_files),
        }
