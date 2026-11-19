import unittest
from pay_evidence import resolve_headers, reconcile
class EvidenceTests(unittest.TestCase):
    def test_units(self):
        for header, expected in [("Shs'000 Shs'000",'thousands'), ('KES KES','units'), ('KShs million','millions'), ('KES bn','billions')]:
            self.assertEqual(resolve_headers([header])[0], expected)
    def test_conflict(self):
        self.assertIsNone(resolve_headers(['KES million', "Shs’000"])[0])
    def test_no_guess(self):
        self.assertIsNone(resolve_headers(['Director 94,760', 'Paid KES 100'])[0])
    def test_reconcile(self):
        self.assertTrue(reconcile([100,200],300,'thousands')['within_rounding'])
        self.assertFalse(reconcile([100,200],400,'thousands')['within_rounding'])
        self.assertIsNone(reconcile([100,None],400,'units'))
