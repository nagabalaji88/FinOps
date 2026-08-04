"""Knowledge connectors.

Each connector performs real API calls against the configured system. When the
credentials for a connector are absent, ``sync`` returns ``status="not_configured"``
with the exact settings required -- it never invents documents.
"""

from __future__ import annotations

import abc
import base64
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger
from app.db.models.knowledge import KnowledgeSource
from app.llm.base import http_client
from app.rag.pipeline import pipeline

log = get_logger("connectors")


class Connector(abc.ABC):
    key: str
    label: str
    required_settings: tuple[str, ...] = ()

    @property
    def configured(self) -> bool:
        return all(getattr(settings, name, None) for name in self.required_settings)

    def missing(self) -> list[str]:
        return [n.upper() for n in self.required_settings if not getattr(settings, n, None)]

    @abc.abstractmethod
    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        """Return documents as {external_id, title, content, uri, author, metadata}."""

    async def sync(self, session: AsyncSession, source: KnowledgeSource) -> dict[str, Any]:
        if not self.configured:
            source.status = "not_configured"
            source.last_error = f"Missing configuration: {', '.join(self.missing())}"
            return {"source": source.key, "status": "not_configured", "missing": self.missing()}
        source.status = "syncing"
        try:
            documents = await self.fetch(source)
        except Exception as exc:
            source.status = "error"
            source.last_error = str(exc)[:1000]
            log.warning("connector_sync_failed", connector=self.key, error=str(exc))
            return {"source": source.key, "status": "error", "error": str(exc)}

        ingested = 0
        for doc in documents:
            await pipeline.ingest_document(
                session,
                source=source,
                title=doc["title"],
                content=doc["content"],
                uri=doc.get("uri"),
                external_id=doc.get("external_id"),
                author=doc.get("author"),
                mime_type=doc.get("mime_type", "text/markdown"),
                classification=source.classification,
                metadata=doc.get("metadata") or {},
            )
            ingested += 1
        source.status = "connected"
        source.last_error = None
        source.last_sync_at = datetime.now(UTC)
        return {"source": source.key, "status": "connected", "documents_ingested": ingested}


class JiraConnector(Connector):
    key = "jira"
    label = "Atlassian Jira"
    required_settings = ("jira_base_url", "jira_email", "jira_api_token")

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        jql = (source.config or {}).get("jql", "ORDER BY updated DESC")
        limit = int((source.config or {}).get("limit", 50))
        auth = base64.b64encode(
            f"{settings.jira_email}:{settings.jira_api_token}".encode()
        ).decode()
        resp = await http_client().get(
            f"{settings.jira_base_url.rstrip('/')}/rest/api/3/search",
            params={"jql": jql, "maxResults": limit,
                    "fields": "summary,description,status,assignee,updated,priority,project"},
            headers={"Authorization": f"Basic {auth}", "Accept": "application/json"},
            timeout=60.0,
        )
        resp.raise_for_status()
        issues = resp.json().get("issues", [])
        documents = []
        for issue in issues:
            fields = issue.get("fields") or {}
            description = _adf_to_text(fields.get("description"))
            documents.append({
                "external_id": issue["key"],
                "title": f"{issue['key']}: {fields.get('summary', '')}",
                "content": f"# {fields.get('summary', '')}\n\n"
                           f"Status: {(fields.get('status') or {}).get('name')}\n"
                           f"Priority: {(fields.get('priority') or {}).get('name')}\n"
                           f"Project: {(fields.get('project') or {}).get('name')}\n\n{description}",
                "uri": f"{settings.jira_base_url.rstrip('/')}/browse/{issue['key']}",
                "author": ((fields.get("assignee") or {}).get("displayName")),
                "metadata": {"status": (fields.get("status") or {}).get("name"),
                             "updated": fields.get("updated")},
            })
        return documents


