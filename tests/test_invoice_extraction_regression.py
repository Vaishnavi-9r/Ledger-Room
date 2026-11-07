"""Regression coverage for synthetic grocery invoice extraction data."""

import unittest

from backend.app.schemas.extraction import FieldConfidence, InvoiceExtraction
from backend.app.services.extraction_service import validate_extraction_totals


class InvoiceExtractionRegressionTests(unittest.TestCase):
    def test_grocery_invoice_amounts_reconcile_and_missing_confidence_is_uncertain(self):
        extraction = InvoiceExtraction(
            subtotal_paise=401000,
            tax_amount_paise=72180,
            total_amount_paise=473180,
            confidence=FieldConfidence(
                subtotal_paise=0.98,
                tax_amount_paise=None,
                total_amount_paise=0.98,
            ),
        )

        self.assertIs(type(extraction.subtotal_paise), int)
        self.assertIs(type(extraction.tax_amount_paise), int)
        self.assertIs(type(extraction.total_amount_paise), int)
        self.assertEqual(
            extraction.subtotal_paise + extraction.tax_amount_paise,
            extraction.total_amount_paise,
        )
        self.assertIsNone(extraction.confidence.tax_amount_paise)
        self.assertIn("tax_amount_paise", extraction.uncertain_fields)
        self.assertNotIn("subtotal_paise", extraction.uncertain_fields)
        self.assertNotIn("total_amount_paise", extraction.uncertain_fields)
        self.assertEqual(validate_extraction_totals(extraction), [])


if __name__ == "__main__":
    unittest.main()
