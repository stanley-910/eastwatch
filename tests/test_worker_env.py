import atexit
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-worker-env-test-"))
watcher = load_watcher(TMP_ROOT)

atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))


class WorkerEnvTest(unittest.TestCase):
    def setUp(self):
        watcher._ZSHENV_PATH_LOADED = False
        watcher._ZSHENV_PATH_CACHE = None

    def test_worker_env_defaults_config_paths_from_home(self):
        home = TMP_ROOT / "home"

        with patch.dict(
            os.environ, {"HOME": str(home), "PATH": "/plist/bin"}, clear=True
        ):
            with patch.object(watcher, "zshenv_path", return_value=None):
                env = watcher.worker_env({})

        self.assertEqual(env["XDG_CONFIG_HOME"], str(home / ".config"))
        self.assertEqual(env["PI_CODING_AGENT_DIR"], str(home / ".config/pi/agent"))

    def test_worker_env_defaults_pi_dir_from_existing_xdg_config_home(self):
        xdg_config_home = TMP_ROOT / "operator-config"
        base_env = {
            "HOME": str(TMP_ROOT / "home"),
            "PATH": "/plist/bin",
            "XDG_CONFIG_HOME": str(xdg_config_home),
        }

        with patch.dict(os.environ, base_env, clear=True):
            with patch.object(watcher, "zshenv_path", return_value=None):
                env = watcher.worker_env({})

        self.assertEqual(env["XDG_CONFIG_HOME"], str(xdg_config_home))
        self.assertEqual(env["PI_CODING_AGENT_DIR"], str(xdg_config_home / "pi/agent"))

    def test_worker_env_preserves_explicit_pi_coding_agent_dir(self):
        pi_coding_agent_dir = TMP_ROOT / "operator-pi-agent"
        base_env = {
            "HOME": str(TMP_ROOT / "home"),
            "PATH": "/plist/bin",
            "PI_CODING_AGENT_DIR": str(pi_coding_agent_dir),
        }

        with patch.dict(os.environ, base_env, clear=True):
            with patch.object(watcher, "zshenv_path", return_value=None):
                env = watcher.worker_env({})

        self.assertEqual(env["PI_CODING_AGENT_DIR"], str(pi_coding_agent_dir))

    def test_worker_env_does_not_synthesize_config_paths_without_home(self):
        with patch.dict(os.environ, {"PATH": "/plist/bin"}, clear=True):
            with patch.object(watcher, "zshenv_path", return_value=None):
                env = watcher.worker_env({})

        self.assertNotIn("XDG_CONFIG_HOME", env)
        self.assertNotIn("PI_CODING_AGENT_DIR", env)

    def test_worker_env_uses_zshenv_path(self):
        completed = watcher.subprocess.CompletedProcess(
            ["/bin/zsh", "-lc", 'print -r -- "$PATH"'],
            0,
            stdout="/from/zshenv/bin:/plist/bin\n",
            stderr="",
        )
        captured_env = {}

        def fake_run(*args, **kwargs):
            captured_env.update(kwargs["env"])
            return completed

        with patch.dict(
            os.environ, {"PATH": "/plist/bin", "HOME": str(TMP_ROOT)}, clear=True
        ):
            with patch.object(watcher.subprocess, "run", side_effect=fake_run) as run:
                env = watcher.worker_env({"host": "git.example.com"})

        self.assertEqual(env["PATH"], "/from/zshenv/bin:/plist/bin")
        self.assertEqual(env["GITLAB_HOST"], "git.example.com")
        run.assert_called_once()
        self.assertEqual(captured_env["PATH"], "/plist/bin")

    def test_worker_env_keeps_existing_path_when_zshenv_path_unavailable(self):
        completed = watcher.subprocess.CompletedProcess(
            ["/bin/zsh", "-lc", 'print -r -- "$PATH"'],
            1,
            stdout="",
            stderr="zsh failed",
        )
        with patch.dict(
            os.environ, {"PATH": "/plist/bin", "HOME": str(TMP_ROOT)}, clear=True
        ):
            with patch.object(watcher.subprocess, "run", return_value=completed):
                env = watcher.worker_env({})

        self.assertEqual(env["PATH"], "/plist/bin")
        self.assertNotIn("GITLAB_HOST", env)


if __name__ == "__main__":
    unittest.main()
