"""Release-mechanics guards for the integration manifest."""
from __future__ import annotations

import json
from pathlib import Path

from custom_components.atmeex_cloud.const import DOMAIN, INTEGRATION_VERSION

MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "custom_components"
    / "atmeex_cloud"
    / "manifest.json"
)


def _manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def test_manifest_version_matches_integration_version():
    """manifest.json and const.INTEGRATION_VERSION must be bumped together.

    HACS keys updates off the manifest version; the User-Agent advertises
    INTEGRATION_VERSION. A release that bumps one without the other ships
    inconsistent version metadata.
    """
    assert _manifest()["version"] == INTEGRATION_VERSION


def test_manifest_domain_matches_const():
    assert _manifest()["domain"] == DOMAIN
