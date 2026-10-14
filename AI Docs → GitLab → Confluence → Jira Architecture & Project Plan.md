# Vendor AI Docs → GitLab → Confluence → Jira: Architecture & Project Plan

Oct 13, 2026 · @Noodles

## Summary

**Key decisions**

Git is the single source of truth. Third-party vendors draft documents with AI on their side, outside the company network, and deliver them through a secure drop. Inside the network nothing uses AI: an intake job scans each delivery and opens a merge request, the project's product owner approves it, and the merge pipeline publishes to Confluence and creates the Jira epics and stories. Nothing reaches Confluence or Jira without a reviewed commit behind it.

The design has four stages, each a separate, replaceable component:

1. **Vendor intake** — the vendor delivers a Markdown package that follows our template, including a structured Backlog section. An internal job quarantines it, scans it and opens a merge request in the central docs repo, under the project's folder.
2. **Publish** — the existing `confluence_sync.py` publishes the doc to Confluence Cloud on merge (already built and tested).
3. **Extract** — in the merge request, a deterministic parser (`extract_backlog.py`) turns the Backlog section into `backlog.yaml`, and the product owner reviews and approves it.
4. **Jira sync** — on merge, a new `jira_sync.py` creates or updates the epics and stories in Jira Cloud from `backlog.yaml`, idempotently, and links them back to the Confluence page.

**Key decisions**

- **No AI inside the company network.** Vendors use AI on their own systems; every internal step is deterministic code, so the same commit always produces the same Jira state.
- **Vendor content is untrusted input.** It is quarantined and scanned, and anything outside Markdown and plain images is rejected before a merge request exists.
- **The template is part of the vendor contract.** Vendors must deliver the doc and its Backlog section in our format; a delivery that fails validation goes back to the vendor, not to an internal fixer.
- **The product owner approves.** CODEOWNERS makes the project's PO a required approver for that project's folder.
- **The backlog is a file, not a side effect.** `backlog.yaml` is versioned and diffable, so scope changes show up in review like code.
- **Git owns scope; Jira owns progress.** Git drives summary, description and acceptance criteria while a ticket is in To Do. After work starts, Jira status, assignee, sprint and points are never overwritten.
- **One central repo, one folder per Jira project** (`docs/PAY/`, `docs/CRM/`), on **Atlassian Cloud** for both Confluence and Jira.
- **No state database.** Pages and issues are found by stable labels derived from file paths and item IDs, the same pattern the Confluence sync already uses.

## Architecture overview

&#91;embedded content: end-to-end architecture · vendor delivery, review, publish\]

The merge request (highlighted) is the only gate. Everything above it drafts or proposes; everything below it runs automatically from approved commits. Inside the MR, the pipeline commits `backlog.yaml` back to the branch for the reviewer.

| Component | Technology | Status |
| --- | --- | --- |
| `confluence_sync.py` | Python 3.12, `markdown`, `requests` | Built and tested |
| Vendor intake job | Python 3.12, SFTP/file-share client, ClamAV, `python-gitlab` | New |
| `extract_backlog.py` | Python 3.12, `markdown-it-py`, `jsonschema` | New |
| `jira_sync.py` | Python 3.12, `requests`, `PyYAML`, `jsonschema` | New |
| Shared CI template | GitLab `include:` project | New |

## Stage 1 — Vendor intake

Vendors never touch GitLab or run anything inside the network. They upload a delivery package to a secure drop; an internal job checks it and opens the merge request on their behalf.

