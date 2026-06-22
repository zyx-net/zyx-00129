import hashlib
import json
import os
import uuid
import zipfile
import tempfile
import shutil
import csv
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from .session import Session, SessionManager, _uuid, _now_iso, HistoryEntry
from .config import Config, DEFAULT_CONFIG
from .reporter import build_report, export_csv, export_json


AUDIT_VERSION = "1.0"
APP_VERSION = "1.0.0"
AUDIT_DIR_NAME = ".irec_audits"
AUDIT_ARCHIVE_EXT = ".irecaudit"

AUDIT_MANIFEST = "audit_manifest.json"
AUDIT_SUMMARY = "session_summary.json"
AUDIT_CONFIG = "config_snapshot.json"
AUDIT_MATCH_DETAILS = "match_details.csv"
AUDIT_REV_SUSP = "reversal_suspension_records.csv"
AUDIT_SOURCE_FP = "source_fingerprints.json"
AUDIT_OPLOG = "operation_log.jsonl"
AUDIT_REPORT_JSON = "full_report/report.json"
AUDIT_REPORT_DIR = "full_report"
AUDIT_SESSION = "session.json"


@dataclass
class AuditMetadata:
    audit_id: str
    created_at: str
    audit_version: str
    app_version: str
    original_session_name: str
    original_session_id: str
    operator: str = ""
    notes: str = ""
    summary: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SourceFingerprint:
    file_hash: str
    file_label: str
    file_size: int
    source_type: str
    record_count: int


@dataclass
class AuditPackage:
    metadata: AuditMetadata
    manifest: Dict[str, Any]
    session_data: Dict[str, Any]
    config_data: Dict[str, Any]
    source_fingerprints: Dict[str, Dict[str, Any]]
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "metadata": asdict(self.metadata),
            "manifest": self.manifest,
            "session": self.session_data,
            "config": self.config_data,
            "source_fingerprints": self.source_fingerprints,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuditPackage":
        meta = AuditMetadata(**data["metadata"])
        return cls(
            metadata=meta,
            manifest=data.get("manifest", {}),
            session_data=data["session"],
            config_data=data.get("config", {}),
            source_fingerprints=data.get("source_fingerprints", {}),
            content_hash=data.get("content_hash", ""),
        )


def _compute_audit_content_hash(
    session_data: dict,
    config_data: dict,
    source_fps: dict,
    metadata: dict,
    manifest: dict,
) -> str:
    raw = json.dumps(
        {
            "session": session_data,
            "config": config_data,
            "source_fingerprints": source_fps,
            "metadata": {
                k: v
                for k, v in metadata.items()
                if k != "created_at" and k != "audit_id"
            },
            "manifest": manifest,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_audit_version_compatibility(audit_version: str) -> Tuple[bool, str]:
    try:
        audit_parts = [int(x) for x in audit_version.split(".")]
        curr_parts = [int(x) for x in AUDIT_VERSION.split(".")]
    except (ValueError, AttributeError):
        return False, f"无法解析审计包版本号: {audit_version}"
    if audit_parts[0] != curr_parts[0]:
        return False, (
            f"审计包主版本不兼容: 审计包 v{audit_version}, 当前支持 v{AUDIT_VERSION}. "
            f"主版本号不同，无法导入。请升级 CLI 或使用创建该审计包的版本。"
        )
    if audit_parts[1] > curr_parts[1]:
        return False, (
            f"审计包次版本过高: 审计包 v{audit_version}, 当前支持 v{AUDIT_VERSION}. "
            f"部分功能可能无法正确恢复。请升级 CLI。"
        )
    return True, ""


def _detect_config_drift(
    audit_cfg: dict, current_cfg: Config) -> Dict[str, Any]:
    drift = {}
    current = asdict(current_cfg)
    current.pop("_config_path", None)
    for key in DEFAULT_CONFIG.keys():
        if key in audit_cfg and key in current:
            if audit_cfg[key] != current[key]:
                drift[key] = {
                    "audit": audit_cfg[key],
                    "current": current[key],
                }
    return drift


def _collect_source_fingerprints(session: Session) -> Dict[str, Dict[str, Any]]:
    fps: Dict[str, Dict[str, Any]] = {}
    inv_sources: Dict[str, Dict[str, Any]] = {}
    txn_sources: Dict[str, Dict[str, Any]] = {}
    for inv in session.invoices.values():
        src = inv.source_file
        if src not in inv_sources:
            inv_sources[src] = {"count": 0}
        inv_sources[src]["count"] += 1
    for txn in session.transactions.values():
        src = txn.source_file
        if src not in txn_sources:
            txn_sources[src] = {"count": 0}
        txn_sources[src]["count"] += 1
    for fh, label in session.imported_files.items():
        file_basename = label.split(" @ ")[0] if " @ " in label else label
        inv_count = inv_sources.get(file_basename, {}).get("count", 0)
        txn_count = txn_sources.get(file_basename, {}).get("count", 0)
        fps[fh] = {
            "file_hash": fh,
            "file_label": label,
            "file_basename": file_basename,
            "invoice_count": inv_count,
            "transaction_count": txn_count,
            "total_records": inv_count + txn_count,
        }
    return fps


def _write_match_details_csv(session: Session, csv_path: str) -> None:
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "匹配ID", "匹配类型", "匹配时间", "发票号", "发票客户",
            "发票金额", "开票日期", "流水号", "流水对方", "流水金额",
            "交易日期", "发票合计", "流水合计", "差额", "备注",
            "是否已撤销", "撤销时间", "撤销原因",
        ])
        for m in sorted(session.matches.values(), key=lambda m: m.matched_at):
            invs = [session.invoices[i] for i in m.invoice_ids if i in session.invoices]
            txns = [session.transactions[t] for t in m.transaction_ids if t in session.transactions]
            inv_sum = sum(i.amount for i in invs)
            txn_sum = sum(t.amount for t in txns)
            max_rows = max(len(invs), len(txns), 1)
            for r in range(max_rows):
                inv = invs[r] if r < len(invs) else {}
                txn = txns[r] if r < len(txns) else {}
                w.writerow([
                    m.id if r == 0 else "",
                    m.match_type if r == 0 else "",
                    m.matched_at if r == 0 else "",
                    getattr(inv, "invoice_no", ""),
                    getattr(inv, "customer_name", ""),
                    getattr(inv, "amount", ""),
                    getattr(inv, "invoice_date", ""),
                    getattr(txn, "txn_id", ""),
                    getattr(txn, "counterparty", ""),
                    getattr(txn, "amount", ""),
                    getattr(txn, "txn_date", ""),
                    round(inv_sum, 2) if r == 0 else "",
                    round(txn_sum, 2) if r == 0 else "",
                    round(abs(inv_sum - txn_sum), 2) if r == 0 else "",
                    m.notes if r == 0 else "",
                    "是" if m.reversed and r == 0 else ("否" if r == 0 else ""),
                    m.reversed_at if m.reversed and r == 0 else "",
                    m.reversed_reason if m.reversed and r == 0 else "",
                ])


