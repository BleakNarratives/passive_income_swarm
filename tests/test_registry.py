import unittest
from pathlib import Path

from swarm.entities import Entity, EntityError
from swarm.registry import Registry, RegistryError

ROOT = Path(__file__).resolve().parents[1]


class TestEntity(unittest.TestCase):
    def test_missing_id_rejected(self):
        with self.assertRaises(EntityError):
            Entity.from_dict({"kind": "agent"})

    def test_unknown_kind_rejected(self):
        with self.assertRaises(EntityError):
            Entity.from_dict({"id": "x", "kind": "wizard"})

    def test_persona_is_not_runnable(self):
        entity = Entity.from_dict({"id": "p", "kind": "persona"})
        self.assertFalse(entity.is_runnable)


class TestRegistry(unittest.TestCase):
    def test_shipped_cabinet_loads_and_is_valid(self):
        reg = Registry.load(ROOT / "swarm" / "cabinet.json")
        self.assertGreater(len(reg), 20)
        self.assertEqual(reg.problems(), [])

    def test_resolve_by_slot_and_id(self):
        reg = Registry.load(ROOT / "swarm" / "cabinet.json")
        by_slot = reg.by_slot("B3")
        self.assertIsNotNone(by_slot)
        self.assertIs(reg.resolve("B3"), by_slot)
        self.assertIs(reg.resolve(by_slot.id), by_slot)

    def test_slot_collision_detected(self):
        raw = {
            "cabinet": {"columns": "AB", "rows": 2},
            "entities": [
                {"id": "a", "kind": "agent", "slot": "A1"},
                {"id": "b", "kind": "agent", "slot": "A1"},
            ],
        }
        with self.assertRaises(RegistryError):
            Registry.from_dict(raw)

    def test_missing_parent_detected(self):
        raw = {
            "cabinet": {"columns": "AB", "rows": 2},
            "entities": [{"id": "kid", "kind": "subagent", "parent": "ghost"}],
        }
        with self.assertRaises(RegistryError):
            Registry.from_dict(raw)

    def test_auto_slot_assignment_in_reading_order(self):
        raw = {
            "cabinet": {"columns": "AB", "rows": 2},
            "entities": [
                {"id": "a", "kind": "agent"},
                {"id": "b", "kind": "agent"},
                {"id": "c", "kind": "agent"},
            ],
        }
        reg = Registry.from_dict(raw)
        self.assertEqual(reg.get("a").slot, "A1")
        self.assertEqual(reg.get("b").slot, "B1")
        self.assertEqual(reg.get("c").slot, "A2")

    def test_children_and_kind_filters(self):
        reg = Registry.load(ROOT / "swarm" / "cabinet.json")
        kids = reg.children("mrs_higgins")
        self.assertTrue(kids)
        self.assertTrue(all(k.parent == "mrs_higgins" for k in kids))
        self.assertTrue(reg.of_kind("daemon"))


if __name__ == "__main__":
    unittest.main()
