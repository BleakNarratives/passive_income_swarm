import tempfile
import unittest
from pathlib import Path

from swarm.ledger import Ledger
from swarm.registry import Registry
from swarm.vending import VendingMachine, VendError

ROOT = Path(__file__).resolve().parents[1]


class TestVendingMachine(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.registry = Registry.load(ROOT / "swarm" / "cabinet.json")
        self.ledger = Ledger(self.root / "logs" / "swarm.jsonl")
        self.machine = VendingMachine(self.registry, self.ledger, root=self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cabinet_renders_slots(self):
        text = self.machine.render_cabinet()
        self.assertIn("CABINET", text)
        self.assertIn("A1", text)
        self.assertIn("DVM Worker", text)

    def test_pull_stages_ticket_and_logs(self):
        ticket = self.machine.pull("B3")
        self.assertEqual(ticket.entity_id, "thoth")
        self.assertEqual(ticket.slot, "B3")
        self.assertTrue(Path(ticket.path).exists())
        self.assertEqual(self.ledger.count("dispense"), 1)
        self.assertEqual(len(self.machine.pending()), 1)

    def test_pull_is_idempotent(self):
        first = self.machine.pull("augur", idem="job-1")
        second = self.machine.pull("augur", idem="job-1")
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.ledger.count("dispense"), 1)

    def test_persona_cannot_be_pulled(self):
        with self.assertRaises(VendError):
            self.machine.pull("A6")  # persona_higgins
        self.machine.pull("A6", force=True)  # override allowed

    def test_disabled_occupant_refused(self):
        entity = self.registry.get("thoth")
        entity.enabled = False
        with self.assertRaises(VendError):
            self.machine.pull("B3")
        self.machine.pull("B3", force=True)

    def test_unknown_slot_refused(self):
        with self.assertRaises(VendError):
            self.machine.pull("Z9")

    def test_claim_and_complete_write_outbox(self):
        ticket = self.machine.pull("dvm_worker")
        self.machine.claim(ticket.id, worker="w1")
        done = self.machine.complete(ticket.id, result={"revenue_msat": 1000}, ok=True)
        self.assertEqual(done.status, "complete")
        self.assertTrue((self.root / "control" / "outbox" / f"{ticket.id}.result.json").exists())
        self.assertEqual(self.ledger.count("claim"), 1)
        self.assertEqual(self.ledger.count("complete"), 1)

    def test_double_claim_refused(self):
        ticket = self.machine.pull("dvm_worker")
        self.machine.claim(ticket.id)
        with self.assertRaises(VendError):
            self.machine.claim(ticket.id)

    def test_command_generation(self):
        dvm = self.machine.pull("dvm_worker")
        self.assertIn("dvm_worker.py", dvm.command)
        bash = self.machine.pull("dominance_scout")
        self.assertTrue(bash.command.startswith("bash "))

    def test_polyglot_command_generation(self):
        # Scouts and agents are not all Python: Elixir, Node, Deno and compiled C++.
        expected = {
            "whorl_sensor": "elixir ",
            "market_watch": "node ",
            "edge_probe": "deno run",
            "scout_forge": "scout_forge",
            "wasm_gate": "wasm_gate",
        }
        for entity_id, needle in expected.items():
            command = self.machine.pull(entity_id).command
            self.assertIn(needle, command, f"{entity_id} -> {command}")

    def test_explicit_interpreter_overrides_runtime(self):
        entity = self.registry.get("whorl_sensor")
        entity.interpreter = "/opt/beam/bin/myvm"
        command = self.machine._command_for(entity)
        self.assertTrue(command.startswith("/opt/beam/bin/myvm "))
        entity.interpreter = None


if __name__ == "__main__":
    unittest.main()
