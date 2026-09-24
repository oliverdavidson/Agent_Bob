"""Original documents. Paths are content-addressed (by SHA-256), so writes are idempotent and
an original is never overwritten. In Azure the container has versioning and a retention
policy (see infra/main.bicep)."""

from pathlib import Path
from typing import Protocol

from bob.config import Settings


class Storage(Protocol):
    def put(self, path: str, data: bytes, content_type: str) -> None: ...

    def get(self, path: str) -> bytes: ...


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def put(self, path: str, data: bytes, content_type: str) -> None:
        target = self.root / path
        if target.exists():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def get(self, path: str) -> bytes:
        return (self.root / path).read_bytes()


class BlobStorage:
    def __init__(self, account_url: str, container: str):
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient

        service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        self.container = service.get_container_client(container)

    def put(self, path: str, data: bytes, content_type: str) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        try:
            self.container.upload_blob(
                path, data, overwrite=False, content_settings=ContentSettings(content_type)
            )
        except ResourceExistsError:
            pass

    def get(self, path: str) -> bytes:
        return self.container.download_blob(path).readall()


def make_storage(settings: Settings) -> Storage:
    if settings.blob_account_url:
        return BlobStorage(settings.blob_account_url, settings.blob_container)
    return LocalStorage(settings.local_storage_dir)
