"""Ticket 01 baseline gate.

Locks the three facts the whole plan depends on: the real package/target
paths (T001), the existing assertions that must not regress (T002), and an
independently executable SC-001..SC-006 checklist (T003).

Every expectation below is a hand-derived literal from the spec/plan, never
computed from the artifact under test. Removing or drifting the baseline
record must make these tests fail.
"""

import importlib
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = Path(
    "/home/zhang/.hermes/workspace/.factory/04-tickets/01-baseline.json"
)
QUICKSTART_PATH = Path(
    "/home/zhang/.hermes/workspace/.factory/03-plan/quickstart.md"
)

# Hand-derived from spec.md §5.
SUCCESS_CRITERIA = {
    "SC-001", "SC-002", "SC-003", "SC-004", "SC-005", "SC-006",
}

# Hand-derived from tasks.md path conventions and quickstart.md preconditions.
CRITICAL_EXISTING_PATHS = {
    "pyproject.toml",
    "src/clipllm02/__init__.py",
    "src/clipllm02/policy.py",
    "src/clipllm02/classify.py",
    "spec/builtin_policy.json",
    "spec/builtin_settings.json",
    "tests/test_features_policy.py",
}
PLANNED_NEW_PATHS = {"tests/test_decision_router.py"}

# Hand-derived from src/clipllm02/__init__.py exports.
CRITICAL_MODULES = {
    "models", "features", "modality", "policy", "prompts", "settings",
    "suggest", "classify", "cli",
}

# Hand-derived from tests/test_features_policy.py (the regression baseline).
CRITICAL_ASSERTION_NODEIDS = {
    "tests/test_features_policy.py::TestFeatures::test_nacl_is_chem_and_latin_word",
    "tests/test_features_policy.py::TestFeatures::test_emc2_is_math_and_physics",
    "tests/test_features_policy.py::TestPolicy::test_empty_is_block",
    "tests/test_features_policy.py::TestPolicy::test_url_is_blocked",
    "tests/test_features_policy.py::TestPolicy::test_secret_is_blocked",
    "tests/test_features_policy.py::TestPolicy::test_absolute_length_still_blocks",
    "tests/test_features_policy.py::TestPolicy::test_duplicate_is_skipped",
    "tests/test_features_policy.py::TestPolicy::test_intercept_record_has_no_raw_content",
}

# Hand-derived: ADR-006 daily_limit removal (reconciled in ticket 02) must stay gone.
REMOVED_ASSERTION_NODEIDS = {
    "tests/test_features_policy.py::TestPolicy::test_quota_blocks",
}


def _load_baseline():
    assert BASELINE_PATH.is_file(), f"ticket-01 baseline record missing: {BASELINE_PATH}"
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _removed_entries(data, key):
    """Later tickets' legitimate deletions, recorded as facts in the baseline."""
    removed = set()
    for entry in data.get("removed_by_later_tickets", {}).values():
        removed.update(entry.get(key, ()))
    return removed


def test_baseline_record_exists_with_all_three_sections():
    """The ticket-01 record must exist and cover T001, T002 and T003."""
    data = _load_baseline()
    assert {"target_paths", "package_modules", "regression_assertions",
            "success_criteria"} <= set(data)


def test_target_paths_match_reality():
    """Every recorded target path exists exactly as its status claims."""
    data = _load_baseline()
    records = {r["path"]: r["status"] for r in data["target_paths"]}
    assert CRITICAL_EXISTING_PATHS <= set(records)
    assert PLANNED_NEW_PATHS <= set(records)
    removed = _removed_entries(data, "target_paths")
    assert removed <= set(records), f"unrecorded removal: {removed - set(records)}"
    for path, status in records.items():
        if path in removed:
            assert not (PROJECT_ROOT / path).exists(), \
                f"recorded-removed path still exists: {path}"
            continue
        assert status in {"exists", "planned_new"}
        assert (PROJECT_ROOT / path).exists() == (status == "exists"), (
            f"path/status drift: {path} recorded as {status}"
        )


def test_package_modules_are_real():
    """Recorded modules are importable files of the clipllm02 package."""
    data = _load_baseline()
    modules = set(data["package_modules"])
    assert CRITICAL_MODULES <= modules
    removed = _removed_entries(data, "package_modules")
    assert removed <= modules, f"unrecorded module removal: {removed - modules}"
    for name in modules - removed:
        assert (PROJECT_ROOT / "src/clipllm02" / f"{name}.py").is_file(), (
            f"recorded module has no source file: {name}"
        )
    for name in removed:
        assert not (PROJECT_ROOT / "src/clipllm02" / f"{name}.py").is_file(), (
            f"recorded-removed module still has source file: {name}"
        )


def test_regression_assertions_reference_real_test_methods():
    """Each locked assertion is a real, still-present test method."""
    data = _load_baseline()
    recorded = data["regression_assertions"]
    assert CRITICAL_ASSERTION_NODEIDS <= set(recorded)
    assert not (REMOVED_ASSERTION_NODEIDS & set(recorded)), \
        "daily_limit assertion must stay removed per ADR-006"
    module = importlib.import_module("tests.test_features_policy")
    for nodeid in recorded:
        file_part, _, tail = nodeid.partition("::")
        assert file_part == "tests/test_features_policy.py"
        cls_name, _, method = tail.partition("::")
        cls = getattr(module, cls_name)
        assert callable(getattr(cls, method)), f"missing locked assertion: {nodeid}"
        assert recorded[nodeid].strip(), f"empty assertion note: {nodeid}"


def test_success_criteria_have_independently_executable_commands():
    """Each SC can be collected on its own with the command the checklist records."""
    data = _load_baseline()
    checklist = data["success_criteria"]
    assert set(checklist) == SUCCESS_CRITERIA
    quickstart = QUICKSTART_PATH.read_text(encoding="utf-8")
    for sc, entry in checklist.items():
        command = entry["command"]
        files = entry["files"]
        assert "pytest" in command and "待测" not in command and "TODO" not in command
        assert files, f"{sc} has no verification target"
        assert sc in quickstart, f"{sc} missing from quickstart.md"
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", *files],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"{sc} command is not collectable: {proc.stderr[-400:]}"


def test_baseline_records_src_untouched_at_ticket_01():
    """Ticket 01 must record that it left the production source tree untouched."""
    data = _load_baseline()
    assert data["src_modified"] is False
    assert data["baseline_suite"]["captured_before_any_change"] is True
