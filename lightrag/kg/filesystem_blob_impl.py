"""Filesystem-backed :class:`BaseBlobStorage` implementation.

This is the default, single-machine blob store used by LightRAG's multimodal
pipeline to persist the *original bytes* of image inputs (and, later,
audio/video clips). It sits alongside the existing JsonKVStorage /
NetworkXStorage / NanoVectorDBStorage stack and requires **no external
services**.

Storage layout
--------------

Under ``{working_dir}/blobs/{workspace}/``, each blob is stored as two
sibling files at a two-character fanout directory to keep any single
directory small::

    {working_dir}/blobs/{workspace}/{xx}/{blob_id}{ext}
    {working_dir}/blobs/{workspace}/{xx}/{blob_id}.meta.json

where ``xx`` is the first two characters of the sha-like blob id *after*
its type prefix. For example::

    img-ab12cd34...  ->  blobs/{workspace}/ab/img-ab12cd34...png
                     ->  blobs/{workspace}/ab/img-ab12cd34....meta.json

The sidecar JSON always contains::

    {
      "blob_id":     str,
      "namespace":   str,          # storage namespace (e.g. "image_blobs")
      "content_type": str,         # e.g. "image/png"
      "size":        int,          # bytes
      "created_at":  str,          # ISO-8601 UTC
      "extra":       dict,         # user-supplied metadata passed to put()
    }

Backup / migration
------------------
The store is just files on disk. ``rsync``, ``tar``, or a cp -r of the
``blobs/`` directory is a complete, trivially-restorable backup.

Workspace isolation
-------------------
Each workspace lives under its own subdirectory; two LightRAG instances
with different ``workspace`` values never see each other's data even when
pointed at the same ``working_dir``.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, final

from lightrag.base import BaseBlobStorage
from lightrag.utils import logger


# Extra mime-to-extension mappings for content types the stdlib doesn't know
# or maps to a non-canonical extension. Used only when the stdlib's
# ``mimetypes.guess_extension`` returns None or an undesirable result.
_MIME_EXT_OVERRIDES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "image/svg+xml": ".svg",
    "image/heic": ".heic",
    "image/avif": ".avif",
    "application/pdf": ".pdf",
    "application/octet-stream": ".bin",
}


def _extension_for_mime(content_type: str) -> str:
    """Return a filesystem extension for the given MIME type."""
    ct = (content_type or "application/octet-stream").split(";", 1)[0].strip().lower()
    if ct in _MIME_EXT_OVERRIDES:
        return _MIME_EXT_OVERRIDES[ct]
    guessed = mimetypes.guess_extension(ct)
    return guessed or ".bin"


def _safe_fanout(blob_id: str) -> str:
    """Return a 2-char fanout directory name derived from the blob id.

    We skip over any ``<type>-`` prefix (e.g. ``img-``) so that the fanout
    reflects the hash portion rather than the type, which would otherwise
    stack every image in the same subdirectory.
    """
    candidate = blob_id.split("-", 1)[-1] if "-" in blob_id else blob_id
    # Keep only filesystem-safe chars; collapse if the stripped id is too short.
    cleaned = "".join(c for c in candidate if c.isalnum())
    if len(cleaned) >= 2:
        return cleaned[:2].lower()
    return "00"


@final
@dataclass
class FileSystemBlobStorage(BaseBlobStorage):
    """Local-disk blob storage. The default backend for single-machine setups."""

    # ``namespace``, ``workspace``, and ``global_config`` come from StorageNameSpace.

    def __post_init__(self) -> None:
        working_dir = self.global_config["working_dir"]
        # Two levels of subdirectory: blobs/ isolates blob data from KV/graph
        # files; {workspace}/ isolates per-LightRAG-instance data.
        if self.workspace:
            workspace_root = os.path.join(working_dir, "blobs", self.workspace)
        else:
            # Keep the empty-workspace case in its own subdir so that if the
            # caller later sets a workspace, the data doesn't visually mix.
            workspace_root = os.path.join(working_dir, "blobs", "_default")
            self.workspace = self.workspace or ""

        # Per-namespace subdir: e.g. blobs/{workspace}/image_blobs/
        self._root: str = os.path.join(workspace_root, self.namespace)
        # Lock for concurrent put/delete within a single process. Cross-process
        # safety is provided by atomic rename + content-hashed blob ids.
        self._write_lock: asyncio.Lock | None = None

    async def initialize(self) -> None:
        """Create the storage directory if it does not yet exist."""
        os.makedirs(self._root, exist_ok=True)
        self._write_lock = asyncio.Lock()
        logger.info(
            f"[{self.workspace}] FileSystemBlobStorage initialized "
            f"namespace={self.namespace} root={self._root}"
        )

    async def finalize(self) -> None:
        """No-op: all state is already on disk after each ``put``."""
        return None

    async def index_done_callback(self) -> None:
        """No-op: there is no in-memory buffer to flush."""
        return None

    async def drop(self) -> dict[str, str]:
        """Remove every blob + sidecar under this namespace.

        Only removes the namespace subtree — never the shared working_dir
        or sibling namespaces belonging to other storages.
        """
        try:
            if self._write_lock is not None:
                async with self._write_lock:
                    if os.path.isdir(self._root):
                        shutil.rmtree(self._root, ignore_errors=False)
                    os.makedirs(self._root, exist_ok=True)
            else:
                if os.path.isdir(self._root):
                    shutil.rmtree(self._root, ignore_errors=False)
                os.makedirs(self._root, exist_ok=True)
            return {"status": "success", "message": "data dropped"}
        except Exception as e:  # pragma: no cover - defensive
            logger.error(
                f"[{self.workspace}] FileSystemBlobStorage drop failed: {e}"
            )
            return {"status": "error", "message": str(e)}

    # ------------------------------------------------------------------
    # Path helpers (private)
    # ------------------------------------------------------------------
    def _fanout_dir(self, blob_id: str) -> str:
        return os.path.join(self._root, _safe_fanout(blob_id))

    def _blob_path(self, blob_id: str, ext: str) -> str:
        return os.path.join(self._fanout_dir(blob_id), f"{blob_id}{ext}")

    def _meta_path(self, blob_id: str) -> str:
        # Sidecar is always a JSON file next to the blob, suffixed
        # ``.meta.json`` and named from the raw blob id (no content-type
        # extension) so we can find it without knowing the mime type.
        return os.path.join(self._fanout_dir(blob_id), f"{blob_id}.meta.json")

    def _find_existing_blob_path(self, blob_id: str) -> str | None:
        """Locate an existing blob file without knowing its extension.

        Returns the first match under the fanout directory that starts with
        ``{blob_id}`` and is not the sidecar.
        """
        fanout = self._fanout_dir(blob_id)
        if not os.path.isdir(fanout):
            return None
        for name in os.listdir(fanout):
            if name == f"{blob_id}.meta.json":
                continue
            if name.startswith(blob_id):
                return os.path.join(fanout, name)
        return None

    # ------------------------------------------------------------------
    # BaseBlobStorage API
    # ------------------------------------------------------------------
    async def put(
        self,
        blob_id: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        if not blob_id:
            raise ValueError("FileSystemBlobStorage.put: blob_id must be non-empty")
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(
                f"FileSystemBlobStorage.put: data must be bytes, "
                f"got {type(data).__name__}"
            )
        data_bytes = bytes(data)

        ext = _extension_for_mime(content_type)
        fanout = self._fanout_dir(blob_id)
        blob_path = self._blob_path(blob_id, ext)
        meta_path = self._meta_path(blob_id)

        sidecar = {
            "blob_id": blob_id,
            "namespace": self.namespace,
            "workspace": self.workspace,
            "content_type": content_type,
            "size": len(data_bytes),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "extra": metadata or {},
            "filename": os.path.basename(blob_path),
        }

        # Serialize writes for the same blob_id within the process.
        lock = self._write_lock
        if lock is None:
            # Caller forgot to initialize(); fall back to a transient lock.
            lock = asyncio.Lock()

        def _sync_write() -> None:
            os.makedirs(fanout, exist_ok=True)
            # If an older blob exists with a different extension (e.g. the
            # user re-ingested the same id with a different content type),
            # remove the stale file so we don't leak disk space.
            existing = self._find_existing_blob_path(blob_id)
            if existing is not None and existing != blob_path:
                try:
                    os.remove(existing)
                except OSError:  # pragma: no cover - best-effort cleanup
                    pass

            # Atomic write: stage then rename. rename() is atomic on the same
            # filesystem on Windows (when target doesn't exist) and on POSIX.
            tmp_blob = blob_path + ".tmp"
            with open(tmp_blob, "wb") as f:
                f.write(data_bytes)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:  # pragma: no cover
                    pass
            os.replace(tmp_blob, blob_path)

            tmp_meta = meta_path + ".tmp"
            with open(tmp_meta, "w", encoding="utf-8") as f:
                json.dump(sidecar, f, ensure_ascii=False, indent=2)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:  # pragma: no cover
                    pass
            os.replace(tmp_meta, meta_path)

        async with lock:
            await asyncio.to_thread(_sync_write)

        logger.debug(
            f"[{self.workspace}] blob put id={blob_id} size={len(data_bytes)} "
            f"ct={content_type} -> {blob_path}"
        )
        # The "reference" for a filesystem backend is the absolute local path.
        return os.path.abspath(blob_path)

    async def get(self, blob_id: str) -> bytes | None:
        def _sync_read() -> bytes | None:
            path = self._find_existing_blob_path(blob_id)
            if path is None:
                return None
            with open(path, "rb") as f:
                return f.read()

        return await asyncio.to_thread(_sync_read)

    async def get_reference(self, blob_id: str) -> str | None:
        def _sync_ref() -> str | None:
            path = self._find_existing_blob_path(blob_id)
            return os.path.abspath(path) if path else None

        return await asyncio.to_thread(_sync_ref)

    async def get_metadata(self, blob_id: str) -> dict[str, Any] | None:
        def _sync_meta() -> dict[str, Any] | None:
            meta_path = self._meta_path(blob_id)
            if not os.path.isfile(meta_path):
                return None
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:  # pragma: no cover
                logger.warning(
                    f"[{self.workspace}] failed to read sidecar for "
                    f"{blob_id}: {e}"
                )
                return None

        return await asyncio.to_thread(_sync_meta)

    async def exists(self, blob_id: str) -> bool:
        def _sync_exists() -> bool:
            return self._find_existing_blob_path(blob_id) is not None

        return await asyncio.to_thread(_sync_exists)

    async def delete(self, blob_id: str) -> bool:
        lock = self._write_lock or asyncio.Lock()

        def _sync_delete() -> bool:
            removed = False
            path = self._find_existing_blob_path(blob_id)
            if path is not None:
                try:
                    os.remove(path)
                    removed = True
                except OSError as e:  # pragma: no cover
                    logger.warning(
                        f"[{self.workspace}] failed to remove blob {blob_id}: {e}"
                    )
            meta_path = self._meta_path(blob_id)
            if os.path.isfile(meta_path):
                try:
                    os.remove(meta_path)
                    removed = True
                except OSError as e:  # pragma: no cover
                    logger.warning(
                        f"[{self.workspace}] failed to remove sidecar for "
                        f"{blob_id}: {e}"
                    )
            return removed

        async with lock:
            return await asyncio.to_thread(_sync_delete)
