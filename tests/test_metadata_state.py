import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from services.onec_data.metadata_state import MetadataState, schema_diff
from tools import BackendError


def state_for(folder):
    return MetadataState(SimpleNamespace(onec_metadata_state_dir=Path(folder), onec_base_id="test",
                                       odata_base_url="https://onec.invalid/odata", odata_username="reader"))


class MetadataStateTests(unittest.TestCase):
    def test_diff_covers_entities_fields_types_keys_and_navigation(self):
        before = {"A": {"fields": {"Key": "Edm.Guid", "Old": "Edm.String", "Value": "Edm.Int32"}, "keys": ["Key"]},
                  "Removed": {"fields": {}, "keys": []}}
        after = {"A": {"fields": {"Key": "Edm.Guid", "New": "Edm.String", "Value": "Edm.Int64"}, "keys": ["Key", "Value"],
                       "navigation": {"Owner": {"Relationship": "StandardODATA.Owner"}}},
                 "Added": {"fields": {"ID": "Edm.Guid"}, "keys": ["ID"]}}
        original = copy.deepcopy(before)
        result = schema_diff(before, after)
        self.assertEqual(result["entities_added"], {"Added": after["Added"]})
        self.assertEqual(result["entities_removed"], {"Removed": before["Removed"]})
        changed = result["entities_changed"]["A"]
        self.assertEqual(changed["fields_added"], {"New": "Edm.String"})
        self.assertEqual(changed["fields_removed"], {"Old": "Edm.String"})
        self.assertEqual(changed["fields_changed"], {"Value": {"before": "Edm.Int32", "after": "Edm.Int64"}})
        self.assertEqual(changed["keys_changed"]["after"], ["Key", "Value"])
        self.assertIn("navigation_changed", changed)
        self.assertEqual(before, original)

    def test_atomic_state_is_readable_and_corruption_is_not_ignored(self):
        with TemporaryDirectory() as folder:
            state = state_for(folder)
            schema = {"A": {"fields": {"Key": "Edm.Guid"}, "keys": ["Key"]}}
            state.record(schema, "0123456789abcdef", True, {"event": "metadata_initialized"})
            restored = state_for(folder).load()
            self.assertEqual(restored["schema"], schema)
            self.assertTrue(restored["accepted"])
            self.assertEqual(len(list(Path(folder).glob("*.changes.jsonl"))), 1)
            state.path.write_text(json.dumps({"format_version": 1, "schema": []}))
            with self.assertRaises(BackendError) as caught:
                state_for(folder).load()
            self.assertEqual(caught.exception.code, "METADATA_STATE_ERROR")

    def test_unwritable_state_location_returns_explicit_error(self):
        with TemporaryDirectory() as folder:
            occupied = Path(folder) / "occupied"
            occupied.write_text("file")
            state = state_for(occupied)
            with self.assertRaises(BackendError) as caught:
                state.record({}, "0123456789abcdef", True, {"event": "metadata_initialized"})
            self.assertEqual(caught.exception.code, "METADATA_STATE_ERROR")
            self.assertFalse(caught.exception.retryable)
