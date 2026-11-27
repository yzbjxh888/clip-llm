"""Ticket 08 gate tests: Agnes SystemOne classify interface (FR-022 / SC-008).

RED first: gateway.py still emits OpenAI chat JSON and reads the key from
settings. These tests never touch the network — urlopen is replaced with a
complete structured SystemOne reply; assertions target our request shape,
argument selection and response decoding.
"""
import json
import os
import unittest
from unittest.mock import patch

from clipllm02 import load_defaults
from clipllm02.desktop import gateway as gmod

# Hand-derived from ADR-009 / builtin_settings.json: 10 text routes;
# `image` is the visual route and must never be a text-classify candidate.
TEXT_ROUTE_IDS = {
    "英语单词", "英语短语", "中文词语", "中文句解", "中文答疑",
    "中文段落", "LaTeX 公式", "代码", "通用解析", "化学物质",
}


class _Reply:
    """Complete /v1/systemone response envelope for the category question."""

    def __init__(self, data):
        self._body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _systemone_reply(choice, probabilities):
    return {"answers": {"category": {
        "choice": choice, "probabilities": probabilities, "confidence": 0.9,
    }}, "usage": {"total_tokens": 12}}


class TestSystemOneRequestShape(unittest.TestCase):
    def test_payload_uses_choice_shape_and_systemone_model(self):
        categories = {"英语单词": "英语单词", "中文词语": "中文词语", "通用解析": "其他"}
        payload = gmod.build_systemone_request(
            "NaCl", categories, instructions="挑最匹配的一条")
        self.assertEqual(payload, {
            "state": "NaCl",
            "questions": {"category": {
                "type": "choice",
                "instructions": "挑最匹配的一条",
                "criteria": categories,
            }},
            "model": "agnes-sys1-1.0",
        })


class TestCategoriesFromDecisionModel(unittest.TestCase):
    """ADR-010: B is the source; route_registry no longer derives categories."""

    def test_categories_come_from_b_with_catchall(self):
        b = load_defaults().get("decision_model_categories") or {}
        cats = gmod.systemone_categories(b)
        self.assertEqual(set(cats), TEXT_ROUTE_IDS)
        self.assertNotIn("图片识别", cats)
        self.assertEqual(cats["英语单词"], "英语单词，单个词")
        self.assertTrue(cats["通用解析"], "general must carry a catch-all description")

    def test_b_edit_changes_candidate_list(self):
        # Editing B directly changes candidates, independent of any route list.
        cats = gmod.systemone_categories({"英语单词": "英语单词"})
        self.assertEqual(set(cats), {"英语单词", "通用解析"})


class TestResponseDecoding(unittest.TestCase):
    def test_argmax_wins_over_reported_choice(self):
        reply = _systemone_reply("中文词语", {"中文词语": 0.2, "英语单词": 0.7})
        self.assertEqual(gmod.decode_systemone_classify(reply), "英语单词")

    def test_choice_used_when_probabilities_absent(self):
        reply = {"answers": {"category": {"choice": "LaTeX 公式"}}}
        self.assertEqual(gmod.decode_systemone_classify(reply), "LaTeX 公式")

    def test_malformed_response_yields_none(self):
        for bad in ({}, {"answers": {}}, {"answers": {"category": {}}}):
            with self.subTest(bad=bad):
                self.assertIsNone(gmod.decode_systemone_classify(bad))


class TestKeyAndSingleNetworkPoint(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def _call(self, **env):
        with patch.dict(os.environ, {"AGNES_API_KEY": "ENV_KEY_LITERAL", **env}), \
                patch.object(gmod.urllib.request, "urlopen",
                             return_value=_Reply(_systemone_reply(
                                 "英语单词", {"英语单词": 0.9}))) as up:
            out = gmod.request_route(self.settings, state="NaCl",
                                     route_registry=self.settings.route_registry())
        return out, up.call_args.args[0]

    def test_default_endpoint_model_and_choice_payload(self):
        out, req = self._call()
        self.assertEqual(req.full_url, "https://apihub.agnes-ai.com/v1/systemone")
        payload = json.loads(req.data)
        self.assertEqual(payload["model"], "agnes-sys1-1.0")
        self.assertEqual(payload["state"], "NaCl")
        self.assertEqual(payload["questions"]["category"]["type"], "choice")
        self.assertEqual(json.loads(out)["selected_route_id"], "英语单词")

    def test_key_comes_from_env_not_engineering_config(self):
        self.settings.data["decision_model_config"]["main_model_key"] = "CONFIG_KEY_IGNORED"
        out, req = self._call()
        self.assertEqual(req.get_header("Authorization"), "Bearer ENV_KEY_LITERAL")
        self.assertEqual(self.settings.decision_model_config["main_model_key"],
                         "CONFIG_KEY_IGNORED", "must not mutate settings")

    def test_failure_never_leaks_key(self):
        with patch.dict(os.environ, {"AGNES_API_KEY": "ENV_KEY_LITERAL"}), \
                patch.object(gmod.urllib.request, "urlopen", side_effect=OSError("boom")):
            out = gmod.request_route(self.settings, state="NaCl",
                                     route_registry=self.settings.route_registry())
        self.assertEqual(out, "")
        self.assertNotIn("ENV_KEY_LITERAL", out)

    def test_missing_key_makes_no_request(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(gmod.Path, "home", return_value=gmod.Path("/nonexistent")), \
                patch.object(gmod.urllib.request, "urlopen") as up:
            out = gmod.request_route(self.settings, state="NaCl",
                                     route_registry=self.settings.route_registry())
        self.assertEqual(out, "")
        up.assert_not_called()


if __name__ == "__main__":
    unittest.main()
