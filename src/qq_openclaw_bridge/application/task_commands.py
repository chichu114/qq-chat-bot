import re
from typing import Optional

from ..models import AgentRequest


_STATUS = re.compile(r"^任务状态\s+(\S+)$")
_CANCEL = re.compile(r"^取消任务\s+(\S+)$")
_NOT_FOUND = "没找到这项任务。"
_ACTIVE = {"queued", "running"}


class TaskCommandHandler:
    """Local owner-scoped task commands; unmatched text remains available to later handlers."""

    def __init__(self, repository, service, list_limit: int = 10) -> None:
        self._repository = repository
        self._service = service
        self._list_limit = max(1, int(list_limit))

    def handle(self, request: AgentRequest) -> Optional[str]:
        text = request.text.strip()
        if text == "查看任务":
            return self._list(request.user_id)
        match = _STATUS.match(text)
        if match is not None:
            return self._status(request.user_id, match.group(1))
        match = _CANCEL.match(text)
        if match is not None:
            return self._cancel(request.user_id, match.group(1))
        return None

    def _list(self, user_id: int) -> str:
        tasks = self._repository.list_tasks(user_id=user_id, limit=self._list_limit)
        if not tasks:
            return "还没有可查看的任务。"
        return "\n".join(
            "#%s %s/%s" % (task["id"], task["status"], task["stage"])
            for task in tasks
        )

    def _status(self, user_id: int, task_id: str) -> str:
        task = self._repository.get_task(task_id, user_id=user_id)
        if task is None:
            return _NOT_FOUND
        return "任务 #%s：%s/%s。" % (task["id"], task["status"], task["stage"])

    def _cancel(self, user_id: int, task_id: str) -> str:
        task = self._repository.get_task(task_id, user_id=user_id)
        if task is None or task["status"] not in _ACTIVE:
            return _NOT_FOUND
        if not self._service.cancel(str(task["id"]), user_id):
            return _NOT_FOUND
        return "任务 #%s 已请求取消。" % task["id"]


__all__ = ["TaskCommandHandler"]
