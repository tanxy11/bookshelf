"""Image upload endpoint for review/note markdown."""

from __future__ import annotations

import io
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile

router = APIRouter(prefix="/api/uploads")

MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_WIDTH = 1600
WEBP_QUALITY = 82
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}


def _get_deps() -> tuple:
    from api.main import _auth, UPLOADS_DIR
    return _auth, UPLOADS_DIR


@router.post("", status_code=201)
async def create_upload(request: Request, file: UploadFile = File(...)) -> dict:
    _auth, uploads_dir = _get_deps()
    _auth(request)

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=422, detail="Empty upload.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="Image exceeds 15 MB limit.")

    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()  # force full decode: validates actual bytes, not the content-type header
    except (UnidentifiedImageError, OSError):
        raise HTTPException(status_code=422, detail="File is not a supported image.")
    if img.format not in ALLOWED_FORMATS:
        raise HTTPException(status_code=422, detail=f"Unsupported image format: {img.format}.")

    # exif_transpose + re-encode also strips EXIF metadata (incl. GPS)
    img = ImageOps.exif_transpose(img)
    if img.width > MAX_WIDTH:
        img.thumbnail((MAX_WIDTH, 100_000), Image.LANCZOS)
    if img.mode not in ("RGB", "RGBA"):
        has_alpha = "A" in img.mode or "transparency" in img.info
        img = img.convert("RGBA" if has_alpha else "RGB")

    name = f"{uuid.uuid4().hex}.webp"
    uploads_dir.mkdir(parents=True, exist_ok=True)
    img.save(uploads_dir / name, "WEBP", quality=WEBP_QUALITY, method=6)
    return {"url": f"/uploads/{name}", "filename": name}
