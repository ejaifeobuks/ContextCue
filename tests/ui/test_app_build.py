"""scripts/contextcue-app.sh build: the assembled App Lab app is self-contained,
importable, keeps its data across rebuilds and never overwrites other folders."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "contextcue-app.sh"


@unittest.skipUnless(shutil.which("bash"), "bash is required for the build script")
class AppBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="contextcue-build-"))
        self.app = self.tmp / "contextcue"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def build(self, dest):
        return subprocess.run(["bash", str(SCRIPT), "build", str(dest)],
                              capture_output=True, text=True, timeout=60)

    def test_build_is_self_contained_and_importable(self):
        result = self.build(self.app)
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative in ("app.yaml", "README.md", "sketch/sketch.ino", "sketch/sketch.yaml",
                         "python/main.py", "python/requirements.txt", "python/build_info.txt", "python/ai_eval.py",
                         "python/services/ui/main.py", "python/services/ui/assets/index.html",
                         "python/services/ui/assets/icons/sprite.svg",
                         "python/services/application/fixtures/demo_scenarios.json",
                         "python/apps/contextcue-hardware/python/contextcue_hardware/gateway.py",
                         "python/apps/contextcue-hardware/python/contextcue_contracts/__init__.py"):
            self.assertTrue((self.app / relative).is_file(), relative)
        self.assertEqual(list(self.app.rglob("__pycache__")), [])
        self.assertIn("name: ContextCue", (self.app / "app.yaml").read_text())
        self.assertIn("commit ", (self.app / "python/build_info.txt").read_text())
        # The assembled entry point resolves every import from inside the app
        # folder alone (App Lab mounts nothing else).
        check = subprocess.run([sys.executable, "-I", str(self.app / "python/services/ui/main.py"), "--help"],
                               capture_output=True, text=True, timeout=30, cwd=self.tmp)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn("--simulated", check.stdout)

    def test_entry_point_asks_for_the_declared_model(self):
        # The model server lists every downloaded model (Qwen first), so the
        # app must name the one app.yaml declares or it silently uses another.
        self.assertEqual(self.build(self.app).returncode, 0)
        sys.path.insert(0, str(self.app / "python"))
        try:
            import importlib
            import main
            importlib.reload(main)
            self.assertEqual(main.declared_model(self.app / "app.yaml"), "Qwen3.5-0.8B-Q4_0")
            (self.app / "app.yaml").write_text("bricks:\n  - arduino:llm:\n      model: llamacpp:gemma-3-1b-it-Q4_0\n")
            self.assertEqual(main.declared_model(self.app / "app.yaml"), "gemma-3-1b-it-Q4_0")
            self.assertIsNone(main.declared_model(self.app / "missing.yaml"))
        finally:
            sys.path.remove(str(self.app / "python"))
            sys.modules.pop("main", None)

    def test_rebuild_keeps_data_and_cache(self):
        self.assertEqual(self.build(self.app).returncode, 0)
        (self.app / "data").mkdir()
        (self.app / "data" / "contextcue.db").write_bytes(b"settings")
        (self.app / ".cache").mkdir()
        (self.app / ".cache" / "app-compose.yaml").write_text("kept")
        (self.app / "python" / "stale.py").write_text("removed on rebuild")
        self.assertEqual(self.build(self.app).returncode, 0)
        self.assertEqual((self.app / "data" / "contextcue.db").read_bytes(), b"settings")
        self.assertEqual((self.app / ".cache" / "app-compose.yaml").read_text(), "kept")
        self.assertFalse((self.app / "python" / "stale.py").exists())

    def test_refuses_to_overwrite_other_folders(self):
        other = self.tmp / "someone-elses-app"
        other.mkdir()
        (other / "app.yaml").write_text("name: Something Else\n")
        result = self.build(other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not a ContextCue app", result.stderr)
        self.assertEqual((other / "app.yaml").read_text(), "name: Something Else\n")
        inside_repo = self.build(ROOT / "apps" / "contextcue")
        self.assertNotEqual(inside_repo.returncode, 0)
        self.assertIn("refusing", inside_repo.stderr)


if __name__ == "__main__":
    unittest.main()
