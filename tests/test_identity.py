import unittest

import _util
from esa import identity
from esa import manifest

# Golden vector, also published in docs/specs/manifest-v1.md.
GOLDEN_FILES = [{"path": "config.json", "sha256": "sha256:" + "0" * 64, "size_bytes": 2}]
GOLDEN_CANONICAL = (
    b'[{"path":"config.json","sha256":"sha256:'
    + b"0" * 64
    + b'","size_bytes":2}]'
)
GOLDEN_DIGEST = "sha256:a8f30ee0f6cae65dbce836376128027542732ca34c94d6161ff69854f7da7a35"


def _reversed(obj):
    """Rebuild every object with its keys in reverse insertion order."""
    if isinstance(obj, dict):
        items = [(k, _reversed(v)) for k, v in obj.items()]
        obj.clear()
        obj.update(reversed(items))
    elif isinstance(obj, list):
        for item in obj:
            _reversed(item)
    return obj


class GoldenVectorTest(unittest.TestCase):
    def test_canonical_bytes(self):
        self.assertEqual(identity.canonical_bytes(GOLDEN_FILES), GOLDEN_CANONICAL)

    def test_canonical_independent_of_key_order(self):
        reordered = [{"size_bytes": 2, "sha256": "sha256:" + "0" * 64, "path": "config.json"}]
        self.assertEqual(identity.canonical_bytes(reordered), GOLDEN_CANONICAL)

    def test_payload_digest(self):
        self.assertEqual(identity.payload_digest(GOLDEN_FILES), GOLDEN_DIGEST)


class IndependentIdentityTest(unittest.TestCase):
    """Identities computed by an independent generator agree with the module."""

    def test_profile_hashes(self):
        for name in ("linux-profile", "macos-profile"):
            self.assertEqual(identity.profile_content_hash(_util.load(name)), _util.EXPECTED[name])

    def test_artifact_ids(self):
        for name in ("linux-artifact", "macos-artifact", "payload-artifact"):
            doc = _util.load(name)
            self.assertEqual(doc["artifact_id"], _util.EXPECTED[name])
            self.assertEqual(identity.artifact_id(doc), _util.EXPECTED[name])

    def test_profile_reference_in_artifact(self):
        doc = _util.load("linux-artifact")
        self.assertEqual(doc["profile"]["content_sha256"], _util.EXPECTED["linux-profile"])


class StabilityTest(unittest.TestCase):
    def test_artifact_id_ignores_descriptive_fields(self):
        base = _util.load("linux-artifact")
        expected = identity.artifact_id(base)
        mutations = {
            "label": lambda d: d.__setitem__("label", "renamed"),
            "profile label": lambda d: d["profile"].__setitem__("label", "renamed"),
            "observations": lambda d: d.__setitem__("observations", []),
            "validation": lambda d: d["validation"][0].__setitem__("result", "fail"),
            "upstream_licenses": lambda d: d["upstream_licenses"].pop(),
            "key order": _reversed,
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                doc = _util.load("linux-artifact")
                mutate(doc)
                self.assertEqual(identity.artifact_id(doc), expected)
                self.assertEqual(manifest.validate(doc), [])

    def test_artifact_id_changes_with_bound_fields(self):
        base = identity.artifact_id(_util.load("linux-artifact"))
        mutations = {
            "payload digest and file": lambda d: d["payload"]["files"][0].__setitem__("sha256", "sha256:" + "1" * 64),
            "profile hash": lambda d: d["profile"].__setitem__("content_sha256", "sha256:" + "2" * 64),
            "profile revision": lambda d: d["profile"].__setitem__("revision", 2),
            "esa revision": lambda d: d.__setitem__("esa_revision", "f" * 40),
            "builder version": lambda d: d["builders"][0].__setitem__("version", "2.31.3"),
            "resources": lambda d: d["resources"].__setitem__("cpu_count", 8),
            "capability": lambda d: d["capabilities"]["guest"].pop(),
        }
        for name, mutate in mutations.items():
            with self.subTest(mutation=name):
                doc = _util.load("linux-artifact")
                mutate(doc)
                self.assertNotEqual(identity.artifact_id(doc), base)

    def test_profile_hash_ignores_label_only(self):
        base = identity.profile_content_hash(_util.load("linux-profile"))
        doc = _util.load("linux-profile")
        doc["label"] = "renamed"
        self.assertEqual(identity.profile_content_hash(doc), base)
        doc["revision"] = 2
        self.assertNotEqual(identity.profile_content_hash(doc), base)

    def test_same_payload_different_provenance(self):
        a = _util.load("linux-artifact")
        b = _util.load("linux-artifact")
        b["esa_revision"] = "e" * 40
        self.assertEqual(a["payload"]["digest"], b["payload"]["digest"])
        self.assertNotEqual(identity.artifact_id(a), identity.artifact_id(b))

    def test_artifact_id_format(self):
        value = identity.artifact_id(_util.load("macos-artifact"))
        self.assertRegex(value, r"\Aesa-artifact-v1:sha256:[0-9a-f]{64}\Z")


if __name__ == "__main__":
    unittest.main()