def _write_rev_susp_csv(session: Session, csv_path: str) -> None:
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["类型", "记录ID", "关联标识", "时间", "原因", "详细信息"])
        for m in session.matches.values():
            if m.reversed:
                invs = [session.invoices[i].invoice_no for i in m.invoice_ids if i in session.invoices]
                txns = [session.transactions[t].txn_id for t in m.transaction_ids if t in session.transactions]
                w.writerow([
                    "撤销匹配",
                    m.id,
                    f"发票:{','.join(invs)} | 流水:{','.join(txns)}",
                    m.reversed_at,
                    m.reversed_reason,
                    f"原匹配类型:{m.match_type}, 匹配时间:{m.matched_at}",
                ])
        for inv in session.invoices.values():
            if inv.suspended:
                w.writerow([
                    "挂起发票",
                    inv.id,
                    inv.invoice_no,
                    inv.created_at,
                    inv.suspend_reason,
                    f"客户:{inv.customer_name}, 金额:{inv.amount}, 日期:{inv.invoice_date}",
                ])
        for txn in session.transactions.values():
            if txn.suspended:
                w.writerow([
                    "挂起流水",
                    txn.id,
                    txn.txn_id,
                    txn.created_at,
                    txn.suspend_reason,
                    f"对方:{txn.counterparty}, 金额:{txn.amount}, 日期:{txn.txn_date}",
                ])


def _write_operation_log_jsonl(session: Session, jsonl_path: str) -> None:
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for h in session.history:
            line = json.dumps(
                {
                    "id": h.id,
                    "action": h.action,
                    "timestamp": h.timestamp,
                    "details": h.details,
                },
                ensure_ascii=False,
            )
            f.write(line + "\n")


def _validate_audit_structure(data: dict) -> List[str]:
    errors = []
    if "metadata" not in data:
        errors.append("缺少 metadata 字段")
    if "session" not in data:
        errors.append("缺少 session 字段")
    else:
        sess = data["session"]
        for key in ["session_id", "name", "created_at", "updated_at"]:
            if key not in sess:
                errors.append(f"session 缺少必要字段: {key}")
        for key in ["invoices", "transactions", "matches", "history", "imported_files"]:
            if key not in sess:
                sess[key] = {} if key != "history" else []
                errors.append(f"[警告] session 缺少字段 {key}，已使用空值替代")
    if "config" not in data:
        errors.append("[警告] 缺少 config 字段，导入时将使用当前配置")
    if "manifest" not in data:
        errors.append("[警告] 缺少 manifest 字段")
    if "source_fingerprints" not in data:
        errors.append("[警告] 缺少 source_fingerprints 字段")
    return errors


