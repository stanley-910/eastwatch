"""GitHub Status-first poller, shadow writer, outbox and prune recovery.

The watcher's GitLab path is untouched; GitHub projects reach the SAME dispatch
machinery by mapping the canonical project's `Status` single-select through the
pinned bijective Status<->shadow-label table. These tests fake the `gh`
subprocess (client-level tests) or the client itself (poller-logic tests), the
same way the existing suite fakes `glab`.
"""

from __future__ import annotations

import atexit
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import load_watcher

TMP_ROOT = Path(tempfile.mkdtemp(prefix="eastwatch-github-status-test-"))
watcher = load_watcher(TMP_ROOT)
atexit.register(lambda: shutil.rmtree(TMP_ROOT, ignore_errors=True))

PROJECT_ID = "PVT_kwABC"


def gh_proj(client):
    return {
        "forge": "github",
        "host": "github.com",
        "path": "owner/repo",
        "github_project_id": PROJECT_ID,
        "_github": client,
    }


def item(item_id, number, status_name, updated_at, *, content_id=None, title="t", state="OPEN"):
    option = f"opt-{status_name}" if status_name else None
    return {
        "item_id": item_id,
        "updated_at": updated_at,
        "content_id": content_id or f"I_{number}",
        "number": number,
        "state": state,
        "title": title,
        "url": f"https://github.com/owner/repo/issues/{number}",
        "body": "body",
        "status_name": status_name,
        "option_id": option,
    }


class FakeGitHub:
    """Client-interface fake for poller/shadow/prune/outbox logic tests."""

    def __init__(self, items=None, project_id=PROJECT_ID):
        self.project_id = project_id
        self.repo = "owner/repo"
        self._items = list(items or [])
        self.label_calls: list[tuple] = []
        self.status_calls: list[tuple] = []
        self.added: list[str] = []
        self.hydrated: list[int] = []
        self._issue_items: dict[int, dict] = {}
        self._issue_labels: dict[int, list[str]] = {}
        self._live: dict[str, str | None] = {i["item_id"]: i["status_name"] for i in self._items}

    def load_status_schema(self):
        pass

    def status_option_id(self, status_name):
        return f"opt-{status_name}"

    def fetch_items(self):
        return [dict(i) for i in self._items]

    def hydrate_issue(self, number):
        self.hydrated.append(int(number))
        return {"title": "t", "url": f"https://github.com/owner/repo/issues/{number}", "body": "body"}

    def item_status_name(self, item_id):
        if item_id in self._live:
            return self._live[item_id]
        for i in self._items:
            if i["item_id"] == item_id:
                return i["status_name"]
        return None

    def issue_labels(self, number):
        return list(self._issue_labels.get(int(number), []))

    def set_labels(self, number, add=(), remove=()):
        self.label_calls.append((int(number), list(add), list(remove)))
        cur = set(self._issue_labels.get(int(number), []))
        cur.difference_update(remove)
        cur.update(add)
        self._issue_labels[int(number)] = sorted(cur)

    def issue_item(self, number):
        return dict(
            self._issue_items.get(int(number), {"item_id": None, "content_id": f"I_{number}", "state": "OPEN"})
        )

    def add_item(self, content_id):
        self.added.append(content_id)
        return f"item-for-{content_id}"

    def set_status(self, item_id, status_name):
        self.status_calls.append((item_id, status_name))
        self._live[item_id] = status_name

    def set_status_fenced(self, item_id, status_name, **kw):
        self.set_status(item_id, status_name)
        return self.item_status_name(item_id) == status_name


def fresh_ps(**over):
    ps = {
        "bootstrapped": True,
        "github_observations": {},
        "github_outbox": {},
        "github_restore": {},
        "conversations": {},
        "mr_index": {},
    }
    ps.update(over)
    return ps


# --------------------------------------------------------------------------- poller


