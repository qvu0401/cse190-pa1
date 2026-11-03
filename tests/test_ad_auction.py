import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError


class AuctionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        source = Path(__file__).resolve().parents[1] / "starter" / "app.py"
        app_file = Path(self.directory.name) / "app.py"
        app_file.write_text(source.read_text())
        spec = importlib.util.spec_from_file_location("test_boogle", app_file)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.client = self.module.app.test_client()

    def tearDown(self):
        self.directory.cleanup()

    def response(self, bid_request):
        def bid(bid_id, price):
            return {
                "id": bid_id, "impid": "1", "price": price,
                "w": 300, "h": 250, "adm": bid_id,
                "burl": f"http://dsp.example/{bid_id}?price=${{AUCTION_PRICE}}",
            }

        return [{
            "id": bid_request["id"], "cur": "USD",
            "seatbid": [{"bid": [bid("winner", 2.4902), bid("second", 2.2923)]}],
        }]

    def test_second_price_uses_other_valid_bid(self):
        calls = []

        class BillingResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def read(self):
                return b""

        def bill(url, timeout):
            calls.append(url)
            return BillingResponse()

        with patch.object(self.module, "collect_bids", side_effect=self.response), \
             patch.object(self.module, "urlopen", side_effect=bill):
            response = self.client.get("/ad")

        self.assertEqual(response.data, b"winner")
        self.assertEqual(calls, ["http://dsp.example/winner?price=2.3023"])

    def test_ad_is_not_shown_when_billing_fails(self):
        with patch.object(self.module, "collect_bids", side_effect=self.response), \
             patch.object(self.module, "urlopen", side_effect=URLError("unreachable")):
            response = self.client.get("/ad")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"")


if __name__ == "__main__":
    unittest.main()
