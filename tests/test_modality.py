import unittest

from clipllm02 import (
    AssetStore, InputEnvelope, MemoryAssetStore, ModalityArbiter,
    load_defaults, load_settings, validate_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 48
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 48
WEBP = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"0" * 32


class TestImageValidation(unittest.TestCase):
    def test_valid_headers(self):
        for data, mime in ((PNG, "image/png"), (JPEG, "image/jpeg"),
                           (WEBP, "image/webp")):
            self.assertIsNone(validate_image(data, mime, 8 * 1024 * 1024))

    def test_bad_header_and_mime(self):
        self.assertIsNotNone(validate_image(b"not a png", "image/png", 1024))
        self.assertIsNotNone(validate_image(PNG, "image/gif", 1024))
        self.assertIsNotNone(validate_image(PNG, "image/png", 10))


class TestAssetStore(unittest.TestCase):
    def test_memory_store_keeps_no_base64_in_ref(self):
        store = MemoryAssetStore()
        ref = store.put(PNG, "image/png", width=10, height=20)
        self.assertEqual(ref.mime, "image/png")
        self.assertEqual(len(ref.sha256), 64)
        self.assertTrue(ref.local_path.startswith("memory://"))
        self.assertNotIn("base64", str(ref.as_dict()))
        self.assertEqual(store.saved[ref.sha256], PNG)

    def test_disk_store_is_atomic_and_deduplicated(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = AssetStore(tmp)
            a = store.put(PNG, "image/png")
            b = store.put(PNG, "image/png")
            self.assertEqual(a.local_path, b.local_path)
            self.assertTrue(a.local_path.endswith(".png"))

    def test_bad_image_rejected(self):
        with self.assertRaises(ValueError):
            MemoryAssetStore().put(b"junk", "image/png")


class TestModalityArbiter(unittest.TestCase):
    def setUp(self):
        self.clock = lambda: 100.0

    def _arbiter(self, settings):
        return ModalityArbiter(settings, store=MemoryAssetStore(), clock=self.clock)

    def test_window_not_elapsed_returns_none(self):
        arb = self._arbiter(load_defaults())
        arb.push_text("hello")
        arb.push_image(PNG, "image/png")
        self.assertIsNone(arb.resolve(now=100.1))
        self.assertIsNotNone(arb.resolve(force=True))

    def test_image_only_discards_text(self):
        arb = self._arbiter(load_defaults())
        arb.push_text("hello")
        arb.push_image(PNG, "image/png")
        env = arb.resolve(force=True)
        self.assertEqual(env.modality, "image")
        self.assertIsNone(env.text)
        self.assertEqual(len(env.assets), 1)

    def test_mixed_keeps_both(self):
        settings = load_settings({"image_text_conflict": "mixed"})
        arb = self._arbiter(settings)
        arb.push_text("解释这个公式")
        arb.push_image(PNG, "image/png")
        env = arb.resolve(force=True)
        self.assertEqual(env.modality, "mixed")
        self.assertEqual(env.text, "解释这个公式")
        self.assertEqual(len(env.assets), 1)

    def test_ask_marks_choice_needed(self):
        settings = load_settings({"image_text_conflict": "ask"})
        arb = self._arbiter(settings)
        arb.push_text("hello")
        arb.push_image(PNG, "image/png")
        env = arb.resolve(force=True)
        self.assertTrue(env.derived.get("needs_modality_choice"))

    def test_text_only_discards_image(self):
        settings = load_settings({"image_text_conflict": "text_only"})
        arb = self._arbiter(settings)
        arb.push_text("hello")
        arb.push_image(PNG, "image/png")
        env = arb.resolve(force=True)
        self.assertEqual(env.modality, "text")
        self.assertEqual(env.assets, ())

    def test_resolve_clears_pending(self):
        arb = self._arbiter(load_defaults())
        arb.push_text("hello")
        self.assertIsNotNone(arb.resolve(force=True))
        self.assertFalse(arb.has_pending)
        self.assertIsNone(arb.resolve(force=True))

    def test_envelope_never_carries_raw_text_in_dict(self):
        env = InputEnvelope.for_text("含有隐私的原文")
        self.assertNotIn("含有隐私的原文", str(env.as_dict()))
        self.assertEqual(env.query_hash, env.text_hash)


if __name__ == "__main__":
    unittest.main()
