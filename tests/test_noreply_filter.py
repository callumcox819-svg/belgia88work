import unittest

from services.incoming_mail_worker import _is_noreply_automated_sender


class TestNoreplyFilter(unittest.TestCase):
    def test_blocks_instagram_noreply(self):
        self.assertTrue(_is_noreply_automated_sender("no-reply@mail.instagram.com"))

    def test_blocks_common_noreply(self):
        for addr in (
            "noreply@google.com",
            "no_reply@shop.example",
            "donotreply@service.io",
            "no-reply+tag@domain.com",
        ):
            self.assertTrue(_is_noreply_automated_sender(addr), addr)

    def test_allows_seller_emails(self):
        for addr in (
            "kurt.seller@gmail.com",
            "info@2dehands-verkoper.be",
            "anna.zehringer@example.com",
            "harriettekukzr073@gmail.com",
        ):
            self.assertFalse(_is_noreply_automated_sender(addr), addr)

    def test_allows_mailer_daemon(self):
        self.assertFalse(_is_noreply_automated_sender("mailer-daemon@gmail.com"))


if __name__ == "__main__":
    unittest.main()
