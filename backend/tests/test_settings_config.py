"""The checked-in configuration must satisfy the Settings contract.

The published checkpoint omitted ``app.profiles_root`` while ``_build``
required it. This test binds the minimal committed-config fix to the actual
loader contract instead of relying on a private local file.
"""
from __future__ import annotations

import tomllib
import unittest
from pathlib import Path

from app.config import settings as settings_module


class SettingsConfigTests(unittest.TestCase):
    def test_checked_in_config_has_the_required_profiles_root(self):
        path = Path(settings_module._CONFIG_PATH)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertIn("profiles_root", data["app"])
        self.assertTrue(str(data["app"]["profiles_root"]).strip())

    def test_checked_in_config_builds_without_a_private_file(self):
        path = Path(settings_module._CONFIG_PATH)
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        settings = settings_module._build(
            data, path, uses_external_config=False)
        self.assertEqual(settings.profiles_root.name, "profiles")
        self.assertTrue(settings.profiles_root.is_absolute())
        self.assertFalse(settings.music.live_generation_authorized)


if __name__ == "__main__":
    unittest.main()
