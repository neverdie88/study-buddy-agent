# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""School-materials retrieval tools (RAG over the kid's real class content).

Retrieval backends:

* **Vertex AI Search** (``discoveryengine``) when ``BUDDY_VERTEX_SEARCH_DATASTORE``
  is configured - the production path for a parent's uploaded PDFs/Docs.
* **Persistent SQLite FTS5 index** built from ``data/sample_school_materials``
  (BM25 ranking). It is stored on disk and only rebuilt when files change, so
  it works offline, in CI and on a single Cloud Run instance.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.observability.tracing import traced_span
from app.schemas.curriculum_schemas import (
    ListStudyGuidesOutput,
    MaterialPassage,
    SearchSchoolMaterialsInput,
    SearchSchoolMaterialsOutput,
    StudyGuideSummary,
    Subject,
)
from app.tools.errors import guided_error, validation_error

logger = get_logger(__name__)

_FRONT_MATTER = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def _parse_material(path: Path) -> tuple[dict, list[tuple[str, str]]]:
    """Return (metadata, [(section_heading, section_text), ...])."""
    raw = path.read_text(encoding="utf-8")
    meta: dict = {"title": path.stem, "subject": "any", "topics": []}
    match = _FRONT_MATTER.match(raw)
    if match:
        for line in match.group(1).splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                value = value.strip()
                if value.startswith("[") and value.endswith("]"):
                    meta[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
                else:
                    meta[key.strip()] = value
        raw = raw[match.end():]

    sections: list[tuple[str, str]] = []
    heading, buf = meta["title"], []
    for line in raw.splitlines():
        if line.startswith("#"):
            if "".join(buf).strip():
                sections.append((heading, "\n".join(buf).strip()))
            heading, buf = line.lstrip("#").strip(), []
        else:
            buf.append(line)
    if "".join(buf).strip():
        sections.append((heading, "\n".join(buf).strip()))
    return meta, sections


class LocalMaterialIndex:
    """Persistent BM25 full-text index over the school materials folder."""

    def __init__(self, materials_dir: str, db_path: str) -> None:
        self._dir = Path(materials_dir)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._lock = threading.Lock()
        self.guides: list[StudyGuideSummary] = []
        self._ensure_index()

    def _fingerprint(self) -> str:
        h = hashlib.sha256()
        for p in sorted(self._dir.glob("*.md")):
            h.update(p.name.encode())
            h.update(p.read_bytes())
        return h.hexdigest()

    def _ensure_index(self) -> None:
        fingerprint = self._fingerprint()
        with self._lock, self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
            self._conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(source_title, subject, heading, body)"
            )
            row = self._conn.execute("SELECT v FROM meta WHERE k='fingerprint'").fetchone()
            rebuild = not row or row[0] != fingerprint

            if rebuild:
                self._conn.execute("DELETE FROM passages")
            for path in sorted(self._dir.glob("*.md")):
                meta, sections = _parse_material(path)
                subject = meta.get("subject", "any")
                self.guides.append(
                    StudyGuideSummary(
                        title=meta["title"],
                        subject=Subject(subject) if subject in Subject._value2member_map_ else Subject.ANY,
                        topics=list(meta.get("topics", [])),
                    )
                )
                if rebuild:
                    for heading, body in sections:
                        self._conn.execute(
                            "INSERT INTO passages(source_title, subject, heading, body) VALUES (?,?,?,?)",
                            (meta["title"], subject, heading, body),
                        )
            if rebuild:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta(k, v) VALUES('fingerprint', ?)", (fingerprint,)
                )
                logger.info("material_index_rebuilt", extra={"guides": len(self.guides)})

    def search(self, query: str, subject: Subject, limit: int) -> list[MaterialPassage]:
        terms = [t for t in re.findall(r"[A-Za-z0-9]+", query.lower()) if len(t) > 2]
        if not terms:
            return []
        fts_query = " OR ".join(f'"{t}"' for t in terms)
        sql = "SELECT source_title, subject, heading, body, bm25(passages) AS score FROM passages WHERE passages MATCH ?"
        params: list = [fts_query]
        if subject != Subject.ANY:
            sql += " AND subject IN (?, 'any')"
            params.append(subject.value)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return []
        best = min(r[4] for r in rows)  # bm25: lower (more negative) is better
        passages = []
        for title, subj, heading, body, score in rows:
            relevance = round(min(1.0, max(0.05, score / best if best else 0.0)), 3)
            passages.append(
                MaterialPassage(
                    source_title=f"{title} > {heading}",
                    subject=Subject(subj) if subj in Subject._value2member_map_ else Subject.ANY,
                    excerpt=body[:900],
                    relevance_score=relevance,
                )
            )
        return passages


