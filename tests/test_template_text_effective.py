"""Ticket 14 gate tests: user-edited template text must reach the request.

RED first: compose_prompt reads routes[rid].prompt and ignores
route_prompts[rid], so text written by apply_template_edit never appears in
the composed system prompt. All tests use in-memory settings; no network/UI.
"""
import unittest

from clipllm02 import compose_prompt, load_defaults
from clipllm02.settings import apply_template_edit

ROUTE_ID = "英语单词"
EDITED_TEXT = "【用户改过的模板】第四纪冰期词汇记忆法·只此一家"
ROUTE_PROMPT_FRAGMENT = "你是一位英语词汇专家，服务于"


class TestTemplateTextReachesComposedPrompt(unittest.TestCase):
    def setUp(self):
        self.settings = load_defaults()

    def test_edited_template_text_reaches_composed_prompt(self):
        """A template edit must replace the route prompt in the final prompt."""
        edited = apply_template_edit(self.settings, ROUTE_ID, EDITED_TEXT)
        # Guard against a vacuous pass: the field really is populated.
        self.assertIn(ROUTE_ID, edited.route_prompts)
        self.assertEqual(edited.route_prompts[ROUTE_ID], EDITED_TEXT)

        intent = edited.intent("en.legacy")
        assert intent is not None
        prompt = compose_prompt(intent, edited).prompt
        self.assertIn(EDITED_TEXT, prompt)
        self.assertNotIn(ROUTE_PROMPT_FRAGMENT, prompt)

    def test_unedited_route_still_falls_back_to_route_prompt(self):
        """With no user edit, the built-in route prompt remains the source."""
        self.assertNotIn(ROUTE_ID, self.settings.route_prompts)
        intent = self.settings.intent("en.legacy")
        assert intent is not None
        prompt = compose_prompt(intent, self.settings).prompt
        self.assertIn(ROUTE_PROMPT_FRAGMENT, prompt)
        self.assertNotIn(EDITED_TEXT, prompt)

    def test_intent_override_still_precedes_edited_template(self):
        """An intent's own prompt stays authoritative over the route template."""
        edited = apply_template_edit(self.settings, ROUTE_ID, EDITED_TEXT)
        intent = edited.intent("en.read")
        assert intent is not None
        self.assertTrue(intent.prompt_override.strip())

        prompt = compose_prompt(intent, edited).prompt
        self.assertIn(intent.prompt_override.strip(), prompt)
        self.assertNotIn(EDITED_TEXT, prompt)

    def test_template_edit_does_not_writeback_routes_or_decision_model(self):
        """The fix must stay one-way: only route_prompts may change."""
        edited = apply_template_edit(self.settings, ROUTE_ID, EDITED_TEXT)
        self.assertEqual(edited.routes, self.settings.routes)
        self.assertEqual(edited.data["decision_model_categories"],
                         self.settings.data["decision_model_categories"])


if __name__ == "__main__":
    unittest.main()
