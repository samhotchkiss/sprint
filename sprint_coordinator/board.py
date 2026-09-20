from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from sprint_coordinator.util import read_json


class BoardError(RuntimeError):
    pass


class BoardClient:
    """Loopback Sprint HTTP client. Auth comes from server.json and is never logged."""

    supports_idempotent_posts = True

    def __init__(self, board_data_dir: Path, opener=None):
        self.board_data_dir = Path(board_data_dir)
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({}))

    def _info(self) -> dict:
        info = read_json(self.board_data_dir / "server.json")
        if not info:
            raise BoardError("board is not running")
        return info

    def _url(self, path: str) -> str:
        info = self._info()
        port = int(info["port"])
        if not path.startswith("/"):
            path = "/" + path
        return "http://127.0.0.1:%d%s" % (port, path)

    def _headers(self) -> dict:
        info = self._info()
        token = info.get("token")
        if not token:
            token_path = self.board_data_dir / "token"
            try:
                token = token_path.read_text(encoding="utf-8").strip()
            except OSError:
                token = None
        if not token:
            raise BoardError("board token missing")
        return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}

    def request(self, method: str, path: str, body=None, timeout: float = 5.0, idempotency_key: str | None = None) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = self._headers()
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(
            self._url(path), data=data, headers=headers, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                if not raw:
                    return {}
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise BoardError("http_%s" % exc.code) from None
        except urllib.error.URLError:
            raise BoardError("unreachable") from None

    def get(self, path: str, timeout: float = 5.0) -> dict:
        return self.request("GET", path, timeout=timeout)

    def post(self, path: str, body: dict, timeout: float = 5.0,
             idempotency_key: str | None = None) -> dict:
        return self.request("POST", path, body, timeout=timeout,
                            idempotency_key=idempotency_key)

    def events_after(self, after: int, limit: int = 500) -> dict:
        q = urllib.parse.urlencode({"after": int(after), "limit": int(limit)})
        return self.get("/api/events?%s" % q)

    def autoheal(self) -> dict:
        return self.get("/api/autoheal")

    def settings(self) -> dict:
        return self.get("/api/settings")

    def orchestrator_cursor(self) -> dict:
        return self.get("/api/cursors/orchestrator")

    def post_sidebar(self, text: str, detail=None, *, idempotency_key: str | None = None) -> dict:
        body = {"text": text, "actor": "session"}
        if detail is not None:
            body["detail"] = detail
        return self.post("/api/sidebar", body, idempotency_key=idempotency_key)

    def post_card_chat(self, card_num: int, text: str, detail=None, *,
                       idempotency_key: str | None = None) -> dict:
        body = {"text": text, "actor": "session"}
        if detail is not None:
            body["detail"] = detail
        return self.post("/api/cards/%d/chat" % int(card_num), body,
                         idempotency_key=idempotency_key)

    def context(self, destination):
        if destination and destination.startswith('card:'):
            number = int(destination.split(':',1)[1])
            detail = self.get('/api/cards/%d' % number)
            card = detail.get('card') or {}
            return {'card': {k:card.get(k) for k in ('num','title','body','state','question','executor','model','worktree','branch')},
                    'timeline': (detail.get('timeline') or [])[-25:]}
        board = self.get('/api/board')
        return {'cards': [{k:c.get(k) for k in ('num','title','state','executor','model','question')}
                          for c in board.get('cards',[]) if c.get('state') not in ('completed','canceled')],
                'sidebar': board.get('sidebar',[])[-25:]}
