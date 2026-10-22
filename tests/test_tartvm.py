"""Tart 2.40.1 config.json reading, profile comparison and instance diffs."""

import json
import unittest

import _fakes
import _util  # noqa: F401  (puts src on sys.path)

from esa import tartvm
from esa.errors import Refusal


def encode(cfg):
    return json.dumps(cfg).encode("ascii")


RESOURCES = {"cpu_count": 4, "memory": "8GiB", "disk": "1GiB"}


class ConfigTest(unittest.TestCase):
    def refused(self, cfg, pointer, reader=tartvm.parse_config):
        with self.assertRaises(Refusal) as ctx:
            reader(encode(cfg))
        self.assertIn(pointer, [p for p, _ in ctx.exception.errors])

    def test_full_linux_and_darwin_configs(self):
        self.assertEqual(tartvm.parse_config(encode(_fakes.LINUX_CONFIG))["os"], "linux")
        self.assertEqual(tartvm.parse_config(encode(_fakes.DARWIN_CONFIG))["os"], "darwin")

    def test_unknown_key_refused(self):
        self.refused(dict(_fakes.LINUX_CONFIG, platform="x"), "config.json/platform")

    def test_darwin_key_in_linux_config_refused(self):
        self.refused(dict(_fakes.LINUX_CONFIG, ecid="c3ludGhldGlj"), "config.json/ecid")

    def test_darwin_config_needs_identifiers(self):
        cfg = dict(_fakes.DARWIN_CONFIG)
        del cfg["ecid"]
        self.refused(cfg, "config.json/ecid")

    def test_non_raw_disk_refused(self):
        self.refused(dict(_fakes.LINUX_CONFIG, diskFormat="asif"), "config.json/diskFormat")

    def test_memory_is_an_integer_byte_count(self):
        self.refused(dict(_fakes.LINUX_CONFIG, memorySize="8GiB"), "config.json/memorySize")
        self.refused(dict(_fakes.LINUX_CONFIG, memorySize=True), "config.json/memorySize")

    def test_strict_reader_requires_full_form(self):
        cfg = dict(_fakes.DARWIN_CONFIG)
        for key in ("os", "arch", "display", "diskFormat"):
            del cfg[key]
        self.refused(cfg, "config.json/os")

    def test_ingress_reader_models_tart_defaults(self):
        cfg = dict(_fakes.DARWIN_CONFIG)
        for key in ("os", "arch", "display", "diskFormat"):
            del cfg[key]
        parsed = tartvm.parse_ingress_config(encode(cfg))
        self.assertEqual((parsed["os"], parsed["arch"], parsed["diskFormat"]), ("darwin", "arm64", "raw"))

    def test_compare_resources(self):
        cfg = tartvm.parse_config(encode(dict(_fakes.LINUX_CONFIG, memorySize=4294967296)))
        self.assertEqual(tartvm.compare_config(cfg, "nixos-minimal", RESOURCES),
                         [("config.json/memorySize", "does not equal resources.memory in bytes")])
        self.assertEqual([p for p, _ in tartvm.compare_config(cfg, "tahoe-vanilla", RESOURCES)][0],
                         "config.json/os")

    def test_darwin_minimums(self):
        cfg = tartvm.parse_config(encode(dict(_fakes.DARWIN_CONFIG, cpuCountMin=8)))
        self.assertIn(("config.json/cpuCount", "is below cpuCountMin"),
                      tartvm.compare_config(cfg, "tahoe-vanilla", RESOURCES))


class InstanceDiffTest(unittest.TestCase):
    def setUp(self):
        self.seed = dict(_fakes.DARWIN_CONFIG)

    def test_selected_identifiers_changed(self):
        inst = dict(self.seed, macAddress="02:00:00:00:00:99", ecid="bmV3LWVjaWQtMDAx")
        self.assertEqual(tartvm.instance_diff(self.seed, inst, ("macAddress", "ecid")), [])

    def test_unchanged_mac_refused(self):
        self.assertEqual(tartvm.instance_diff(self.seed, dict(self.seed), ("macAddress",)),
                         [("config.json/macAddress", "was expected to change and did not")])

    def test_hardware_model_change_refused(self):
        inst = dict(self.seed, macAddress="02:00:00:00:00:99", hardwareModel="b3RoZXI=")
        self.assertEqual(tartvm.instance_diff(self.seed, inst, ("macAddress",)),
                         [("config.json/hardwareModel", "changed outside the selected instance transformations")])

    def test_unselected_ecid_change_refused(self):
        inst = dict(self.seed, macAddress="02:00:00:00:00:99", ecid="bmV3LWVjaWQtMDAx")
        self.assertEqual([p for p, _ in tartvm.instance_diff(self.seed, inst, ("macAddress",))],
                         ["config.json/ecid"])

    def test_dropped_key_refused(self):
        inst = dict(self.seed, macAddress="02:00:00:00:00:99")
        del inst["display"]
        self.assertEqual([p for p, _ in tartvm.instance_diff(self.seed, inst, ("macAddress",))],
                         ["config.json/display"])


if __name__ == "__main__":
    unittest.main()
