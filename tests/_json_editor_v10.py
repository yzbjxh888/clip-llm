"""Test-only helpers for the v10 full-JSON template editor (ticket 26).

Older tickets drove ``route_prompt`` as a plain-text prompt box. v10 turned it
into a formatted JSON editor, so those tests now set/read the ``prompt`` field
through this helper: the production widget and signal wiring stay under test.
"""
import json


def editor_json(dlg):
    """Parse the template editor's JSON (fails loudly if it is not JSON)."""
    return json.loads(dlg.route_prompt.toPlainText())


def set_editor_prompt(dlg, prompt):
    """Drive the real editor: change only the JSON prompt, keep the rest intact."""
    pack = editor_json(dlg)
    pack["prompt"] = prompt
    dlg.route_prompt.setPlainText(json.dumps(pack, ensure_ascii=False, indent=2))
