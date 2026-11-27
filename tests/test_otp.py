import unittest

from tests.helpers import ROOT  # noqa: F401
from velkey import otp


class TestTotp(unittest.TestCase):
    def test_rfc6238_sha1(self):
        o = otp.OtpConfig(b"12345678901234567890", digits=8)
        cases = {59: "94287082", 1111111109: "07081804", 1111111111: "14050471",
                 1234567890: "89005924", 2000000000: "69279037"}
        for t, code in cases.items():
            self.assertEqual(o.code(t), code, t)

    def test_six_digits_and_remaining(self):
        o = otp.OtpConfig(b"12345678901234567890")
        self.assertEqual(o.code(59), "287082")
        self.assertEqual(o.remaining(59), 1)

    def test_setup_key_vs_code(self):
        self.assertTrue(otp.looks_like_code("123 456"))
        self.assertFalse(otp.looks_like_code("JBSW Y3DP EHPK 3PXP"))  # a demo setup key

    def test_parse_uri(self):
        cfg = otp.parse_uri("otpauth://totp/Example:alex@example.com?secret=JBSWY3DPEHPK3PXP&period=30&digits=6")
        self.assertEqual(cfg.digits, 6)
        self.assertEqual(cfg.period, 30)
        self.assertEqual(cfg.secret, b"Hello!\xde\xad\xbe\xef")


if __name__ == "__main__":
    unittest.main()
