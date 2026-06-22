import hashlib
import json
import os
import uuid
import zipfile
import tempfile
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple

from .session import Session, SessionManager, _uuid, _now_iso
from .config import Config, DEFAULT_CONFIG


SNAPSHOT_VERSION = "1.0"
APP_VERSION = "1.0.0"
SNAPSHOT_DIR_NAME = ".irec_snapshots"
SNAPSHOT_ARCHIVE_EXT = ".irecsnap"


@dataclass
class SnapshotMetadata:
    snapshot_id: str
    created_at: str
    snapshot_version: str
    app_version: str
    original_session_name: str
    original_session_id: str
    summary: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    exported_by: str = ""


@dataclass
class SnapshotPackage:
    metadata: SnapshotMetadata
    session_data: Dict[str, Any]
    config_data: Dict[str, Any]
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "metadata": asdict(self.metadata),
            "session": self.session_data,
            "config": self.config_data,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SnapshotPackage":
        meta = SnapshotMetadata(**data["metadata"])
        return cls(
            metadata=meta,
            session_data=data["session"],
            config_data=data.get("config", {}),
            content_hash=data.get("content_hash", ""),
        )


def _compute_content_hash(session_data: dict, config_data: dict, metadata: dict) -> str:
    raw = json.dumps({
        "session": session_data,
        "config": config_data,
        "metadata": {k: v for k, v in metadata.items() if k != "created_at" and k != "snapshot_id"},
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_snapshot_structure(data: dict) -> List[str]:
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
    return errors


def _check_version_compatibility(snapshot_version: str) -> Tuple[bool, str]:
    try:
        snap_parts = [int(x) for x in snapshot_version.split(".")]
        curr_parts = [int(x) for x in SNAPSHOT_VERSION.split(".")]
    except (ValueError, AttributeError):
        return False, f"无法解析快照版本号: {snapshot_version}"

    if snap_parts[0] != curr_parts[0]:
        return False, (f"快照主版本不兼容: 快照 v{snapshot_version}, 当前支持 v{SNAPSHOT_VERSION}. "
                       f"主版本号不同，无法导入。请升级 CLI 或使用创建该快照的版本。")
    if snap_parts[1] > curr_parts[1]:
        return False, (f"快照次版本过高: 快照 v{snapshot_version}, 当前支持 v{SNAPSHOT_VERSION}. "
                       f"部分功能可能无法正确恢复。请升级 CLI。")
    return True, ""


class SnapshotManager:
    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir or os.getcwd()).resolve()
        self.snapshot_dir = self.base_dir / SNAPSHOT_DIR_NAME
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)

    def _snapshot_archive_path(self, filename: str) -> Path:
        if not filename.endswith(SNAPSHOT_ARCHIVE_EXT):
            filename = filename + SNAPSHOT_ARCHIVE_EXT
        return self.snapshot_dir / filename

    def list_snapshots(self) -> List[Dict[str, Any]]:
        snapshots = []
        for p in sorted(self.snapshot_dir.glob(f"*{SNAPSHOT_ARCHIVE_EXT}")):
            try:
                info = self.info(p.name)
                if info:
                    snapshots.append(info)
            except Exception as e:
                snapshots.append({
                    "file": p.name,
                    "snapshot_id": "corrupt",
                    "error": str(e),
                })
        return snapshots

    def info(self, filename_or_path: str) -> Optional[Dict[str, Any]]:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._snapshot_archive_path(filename_or_path)
        if not path.exists():
            return None

        try:
            pkg = self._load_archive(path)
            meta = pkg.metadata
            return {
                "file": path.name,
                "path": str(path),
                "snapshot_id": meta.snapshot_id,
                "snapshot_version": meta.snapshot_version,
                "app_version": meta.app_version,
                "created_at": meta.created_at,
                "original_session_name": meta.original_session_name,
                "original_session_id": meta.original_session_id,
                "summary": meta.summary,
                "notes": meta.notes,
                "has_config": bool(pkg.config_data),
                "invoices_count": len(pkg.session_data.get("invoices", {})),
                "transactions_count": len(pkg.session_data.get("transactions", {})),
                "matches_count": len(pkg.session_data.get("matches", {})),
                "history_count": len(pkg.session_data.get("history", [])),
                "content_hash_valid": self._verify_hash(pkg),
            }
        except Exception:
            return None

    def _verify_hash(self, pkg: SnapshotPackage) -> bool:
        computed = _compute_content_hash(
            pkg.session_data, pkg.config_data, asdict(pkg.metadata)
        )
        return computed == pkg.content_hash

    def _load_archive(self, path: Path) -> SnapshotPackage:
        errors = []
        try:
            with zipfile.ZipFile(path, "r") as zf:
                if "snapshot.json" not in zf.namelist():
                    raise ValueError("快照归档缺少 snapshot.json 核心文件")
                with zf.open("snapshot.json", "r") as f:
                    data = json.load(f)
        except zipfile.BadZipFile:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)

        errors = _validate_snapshot_structure(data)
        if any(not e.startswith("[警告]") for e in errors):
            raise ValueError("快照结构损坏: " + "; ".join(errors))

        pkg = SnapshotPackage.from_dict(data)
        return pkg

    def export(self, session: Session, config: Config,
               filename: Optional[str] = None, notes: str = "",
               exported_by: str = "") -> Tuple[str, SnapshotPackage]:
        sm = SessionManager(config.session_dir)
        summary = sm.status_summary(session)

        snapshot_id = f"snap_{_uuid()}"
        created_at = _now_iso()

        metadata = SnapshotMetadata(
            snapshot_id=snapshot_id,
            created_at=created_at,
            snapshot_version=SNAPSHOT_VERSION,
            app_version=APP_VERSION,
            original_session_name=session.name,
            original_session_id=session.session_id,
            summary=summary,
            notes=notes,
            exported_by=exported_by,
        )

        sm.add_history(
            session, "snapshot_export",
            snapshot_id=snapshot_id,
            snapshot_file="",
            snapshot_version=SNAPSHOT_VERSION,
            notes=notes,
            summary_snapshot=summary,
        )

        session_data = session.to_dict()
        config_dict = asdict(config)
        config_dict.pop("_config_path", None)

        # 修正 history 中的 snapshot_file（文件名在后面才确定）
        for h in session_data.get("history", []):
            if (h.get("action") == "snapshot_export"
                    and h.get("details", {}).get("snapshot_id") == snapshot_id
                    and not h["details"].get("snapshot_file")):
                h["details"]["snapshot_file"] = "pending"

        content_hash = _compute_content_hash(session_data, config_dict, asdict(metadata))

        pkg = SnapshotPackage(
            metadata=metadata,
            session_data=session_data,
            config_data=config_dict,
            content_hash=content_hash,
        )

        if not filename:
            safe_name = session.name.replace(" ", "_").replace("/", "_")
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{safe_name}_{timestamp}{SNAPSHOT_ARCHIVE_EXT}"
        elif not filename.endswith(SNAPSHOT_ARCHIVE_EXT):
            filename = filename + SNAPSHOT_ARCHIVE_EXT

        # 把真正的文件名写入 history 的 details 中（更新 session_data 和内存中的 session）
        for h in session_data.get("history", []):
            if (h.get("action") == "snapshot_export"
                    and h.get("details", {}).get("snapshot_id") == snapshot_id):
                h["details"]["snapshot_file"] = filename
        # 同步更新内存中 session 的对应 HistoryEntry
        for h in session.history:
            if (h.action == "snapshot_export"
                    and h.details.get("snapshot_id") == snapshot_id):
                h.details["snapshot_file"] = filename
        # 因为 filename 变了，重新计算 hash 和更新 pkg
        content_hash = _compute_content_hash(session_data, config_dict, asdict(metadata))
        pkg.session_data = session_data
        pkg.content_hash = content_hash

        archive_path = self.snapshot_dir / filename

        with tempfile.TemporaryDirectory() as tmpdir:
            json_path = Path(tmpdir) / "snapshot.json"
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(pkg.to_dict(), f, ensure_ascii=False, indent=2)
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(json_path, arcname="snapshot.json")

        return str(archive_path), pkg

    def analyze(self, filename_or_path: str) -> Dict[str, Any]:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._snapshot_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"快照文件不存在: {filename_or_path}")

        pkg = self._load_archive(path)
        result: Dict[str, Any] = {}

        ok, msg = _check_version_compatibility(pkg.metadata.snapshot_version)
        result["version_ok"] = ok
        result["version_message"] = msg
        result["version_snapshot"] = pkg.metadata.snapshot_version
        result["version_current"] = SNAPSHOT_VERSION

        result["content_hash_valid"] = self._verify_hash(pkg)
        result["warnings"] = []
        for e in _validate_snapshot_structure(pkg.to_dict()):
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

        result["invoices_count"] = len(sess_data.get("invoices", {}))
        result["transactions_count"] = len(sess_data.get("transactions", {}))
        result["matches_count"] = len(sess_data.get("matches", {}))
        result["history_count"] = len(sess_data.get("history", []))
        result["imported_files_count"] = len(sess_data.get("imported_files", {}))

        result["imported_file_hashes"] = list(sess_data.get("imported_files", {}).keys())

        result["snapshot_id"] = pkg.metadata.snapshot_id
        result["created_at"] = pkg.metadata.created_at
        result["notes"] = pkg.metadata.notes

        return result

    def import_snapshot(self, filename_or_path: str, sm: SessionManager,
                        target_session_name: Optional[str] = None,
                        conflict_mode: str = "ask",
                        apply_config: bool = False) -> Dict[str, Any]:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._snapshot_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"快照文件不存在: {filename_or_path}")

        pkg = self._load_archive(path)

        ok, msg = _check_version_compatibility(pkg.metadata.snapshot_version)
        if not ok:
            raise ValueError(f"版本不兼容: {msg}")

        analysis = self.analyze(str(path))
        warnings = list(analysis.get("warnings", []))
        missing_cfg = analysis.get("missing_config_keys", [])
        if missing_cfg:
            warnings.append(f"[警告] 快照中缺少配置项: {', '.join(missing_cfg)}，将使用默认值填充")

        orig_name = pkg.session_data.get("name", "restored")
        orig_id = pkg.session_data.get("session_id", "")
        desired_name = target_session_name or orig_name

        exists = sm.exists(desired_name)
        if exists:
            if conflict_mode == "reject":
                raise FileExistsError(
                    f"会话 '{desired_name}' 已存在 (冲突模式: reject). "
                    f"可使用 --overwrite 覆盖或指定 --as <新名称>"
                )
            elif conflict_mode == "rename":
                counter = 1
                new_name = f"{desired_name}_restored"
                while sm.exists(new_name):
                    counter += 1
                    new_name = f"{desired_name}_restored{counter}"
                desired_name = new_name
            elif conflict_mode == "overwrite":
                pass
            else:
                raise FileExistsError(
                    f"会话 '{desired_name}' 已存在. "
                    f"使用 --overwrite 覆盖, --reject 拒绝, 或 --as <新名称> 重命名导入"
                )

        if apply_config and pkg.config_data:
            cfg_snapshot = Config(**{
                k: v for k, v in pkg.config_data.items()
                if k in Config.__dataclass_fields__ and k != "_config_path"
            })
            cfg_snapshot._config_path = Config._find_config_path()
            cfg_snapshot.save()

        if sm.exists(desired_name) and conflict_mode == "overwrite":
            sm.delete(desired_name)

        new_session = Session.from_dict(pkg.session_data)
        new_session.name = desired_name
        new_session_path = sm._session_path(desired_name)
        if new_session_path.exists():
            if conflict_mode == "overwrite":
                new_session_path.unlink()
            else:
                pass

        new_session.session_id = _uuid()
        new_session.created_at = pkg.session_data.get("created_at", new_session.created_at)
        new_session.updated_at = _now_iso()

        sm.add_history(
            new_session, "snapshot_import",
            source_snapshot_id=pkg.metadata.snapshot_id,
            source_snapshot_file=path.name,
            original_session_name=orig_name,
            original_session_id=orig_id,
            target_session_name=desired_name,
            target_session_id=new_session.session_id,
            conflict_mode=conflict_mode,
            apply_config=apply_config,
            warnings=warnings,
        )

        sm._save(new_session)

        return {
            "success": True,
            "session_name": desired_name,
            "session_id": new_session.session_id,
            "original_session_name": orig_name,
            "original_session_id": orig_id,
            "snapshot_id": pkg.metadata.snapshot_id,
            "snapshot_file": path.name,
            "conflict_mode": conflict_mode,
            "overwritten": exists and conflict_mode == "overwrite",
            "renamed": desired_name != (target_session_name or orig_name),
            "warnings": warnings,
            "missing_config_keys": missing_cfg,
            "apply_config": apply_config,
            "content_hash_valid": analysis.get("content_hash_valid", False),
        }

    def delete(self, filename_or_path: str) -> bool:
        path = Path(filename_or_path)
        if not path.exists():
            path = self._snapshot_archive_path(filename_or_path)
        if not path.exists():
            raise FileNotFoundError(f"快照文件不存在: {filename_or_path}")
        path.unlink()
        return True

    def find_duplicate_import_sources(self, target_session: Session,
                                       snapshot_pkg: SnapshotPackage) -> Dict[str, List[str]]:
        target_hashes = set(target_session.imported_files.keys())
        snapshot_hashes = set(snapshot_pkg.session_data.get("imported_files", {}).keys())
        common = target_hashes & snapshot_hashes
        result: Dict[str, List[str]] = {"both": [], "target_only": [], "snapshot_only": []}
        for h in common:
            src1 = target_session.imported_files.get(h, h)
            src2 = snapshot_pkg.session_data.get("imported_files", {}).get(h, h)
            result["both"].append(f"{src1} <-> {src2}")
        for h in target_hashes - snapshot_hashes:
            result["target_only"].append(target_session.imported_files.get(h, h))
        for h in snapshot_hashes - target_hashes:
            result["snapshot_only"].append(
                snapshot_pkg.session_data.get("imported_files", {}).get(h, h)
            )
        return result
