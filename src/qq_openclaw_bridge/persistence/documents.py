import os
from pathlib import Path
from typing import Mapping, Tuple

from .database import SqliteDatabase, sqlite_timestamp


class DocumentRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def ensure(self, documents: Mapping[str, Tuple[Path, str]]) -> None:
        with self._database.locked():
            for name, (path, default) in documents.items():
                self._read_unlocked(name, Path(path), default)

    def read(self, name: str, export_path: Path, default: str) -> str:
        with self._database.locked():
            return self._read_unlocked(name, Path(export_path), default)

    def write(self, name: str, export_path: Path, content: str) -> None:
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO documents(name, content, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                      content=excluded.content,
                      updated_at=excluded.updated_at
                    """,
                    (name, content, sqlite_timestamp()),
                )
            atomic_write(Path(export_path), content)
            self._database.harden_files()

    def _read_unlocked(
        self, name: str, export_path: Path, default: str
    ) -> str:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT content FROM documents WHERE name=?", (name,)
            ).fetchone()
            if row is None:
                try:
                    content = export_path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    content = default
                connection.execute(
                    "INSERT INTO documents(name, content, updated_at) VALUES (?, ?, ?)",
                    (name, content, sqlite_timestamp()),
                )
            else:
                content = str(row[0])

        try:
            exported = export_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            exported = None
        if exported != content:
            atomic_write(export_path, content)
        self._database.harden_files()
        return content


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)
