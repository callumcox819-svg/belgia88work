import unittest

from services.aqua_keys import aqua_service_for_html_dir, normalize_aqua_service
from services.html_templates import html_subdir_for_service, html_template_path


class AquaHtmlRoutingTests(unittest.TestCase):
    def test_normalize_services(self):
        self.assertEqual(normalize_aqua_service("2dehands.be"), "2dehands_be")
        self.assertEqual(normalize_aqua_service("bpost.be"), "bpost_be")

    def test_html_subdirs(self):
        self.assertEqual(html_subdir_for_service("2dehands_be"), "2dehands_be")
        self.assertEqual(html_subdir_for_service("bpost_be"), "bpost_be")
        self.assertEqual(aqua_service_for_html_dir("bpost_be"), "bpost_be")

    def test_template_paths_exist(self):
        for service, files in (
            ("2dehands_be", ("confirmation.html", "confirmation_new.html", "return.html")),
            ("bpost_be", ("confirmation.html", "return.html")),
        ):
            for name in files:
                p = html_template_path(service, name)
                self.assertIsNotNone(p, msg=f"{service}/{name}")
                self.assertTrue(p.is_file(), msg=f"{service}/{name}")

    def test_service_picks_different_dirs(self):
        go_2d = html_template_path("2dehands_be", "confirmation.html")
        go_bp = html_template_path("bpost_be", "confirmation.html")
        self.assertNotEqual(go_2d, go_bp)


if __name__ == "__main__":
    unittest.main()
