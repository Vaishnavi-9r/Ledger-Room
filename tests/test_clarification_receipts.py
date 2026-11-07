from __future__ import annotations

import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from PIL import Image
from sqlalchemy.exc import SQLAlchemyError

from backend.app.models import BankLine, ChaseEvent, ClarificationReceipt, ClarificationRequest
from backend.app.schemas.clarification_receipt import ReceiptExtraction
from backend.app.services.clarification_receipt_service import (
    ClarificationReceiptNotFoundError,
    ReceiptImageValidationError,
    ReceiptPersistenceError,
    ReceiptTooLargeError,
    upload_clarification_receipt as _upload_clarification_receipt,
)
from backend.app.services.invoice_service import MAX_UPLOAD_SIZE
from backend.app.services.receipt_extraction_service import (
    ReceiptExtractionProviderError,
)

TEST_BUSINESS_ID = uuid4()


async def upload_clarification_receipt(db, request_id, original_filename, contents, provider):
    return await _upload_clarification_receipt(
        db, TEST_BUSINESS_ID, request_id, original_filename, contents, provider,
    )


def _jpeg_bytes() -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", (4, 4), "white").save(buffer, format="JPEG")
    return buffer.getvalue()


class _FakeProvider:
    def __init__(self, result=None, error=None):
        self.result = result or ReceiptExtraction(
            merchant_name="Example Market",
            document_type="payment_receipt",
            document_number="R-109",
            document_date=date(2026, 11, 5),
            total_amount_paise=350000,
            payment_method="UPI",
            payment_reference="UPI-551",
            confidence={
                "merchant_name": 0.94,
                "document_type": 0.92,
                "document_number": 0.88,
                "document_date": 0.95,
                "total_amount_paise": 0.99,
                "payment_method": 0.86,
                "payment_reference": 0.9,
            },
        )
        self.error = error
        self.calls = 0

    async def extract(self, image_bytes, media_type):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class _FakeSession:
    def __init__(self, clarification, fail_flush=False):
        self.clarifications = {clarification.id: clarification} if clarification else {}
        self.receipts = []
        self.events = []
        self.invoices = []
        self.fail_flush = fail_flush

    def get(self, model, identity):
        if model is ClarificationRequest:
            return self.clarifications.get(identity)
        return None

    def scalar(self, statement):
        # This service issues a locked select by clarification ID; the fake only
        # has one request and returns it after the initial existence check.
        entity = statement.column_descriptions[0]["entity"]
        if entity is ClarificationRequest:
            return next(iter(self.clarifications.values()), None)
        return None

    def add(self, row):
        if isinstance(row, ClarificationReceipt):
            self.receipts.append(row)
        elif isinstance(row, ChaseEvent):
            self.events.append(row)

    def flush(self):
        if self.fail_flush:
            raise SQLAlchemyError("simulated persistence failure")

    def rollback(self):
        return None

    @contextmanager
    def begin(self):
        statuses = {
            key: (row.status, row.updated_at)
            for key, row in self.clarifications.items()
        }
        receipts_len, events_len = len(self.receipts), len(self.events)
        try:
            yield self
        except Exception:
            for key, values in statuses.items():
                self.clarifications[key].status, self.clarifications[key].updated_at = values
            del self.receipts[receipts_len:]
            del self.events[events_len:]
            raise


def _clarification():
    bank_line = BankLine(
        id=uuid4(),
        transaction_date=date(2026, 11, 5),
        description="UNKNOWN HARDWARE STORE",
        amount_paise=350000,
        currency="INR",
    )
    request = ClarificationRequest(
        id=uuid4(),
        bank_line_id=bank_line.id,
        bank_line=bank_line,
        status="pending",
        question="Please provide the receipt.",
    )
    return request


class ClarificationReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_receipt_upload_is_persisted_and_associated(self):
        request = _clarification()
        session = _FakeSession(request)
        provider = _FakeProvider()
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.services.clarification_receipt_service.RECEIPT_DIRECTORY",
            Path(directory),
        ):
            response = await upload_clarification_receipt(
                session, request.id, "store.jpg", _jpeg_bytes(), provider
            )
            self.assertTrue((Path(directory) / response.stored_filename).is_file())

        self.assertEqual(response.status, "receipt_received")
        self.assertEqual(response.extraction.extraction.total_amount_paise, 350000)
        self.assertEqual(len(session.receipts), 1)
        self.assertEqual(session.receipts[0].clarification_request_id, request.id)
        self.assertEqual(request.status, "receipt_received")
        self.assertEqual(session.events[0].event_type, "clarification_receipt_uploaded")

    async def test_invalid_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.services.clarification_receipt_service.RECEIPT_DIRECTORY",
            Path(directory),
        ):
            with self.assertRaises(ReceiptImageValidationError):
                await upload_clarification_receipt(
                    _FakeSession(_clarification()),
                    _clarification().id,
                    "bad.jpg",
                    b"not an image",
                    _FakeProvider(),
                )
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_oversized_image_is_rejected(self):
        with self.assertRaises(ReceiptTooLargeError):
            await upload_clarification_receipt(
                _FakeSession(_clarification()),
                _clarification().id,
                "large.jpg",
                b"x" * (MAX_UPLOAD_SIZE + 1),
                _FakeProvider(),
            )

    async def test_unknown_clarification_id_returns_not_found(self):
        provider = _FakeProvider()
        with self.assertRaises(ClarificationReceiptNotFoundError):
            await upload_clarification_receipt(
                _FakeSession(None), uuid4(), "store.jpg", _jpeg_bytes(), provider
            )
        self.assertEqual(provider.calls, 0)

    async def test_gemini_failure_preserves_receipt_and_records_outcome(self):
        request = _clarification()
        session = _FakeSession(request)
        provider = _FakeProvider(error=ReceiptExtractionProviderError("provider failed"))
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.services.clarification_receipt_service.RECEIPT_DIRECTORY",
            Path(directory),
        ):
            response = await upload_clarification_receipt(
                session, request.id, "store.jpg", _jpeg_bytes(), provider
            )
            self.assertTrue((Path(directory) / response.stored_filename).exists())
        self.assertEqual(response.extraction.status, "failed")
        self.assertIsNone(response.extraction.extraction)
        self.assertEqual(session.receipts[0].extraction_status, "failed")
        self.assertEqual(session.events[0].details["extraction_status"], "failed")
        self.assertEqual(request.status, "receipt_received")

    async def test_database_failure_rolls_back_and_cleans_up_file(self):
        request = _clarification()
        session = _FakeSession(request, fail_flush=True)
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.services.clarification_receipt_service.RECEIPT_DIRECTORY",
            Path(directory),
        ):
            with self.assertRaises(ReceiptPersistenceError):
                await upload_clarification_receipt(
                    session, request.id, "store.jpg", _jpeg_bytes(), _FakeProvider()
                )
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(request.status, "pending")
        self.assertEqual(session.receipts, [])
        self.assertEqual(session.events, [])

    async def test_payment_receipt_does_not_create_invoice(self):
        request = _clarification()
        session = _FakeSession(request)
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.services.clarification_receipt_service.RECEIPT_DIRECTORY",
            Path(directory),
        ):
            await upload_clarification_receipt(
                session, request.id, "store.jpg", _jpeg_bytes(), _FakeProvider()
            )
        self.assertEqual(session.invoices, [])

    async def test_repeated_uploads_retain_prior_evidence(self):
        request = _clarification()
        session = _FakeSession(request)
        with tempfile.TemporaryDirectory() as directory, patch(
            "backend.app.services.clarification_receipt_service.RECEIPT_DIRECTORY",
            Path(directory),
        ):
            first = await upload_clarification_receipt(
                session, request.id, "first.jpg", _jpeg_bytes(), _FakeProvider()
            )
            second = await upload_clarification_receipt(
                session, request.id, "second.jpg", _jpeg_bytes(), _FakeProvider()
            )
            self.assertNotEqual(first.receipt_id, second.receipt_id)
            self.assertTrue((Path(directory) / first.stored_filename).exists())
            self.assertTrue((Path(directory) / second.stored_filename).exists())
        self.assertEqual(len(session.receipts), 2)
        self.assertEqual(len(session.events), 2)
        self.assertEqual(
            {event.details["receipt_id"] for event in session.events},
            {first.receipt_id, second.receipt_id},
        )


if __name__ == "__main__":
    unittest.main()
