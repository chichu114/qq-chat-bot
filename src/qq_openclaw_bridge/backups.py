import fcntl
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from .config import Config
from .runtime.backup_crypto import BackupEnvelope, BackupIntegrityError
from .runtime.secrets import BackupKeyProvider


MANIFEST_NAME = "manifest.json"
BACKUP_VERSION = 1
_MAX_BACKUP_FILE_BYTES = 256 * 1024 * 1024
_MAX_BACKUP_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024


class BackupManager:
    def __init__(
        self,
        backup_dir: Path,
        sources: Dict[str, Path],
        now_provider: Optional[Callable[[], datetime]] = None,
        interval_hours: int = 24,
        retention_days: int = 30,
        max_auto_backups: int = 30,
        key_provider: Optional[object] = None,
    ) -> None:
        self.backup_dir = Path(backup_dir).expanduser()
        self.sources = {
            name: Path(path).expanduser() for name, path in sources.items()
        }
        self._now_provider = now_provider or (
            lambda: datetime.now(tz=ZoneInfo("Asia/Shanghai"))
        )
        self.interval_hours = max(1, interval_hours)
        self.retention_days = max(1, retention_days)
        self.max_auto_backups = max(1, max_auto_backups)
        self._key_provider = key_provider or BackupKeyProvider()

    def create_backup(self, kind: str = "manual") -> Dict[str, object]:
        with self._lock():
            return self._create_backup_unlocked(kind)

    def run_if_due(self) -> Optional[Dict[str, object]]:
        with self._lock():
            now = self._now_provider()
            automatic = [
                item
                for item in self._list_backups_unlocked()
                if item["kind"] == "auto"
                and item["encryption"] == "encrypted"
                and item["valid"]
            ]
            if automatic:
                latest = max(_parse_datetime(item["created_at"], now) for item in automatic)
                latest = min(latest, now)
                if now < latest + timedelta(hours=self.interval_hours):
                    return None
            created = self._create_backup_unlocked("auto")
            self._prune_unlocked()
            return created

    def list_backups(self) -> List[Dict[str, object]]:
        with self._lock():
            return self._list_backups_unlocked()

    def verify_backup(self, name: str) -> Dict[str, object]:
        with self._lock():
            return self._verify_unlocked(self._backup_path(name))

    def restore_backup(self, name: str) -> str:
        with self._lock():
            backup_path = self._backup_path(name)
            payload, verification = self._validated_payload(backup_path)
            if not verification["valid"]:
                raise ValueError("备份校验失败：%s" % verification["error"])
            assert payload is not None

            self._create_backup_unlocked("pre-restore")
            staged = []
            staged_sidecars = []
            try:
                with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
                    manifest = json.loads(archive.read(MANIFEST_NAME))
                    for archive_name in manifest["files"]:
                        destination = self.sources.get(archive_name)
                        if destination is None:
                            continue
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if destination.is_symlink():
                            raise ValueError("受管恢复目标不允许使用符号链接")
                        _clean_restore_temporaries(destination)
                        replacement = _write_secure_temporary(
                            destination,
                            "restore",
                            archive.read(archive_name),
                        )
                        try:
                            rollback = (
                                _copy_secure_temporary(destination, "rollback")
                                if destination.is_file()
                                else None
                            )
                        except Exception:
                            replacement.unlink(missing_ok=True)
                            raise
                        staged.append(
                            {
                                "destination": destination,
                                "replacement": replacement,
                                "rollback": rollback,
                                "existed": destination.is_file(),
                                "committed": False,
                            }
                        )
                        if destination.suffix in (".sqlite3", ".db"):
                            for suffix in ("-wal", "-shm"):
                                sidecar = Path(str(destination) + suffix)
                                if sidecar.is_symlink():
                                    raise ValueError(
                                        "SQLite 恢复附属文件不允许使用符号链接"
                                    )
                                if not sidecar.is_file():
                                    continue
                                _clean_restore_temporaries(sidecar)
                                staged_sidecars.append(
                                    {
                                        "destination": sidecar,
                                        "rollback": _copy_secure_temporary(
                                            sidecar, "rollback"
                                        ),
                                        "removed": False,
                                    }
                                )
                rollback_failed = False
                try:
                    for item in staged:
                        os.replace(item["replacement"], item["destination"])
                        item["replacement"] = None
                        item["committed"] = True
                        os.chmod(item["destination"], 0o600)
                    for item in staged_sidecars:
                        item["destination"].unlink()
                        item["removed"] = True
                except Exception:
                    for item in reversed(staged_sidecars):
                        if not item["removed"]:
                            continue
                        try:
                            os.replace(item["rollback"], item["destination"])
                            item["rollback"] = None
                        except Exception:
                            rollback_failed = True
                    for item in reversed(staged):
                        if not item["committed"]:
                            continue
                        try:
                            if item["existed"]:
                                os.replace(item["rollback"], item["destination"])
                                item["rollback"] = None
                            else:
                                item["destination"].unlink(missing_ok=True)
                        except Exception:
                            rollback_failed = True
                    if rollback_failed:
                        raise RuntimeError(
                            "恢复失败且自动回滚不完整，"
                            "请使用恢复前备份人工恢复"
                        ) from None
                    raise
                restored = len(staged)
                return (
                    "恢复完成，共恢复 %d 个受管文件。"
                    "重启智能体后配置会全部生效。" % restored
                )
            finally:
                _cleanup_staged_files(staged)
                _cleanup_staged_files(staged_sidecars)

    def prune(self) -> int:
        with self._lock():
            return self._prune_unlocked()

    def _create_backup_unlocked(self, kind: str) -> Dict[str, object]:
        safe_kind = kind if kind in ("manual", "auto", "pre-restore") else "manual"
        self._ensure_backup_dir()
        now = self._now_provider()
        stamp = now.strftime("%Y%m%d-%H%M%S-%f")
        final_path = self.backup_dir / (
            "%s-%s.chichu-backup" % (safe_kind, stamp)
        )
        manifest: Dict[str, object] = {
            "version": BACKUP_VERSION,
            "created_at": now.isoformat(timespec="seconds"),
            "kind": safe_kind,
            "files": {},
        }
        plaintext_path = None
        encrypted_path = None
        try:
            descriptor, plaintext_name = tempfile.mkstemp(
                prefix=".%s-%s-" % (safe_kind, stamp),
                suffix=".zip.tmp",
                dir=str(self.backup_dir),
            )
            os.close(descriptor)
            plaintext_path = Path(plaintext_name)
            os.chmod(plaintext_path, 0o600)
            total_uncompressed = 0
            with zipfile.ZipFile(
                plaintext_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for archive_name, source_path in self.sources.items():
                    if not source_path.is_file():
                        continue
                    remaining = _MAX_BACKUP_UNCOMPRESSED_BYTES - total_uncompressed
                    content = _read_source_bytes(source_path, remaining)
                    if archive_name == "project/.env":
                        content = _sanitized_env(content)
                    total_uncompressed += len(content)
                    archive.writestr(archive_name, content)
                    manifest["files"][archive_name] = {
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "size": len(content),
                    }
                manifest_bytes = json.dumps(
                    manifest, ensure_ascii=False, indent=2
                ).encode("utf-8")
                if (
                    len(manifest_bytes) > _MAX_MANIFEST_BYTES
                    or total_uncompressed + len(manifest_bytes)
                    > _MAX_BACKUP_UNCOMPRESSED_BYTES
                ):
                    raise ValueError("备份内容过大")
                archive.writestr(
                    MANIFEST_NAME,
                    manifest_bytes,
                )

            key = self._key_provider.get_or_create()
            plaintext = _read_bounded_path(
                plaintext_path, _MAX_BACKUP_FILE_BYTES
            )
            encrypted = BackupEnvelope.encrypt(plaintext, key)
            descriptor, encrypted_name = tempfile.mkstemp(
                prefix=".%s-" % final_path.name,
                suffix=".tmp",
                dir=str(self.backup_dir),
            )
            encrypted_path = Path(encrypted_name)
            with os.fdopen(descriptor, "wb") as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(encrypted_path, 0o600)
            os.replace(encrypted_path, final_path)
            encrypted_path = None
            os.chmod(final_path, 0o600)
            return self._backup_info_unlocked(final_path)
        finally:
            if plaintext_path is not None:
                plaintext_path.unlink(missing_ok=True)
            if encrypted_path is not None:
                encrypted_path.unlink(missing_ok=True)

    def _list_backups_unlocked(self) -> List[Dict[str, object]]:
        if not self.backup_dir.is_dir():
            return []
        paths = list(self.backup_dir.glob("*.chichu-backup"))
        paths.extend(self.backup_dir.glob("*.zip"))
        key_state = {"attempted": False, "key": None}
        items = [
            self._backup_info_unlocked(path, key_state)
            for path in paths
            if path.is_file()
        ]
        return sorted(items, key=lambda item: item["created_at"], reverse=True)

    def _backup_info_unlocked(
        self, path: Path, key_state: Optional[Dict[str, object]] = None
    ) -> Dict[str, object]:
        verification = self._verify_unlocked(path, key_state)
        stat_result = path.lstat()
        kind = str(verification.get("kind", "unknown"))
        created_at = str(
            verification.get(
                "created_at",
                datetime.fromtimestamp(stat_result.st_mtime).isoformat(
                    timespec="seconds"
                ),
            )
        )
        return {
            "name": path.name,
            "size": stat_result.st_size,
            "mtime": datetime.fromtimestamp(stat_result.st_mtime).isoformat(
                timespec="seconds"
            ),
            "created_at": created_at,
            "kind": kind,
            "encryption": verification.get("encryption", "unknown"),
            "valid": verification["valid"],
            "file_count": verification["file_count"],
        }

    def _verify_unlocked(
        self, path: Path, key_state: Optional[Dict[str, object]] = None
    ) -> Dict[str, object]:
        _, verification = self._validated_payload(path, key_state)
        return verification

    def _validated_payload(
        self, path: Path, key_state: Optional[Dict[str, object]] = None
    ):
        encryption = (
            "encrypted"
            if path.name.endswith(".chichu-backup")
            else "legacy-unencrypted"
        )
        if not path.is_file() or path.is_symlink():
            return None, {
                "valid": False,
                "file_count": 0,
                "error": "备份不存在",
                "encryption": encryption,
            }
        if path.lstat().st_size > _MAX_BACKUP_FILE_BYTES:
            return None, {
                "valid": False,
                "file_count": 0,
                "error": "备份文件过大",
                "encryption": encryption,
            }
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
            with os.fdopen(descriptor, "rb") as source:
                raw = source.read(_MAX_BACKUP_FILE_BYTES + 1)
        except OSError:
            return None, {
                "valid": False,
                "file_count": 0,
                "error": "备份无法读取",
                "encryption": encryption,
            }
        if len(raw) > _MAX_BACKUP_FILE_BYTES:
            return None, {
                "valid": False,
                "file_count": 0,
                "error": "备份文件过大",
                "encryption": encryption,
            }
        if encryption == "encrypted":
            key = None
            if key_state is None:
                try:
                    key = self._key_provider.get_or_create()
                except Exception:
                    pass
            else:
                if not key_state["attempted"]:
                    key_state["attempted"] = True
                    try:
                        key_state["key"] = self._key_provider.get_or_create()
                    except Exception:
                        key_state["key"] = None
                key = key_state["key"]
            if not isinstance(key, bytes):
                return None, {
                    "valid": False,
                    "file_count": 0,
                    "error": "备份密钥不可用",
                    "encryption": encryption,
                }
            try:
                payload = BackupEnvelope.decrypt(raw, key)
            except BackupIntegrityError:
                return None, {
                    "valid": False,
                    "file_count": 0,
                    "error": "加密备份认证失败",
                    "encryption": encryption,
                }
        else:
            payload = raw
        verification = self._verify_zip_payload(payload)
        verification["encryption"] = encryption
        return payload, verification

    def _verify_zip_payload(self, payload: bytes) -> Dict[str, object]:
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
                infos = archive.infolist()
                names = [item.filename for item in infos]
                if len(names) != len(set(names)) or len(names) > len(self.sources) + 1:
                    return {"valid": False, "file_count": 0, "error": "ZIP 成员无效"}
                if MANIFEST_NAME not in names:
                    return {"valid": False, "file_count": 0, "error": "文件清单缺失"}
                if sum(item.file_size for item in infos) > _MAX_BACKUP_UNCOMPRESSED_BYTES:
                    return {"valid": False, "file_count": 0, "error": "备份展开后过大"}
                manifest_info = archive.getinfo(MANIFEST_NAME)
                if manifest_info.file_size > _MAX_MANIFEST_BYTES:
                    return {"valid": False, "file_count": 0, "error": "文件清单过大"}
                manifest = json.loads(archive.read(MANIFEST_NAME))
                if manifest.get("version") != BACKUP_VERSION:
                    return {"valid": False, "file_count": 0, "error": "备份版本不支持"}
                files = manifest.get("files")
                if not isinstance(files, dict):
                    return {"valid": False, "file_count": 0, "error": "文件清单缺失"}
                if set(files) | {MANIFEST_NAME} != set(names):
                    return {"valid": False, "file_count": 0, "error": "ZIP 成员无效"}
                created_at = manifest.get("created_at")
                if not isinstance(created_at, str):
                    return {"valid": False, "file_count": 0, "error": "创建时间无效"}
                try:
                    _parse_iso_datetime(created_at)
                except ValueError:
                    return {"valid": False, "file_count": 0, "error": "创建时间无效"}
                for archive_name, metadata in files.items():
                    if (
                        not isinstance(archive_name, str)
                        or not isinstance(metadata, dict)
                    ):
                        return {"valid": False, "file_count": 0, "error": "文件清单无效"}
                    if archive_name not in self.sources:
                        return {"valid": False, "file_count": 0, "error": "包含未授权文件"}
                    expected_size = metadata.get("size")
                    if (
                        isinstance(expected_size, bool)
                        or not isinstance(expected_size, int)
                        or expected_size < 0
                        or archive.getinfo(archive_name).file_size != expected_size
                    ):
                        return {
                            "valid": False,
                            "file_count": len(files),
                            "error": "文件大小不匹配",
                        }
                    content = archive.read(archive_name)
                    if hashlib.sha256(content).hexdigest() != metadata.get("sha256"):
                        return {
                            "valid": False,
                            "file_count": len(files),
                            "error": "文件哈希不匹配",
                        }
                    if len(content) != expected_size:
                        return {
                            "valid": False,
                            "file_count": len(files),
                            "error": "文件大小不匹配",
                        }
                return {
                    "valid": True,
                    "file_count": len(files),
                    "error": "",
                    "kind": str(manifest.get("kind", "unknown")),
                    "created_at": created_at,
                }
        except (OSError, ValueError, KeyError, zipfile.BadZipFile, json.JSONDecodeError):
            return {"valid": False, "file_count": 0, "error": "ZIP 内容无效"}

    def _prune_unlocked(self) -> int:
        now = self._now_provider()
        automatic = [
            item
            for item in self._list_backups_unlocked()
            if item["encryption"] == "encrypted"
            and item["name"].startswith("auto-")
        ]
        automatic.sort(key=lambda item: item["created_at"], reverse=True)
        expired_before = now - timedelta(days=self.retention_days)
        remove_names = set()
        for index, item in enumerate(automatic):
            created_at = _parse_datetime(item["created_at"], now)
            if index >= self.max_auto_backups or created_at < expired_before:
                remove_names.add(item["name"])
        for name in remove_names:
            (self.backup_dir / name).unlink(missing_ok=True)
        return len(remove_names)

    def _backup_path(self, name: str) -> Path:
        if (
            not name
            or Path(name).name != name
            or not name.endswith((".zip", ".chichu-backup"))
        ):
            raise ValueError("备份文件名不合法")
        return self.backup_dir / name

    @contextmanager
    def _lock(self):
        self._ensure_backup_dir()
        lock_path = self.backup_dir / ".backup.lock"
        with lock_path.open("a+") as lock_file:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                self._cleanup_plaintext_temporaries_unlocked()
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _ensure_backup_dir(self) -> None:
        self.backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.backup_dir.is_symlink():
            raise ValueError("备份目录不允许使用符号链接")
        os.chmod(self.backup_dir, 0o700)

    def _cleanup_plaintext_temporaries_unlocked(self) -> None:
        for path in self.backup_dir.glob(".*.zip.tmp"):
            path.unlink(missing_ok=True)


def create_backup_manager(config: Config) -> BackupManager:
    project_dir = Path(__file__).resolve().parents[2]
    workspace_dir = Path(config.housekeeper_memory_path).expanduser().parent
    sources = {
        "project/.env": project_dir / ".env",
        "workspace/MEMORY.md": Path(config.housekeeper_memory_path),
        "workspace/TODO.md": Path(config.housekeeper_todo_path),
        "workspace/STYLE.md": Path(config.style_memory_path),
        "workspace/REMINDER_STATE.md": Path(config.reminder_state_path),
    }
    for filename in (
        "AGENTS.md",
        "SOUL.md",
        "IDENTITY.md",
        "USER.md",
        "TOOLS.md",
        "HEARTBEAT.md",
    ):
        sources["workspace/%s" % filename] = workspace_dir / filename
    if config.enable_sqlite_store:
        sources["workspace/chichu.sqlite3"] = Path(config.state_database_path)
    return BackupManager(
        backup_dir=Path(config.auto_backup_dir),
        sources=sources,
        interval_hours=config.auto_backup_interval_hours,
        retention_days=config.auto_backup_retention_days,
        max_auto_backups=config.auto_backup_max_files,
    )


def _parse_datetime(raw: object, reference: datetime) -> datetime:
    parsed = _parse_iso_datetime(str(raw))
    if parsed.tzinfo is None and reference.tzinfo is not None:
        parsed = parsed.replace(tzinfo=reference.tzinfo)
    return parsed


def _parse_iso_datetime(raw: str) -> datetime:
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    return datetime.fromisoformat(normalized)


def _write_secure_temporary(destination: Path, label: str, content: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.%s-" % (destination.name, label),
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _copy_secure_temporary(destination: Path, label: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".%s.%s-" % (destination.name, label),
        suffix=".tmp",
        dir=str(destination.parent),
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(destination, temporary)
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as source:
            os.fsync(source.fileno())
        return temporary
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _clean_restore_temporaries(destination: Path) -> None:
    for label in ("restore", "rollback"):
        pattern = ".%s.%s-*.tmp" % (destination.name, label)
        for path in destination.parent.glob(pattern):
            path.unlink(missing_ok=True)


def _cleanup_staged_files(staged) -> None:
    for item in staged:
        replacement = item.get("replacement")
        rollback = item.get("rollback")
        if replacement is not None:
            replacement.unlink(missing_ok=True)
        if rollback is not None:
            rollback.unlink(missing_ok=True)


def _read_source_bytes(path: Path, max_bytes: int) -> bytes:
    if max_bytes < 0 or path.is_symlink():
        raise ValueError("受管备份源过大或不安全")
    if path.suffix not in (".sqlite3", ".db"):
        return _read_bounded_path(path, max_bytes)
    with tempfile.TemporaryDirectory(prefix="chichu-sqlite-backup-") as temp_dir:
        snapshot = Path(temp_dir) / path.name
        source_uri = path.resolve().as_uri() + "?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            with sqlite3.connect(str(snapshot)) as destination:
                source.backup(destination)
        return _read_bounded_path(snapshot, max_bytes)


def _read_bounded_path(path: Path, max_bytes: int) -> bytes:
    if max_bytes < 0:
        raise ValueError("备份内容过大")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            content = source.read(max_bytes + 1)
    except OSError:
        raise ValueError("受管备份源无法安全读取") from None
    if len(content) > max_bytes:
        raise ValueError("备份内容过大")
    return content


def _sanitized_env(content: bytes) -> bytes:
    text = content.decode("utf-8")
    output = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key, _ = line.split("=", 1)
            normalized_key = key.strip().upper()
            if normalized_key.endswith(
                ("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET")
            ) or normalized_key in ("API_KEY", "TOKEN", "PASSWORD", "SECRET"):
                line = "%s=" % key
        output.append(line)
    return ("\n".join(output).rstrip("\n") + "\n").encode("utf-8")
