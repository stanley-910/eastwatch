"""Config trigger names the code doesn't know must warn loudly, not fail silently.

The watcher only polls the intersection of TRIGGER_LABELS and each project's
configured `triggers:` list — a stale name in config (e.g. pre-rename
`ready-for-agent`) makes that intersection empty and the fleet dies silently.
"""

from __future__ import annotations

import unittest

from eastwatch import watcher


class UnknownTriggerWarningTest(unittest.TestCase):
    def test_stale_label_name_warns_and_names_the_valid_set(self):
        cfg = {
            "projects": [
                {"path": "owner/example", "triggers": ["ready-for-agent", "mention"]}
            ]
        }
        with self.assertLogs(watcher.log, level="WARNING") as captured:
            watcher.warn_unknown_triggers(cfg)
        self.assertEqual(len(captured.output), 1)
        self.assertIn("owner/example", captured.output[0])
        self.assertIn("ready-for-agent", captured.output[0])
        self.assertIn("agent::ready", captured.output[0])

    def test_current_vocabulary_is_silent(self):
        cfg = {
            "projects": [
                {
                    "path": "owner/example",
                    "triggers": [
                        "agent::ready",
                        "agent::ready-research",
                        "mention",
                        "emoji",
                    ],
                }
            ]
        }
        with self.assertNoLogs(watcher.log, level="WARNING"):
            watcher.warn_unknown_triggers(cfg)

    def test_missing_triggers_key_is_silent(self):
        with self.assertNoLogs(watcher.log, level="WARNING"):
            watcher.warn_unknown_triggers({"projects": [{"path": "owner/example"}]})


if __name__ == "__main__":
    unittest.main()
