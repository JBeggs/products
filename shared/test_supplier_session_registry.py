import unittest

from shared.supplier_session_registry import (
    CDP_SUPPLIERS,
    SessionKind,
    build_registry,
    cdp_entries,
    session_backed_entries,
)
from shared.suppliers import SUPPLIERS


class SupplierSessionRegistryTests(unittest.TestCase):
    def test_registry_has_all_suppliers(self):
        registry = build_registry()
        slugs = {e.slug for e in registry}
        self.assertIn("manual", slugs)
        self.assertIn("temu", slugs)
        self.assertIn("junkmail", slugs)
        self.assertIn("shein", slugs)
        self.assertEqual(slugs, set(SUPPLIERS.keys()))
        self.assertEqual(len(registry), len(SUPPLIERS))

    def test_session_backed_count(self):
        backed = session_backed_entries()
        expected_slugs = set(SUPPLIERS.keys()) - {"manual"}
        self.assertEqual({e.slug for e in backed}, expected_slugs)
        self.assertEqual(len(backed), len(expected_slugs))

    def test_cdp_only_two(self):
        cdp = cdp_entries()
        self.assertEqual({e.slug for e in cdp}, set(CDP_SUPPLIERS.keys()))

    def test_manual_is_none(self):
        manual = next(e for e in build_registry() if e.slug == "manual")
        self.assertEqual(manual.kind, SessionKind.NONE)

    def test_gumtree_chrome_profile(self):
        gum = next(e for e in build_registry() if e.slug == "gumtree")
        self.assertEqual(gum.kind, SessionKind.CHROME_PROFILE)

    def test_makro_json(self):
        makro = next(e for e in build_registry() if e.slug == "makro")
        self.assertEqual(makro.kind, SessionKind.JSON)

    def test_aliexpress_has_login_url(self):
        ae = next(e for e in build_registry() if e.slug == "aliexpress")
        self.assertTrue(ae.login_url)
        self.assertIn("aliexpress.com", ae.login_url)

    def test_makro_has_login_url(self):
        makro = next(e for e in build_registry() if e.slug == "makro")
        self.assertTrue(makro.login_url)
        self.assertIn("makro.co.za", makro.login_url)

    def test_takealot_chrome_profile_login_url(self):
        ta = next(e for e in build_registry() if e.slug == "takealot")
        self.assertEqual(ta.kind, SessionKind.CHROME_PROFILE)
        self.assertTrue(ta.login_url)
        self.assertIn("takealot.com", ta.login_url)

    def test_shein_json_login_url(self):
        shein = next(e for e in build_registry() if e.slug == "shein")
        self.assertEqual(shein.kind, SessionKind.JSON)
        self.assertTrue(shein.login_url)
        self.assertIn("shein.com", shein.login_url)


if __name__ == "__main__":
    unittest.main()
