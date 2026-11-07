"""Invoice image validation and storage facade."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4
import warnings

from PIL import Image, UnidentifiedImageError

from .evidence_storage import (
    EvidenceObjectNotFoundError,
    EvidenceStorageError,
    build_evidence_key,
    delete_evidence,
    get_storage,
    read_evidence,
)

MAX_UPLOAD_SIZE = 10 * 1024 * 1024
ALLOWED_FORMATS = {"jpg": "JPEG", "jpeg": "JPEG", "png": "PNG"}
INVOICE_DIRECTORY = Path(__file__).resolve().parents[3] / "data" / "invoices"
MEDIA_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png"}


class InvoiceImageError(Exception):
    """Raised when uploaded bytes are not a valid image."""


class InvoiceFormatError(Exception):
    """Raised when the image format is unsupported or mismatches its extension."""


def sanitize_original_filename(original_filename: str) -> str:
    """Retain a bounded basename only; never persist path components/control chars."""
    name = (original_filename or "").replace("\\", "/").split("/")[-1]
    name = "".join(char for char in name if char >= " " and char not in {"\x7f", "\x00"})
    return name[:512] or "uploaded-image"


def validate_image_upload(contents: bytes, original_filename: str, declared_mime: str | None) -> tuple[str, str, str]:
    safe_name = sanitize_original_filename(original_filename)
    extension = safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if extension not in ALLOWED_FORMATS:
        raise InvoiceFormatError("Unsupported file extension. Upload a JPG, JPEG, or PNG image.")
    expected_mime = MEDIA_TYPES[extension]
    if declared_mime and declared_mime.split(";", 1)[0].strip().lower() != expected_mime:
        raise InvoiceFormatError("The declared MIME type does not match the file extension.")
    _validate_image(contents, extension)
    return safe_name, extension, expected_mime


def _validate_image(contents: bytes, extension: str) -> None:
    expected_format = ALLOWED_FORMATS[extension]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(contents)) as image:
                detected_format = image.format
                image.verify()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning) as exc:
        raise InvoiceImageError("The uploaded file is not a valid image.") from exc

    if detected_format != expected_format:
        raise InvoiceFormatError(
            "The image content does not match its filename extension."
        )


def store_invoice(
    contents: bytes, original_filename: str, extension: str, business_id: UUID,
    content_type: str | None = None,
) -> dict[str, object]:
    """Store an image under an application-generated, tenant-scoped object key."""
    if len(contents) > MAX_UPLOAD_SIZE:
        raise InvoiceImageError("The invoice image exceeds the 10 MB upload limit.")
    safe_name, validated_extension, media_type = validate_image_upload(
        contents, original_filename, content_type or MEDIA_TYPES.get(extension.lower()),
    )
    if extension.lower() != validated_extension:
        raise InvoiceFormatError("The image extension is invalid.")
    invoice_id = str(uuid4())
    stored_filename = f"{invoice_id}.{validated_extension}"
    provider = get_storage()
    key = build_evidence_key(business_id, "invoices", UUID(invoice_id), validated_extension)
    provider.put(key, contents, media_type)

    return {
        "invoice_id": invoice_id,
        "original_filename": safe_name,
        "stored_filename": stored_filename,
        "file_size": len(contents),
        "upload_status": "uploaded",
        "storage_provider": provider.provider,
        "storage_key": key,
        "content_type": media_type,
    }


class InvoiceNotFoundError(Exception):
    """Raised when an invoice ID does not identify a stored invoice image."""


def get_invoice_path(invoice_id: str, stored_filename: str) -> Path:
    """Resolve a legacy local object key; cloud objects are streamed by adapter."""

    try:
        canonical_id = str(UUID(invoice_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvoiceNotFoundError("Invoice not found.") from exc

    root = INVOICE_DIRECTORY.resolve()
    if Path(stored_filename).name != stored_filename or Path(stored_filename).stem != canonical_id:
        raise InvoiceNotFoundError("Invoice not found.")
    stored_path = (INVOICE_DIRECTORY / stored_filename).resolve()
    if stored_path.parent != root or not stored_path.is_file():
        raise InvoiceNotFoundError("Invoice not found.")
    return stored_path


def remove_invoice_file(invoice_id: str, stored_filename: str) -> None:
    """Compatibility cleanup for a newly written legacy local filename."""
    get_invoice_path(invoice_id, stored_filename).unlink(missing_ok=True)


def read_invoice_evidence(storage_provider: str, storage_key: str) -> bytes:
    try:
        return read_evidence(storage_provider, storage_key)
    except EvidenceObjectNotFoundError as exc:
        raise InvoiceNotFoundError("Invoice image not found.") from exc


def remove_evidence(storage_provider: str, storage_key: str) -> None:
    delete_evidence(storage_provider, storage_key)
