"""Load a manifest and refuse it unless it satisfies every v1 rule."""

from __future__ import annotations

from esa import manifest
from esa import strictjson
from esa.errors import Refusal, UsageError


def load_valid(path: str, expect_kind=None, check_artifact_id=True) -> dict:
    try:
        doc = strictjson.load_path(path)
    except OSError as exc:
        raise UsageError("%s: cannot read: %s" % (path, exc.strerror or exc)) from None
    except strictjson.StrictJSONError as exc:
        raise Refusal([("", str(exc))]) from None
    errors = manifest.validate(doc, check_artifact_id=check_artifact_id)
    if errors:
        raise Refusal(errors)
    if expect_kind is not None and doc["kind"] != expect_kind:
        raise Refusal([("/kind", "this command requires a %s manifest" % expect_kind)])
    return doc
