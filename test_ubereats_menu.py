import csv
import json
import tempfile
import unittest
from pathlib import Path

from ubereats_menu import extract_items, write_outputs


class ExtractItemsTests(unittest.TestCase):
    def test_handles_optional_fields_and_varied_text_shapes(self):
        store = {
            "catalogSectionsMap": {
                "main": [
                    {
                        "type": "VERTICAL_GRID",
                        "payload": {
                            "standardItemsPayload": {
                                "title": "Entrees",
                                "catalogItems": [
                                    {
                                        "uuid": "item-1",
                                        "title": {"text": "Bowl"},
                                        "itemDescription": {"text": "Rice\nand beans"},
                                        "price": 1299,
                                        "priceTagline": {"text": "$12.99"},
                                        "isSoldOut": False,
                                    },
                                    {
                                        "title": "Market price",
                                        "priceTagline": {"text": "Price varies"},
                                    },
                                    None,
                                ],
                            }
                        },
                    },
                    "unexpected data",
                ],
                "broken": None,
            }
        }

        items = extract_items(store)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["name"], "Bowl")
        self.assertEqual(items[0]["description"], "Rice and beans")
        self.assertEqual(items[0]["price"], 12.99)
        self.assertIsNone(items[1]["price"])

    def test_malformed_store_returns_no_items(self):
        self.assertEqual(extract_items(None), [])
        self.assertEqual(extract_items({"catalogSectionsMap": []}), [])


class OutputTests(unittest.TestCase):
    def test_unknown_price_is_not_written_as_zero(self):
        items = [{
            "category": "Entrees",
            "name": "Market price",
            "price": None,
            "description": "",
            "image_url": "",
            "sold_out": False,
            "uuid": "",
        }]

        with tempfile.TemporaryDirectory() as directory:
            out_dir = Path(directory)
            write_outputs("Test restaurant", items, out_dir)

            data = json.loads((out_dir / "menu.json").read_text(encoding="utf-8"))
            with (out_dir / "menu.csv").open(encoding="utf-8") as file:
                rows = list(csv.DictReader(file))
            app_text = (out_dir / "menu_for_app.txt").read_text(encoding="utf-8")

        self.assertIsNone(data["items"][0]["price"])
        self.assertEqual(rows[0]["price"], "")
        self.assertIn("Market price", app_text)
        self.assertNotIn("0.00", app_text)


if __name__ == "__main__":
    unittest.main()
