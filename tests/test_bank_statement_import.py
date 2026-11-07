"""Unit tests for bank statement CSV parsing and duplicate fingerprints."""

import unittest
from datetime import date
from types import SimpleNamespace

from backend.app.services.bank_statement_service import (
    BankStatementTransaction,
    deduplicate_transactions,
    parse_bank_statement_csv,
    rupees_to_paise,
)


class BankStatementImportTests(unittest.TestCase):
    def test_valid_rows_convert_rupees_and_apply_debit_credit_signs(self):
        contents = (
            b"transaction_date,description,debit,credit,reference\n"
            b"2026-11-03,SRI VENKATESWARA TRADERS,4731.80,0,TXN1001\n"
            b"2026-11-04,CUSTOMER PAYMENT,0,8000.00,TXN1003\n"
        )

        parsed = parse_bank_statement_csv(contents)

        self.assertEqual(parsed.errors, [])
        self.assertEqual(parsed.total_rows, 2)
        self.assertEqual(parsed.transactions[0].amount_paise, 473180)
        self.assertEqual(parsed.transactions[1].amount_paise, -800000)
        self.assertEqual(parsed.transactions[0].currency, "INR")

    def test_invalid_date_is_reported_with_csv_row_number(self):
        parsed = parse_bank_statement_csv(
            b"transaction_date,description,debit,credit,reference\n"
            b"2026-02-30,SHOP,10.00,0,REF1\n"
        )

        self.assertEqual(parsed.total_rows, 1)
        self.assertTrue(any(error.row_number == 2 for error in parsed.errors))
        self.assertTrue(any("calendar date" in error.message for error in parsed.errors))

    def test_invalid_amounts_are_rejected(self):
        for amount in ("-1.00", "abc", "1.001"):
            with self.subTest(amount=amount):
                with self.assertRaises(ValueError):
                    rupees_to_paise(amount, "debit")

    def test_decimal_conversion_uses_integer_paise(self):
        self.assertEqual(rupees_to_paise("4731.80", "debit"), 473180)
        self.assertIs(type(rupees_to_paise("0.01", "credit")), int)

    def test_both_debit_and_credit_positive_is_rejected(self):
        parsed = parse_bank_statement_csv(
            b"transaction_date,description,debit,credit,reference\n"
            b"2026-11-03,SHOP,10.00,1.00,REF1\n"
        )

        self.assertTrue(
            any("Exactly one" in error.message for error in parsed.errors)
        )

    def test_both_debit_and_credit_zero_is_rejected(self):
        parsed = parse_bank_statement_csv(
            b"transaction_date,description,debit,credit,reference\n"
            b"2026-11-03,SHOP,0,0,REF1\n"
        )

        self.assertTrue(
            any("Exactly one" in error.message for error in parsed.errors)
        )

    def test_fingerprint_deduplicates_batch_and_existing_records(self):
        first = BankStatementTransaction(
            date(2026, 11, 3), "SRI VENKATESWARA TRADERS", 473180, "TXN1001"
        )
        same_fingerprint = BankStatementTransaction(
            date(2026, 11, 3), "sri   venkateswara traders", 473180, "txn1001"
        )
        existing = SimpleNamespace(
            transaction_date=date(2026, 11, 4),
            description="CUSTOMER PAYMENT",
            amount_paise=-800000,
            external_reference="TXN1003",
        )
        already_imported = BankStatementTransaction(
            date(2026, 11, 4), "customer payment", -800000, "txn1003"
        )

        accepted, duplicate_count = deduplicate_transactions(
            [first, same_fingerprint, already_imported], [existing]
        )

        self.assertEqual(accepted, [first])
        self.assertEqual(duplicate_count, 2)

    def test_different_reference_is_a_distinct_fingerprint(self):
        first = BankStatementTransaction(
            date(2026, 11, 3), "SHOP", 10000, "REF1"
        )
        second = BankStatementTransaction(
            date(2026, 11, 3), "SHOP", 10000, "REF2"
        )

        accepted, duplicate_count = deduplicate_transactions([first, second])

        self.assertEqual(accepted, [first, second])
        self.assertEqual(duplicate_count, 0)


if __name__ == "__main__":
    unittest.main()
