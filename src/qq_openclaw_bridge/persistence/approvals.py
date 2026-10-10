from typing import Dict, List, Optional

from .database import SqliteDatabase


_APPROVAL_COLUMNS = (
    "approval_id",
    "user_id",
    "command",
    "description",
    "status",
    "created_at",
    "expires_at",
)


class ApprovalRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def save(
        self,
        approval_id: str,
        user_id: int,
        command: str,
        description: str,
        expires_at: str,
        created_at: str,
    ) -> None:
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    "DELETE FROM approvals WHERE user_id=? AND status='pending'",
                    (user_id,),
                )
                connection.execute(
                    """
                    INSERT INTO approvals(
                      approval_id, user_id, command, description,
                      status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (
                        approval_id,
                        user_id,
                        command,
                        description,
                        created_at,
                        expires_at,
                    ),
                )
            self._database.harden_files()

    def get_pending(self, user_id: int) -> Optional[Dict[str, object]]:
        with self._database.locked():
            with self._database.connect() as connection:
                row = connection.execute(
                    """
                    SELECT approval_id, user_id, command, description,
                           status, created_at, expires_at
                    FROM approvals
                    WHERE user_id=? AND status='pending'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
        if row is None:
            return None
        return dict(zip(_APPROVAL_COLUMNS, row))

    def list_recent(self, limit: int = 100) -> List[Dict[str, object]]:
        bounded_limit = min(100, max(1, int(limit)))
        with self._database.locked():
            with self._database.connect() as connection:
                rows = connection.execute(
                    """
                    SELECT approval_id, user_id, command, description,
                           status, created_at, expires_at
                    FROM approvals
                    ORDER BY created_at DESC, approval_id DESC
                    LIMIT ?
                    """,
                    (bounded_limit,),
                ).fetchall()
        return [dict(zip(_APPROVAL_COLUMNS, row)) for row in rows]

    def delete(self, approval_id: str) -> None:
        with self._database.locked():
            with self._database.connect() as connection:
                connection.execute(
                    "DELETE FROM approvals WHERE approval_id=?", (approval_id,)
                )
            self._database.harden_files()