class PollerTest(unittest.TestCase):
    def test_adopt_only_bootstrap(self):
        client = FakeGitHub()
        ps = fresh_ps(bootstrapped=False)
        diff = watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])
        self.assertEqual(ps["github_outbox"], {})  # no dispatch on bootstrap
        self.assertIn("PI_1", ps["github_observations"])
        self.assertEqual(diff["changed"], [])  # no shadow on bootstrap

    def test_no_dispatch_on_first_sighting_already_ready(self):
        client = FakeGitHub()
        ps = fresh_ps()  # bootstrapped, but this item never seen before
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])
        self.assertEqual(ps["github_outbox"], {})
        self.assertIn("PI_1", ps["github_observations"])

    def test_dispatch_on_ready_transition_from_triage(self):
        client = FakeGitHub()
        ps = fresh_ps(
            github_observations={
                "PI_1": {"generation": watcher.github_generation(PROJECT_ID, item("PI_1", 7, "Triage", "t0")),
                         "status_name": "Triage", "number": 7, "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Triage"}
            }
        )
        diff = watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])
        self.assertEqual(len(ps["github_outbox"]), 1)
        entry = next(iter(ps["github_outbox"].values()))
        self.assertEqual(entry["label"], watcher.TRIGGER_LABELS[0])
        self.assertFalse(entry["dispatched"])
        self.assertEqual(entry["issue"]["iid"], 7)
        self.assertIn("PI_1", diff["changed"])  # also mirrored to shadow

    def test_research_transition_maps_to_research_label(self):
        client = FakeGitHub()
        ps = fresh_ps(
            github_observations={
                "PI_1": {"generation": "old", "status_name": "Needs-info", "number": 7,
                         "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Needs-info"}
            }
        )
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready-research", "t1")])
        entry = next(iter(ps["github_outbox"].values()))
        self.assertEqual(entry["label"], watcher.TRIGGER_LABELS[1])

    def test_collapsed_same_tick_final_state_only(self):
        # Item passed Triage -> Ready -> Working between ticks; we only observe
        # the final state Working, so no (transient-Ready) dispatch fires.
        client = FakeGitHub()
        ps = fresh_ps(
            github_observations={
                "PI_1": {"generation": "old", "status_name": "Triage", "number": 7,
                         "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Triage"}
            }
        )
        diff = watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Working", "t2")])
        self.assertEqual(ps["github_outbox"], {})  # final state Working != Ready
        self.assertEqual(ps["github_observations"]["PI_1"]["status_name"], "Working")
        self.assertIn("PI_1", diff["changed"])

    def test_no_dispatch_into_ready_from_non_whitelisted_prior(self):
        # Ready reached from Parked (not in the from-whitelist) is not a dispatch.
        client = FakeGitHub()
        ps = fresh_ps(
            github_observations={
                "PI_1": {"generation": "old", "status_name": "Parked", "number": 7,
                         "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Parked"}
            }
        )
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])
        self.assertEqual(ps["github_outbox"], {})

    def _triage_obs(self):
        return {"PI_1": {"generation": "old", "status_name": "Triage", "number": 7,
                         "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Triage"}}

    def test_trigger_gating_blocks_disabled_kind(self):
        # Q1: project enables only agent::ready; a Ready-research transition must
        # not dispatch (parity with GitLab per-label enablement).
        client = FakeGitHub()
        ps = fresh_ps(github_observations=self._triage_obs())
        watcher.poll_github_status(
            client, gh_proj(client), ps, [item("PI_1", 7, "Ready-research", "t1")],
            triggers=["agent::ready"],
        )
        self.assertEqual(ps["github_outbox"], {})

    def test_trigger_gating_allows_enabled_kind(self):
        client = FakeGitHub()
        ps = fresh_ps(github_observations=self._triage_obs())
        watcher.poll_github_status(
            client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")],
            triggers=["agent::ready", "mention"],
        )
        self.assertEqual(len(ps["github_outbox"]), 1)

    def test_dispatch_hydrates_issue_text(self):
        # Q6: issue title/url/body are fetched only when a dispatch fires.
        client = FakeGitHub()
        ps = fresh_ps(github_observations=self._triage_obs())
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])
        self.assertEqual(client.hydrated, [7])
        entry = next(iter(ps["github_outbox"].values()))
        self.assertEqual(entry["issue"]["web_url"], "https://github.com/owner/repo/issues/7")


