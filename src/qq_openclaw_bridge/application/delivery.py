import asyncio
import json
import logging
import math
import re
from datetime import datetime
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from ..models import AgentRequest


_OBSERVATION_KIND = "e3_observation"
_FINAL_KINDS = {"reply", "task_final"}
_RESERVED_KINDS = {"task_final", "task_notice", "e3_observation"}
_MAX_OBSERVATION_BYTES = 32768
_TASK_FINAL_ECHO = re.compile(r"^task:([A-Za-z0-9_-]{1,80}):final$")
_TASK_NOTICE_KEY = re.compile(
    r"^task:([A-Za-z0-9_-]{1,80}):notice:(?:1|progress)$"
)


class ReliableDeliveryService:
    """Delivers direct replies or durable Outbox records."""

    def __init__(
        self,
        state_store: Optional[object],
        sender: Callable[..., Awaitable[None]],
        retry_base_seconds: int,
        max_attempts: int,
        logger: Optional[logging.Logger] = None,
        observation_handler: Optional[
            Callable[[AgentRequest, str], Awaitable[None]]
        ] = None,
        now_provider: Optional[Callable[[], datetime]] = None,
        outbox_lease_seconds: int = 300,
        task_notice_interval_seconds: int = 60,
        final_delivery_handler: Optional[
            Callable[[int, str], Awaitable[None]]
        ] = None,
    ) -> None:
        self._state_store = state_store
        self._sender = sender
        self._retry_base_seconds = retry_base_seconds
        self._max_attempts = max_attempts
        self._logger = logger or logging.getLogger("qq_openclaw_bridge")
        self._observation_handler = observation_handler
        self._now_provider = now_provider or (
            lambda: datetime.now(tz=ZoneInfo("UTC"))
        )
        self._outbox_lease_seconds = outbox_lease_seconds
        self._task_notice_interval_seconds = task_notice_interval_seconds
        self._final_delivery_handler = final_delivery_handler

    async def send_reply(
        self, websocket, request: AgentRequest, message: str
    ) -> None:
        echo = "reply:%s" % request.message_id if request.message_id else None
        await self.send(
            websocket,
            request.user_id,
            message,
            echo=echo,
            message_id=request.message_id,
        )

    async def send(
        self,
        websocket,
        user_id: int,
        message: str,
        echo: Optional[str],
        message_id: Optional[str],
        *,
        kind: str = "reply",
        dedupe_key: Optional[str] = None,
        complete_inbound: bool = True,
    ) -> bool:
        if kind in _RESERVED_KINDS:
            raise ValueError("reserved outbox kind requires a controlled API")
        if not isinstance(complete_inbound, bool):
            raise ValueError("complete_inbound must be a boolean")
        if kind == "reply" and not complete_inbound:
            raise ValueError("complete_inbound=False is unsupported for reply rows")
        effective_key = dedupe_key
        if effective_key is None and message_id:
            effective_key = "reply:%s" % message_id
        if self._state_store is None or effective_key is None:
            await self._sender(websocket, user_id, message, echo)
            return True

        row = self._state_store.enqueue_outbox(
            kind=kind,
            user_id=user_id,
            message=message,
            echo=echo,
            dedupe_key=effective_key,
        )
        if row["status"] == "sent":
            return self._existing_sent_row_matches(
                row,
                kind,
                user_id,
                echo,
                effective_key,
                message_id,
                complete_inbound,
            )
        await self.deliver_once(websocket)
        current = self._state_store.get_outbox(effective_key)
        delivered = current is not None and current["status"] == "sent"
        return delivered

    async def send_final(
        self,
        websocket,
        request: AgentRequest,
        message: str,
        *,
        task_id: Optional[str] = None,
    ) -> bool:
        if not request.message_id:
            await self._sender(websocket, request.user_id, message, None)
            if self._observation_handler is not None:
                await self._observation_handler(request, message)
            return True

        final_key = "reply:%s" % request.message_id
        observation_key = "observation:%s" % request.message_id
        observation_message = self._encode_observation(request, message)
        if self._state_store is None:
            await self._sender(websocket, request.user_id, message, final_key)
            if self._observation_handler is not None:
                await self._observation_handler(request, message)
            return True

        if task_id is None:
            row = self._state_store.enqueue_final_with_observation(
                kind="reply",
                user_id=request.user_id,
                message=message,
                dedupe_key=final_key,
                echo=final_key,
                observation_message=observation_message,
                observation_key=observation_key,
                now=self._now_provider(),
            )
        else:
            row = self._state_store.commit_task_final(
                task_id=str(task_id),
                user_id=request.user_id,
                message=message,
                final_key=final_key,
                final_echo="task:%s:final" % task_id,
                observation_message=observation_message,
                observation_key=observation_key,
                now=self._now_provider(),
            )
            if row is None:
                raise asyncio.CancelledError()
            row = self._state_store.get_outbox(final_key)

        if row is not None and row["status"] != "sent":
            await self.deliver_once(websocket)
        current = self._state_store.get_outbox(final_key)
        return current is not None and current["status"] == "sent"

    async def send_task_notice(
        self,
        websocket,
        task_id: str,
        user_id: int,
        message: str,
        *,
        role: str,
    ) -> bool:
        if self._state_store is None:
            await self._sender(websocket, user_id, message, None)
            return True
        row = self._state_store.enqueue_task_notice(
            task_id=str(task_id),
            user_id=user_id,
            message=message,
            role=role,
            now=self._now_provider(),
            interval_seconds=self._task_notice_interval_seconds,
        )
        if row["status"] == "sent":
            return True
        await self.deliver_once(websocket)
        current = self._state_store.get_outbox(str(row["dedupe_key"]))
        return current is not None and current["status"] == "sent"

    async def deliver_once(self, websocket) -> bool:
        if self._state_store is None:
            return False
        row = self._state_store.claim_due_outbox(
            now=self._now_provider(),
            max_attempts=self._max_attempts,
            lease_seconds=self._outbox_lease_seconds,
            task_notice_interval_seconds=self._task_notice_interval_seconds,
        )
        if row is None:
            return False
        if row["kind"] in _RESERVED_KINDS:
            valid = self._state_store.validate_reserved_outbox(int(row["id"]))
            if valid and row["kind"] == _OBSERVATION_KIND:
                try:
                    self._decode_observation(str(row["message"]))
                except (TypeError, ValueError, json.JSONDecodeError):
                    valid = False
            if not valid:
                self._state_store.quarantine_reserved_outbox(
                    int(row["id"]), now=self._now_provider()
                )
                return False
        try:
            if row["kind"] == _OBSERVATION_KIND:
                if self._observation_handler is None:
                    raise RuntimeError("observation handler unavailable")
                request, reply = self._decode_observation(str(row["message"]))
                await self._observation_handler(request, reply)
            else:
                await self._sender(
                    websocket,
                    int(row["user_id"]),
                    str(row["message"]),
                    row.get("echo"),
                )
        except asyncio.CancelledError:
            self._mark_retry(row, "delivery cancelled")
            raise
        except Exception:
            self._mark_retry(row, "delivery failed")
            self._logger.warning(
                "outbox delivery failed: id=%s", row["id"]
            )
            return False

        task_id = (
            self._task_id_from_final(row)
            if row["kind"] == "task_final"
            else None
        )
        notice_task_id = (
            self._task_id_from_notice(row)
            if row["kind"] == "task_notice"
            else None
        )
        dedupe_key = str(row["dedupe_key"])
        ordinary_final = bool(
            row["kind"] == "reply" and dedupe_key.startswith("reply:")
        )
        claim_token = row.get("claim_token")
        if task_id is not None:
            acknowledged = self._state_store.e4_repository.mark_task_final_sent(
                int(row["id"]), claim_token, now=self._now_provider()
            )
        elif notice_task_id is not None:
            acknowledged = self._state_store.e4_repository.mark_task_notice_sent(
                int(row["id"]), claim_token, now=self._now_provider()
            )
        elif ordinary_final:
            acknowledged = self._state_store.mark_final_outbox_sent(
                int(row["id"]), claim_token, now=self._now_provider()
            )
        else:
            acknowledged = self._state_store.mark_outbox_sent(
                int(row["id"]), claim_token, now=self._now_provider()
            )
        if not acknowledged:
            return False
        try:
            self._state_store.set_runtime_metric(
                "last_delivery_success_at",
                self._now_provider().isoformat(timespec="seconds"),
            )
        except Exception:
            self._logger.warning("outbox delivery metric write failed")
        if row["kind"] == "task_final":
            if task_id is None:
                raise ValueError("invalid task final echo")
        if row["kind"] in _FINAL_KINDS:
            await self._complete_final_delivery(row, dedupe_key)
            await self._deliver_unlocked_observation(websocket, dedupe_key)
        return True

    async def _complete_final_delivery(self, row, dedupe_key: str) -> None:
        if self._final_delivery_handler is None:
            return
        prefix, separator, message_id = dedupe_key.partition(":")
        if prefix != "reply" or separator != ":" or not message_id:
            self._logger.warning("final delivery completion key is invalid")
            return
        try:
            await self._final_delivery_handler(int(row["user_id"]), message_id)
        except Exception:
            self._logger.warning("orchestration delivery completion failed")

    async def _deliver_unlocked_observation(self, websocket, final_key: str) -> None:
        observation = self._state_store.get_outbox(
            "observation:%s" % final_key.split(":", 1)[-1]
        )
        if observation is None or observation["status"] != "pending":
            return
        await self.deliver_once(websocket)

    @staticmethod
    def _existing_sent_row_matches(
        row, kind, user_id, echo, dedupe_key, message_id, complete_inbound
    ) -> bool:
        stored_user = row.get("user_id")
        if (
            isinstance(stored_user, bool)
            or not isinstance(stored_user, int)
            or stored_user != user_id
            or row.get("kind") != kind
            or row.get("dedupe_key") != dedupe_key
            or row.get("echo") != echo
        ):
            return False
        if kind == "reply" and complete_inbound:
            return bool(
                message_id
                and dedupe_key == "reply:%s" % message_id
                and echo == dedupe_key
            )
        return True

    def _mark_retry(self, row, detail: str) -> None:
        task_id = None
        if row["kind"] == "task_final":
            task_id = self._task_id_from_final(row)
        elif row["kind"] == "task_notice":
            task_id = self._task_id_from_notice(row)
        if task_id is not None:
            self._state_store.e4_repository.mark_task_outbox_retry(
                int(row["id"]),
                row.get("claim_token"),
                detail,
                self._retry_base_seconds,
                self._max_attempts,
                now=self._now_provider(),
            )
            return
        self._state_store.mark_outbox_retry(
            int(row["id"]),
            row.get("claim_token"),
            detail,
            retry_base_seconds=self._retry_base_seconds,
            max_attempts=self._max_attempts,
            now=self._now_provider(),
        )

    @staticmethod
    def _task_id_from_final(row) -> Optional[str]:
        echo = row.get("echo")
        if not isinstance(echo, str):
            return None
        match = _TASK_FINAL_ECHO.fullmatch(echo)
        return None if match is None else match.group(1)

    @staticmethod
    def _task_id_from_notice(row) -> Optional[str]:
        key = row.get("dedupe_key")
        echo = row.get("echo")
        if not isinstance(key, str) or echo != key:
            return None
        match = _TASK_NOTICE_KEY.fullmatch(key)
        return None if match is None else match.group(1)

    @staticmethod
    def _encode_observation(request: AgentRequest, reply: str) -> str:
        payload = {
            "user_id": request.user_id,
            "message_id": request.message_id,
            "text": request.text,
            "timestamp": request.timestamp,
            "reply": reply,
        }
        ReliableDeliveryService._validate_observation(payload)
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > _MAX_OBSERVATION_BYTES:
            raise ValueError("observation payload is too large")
        return encoded

    @staticmethod
    def _decode_observation(value: str):
        if len(value.encode("utf-8")) > _MAX_OBSERVATION_BYTES:
            raise ValueError("observation payload is too large")
        payload = json.loads(value)
        ReliableDeliveryService._validate_observation(payload)
        return AgentRequest(
            payload["user_id"],
            payload["message_id"],
            payload["text"],
            float(payload["timestamp"]),
        ), payload["reply"]

    @staticmethod
    def _validate_observation(payload) -> None:
        expected = {"user_id", "message_id", "text", "timestamp", "reply"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("invalid observation payload")
        if (
            type(payload["user_id"]) is not int
            or not 0 < payload["user_id"] <= 10**20 - 1
            or not isinstance(payload["message_id"], str)
            or not payload["message_id"]
            or not isinstance(payload["text"], str)
            or not isinstance(payload["reply"], str)
        ):
            raise ValueError("invalid observation payload")
        if (
            type(payload["timestamp"]) not in (int, float)
            or not math.isfinite(payload["timestamp"])
        ):
            raise ValueError("invalid observation payload")
        for key in ("message_id", "text", "reply"):
            if len(payload[key].encode("utf-8")) > _MAX_OBSERVATION_BYTES:
                raise ValueError("invalid observation payload")

    @staticmethod
    def _utc_now_text() -> str:
        return datetime.now(tz=ZoneInfo("UTC")).isoformat(timespec="seconds")