**Delivery package** (one zip per document, defined in the vendor's statement of work):

```
PAY-feature-x-design-v2.zip
  feature-x-design.md      # the doc, in our template, with front matter and ## Backlog
  img/                     # PNG or JPEG only
  manifest.json            # vendor id, project key, version, SHA-256 of every file
```

**How a delivery flows**

1. The vendor uploads the zip to their own folder on the secure drop (SFTP or a managed file share, one folder per vendor, write-only for them).
2. A scheduled `docs:intake` job (every 15 minutes) moves new packages into quarantine.
3. Checks, all of which must pass: checksums match the manifest; malware scan; file types on the allow-list (`.md`, `.png`, `.jpg`); size limits; no raw HTML, scripts or external image links in the Markdown; front-matter and Backlog validation; secrets and PII scan.
4. **Fails** → the package moves to `rejected/`, and a report with line-numbered errors goes back into the vendor's folder and to the vendor manager. Nothing reaches GitLab.
5. **Passes** → the job commits the files under `docs/<project>/` on a new branch as the intake bot and opens an MR, labelled with the vendor and delivery ID. A new version of an existing doc opens an MR against the same file, so the PO sees a normal diff.

Internal staff can also submit a doc by hand (`docflow submit`, or the Web IDE). It runs through the same checks.

**Repository layout**

```
docs/                               # one central repo for all projects
  PAY/                              # folder name = Jira project key
    _project.yaml                   # Confluence space + parent page, Jira issue types, field IDs, approved vendors
    feature-x-design.md             # vendor doc, includes a ## Backlog section
    feature-x-design.backlog.yaml   # generated from ## Backlog, approved by the PO
    img/
  CRM/
    ...
templates/
  design-doc.md                     # the format given to vendors
ci/
  confluence-sync/                  # existing
  intake/                           # new: drop pickup, checks, MR creation
  backlog-extract/                  # new: deterministic parser
  jira-sync/                        # new
CODEOWNERS                          # /docs/PAY/ @pay-product-owner @pay-tech-lead
```

**Required front matter** (validated at intake):

```
---
title: Feature X — Payments Retry Design
doc_type: design            # design | prd | runbook | adr | reference
owner: jsmith               # internal GitLab username; mapped to a Jira Cloud accountId
generate_backlog: true      # false for reference docs and runbooks
vendor:
  id: acme-consulting       # must be listed in the folder's _project.yaml
  delivery: ACME-2026-0142
  ai_assisted: true         # disclosed by the vendor; shown on the Confluence banner
---
```

The Jira project comes from the folder name. A vendor can only deliver to projects that list it in `_project.yaml`.

**Review gates on the MR**

- **Product owner approval is required.** CODEOWNERS lists each project's PO on its folder, and the protected default branch requires code owner approval (GitLab Premium). A tech lead can be a second, optional approver.
- An MR description generated by intake: vendor, delivery ID, files changed, check results, and the Jira dry run.
- CI re-runs every intake check, so a hand-edited MR gets the same scrutiny.
- `vendor-delivery` and `ai-assisted` MR labels, so these docs can be reported on separately.

## Stage 2 — Publish to Confluence

This stage is already built: `confluence_sync.py` publishes `docs/` on every merge to the default branch, creating new pages and updating changed ones. Four small additions connect it to the rest of the flow.

- **One run per project folder.** The job loops over `docs/*/`, running the sync once per folder with that project's Confluence space and parent page from `_project.yaml`. A failure in one project does not stop the others.

* **Skip backlog files.** Add `*.backlog.yaml` to the ignore list (it already only reads `.md`, so this is a guard, not a change).
* **Write a page map.** Emit `confluence-pages.json` (source path → page ID and URL) as a job artifact. Stage 4 reads it to put the Confluence link on each epic, so Jira sync runs after Confluence publish in the same pipeline.
* **Embed the Jira issues.** After Jira sync, the doc's page gets a Jira Issues macro filtered by the doc's label (for example `docflow-pay-feature-x`), so the page always shows live ticket status without a second update.
* **Provenance on the banner.** The existing "generated from Git" banner adds the vendor, delivery ID and AI-assisted flag, plus the MR approvers from front matter, so readers can see who signed off.

Unchanged behaviour carries over: unchanged pages are skipped, pages are tracked by label so a retitle keeps history, hand-made pages are never overwritten, and deletes happen only with `SYNC_PRUNE=true`.

## Stage 3 — Backlog extraction (no LLM)

The epics and stories are written into the doc itself, in a fixed `## Backlog` section, by the vendor. The pipeline only parses and validates that section; no model runs in CI.

**Template convention** (in `templates/design-doc.md`, which is attached to every vendor statement of work):

```markdown
## Backlog

### Epic E1: Automatic retry for failed card payments
Retry soft-declined payments up to 3 times with backoff.
Source: #retry-policy

#### Story E1-S1: Classify decline codes as soft or hard
Map processor decline codes to retryable or final.
Points: 3
Acceptance criteria:
- Given a soft decline code, when classified, then it is marked retryable
- Given an unknown code, when classified, then it is treated as hard

#### Story E1-S2: Schedule retries with exponential backoff
Points: 5
Depends on: E1-S1
Acceptance criteria:
- Given a retryable decline, when scheduled, then attempts run at 1h, 6h, 24h
```

**How it runs**

1. `docs:extract-backlog` runs on MR pipelines for changed docs with `generate_backlog: true`.
2. `extract_backlog.py` parses the Backlog section with a real Markdown parser (headings, `Key: value` lines, bullet lists), not free-text guessing, and writes `<doc>.backlog.yaml`.
3. It validates against a JSON Schema: IDs present and unique, every story under an epic, `Depends on` resolves, points in the allowed set (1, 2, 3, 5, 8, 13), at least one acceptance criterion per story. Errors cite the doc's line number and fail the MR.
4. The bot commits the YAML to the MR branch with `[skip extract]` so it never loops. A hand edit to the YAML fails CI with "edit the Backlog section instead", so the doc stays the single source.
5. The product owner reviews the doc, the YAML diff and the Jira dry-run comment, then approves the MR.

**Why keep a generated `backlog.yaml`:** it is the stable contract that Jira sync reads, its diff shows the PO exactly what will change in Jira, and other producers can write the same file later without any change to Jira sync.

**`backlog.yaml` produced from the example above**

```yaml
version: 1
source: docs/PAY/feature-x-design.md
jira_project: PAY              # from the folder name
epics:
  - id: E1
    summary: Automatic retry for failed card payments
    description: Retry soft-declined payments up to 3 times with backoff.
    source_section: "#retry-policy"
    stories:
      - id: E1-S1
        summary: Classify decline codes as soft or hard
        description: Map processor decline codes to retryable or final.
        story_points: 3
        depends_on: []
        acceptance_criteria:
          - Given a soft decline code, when classified, then it is marked retryable
          - Given an unknown code, when classified, then it is treated as hard
      - id: E1-S2
        summary: Schedule retries with exponential backoff
        story_points: 5
        depends_on: [E1-S1]
        acceptance_criteria:
          - Given a retryable decline, when scheduled, then attempts run at 1h, 6h, 24h
```

**Stable IDs.** `E1-S2` is the key Jira sync uses to find the same issue next time. Authors keep IDs when editing, and CI rejects reusing a removed ID for a different story.

## Stage 4 — Jira sync

A new `jira_sync.py` reads every `*.backlog.yaml` on merge and makes Jira match it: new items are created, changed items are updated, unchanged items are skipped, and removed items are flagged, never deleted. It follows the same patterns as `confluence_sync.py`: stdlib + `requests`, `--validate-only`, `--dry-run`, a JSON report, and exit codes 0 / 1 / 2.

**Idempotency without a database.** Each issue carries two labels: `docflow-<repo hash>` (everything from this repo) and `docflow-<hash of source path + item id>` (this exact item). One JQL query per run (`labels = docflow-<repo hash>`) loads every managed issue. A content hash in an issue entity property (`docflow.hash`) lets unchanged items be skipped without a field-by-field compare.

**Per item, in order**

1. Epics first, then stories, so `parent` can be set on create.
2. Not found → create with summary, description (doc excerpt + acceptance criteria + link to the Confluence section), labels, components, points, reporter = doc `owner`.
3. Found and hash changed → apply the field-ownership rules below.
4. `depends_on` → a "Blocks" issue link, added if missing.
5. Each epic gets a remote link to its Confluence page (from `confluence-pages.json`).
6. Removed from YAML → add label `docflow-removed` and a comment pointing to the MR. If still in To Do, transition to Won't Do; otherwise leave it for the team.

**Field ownership: Git owns scope, Jira owns progress**

| Field | Source of truth | Rule |
| --- | --- | --- |
| Summary, description, acceptance criteria | Git | Overwritten while status category is To Do. After that, Git changes are posted as a comment ("Spec changed in MR !123: ...") instead of overwriting |
| Epic / parent link, components, labels | Git | Always synced; Jira-side extra labels are kept |
| Story points | Git until estimated | Set on create; overwritten only while To Do and unsprinted |
| Status, assignee, sprint, fix version, priority | Jira | Never written after create |
| Comments, worklogs, attachments | Jira | Never touched |

**Jira Cloud specifics**

| Concern | Approach |
| --- | --- |
| Auth | Atlassian service account with an API token, Basic auth over HTTPS |
| Epic → story link | The `parent` field on the story (the older Epic Link field is deprecated on Cloud) |
| Finding managed issues | JQL through `/rest/api/3/search/jql`, paging with the returned token |
| Description format | REST v2 with wiki markup keeps the converter simple; v3 would need Atlassian Document Format |
| People | Cloud uses accountIds, not usernames: map the doc `owner` to an accountId by email lookup, cached per run |
| Volume and rate limits | Bulk create (`/rest/api/2/issue/bulk`, up to 50 per call); honour 429 and `Retry-After` |

Issue type names, the story points field ID, the Won't Do transition and the Confluence parent page differ per project, so they live in each folder's `_project.yaml`. A one-time setup command reads them from Jira and writes the file.

**Automation options considered**

| Option | Verdict | Why |
| --- | --- | --- |
| GitLab CI + Python (`jira_sync.py`) | **Chosen** | Runs after review, has dry runs, tests, idempotency and an audit trail; same pattern as the Confluence sync |
| Jira Automation incoming webhook, called from CI | Fallback | Little code, but creating many linked issues with stable IDs and update rules gets brittle; logic lives outside Git |
| GitLab–Jira built-in integration | Complementary | Links commits and MRs to existing issues by key; it does not create issues. Turn it on for traceability once stories exist |
| Atlassian Forge app | Later, if needed | Native and polished, but a separate codebase and deployment for little gain at this stage |
| Commercial sync tools (e.g. Exalate, Unito) | Not recommended | Licence cost and they don't understand the YAML backlog |

## Pipeline design

One `.gitlab-ci.yml` with two shapes. Merge request pipelines generate and check; default-branch pipelines publish. Nothing writes to Confluence or Jira from an unmerged branch.

| Job | Stage | Runs on | What it does | Writes to |
| --- | --- | --- | --- | --- |
| `docs:intake` | intake | schedule, every 15 min | Picks up vendor packages, quarantines, scans, validates; opens an MR or writes a rejection report | GitLab branch + MR, vendor folder |
| `docs:lint` | validate | MR | Front-matter schema, Markdown rules (no raw HTML or external images), secrets/PII scan | — |
| `docs:extract-backlog` | generate | MR, doc changed | Parses `## Backlog` → `*.backlog.yaml`, schema-validated, committed to the MR branch | MR branch |
| `docs:validate` | validate | MR | `confluence_sync.py --validate-only --strict`; `jira_sync.py --validate-only` (IDs unique, deps resolve, required fields) | — |
| `docs:preview` | preview | MR | Both scripts with `--dry-run`; posts one MR comment for the PO: pages to create/update, issues to create/update/flag | MR comment |
| `docs:publish-confluence` | publish | default branch | `confluence_sync.py` per project folder; saves `confluence-pages.json` | Confluence |
| `docs:sync-jira` | publish | default branch, after Confluence | `jira_sync.py` per project folder; adds Jira links to pages | Jira, Confluence |
| `docs:report` | report | default branch, always | Merges the JSON reports; posts a summary to Teams/Slack | Chat |
| `docflow:tests` | test | MR, `ci/**` changed | pytest for all scripts against fake Confluence/Jira | — |

**Pipeline rules**

- `resource_group: docflow-publish` so two merges never race on the same pages or issues.
- Publish jobs need the protected variables, so they only run on protected branches.
- `docs:sync-jira` is `when: manual` for the first month (a human clicks "play" after reading the preview), then switched to automatic.
- Scheduled nightly run with `--dry-run` reports drift: pages edited by hand in Confluence, or Git-owned Jira fields changed in Jira.
- The scripts live in a shared `docflow` CI template project and are pulled in with `include:`, so any docs repo can adopt them with a few lines.

## Security, governance and AI guardrails

The main risks are a leaked credential, unsafe or sensitive content arriving from a vendor, and vendor-drafted scope reaching a team's backlog unreviewed. Each has a specific control.

**Credentials**

- One Atlassian Cloud service account for Confluence and Jira, limited to the spaces and projects in the repo, plus a separate GitLab bot for intake commits and MR comments. No personal tokens in CI.
- Vendors get write-only accounts on the secure drop, one folder each, with no route into GitLab or Atlassian.
- Tokens stored as masked, protected GitLab CI/CD variables (or pulled from Vault through GitLab's OIDC integration), rotated every 90 days.
- Publish jobs run only on protected branches, so MR pipelines never see the Atlassian token.

**Vendor content (untrusted input)**

- Quarantine first: nothing from the drop is committed until every check passes.
- Malware scan, checksum match against the manifest, file-type allow-list (`.md`, `.png`, `.jpg`; no SVG, since it can carry script) and size limits.
- Markdown rules: no raw HTML, scripts or external image sources. Links to outside sites are allowed but listed in the MR for the PO.
- Secrets and PII scan; docs marked `classification: restricted` cannot publish to wide-access spaces.
- No AI runs inside the network at any step. Vendor AI use is disclosed in front matter and governed by contract: approved tools only, no training on company data, confidentiality and deletion terms.

**Backlog quality**

- Schema validation with line-numbered errors; nothing is repaired silently.
- Traceability: every epic carries `Source`, a link to the section of the doc it comes from.
- Size limits in CI: max 10 epics and 60 stories per doc, max 13 points per story; larger docs must be split.
- The project's product owner must approve the merge request.

**Audit and provenance**

- Git history, MR approvals and pipeline logs give a complete record of who approved what, and when.
- Confluence pages and Jira issues carry the `ai-generated` label and a link back to the MR.
- The JSON reports are kept as pipeline artifacts for 90 days; ship them to your log platform if compliance needs longer.

**Safety rails in the scripts**

- Never delete Jira issues; Confluence deletes only with an explicit prune flag.
- Never overwrite a page or issue the sync didn't create.
- A circuit breaker aborts a run that would create more than 100 issues or update more than 50% of managed ones, unless `DOCFLOW_ALLOW_LARGE=true` is set.

## Delivery plan

About 10 weeks to a multi-project rollout, with no AI inside the network at any point. Until the automated intake ships in Phase 3, staff submit the pilot vendor's deliveries by hand through the same checks.

&#91;embedded content: delivery roadmap · 4 phases, 3 gates\]

Phase 2 is the critical path: the backlog template, its parser and Jira sync ship together, because the PO needs all three to approve a backlog that lands in Jira. The Confluence half is already built.

**Team**

| Role | Allocation | Owns |
| --- | --- | --- |
| Architect / tech lead | 50% | Design, schemas, field-ownership rules, gate reviews |
| Python / DevOps engineers | 1–2 full time | Intake job, scripts, CI template, tests |
| Atlassian admin | \~20% | Service account, spaces, Jira field mapping, permissions |
| Security / infrastructure | \~10% | Secure drop, malware scanning, token handling |
| Vendor manager / procurement | Spot | Template and delivery rules in vendor statements of work; AI-use and confidentiality clauses |
| Pilot PO + tech lead | \~10% | Real deliveries, backlog approval, gate sign-off |

**Risks**

| Risk | Mitigation |
| --- | --- |
| Vendor deliveries don't follow the template | Template and validation rules in the SOW, a sample package to test against, rejection reports with line numbers sent straight back to the vendor |
| Company information shared with vendors ends up in their AI tools | Contract clauses: approved tools only, no training on our data, confidentiality and deletion terms; share only what the vendor needs |
| Unsafe content in a delivery | Quarantine, malware scan, file-type allow-list, no raw HTML or external images, PO review |
| Jira configuration differs between projects | Per-folder `_project.yaml` generated from Jira, sandbox gate before go-live |
| People edit Confluence or Jira fields that Git owns | Banner on pages, nightly drift report, "comment, don't overwrite" rule once work starts |
| Atlassian Cloud API changes or rate limits | Retries with backoff, one client class per product, contract tests against the sandbox |

**Decisions**

- [x] Confluence and Jira: Cloud or Data Center? Decided: Cloud for both.
- [x] Is AI allowed inside the network? Decided: no. Vendors use AI on their side; every internal step is plain code.
- [x] One central docs repo, or a `docs/` folder in each product repo? Decided: one central repo with a folder per project.
- [x] Who approves the backlog file? Decided: the project's product owner.

Still open:

- [ ] Pilot project (folder and Jira key), its product owner, and the first vendor.
- [ ] Secure drop: which platform is already approved for vendor file exchange (SFTP, managed file transfer, or a file share)?
- [ ] Is the GitLab tier Premium or above? Required code owner approval needs it; on Free, use a protected-branch rule plus a CI check that the PO approved.
- [ ] Does legal need more than the `ai_assisted` flag to disclose vendor AI use on published pages?