class FetchInputsTest(unittest.TestCase):
    def test_bootstrap_tick_adopts_and_reports_empty(self):
        client = FakeGitHub(items=[item("PI_1", 7, "Ready", "t1")])
        ps = fresh_ps(bootstrapped=False)
        out = watcher.fetch_github_inputs(client, gh_proj(client), ps)
        self.assertEqual(out["label_fires"], [])
        self.assertEqual(ps["github_outbox"], {})
        self.assertEqual(client.label_calls, [])  # no shadow on bootstrap
        self.assertIn("PI_1", ps["github_observations"])

    def test_transition_tick_queues_outbox_and_shadows(self):
        client = FakeGitHub(items=[item("PI_1", 7, "Ready", "t1")])
        ps = fresh_ps(
            github_observations={
                "PI_1": {"generation": "old", "status_name": "Triage", "number": 7,
                         "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Triage"}
            }
        )
        watcher.fetch_github_inputs(client, gh_proj(client), ps)
        self.assertEqual(len(ps["github_outbox"]), 1)
        # Shadow mirrored the changed item (final Status Ready).
        self.assertEqual(client.label_calls[0][1], [watcher.TRIGGER_LABELS[0]])
        # Commit phase then derives the fire from the durable outbox.
        fires = watcher.github_dispatch_fires(ps)
        self.assertEqual(fires[0]["label"], watcher.TRIGGER_LABELS[0])


# --------------------------------------------------------------------------- outbox idempotence


class OutboxTest(unittest.TestCase):
    def _fire_once(self, ps, client):
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])

    def test_crash_replay_is_idempotent(self):
        client = FakeGitHub()
        ps = fresh_ps(
            github_observations={
                "PI_1": {"generation": "old", "status_name": "Triage", "number": 7,
                         "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Triage"}
            }
        )
        self._fire_once(ps, client)
        gen = next(iter(ps["github_outbox"]))

        # Crash between observe->persist->dispatch: outbox persisted undispatched.
        fires_a = watcher.github_dispatch_fires(ps)
        self.assertEqual(len(fires_a), 1)
        # Replay before marking: same generation, exactly one fire (not two).
        fires_b = watcher.github_dispatch_fires(ps)
        self.assertEqual([f["_generation"] for f in fires_b], [gen])

        # Commit succeeds: mark consumed in the same atomic save.
        watcher.github_mark_dispatched(ps, fires_b)
        self.assertTrue(ps["github_outbox"][gen]["dispatched"])
        self.assertEqual(watcher.github_dispatch_fires(ps), [])

        # Re-polling the unchanged item adds no new command.
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])
        self.assertEqual(list(ps["github_outbox"]), [gen])
        self.assertEqual(watcher.github_dispatch_fires(ps), [])

    def test_new_generation_after_redispatch(self):
        client = FakeGitHub()
        ps = fresh_ps(
            github_observations={
                "PI_1": {"generation": "old", "status_name": "Failed", "number": 7,
                         "content_id": "I_7", "updated_at": "t0", "option_id": "opt-Failed"}
            }
        )
        # Failed -> Ready fires; later Failed -> Ready again (new updatedAt) fires anew.
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t1")])
        watcher.github_mark_dispatched(ps, watcher.github_dispatch_fires(ps))
        ps["github_observations"]["PI_1"]["status_name"] = "Failed"
        ps["github_observations"]["PI_1"]["generation"] = "old2"
        watcher.poll_github_status(client, gh_proj(client), ps, [item("PI_1", 7, "Ready", "t9")])
        undispatched = [e for e in ps["github_outbox"].values() if not e["dispatched"]]
        self.assertEqual(len(undispatched), 1)


# --------------------------------------------------------------------------- shadow writer


