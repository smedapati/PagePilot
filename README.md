# Markdown → Confluence sync for GitLab CI

Publishes every `.md` file under `docs/` to a Confluence space on each merge to the default branch.

| In Git | In Confluence |
|---|---|
| New `.md` file | New page |
| Changed `.md` file (or a changed image it uses) | Same page updated, new version in page history |
| Unchanged file | Skipped, so no version noise |
| Folder | Parent page. Its `index.md` or `README.md` becomes the folder page; otherwise a page listing its children is generated |
| Relative image / file link | Uploaded as a page attachment |
| Link to another `.md` | Confluence page link |
| Code fence, table, `> [!NOTE]`/`[!WARNING]` callout | Code macro, table, Info/Warning macro |
| Deleted `.md` file | Page deleted only when `SYNC_PRUNE=true` |

Each page gets a banner linking back to its source file in GitLab and noting that Confluence edits will be overwritten.

## Layout

```
.gitlab-ci.yml                     # validate on MRs, publish on default branch
ci/confluence-sync/
  confluence_sync.py               # the sync script
  requirements.txt
  tests/test_sync.py               # pytest, no network needed
docs/                              # your Markdown (sample included)
```

Copy `ci/confluence-sync/` into your docs repo and merge the jobs from `.gitlab-ci.yml` into yours.

## Setup

1. **Create a credential.**
   - *Confluence Cloud:* create an API token at id.atlassian.com → Security → API tokens, ideally for a service account.
   - *Confluence Data Center / Server:* create a Personal Access Token from your profile → Personal Access Tokens.
   The account needs *Add / Edit / Delete pages* and *Add attachments* in the target space.
2. **Find the parent page ID** (optional): open the page, the ID is in the URL (`/pages/123456/...`) or under ••• → Page information.
3. **Add CI/CD variables** in GitLab (Settings → CI/CD → Variables). Mark the token *Masked* and *Protected*.

| Variable | Required | Example |
|---|---|---|
| `CONFLUENCE_BASE_URL` | yes | Cloud: `https://yourco.atlassian.net/wiki` · DC: `https://confluence.yourco.com` |
| `CONFLUENCE_API_TOKEN` | yes | API token (Cloud) or PAT (DC) |
| `CONFLUENCE_SPACE_KEY` | yes | `ENG` |
| `CONFLUENCE_USER` | Cloud only | `svc-docs@yourco.com`. Leave unset on DC, the PAT is sent as a Bearer token |
| `CONFLUENCE_PARENT_PAGE_ID` | no | `123456`. Pages go under it instead of at the space root |
| `CONFLUENCE_TITLE_PREFIX` | no | `[Payments] ` |
| `DOCS_DIR` | no | defaults to `docs` |
| `SYNC_EXCLUDE` | no | `drafts/*,*.internal.md` |
| `SYNC_PRUNE` | no | `true` to delete pages for removed files |
| `CONFLUENCE_CA_BUNDLE` | no | path to your corporate CA bundle for self-hosted Confluence |
| `CONFLUENCE_ADOPT_EXISTING` | no | `true` to take over pages with matching titles that were created by hand |

4. Run the pipeline manually once (CI/CD → Run pipeline on the default branch) to do the first full publish.

## Pipeline behaviour

- **Merge requests:** `docs:validate` converts every file locally with `--strict` (broken links, missing images or invalid markup fail the MR). If the token is visible to MR pipelines it also prints a dry run of what would change.
- **Default branch:** `docs:publish` syncs. A `resource_group` stops two pipelines publishing at once.
- **Schedules / manual runs** also publish, which is a handy way to undo manual edits made in Confluence.
- A JSON report (`confluence-sync-report.json`) is kept as a job artifact.

## Writing docs

- **Page title** comes from front matter `title:`, else the first `# Heading` (which is then dropped from the body since Confluence shows the title), else the file name.
- **Titles must be unique within the space** (a Confluence rule). The script checks this before touching anything. Use `title:` or `CONFLUENCE_TITLE_PREFIX` when two files would clash.
- Skip a file with front matter `publish: false`. Files and folders starting with `.` are always skipped.

```markdown
---
title: Database Failover Runbook
---
> [!WARNING]
> Never fail over during the nightly batch window.
```

## How pages are tracked

The script needs no state file. Each page it creates gets two labels: one for the repo and one derived from the source file's path. On the next run it finds its pages by label, so renaming a page's title keeps the same page and history. A hash of the rendered content is stored in the version comment, which is how unchanged files are skipped.

The script will not overwrite a page it did not create. If a page with the same title already exists, that file fails with a clear message, unless you set `CONFLUENCE_ADOPT_EXISTING=true`.

Moving a file to a different path is treated as a new page (and, with prune on, the old one is deleted). Use `title:` front matter if you need a stable title across moves.

## Running locally

```bash
pip install -r ci/confluence-sync/requirements.txt
python ci/confluence-sync/confluence_sync.py --validate-only     # no network
export CONFLUENCE_BASE_URL=... CONFLUENCE_API_TOKEN=... CONFLUENCE_SPACE_KEY=...
python ci/confluence-sync/confluence_sync.py --dry-run           # show what would change
python ci/confluence-sync/confluence_sync.py                     # publish
```

Other flags: `--force` re-publishes everything (use after changing the script's output), `--prune`, `--strict`, `--report FILE`, `-v`.

Exit codes: `0` success, `1` some pages failed (or warnings in `--strict`), `2` configuration, credential or connection problem.

## Limitations

- Mermaid/PlantUML blocks are published as code blocks, since rendering them needs a Confluence app your instance may not have.
- Raw HTML in Markdown is passed through; if it is not valid XHTML the file is flagged during validation.
- Uses the v1 REST content API, which both Cloud and Data Center support.
