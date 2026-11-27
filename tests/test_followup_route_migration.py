"""Ticket 18 gate tests: migrate_legacy_route_ids must cover followup_route.

RED first: the migration rewrites B keys, routes/route_prompts keys,
primary_route and includes[].route_id, but leaves intent.followup_route as
the old English id ("latex") behind. A version-9 config therefore passes
migration yet fails validation with "二次查询模板 'latex' 不存在".

No real network and no mutation of the real user config: the integration
case runs against a temporary copy of the shipped backup.
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clipllm02.settings import (LEGACY_ROUTE_ID_MAP, migrate_legacy_route_ids,
                                validate)
from clipllm02.studio import config as config_mod

BACKUP_CONFIG = Path.home() / ".config/clip-llm02.bak-20261127-102543/settings.json"
EXPECTED_LATEX_ID = "LaTeX 公式"  # hand-derived from LEGACY_ROUTE_ID_MAP / spec


class TestFollowupRouteMigration(unittest.TestCase):
    def test_legacy_followup_route_is_migrated(self):
        data = {"intents": [{"id": "image.ocr", "followup_route": "latex"}]}

        changed = migrate_legacy_route_ids(data)

        self.assertTrue(changed)
        intent = data["intents"][0]
        self.assertIn("followup_route", intent)
        self.assertEqual(intent["followup_route"], EXPECTED_LATEX_ID)
        self.assertEqual(intent["followup_route"], LEGACY_ROUTE_ID_MAP["latex"])

    def test_migration_is_idempotent(self):
        data = {"intents": [{"id": "image.ocr", "followup_route": "latex"}]}
        migrate_legacy_route_ids(data)

        self.assertFalse(migrate_legacy_route_ids(data))
        self.assertEqual(data["intents"][0]["followup_route"], EXPECTED_LATEX_ID)

    def test_already_migrated_auto_and_empty_values_are_untouched(self):
        data = {"intents": [
            {"id": "a", "followup_route": EXPECTED_LATEX_ID},
            {"id": "b", "followup_route": "auto"},
            {"id": "c", "followup_route": ""},
            {"id": "d"},
        ]}

        self.assertFalse(migrate_legacy_route_ids(data))
        self.assertEqual([i.get("followup_route") for i in data["intents"]],
                         [EXPECTED_LATEX_ID, "auto", "", None])


class TestFollowupRouteMigrationOnRealBackup(unittest.TestCase):
    @unittest.skipUnless(BACKUP_CONFIG.is_file(), "backup config missing")
    def test_version9_backup_followup_route_migrates_and_validates(self):
        with TemporaryDirectory() as tmp:
            cfg_dir = Path(tmp) / "clip-llm02"
            cfg_dir.mkdir()
            path = cfg_dir / "settings.json"
            path.write_text(BACKUP_CONFIG.read_text(encoding="utf-8"),
                            encoding="utf-8")
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
                settings = config_mod.load_config()
                saved = json.loads(path.read_text(encoding="utf-8"))

        ocr = next(i for i in settings.data["intents"] if i.get("id") == "image.ocr")
        self.assertEqual(ocr["followup_route"], EXPECTED_LATEX_ID)
        saved_ocr = next(i for i in saved["intents"] if i.get("id") == "image.ocr")
        self.assertEqual(saved_ocr["followup_route"], EXPECTED_LATEX_ID)
        self.assertNotIn("latex", [i.get("followup_route")
                                   for i in settings.data["intents"]])

        errors = validate(settings)
        latex_errors = [e.message for e in errors if "latex" in e.message.lower()]
        self.assertEqual(latex_errors, [], f"unexpected latex validation errors: {latex_errors}")


if __name__ == "__main__":
    unittest.main()
