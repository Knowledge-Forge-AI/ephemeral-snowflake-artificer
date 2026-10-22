import unittest

import _util  # noqa: F401  (adds src to sys.path)
from esa import strictjson


class StrictJSONTest(unittest.TestCase):
    def refused(self, data: bytes, fragment: str):
        with self.assertRaises(strictjson.StrictJSONError) as ctx:
            strictjson.loads(data)
        self.assertIn(fragment, str(ctx.exception))

    def test_accepts_plain_object(self):
        self.assertEqual(strictjson.loads(b'{"a": 1, "b": [true, null, "x"]}'),
                         {"a": 1, "b": [True, None, "x"]})

    def test_duplicate_key(self):
        self.refused(b'{"a": 1, "a": 2}', "duplicate key")

    def test_nested_duplicate_key(self):
        self.refused(b'{"a": {"b": 1, "b": 1}}', "duplicate key")

    def test_nan_and_infinity(self):
        for text in (b'{"a": NaN}', b'{"a": Infinity}', b'{"a": -Infinity}'):
            self.refused(text, "non-finite")

    def test_floats_refused(self):
        for text in (b'{"a": 4.0}', b'{"a": 1e3}', b'{"a": -0.5}'):
            self.refused(text, "non-integer")

    def test_oversized_integer(self):
        self.refused(b'{"a": 12345678901234567890}', "digits")

    def test_bom(self):
        self.refused(b'\xef\xbb\xbf{"a": 1}', "byte-order mark")

    def test_trailing_data(self):
        self.refused(b'{"a": 1} {"b": 2}', "invalid JSON")

    def test_array_top_level(self):
        self.refused(b'[{"a": 1}]', "top level")

    def test_not_utf8(self):
        self.refused(b'{"a": "\xff"}', "not UTF-8")

    def test_oversized_document(self):
        self.refused(b'{"a": "' + b"x" * strictjson.MAX_BYTES + b'"}', "exceeds")


if __name__ == "__main__":
    unittest.main()
