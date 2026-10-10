import hashlib
import ipaddress
import json
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_NoRedirectHandler())


def _request_bytes(request: Request, timeout: float) -> bytes:
    with _NO_REDIRECT_OPENER.open(request, timeout=timeout) as response:
        return response.read()


def _is_loopback_base_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            return False
        hostname = parsed.hostname
        if not hostname or parsed.port is None:
            return False
        if hostname.lower() == "localhost":
            return True
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class NapCatRestarter:
    def __init__(
        self,
        base_url: str,
        config_path: Path,
        timeout_seconds: float = 5.0,
        requester: Optional[Callable[[Request, float], bytes]] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._config_path = Path(config_path).expanduser()
        self._timeout_seconds = timeout_seconds
        self._requester = requester or _request_bytes

    def run(self) -> bool:
        try:
            if not _is_loopback_base_url(self._base_url):
                return False
            config = json.loads(self._config_path.read_text(encoding="utf-8"))
            token = str(config.get("token", "")).strip()
            if not token:
                return False
            token_hash = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
            login = self._post_json(
                "/api/auth/login",
                {"hash": token_hash, "totpCode": ""},
            )
            data = login.get("data")
            if login.get("code") != 0 or not isinstance(data, dict):
                return False
            credential_value = data.get("Credential")
            if not isinstance(credential_value, str):
                return False
            credential = credential_value.strip()
            if not credential:
                return False
            restarted = self._post_json(
                "/api/Process/Restart",
                None,
                authorization="Bearer %s" % credential,
            )
            return restarted.get("code") == 0
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _post_json(
        self,
        path: str,
        payload: Optional[dict],
        authorization: str = "",
    ) -> dict:
        headers = {"Content-Type": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        request = Request(
            self._base_url + path,
            data=json.dumps(payload or {}, separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        raw = self._requester(request, self._timeout_seconds)
        response = json.loads(raw.decode("utf-8"))
        if not isinstance(response, dict):
            raise ValueError("invalid NapCat response")
        return response
