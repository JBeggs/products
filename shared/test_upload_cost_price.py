import unittest

from shared.upload import _crm_cost_price


class CrmCostPriceTests(unittest.TestCase):
    def test_shein_import_uses_source_price_not_uplifted_cost(self):
        data = {
            "shein_price": 734,
            "cost": 880.8,
        }
        self.assertEqual(_crm_cost_price(data, "shein"), "734")

    def test_non_import_uses_cost(self):
        data = {
            "northernbolt_price": 100,
            "cost": 125,
        }
        self.assertEqual(_crm_cost_price(data, "northernbolt"), "125")

    def test_import_falls_back_to_cost_when_source_price_missing(self):
        data = {"cost": 880.8}
        self.assertEqual(_crm_cost_price(data, "shein"), "880.8")


if __name__ == "__main__":
    unittest.main()
