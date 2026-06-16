import unittest

from shared.shein_extract import shein_goods_id_from_url


class SheinExtractTests(unittest.TestCase):
    def test_goods_id_from_p_url(self):
        url = (
            "https://za.shein.com/Some-Product-p-464302913.html"
        )
        self.assertEqual(shein_goods_id_from_url(url), "464302913")


if __name__ == "__main__":
    unittest.main()
