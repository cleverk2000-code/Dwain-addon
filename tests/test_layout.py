import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("layout", Path(__file__).parents[1] / "custom_components/dwain_addon/layout.py")
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = layout.LayoutStore(self.temp.name)
        self.store.config.mkdir(parents=True)
        self.area = self.store.config / "areas.yaml"
        self.entity = self.store.config / "entities.yaml"
        self.area.write_text("# keep on undo\nliving:\n  sort_order: 4\n  icon: mdi:sofa\n", encoding="utf-8")
        self.entity.write_text("light.test:\n  hidden: true\n  custom_card: true\n  sort_order: 5\n", encoding="utf-8")
        self.allowed = {"areas": {"living", "bed"}, "entities": {"light.test", "sensor.battery"}}
        self.original = self.store.raw()
        self.rev = self.store.snapshot()["revision"]
        self.patch = {"areas": {"living": {"sort_order": 1}},
                      "entities": {"light.test": {"hidden_in_area": True, "col_span": 2}}}

    def apply(self):
        return self.store.apply(self.patch, self.rev, self.allowed)

    def test_preview_read_only_and_apply_matches(self):
        preview = self.store.preview(self.patch, self.rev, self.allowed)
        self.assertEqual(len(preview["changes"]), 3)
        self.assertEqual(self.store.raw(), self.original)
        self.assertFalse(self.store.backups.exists())
        result = self.apply()
        self.assertEqual(result["revision"], preview["next_revision"])
        current = self.store.snapshot()["configuration"]
        self.assertEqual(current["areas"]["living"]["icon"], "mdi:sofa")
        self.assertTrue(current["entities"]["light.test"]["hidden"])
        self.assertTrue(current["entities"]["light.test"]["custom_card"])
        self.assertTrue(current["entities"]["light.test"]["hidden_in_area"])
        self.assertEqual(current["entities"]["light.test"]["col_span"], "2")

    def test_restore_exact_bytes_and_backup(self):
        result = self.apply()
        preview = self.store.restore_preview(result["backup_id"], result["revision"])
        self.assertEqual(set(preview["files"]), {"areas", "entities"})
        restored = self.store.restore(result["backup_id"], result["revision"])
        self.assertEqual(self.store.raw(), self.original)
        self.assertEqual(restored["revision"], self.rev)
        self.assertNotEqual(restored["backup_id"], result["backup_id"])

    def test_stale_preview_and_apply(self):
        self.apply()
        for fn in (self.store.preview, self.store.apply):
            with self.assertRaises(layout.LayoutError):
                fn(self.patch, self.rev, self.allowed)

    def test_noop(self):
        result = self.store.apply({"areas": {"living": {"sort_order": 4}}}, self.rev, self.allowed)
        self.assertFalse(result["changed"])
        self.assertFalse(self.store.backups.exists())

    def test_invalid_patch_does_not_write(self):
        for data in (
            {}, {"other": {}}, {"areas": {"missing": {"sort_order": 1}}},
            {"entities": {"light.test": {"hidden": "false"}}},
            {"entities": {"light.test": {"col_span": 3}}},
            {"entities": {"light.test": {"col_span_lg": 4}}},
            {"entities": {"light.test": {"col_span_xl": 5}}},
            {"entities": {"light.test": {"sort_order": True}}},
            {"entities": {"light.test": {"custom_card": False}}},
            {"entities": {"light.test": {"sort_order": 0}}},
            {"entities": {"light.test": {"sort_order": 10001}}},
            {"entities": {"light.test": {"hidden_in_area": None}}},
            {"areas": {"living": {"icon": "<script>"}}},
            {"areas": {"living": {"hidden": True}}},
        ):
            with self.subTest(data=data), self.assertRaises(layout.LayoutError):
                self.store.apply(data, self.rev, self.allowed)
        self.assertEqual(self.store.raw(), self.original)
        self.assertFalse(self.store.backups.exists())

    def test_malformed_duplicate_and_custom_tag(self):
        for raw in ("living: [1]", "living: {}\nliving: {}", "living: !include other.yaml", "living: {sort_order: 1, sort_order: 2}"):
            self.area.write_text(raw)
            with self.subTest(raw=raw), self.assertRaises(layout.LayoutError):
                self.store.snapshot()

    def test_yaml_alias_does_not_mutate_unrelated_entity(self):
        self.entity.write_text("light.test: &base {hidden: true}\nsensor.battery: *base\n")
        rev = self.store.snapshot()["revision"]
        self.store.apply({"entities": {"light.test": {"hidden": False}}}, rev, self.allowed)
        settings = self.store.snapshot()["configuration"]["entities"]
        self.assertFalse(settings["light.test"]["hidden"])
        self.assertTrue(settings["sensor.battery"]["hidden"])

    def test_missing_file_roundtrip(self):
        self.entity.unlink()
        rev = self.store.snapshot()["revision"]
        result = self.store.apply(self.patch, rev, self.allowed)
        self.assertTrue(self.entity.exists())
        self.store.restore(result["backup_id"], result["revision"])
        self.assertFalse(self.entity.exists())

    def test_second_file_write_failure_rolls_back(self):
        original_atomic = layout._atomic
        failed = False
        def fail_once(path, raw):
            nonlocal failed
            if path == self.entity and not failed:
                failed = True
                raise OSError("injected")
            return original_atomic(path, raw)
        with patch.object(layout, "_atomic", side_effect=fail_once):
            with self.assertRaises(OSError):
                self.apply()
        self.assertEqual(self.store.raw(), self.original)
        self.assertIsNone(self.store.pending_id())

    def test_interrupted_recovery_and_write_block(self):
        before, after, _ = self.store.plan(self.patch, self.rev, self.allowed)
        backup_id = "a" * 32
        layout._atomic(self.store._backup_path(backup_id), json.dumps({"id": backup_id, "before": before, "after": after}))
        layout._atomic(self.store.pending, json.dumps(backup_id))
        layout._atomic(self.area, after["areas"])
        rev = self.store.snapshot()["revision"]
        with self.assertRaises(layout.LayoutError):
            self.store.apply(self.patch, rev, self.allowed)
        self.store.recover(backup_id)
        self.assertEqual(self.store.raw(), self.original)
        self.assertIsNone(self.store.pending_id())

    def test_recovery_refuses_external_write(self):
        before, after, _ = self.store.plan(self.patch, self.rev, self.allowed)
        backup_id = "b" * 32
        layout._atomic(self.store._backup_path(backup_id), json.dumps({"id": backup_id, "before": before, "after": after}))
        layout._atomic(self.store.pending, json.dumps(backup_id))
        self.area.write_text("living: {sort_order: 20}")
        with self.assertRaises(layout.LayoutError):
            self.store.recover(backup_id)
        self.assertEqual(self.store.pending_id(), backup_id)
        self.assertIn("20", self.area.read_text())

    def test_restore_refuses_newer_edits(self):
        result = self.apply()
        self.area.write_text("living: {sort_order: 20}")
        rev = self.store.snapshot()["revision"]
        with self.assertRaises(layout.LayoutError):
            self.store.restore(result["backup_id"], rev)
        self.assertIn("20", self.area.read_text())

    def test_symlink_and_backup_traversal(self):
        with self.assertRaises(layout.LayoutError):
            self.store.restore("../secrets", self.rev)
        self.area.unlink()
        self.area.symlink_to(self.entity)
        with self.assertRaises(layout.LayoutError):
            self.store.snapshot()

    def test_distinct_flags_and_all_size_limits(self):
        result = self.store.apply({"entities": {"light.test": {"hidden": False, "hidden_in_area": True, "excluded": False, "row_span": 2, "row_span_lg": 3, "row_span_xl": 4, "col_span_xl": 4}}}, self.rev, self.allowed)
        settings = self.store.snapshot()["configuration"]["entities"]["light.test"]
        self.assertFalse(settings["hidden"])
        self.assertTrue(settings["hidden_in_area"])
        self.assertFalse(settings["excluded"])
        self.assertEqual(settings["row_span_xl"], "4")
        self.assertTrue(result["changed"])


if __name__ == "__main__":
    unittest.main()
