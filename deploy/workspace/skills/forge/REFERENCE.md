# Forge reference — default conventions

These are the central board conventions. Repo-local tracker documents do not
override them. The canonical contract is eastwatch's
`docs/label-contract.md`; this reference mirrors it for Forge clients.

## Authority — one vocabulary, platform-native

The label names below are one shared vocabulary; which copy of the state is
*authoritative* is platform-native:

- **GitLab** — the mutually exclusive `agent::*` / `triage::*` label is the
  authoritative open-ticket state; of these, only `agent::ready` /
  `agent::ready-research` are watcher triggers (the other lifecycle labels are
  status, not triggers). GitLab auto-swaps same-scope labels, so a card is in
  exactly one lane.
- **GitHub** — the canonical project's **Status** is the authoritative state
  and the only lifecycle command channel. The `agent::*` / `triage::*` label
  is a eastwatch-written shadow of the last observed Status: eventually
  consistent and searchable for recovery, but never a command, never a
  trigger, and not a complete transition journal. Only eastwatch writes
  it; humans change Status by dragging in the canonical project, and agents
  and skills change it through `glab-board` verbs. Any actor that discovers
  work through a state-shadow label MUST re-read the canonical Status before
  acting. Status carries an option for every open state (including
  `Needs-info`), and exactly one corresponding shadow label may be present.

`hitl`, category, `wayfinder:*`, and `wontfix` are orthogonal labels on both
platforms; Closed is outside reconciliation.

## Label vocabulary

Every triaged issue gets exactly one category and one triage or lifecycle state:

| Label                    | Kind      | Meaning                                  | Color     |
| ------------------------ | --------- | ---------------------------------------- | --------- |
| `bug`                    | category  | something is broken                      | `#d9534f` |
| `enhancement`            | category  | new feature / improvement                | `#5bc0de` |
| `triage::pending`        | triage    | maintainer must evaluate                 | `#f0ad4e` |
| `triage::needs-info`     | triage    | waiting on reporter                      | `#f7e463` |
| `agent::ready`           | trigger   | specified implementation work            | `#5cb85c` |
| `agent::ready-research`  | trigger   | specified research work                  | `#45b39d` |
| `agent::working`         | lifecycle | implementation run active                | `#1f883d` |
| `agent::researching`     | lifecycle | research run active                      | `#33aaff` |
| `agent::parked`          | lifecycle | paused or waiting on a human             | `#f0ad4e` |
| `agent::mr-ready`        | lifecycle | done, MR opened, awaiting review         | `#6f42c1` |
| `agent::failed`          | lifecycle | run crashed or timed out                 | `#c0392b` |
| `agent::for-human`       | lifecycle | needs a human                            | `#337ab7` |
| `hitl`                   | facet     | cannot proceed without the human         | `#cc0033` |
| `wontfix`                | terminal  | will not be actioned (closed)            | `#777777` |
| `wayfinder:map`          | wayfinder | shared investigation map                 | `#6699cc` |
| `wayfinder:research`     | wayfinder | research ticket                          | `#33aa33` |
| `wayfinder:prototype`    | wayfinder | prototype ticket                         | `#ff9900` |
| `wayfinder:grilling`     | wayfinder | developer interview ticket               | `#cc3399` |
| `wayfinder:task`         | wayfinder | implementation task                      | `#8e8e8e` |

`hitl` is orthogonal to type: the ticket cannot proceed without the human. One
query is then the human's whole queue (`-l hitl`); agent-takeable work is
`--not -l hitl`. Mixed tickets carry it and mark steps `HITL:`/`AFK:`.

`glab-board setup` renames legacy labels in place and creates every contract
label idempotently. On GitLab it also adds lifecycle columns to the default
board; on GitHub it reports the one-time manual Projects v2 board-view step.

## Ticket body template

Use the compact question template for investigation tickets:

```markdown
### Question

<one-line framing of the decision/work>

**Today:** <current state, one line>
**Decide one:** / numbered steps — numbered lists + sub-bullets, never a
run-on paragraph. <mark steps `HITL:` / `AFK:` when mixed>

**Gates:** <what this ticket unblocks>
**Detail:** <file/handoff pointer>
```

Before promoting a ticket to Ready with `glab-board ready <iid>`, turn
implementation tickets into a closed execution contract:

```markdown
### Scope
<repository/surface being changed and explicit non-goals>

### Locked decisions
<resolved behavior; no Open questions remain>

### Contract
<exact arguments, outputs, schemas, or state transitions>

### Edit sites
<small ordered file/symbol list plus the closest precedent>

### Tests and done when
<required focused cases, then repository verification command>
```

Repository boundaries belong both in durable repo guidance and in any ticket
whose wording crosses surfaces. Keep generic worktree/claim/MR instructions
out of issue bodies: `glab-board start` and `finish` own that workflow.

## Merge request bodies

Prefer a concise body with `Summary` and `Verification` sections. Agents that
have useful implementation context pass it to
`glab-board finish <iid> --description-file <path>`; the file omits issue-closing
syntax and the eastwatch marker because `finish` owns both. Bare `finish`
derives summary bullets from commit subjects and records the verifier it ran.
An explicit file updates an existing MR; a bare retry preserves human edits.

## Blocking (GitLab native)

Direction matters. "#A must wait for #B" = record from A's side:

```bash
glab api -X POST "projects/$ENC/issues/<A>/links" \
  -f target_project_id=$PID -f target_issue_iid=<B> -f link_type=is_blocked_by
```

(`glab-board block A B` does exactly this.) An item's `blocked` boolean counts
**open** blockers only — closing the last blocker flips it automatically;
that is the frontier engine.

## Wayfinder operations (GitLab default)

On GitLab an Issue cannot parent another Issue, so child tickets are **Task**
work items parented to the map Issue. Tasks share the issue iid space, appear
in `/issues` REST (`issue_type: task`), and take labels/assignees/blocking
like issues.

```bash
# map = an Issue labelled wayfinder:map; capture its global work-item id:
MAP_WI=$(glab api graphql -f query="{project(fullPath:\"$PROJECT\"){workItems(iids:[\"$MAP_IID\"]){nodes{id}}}}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["data"]["project"]["workItems"]["nodes"][0]["id"])')

# child ticket = Task parented to the map (then label via issues REST):
glab api graphql -f query="
mutation { workItemCreate(input:{
  projectPath:\"$PROJECT\", title:\"<title>\",
  workItemTypeId:\"gid://gitlab/WorkItems::Type/5\",
  hierarchyWidget:{ parentId:\"$MAP_WI\" }
}){ workItem{ iid } errors } }"
glab api -X PUT "projects/$ENC/issues/<child-iid>" -f 'labels=wayfinder:grilling'
```

Create tickets first, wire blocking second (iids must exist). Resolve = post a
`## Resolution` comment, close, append a one-liner to the map's
"Decisions so far".

## Observation intake

Turning a raw notes file into issues: split into discrete observations →
dedupe against open issues **by concept** (`glab-board list`, `--search`) →
classify (category + state; default `triage::pending`, `agent::ready` only
when fully specified) → create with `glab-board create --title ...` → report
 a table (observation → iid/URL or
"duplicate of #N").

## Recording contract (agent-link)

Where the metadata lives and how it is discovered: per-worktree git config
(`agent.issue` / `agent.mr`), written by `agent-link issue|mr <url>` from the
worktree. `glab-board start` records the issue; `finish` records the MR. The
lower-level `grab` and `mr` verbs do the same for custom workflows. Read side
(human hotkeys, `/mr`, `/issue`) resolves cwd → pane process tree → pane
pointer → newest recording among the repo's worktrees.