class ShadowTest(unittest.TestCase):
    def test_full_replacement_of_shadow_labels(self):
        client = FakeGitHub(items=[item("PI_1", 7, "Working", "t1")])
        ps = fresh_ps(github_observations={"PI_1": {"number": 7, "status_name": "Working"}})
        watcher.github_shadow_write(client, gh_proj(client), ps, ["PI_1"])
        self.assertEqual(len(client.label_calls), 1)
        number, add, remove = client.label_calls[0]
        self.assertEqual(number, 7)
        self.assertEqual(add, [watcher.WORKING_LABEL])
        self.assertEqual(set(remove), set(watcher.SHADOW_LABELS) - {watcher.WORKING_LABEL})
        self.assertEqual(len(remove), 9)

    def test_prewrite_recheck_skips_when_status_moved(self):
        # Observation said Working, but live Status is already Review -> skip.
        client = FakeGitHub(items=[item("PI_1", 7, "Review", "t2")])
        ps = fresh_ps(github_observations={"PI_1": {"number": 7, "status_name": "Working"}})
        watcher.github_shadow_write(client, gh_proj(client), ps, ["PI_1"])
        self.assertEqual(client.label_calls, [])

    def test_status_to_label_bijection(self):
        self.assertEqual(len(watcher.STATUS_TO_LABEL), 10)
        self.assertEqual(set(watcher.LABEL_TO_STATUS), set(watcher.SHADOW_LABELS))
        for status, label in watcher.STATUS_TO_LABEL.items():
            self.assertEqual(watcher.LABEL_TO_STATUS[label], status)


# --------------------------------------------------------------------------- prune recovery


class PruneTest(unittest.TestCase):
    def test_reads_conversation_status_first(self):
        client = FakeGitHub()  # issue #7 no longer in the project
        ps = fresh_ps(
            github_observations={"PI_old": {"number": 7, "content_id": "I_7", "status_name": "Working"}},
            github_restore={"7": "Working"},
        )
        watcher.github_prune_recovery(client, gh_proj(client), ps, present=set())
        self.assertEqual(client.added, ["I_7"])
        self.assertEqual(client.status_calls, [("item-for-I_7", "Working")])
        self.assertNotIn("PI_old", ps["github_observations"])
        self.assertIn("item-for-I_7", ps["github_observations"])

    def test_falls_back_to_shadow_label(self):
        client = FakeGitHub()
        client._issue_labels[7] = ["agent::parked", "category::feature"]
        ps = fresh_ps(
            github_observations={"PI_old": {"number": 7, "content_id": "I_7", "status_name": "Parked"}},
        )
        watcher.github_prune_recovery(client, gh_proj(client), ps, present=set())
        self.assertEqual(client.status_calls, [("item-for-I_7", "Parked")])

    def test_defaults_to_triage_when_nothing_known(self):
        client = FakeGitHub()
        ps = fresh_ps(
            github_observations={"PI_old": {"number": 7, "content_id": "I_7", "status_name": "Ready"}},
        )
        watcher.github_prune_recovery(client, gh_proj(client), ps, present=set())
        self.assertEqual(client.status_calls, [("item-for-I_7", "Triage")])

    def test_present_item_not_pruned(self):
        client = FakeGitHub()
        ps = fresh_ps(github_observations={"PI_1": {"number": 7, "content_id": "I_7", "status_name": "Ready"}})
        watcher.github_prune_recovery(client, gh_proj(client), ps, present={"PI_1"})
        self.assertEqual(client.added, [])
        self.assertIn("PI_1", ps["github_observations"])


# --------------------------------------------------------------------------- write seam (Status-first)


