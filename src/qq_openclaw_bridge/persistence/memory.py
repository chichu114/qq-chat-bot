import json
from typing import Dict, List, Optional

from ..domain.memory2 import (
    build_feature_vector,
    extract_keywords,
    normalize_memory_text,
    rank_memory_records,
)
from .database import SqliteDatabase, sqlite_timestamp


class MemoryRepository:
    def __init__(self, database: SqliteDatabase) -> None:
        self._database = database

    def record_turn(
        self, user_id: int, role: str, content: str, dedupe_key: str
    ) -> Dict[str, object]:
        if role not in ("user", "assistant"):
            raise ValueError("role must be user or assistant")
        now = sqlite_timestamp()
        with self._database.locked(), self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_turns(user_id, role, content, dedupe_key, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO NOTHING
                """,
                (user_id, role, content, dedupe_key, now),
            )
            row = connection.execute(
                """
                SELECT id, user_id, role, content, dedupe_key, created_at
                FROM conversation_turns WHERE dedupe_key=?
                """,
                (dedupe_key,),
            ).fetchone()
        assert row is not None
        return _turn_dict(row)

    def unsummarized_turns(self, user_id: int, limit: int) -> List[Dict[str, object]]:
        with self._database.connect() as connection:
            last_row = connection.execute(
                "SELECT MAX(end_turn_id) FROM conversation_summaries WHERE user_id=?",
                (user_id,),
            ).fetchone()
            last_id = int(last_row[0] or 0)
            rows = connection.execute(
                """
                SELECT id, user_id, role, content, dedupe_key, created_at
                FROM conversation_turns
                WHERE user_id=? AND id>?
                ORDER BY id LIMIT ?
                """,
                (user_id, last_id, limit),
            ).fetchall()
        return [_turn_dict(row) for row in rows]

    def save_summary(
        self,
        user_id: int,
        start_turn_id: int,
        end_turn_id: int,
        content: str,
        model: str,
    ) -> Dict[str, object]:
        keywords = extract_keywords(content)
        vector = build_feature_vector(content)
        now = sqlite_timestamp()
        with self._database.locked(), self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_summaries(
                  user_id, start_turn_id, end_turn_id, content,
                  keywords_json, vector_json, model, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, start_turn_id, end_turn_id) DO UPDATE SET
                  content=excluded.content,
                  keywords_json=excluded.keywords_json,
                  vector_json=excluded.vector_json,
                  model=excluded.model
                """,
                (
                    user_id,
                    start_turn_id,
                    end_turn_id,
                    content,
                    json.dumps(keywords, ensure_ascii=False),
                    json.dumps(vector),
                    model,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT id, user_id, start_turn_id, end_turn_id, content,
                       keywords_json, vector_json, model, created_at
                FROM conversation_summaries
                WHERE user_id=? AND start_turn_id=? AND end_turn_id=?
                """,
                (user_id, start_turn_id, end_turn_id),
            ).fetchone()
        assert row is not None
        return _summary_dict(row)

    def add_candidate(
        self,
        user_id: int,
        layer: str,
        category: str,
        content: str,
        confidence: float,
        source_message_id: Optional[str],
    ) -> Dict[str, object]:
        return self._upsert_fact(
            user_id=user_id,
            layer=layer,
            category=category,
            content=content,
            status="pending",
            confidence=confidence,
            source_kind="automatic",
            source_message_id=source_message_id,
        )

    def add_confirmed_fact(
        self,
        user_id: int,
        layer: str,
        content: str,
        source_kind: str,
        category: str = "explicit",
        source_message_id: Optional[str] = None,
    ) -> Dict[str, object]:
        return self._upsert_fact(
            user_id=user_id,
            layer=layer,
            category=category,
            content=content,
            status="confirmed",
            confidence=1.0,
            source_kind=source_kind,
            source_message_id=source_message_id,
        )

    def _upsert_fact(
        self,
        user_id: int,
        layer: str,
        category: str,
        content: str,
        status: str,
        confidence: float,
        source_kind: str,
        source_message_id: Optional[str],
    ) -> Dict[str, object]:
        if layer not in ("profile", "long_term"):
            raise ValueError("layer must be profile or long_term")
        normalized = normalize_memory_text(content)
        if not normalized:
            raise ValueError("memory content must not be empty")
        now = sqlite_timestamp()
        confirmed_at = now if status == "confirmed" else None
        keywords = extract_keywords(content)
        vector = build_feature_vector(content)
        with self._database.locked(), self._database.connect() as connection:
            existing = connection.execute(
                "SELECT status FROM memory_facts WHERE user_id=? AND normalized_content=?",
                (user_id, normalized),
            ).fetchone()
            effective_status = status
            if existing and existing[0] == "confirmed" and status == "pending":
                effective_status = "confirmed"
                confirmed_at = now
            if existing and existing[0] == "rejected" and status == "pending":
                effective_status = "rejected"
                confirmed_at = None
            connection.execute(
                """
                INSERT INTO memory_facts(
                  user_id, layer, category, content, normalized_content, status,
                  confidence, source_kind, source_message_id, keywords_json,
                  vector_json, created_at, updated_at, confirmed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, normalized_content) DO UPDATE SET
                  layer=excluded.layer,
                  category=excluded.category,
                  content=excluded.content,
                  status=excluded.status,
                  confidence=MAX(memory_facts.confidence, excluded.confidence),
                  source_kind=excluded.source_kind,
                  source_message_id=COALESCE(excluded.source_message_id, memory_facts.source_message_id),
                  keywords_json=excluded.keywords_json,
                  vector_json=excluded.vector_json,
                  updated_at=excluded.updated_at,
                  confirmed_at=COALESCE(excluded.confirmed_at, memory_facts.confirmed_at)
                """,
                (
                    user_id,
                    layer,
                    category,
                    content.strip(),
                    normalized,
                    effective_status,
                    max(0.0, min(1.0, float(confidence))),
                    source_kind,
                    source_message_id,
                    json.dumps(keywords, ensure_ascii=False),
                    json.dumps(vector),
                    now,
                    now,
                    confirmed_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM memory_facts WHERE user_id=? AND normalized_content=?",
                (user_id, normalized),
            ).fetchone()
        assert row is not None
        return _fact_dict(row)

    def confirm_fact(self, user_id: int, fact_id: int) -> Optional[Dict[str, object]]:
        return self._set_fact_status(user_id, fact_id, "confirmed")

    def reject_fact(self, user_id: int, fact_id: int) -> Optional[Dict[str, object]]:
        return self._set_fact_status(user_id, fact_id, "rejected")

    def _set_fact_status(
        self, user_id: int, fact_id: int, status: str
    ) -> Optional[Dict[str, object]]:
        now = sqlite_timestamp()
        confirmed_at = now if status == "confirmed" else None
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE memory_facts
                SET status=?, updated_at=?, confirmed_at=?
                WHERE id=? AND user_id=? AND status='pending'
                """,
                (status, now, confirmed_at, fact_id, user_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM memory_facts WHERE id=? AND user_id=?",
                (fact_id, user_id),
            ).fetchone()
        return _fact_dict(row) if row is not None else None

    def list_facts(
        self, user_id: int, status: Optional[str] = None, limit: int = 100
    ) -> List[Dict[str, object]]:
        where = "WHERE user_id IN (?, 0)"
        parameters: List[object] = [user_id]
        if status is not None:
            where += " AND status=?"
            parameters.append(status)
        parameters.append(limit)
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM memory_facts %s ORDER BY id DESC LIMIT ?" % where,
                tuple(parameters),
            ).fetchall()
        return [_fact_dict(row) for row in rows]

    def search_confirmed(
        self, user_id: int, query: str, limit: int = 6
    ) -> List[Dict[str, object]]:
        candidates = self.list_facts(user_id, status="confirmed", limit=500)
        ranked = rank_memory_records(query, candidates, limit=limit)
        return [item for item in ranked if float(item["relevance"]) >= 0.12]

    def delete_confirmed_content(self, user_id: int, content: str) -> bool:
        normalized = normalize_memory_text(content)
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(
                """
                DELETE FROM memory_facts
                WHERE user_id IN (?, 0) AND normalized_content=? AND status='confirmed'
                """,
                (user_id, normalized),
            )
        return cursor.rowcount > 0

    def clear_confirmed(self, user_id: int) -> int:
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM memory_facts WHERE user_id IN (?, 0) AND status='confirmed'",
                (user_id,),
            )
        return int(cursor.rowcount)

    def recent_summaries(self, user_id: int, limit: int = 20) -> List[Dict[str, object]]:
        with self._database.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, start_turn_id, end_turn_id, content,
                       keywords_json, vector_json, model, created_at
                FROM conversation_summaries
                WHERE user_id=? ORDER BY end_turn_id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return [_summary_dict(row) for row in rows]

    def search_summaries(
        self, user_id: int, query: str, limit: int = 3
    ) -> List[Dict[str, object]]:
        return [
            item
            for item in rank_memory_records(
                query, self.recent_summaries(user_id, 100), limit=limit
            )
            if float(item["relevance"]) >= 0.12
        ]

    def stats(self, user_id: int) -> Dict[str, int]:
        with self._database.connect() as connection:
            turns = connection.execute(
                "SELECT COUNT(*) FROM conversation_turns WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            summaries = connection.execute(
                "SELECT COUNT(*) FROM conversation_summaries WHERE user_id=?", (user_id,)
            ).fetchone()[0]
            rows = connection.execute(
                """
                SELECT status, COUNT(*) FROM memory_facts
                WHERE user_id IN (?, 0) GROUP BY status
                """,
                (user_id,),
            ).fetchall()
        status_counts = {str(status): int(count) for status, count in rows}
        return {
            "turns": int(turns),
            "summaries": int(summaries),
            "pending": status_counts.get("pending", 0),
            "confirmed": status_counts.get("confirmed", 0),
            "rejected": status_counts.get("rejected", 0),
        }

    def start_analysis_run(
        self,
        user_id: int,
        start_turn_id: int,
        end_turn_id: int,
        model: str,
    ) -> int:
        now = sqlite_timestamp()
        with self._database.locked(), self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memory_analysis_runs(
                  user_id, start_turn_id, end_turn_id, status, model,
                  detail, created_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, NULL, ?, ?)
                """,
                (user_id, start_turn_id, end_turn_id, model, now, now),
            )
            return int(cursor.lastrowid)

    def finish_analysis_run(
        self, run_id: int, status: str, detail: Optional[str] = None
    ) -> None:
        with self._database.locked(), self._database.connect() as connection:
            connection.execute(
                """
                UPDATE memory_analysis_runs
                SET status=?, detail=?, updated_at=? WHERE id=?
                """,
                (status, detail, sqlite_timestamp(), run_id),
            )

    def latest_analysis_run(self, user_id: int) -> Optional[Dict[str, object]]:
        with self._database.connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, start_turn_id, end_turn_id, status,
                       model, detail, created_at, updated_at
                FROM memory_analysis_runs
                WHERE user_id=? ORDER BY id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": int(row[0]),
            "user_id": int(row[1]),
            "start_turn_id": int(row[2]),
            "end_turn_id": int(row[3]),
            "status": str(row[4]),
            "model": str(row[5]),
            "detail": row[6],
            "created_at": str(row[7]),
            "updated_at": str(row[8]),
        }


def _turn_dict(row) -> Dict[str, object]:
    return {
        "id": int(row[0]),
        "user_id": int(row[1]),
        "role": str(row[2]),
        "content": str(row[3]),
        "dedupe_key": str(row[4]),
        "created_at": str(row[5]),
    }


def _summary_dict(row) -> Dict[str, object]:
    return {
        "id": int(row[0]),
        "user_id": int(row[1]),
        "start_turn_id": int(row[2]),
        "end_turn_id": int(row[3]),
        "content": str(row[4]),
        "keywords": tuple(json.loads(row[5])),
        "vector": tuple(float(value) for value in json.loads(row[6])),
        "model": str(row[7]),
        "created_at": str(row[8]),
        "layer": "summary",
    }


def _fact_dict(row) -> Dict[str, object]:
    return {
        "id": int(row[0]),
        "user_id": int(row[1]),
        "layer": str(row[2]),
        "category": str(row[3]),
        "content": str(row[4]),
        "normalized_content": str(row[5]),
        "status": str(row[6]),
        "confidence": float(row[7]),
        "source_kind": str(row[8]),
        "source_message_id": row[9],
        "keywords": tuple(json.loads(row[10])),
        "vector": tuple(float(value) for value in json.loads(row[11])),
        "created_at": str(row[12]),
        "updated_at": str(row[13]),
        "confirmed_at": row[14],
    }
