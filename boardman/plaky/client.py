import time
from typing import Any, Dict, List, Optional

import httpx

from boardman.plaky.placement import context_board_id, context_group_id
from boardman.settings import settings


async def _request_with_rate_limit_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    headers: Dict[str, str],
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    retries: int = 2,
) -> httpx.Response:
    for attempt in range(retries + 1):
        response = await client.request(
            method=method, url=url, headers=headers, json=json, params=params, timeout=20
        )

        if response.status_code != 429:
            return response

        if attempt == retries:
            return response

        retry_after = response.headers.get("Retry-After")
        wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2
        time.sleep(wait_seconds)

    return response


def _request_sync_with_rate_limit_retry(
    client: httpx.Client,
    method: str,
    url: str,
    headers: Dict[str, str],
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    retries: int = 2,
) -> httpx.Response:
    for attempt in range(retries + 1):
        response = client.request(
            method=method, url=url, headers=headers, json=json, params=params, timeout=20
        )
        if response.status_code != 429:
            return response
        if attempt == retries:
            return response
        retry_after = response.headers.get("Retry-After")
        wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 2
        time.sleep(wait_seconds)
    return response


def _headers(api_key: str) -> Dict[str, str]:
    """Plaky public API uses X-API-Key (see https://docs.plaky.com/)."""
    return {
        "X-API-Key": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _normalize_id(obj: Dict[str, Any]) -> str:
    return str(obj.get("id") or obj.get("_id") or obj.get("boardId") or obj.get("groupId") or "")


def _normalize_name(obj: Dict[str, Any]) -> str:
    return str(obj.get("name") or obj.get("title") or obj.get("label") or "")


def _extract_row_list(payload: Any) -> List[Any]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for k in ("data", "spaces", "boards", "items", "groups", "users", "results", "content", "records"):
        v = payload.get(k)
        if isinstance(v, list):
            return v
    return []


def _payload_has_more(payload: Any) -> bool:
    return isinstance(payload, dict) and payload.get("hasMore") is True


def _groups_from_board_payload(board: Dict[str, Any]) -> List[Dict[str, str]]:
    for key in (
        "groups",
        "sections",
        "columns",
        "boardGroups",
        "swimlanes",
        "groupDefinitions",
        "itemGroups",
        "board_groups",
    ):
        block = board.get(key)
        if not isinstance(block, list) or not block:
            continue
        out: List[Dict[str, str]] = []
        for x in block:
            if isinstance(x, dict) and _normalize_id(x):
                out.append({"id": _normalize_id(x), "name": _normalize_name(x)})
        if out:
            return out
    return []


def _public_api_root_from_base_url(base_url: str) -> Optional[str]:
    """
    Plaky documented base: https://api.plaky.com/v1/public
    Migrate common misconfig (…/v2) to v1/public.
    """
    u = (base_url or "").strip().rstrip("/")
    if "/v1/public" in u:
        i = u.index("/v1/public")
        return u[: i + len("/v1/public")]
    if "api.plaky.com" in u and "/v2" in u:
        return "https://api.plaky.com/v1/public"
    return None


class PlakyClient:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None):
        self.api_key = api_key or settings.plaky_api_key
        self.base_url = base_url or settings.plaky_api_base
        self._client: Optional[httpx.AsyncClient] = None
        self._board_to_space: Dict[str, str] = {}

    def _public_root(self) -> Optional[str]:
        return _public_api_root_from_base_url(self.base_url)

    async def __aenter__(self):
        self._client = httpx.AsyncClient()
        return self

    async def __aexit__(self, *args):
        if self._client:
            await self._client.aclose()

    async def _get_paginated(self, client: httpx.AsyncClient, root: str, path: str) -> List[Dict[str, Any]]:
        page = 1
        accum: List[Dict[str, Any]] = []
        base = root.rstrip("/")
        p = path if path.startswith("/") else f"/{path}"
        while page <= 500:
            url = f"{base}{p}"
            params: Dict[str, Any] = {"page": page, "pageSize": 100}
            response = await _request_with_rate_limit_retry(
                client, "GET", url, headers=_headers(self.api_key), params=params
            )
            if response.status_code != 200:
                break
            try:
                payload = response.json()
            except ValueError:
                break
            rows = [x for x in _extract_row_list(payload) if isinstance(x, dict)]
            accum.extend(rows)
            if not _payload_has_more(payload):
                break
            if not rows:
                break
            page += 1
        return accum

    @staticmethod
    def _payload_item_id(payload: Dict[str, Any]) -> str:
        """Best-effort item id extraction across Plaky response shapes."""
        direct = str(
            payload.get("id")
            or payload.get("itemId")
            or payload.get("taskId")
            or payload.get("_id")
            or ""
        ).strip()
        if direct:
            return direct
        for key in ("item", "task", "data", "result"):
            nested = payload.get(key)
            if isinstance(nested, dict):
                nid = str(
                    nested.get("id")
                    or nested.get("itemId")
                    or nested.get("taskId")
                    or nested.get("_id")
                    or ""
                ).strip()
                if nid:
                    return nid
        return ""

    def _get_paginated_sync(self, client: httpx.Client, root: str, path: str) -> List[Dict[str, Any]]:
        page = 1
        accum: List[Dict[str, Any]] = []
        base = root.rstrip("/")
        p = path if path.startswith("/") else f"/{path}"
        hdr = _headers(self.api_key)
        while page <= 500:
            url = f"{base}{p}"
            params: Dict[str, Any] = {"page": page, "pageSize": 100}
            response = _request_sync_with_rate_limit_retry(
                client, "GET", url, headers=hdr, params=params
            )
            if response.status_code != 200:
                break
            try:
                payload = response.json()
            except ValueError:
                break
            rows = [x for x in _extract_row_list(payload) if isinstance(x, dict)]
            accum.extend(rows)
            if not _payload_has_more(payload):
                break
            if not rows:
                break
            page += 1
        return accum

    async def resolve_space_for_board(self, board_id: str) -> Optional[str]:
        bid = board_id.strip()
        if bid in self._board_to_space:
            return self._board_to_space[bid]
        await self.list_boards()
        return self._board_to_space.get(bid)

    async def list_boards(self) -> Dict[str, Any]:
        """List boards. Uses Plaky v1/public when base URL matches; else legacy /boards paths."""
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing.", "boards": []}

        root = self._public_root()
        if root:
            async with httpx.AsyncClient() as client:
                spaces = await self._get_paginated(client, root, "/spaces")
                boards_out: List[Dict[str, Any]] = []
                for sp in spaces:
                    sid = str(sp.get("id") or "").strip()
                    if not sid:
                        continue
                    bds = await self._get_paginated(client, root, f"/spaces/{sid}/boards")
                    for b in bds:
                        bid = _normalize_id(b)
                        if bid:
                            boards_out.append(
                                {"id": bid, "name": _normalize_name(b), "space_id": sid}
                            )
                            self._board_to_space[bid] = sid
                if boards_out:
                    return {"ok": True, "boards": boards_out, "status": 200}
                last_status = 404
                return {
                    "ok": False,
                    "status": last_status,
                    "message": "Plaky v1/public: no boards returned (check API key and space access).",
                    "boards": [],
                }

        base = self.base_url.rstrip("/")
        last_status = 0
        async with httpx.AsyncClient() as client:
            for path in ("/boards", "/projects"):
                url = f"{base}{path}"
                response = await _request_with_rate_limit_retry(
                    client, "GET", url, headers=_headers(self.api_key)
                )
                last_status = response.status_code
                if response.status_code != 200:
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    continue
                rows = (
                    payload
                    if isinstance(payload, list)
                    else payload.get("boards") or payload.get("projects") or []
                )
                if isinstance(rows, list):
                    boards = [
                        {"id": _normalize_id(x), "name": _normalize_name(x)}
                        for x in rows
                        if isinstance(x, dict) and _normalize_id(x)
                    ]
                    return {"ok": True, "boards": boards, "status": response.status_code}
        return {
            "ok": False,
            "status": last_status,
            "message": "Could not list boards (check PLAKY_API_BASE and API version).",
            "boards": [],
        }

    async def list_workspace_users(self) -> Dict[str, Any]:
        """Plaky v1/public: GET /users (paginated)."""
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing.", "users": []}

        root = self._public_root()
        if not root:
            return {
                "ok": False,
                "status": 400,
                "message": "Workspace user listing requires Plaky v1 public API base (…/v1/public).",
                "users": [],
            }

        async with httpx.AsyncClient() as client:
            rows = await self._get_paginated(client, root, "/users")
        users: List[Dict[str, Any]] = []
        for x in rows:
            if not isinstance(x, dict):
                continue
            uid = str(x.get("id") or x.get("userId") or "").strip()
            if not uid:
                continue
            name = str(x.get("name") or x.get("displayName") or x.get("fullName") or x.get("email") or uid)
            email = x.get("email")
            pe = x.get("primaryEmail")
            gh_login = (
                x.get("githubUsername")
                or x.get("githubLogin")
                or x.get("github_login")
                or x.get("github")
            )
            users.append(
                {
                    "id": uid,
                    "name": name,
                    "email": email if isinstance(email, str) else None,
                    "primaryEmail": pe if isinstance(pe, str) else None,
                    "github_login": str(gh_login).strip() if gh_login else None,
                }
            )
        return {"ok": True, "status": 200, "users": users}

    def list_workspace_users_sync(self) -> Dict[str, Any]:
        """Sync variant for assignment loader (blocking). Same shape as list_workspace_users."""
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing.", "users": []}

        root = self._public_root()
        if not root:
            return {
                "ok": False,
                "status": 400,
                "message": "Workspace user listing requires Plaky v1 public API base (…/v1/public).",
                "users": [],
            }

        with httpx.Client(timeout=30) as client:
            rows = self._get_paginated_sync(client, root, "/users")
        users: List[Dict[str, Any]] = []
        for x in rows:
            if not isinstance(x, dict):
                continue
            uid = str(x.get("id") or x.get("userId") or "").strip()
            if not uid:
                continue
            name = str(x.get("name") or x.get("displayName") or x.get("fullName") or x.get("email") or uid)
            email = x.get("email")
            pe = x.get("primaryEmail")
            gh_login = (
                x.get("githubUsername")
                or x.get("githubLogin")
                or x.get("github_login")
                or x.get("github")
            )
            users.append(
                {
                    "id": uid,
                    "name": name,
                    "email": email if isinstance(email, str) else None,
                    "primaryEmail": pe if isinstance(pe, str) else None,
                    "github_login": str(gh_login).strip() if gh_login else None,
                }
            )
        return {"ok": True, "status": 200, "users": users}

    async def list_groups(self, board_id: str) -> Dict[str, Any]:
        """Groups/sections for a board (from board payload on v1/public)."""
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing.", "groups": []}

        br = await self.get_board(board_id.strip())
        if not br.get("ok"):
            return {
                "ok": False,
                "status": br.get("status", 404),
                "message": br.get("message") or "Could not load board for groups.",
                "groups": [],
            }
        board = br.get("board")
        if not isinstance(board, dict):
            return {"ok": False, "status": 404, "message": "Invalid board payload.", "groups": []}
        groups = _groups_from_board_payload(board)
        if groups:
            return {"ok": True, "groups": groups, "status": 200}

        bid = board_id.strip()
        base = self.base_url.rstrip("/")
        last_status = 404
        async with httpx.AsyncClient() as client:
            candidates = [
                f"{base}/boards/{bid}/groups",
                f"{base}/boards/{bid}/sections",
                f"{base}/groups",
            ]
            for i, url in enumerate(candidates):
                params = None
                if i == 2:
                    params = {"board_id": bid, "boardId": bid}
                response = await _request_with_rate_limit_retry(
                    client, "GET", url, headers=_headers(self.api_key), params=params
                )
                last_status = response.status_code
                if response.status_code != 200:
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    continue
                rows = (
                    payload
                    if isinstance(payload, list)
                    else payload.get("groups") or payload.get("sections") or []
                )
                if isinstance(rows, list):
                    groups = [
                        {"id": _normalize_id(x), "name": _normalize_name(x)}
                        for x in rows
                        if isinstance(x, dict) and _normalize_id(x)
                    ]
                    return {"ok": True, "groups": groups, "status": response.status_code}
        return {
            "ok": False,
            "status": last_status,
            "message": f"Could not list groups for board {bid!r}.",
            "groups": [],
        }

    async def get_board(self, board_id: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing.", "board": None}

        root = self._public_root()
        bid = board_id.strip()
        if root:
            sid = await self.resolve_space_for_board(bid)
            if not sid:
                return {
                    "ok": False,
                    "status": 404,
                    "message": f"Could not resolve space for board_id={bid!r}.",
                    "board": None,
                }
            url = f"{root.rstrip('/')}/spaces/{sid}/boards/{bid}"
            async with httpx.AsyncClient() as client:
                response = await _request_with_rate_limit_retry(
                    client, "GET", url, headers=_headers(self.api_key)
                )
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    return {"ok": True, "status": response.status_code, "board": payload}
            return {
                "ok": False,
                "status": response.status_code,
                "message": f"Could not load board {bid!r}: {response.text[:200]}",
                "board": None,
            }

        base = self.base_url.rstrip("/")
        last_status = 404
        last_snip = ""
        async with httpx.AsyncClient() as client:
            for path in (
                f"/boards/{bid}",
                f"/projects/{bid}",
                f"/boards/{bid}/details",
            ):
                url = f"{base}{path}"
                response = await _request_with_rate_limit_retry(
                    client, "GET", url, headers=_headers(self.api_key)
                )
                last_status = response.status_code
                last_snip = response.text[:200]
                if response.status_code != 200:
                    continue
                try:
                    payload = response.json()
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    return {"ok": True, "status": response.status_code, "board": payload}
        return {
            "ok": False,
            "status": last_status,
            "message": f"Could not load board {bid!r} ({last_status}): {last_snip}",
            "board": None,
        }

    async def list_board_items(
        self,
        board_id: str,
        *,
        max_pages: int = 15,
    ) -> Dict[str, Any]:
        """
        Paginated item list for a board (Plaky v1/public GET .../boards/{id}/items).
        Used for PR↔task fuzzy candidate generation.
        """
        if not self.api_key:
            return {"ok": False, "items": [], "message": "PLAKY_API_KEY is missing."}
        root = self._public_root()
        if not root:
            return {
                "ok": False,
                "items": [],
                "message": "Plaky v1/public base URL required for board item listing.",
            }
        bid = board_id.strip()
        sid = await self.resolve_space_for_board(bid)
        if not sid:
            return {"ok": False, "items": [], "message": "Could not resolve space for board"}
        path = f"/spaces/{sid}/boards/{bid}/items"
        async with httpx.AsyncClient() as client:
            page = 1
            accum: List[Dict[str, Any]] = []
            base = root.rstrip("/")
            p = path if path.startswith("/") else f"/{path}"
            while page <= max_pages:
                url = f"{base}{p}"
                params: Dict[str, Any] = {"page": page, "pageSize": 100}
                response = await _request_with_rate_limit_retry(
                    client, "GET", url, headers=_headers(self.api_key), params=params
                )
                if response.status_code != 200:
                    break
                try:
                    payload = response.json()
                except ValueError:
                    break
                rows = [x for x in _extract_row_list(payload) if isinstance(x, dict)]
                accum.extend(rows)
                if not _payload_has_more(payload):
                    break
                if not rows:
                    break
                page += 1
        return {"ok": True, "items": accum, "status": 200}

    async def _enforce_item_text(
        self,
        *,
        board_id: str,
        item_id: str,
        title: str,
        description: str,
    ) -> Dict[str, Any]:
        root = self._public_root()
        if not root:
            return {"ok": False, "message": "v1/public base URL required"}
        sid = await self.resolve_space_for_board(board_id.strip())
        if not sid:
            return {"ok": False, "message": "Could not resolve space for board"}

        base = f"{root.rstrip('/')}/spaces/{sid}/boards/{board_id.strip()}/items/{item_id.strip()}"
        hdr = _headers(self.api_key)
        bodies: List[Dict[str, Any]] = [
            {"name": title, "description": description},
            {"title": title, "description": description},
            {"item": {"name": title, "description": description}},
            {"item": {"title": title, "description": description}},
            {"fields": {"name": title, "description": description}},
        ]

        async with httpx.AsyncClient() as client:
            for method in ("PATCH", "PUT"):
                for body in bodies:
                    r = await _request_with_rate_limit_retry(
                        client, method, base, headers=hdr, json=body
                    )
                    if r.status_code in (200, 201, 204):
                        return {"ok": True, "status": r.status_code, "mode": f"{method} {list(body.keys())[0]}"}

        # Some boards expose title/description as item fields with board-specific keys.
        title_fields: List[str] = []
        description_fields: List[str] = []
        try:
            from boardman.plaky.board_schema import fetch_board_schema_bundle

            sch = await fetch_board_schema_bundle(board_id.strip())
            normalized = sch.get("normalized") if isinstance(sch, dict) else None
            fields = normalized.get("fields") if isinstance(normalized, dict) else []
            if isinstance(fields, list):
                for f in fields:
                    if not isinstance(f, dict):
                        continue
                    key = str(f.get("key") or "").strip()
                    name = str(f.get("name") or "").strip().lower()
                    if not key:
                        continue
                    if any(tok in name for tok in ("title", "name", "task")):
                        title_fields.append(key)
                    if any(tok in name for tok in ("description", "details", "desc", "summary")):
                        description_fields.append(key)
        except Exception:
            pass

        patch_values: Dict[str, Any] = {"name": title, "title": title, "description": description}
        for k in title_fields:
            patch_values[k] = title
        for k in description_fields:
            patch_values[k] = description

        field_patch = await self.patch_item_field_values(
            board_id.strip(),
            item_id.strip(),
            patch_values,
        )
        if field_patch.get("ok"):
            return {"ok": True, "mode": "field_patch", "field_patch": field_patch}
        return {"ok": False, "field_patch": field_patch}

    async def _create_item_hierarchy(
        self,
        board_id: str,
        group_id: str,
        title: str,
        description: str,
        priority: str,
    ) -> Dict[str, Any]:
        root = self._public_root()
        last_status = 400
        last_snip = ""
        if root:
            sid = await self.resolve_space_for_board(board_id)
            if not sid:
                return {
                    "ok": False,
                    "status": 404,
                    "message": f"Could not resolve space for board_id={board_id!r}.",
                }
            url = f"{root.rstrip('/')}/spaces/{sid}/boards/{board_id}/items"
            title_fields: List[str] = []
            description_fields: List[str] = []
            try:
                from boardman.plaky.board_schema import fetch_board_schema_bundle

                sch = await fetch_board_schema_bundle(board_id.strip())
                normalized = sch.get("normalized") if isinstance(sch, dict) else None
                fields = normalized.get("fields") if isinstance(normalized, dict) else []
                if isinstance(fields, list):
                    for f in fields:
                        if not isinstance(f, dict):
                            continue
                        key = str(f.get("key") or "").strip()
                        name = str(f.get("name") or "").strip().lower()
                        if not key:
                            continue
                        if any(tok in name for tok in ("title", "name", "task")):
                            title_fields.append(key)
                        if any(tok in name for tok in ("description", "details", "desc", "summary")):
                            description_fields.append(key)
            except Exception:
                pass
            text_fields: List[Dict[str, Any]] = []
            for k in title_fields:
                text_fields.append({"itemFieldKey": k, "value": title})
            for k in description_fields:
                text_fields.append({"itemFieldKey": k, "value": description})
            bodies: List[Dict[str, Any]] = [
                {
                    "title": title,
                    "description": description,
                    "groupId": group_id,
                },
                {
                    "title": title,
                    "description": description,
                    "group_id": group_id,
                },
                {
                    "name": title,
                    "description": description,
                    "groupId": group_id,
                },
                {
                    "name": title,
                    "description": description,
                    "group_id": group_id,
                },
                {
                    "groupId": group_id,
                    "itemFields": text_fields,
                },
                {
                    "group_id": group_id,
                    "item_fields": text_fields,
                },
                {
                    "groupId": group_id,
                    "fields": text_fields,
                },
            ]
            async with httpx.AsyncClient() as client:
                for body in bodies:
                    response = await _request_with_rate_limit_retry(
                        client, "POST", url, headers=_headers(self.api_key), json=body
                    )
                    last_status = response.status_code
                    last_snip = response.text[:200]
                    if response.status_code in (200, 201):
                        payload = response.json()
                        task_id = self._payload_item_id(payload)
                        task_url = payload.get("url") or payload.get("taskUrl")
                        out = {
                            "ok": True,
                            "status": response.status_code,
                            "task": payload,
                            "task_id": str(task_id or "").strip() or None,
                            "task_url": task_url,
                        }
                        item_id = str(task_id or "").strip()
                        if not item_id:
                            listed = await self.list_board_items(board_id, max_pages=1)
                            if listed.get("ok"):
                                rows = listed.get("items") or []
                                if isinstance(rows, list):
                                    for row in reversed(rows):
                                        if not isinstance(row, dict):
                                            continue
                                        rid = str(
                                            row.get("id")
                                            or row.get("itemId")
                                            or row.get("taskId")
                                            or row.get("_id")
                                            or ""
                                        ).strip()
                                        rgid = str(
                                            row.get("groupId")
                                            or row.get("group_id")
                                            or (row.get("group") or {}).get("id")
                                            or ""
                                        ).strip()
                                        if rid and (not rgid or rgid == group_id):
                                            item_id = rid
                                            break
                        created_name = str(payload.get("name") or payload.get("title") or "").strip().lower()
                        needs_repair = bool(item_id) and (
                            not created_name
                            or created_name in ("new item", "untitled", "new task")
                            or title.strip().lower() not in created_name
                        )
                        if item_id and (needs_repair or description.strip()):
                            out["text_repair"] = await self._enforce_item_text(
                                board_id=board_id,
                                item_id=item_id,
                                title=title,
                                description=description,
                            )
                        if item_id:
                            out["task_id"] = item_id
                        return out
                    if response.status_code not in (404, 422):
                        break
            return {
                "ok": False,
                "status": last_status,
                "message": f"Plaky item create failed ({last_status}): {last_snip}",
            }

    async def add_item_comment_public(self, board_id: str, item_id: str, text: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "message": "PLAKY_API_KEY is missing."}
        root = self._public_root()
        if not root:
            return {"ok": False, "message": "v1/public base URL required"}
        sid = await self.resolve_space_for_board(board_id.strip())
        if not sid:
            return {"ok": False, "message": "Could not resolve space for board"}
        body = (text or "").strip()
        if not body:
            return {"ok": True, "skipped": True, "message": "empty comment"}
        url = f"{root.rstrip('/')}/spaces/{sid}/boards/{board_id.strip()}/items/{item_id.strip()}/comments"
        payload = {"text": body}
        async with httpx.AsyncClient() as client:
            r = await _request_with_rate_limit_retry(client, "POST", url, headers=_headers(self.api_key), json=payload)
        if r.status_code in (200, 201):
            try:
                cmt = r.json()
            except ValueError:
                cmt = {}
            return {"ok": True, "status": r.status_code, "comment": cmt}
        return {"ok": False, "status": r.status_code, "message": r.text[:300]}

        base = self.base_url.rstrip("/")
        bodies = [
            {
                "board_id": board_id,
                "group_id": group_id,
                "name": title,
                "description": description,
                "priority": priority,
            },
            {
                "boardId": board_id,
                "groupId": group_id,
                "title": title,
                "description": description,
                "priority": priority,
            },
        ]
        async with httpx.AsyncClient() as client:
            for path in ("/items", "/tasks"):
                url = f"{base}{path}"
                for body in bodies:
                    response = await _request_with_rate_limit_retry(
                        client, "POST", url, headers=_headers(self.api_key), json=body
                    )
                    last_status = response.status_code
                    last_snip = response.text[:200]
                    if response.status_code in (200, 201):
                        payload = response.json()
                        task_id = payload.get("id") or payload.get("taskId") or payload.get("itemId")
                        task_url = (
                            payload.get("url")
                            or payload.get("taskUrl")
                            or (f"https://app.plaky.com/task/{task_id}" if task_id else None)
                        )
                        return {"ok": True, "status": response.status_code, "task": payload, "task_url": task_url}
                    if response.status_code not in (404, 422):
                        break
        return {
            "ok": False,
            "status": last_status,
            "message": f"Plaky item create failed ({last_status}): {last_snip}",
        }

    async def get_board_item_public(self, board_id: str, item_id: str) -> Dict[str, Any]:
        """GET item on Plaky v1/public (custom fields + group on full payload)."""
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing.", "item": None}
        root = self._public_root()
        if not root:
            return {"ok": False, "message": "v1/public base URL required", "item": None}
        sid = await self.resolve_space_for_board(board_id.strip())
        if not sid:
            return {"ok": False, "message": "Could not resolve space for board", "item": None}
        url = f"{root.rstrip('/')}/spaces/{sid}/boards/{board_id.strip()}/items/{item_id.strip()}"
        async with httpx.AsyncClient() as client:
            r = await _request_with_rate_limit_retry(client, "GET", url, headers=_headers(self.api_key))
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code, "message": r.text[:300], "item": None}
        try:
            payload = r.json()
        except ValueError:
            return {"ok": False, "message": "Invalid JSON from Plaky", "item": None}
        return {"ok": True, "status": r.status_code, "item": payload if isinstance(payload, dict) else {}}

    async def delete_board_item(self, board_id: str, item_id: str) -> Dict[str, Any]:
        """DELETE an item from a Plaky board via v1/public."""
        if not self.api_key:
            return {"ok": False, "message": "PLAKY_API_KEY is missing."}
        root = self._public_root()
        if not root:
            return {"ok": False, "message": "v1/public base URL required"}
        sid = await self.resolve_space_for_board(board_id.strip())
        if not sid:
            return {"ok": False, "message": "Could not resolve space for board"}
        url = f"{root.rstrip('/')}/spaces/{sid}/boards/{board_id.strip()}/items/{item_id.strip()}"
        async with httpx.AsyncClient() as client:
            r = await _request_with_rate_limit_retry(client, "DELETE", url, headers=_headers(self.api_key))
        if r.status_code in (200, 204):
            return {"ok": True, "status": r.status_code}
        return {"ok": False, "status": r.status_code, "message": r.text[:300]}

    @staticmethod
    def _patch_value_candidates(v: Any) -> List[Any]:
        """
        Plaky PERSON fields expect a structured value (see public API docs), not a bare user-id string.
        Try the caller's value first, then common person-field shapes so status/select fields still work.
        """
        out: List[Any] = [v]
        if isinstance(v, bool):
            return out
        if isinstance(v, int):
            out.append({"users": [{"id": v}], "teams": []})
            return out
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return [v]
            if s.isdigit():
                n = int(s)
                out.append({"users": [{"id": n}], "teams": []})
            out.append({"users": [{"id": s}], "teams": []})
        return out

    async def patch_item_field_values(
        self,
        board_id: str,
        item_id: str,
        values: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Set custom / board field values via Plaky v1/public PATCH .../items/{id}/fields.
        `values` maps itemFieldKey (or field id from board schema) -> value (string, id, or structure API expects).
        """
        if not self.api_key:
            return {"ok": False, "message": "PLAKY_API_KEY is missing."}
        if not values:
            return {"ok": True, "skipped": True, "message": "no field values supplied"}
        root = self._public_root()
        if not root:
            return {"ok": False, "message": "Plaky v1/public base URL required for field patch"}
        sid = await self.resolve_space_for_board(board_id.strip())
        if not sid:
            return {"ok": False, "message": "Could not resolve space for board"}
        base = f"{root.rstrip('/')}/spaces/{sid}/boards/{board_id.strip()}/items/{item_id.strip()}"
        hdr = _headers(self.api_key)

        def _bulk_bodies_for(mapping: Dict[str, Any]) -> List[Dict[str, Any]]:
            entries_kv = [{"key": str(k), "value": val} for k, val in mapping.items()]
            entries_ifk = [{"itemFieldKey": str(k), "value": val} for k, val in mapping.items()]
            return [
                {"fields": entries_ifk},
                {"fieldValues": entries_ifk},
                {"fieldUpdates": entries_ifk},
                {"fields": entries_kv},
                {"updates": entries_ifk},
                dict(mapping),
            ]

        # Bulk: try raw mapping, then a person-shaped mapping (Plaky user ids -> users[]).
        # Do not treat free text (e.g. owner/repo) as a user id — that breaks the second bulk pass.
        def _coerce_person_bulk_value(v: Any) -> Any:
            if isinstance(v, str) and v.strip():
                s = v.strip()
                if "/" in s or "\n" in s:
                    return v
                uid: Any = int(s) if s.isdigit() else s
                return {"users": [{"id": uid}], "teams": []}
            return v

        person_mapping: Dict[str, Any] = {}
        for k, v in values.items():
            person_mapping[k] = _coerce_person_bulk_value(v)

        bulk_bodies: List[Dict[str, Any]] = []
        bulk_bodies.extend(_bulk_bodies_for(values))
        if person_mapping != values:
            bulk_bodies.extend(_bulk_bodies_for(person_mapping))

        async with httpx.AsyncClient() as client:
            for body in bulk_bodies:
                url = f"{base}/fields"
                r = await _request_with_rate_limit_retry(client, "PATCH", url, headers=hdr, json=body)
                if r.status_code in (200, 201, 204):
                    try:
                        parsed = r.json() if r.content else {}
                    except ValueError:
                        parsed = {}
                    return {"ok": True, "status": r.status_code, "mode": "bulk", "response": parsed}

            per_ok: List[str] = []
            per_fail: List[Dict[str, Any]] = []
            for k, v in values.items():
                url_single = f"{base}/fields/{k}"
                last_status = 0
                last_snip = ""
                hit = False
                for val in self._patch_value_candidates(v):
                    bodies: List[Dict[str, Any]] = [
                        {"value": val},
                        {"fieldValue": val},
                        {"selectedValue": val},
                        {"selectedOptionId": val},
                    ]
                    if not isinstance(val, dict):
                        bodies.insert(2, {"text": str(val)})
                    for body in bodies:
                        r = await _request_with_rate_limit_retry(client, "PATCH", url_single, headers=hdr, json=body)
                        last_status = r.status_code
                        last_snip = r.text[:500]
                        if r.status_code in (200, 201, 204):
                            per_ok.append(str(k))
                            hit = True
                            break
                    if hit:
                        break
                if not hit:
                    per_fail.append({"key": k, "status": last_status, "message": last_snip})
            return {
                "ok": len(per_fail) == 0,
                "mode": "per_field",
                "patched_keys": per_ok,
                "failed": per_fail,
            }

    async def create_task(
        self,
        title: str,
        description: str,
        priority: str = "medium",
        *,
        board_id: Optional[str] = None,
        group_id: Optional[str] = None,
        field_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        bid = (board_id or "").strip() or context_board_id()
        gid = (group_id or "").strip() or context_group_id()

        if bid and gid:
            res = await self._create_item_hierarchy(bid, gid, title, description, priority)
            if res.get("ok") and description.strip():
                task = res.get("task") if isinstance(res.get("task"), dict) else {}
                iid = str(
                    res.get("task_id")
                    or task.get("id")
                    or task.get("itemId")
                    or task.get("taskId")
                    or task.get("_id")
                    or ""
                ).strip()
                if iid:
                    res["description_comment"] = await self.add_item_comment_public(
                        bid,
                        iid,
                        f"Description:\n{description.strip()}",
                    )
            if res.get("ok") and field_values:
                task = res.get("task") if isinstance(res.get("task"), dict) else {}
                iid = str(
                    res.get("task_id")
                    or task.get("id")
                    or task.get("itemId")
                    or task.get("taskId")
                    or task.get("_id")
                    or ""
                ).strip()
                if iid:
                    res["field_patch"] = await self.patch_item_field_values(bid, iid, field_values)
                else:
                    res["field_patch"] = {
                        "ok": False,
                        "message": "Created item but could not read id for field patch",
                    }
            return res

        url = f"{self.base_url.rstrip('/')}/tasks"
        body = {"title": title, "description": description, "priority": priority}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "POST", url, headers=_headers(self.api_key), json=body
            )

        if response.status_code in (200, 201):
            payload = response.json()
            task_id = payload.get("id") or payload.get("taskId")
            task_url = (
                payload.get("url")
                or payload.get("taskUrl")
                or (f"https://app.plaky.com/task/{task_id}" if task_id else None)
            )
            return {"ok": True, "status": response.status_code, "task": payload, "task_url": task_url}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {
            "ok": False,
            "status": response.status_code,
            "message": f"Failed to create task ({response.status_code}): {response.text[:200]}",
        }

    async def get_tasks(self, status: str = "open") -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        if self._public_root():
            return {
                "ok": True,
                "status": 200,
                "tasks": [],
                "message": "Global /tasks listing is not on Plaky v1 public API; use board items or match_board.",
            }

        url = f"{self.base_url.rstrip('/')}/tasks"
        params = {"status": status}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "GET", url, headers=_headers(self.api_key), params=params
            )

        if response.status_code == 200:
            payload = response.json()
            tasks: List[Dict[str, Any]] = payload if isinstance(payload, list) else payload.get("tasks", [])
            return {"ok": True, "status": response.status_code, "tasks": tasks}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {
            "ok": False,
            "status": response.status_code,
            "message": f"Failed to fetch tasks ({response.status_code}): {response.text[:200]}",
        }

    async def add_comment(self, task_id: str, body: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url.rstrip('/')}/tasks/{task_id}/comments"
        payload = {"body": body}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "POST", url, headers=_headers(self.api_key), json=payload
            )

        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "comment": response.json()}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to add comment ({response.status_code}): {response.text[:200]}"}

    async def update_task_status(self, task_id: str, status: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url.rstrip('/')}/tasks/{task_id}"
        payload = {"status": status}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "PATCH", url, headers=_headers(self.api_key), json=payload
            )

        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "task": response.json()}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to update task ({response.status_code}): {response.text[:200]}"}

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url.rstrip('/')}/tasks/{task_id}"

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(client, "GET", url, headers=_headers(self.api_key))

        if response.status_code == 200:
            return {"ok": True, "status": response.status_code, "task": response.json()}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to get task ({response.status_code}): {response.text[:200]}"}

    async def update_task_fields(
        self,
        task_id: str,
        *,
        title: Optional[str] = None,
        description: Optional[str] = None,
        priority: Optional[str] = None,
        status: Optional[str] = None,
    ) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        body: Dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if priority is not None:
            body["priority"] = priority
        if status is not None:
            body["status"] = status
        if not body:
            return {"ok": False, "status": 400, "message": "No fields to update."}

        url = f"{self.base_url.rstrip('/')}/tasks/{task_id}"

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "PATCH", url, headers=_headers(self.api_key), json=body
            )

        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "task": response.json()}

        if response.status_code == 429:
            return {"ok": False, "status": 429, "message": "Plaky API rate limited the request."}

        return {"ok": False, "status": response.status_code, "message": f"Failed to patch task ({response.status_code}): {response.text[:200]}"}

    async def create_subtask(self, parent_task_id: str, title: str, description: str = "") -> Dict[str, Any]:
        """Try Plaky subtask endpoint; on 404, add a structured comment as fallback."""
        if not self.api_key:
            return {"ok": False, "status": 400, "message": "PLAKY_API_KEY is missing."}

        url = f"{self.base_url.rstrip('/')}/tasks/{parent_task_id}/subtasks"
        payload = {"title": title, "description": description or ""}

        async with httpx.AsyncClient() as client:
            response = await _request_with_rate_limit_retry(
                client, "POST", url, headers=_headers(self.api_key), json=payload
            )

        if response.status_code in (200, 201):
            return {"ok": True, "status": response.status_code, "subtask": response.json()}

        body = f"**Subtask:** {title}\n{description}".strip()
        return await self.add_comment(parent_task_id, body)