class WriteSeamTest(unittest.TestCase):
    def test_active_write_sets_status_only_no_labels(self):
        # Q3: Status writers never touch labels — the poller's shadow writer is
        # the single label writer.
        client = FakeGitHub()
        client._issue_items[7] = {"item_id": "PI_1", "content_id": "I_7", "state": "OPEN"}
        client._live["PI_1"] = "Ready"
        watcher.set_issue_agent_label(None, gh_proj(client), "7", watcher.WORKING_LABEL)
        self.assertEqual(client.status_calls, [("PI_1", "Working")])
        self.assertEqual(client.label_calls, [])  # no direct shadow write

    def test_terminal_write_over_active_status_succeeds(self):
        client = FakeGitHub()
        client._issue_items[7] = {"item_id": "PI_1", "content_id": "I_7", "state": "OPEN"}
        client._live["PI_1"] = "Working"  # worker was running
        watcher.set_issue_agent_label(None, gh_proj(client), "7", watcher.MR_READY_LABEL)
        self.assertEqual(client.status_calls, [("PI_1", "Review")])

    def test_terminal_write_refused_over_newer_nonactive_status(self):
        # Q2: a human dragged the card to Parked; a stale worker completion must
        # not clobber that newer non-active Status.
        client = FakeGitHub()
        client._issue_items[7] = {"item_id": "PI_1", "content_id": "I_7", "state": "OPEN"}
        client._live["PI_1"] = "Parked"
        watcher.set_issue_agent_label(None, gh_proj(client), "7", watcher.MR_READY_LABEL)
        self.assertEqual(client.status_calls, [])  # refused

    def test_idempotent_when_status_already_set(self):
        client = FakeGitHub()
        client._issue_items[7] = {"item_id": "PI_1", "content_id": "I_7", "state": "OPEN"}
        client._live["PI_1"] = "Working"
        watcher.set_issue_agent_label(None, gh_proj(client), "7", watcher.WORKING_LABEL)
        self.assertEqual(client.status_calls, [])  # no-op

    def test_missing_item_added_then_fenced(self):
        # Q4: item not in project -> add, then fenced Status write (survives the
        # Item-added->Triage workflow). Fresh add bypasses the terminal guard.
        client = FakeGitHub()
        watcher.set_issue_agent_label(None, gh_proj(client), "7", watcher.FAILED_LABEL)
        self.assertEqual(client.added, ["I_7"])
        self.assertEqual(client.status_calls, [("item-for-I_7", "Failed")])

    def test_direct_label_write_is_noop_on_github(self):
        # Q3: set_issue_labels (used by interrupted-launch recovery) writes no
        # labels on GitHub — the poller owns the shadow.
        client = FakeGitHub()
        watcher.set_issue_labels(None, gh_proj(client), "7", remove=[watcher.WORKING_LABEL])
        self.assertEqual(client.label_calls, [])

    def test_post_note_uses_gh_issue_comment(self):
        client = FakeGitHub()
        posts = []
        client.post_comment = lambda number, body: posts.append((number, body)) or {"id": "url"}
        conv = {"reply_target": {"kind": "issue", "issue_iid": "7"}, "issue_iid": "7"}
        result = watcher.post_conversation_note(None, gh_proj(client), conv, "done")
        self.assertEqual(posts, [(7, "done")])
        self.assertEqual(result, {"id": "url"})


# --------------------------------------------------------------------------- config validation


class ConfigValidationTest(unittest.TestCase):
    def test_github_requires_project_id(self):
        with self.assertRaises(watcher.ConfigurationError):
            watcher.validate_github_projects([{"forge": "github", "path": "owner/repo"}])

    def test_github_rejects_non_pvt_id(self):
        with self.assertRaises(watcher.ConfigurationError):
            watcher.validate_github_projects(
                [{"forge": "github", "path": "owner/repo", "github_project_id": "12345"}]
            )

    def test_github_requires_owner_name_path(self):
        with self.assertRaises(watcher.ConfigurationError):
            watcher.validate_github_projects(
                [{"forge": "github", "path": "repo", "github_project_id": PROJECT_ID}]
            )

    def test_valid_github_project_passes(self):
        watcher.validate_github_projects(
            [{"forge": "github", "path": "owner/repo", "github_project_id": PROJECT_ID}]
        )

    def test_gitlab_project_unaffected(self):
        # No forge key -> GitLab, no github validation applies.
        watcher.validate_github_projects([{"path": "group/project", "id": 171409}])


