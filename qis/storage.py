"""Content-addressed blob store (sha256) + the files table on top of it, with retention and signed URLs."""
from __future__ import annotations

import hashlib
import hmac
import io
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from .db import DB
from .ids import new_id

log = logging.getLogger("qis.storage")

MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "application/json": "json", "text/plain": "txt"}


class InputError(ValueError):
    """A client-supplied file is unusable (too big, not an image...)."""


@dataclass
class ImageInfo:
    data: bytes
    width: int
    height: int
    has_alpha: bool
    mime: str = "image/png"


def normalize_image(data: bytes, max_bytes: int, max_pixels: int) -> ImageInfo:
    """Decode, bound and re-encode an image to a clean PNG (drops metadata, stops polyglots and bombs)."""
    if len(data) > max_bytes:
        raise InputError(f"image is {len(data)} bytes, limit is {max_bytes}")
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if w * h > max_pixels:
                raise InputError(f"image is {w}x{h} = {w * h} pixels, limit is {max_pixels}")
            if im.format not in ("PNG", "JPEG", "WEBP", "BMP", "GIF", "TIFF"):
                raise InputError(f"unsupported image format {im.format}")
            im.load()
            has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
            im = im.convert("RGBA" if has_alpha else "RGB")
            buf = io.BytesIO()
            im.save(buf, "PNG", compress_level=4)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
        raise InputError(f"not a readable image: {e}") from e
    return ImageInfo(buf.getvalue(), w, h, has_alpha)


def image_info(data: bytes) -> tuple[int, int, bool]:
    with Image.open(io.BytesIO(data)) as im:
        return im.width, im.height, im.mode in ("RGBA", "LA", "PA")


def reencode_png(data: bytes) -> ImageInfo:
    """Outputs from ComfyUI: strip the embedded workflow metadata, keep pixels and alpha as-is."""
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        has_alpha = im.mode in ("RGBA", "LA", "PA")
        buf = io.BytesIO()
        im.save(buf, "PNG", compress_level=4)
        return ImageInfo(buf.getvalue(), im.width, im.height, has_alpha)


class Storage:
    def __init__(self, db: DB, root: Path, secret: bytes):
        self.db = db
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.secret = secret

    # ---------- blobs ----------
    def _blob_path(self, sha: str, ext: str) -> Path:
        return self.root / sha[:2] / sha[2:4] / f"{sha}.{ext}"

    def _put_blob(self, data: bytes, ext: str) -> str:
        sha = hashlib.sha256(data).hexdigest()
        row = self.db.one("SELECT path FROM blobs WHERE sha256=?", (sha,))
        if row and Path(row["path"]).exists():
            self.db.execute("UPDATE blobs SET refcount=refcount+1 WHERE sha256=?", (sha,))
            return sha
        path = self._blob_path(sha, ext)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        if row:
            self.db.execute("UPDATE blobs SET path=?, refcount=refcount+1 WHERE sha256=?", (str(path), sha))
        else:
            self.db.insert("blobs", {"sha256": sha, "bytes": len(data), "path": str(path), "refcount": 1, "created_at": time.time()})
        return sha

    # ---------- files ----------
    def put(self, data: bytes, *, mime: str = "image/png", purpose: str = "output", hours: float = 72,
            owner_key_id: str | None = None, job_id: str | None = None, name: str = "",
            width: int | None = None, height: int | None = None, has_alpha: bool | None = None) -> dict:
        sha = self._put_blob(data, MIME_EXT.get(mime, "bin"))
        if mime.startswith("image/") and width is None:
            try:
                width, height, has_alpha = image_info(data)
            except Exception:
                pass
        now = time.time()
        row = {"id": new_id("file"), "sha256": sha, "bytes": len(data), "mime": mime, "width": width, "height": height,
               "has_alpha": int(bool(has_alpha)), "purpose": purpose, "owner_key_id": owner_key_id, "job_id": job_id,
               "name": name, "created_at": now, "expires_at": now + hours * 3600}
        self.db.insert("files", row)
        return row

    def put_image(self, info: ImageInfo, **kw) -> dict:
        return self.put(info.data, mime=info.mime, width=info.width, height=info.height, has_alpha=info.has_alpha, **kw)

    def get(self, file_id: str) -> dict | None:
        return self.db.one("SELECT f.*, b.path FROM files f JOIN blobs b ON b.sha256=f.sha256 WHERE f.id=?", (file_id,))

    def read(self, file_id: str) -> bytes:
        row = self.get(file_id)
        if not row:
            raise KeyError(file_id)
        return Path(row["path"]).read_bytes()

    def set_expiry(self, file_ids: list[str], hours: float, purpose: str | None = None) -> None:
        exp = time.time() + hours * 3600
        for fid in file_ids:
            fields = {"expires_at": exp}
            if purpose:
                fields["purpose"] = purpose
            self.db.update("files", "id", fid, fields)

    def delete(self, file_id: str) -> bool:
        row = self.db.one("SELECT sha256 FROM files WHERE id=?", (file_id,))
        if not row:
            return False
        self.db.execute("DELETE FROM files WHERE id=?", (file_id,))
        self.db.execute("UPDATE blobs SET refcount=MAX(refcount-1, 0) WHERE sha256=?", (row["sha256"],))
        return True

    def cleanup(self, max_bytes: int | None = None) -> dict:
        """Delete expired files, then unreferenced blobs; if still above max_bytes drop the oldest files."""
        now = time.time()
        expired = [r["id"] for r in self.db.all("SELECT id FROM files WHERE expires_at < ?", (now,))]
        for fid in expired:
            self.delete(fid)
        evicted = 0
        if max_bytes is not None:
            total = self.db.scalar("SELECT COALESCE(SUM(bytes),0) FROM blobs WHERE refcount>0") or 0
            if total > max_bytes:
                for r in self.db.all("SELECT id, bytes FROM files ORDER BY created_at"):
                    if total <= max_bytes * 0.9:
                        break
                    self.delete(r["id"])
                    total -= r["bytes"] or 0
                    evicted += 1
        removed = 0
        for b in self.db.all("SELECT sha256, path FROM blobs WHERE refcount<=0"):
            try:
                Path(b["path"]).unlink(missing_ok=True)
            except OSError as e:
                log.warning("cannot delete %s: %s", b["path"], e)
                continue
            self.db.execute("DELETE FROM blobs WHERE sha256=? AND refcount<=0", (b["sha256"],))
            removed += 1
        return {"expired": len(expired), "evicted": evicted, "blobs_removed": removed}

    def usage_bytes(self) -> int:
        return int(self.db.scalar("SELECT COALESCE(SUM(bytes),0) FROM blobs WHERE refcount>0") or 0)

    # ---------- signed URLs ----------
    def sign(self, file_id: str, exp: int) -> str:
        return hmac.new(self.secret, f"file|{file_id}|{exp}".encode(), hashlib.sha256).hexdigest()[:32]

    def verify(self, file_id: str, exp: int, sig: str) -> bool:
        return exp >= time.time() and hmac.compare_digest(self.sign(file_id, exp), sig or "")

    def signed_path(self, file_id: str, ttl_s: int) -> str:
        exp = int(time.time() + ttl_s)
        return f"/v1/files/{file_id}/content?exp={exp}&sig={self.sign(file_id, exp)}"
