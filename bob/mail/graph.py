"""Microsoft Graph access to Bob's mailbox.

The app registration must NOT hold tenant-wide Mail.* permissions. Grant access to this one
mailbox with Exchange Online RBAC for Applications (see README).
"""

import base64
from datetime import datetime

import httpx

from bob.config import Settings
from bob.mail.source import MailAttachment, MailMessage

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPE = "https://graph.microsoft.com/.default"
FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"


def _credential(settings: Settings):
    from azure.identity import ClientSecretCredential, DefaultAzureCredential

    if settings.graph_client_secret:
        return ClientSecretCredential(
            settings.graph_tenant_id, settings.graph_client_id, settings.graph_client_secret
        )
    return DefaultAzureCredential()


class GraphMailSource:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        self._credential = _credential(settings)
        self._http = http or httpx.Client(timeout=60)
        self._base = f"{GRAPH}/users/{settings.mailbox}"
        self._processed_folder_id: str | None = None

    def _headers(self) -> dict[str, str]:
        token = self._credential.get_token(SCOPE).token
        return {
            "Authorization": f"Bearer {token}",
            "Prefer": 'outlook.body-content-type="text"',
        }

    def _get(self, url: str, **params) -> dict:
        r = self._http.get(url, headers=self._headers(), params=params or None)
        r.raise_for_status()
        return r.json()

    def list_unprocessed(self, limit: int = 25) -> list[MailMessage]:
        data = self._get(
            f"{self._base}/mailFolders/inbox/messages",
            **{
                "$top": str(limit),
                "$orderby": "receivedDateTime asc",
                "$select": (
                    "id,internetMessageId,conversationId,from,subject,body,"
                    "receivedDateTime,internetMessageHeaders"
                ),
            },
        )
        messages = []
        for m in data.get("value", []):
            sender = (m.get("from") or {}).get("emailAddress") or {}
            messages.append(
                MailMessage(
                    id=m["id"],
                    internet_message_id=m.get("internetMessageId"),
                    conversation_id=m.get("conversationId"),
                    sender_address=(sender.get("address") or "").lower(),
                    sender_name=sender.get("name"),
                    subject=m.get("subject") or "",
                    body_text=(m.get("body") or {}).get("content") or "",
                    received_at=datetime.fromisoformat(m["receivedDateTime"]),
                    headers=[
                        (h.get("name", ""), h.get("value", ""))
                        for h in m.get("internetMessageHeaders") or []
                    ],
                )
            )
        return messages

    def get_attachments(self, message_id: str) -> list[MailAttachment]:
        data = self._get(f"{self._base}/messages/{message_id}/attachments")
        result = []
        for a in data.get("value", []):
            if a.get("@odata.type") != FILE_ATTACHMENT:
                continue  # item and reference attachments are not supported yet
            content_type = a.get("contentType") or "application/octet-stream"
            if a.get("isInline") and content_type.startswith("image/"):
                continue  # signature logos and similar
            if a.get("contentBytes"):
                raw = base64.b64decode(a["contentBytes"])
            else:  # large attachments omit contentBytes
                r = self._http.get(
                    f"{self._base}/messages/{message_id}/attachments/{a['id']}/$value",
                    headers=self._headers(),
                )
                r.raise_for_status()
                raw = r.content
            result.append(MailAttachment(a.get("name") or "attachment", content_type, raw))
        return result

    def mark_processed(self, message_id: str) -> None:
        """Move the message out of the inbox so it is not listed again."""
        folder_id = self._ensure_processed_folder()
        r = self._http.post(
            f"{self._base}/messages/{message_id}/move",
            headers=self._headers(),
            json={"destinationId": folder_id},
        )
        r.raise_for_status()

    def _ensure_processed_folder(self) -> str:
        if self._processed_folder_id:
            return self._processed_folder_id
        name = self.settings.processed_folder
        data = self._get(f"{self._base}/mailFolders", **{"$filter": f"displayName eq '{name}'"})
        if data.get("value"):
            self._processed_folder_id = data["value"][0]["id"]
        else:
            r = self._http.post(
                f"{self._base}/mailFolders", headers=self._headers(), json={"displayName": name}
            )
            r.raise_for_status()
            self._processed_folder_id = r.json()["id"]
        return self._processed_folder_id

    def send(self, to: list[str], subject: str, body: str) -> None:
        r = self._http.post(
            f"{self._base}/sendMail",
            headers=self._headers(),
            json={
                "message": {
                    "subject": subject,
                    "body": {"contentType": "Text", "content": body},
                    "toRecipients": [{"emailAddress": {"address": a}} for a in to],
                },
                "saveToSentItems": True,
            },
        )
        r.raise_for_status()
