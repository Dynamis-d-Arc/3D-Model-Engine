"""Upload validation and normalisation: PNG / JPG / PDF -> normalised PNG + thumbnail.

File types are checked by content (magic bytes), never by the user-supplied name.
PDF floor plans are rasterised with PyMuPDF; the page count and chosen page are recorded.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps

MAX_STORED_SIDE = 4096
THUMB_SIDE = 512
PDF_DPI = 200

Image.MAX_IMAGE_PIXELS = 120_000_000  # explicit decompression-bomb limit


class UploadRejected(ValueError):
    pass


def sniff_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.lstrip()[:5] == b"%PDF-":
        return "pdf"
    return None


@dataclass
class NormalizedImage:
    kind: str           # png | jpg | pdf
    width: int
    height: int
    pages: int
    page: int
    normalized: Path    # PNG
    thumbnail: Path     # PNG


def normalize_upload(data: bytes, out_dir: Path, page: int = 0) -> NormalizedImage:
    kind = sniff_type(data[:16])
    if kind is None:
        raise UploadRejected("Unsupported file type: upload a PNG, JPG or PDF.")
    out_dir.mkdir(parents=True, exist_ok=True)
    pages = 1
    try:
        if kind == "pdf":
            import fitz  # PyMuPDF
            with fitz.open(stream=data, filetype="pdf") as doc:
                pages = doc.page_count
                if pages == 0:
                    raise UploadRejected("The PDF has no pages.")
                if not 0 <= page < pages:
                    raise UploadRejected(f"Page {page + 1} does not exist; the PDF has {pages} page(s).")
                pix = doc[page].get_pixmap(dpi=PDF_DPI, alpha=False)
                img = Image.open(io.BytesIO(pix.tobytes("png")))
        else:
            img = Image.open(io.BytesIO(data))
            img.load()
            img = ImageOps.exif_transpose(img)
    except UploadRejected:
        raise
    except Image.DecompressionBombError as exc:
        raise UploadRejected("The image is too large (pixel count).") from exc
    except Exception as exc:  # corrupt files
        raise UploadRejected(f"The file could not be read as a {kind.upper()}: {exc}") from exc
    if img.mode not in ("RGB", "L"):
        background = Image.new("RGB", img.size, "white")
        rgba = img.convert("RGBA")
        background.paste(rgba, mask=rgba.split()[-1])
        img = background
    img = img.convert("RGB")
    if max(img.size) > MAX_STORED_SIDE:
        img.thumbnail((MAX_STORED_SIDE, MAX_STORED_SIDE), Image.Resampling.LANCZOS)
    normalized = out_dir / "normalized.png"
    img.save(normalized, optimize=True)
    thumb = img.copy()
    thumb.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
    thumb_path = out_dir / "thumb.png"
    thumb.save(thumb_path, optimize=True)
    return NormalizedImage(kind, img.width, img.height, pages, page, normalized, thumb_path)


def image_data_url(path: Path, max_side: int, photo: bool = False) -> tuple[str, tuple[int, int]]:
    """Downscaled data URL for model input. Plans stay PNG (crisp lines); photos use JPEG."""
    return pil_data_url(Image.open(path), max_side, photo)


def pil_data_url(img: Image.Image, max_side: int, photo: bool = False) -> tuple[str, tuple[int, int]]:
    img = img.convert("RGB")
    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    if photo:
        img.save(buf, "JPEG", quality=88)
        mime = "image/jpeg"
    else:
        img.save(buf, "PNG", optimize=True)
        mime = "image/png"
    return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}", img.size
