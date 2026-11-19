import unittest
from monetary_units import pay_scale, convert_pay, audit_flags

class MonetaryIntegrity(unittest.TestCase):
    def test_britam(self):
        self.assertEqual(110347259 * pay_scale(None), 110347259)
        self.assertEqual(86916418 * pay_scale(None), 86916418)
        self.assertEqual(12367500 * pay_scale(None), 12367500)
    def test_explicit_units(self):
        for value, unit in [(86916418,'units'), (86916.418,'thousands'), (86.916418,'millions')]:
            self.assertAlmostEqual(convert_pay(value,unit,'units'),86916418)
    def test_ai_mixed(self):
        self.assertEqual(convert_pay(1000,'thousands','millions'),1)
        with self.assertRaises(ValueError): convert_pay(5,None,'units')
    def test_anomaly(self):
        self.assertIn('pay_exceeds_turnover',audit_flags(110347259,'millions',110347260,41654200000))
        self.assertEqual(audit_flags(110347259,None,110347260,41654200000),[])

if __name__ == '__main__': unittest.main()