class AuditManager:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir or os.getcwd()).resolve()
        self.audit_dir = self.base_dir / AUDIT_DIR_NAME
        self.audit_dir.mkdir(parents=True, exist_ok=True)

    def _audit_archive_path(self, filename: str) -> Path:
        if not filename.endswith(AUDIT_ARCHIVE_EXT):
            filename = filename + AUDIT_ARCHIVE_EXT
        return self.audit_dir / filename

    def list_audits(self) -> List[Dict[str, Any]]:
        audits = []
        pattern = "*" + AUDIT_ARCHIVE_EXT
        for p in sorted(self.audit_dir.glob(pattern)):
            try:
                info = self.info(p.name)
                if info:
                    audits.append(info)
            except Exception as e:
                audits.append({
                    "file": p.name,
                    "audit_id": "corrupt",
                    "error": str(e),
                })
        return audits

    def info(self, filename_or_path: str) -> Optional[Dict[str, Any]]:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._audit_archive_path(filename_or_path)
        if not path.exists():
            return None
        try:
            pkg = self._load_archive(path)
            meta = pkg.metadata
            manifest = pkg.manifest
            return {
                "file": path.name,
                "path": str(path),
                "audit_id": meta.audit_id,
                "audit_version": meta.audit_version,
                "app_version": meta.app_version,
                "created_at": meta.created_at,
                "original_session_name": meta.original_session_name,
                "original_session_id": meta.original_session_id,
                "operator": meta.operator,
                "notes": meta.notes,
                "summary": meta.summary,
                "manifest": manifest,
                "has_config": bool(pkg.config_data),
                "invoices_count": len(pkg.session_data.get("invoices", {})),
                "transactions_count": len(pkg.session_data.get("transactions", {})),
                "matches_count": len(pkg.session_data.get("matches", {})),
                "history_count": len(pkg.session_data.get("history", [])),
                "source_files_count": len(pkg.source_fingerprints),
                "content_hash_valid": self._verify_hash(pkg),
            }
        except Exception:
            return None

    def _verify_hash(self, pkg: AuditPackage) -> bool:
        computed = _compute_audit_content_hash(
            pkg.session_data, pkg.config_data, pkg.source_fingerprints, asdict(pkg.metadata), pkg.manifest)
        return computed == pkg.content_hash

    def _load_archive(self, path: Path) -> AuditPackage:
        try:
            with zipfile.ZipFile(path, "r") as zf:
                if AUDIT_MANIFEST not in zf.namelist():
                    raise ValueError("审计包归档缺少 audit_manifest.json 核心文件")
                with zf.open(AUDIT_MANIFEST, "r") as f:
                    data = json.load(f)
        except zipfile.BadZipFile:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

        errors = _validate_audit_structure(data)
        if any(not e.startswith("[警告]") for e in errors):
            raise ValueError("审计包结构损坏: " + "; ".join(errors))
        pkg = AuditPackage.from_dict(data)
        return pkg

    def export(
        self,
        session: Session,
        config: Config,
        filename: Optional[str] = None,
        notes: str = "",
        operator: str = "",
    ) -> Tuple[str, AuditPackage]:
        sm = SessionManager(config.session_dir)
        summary = sm.status_summary(session)
        report = build_report(session, config)

        audit_id = f"audit_{_uuid()}"
        created_at = _now_iso()

        source_fps = _collect_source_fingerprints(session)

        metadata = AuditMetadata(
            audit_id=audit_id,
            created_at=created_at,
            audit_version=AUDIT_VERSION,
            app_version=APP_VERSION,
            original_session_name=session.name,
            original_session_id=session.session_id,
            operator=operator,
            notes=notes,
            summary=summary,
        )

        manifest = {
            "files": {
                AUDIT_SUMMARY: "会话摘要",
                AUDIT_CONFIG: "配置快照",
                AUDIT_MATCH_DETAILS: "匹配明细",
                AUDIT_REV_SUSP: "撤销与挂起记录",
                AUDIT_SOURCE_FP: "来源文件指纹",
                AUDIT_OPLOG: "操作日志(JSONL)",
                AUDIT_REPORT_JSON: "完整报告(JSON)",
                f"{AUDIT_REPORT_DIR}/summary.csv": "报告-汇总表",
                f"{AUDIT_REPORT_DIR}/matches.csv": "报告-匹配明细",
                f"{AUDIT_REPORT_DIR}/unmatched_invoices.csv": "报告-未匹配发票",
                f"{AUDIT_REPORT_DIR}/unmatched_transactions.csv": "报告-未匹配流水",
                f"{AUDIT_REPORT_DIR}/reversed_matches.csv": "报告-撤销匹配",
                AUDIT_SESSION: "完整会话数据",
            },
            "history_count": len(session.history),
            "source_file_count": len(source_fps),
            "match_count": len(session.matches),
        }

        sm.add_history(
            session,
            "audit_export",
            audit_id=audit_id,
            audit_file="",
            audit_version=AUDIT_VERSION,
            notes=notes,
            operator=operator,
            summary_snapshot=summary,
        )

        session_data = session.to_dict()
        config_dict = asdict(config)
        config_dict.pop("_config_path", None)

        for h in session_data.get("history", []):
            if (
                h.get("action") == "audit_export"
                and h.get("details", {}).get("audit_id") == audit_id
                and not h["details"].get("audit_file")
            ):
                h["details"]["audit_file"] = "pending"

        content_hash = _compute_audit_content_hash(
            session_data, config_dict, source_fps, asdict(metadata), manifest)

        pkg = AuditPackage(
            metadata=metadata,
            manifest=manifest,
            session_data=session_data,
            config_data=config_dict,
            source_fingerprints=source_fps,
            content_hash=content_hash,
        )

        if not filename:
            safe_name = session.name.replace(" ", "_").replace("/", "_")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_name}_audit_{timestamp}{AUDIT_ARCHIVE_EXT}"
        elif not filename.endswith(AUDIT_ARCHIVE_EXT):
            filename = filename + AUDIT_ARCHIVE_EXT

        for h in session_data.get("history", []):
            if (
                h.get("action") == "audit_export"
                and h.get("details", {}).get("audit_id") == audit_id
            ):
                h["details"]["audit_file"] = filename
        for h in session.history:
            if (
                h.action == "audit_export"
                and h.details.get("audit_id") == audit_id
            ):
                h.details["audit_file"] = filename
        content_hash = _compute_audit_content_hash(
            session_data, config_dict, source_fps, asdict(metadata), manifest)
        pkg.session_data = session_data
        pkg.content_hash = content_hash

        archive_path = self.audit_dir / filename

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            summary_path = tmp / AUDIT_SUMMARY
            with open(summary_path, "w", encoding="utf-8") as f:
                json.dump(summary, f, ensure_ascii=False, indent=2)

            config_path = tmp / AUDIT_CONFIG
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, ensure_ascii=False, indent=2)

            match_csv_path = tmp / AUDIT_MATCH_DETAILS
            _write_match_details_csv(session, str(match_csv_path))

            rev_susp_path = tmp / AUDIT_REV_SUSP
            _write_rev_susp_csv(session, str(rev_susp_path))

            fp_path = tmp / AUDIT_SOURCE_FP
            with open(fp_path, "w", encoding="utf-8") as f:
                json.dump(source_fps, f, ensure_ascii=False, indent=2)

            oplog_path = tmp / AUDIT_OPLOG
            _write_operation_log_jsonl(session, str(oplog_path))

            report_dir = tmp / AUDIT_REPORT_DIR
            report_dir.mkdir(exist_ok=True)
            report_json_path = report_dir / "report.json"
            export_json(report, str(report_json_path))
            export_csv(report, str(report_dir))

            session_path = tmp / AUDIT_SESSION
            with open(session_path, "w", encoding="utf-8") as f:
                json.dump(session_data, f, ensure_ascii=False, indent=2)

            manifest_path = tmp / AUDIT_MANIFEST
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(pkg.to_dict(), f, ensure_ascii=False, indent=2)

            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for sub in tmp.rglob("*"):
                    if sub.is_file():
                        arcname = str(sub.relative_to(tmp)).replace("\\", "/")
                        zf.write(str(sub), arcname=arcname)

        return str(archive_path), pkg

    def analyze(self, filename_or_path: str, current_config: Optional[Config] = None) -> Dict[str, Any]:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._audit_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"审计包不存在: {filename_or_path}")

        pkg = self._load_archive(path)
        result: Dict[str, Any] = {}

        ok, msg = _check_audit_version_compatibility(pkg.metadata.audit_version)
        result["version_ok"] = ok
        result["version_message"] = msg
        result["version_audit"] = pkg.metadata.audit_version
        result["version_current"] = AUDIT_VERSION
        result["content_hash_valid"] = self._verify_hash(pkg)
        result["warnings"] = []
        for e in _validate_audit_structure(pkg.to_dict()):
            if e.startswith("[警告]"):
                result["warnings"].append(e)

        sess_data = pkg.session_data
        cfg_data = pkg.config_data
        result["original_session_name"] = sess_data.get("name", "?")
        result["original_session_id"] = sess_data.get("session_id", "?")

        missing_cfg_keys = [k for k in DEFAULT_CONFIG.keys() if k not in cfg_data]
        result["missing_config_keys"] = missing_cfg_keys
        result["config_complete"] = len(missing_cfg_keys) == 0
        result["config_data"] = cfg_data

        if current_config is not None:
            result["config_drift"] = _detect_config_drift(cfg_data, current_config)
        else:
            result["config_drift"] = {}

        result["invoices_count"] = len(sess_data.get("invoices", {}))
        result["transactions_count"] = len(sess_data.get("transactions", {}))
        result["matches_count"] = len(sess_data.get("matches", {}))
        result["history_count"] = len(sess_data.get("history", []))
        result["imported_files_count"] = len(sess_data.get("imported_files", {}))
        result["imported_file_hashes"] = list(sess_data.get("imported_files", {}).keys())
        result["source_fingerprints"] = pkg.source_fingerprints

        result["audit_id"] = pkg.metadata.audit_id
        result["created_at"] = pkg.metadata.created_at
        result["notes"] = pkg.metadata.notes
        result["operator"] = pkg.metadata.operator
        result["manifest"] = pkg.manifest

        return result

    def import_audit(
        self,
        filename_or_path: str,
        sm: SessionManager,
        target_session_name: Optional[str] = None,
        conflict_mode: str = "ask",
        apply_config: bool = False,
        current_config: Optional[Config] = None,
        compare_session: Optional[Session] = None,
        save_precheck: bool = True,
    ) -> Dict[str, Any]:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._audit_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"审计包不存在: {filename_or_path}")

        effective_conflict = conflict_mode if conflict_mode != "ask" else "reject"

        precheck_result = self.precheck(
            str(path),
            sm,
            target_session_name=target_session_name,
            conflict_mode=effective_conflict,
            apply_config=apply_config,
            current_config=current_config,
            compare_session=compare_session,
        )

        if save_precheck:
            precheck_store = PrecheckStore()
            precheck_store.save(precheck_result)

        pkg = self._load_archive(path)

        ok, msg = _check_audit_version_compatibility(pkg.metadata.audit_version)
        if not ok:
            raise ValueError(f"版本不兼容: {msg}")

        analysis = self.analyze(str(path), current_config=current_config)
        warnings = list(analysis.get("warnings", []))
        missing_cfg = analysis.get("missing_config_keys", [])
        if missing_cfg:
            warnings.append(
                f"[警告] 审计包中缺少配置项: {', '.join(missing_cfg)}，将使用默认值填充"
            )

        config_drift = analysis.get("config_drift", {})
        if config_drift and not apply_config:
            drift_items = ", ".join(config_drift.keys())
            warnings.append(
                f"[提示] 配置漂移检测: 以下配置与当前工作目录配置不同: {drift_items}"
            )

        duplicate_sources = analysis.get("imported_file_hashes", [])

        orig_name = pkg.session_data.get("name", "restored")
        orig_id = pkg.session_data.get("session_id", "")
        intended_session_name = target_session_name or orig_name
        desired_name = intended_session_name

        exists = sm.exists(desired_name)
        overwritten = False
        renamed = False

        if exists:
            if conflict_mode == "reject":
                raise FileExistsError(
                    f"会话 '{desired_name}' 已存在 (冲突模式: reject). "
                    f"可使用 --overwrite 覆盖或指定 --as <新名称> 另存新副本"
                )
            elif conflict_mode == "rename":
                counter = 1
                new_name = f"{desired_name}_restored"
                while sm.exists(new_name):
                    counter += 1
                    new_name = f"{desired_name}_restored{counter}"
                desired_name = new_name
                renamed = True
            elif conflict_mode == "overwrite":
                overwritten = True
            else:
                raise FileExistsError(
                    f"会话 '{desired_name}' 已存在. "
                    f"使用 --overwrite 覆盖, --reject 拒绝, 或 --as <新名称> 另存新副本"
                )

        if apply_config and pkg.config_data:
            cfg_audit = Config(**{
                k: v
                for k, v in pkg.config_data.items()
                if k in Config.__dataclass_fields__ and k != "_config_path"
            })
            cfg_audit._config_path = Config._find_config_path()
            cfg_audit.save()

        if sm.exists(desired_name) and conflict_mode == "overwrite":
            sm.delete(desired_name)

        new_session = Session.from_dict(pkg.session_data)
        new_session.name = desired_name

        new_session_path = sm._session_path(desired_name)
        if new_session_path.exists() and conflict_mode == "overwrite":
            new_session_path.unlink()

        new_session.session_id = _uuid()
        new_session.created_at = pkg.session_data.get("created_at", new_session.created_at)
        new_session.updated_at = _now_iso()

        precheck_summary = {
            "precheck_id": precheck_result.precheck_id,
            "version_ok": precheck_result.version_ok,
            "content_hash_valid": precheck_result.content_hash_valid,
            "config_drift_count": len(precheck_result.config_drift),
            "config_drift_keys": list(precheck_result.config_drift.keys()),
            "config_drift_summary": precheck_result.config_drift_summary,
            "missing_config_keys": precheck_result.missing_config_keys,
            "duplicate_source_count": len(precheck_result.duplicate_sources.get("both", [])),
            "duplicate_sources": precheck_result.duplicate_sources,
            "session_existed_before": precheck_result.session_exists,
            "precheck_conclusion": "pass" if precheck_result.importable else "fail",
            "precheck_conflict_resolution": (
                "overwrite" if precheck_result.will_overwrite else
                "rename" if precheck_result.will_rename else
                "reject" if precheck_result.will_reject else
                "new_session"
            ),
            "final_conflict_resolution": (
                "overwrite" if overwritten else
                "rename" if renamed else
                "reject" if precheck_result.will_reject else
                "new_session"
            ),
            "intended_session_name": intended_session_name,
            "final_session_name": desired_name,
            "warnings_count": len(precheck_result.warnings),
            "errors_count": len(precheck_result.errors),
            "warnings": precheck_result.warnings,
            "errors": precheck_result.errors,
        }

        final_action_label = (
            "overwrite" if overwritten else
            "rename" if renamed else
            "create"
        )
        final_reason = ""
        if overwritten:
            final_reason = "同名会话已存在，用户指定 --overwrite 强制覆盖"
        elif renamed:
            final_reason = "同名会话已存在，用户指定 --auto-rename 自动另存新副本"
        elif precheck_result.session_exists and conflict_mode == "reject":
            final_reason = "同名会话已存在，冲突模式为 reject，应被拒绝（不会走到这里）"
        else:
            final_reason = "目标会话名未被占用，创建新会话"

        sm.add_history(
            new_session,
            "audit_import",
            source_audit_id=pkg.metadata.audit_id,
            source_audit_file=path.name,
            original_session_name=orig_name,
            original_session_id=orig_id,
            target_session_name=intended_session_name,
            target_session_id=new_session.session_id,
            final_session_name=desired_name,
            conflict_mode=conflict_mode,
            apply_config=apply_config,
            warnings=warnings,
            config_drift_detected=list(config_drift.keys()) if config_drift else [],
            config_drift_full=config_drift,
            duplicate_source_files=duplicate_sources,
            precheck_id=precheck_result.precheck_id,
            precheck_summary=precheck_summary,
            final_action=final_action_label,
            final_action_reason=final_reason,
            conflict_branch_result={
                "session_existed_before": precheck_result.session_exists,
                "overwritten": overwritten,
                "renamed": renamed,
                "original_name": intended_session_name if (overwritten or precheck_result.session_exists) else "",
                "final_name": desired_name,
                "intended_session_name": intended_session_name,
            },
            import_timestamp=_now_iso(),
        )

        sm._save(new_session)

        if save_precheck:
            precheck_store = PrecheckStore()
            old_precheck = precheck_store.load(precheck_result.precheck_id)
            if old_precheck is not None:
                old_precheck.import_executed = True
                old_precheck.imported_at = _now_iso()
                old_precheck.imported_session_name = desired_name
                old_precheck.imported_session_id = new_session.session_id
                old_precheck.actual_final_action = final_action_label
                old_precheck.actual_conflict_mode = conflict_mode
                precheck_store.save(old_precheck)

        return {
            "success": True,
            "session_name": desired_name,
            "session_id": new_session.session_id,
            "original_session_name": orig_name,
            "original_session_id": orig_id,
            "intended_session_name": intended_session_name,
            "audit_id": pkg.metadata.audit_id,
            "audit_file": path.name,
            "conflict_mode": conflict_mode,
            "overwritten": overwritten,
            "renamed": renamed,
            "warnings": warnings,
            "missing_config_keys": missing_cfg,
            "apply_config": apply_config,
            "content_hash_valid": analysis.get("content_hash_valid", False),
            "config_drift": config_drift,
            "duplicate_sources": duplicate_sources,
            "precheck_id": precheck_result.precheck_id,
            "precheck_summary": precheck_summary,
            "conflict_original_name": intended_session_name if (overwritten or precheck_result.session_exists) else "",
            "conflict_final_name": desired_name,
        }

    def replay_log(
        self,
        filename_or_path: str,
        target_session: Session,
        sm: SessionManager,
    ) -> Dict[str, Any]:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._audit_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"审计包不存在: {filename_or_path}")

        count = 0
        with zipfile.ZipFile(path, "r") as zf:
            if AUDIT_OPLOG not in zf.namelist():
                raise ValueError("审计包中无操作日志文件")
            with zf.open(AUDIT_OPLOG, "r") as f:
                for raw_line in f:
                    line = raw_line.decode("utf-8").strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    sm.add_history(
                        target_session,
                        entry["action"],
                        **entry.get("details", {}),
                    )
                    count += 1

        return {
            "replayed_count": count,
        }

    def delete(self, filename_or_path: str) -> bool:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._audit_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"审计包不存在: {filename_or_path}")
        path.unlink()
        return True

    def find_audit_imports(
        self,
        audit_id: str,
        sm: SessionManager,
    ) -> List[Dict[str, Any]]:
        """反向查找：给定审计包 audit_id，在所有会话中查找从它导入的历史记录。
        返回结构化列表，每项对应一次 audit_import 操作，用于三处入口复查时复用。"""
        results: List[Dict[str, Any]] = []
        for sess_info in sm.list_sessions():
            try:
                sess = sm.load(sess_info["name"])
            except Exception:
                continue
            for h in sess.history:
                if h.action != "audit_import":
                    continue
                if h.details.get("source_audit_id") != audit_id:
                    continue
                d = h.details
                pre_sum = d.get("precheck_summary", {})
                conflict = d.get("conflict_branch_result", {})
                intended_sn = (
                    conflict.get("intended_session_name")
                    or conflict.get("original_name")
                    or d.get("target_session_name", "")
                )
                conflict_orig = intended_sn
                conflict_final = (
                    conflict.get("final_name")
                    or d.get("final_session_name")
                    or sess.name
                )
                results.append({
                    "session_name": sess.name,
                    "session_id": sess.session_id,
                    "history_entry_id": h.id,
                    "history_timestamp": h.timestamp,
                    "source_audit_id": d.get("source_audit_id", ""),
                    "source_audit_file": d.get("source_audit_file", ""),
                    "original_session_name": d.get("original_session_name", ""),
                    "original_session_id": d.get("original_session_id", ""),
                    "target_session_name": intended_sn,
                    "target_session_id": d.get("target_session_id", ""),
                    "final_session_name": conflict_final,
                    "intended_session_name": intended_sn,
                    "conflict_mode": d.get("conflict_mode", ""),
                    "apply_config": d.get("apply_config", False),
                    "final_action": d.get("final_action", ""),
                    "final_action_reason": d.get("final_action_reason", ""),
                    "final_action_label": (
                        "覆盖（overwrite）" if d.get("final_action") == "overwrite" else
                        "自动重命名（auto-rename）" if d.get("final_action") == "rename" else
                        "拒绝（reject）" if d.get("final_action") == "reject" else
                        "创建新会话"
                    ),
                    "precheck_id": d.get("precheck_id", ""),
                    "precheck_conclusion": (
                        "通过（可导入）" if pre_sum.get("precheck_conclusion") == "pass" else
                        "失败（不可导入）" if pre_sum.get("precheck_conclusion") == "fail" else
                        "(未知)"
                    ),
                    "precheck_conflict_resolution": pre_sum.get("precheck_conflict_resolution", ""),
                    "final_conflict_resolution": pre_sum.get("final_conflict_resolution", ""),
                    "session_existed_before": conflict.get("session_existed_before", False),
                    "overwritten": conflict.get("overwritten", False),
                    "renamed": conflict.get("renamed", False),
                    "conflict_original_name": conflict_orig,
                    "conflict_final_name": conflict_final,
                    "conflict_branch_result": conflict,
                    "config_drift_keys": d.get("config_drift_detected", []),
                    "config_drift_count": len(d.get("config_drift_detected", [])),
                    "config_drift_full": d.get("config_drift_full", {}),
                    "config_drift_summary": pre_sum.get("config_drift_summary", {}),
                    "duplicate_source_count": pre_sum.get("duplicate_source_count", 0),
                    "duplicate_sources": pre_sum.get("duplicate_sources", {}),
                    "warnings": d.get("warnings", []),
                    "missing_config_keys": pre_sum.get("missing_config_keys", []),
                    "import_timestamp": d.get("import_timestamp", h.timestamp),
                })
        return results

    def check_duplicate_sources(
        self,
        target_session: Session,
        audit_pkg: AuditPackage,
    ) -> Dict[str, List[str]]:
        target_hashes = set(target_session.imported_files.keys())
        audit_hashes = set(
            audit_pkg.session_data.get("imported_files", {}).keys())
        common = target_hashes & audit_hashes
        result: Dict[str, List[str]] = {
            "both": [],
            "target_only": [],
            "audit_only": [],
        }
        for h in common:
            src1 = target_session.imported_files.get(h, h)
            src2 = audit_pkg.session_data.get("imported_files", {}).get(h, h)
            result["both"].append(f"{src1} <-> {src2}")
        for h in target_hashes - audit_hashes:
            result["target_only"].append(target_session.imported_files.get(h, h))
        for h in audit_hashes - target_hashes:
            result["audit_only"].append(
                audit_pkg.session_data.get("imported_files", {}).get(h, h)
            )
        return result

    def precheck(
        self,
        filename_or_path: str,
        sm: SessionManager,
        target_session_name: Optional[str] = None,
        conflict_mode: str = "reject",
        apply_config: bool = False,
        current_config: Optional[Config] = None,
        compare_session: Optional[Session] = None,
    ) -> "PrecheckResult":
        path = Path(filename_or_path)
        if not path.exists():
            path = self._audit_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"审计包不存在: {filename_or_path}")

        precheck_id = f"precheck_{_uuid()}"
        precheck_at = _now_iso()

        errors: List[str] = []
        warnings: List[str] = []

        try:
            pkg = self._load_archive(path)
        except Exception as e:
            return PrecheckResult(
                precheck_id=precheck_id,
                audit_file=path.name,
                audit_path=str(path),
                audit_id="unknown",
                precheck_at=precheck_at,
                target_session_name=target_session_name or "unknown",
                conflict_mode=conflict_mode,
                apply_config=apply_config,
                session_exists=False,
                resolved_name=target_session_name or "unknown",
                will_overwrite=False,
                will_rename=False,
                will_reject=True,
                rename_to="",
                version_ok=False,
                version_message="",
                content_hash_valid=False,
                config_drift={},
                missing_config_keys=[],
                duplicate_sources={"both": [], "target_only": [], "audit_only": []},
                warnings=[],
                errors=[f"审计包无法加载: {e}"],
                importable=False,
                summary={},
            )

        analysis = self.analyze(str(path), current_config=current_config)

        version_ok = analysis.get("version_ok", False)
        version_message = analysis.get("version_message", "")
        content_hash_valid = analysis.get("content_hash_valid", False)
        config_drift = analysis.get("config_drift", {})
        missing_config_keys = analysis.get("missing_config_keys", [])

        config_drift_summary = {}
        if config_drift:
            changed_keys = list(config_drift.keys())
            config_drift_summary = {
                "total": len(changed_keys),
                "changed_keys": changed_keys,
                "diff": {k: v for k, v in list(config_drift.items())[:10]},
            }

        if not version_ok:
            errors.append(f"版本不兼容: {version_message}")

        if not content_hash_valid:
            warnings.append("内容哈希校验失败，文件可能被篡改或损坏")

        for w in analysis.get("warnings", []):
            warnings.append(w)

        if missing_config_keys and not apply_config:
            warnings.append(
                f"审计包缺少配置项: {', '.join(missing_config_keys)}，导入时将使用当前配置"
            )

        if config_drift and not apply_config:
            drift_items = ", ".join(config_drift.keys())
            warnings.append(
                f"配置漂移: 以下 {len(config_drift)} 项配置与当前工作目录不同: {drift_items}"
            )

        orig_name = pkg.session_data.get("name", "restored")
        desired_name = target_session_name or orig_name

        session_exists = sm.exists(desired_name)
        resolved_name, will_overwrite, will_rename, will_reject, rename_to = \
            _precheck_resolve_name(desired_name, sm, conflict_mode)

        if will_reject and session_exists:
            errors.append(
                f"会话 '{desired_name}' 已存在，冲突模式为 reject，导入将被拒绝"
            )

        duplicate_sources: Dict[str, List[str]] = {"both": [], "target_only": [], "audit_only": []}
        if compare_session is not None:
            duplicate_sources = self.check_duplicate_sources(compare_session, pkg)
            if duplicate_sources["both"]:
                warnings.append(
                    f"发现 {len(duplicate_sources['both'])} 个重复导入来源，"
                    f"若两边独立做过匹配，数据可能不一致"
                )

        importable = len(errors) == 0

        summary = {
            "original_session_name": orig_name,
            "original_session_id": pkg.session_data.get("session_id", ""),
            "invoices_count": len(pkg.session_data.get("invoices", {})),
            "transactions_count": len(pkg.session_data.get("transactions", {})),
            "matches_count": len(pkg.session_data.get("matches", {})),
            "history_count": len(pkg.session_data.get("history", [])),
            "imported_files_count": len(pkg.session_data.get("imported_files", {})),
        }

        result = PrecheckResult(
            precheck_id=precheck_id,
            audit_file=path.name,
            audit_path=str(path),
            audit_id=pkg.metadata.audit_id,
            precheck_at=precheck_at,
            target_session_name=desired_name,
            conflict_mode=conflict_mode,
            apply_config=apply_config,
            session_exists=session_exists,
            resolved_name=resolved_name,
            will_overwrite=will_overwrite,
            will_rename=will_rename,
            will_reject=will_reject,
            rename_to=rename_to,
            version_ok=version_ok,
            version_message=version_message,
            content_hash_valid=content_hash_valid,
            config_drift=config_drift,
            missing_config_keys=missing_config_keys,
            duplicate_sources=duplicate_sources,
            warnings=warnings,
            errors=errors,
            importable=importable,
            summary=summary,
            import_executed=False,
            imported_at="",
            imported_session_name="",
            imported_session_id="",
            actual_final_action="",
            actual_conflict_mode="",
            config_drift_summary=config_drift_summary,
        )

        return result