# --------------------------------------------------------------------------- client (gh subprocess faked)


class FakeGhResponder:
    """Routes `gh api graphql` calls to canned JSON by query content."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.pages = [
            {
                "data": {
                    "rateLimit": {"cost": 1, "remaining": 4999},
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": True, "endCursor": "C1"},
                            "nodes": [
                                {
                                    "id": "PI_1", "updatedAt": "2026-07-18T00:00:00Z",
                                    "fieldValueByName": {"name": "Ready", "optionId": "opt-Ready"},
                                    "content": {"__typename": "Issue", "id": "I_7", "number": 7,
                                                "state": "OPEN", "title": "t", "url": "u", "body": "b"},
                                },
                                {  # PR content is skipped
                                    "id": "PI_pr", "updatedAt": "x",
                                    "fieldValueByName": {"name": "Ready", "optionId": "opt-Ready"},
                                    "content": {"__typename": "PullRequest"},
                                },
                            ],
                        }
                    },
                }
            },
            {
                "data": {
                    "rateLimit": {"cost": 1, "remaining": 4998},
                    "node": {
                        "items": {
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                            "nodes": [
                                {  # closed issue skipped
                                    "id": "PI_closed", "updatedAt": "x",
                                    "fieldValueByName": {"name": "Triage", "optionId": "opt-Triage"},
                                    "content": {"__typename": "Issue", "id": "I_9", "number": 9,
                                                "state": "CLOSED", "title": "t", "url": "u", "body": "b"},
                                },
                            ],
                        }
                    },
                }
            },
        ]
        self._page = 0

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        query = next((a for a in args if a.startswith("query=")), "")
        if "field(name:" in query or 'field(name: "Status")' in query:
            body = {"data": {"node": {"field": {
                "id": "FIELD_status",
                "options": [{"id": f"opt-{n}", "name": n} for n in watcher.STATUS_TO_LABEL],
            }}}}
        elif "updateProjectV2ItemFieldValue" in query:
            body = {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PI_1"}}}}
        elif "addProjectV2ItemById" in query:
            body = {"data": {"addProjectV2ItemById": {"item": {"id": "PI_new"}}}}
        elif "node(id: $item)" in query:
            body = {"data": {"node": {"fieldValueByName": {"name": "Review", "optionId": "opt-Review"}}}}
        elif "items(" in query:
            body = self.pages[self._page]
            self._page += 1
        else:
            body = {"data": {}}
        return mock.Mock(returncode=0, stdout=json.dumps(body), stderr="")


class ClientTest(unittest.TestCase):
    def _client(self, responder):
        client = watcher.GitHubProject(PROJECT_ID, "owner/repo")
        self._patch = mock.patch("subprocess.run", side_effect=responder)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        return client

    def test_fetch_items_paginates_and_filters(self):
        responder = FakeGhResponder()
        client = self._client(responder)
        items = client.fetch_items()
        self.assertEqual([i["number"] for i in items], [7])  # PR + closed dropped
        self.assertEqual(items[0]["status_name"], "Ready")
        self.assertEqual(client.last_rate_cost, 1)

    def test_schema_fail_closed_when_status_field_missing(self):
        def responder(args, **kwargs):
            return mock.Mock(returncode=0, stdout=json.dumps({"data": {"node": {"field": None}}}), stderr="")

        client = self._client(responder)
        with self.assertRaises(watcher.ConfigurationError):
            client.load_status_schema()

    def test_schema_fail_closed_when_fixed_option_renamed(self):
        # Q5: a renamed/deleted fixed option (here Needs-info missing) fails closed
        # instead of silently becoming an unmapped Status.
        def responder(args, **kwargs):
            opts = [{"id": f"opt-{n}", "name": n} for n in watcher.STATUS_TO_LABEL if n != "Needs-info"]
            body = {"data": {"node": {"field": {"id": "F", "options": opts}}}}
            return mock.Mock(returncode=0, stdout=json.dumps(body), stderr="")

        client = self._client(responder)
        with self.assertRaises(watcher.ConfigurationError) as cm:
            client.load_status_schema()
        self.assertIn("Needs-info", str(cm.exception))

    def test_extra_option_like_closed_is_allowed(self):
        def responder(args, **kwargs):
            opts = [{"id": f"opt-{n}", "name": n} for n in watcher.STATUS_TO_LABEL]
            opts.append({"id": "opt-Closed", "name": "Closed"})
            body = {"data": {"node": {"field": {"id": "F", "options": opts}}}}
            return mock.Mock(returncode=0, stdout=json.dumps(body), stderr="")

        client = self._client(responder)
        client.load_status_schema()  # no raise

    def test_set_status_fenced_retries_until_readback_matches(self):
        # add + set is reverted once by the workflow, then sticks on retry.
        state = {"n": 0, "live": None}

        def responder(args, **kwargs):
            q = next((a for a in args if a.startswith("query=")), "")
            if "field(name:" in q:
                opts = [{"id": f"opt-{n}", "name": n} for n in watcher.STATUS_TO_LABEL]
                return mock.Mock(returncode=0, stdout=json.dumps({"data": {"node": {"field": {"id": "F", "options": opts}}}}), stderr="")
            if "updateProjectV2ItemFieldValue" in q:
                state["n"] += 1
                # First write is reverted by the workflow; second sticks.
                state["live"] = None if state["n"] == 1 else "opt-Working"
                return mock.Mock(returncode=0, stdout=json.dumps({"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "PI_1"}}}}), stderr="")
            if "node(id: $item)" in q:
                fv = {"name": "Working", "optionId": "opt-Working"} if state["live"] == "opt-Working" else None
                return mock.Mock(returncode=0, stdout=json.dumps({"data": {"node": {"fieldValueByName": fv}}}), stderr="")
            return mock.Mock(returncode=0, stdout=json.dumps({"data": {}}), stderr="")

        client = self._client(responder)
        with mock.patch("time.sleep"):
            ok = client.set_status_fenced("PI_1", "Working")
        self.assertTrue(ok)
        self.assertEqual(state["n"], 2)  # retried once

    def test_set_status_sends_validated_mutation(self):
        responder = FakeGhResponder()
        client = self._client(responder)
        client.set_status("PI_1", "Working")
        mutations = [c for c in responder.calls if any("updateProjectV2ItemFieldValue" in a for a in c)]
        self.assertEqual(len(mutations), 1)
        flat = " ".join(mutations[0])
        self.assertIn("option=opt-Working", flat)
        self.assertIn("field=FIELD_status", flat)

    def test_item_status_name_is_targeted_single_query(self):
        responder = FakeGhResponder()
        client = self._client(responder)
        self.assertEqual(client.item_status_name("PI_1"), "Review")
        # No full items() pagination for a single-item recheck.
        self.assertFalse(any(any("items(" in a for a in c) for c in responder.calls))

    def test_unknown_option_fails_closed(self):
        responder = FakeGhResponder()
        client = self._client(responder)
        with self.assertRaises(watcher.ConfigurationError):
            client.set_status("PI_1", "Nonexistent-status")

    def test_graphql_raises_on_errors_payload(self):
        def responder(args, **kwargs):
            return mock.Mock(returncode=0, stdout=json.dumps({"errors": [{"message": "boom"}]}), stderr="")

        client = self._client(responder)
        with self.assertRaises(watcher.GitHubGraphQLError):
            client.graphql("query { x }")

    def test_retries_then_succeeds(self):
        calls = {"n": 0}

        def responder(args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return mock.Mock(returncode=1, stdout="", stderr="rate limited")
            return mock.Mock(returncode=0, stdout=json.dumps({"data": {"ok": 1}}), stderr="")

        client = self._client(responder)
        with mock.patch("time.sleep"):
            data = client.graphql("query { ok }")
        self.assertEqual(data, {"ok": 1})
        self.assertEqual(calls["n"], 2)


if __name__ == "__main__":
    unittest.main()