def _adf_to_text(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "\n".join(_adf_to_text(n) for n in node)
    if isinstance(node, dict):
        if node.get("type") == "text":
            return node.get("text", "")
        return _adf_to_text(node.get("content"))
    return ""


class ConfluenceConnector(Connector):
    key = "confluence"
    label = "Atlassian Confluence"
    required_settings = ("confluence_base_url", "confluence_email", "confluence_api_token")

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        cfg = source.config or {}
        auth = base64.b64encode(
            f"{settings.confluence_email}:{settings.confluence_api_token}".encode()
        ).decode()
        params: dict[str, Any] = {"limit": int(cfg.get("limit", 50)),
                                  "expand": "body.storage,version,space"}
        if cfg.get("space_key"):
            params["spaceKey"] = cfg["space_key"]
        resp = await http_client().get(
            f"{settings.confluence_base_url.rstrip('/')}/wiki/rest/api/content",
            params=params, headers={"Authorization": f"Basic {auth}"}, timeout=60.0,
        )
        resp.raise_for_status()
        documents = []
        for page in resp.json().get("results", []):
            html = ((page.get("body") or {}).get("storage") or {}).get("value", "")
            documents.append({
                "external_id": page["id"],
                "title": page.get("title", "Untitled"),
                "content": _strip_html(html),
                "uri": f"{settings.confluence_base_url.rstrip('/')}/wiki"
                       f"{(page.get('_links') or {}).get('webui', '')}",
                "author": ((page.get("version") or {}).get("by") or {}).get("displayName"),
                "metadata": {"space": ((page.get("space") or {}).get("key")),
                             "version": (page.get("version") or {}).get("number")},
            })
        return documents


def _strip_html(html: str) -> str:
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = re.sub(r"<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    import html as html_module

    return re.sub(r"\n{3,}", "\n\n", html_module.unescape(text)).strip()


class SlackConnector(Connector):
    key = "slack"
    label = "Slack"
    required_settings = ("slack_bot_token",)

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        cfg = source.config or {}
        channel = cfg.get("channel_id")
        if not channel:
            raise ValueError("Slack source config requires 'channel_id'")
        resp = await http_client().get(
            "https://slack.com/api/conversations.history",
            params={"channel": channel, "limit": int(cfg.get("limit", 200))},
            headers={"Authorization": f"Bearer {settings.slack_bot_token}"}, timeout=60.0,
        )
        payload = resp.json()
        if not payload.get("ok"):
            raise ValueError(f"Slack API error: {payload.get('error')}")
        messages = payload.get("messages", [])
        transcript = "\n".join(
            f"[{datetime.fromtimestamp(float(m['ts']), UTC).isoformat()}] "
            f"{m.get('user') or m.get('bot_id') or 'unknown'}: {m.get('text', '')}"
            for m in reversed(messages)
        )
        return [{
            "external_id": f"slack-{channel}",
            "title": f"Slack #{cfg.get('channel_name', channel)}",
            "content": transcript,
            "uri": f"https://slack.com/app_redirect?channel={channel}",
            "metadata": {"channel_id": channel, "message_count": len(messages)},
        }]


class SharePointConnector(Connector):
    key = "sharepoint"
    label = "SharePoint / Microsoft 365"
    required_settings = ("msgraph_tenant_id", "msgraph_client_id", "msgraph_client_secret",
                         "sharepoint_site_id")

    async def _token(self) -> str:
        resp = await http_client().post(
            f"https://login.microsoftonline.com/{settings.msgraph_tenant_id}/oauth2/v2.0/token",
            data={"client_id": settings.msgraph_client_id,
                  "client_secret": settings.msgraph_client_secret,
                  "scope": "https://graph.microsoft.com/.default",
                  "grant_type": "client_credentials"},
            timeout=45.0,
        )
        resp.raise_for_status()
        return resp.json()["access_token"]

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        token = await self._token()
        headers = {"Authorization": f"Bearer {token}"}
        resp = await http_client().get(
            f"https://graph.microsoft.com/v1.0/sites/{settings.sharepoint_site_id}/pages",
            headers=headers, timeout=60.0,
        )
        resp.raise_for_status()
        documents = []
        for page in resp.json().get("value", [])[: int((source.config or {}).get("limit", 40))]:
            detail = await http_client().get(
                f"https://graph.microsoft.com/v1.0/sites/{settings.sharepoint_site_id}"
                f"/pages/{page['id']}/microsoft.graph.sitePage?$expand=canvasLayout",
                headers=headers, timeout=60.0,
            )
            body = ""
            if detail.status_code < 400:
                layout = detail.json().get("canvasLayout") or {}
                for section in layout.get("horizontalSections", []):
                    for column in section.get("columns", []):
                        for webpart in column.get("webparts", []):
                            body += _strip_html((webpart.get("innerHtml") or "")) + "\n"
            documents.append({
                "external_id": page["id"],
                "title": page.get("title", "SharePoint page"),
                "content": body or page.get("description", ""),
                "uri": page.get("webUrl"),
                "metadata": {"last_modified": page.get("lastModifiedDateTime")},
            })
        return documents


class TeamsConnector(SharePointConnector):
    key = "teams"
    label = "Microsoft Teams"
    required_settings = ("msgraph_tenant_id", "msgraph_client_id", "msgraph_client_secret")

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        cfg = source.config or {}
        team_id, channel_id = cfg.get("team_id"), cfg.get("channel_id")
        if not (team_id and channel_id):
            raise ValueError("Teams source config requires 'team_id' and 'channel_id'")
        token = await self._token()
        resp = await http_client().get(
            f"https://graph.microsoft.com/v1.0/teams/{team_id}/channels/{channel_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"$top": int(cfg.get("limit", 50))}, timeout=60.0,
        )
        resp.raise_for_status()
        messages = resp.json().get("value", [])
        transcript = "\n".join(
            f"[{m.get('createdDateTime')}] "
            f"{((m.get('from') or {}).get('user') or {}).get('displayName', 'unknown')}: "
            f"{_strip_html((m.get('body') or {}).get('content', ''))}"
            for m in reversed(messages)
        )
        return [{
            "external_id": f"teams-{channel_id}",
            "title": f"Teams channel {cfg.get('channel_name', channel_id)}",
            "content": transcript,
            "metadata": {"team_id": team_id, "channel_id": channel_id,
                         "message_count": len(messages)},
        }]


class GitHubConnector(Connector):
    key = "github"
    label = "GitHub"
    required_settings = ("github_token",)

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        cfg = source.config or {}
        repo = cfg.get("repo")
        if not repo:
            raise ValueError("GitHub source config requires 'repo' (owner/name)")
        paths = cfg.get("paths") or ["README.md", "docs"]
        headers = {"Authorization": f"Bearer {settings.github_token}",
                   "Accept": "application/vnd.github+json"}
        documents: list[dict[str, Any]] = []

        async def read_path(path: str) -> None:
            resp = await http_client().get(
                f"https://api.github.com/repos/{repo}/contents/{path}",
                headers=headers, timeout=45.0,
            )
            if resp.status_code >= 400:
                return
            payload = resp.json()
            entries = payload if isinstance(payload, list) else [payload]
            for entry in entries:
                if entry.get("type") == "dir":
                    await read_path(entry["path"])
                elif entry.get("type") == "file" and entry.get("name", "").endswith(
                    (".md", ".markdown", ".rst", ".txt", ".adoc")
                ):
                    blob = await http_client().get(entry["download_url"], timeout=45.0)
                    if blob.status_code < 400:
                        documents.append({
                            "external_id": f"{repo}:{entry['path']}",
                            "title": f"{repo}/{entry['path']}",
                            "content": blob.text,
                            "uri": entry.get("html_url"),
                            "metadata": {"repo": repo, "path": entry["path"],
                                         "sha": entry.get("sha")},
                        })

        for path in paths:
            await read_path(path)
        return documents


class SqlConnector(Connector):
    key = "sql"
    label = "SQL Database"

    @property
    def configured(self) -> bool:
        return True

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        from sqlalchemy import text

        from app.db.session import SessionFactory

        cfg = source.config or {}
        query = cfg.get("query")
        if not query:
            raise ValueError("SQL source config requires a read-only 'query'")
        lowered = query.strip().lower()
        if not lowered.startswith("select") or ";" in query.strip()[:-1]:
            raise ValueError("Only a single SELECT statement is permitted")
        title_column = cfg.get("title_column", "title")
        content_column = cfg.get("content_column", "content")
        id_column = cfg.get("id_column", "id")
        async with SessionFactory() as session:
            rows = (await session.execute(text(query))).mappings().all()
        return [
            {
                "external_id": str(row.get(id_column, index)),
                "title": str(row.get(title_column, f"Row {index}")),
                "content": str(row.get(content_column, "")),
                "metadata": {k: str(v) for k, v in row.items()
                             if k not in {title_column, content_column}},
            }
            for index, row in enumerate(rows)
        ]


class S3Connector(Connector):
    key = "s3"
    label = "S3 / MinIO Object Store"
    required_settings = ("minio_endpoint", "minio_access_key", "minio_secret_key")

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        import asyncio

        from minio import Minio

        cfg = source.config or {}
        bucket = cfg.get("bucket", settings.minio_bucket)
        prefix = cfg.get("prefix", "")
        client = Minio(settings.minio_endpoint, access_key=settings.minio_access_key,
                       secret_key=settings.minio_secret_key, secure=settings.minio_secure)
        objects = await asyncio.to_thread(
            lambda: list(client.list_objects(bucket, prefix=prefix, recursive=True))
        )
        documents = []
        for obj in objects[: int(cfg.get("limit", 50))]:
            if not obj.object_name.endswith((".md", ".txt", ".json", ".csv", ".log")):
                continue

            def _read(name: str = obj.object_name) -> bytes:
                response = client.get_object(bucket, name)
                try:
                    return response.read()
                finally:
                    response.close()
                    response.release_conn()

            data = await asyncio.to_thread(_read)
            documents.append({
                "external_id": f"{bucket}/{obj.object_name}",
                "title": obj.object_name,
                "content": data.decode("utf-8", errors="replace"),
                "uri": f"s3://{bucket}/{obj.object_name}",
                "metadata": {"bucket": bucket, "size": obj.size,
                             "last_modified": obj.last_modified.isoformat()
                             if obj.last_modified else None},
            })
        return documents


class WebConnector(Connector):
    key = "web"
    label = "Web / Intranet pages"

    @property
    def configured(self) -> bool:
        return True

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        urls = (source.config or {}).get("urls") or []
        if not urls:
            raise ValueError("Web source config requires a 'urls' list")
        documents = []
        for url in urls[:50]:
            resp = await http_client().get(url, timeout=45.0)
            if resp.status_code >= 400:
                continue
            documents.append({
                "external_id": url,
                "title": url,
                "content": _strip_html(resp.text),
                "uri": url,
                "metadata": {"status": resp.status_code,
                             "content_type": resp.headers.get("content-type")},
            })
        return documents


class UploadConnector(Connector):
    key = "upload"
    label = "Direct upload"

    @property
    def configured(self) -> bool:
        return True

    async def fetch(self, source: KnowledgeSource) -> list[dict[str, Any]]:
        return []  # Documents arrive through the upload API, not a pull sync.


CONNECTORS: dict[str, Connector] = {
    c.key: c
    for c in [
        JiraConnector(), ConfluenceConnector(), SlackConnector(), SharePointConnector(),
        TeamsConnector(), GitHubConnector(), SqlConnector(), S3Connector(), WebConnector(),
        UploadConnector(),
    ]
}


def connector_status() -> list[dict[str, Any]]:
    return [
        {"key": c.key, "label": c.label, "configured": c.configured,
         "missing_settings": c.missing()}
        for c in CONNECTORS.values()
    ]