PRECHECK_DIR_NAME = ".irec_prechecks"


@dataclass
class PrecheckResult:
    precheck_id: str
    audit_file: str
    audit_path: str
    audit_id: str
    precheck_at: str
    target_session_name: str
    conflict_mode: str
    apply_config: bool
    session_exists: bool
    resolved_name: str
    will_overwrite: bool
    will_rename: bool
    will_reject: bool
    rename_to: str
    version_ok: bool
    version_message: str
    content_hash_valid: bool
    config_drift: Dict[str, Any]
    missing_config_keys: List[str]
    duplicate_sources: Dict[str, List[str]]
    warnings: List[str]
    errors: List[str]
    importable: bool
    summary: Dict[str, Any]
    import_executed: bool = False
    imported_at: str = ""
    imported_session_name: str = ""
    imported_session_id: str = ""
    actual_final_action: str = ""
    actual_conflict_mode: str = ""
    config_drift_summary: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "PrecheckResult":
        field_defaults = {
            "import_executed": False,
            "imported_at": "",
            "imported_session_name": "",
            "imported_session_id": "",
            "actual_final_action": "",
            "actual_conflict_mode": "",
            "config_drift_summary": {},
        }
        for k, v in field_defaults.items():
            if k not in data:
                data[k] = v
        return cls(**data)


