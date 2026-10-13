#!/usr/bin/env python3
"""
confluence_sync.py - publish a tree of Markdown docs to Confluence from GitLab CI.

Behaviour
---------
* New .md file          -> new Confluence page
* Changed .md file      -> existing page updated (new page version)
* Unchanged .md file    -> skipped (no version noise in page history)
* Sub-folders           -> parent pages; a folder's README.md / index.md becomes
                           that folder's page, otherwise a "children" index page
* Local images / files  -> uploaded as page attachments
* Links between .md     -> rewritten to Confluence page links
* Deleted .md file      -> page removed only when --prune is passed

How pages are tracked
---------------------
Every page this script creates carries two labels:
    mdsync-<project>              (all pages from this repo)
    mdsync-<project>-<file-id>    (one specific source file / folder)
The file-id is derived from the file's path, so a page keeps its identity even
if its title changes. A content hash is stored in the page's version message,
which is how unchanged files are skipped.

Works with Confluence Cloud (email + API token) and Data Center / Server
(Personal Access Token). Configuration is read from environment variables so it
fits GitLab CI/CD variables; see README.md.
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import html
import html.entities
import json
import logging
import mimetypes
import os
import posixpath
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

import markdown
import requests
import yaml
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("confluence_sync")

# Bump when the converter output changes so every page is re-published once.
RENDER_SCHEMA_VERSION = "1"
HASH_TAG_RE = re.compile(r"\[mdsync:([0-9a-f]{16,64})\]")
INDEX_FILE_NAMES = ("index.md", "readme.md")  # preference order
PAGE_EXPAND = "version,ancestors,metadata.labels"


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name)
    if val is None or val.strip() == "":
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    docs_dir: Path
    repo_root: Path
    base_url: str | None
    user: str | None
    token: str | None
    space_key: str | None
    parent_id: str | None
    title_prefix: str
    excludes: list[str]
    namespace: str
    adopt_existing: bool
    verify_ssl: bool | str
    banner: bool
    project_url: str | None
    branch: str | None
    commit_sha: str

    @property
    def project_label(self) -> str:
        return "mdsync-" + hashlib.sha1(self.namespace.encode()).hexdigest()[:8]

    def node_label(self, key: str) -> str:
        return f"{self.project_label}-{hashlib.sha1(key.encode()).hexdigest()[:12]}"

    @classmethod
    def from_env(cls, args: argparse.Namespace) -> "Config":
        repo_root = Path(os.environ.get("CI_PROJECT_DIR", ".")).resolve()
        docs_dir = Path(args.docs_dir or os.environ.get("DOCS_DIR", "docs"))
        if not docs_dir.is_absolute():
            docs_dir = (repo_root / docs_dir).resolve()

        verify: bool | str = True
        ca = os.environ.get("CONFLUENCE_CA_BUNDLE")
        if ca:
            verify = ca
        elif not env_bool("CONFLUENCE_VERIFY_SSL", True):
            verify = False

        excludes = [e.strip() for e in os.environ.get("SYNC_EXCLUDE", "").split(",") if e.strip()]
        return cls(
            docs_dir=docs_dir,
            repo_root=repo_root,
            base_url=(os.environ.get("CONFLUENCE_BASE_URL") or "").rstrip("/") or None,
            user=os.environ.get("CONFLUENCE_USER") or None,
            token=os.environ.get("CONFLUENCE_API_TOKEN") or None,
            space_key=os.environ.get("CONFLUENCE_SPACE_KEY") or None,
            parent_id=os.environ.get("CONFLUENCE_PARENT_PAGE_ID") or None,
            title_prefix=os.environ.get("CONFLUENCE_TITLE_PREFIX", ""),
            excludes=excludes,
            namespace=os.environ.get("SYNC_NAMESPACE") or os.environ.get("CI_PROJECT_PATH") or "local",
            adopt_existing=env_bool("CONFLUENCE_ADOPT_EXISTING", False),
            verify_ssl=verify,
            banner=env_bool("SYNC_BANNER", True),
            project_url=os.environ.get("CI_PROJECT_URL"),
            branch=os.environ.get("CI_COMMIT_REF_NAME") or os.environ.get("CI_DEFAULT_BRANCH"),
            commit_sha=os.environ.get("CI_COMMIT_SHA", "local"),
        )

    def require_remote(self) -> None:
        missing = [n for n, v in (
            ("CONFLUENCE_BASE_URL", self.base_url),
            ("CONFLUENCE_API_TOKEN", self.token),
            ("CONFLUENCE_SPACE_KEY", self.space_key),
        ) if not v]
        if missing:
            raise SystemExit(f"Missing required environment variables: {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# Markdown -> Confluence storage format
# --------------------------------------------------------------------------- #
CODE_LANGS = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "jsx": "js",
    "typescript": "js", "ts": "js", "json": "js", "sh": "bash", "shell": "bash",
    "bash": "bash", "zsh": "bash", "console": "bash", "yaml": "yml", "yml": "yml",
    "xml": "xml", "html": "xml", "sql": "sql", "tsql": "sql", "plsql": "sql",
    "java": "java", "kotlin": "java", "csharp": "c#", "cs": "c#", "c#": "c#",
    "cpp": "cpp", "c++": "cpp", "c": "cpp", "css": "css", "scss": "sass",
    "sass": "sass", "diff": "diff", "patch": "diff", "groovy": "groovy",
    "gradle": "groovy", "php": "php", "perl": "perl", "powershell": "powershell",
    "ps1": "powershell", "ruby": "ruby", "rb": "ruby", "scala": "scala",
    "vb": "vb", "erlang": "erl", "applescript": "applescript",
}
ALERT_MACROS = {"note": "info", "tip": "tip", "important": "note", "warning": "warning", "caution": "warning"}
XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}

_ATTR_RE = re.compile(r'([\w:-]+)="([^"]*)"')
_IMG_RE = re.compile(r"<img\s+([^>]*?)\s*/?>")
_LINK_RE = re.compile(r"<a\s+([^>]*)>(.*?)</a>", re.DOTALL)
_CODE_RE = re.compile(r'<pre><code(?:\s+class="([^"]*)")?>(.*?)</code></pre>', re.DOTALL)
_ALERT_RE = re.compile(
    r"<blockquote>\s*<p>\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\][ \t]*\n?(.*?)</blockquote>",
    re.DOTALL | re.IGNORECASE,
)
_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
# Raw HTML void tags written in Markdown (<br>, <hr>) must be self-closed for XHTML.
_VOID_RE = re.compile(r"<(br|hr|wbr|col|input|source)\b([^<>]*?)\s*(?<!/)>", re.IGNORECASE)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.IGNORECASE)

_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
_H1_ATX_RE = re.compile(r"\A\s*#[ \t]+(.+?)[ \t]*#*[ \t]*(?:\n|\Z)")
_H1_SETEXT_RE = re.compile(r"\A\s*([^\n]+)\n=+[ \t]*(?:\n|\Z)")


def pretty_title(name: str) -> str:
    words = re.sub(r"[-_]+", " ", name).split()
    return " ".join(w[:1].upper() + w[1:] for w in words) or name


def parse_source(text: str, fallback_title: str) -> tuple[str, str, dict]:
    """Split front matter and H1 off a Markdown file. Returns (title, body, meta)."""
    text = text.lstrip("\ufeff")
    meta: dict = {}
    m = _FRONT_MATTER_RE.match(text)
    if m:
        try:
            loaded = yaml.safe_load(m.group(1))
            meta = loaded if isinstance(loaded, dict) else {}
        except yaml.YAMLError as exc:
            log.warning("Ignoring invalid front matter: %s", exc)
        text = text[m.end():]

    title = str(meta["title"]).strip() if meta.get("title") else None
    h1 = _H1_ATX_RE.match(text) or _H1_SETEXT_RE.match(text)
    if h1:
        h1_text = h1.group(1).strip()
        # Confluence shows the page title itself, so drop a duplicate leading H1.
        if title is None or title == h1_text:
            title = h1_text
            text = text[h1.end():]
    return title or fallback_title, text, meta


def _numeric_entities(s: str) -> str:
    """Confluence storage is XHTML: turn named HTML entities (&nbsp;) into numeric ones."""
    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in XML_ENTITIES:
            return m.group(0)
        chars = html.entities.html5.get(name + ";")
        return "".join(f"&#{ord(c)};" for c in chars) if chars else m.group(0)
    return _ENTITY_RE.sub(sub, s)


def check_xhtml(storage: str) -> str | None:
    """Return an error message if the storage body is not well-formed XML."""
    try:
        ET.fromstring(f'<root xmlns:ac="urn:ac" xmlns:ri="urn:ri">{storage}</root>')
        return None
    except ET.ParseError as exc:
        return str(exc)


@dataclass
class RenderResult:
    storage: str
    attachments: dict[str, Path]
    warnings: list[str]


class Renderer:
    def __init__(self, docs_root: Path, repo_root: Path, title_for_path: dict[Path, str]):
        self.docs_root = docs_root.resolve()
        self.repo_root = repo_root.resolve()
        self.title_for_path = title_for_path

    def render(self, md_text: str, source_rel: str) -> RenderResult:
        attachments: dict[str, Path] = {}
        warnings: list[str] = []
        src_dir = self.docs_root / posixpath.dirname(source_rel)

        def resolve(ref: str) -> Path | None:
            raw = html.unescape(ref).strip()
            if not raw or raw.startswith("#") or raw.startswith("/") or _SCHEME_RE.match(raw):
                return None
            path = unquote(urlparse(raw).path)
            if not path:
                return None
            candidate = (src_dir / path).resolve()
            try:
                candidate.relative_to(self.repo_root)
            except ValueError:
                return None  # never reach outside the repository
            return candidate

        def attach(target: Path) -> str:
            name = target.name
            if name in attachments and attachments[name] != target:
                digest = hashlib.sha1(str(target).encode()).hexdigest()[:6]
                name = f"{target.stem}-{digest}{target.suffix}"
            attachments[name] = target
            return html.escape(name, quote=True)

        def img_sub(m: re.Match) -> str:
            attrs = dict(_ATTR_RE.findall(m.group(1)))
            src, alt = attrs.get("src", ""), attrs.get("alt", "")
            alt_attr = f' ac:alt="{alt}"' if alt else ""
            if _SCHEME_RE.match(html.unescape(src)):
                return f'<ac:image{alt_attr}><ri:url ri:value="{src}" /></ac:image>'
            target = resolve(src)
            if target and target.is_file():
                return f'<ac:image{alt_attr}><ri:attachment ri:filename="{attach(target)}" /></ac:image>'
            warnings.append(f"{source_rel}: image not found: {html.unescape(src)}")
            return m.group(0)

        def link_sub(m: re.Match) -> str:
            attrs = dict(_ATTR_RE.findall(m.group(1)))
            href, body = attrs.get("href", ""), m.group(2)
            target = resolve(href)
            if target is None:
                return m.group(0)  # external URL, in-page anchor, mailto, etc.
            if target.suffix.lower() == ".md" or target.is_dir():
                title = self.title_for_path.get(target)
                if title:
                    return (f'<ac:link><ri:page ri:content-title="{html.escape(title, quote=True)}" />'
                            f"<ac:link-body>{body}</ac:link-body></ac:link>")
                warnings.append(f"{source_rel}: link to a page that is not published: {html.unescape(href)}")
                return m.group(0)
            if target.is_file():
                return (f'<ac:link><ri:attachment ri:filename="{attach(target)}" />'
                        f"<ac:link-body>{body}</ac:link-body></ac:link>")
            warnings.append(f"{source_rel}: broken link: {html.unescape(href)}")
            return m.group(0)

        def alert_sub(m: re.Match) -> str:
            macro = ALERT_MACROS[m.group(1).lower()]
            body = re.sub(r"^<p>\s*</p>\s*", "", "<p>" + m.group(2).strip())
            return (f'<ac:structured-macro ac:name="{macro}" ac:schema-version="1">'
                    f"<ac:rich-text-body>{body}</ac:rich-text-body></ac:structured-macro>")

        def code_sub(m: re.Match) -> str:
            lang = next((c[len("language-"):] for c in (m.group(1) or "").split()
                         if c.startswith("language-")), "")
            code = html.unescape(m.group(2))
            if code.endswith("\n"):
                code = code[:-1]
            code = code.replace("]]>", "]]]]><![CDATA[>")
            return ('<ac:structured-macro ac:name="code" ac:schema-version="1">'
                    f'<ac:parameter ac:name="language">{CODE_LANGS.get(lang.lower(), "text")}</ac:parameter>'
                    f"<ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body></ac:structured-macro>")

        out = markdown.markdown(md_text, extensions=["extra", "sane_lists"], output_format="xhtml")
        # Order matters: code blocks go last because CDATA contents must not be rewritten.
        out = _IMG_RE.sub(img_sub, out)
        out = _LINK_RE.sub(link_sub, out)
        out = _ALERT_RE.sub(alert_sub, out)
        out = _numeric_entities(out)
        out = _VOID_RE.sub(lambda m: f"<{m.group(1).lower()}{m.group(2)} />", out)
        out = _CODE_RE.sub(code_sub, out)

        xml_error = check_xhtml(out)
        if xml_error:
            warnings.append(f"{source_rel}: output is not valid XHTML ({xml_error}); "
                            "check raw HTML in the file - Confluence will likely reject it")
        return RenderResult(out, attachments, warnings)


# --------------------------------------------------------------------------- #
# Plan: which pages exist, their titles and hierarchy
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    key: str                      # "file:guides/setup.md" or "dir:guides"
    title: str
    parent_key: str | None
    source: Path | None           # None => generated folder index page
    source_rel: str | None        # path relative to docs root
    body_md: str = ""
    depth: int = 0
    storage: str = ""
    attachments: dict[str, Path] = field(default_factory=dict)
    content_hash: str = ""


class PlanError(Exception):
    pass


def _is_excluded(rel: str, excludes: list[str]) -> bool:
    if any(part.startswith(".") for part in rel.split("/")):
        return True
    return any(fnmatch.fnmatch(rel, pat) for pat in excludes)


def build_plan(cfg: Config) -> list[Node]:
    root = cfg.docs_dir
    if not root.is_dir():
        raise PlanError(f"Docs directory not found: {root}")

    files: dict[str, tuple[Path, str, str, dict]] = {}
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or _is_excluded(rel, cfg.excludes):
            continue
        title, body, meta = parse_source(path.read_text(encoding="utf-8"), pretty_title(path.stem))
        if meta.get("publish") is False or meta.get("confluence") is False:
            log.info("Skipping %s (publish: false)", rel)
            continue
        files[rel] = (path, title, body, meta)

    # Folders that (transitively) contain at least one published file.
    dirs: set[str] = set()
    for rel in files:
        d = posixpath.dirname(rel)
        while d:
            dirs.add(d)
            d = posixpath.dirname(d)

    # Pick an index file (index.md, then README.md) for each folder.
    index_for: dict[str, str] = {}
    for d in dirs:
        for name in INDEX_FILE_NAMES:
            match = next((r for r in files if posixpath.dirname(r) == d
                          and posixpath.basename(r).lower() == name), None)
            if match:
                index_for[d] = match
                break

    def dir_key(d: str) -> str | None:
        return f"dir:{d}" if d else None

    nodes: list[Node] = []
    for d in sorted(dirs):
        idx = index_for.get(d)
        if idx:
            path, title, body, _ = files[idx]
            nodes.append(Node(dir_key(d), cfg.title_prefix + title, dir_key(posixpath.dirname(d)),
                              path, idx, body, d.count("/") + 1))
        else:
            nodes.append(Node(dir_key(d), cfg.title_prefix + pretty_title(posixpath.basename(d)),
                              dir_key(posixpath.dirname(d)), None, None, "", d.count("/") + 1))
    used_as_index = set(index_for.values())
    for rel, (path, title, body, _) in files.items():
        if rel in used_as_index:
            continue
        nodes.append(Node(f"file:{rel}", cfg.title_prefix + title, dir_key(posixpath.dirname(rel)),
                          path, rel, body, rel.count("/") + 1))

    # Confluence titles must be unique within a space: fail early and clearly.
    dupes = [t for t, n in Counter(n.title for n in nodes).items() if n > 1]
    if dupes:
        lines = [f'  "{t}": ' + ", ".join(n.source_rel or n.key for n in nodes if n.title == t) for t in dupes]
        raise PlanError("Duplicate page titles (Confluence requires unique titles per space). "
                        "Set a `title:` in front matter or use CONFLUENCE_TITLE_PREFIX:\n" + "\n".join(lines))

    # Parents always before children.
    nodes.sort(key=lambda n: (n.depth, n.key.startswith("file:"), n.key))
    return nodes


def render_plan(nodes: list[Node], cfg: Config) -> list[str]:
    """Render every node to storage format and compute content hashes. Returns warnings."""
    title_for_path: dict[Path, str] = {}
    for n in nodes:
        if n.source:
            title_for_path[n.source.resolve()] = n.title
        if n.key.startswith("dir:"):
            title_for_path[(cfg.docs_dir / n.key[4:]).resolve()] = n.title

    renderer = Renderer(cfg.docs_dir, cfg.repo_root, title_for_path)
    warnings: list[str] = []
    for n in nodes:
        if n.source is None:
            folder = html.escape(n.key[4:])
            n.storage = (f"<p>Pages under <code>{folder}/</code>:</p>"
                         '<ac:structured-macro ac:name="children" ac:schema-version="2" />')
        else:
            result = renderer.render(n.body_md, n.source_rel or "")
            n.storage, n.attachments = result.storage, result.attachments
            warnings.extend(result.warnings)
            if cfg.banner:
                n.storage = _banner(n, cfg) + n.storage

        h = hashlib.sha256()
        h.update(json.dumps({"v": RENDER_SCHEMA_VERSION, "title": n.title,
                             "parent": n.parent_key, "storage": n.storage}).encode())
        for name in sorted(n.attachments):
            h.update(name.encode())
            h.update(hashlib.sha256(n.attachments[name].read_bytes()).digest())
        n.content_hash = h.hexdigest()[:20]
    return warnings


def _banner(n: Node, cfg: Config) -> str:
    repo_rel = n.source.resolve().relative_to(cfg.repo_root).as_posix() if n.source else ""
    if cfg.project_url and cfg.branch:
        src = f'<a href="{html.escape(f"{cfg.project_url}/-/blob/{cfg.branch}/{repo_rel}")}">{html.escape(repo_rel)}</a>'
    else:
        src = f"<code>{html.escape(repo_rel)}</code>"
    return ('<ac:structured-macro ac:name="info" ac:schema-version="1"><ac:rich-text-body>'
            f"<p>This page is generated from {src} in Git. "
            "Edits made directly in Confluence will be overwritten by the next sync.</p>"
            "</ac:rich-text-body></ac:structured-macro>")


# --------------------------------------------------------------------------- #
# Confluence REST client (v1 content API: works on Cloud and Data Center)
# --------------------------------------------------------------------------- #
class ConfluenceError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class ConfluenceClient:
    def __init__(self, base_url: str, token: str, user: str | None = None,
                 verify: bool | str = True, timeout: int = 60):
        self.base_url = base_url.rstrip("/")
        self.api = self.base_url + "/rest/api"
        self.timeout = timeout
        s = requests.Session()
        if user:
            s.auth = (user, token)                           # Cloud: email + API token
        else:
            s.headers["Authorization"] = f"Bearer {token}"   # Data Center: PAT
        s.headers["Accept"] = "application/json"
        s.verify = verify
        retry = Retry(total=5, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=None, respect_retry_after_header=True, raise_on_status=False)
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        self.session = s

    def _req(self, method: str, url: str, **kw: Any) -> Any:
        if not url.startswith("http"):
            url = self.api + url
        resp = self.session.request(method, url, timeout=self.timeout, **kw)
        if resp.status_code >= 400:
            raise ConfluenceError(f"{method} {url} -> HTTP {resp.status_code}: {resp.text[:600]}",
                                  resp.status_code)
        if resp.content and "json" in resp.headers.get("Content-Type", ""):
            return resp.json()
        return None

    def get_page(self, page_id: str) -> dict:
        return self._req("GET", f"/content/{page_id}", params={"expand": "version,space"})

    def iter_managed(self, space: str, label: str) -> Iterator[dict]:
        url: str | None = "/content/search"
        params: dict | None = {"cql": f'space = "{space}" and type = page and label = "{label}"',
                               "limit": 50, "expand": PAGE_EXPAND}
        while url:
            data = self._req("GET", url, params=params)
            yield from data.get("results", [])
            nxt = data.get("_links", {}).get("next")
            url = (data["_links"].get("base", self.base_url) + nxt) if nxt else None
            params = None  # the "next" link already carries the query

    def find_by_title(self, space: str, title: str) -> dict | None:
        data = self._req("GET", "/content", params={"spaceKey": space, "title": title,
                                                    "type": "page", "expand": PAGE_EXPAND})
        results = data.get("results", [])
        return results[0] if results else None

    def create_page(self, space: str, title: str, storage: str, parent_id: str | None,
                    labels: list[str], message: str) -> dict:
        body: dict[str, Any] = {
            "type": "page", "title": title, "space": {"key": space},
            "body": {"storage": {"value": storage, "representation": "storage"}},
            "metadata": {"labels": [{"prefix": "global", "name": lb} for lb in labels]},
            "version": {"number": 1, "message": message},
        }
        if parent_id:
            body["ancestors"] = [{"id": str(parent_id)}]
        try:
            return self._req("POST", "/content", json=body)
        except ConfluenceError as exc:
            if exc.status != 400 or "version" not in str(exc).lower():
                raise
            body.pop("version")  # some versions reject a version block on create
            return self._req("POST", "/content", json=body)

    def update_page(self, page_id: str, space: str, title: str, storage: str,
                    parent_id: str | None, current_version: int, message: str) -> dict:
        body: dict[str, Any] = {
            "id": page_id, "type": "page", "title": title, "space": {"key": space},
            "body": {"storage": {"value": storage, "representation": "storage"}},
            "version": {"number": current_version + 1, "message": message, "minorEdit": True},
        }
        if parent_id:
            body["ancestors"] = [{"id": str(parent_id)}]
        try:
            return self._req("PUT", f"/content/{page_id}", json=body)
        except ConfluenceError as exc:
            if exc.status != 409:
                raise
            latest = self.get_page(page_id)["version"]["number"]  # someone edited meanwhile
            body["version"]["number"] = latest + 1
            return self._req("PUT", f"/content/{page_id}", json=body)

    def add_labels(self, page_id: str, labels: list[str]) -> None:
        self._req("POST", f"/content/{page_id}/label",
                  json=[{"prefix": "global", "name": lb} for lb in labels])

    def upload_attachment(self, page_id: str, name: str, path: Path) -> None:
        headers = {"X-Atlassian-Token": "no-check"}
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        data = path.read_bytes()

        def files() -> dict:
            return {"file": (name, data, mime)}

        try:  # Cloud and newer DC: create-or-update in one call
            self._req("PUT", f"/content/{page_id}/child/attachment", headers=headers,
                      files=files(), data={"minorEdit": "true"})
            return
        except ConfluenceError as exc:
            if exc.status not in (404, 405):
                raise
        existing = self._req("GET", f"/content/{page_id}/child/attachment",
                             params={"filename": name}).get("results", [])
        if existing:
            self._req("POST", f"/content/{page_id}/child/attachment/{existing[0]['id']}/data",
                      headers=headers, files=files(), data={"minorEdit": "true"})
        else:
            self._req("POST", f"/content/{page_id}/child/attachment", headers=headers,
                      files=files(), data={"minorEdit": "true"})

    def delete_page(self, page_id: str) -> None:
        self._req("DELETE", f"/content/{page_id}")


# --------------------------------------------------------------------------- #
# Sync engine
# --------------------------------------------------------------------------- #
@dataclass
class Outcome:
    key: str
    title: str
    action: str                   # created | updated | unchanged | deleted | failed | skipped
    page_id: str | None = None
    detail: str | None = None


def _labels_of(page: dict) -> set[str]:
    return {lb["name"] for lb in page.get("metadata", {}).get("labels", {}).get("results", [])}


def _stored_hash(page: dict) -> str | None:
    m = HASH_TAG_RE.search((page.get("version") or {}).get("message") or "")
    return m.group(1) if m else None


def sync(nodes: list[Node], client: ConfluenceClient, cfg: Config, *,
         dry_run: bool = False, force: bool = False, prune: bool = False) -> list[Outcome]:
    space = cfg.space_key or ""
    project_label = cfg.project_label
    managed: dict[str, dict] = {}
    for page in client.iter_managed(space, project_label):
        for lb in _labels_of(page):
            if lb.startswith(project_label + "-"):
                managed[lb] = page
    log.info("Found %d page(s) previously published from this repo", len(managed))

    outcomes: list[Outcome] = []
    ids: dict[str, str] = {}
    failed: set[str] = set()
    wanted_labels: set[str] = set()
    prefix = "[dry-run] " if dry_run else ""

    for n in nodes:
        label = cfg.node_label(n.key)
        wanted_labels.add(label)
        if n.parent_key and n.parent_key in failed:
            failed.add(n.key)
            outcomes.append(Outcome(n.key, n.title, "skipped", detail="parent page failed"))
            continue
        parent_id = ids.get(n.parent_key) if n.parent_key else cfg.parent_id
        message = f"md-sync {cfg.commit_sha[:8]} {n.source_rel or n.key} [mdsync:{n.content_hash}]"
        try:
            page = managed.get(label)
            if page is None:
                page = client.find_by_title(space, n.title)
                if page is not None and project_label not in _labels_of(page) and not cfg.adopt_existing:
                    raise ConfluenceError(
                        f'A page titled "{n.title}" already exists in space {space} and was not created '
                        "by this sync. Rename the doc, or set CONFLUENCE_ADOPT_EXISTING=true to take it over.")

            if page is None:
                log.info("%sCREATE  %s  <- %s", prefix, n.title, n.source_rel or n.key)
                page_id = f"<new:{n.key}>"
                if not dry_run:
                    created = client.create_page(space, n.title, n.storage, parent_id,
                                                 [project_label, label], message)
                    page_id = created["id"]
                    for name, path in n.attachments.items():
                        client.upload_attachment(page_id, name, path)
                ids[n.key] = page_id
                outcomes.append(Outcome(n.key, n.title, "created", page_id))
                continue

            page_id = page["id"]
            ids[n.key] = page_id
            if not force and _stored_hash(page) == n.content_hash:
                log.debug("UNCHANGED %s", n.title)
                outcomes.append(Outcome(n.key, n.title, "unchanged", page_id))
                continue

            log.info("%sUPDATE  %s  <- %s", prefix, n.title, n.source_rel or n.key)
            if not dry_run:
                for name, path in n.attachments.items():  # upload first so images resolve
                    client.upload_attachment(page_id, name, path)
                client.update_page(page_id, space, n.title, n.storage, parent_id,
                                   page["version"]["number"], message)
                missing = [lb for lb in (project_label, label) if lb not in _labels_of(page)]
                if missing:
                    client.add_labels(page_id, missing)
            outcomes.append(Outcome(n.key, n.title, "updated", page_id))
        except (ConfluenceError, requests.RequestException, OSError) as exc:
            log.error("FAILED  %s: %s", n.title, exc)
            failed.add(n.key)
            outcomes.append(Outcome(n.key, n.title, "failed", detail=str(exc)))

    if prune:
        stale = [p for lb, p in managed.items() if lb not in wanted_labels]
        # Delete deepest pages first so parents are not removed before their children.
        stale.sort(key=lambda p: len(p.get("ancestors", [])), reverse=True)
        for page in stale:
            log.info("%sDELETE  %s (source file removed)", prefix, page["title"])
            try:
                if not dry_run:
                    client.delete_page(page["id"])
                outcomes.append(Outcome("", page["title"], "deleted", page["id"]))
            except (ConfluenceError, requests.RequestException) as exc:
                log.error("FAILED to delete %s: %s", page["title"], exc)
                outcomes.append(Outcome("", page["title"], "failed", page["id"], str(exc)))
    return outcomes


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Publish Markdown docs to Confluence.")
    p.add_argument("--docs-dir", help="Docs folder (default: $DOCS_DIR or ./docs)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--validate-only", action="store_true",
                      help="Convert everything locally and report problems; no network calls")
    mode.add_argument("--dry-run", action="store_true",
                      help="Compare with Confluence and print what would change; no writes")
    p.add_argument("--force", action="store_true", default=env_bool("SYNC_FORCE"),
                   help="Re-publish every page even if unchanged (env: SYNC_FORCE)")
    p.add_argument("--prune", action="store_true", default=env_bool("SYNC_PRUNE"),
                   help="Delete pages whose source file was removed (env: SYNC_PRUNE)")
    p.add_argument("--strict", action="store_true", default=env_bool("SYNC_STRICT"),
                   help="Treat conversion warnings (broken links, missing images) as failures")
    p.add_argument("--report", help="Write a JSON report to this path")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)-7s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    cfg = Config.from_env(args)

    try:
        nodes = build_plan(cfg)
    except PlanError as exc:
        log.error("%s", exc)
        return 2
    warnings = render_plan(nodes, cfg)
    log.info("Planned %d page(s) from %s", len(nodes), cfg.docs_dir)
    for w in warnings:
        log.warning("%s", w)

    outcomes: list[Outcome] = []
    if not args.validate_only:
        cfg.require_remote()
        client = ConfluenceClient(cfg.base_url or "", cfg.token or "", cfg.user, cfg.verify_ssl)
        try:
            if cfg.parent_id:
                parent = client.get_page(cfg.parent_id)
            else:
                client._req("GET", "/space/" + (cfg.space_key or ""))  # connectivity + auth check
        except ConfluenceError as exc:
            if exc.status in (401, 403):
                log.error("Confluence rejected the credentials (HTTP %s). Check CONFLUENCE_USER / "
                          "CONFLUENCE_API_TOKEN and that the account can see space %s.",
                          exc.status, cfg.space_key)
            else:
                log.error("Cannot read %s: %s", f"parent page {cfg.parent_id}" if cfg.parent_id
                          else f"space {cfg.space_key}", exc)
            return 2
        except requests.RequestException as exc:
            log.error("Cannot reach Confluence at %s: %s", cfg.base_url, exc.__class__.__name__)
            log.debug("%s", exc)
            return 2
        if cfg.parent_id:
            if parent["space"]["key"] != cfg.space_key:
                log.error("Parent page %s is in space %s, not %s",
                          cfg.parent_id, parent["space"]["key"], cfg.space_key)
                return 2
        try:
            outcomes = sync(nodes, client, cfg, dry_run=args.dry_run, force=args.force, prune=args.prune)
        except (ConfluenceError, requests.RequestException) as exc:
            log.error("Sync aborted: %s", exc)
            return 2

    counts = Counter(o.action for o in outcomes)
    summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "validation only"
    log.info("Done: %s; %d warning(s)", summary, len(warnings))

    if args.report:
        Path(args.report).write_text(json.dumps({
            "commit": cfg.commit_sha, "space": cfg.space_key, "dry_run": args.dry_run,
            "validate_only": args.validate_only, "counts": counts, "warnings": warnings,
            "pages": [asdict(o) for o in outcomes],
        }, indent=2), encoding="utf-8")

    if counts.get("failed") or counts.get("skipped"):
        return 1
    if args.strict and warnings:
        log.error("Strict mode: %d warning(s) treated as errors", len(warnings))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