def _search_vertex_ai(query: str, limit: int) -> list[MaterialPassage]:
    """Query a Vertex AI Search data store of the parent's uploaded materials."""
    from google.cloud import discoveryengine_v1 as discoveryengine

    settings = get_settings()
    client = discoveryengine.SearchServiceClient()
    serving_config = f"{settings.vertex_search_datastore}/servingConfigs/default_search"
    response = client.search(
        discoveryengine.SearchRequest(
            serving_config=serving_config,
            query=query,
            page_size=limit,
            content_search_spec={"snippet_spec": {"return_snippet": True}},
        )
    )
    passages = []
    for result in response.results:
        data = result.document.derived_struct_data or {}
        snippets = data.get("snippets", [{}])
        passages.append(
            MaterialPassage(
                source_title=str(data.get("title", result.document.id)),
                subject=Subject.ANY,
                excerpt=str(snippets[0].get("snippet", ""))[:900] if snippets else "",
                relevance_score=0.8,
            )
        )
    return passages


@lru_cache(maxsize=1)
def get_material_index() -> LocalMaterialIndex:
    settings = get_settings()
    db_path = str(Path(settings.sqlite_path).with_name("school_materials_index.db"))
    return LocalMaterialIndex(settings.materials_dir, db_path)


# --------------------------------------------------------------------------- #
# ADK tools
# --------------------------------------------------------------------------- #
def search_school_materials(query: str, subject: str = "any", max_results: int = 3) -> dict:
    """Search the kid's own school materials (study guides, worksheets, syllabus).

    Always call this BEFORE explaining a homework concept so the explanation
    matches exactly how the kid's teacher taught it in class.

    Args:
        query: What the kid is studying or stuck on, in plain words
            (e.g. "adding fractions with different denominators").
        subject: One of "math", "science", "english", "history" or "any".
        max_results: How many passages to return, between 1 and 5.

    Returns:
        dict: On success ``{"status": "success", "passages": [{"source_title",
        "subject", "excerpt", "relevance_score"}], "retrieval_backend": str}``.
        On failure a guided error ``{"status": "error", "error_code",
        "message", "recovery_instructions"}``.
    """
    try:
        args = SearchSchoolMaterialsInput(query=query, subject=subject, max_results=max_results)
    except ValidationError as exc:
        return validation_error(exc, "search_school_materials")

    settings = get_settings()
    with traced_span("rag.search_school_materials", subject=args.subject.value, max_results=args.max_results):
        try:
            if settings.vertex_search_datastore:
                passages, backend = _search_vertex_ai(args.query, args.max_results), "vertex_ai_search"
            else:
                passages = get_material_index().search(args.query, args.subject, args.max_results)
                backend = "sqlite_fts5_bm25"
        except Exception as exc:  # noqa: BLE001
            logger.exception("material_search_failed")
            return guided_error(
                "RETRIEVAL_UNAVAILABLE",
                f"School materials search is temporarily unavailable ({type(exc).__name__}).",
                "Tell the kid you couldn't open their class notes right now, then explain the concept "
                "using general grade-appropriate knowledge and suggest checking with their teacher's method.",
            )

    if not passages:
        return guided_error(
            "NO_MATCHING_MATERIALS",
            f"No class materials matched '{args.query}'.",
            "Retry once with simpler keywords (e.g. the core concept name like 'fractions' or 'orbits') "
            "or subject='any'. If still empty, call list_available_study_guides to see what topics exist.",
        )
    return SearchSchoolMaterialsOutput(passages=passages, retrieval_backend=backend).model_dump(mode="json")


def list_available_study_guides() -> dict:
    """List every study guide available in the kid's school materials library.

    Use this when the kid asks "what are we learning?" or when
    ``search_school_materials`` returns no results and you need to discover
    which topics exist.

    Returns:
        dict: ``{"status": "success", "guides": [{"title", "subject", "topics"}]}``.
    """
    try:
        return ListStudyGuidesOutput(guides=get_material_index().guides).model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        return guided_error(
            "LIBRARY_UNAVAILABLE",
            f"Could not load the study guide library ({type(exc).__name__}).",
            "Ask the kid which subject and topic their homework is about and continue tutoring from that.",
        )