class PrecheckStore:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir or os.getcwd()).resolve()
        self.precheck_dir = self.base_dir / PRECHECK_DIR_NAME
        self.precheck_dir.mkdir(parents=True, exist_ok=True)

    def _precheck_path(self, precheck_id: str) -> Path:
        return self.precheck_dir / f"{precheck_id}.json"

    def save(self, result: PrecheckResult) -> str:
        path = self._precheck_path(result.precheck_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
        return result.precheck_id

    def load(self, precheck_id: str) -> Optional[PrecheckResult]:
        path = self._precheck_path(precheck_id)
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return PrecheckResult.from_dict(data)
        except Exception:
            return None

    def list(self) -> List[Dict[str, Any]]:
        results = []
        for p in sorted(self.precheck_dir.glob("*.json"), reverse=True):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                results.append({
                    "precheck_id": data.get("precheck_id", p.stem),
                    "audit_file": data.get("audit_file", ""),
                    "audit_id": data.get("audit_id", ""),
                    "precheck_at": data.get("precheck_at", ""),
                    "target_session_name": data.get("target_session_name", ""),
                    "importable": data.get("importable", False),
                    "import_executed": data.get("import_executed", False),
                    "imported_at": data.get("imported_at", ""),
                    "imported_session_name": data.get("imported_session_name", ""),
                    "imported_session_id": data.get("imported_session_id", ""),
                    "actual_final_action": data.get("actual_final_action", ""),
                })
            except Exception:
                results.append({
                    "precheck_id": p.stem,
                    "audit_file": "",
                    "audit_id": "",
                    "precheck_at": "",
                    "target_session_name": "",
                    "importable": False,
                    "import_executed": False,
                    "imported_at": "",
                    "imported_session_name": "",
                    "imported_session_id": "",
                    "actual_final_action": "",
                })
        return results

    def delete(self, precheck_id: str) -> bool:
        path = self._precheck_path(precheck_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def clear(self) -> int:
        count = 0
        for p in self.precheck_dir.glob("*.json"):
            p.unlink()
            count += 1
        return count


def _precheck_resolve_name(
    desired_name: str,
    sm: SessionManager,
    conflict_mode: str,
) -> Tuple[str, bool, bool, bool, str]:
    exists = sm.exists(desired_name)
    will_overwrite = False
    will_rename = False
    will_reject = False
    resolved = desired_name
    rename_to = ""

    if exists:
        if conflict_mode == "reject":
            will_reject = True
        elif conflict_mode == "rename":
            will_rename = True
            counter = 1
            new_name = f"{desired_name}_restored"
            while sm.exists(new_name):
                counter += 1
                new_name = f"{desired_name}_restored{counter}"
            resolved = new_name
            rename_to = new_name
        elif conflict_mode == "overwrite":
            will_overwrite = True
            resolved = desired_name
        else:
            will_reject = True

    return resolved, will_overwrite, will_rename, will_reject, rename_to


def _detect_missing_files(pkg: AuditPackage) -> List[str]:
    missing = []
    manifest = pkg.manifest
    if not manifest or "files" not in manifest:
        return missing
    expected_files = list(manifest["files"].keys())
    for f in expected_files:
        if f == AUDIT_MANIFEST:
            continue
    return missing
