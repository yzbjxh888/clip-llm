"""票据 36 gate（v20 删除型改动）：classic 应用必须字面消失，Studio 共用件必须保留。

前 8 条是删除断言——改动前应具名 RED；后 2 条是"严禁误删"回归守卫，
RED 阶段即绿（被守卫的实现本来就存在），已在票据中按 CP-A/CP-B 临时反证。
"""
import importlib
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "clipllm02"

CLASSIC_MODULES = (
    "desktop/app.py",
    "desktop/menu_window.py",
    "desktop/result_window.py",
    "desktop/settings_window.py",
    "desktop/__main__.py",
)
CLASSIC_TEST_FILES = (
    "test_desktop.py",
    "test_desktop_fixes.py",
    "test_always_on_domain_combo_v12.py",
)


def _production_sources():
    return {str(p.relative_to(SRC)): p.read_text(encoding="utf-8")
            for p in SRC.rglob("*.py") if "__pycache__" not in p.parts}


class TestClassicFilesGone(unittest.TestCase):
    def test_classic_production_modules_absent(self):
        remaining = [p for p in CLASSIC_MODULES if (SRC / p).exists()]
        self.assertEqual(remaining, [], f"classic 生产模块仍存在: {remaining}")

    def test_classic_test_files_absent(self):
        remaining = [f for f in CLASSIC_TEST_FILES
                     if (ROOT / "tests" / f).exists()]
        self.assertEqual(remaining, [], f"classic 测试文件仍存在: {remaining}")


class TestNoClassicReferences(unittest.TestCase):
    def test_no_menuwindow_in_production_source(self):
        hits = [f"{name}: MenuWindow"
                for name, text in _production_sources().items()
                if "MenuWindow" in text]
        self.assertEqual(hits, [], f"生产代码残留 MenuWindow: {hits}")

    def test_no_classic_module_refs_in_production_source(self):
        forbidden = ("menu_window", "result_window", "settings_window",
                     "clipllm02.desktop.app", "clipllm02.desktop.__main__")
        hits = [f"{name}: {literal}"
                for name, text in _production_sources().items()
                for literal in forbidden if literal in text]
        self.assertEqual(hits, [], f"生产代码残留 classic 模块引用: {hits}")

    def test_no_classic_entry_point_in_pyproject(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertNotIn("clipllm02-classic", text)

    def test_no_classic_script_name_in_studio_main(self):
        text = (SRC / "studio" / "__main__.py").read_text(encoding="utf-8")
        self.assertNotIn("clipllm02-classic", text)

    def test_desktop_docstring_has_no_classic_modules(self):
        desktop = importlib.import_module("clipllm02.desktop")
        doc = desktop.__doc__ or ""
        for name in ("app.py", "menu_window.py", "result_window.py",
                     "settings_window.py"):
            self.assertNotIn(name, doc, f"desktop docstring 仍列出 {name}")

    def test_no_classic_imports_in_tests(self):
        forbidden = ("clipllm02.desktop.app", "clipllm02.desktop.menu_window",
                     "clipllm02.desktop.result_window",
                     "clipllm02.desktop.settings_window",
                     "clipllm02.desktop.__main__")
        hits = []
        for path in sorted((ROOT / "tests").glob("*.py")):
            if path.name == Path(__file__).name:   # 本 gate 以字面量描述被禁串
                continue
            text = path.read_text(encoding="utf-8")
            hits.extend(f"{path.name}: {literal}"
                        for literal in forbidden if literal in text)
        self.assertEqual(hits, [], f"测试仍导入 classic 模块: {hits}")


class TestStudioNotBroken(unittest.TestCase):
    """回归守卫：RED 阶段即绿；CP-A / CP-B 临时反证，见票据 36。"""

    def test_entry_points_desktop_and_studio_intact(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('clipllm02-desktop = "clipllm02.studio.__main__:main"', text)
        self.assertIn('clipllm02-studio = "clipllm02.studio.__main__:main"', text)

    def test_retained_desktop_modules_import(self):
        for module in ("clipllm02.desktop.gateway",
                       "clipllm02.desktop.window_base",
                       "clipllm02.desktop.x11_state",
                       "clipllm02.desktop.clipboard_worker"):
            importlib.import_module(module)


if __name__ == "__main__":
    unittest.main()
