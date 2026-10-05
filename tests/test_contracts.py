import unittest

from swarm.contracts import (
    SCHEMA,
    Envelope,
    PayloadError,
    content_digest,
    parse,
    verify_digest,
)


class TestDigest(unittest.TestCase):
    def test_key_order_does_not_change_digest(self):
        self.assertEqual(
            content_digest({"a": 1, "b": 2}),
            content_digest({"b": 2, "a": 1}),
        )

    def test_digest_changes_with_content(self):
        self.assertNotEqual(content_digest({"a": 1}), content_digest({"a": 2}))

    def test_verify(self):
        payload = {"trail": "niche-x", "strength": 1}
        self.assertTrue(verify_digest(payload, content_digest(payload)))
        self.assertFalse(verify_digest(payload, content_digest({"other": 1})))
        self.assertFalse(verify_digest(payload, "not-a-digest"))


class TestEnvelope(unittest.TestCase):
    def test_build_computes_valid_digest(self):
        env = Envelope.build("scout", "intel", {"text": "rare find"}, confidence=0.9)
        self.assertEqual(env.schema, SCHEMA)
        self.assertEqual(len(env.content_sha256), 64)
        env.validate()

    def test_validate_rejects_tampered_payload(self):
        env = Envelope.build("scout", "intel", {"text": "original"})
        env.payload = {"text": "tampered"}
        with self.assertRaises(PayloadError):
            env.validate()

    def test_validate_rejects_unknown_kind(self):
        env = Envelope.build("scout", "intel", {"a": 1})
        env.kind = "gossip"
        with self.assertRaises(PayloadError):
            env.validate()

    def test_validate_rejects_bad_schema(self):
        env = Envelope.build("scout", "intel", {"a": 1})
        env.schema = "swarm.payload/99"
        with self.assertRaises(PayloadError):
            env.validate()

    def test_confidence_bounds(self):
        env = Envelope.build("scout", "intel", {"a": 1})
        env.confidence = 1.5
        with self.assertRaises(PayloadError):
            env.validate()

    def test_ad_hoc_json_rejected(self):
        with self.assertRaises(PayloadError):
            Envelope.from_dict({"emitter": "x", "kind": "intel", "payload": {"a": 1}})

    def test_parse_rejects_non_json(self):
        with self.assertRaises(PayloadError):
            parse("{not json")

    def test_roundtrip_through_sidecar_projection(self):
        env = Envelope.build("scout", "pheromone", {"trail": "t", "strength": 0.5})
        restored = Envelope.from_dict(env.to_sidecar())
        self.assertEqual(restored.content_sha256, env.content_sha256)
        self.assertEqual(restored.payload, env.payload)


if __name__ == "__main__":
    unittest.main()
