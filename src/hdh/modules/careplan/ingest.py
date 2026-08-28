"""Reading a corpus off disk.

A corpus is a directory: a ``corpus.json`` manifest plus Markdown
documents. The manifest carries the licence once rather than every file
repeating it, and front matter on a document carries whatever a retrieval
filter will want to select on.

Corpora ship **in the repo** because they are small curated text that we
wrote — not the copyrighted originals they derive from. Anything that
cannot be committed here does not belong in a corpus; it belongs behind a
loader, the way the licensed vocabularies already are.
"""

from __future__ import annotations

import json
import pathlib

from hdh.modules.careplan.knowledge import KnowledgeDoc

#: Corpora that ship with the module.
BUNDLED = pathlib.Path(__file__).resolve().parent / "knowledge"


class CorpusError(RuntimeError):
    """A corpus on disk is not readable as one."""


def corpus_root(name: str, root: pathlib.Path | None = None) -> pathlib.Path:
    """Where a named corpus lives (bundled unless ``root`` overrides)."""
    return (root or BUNDLED) / name


def available(root: pathlib.Path | None = None) -> list[str]:
    """Corpus names on disk — a directory with a manifest counts."""
    base = root or BUNDLED
    if not base.is_dir():
        return []
    return sorted(d.name for d in base.iterdir() if d.is_dir() and (d / "corpus.json").is_file())


def _front_matter(text: str) -> tuple[dict, str]:
    """Split an optional ``---`` JSON front-matter block from the body.

    JSON rather than YAML: the project already parses JSON everywhere and
    a second document format for six small files is not worth the
    dependency.
    """
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw = text[4:end]
    body = text[end + 5 :]
    try:
        meta = json.loads(raw)
    except json.JSONDecodeError as err:
        raise CorpusError(f"front matter is not valid JSON: {err}") from None
    if not isinstance(meta, dict):
        raise CorpusError("front matter must be a JSON object")
    return meta, body


def read_corpus(name: str, root: pathlib.Path | None = None) -> tuple[dict, list[KnowledgeDoc]]:
    """The manifest and every document in a corpus.

    Raises:
        CorpusError: the directory, the manifest, or a document is missing
            something a citation needs. A chunk that cannot say where it
            came from is not ingestable — the whole point of retrieval over
            fine-tuning is that a plan element can cite its evidence.
    """
    directory = corpus_root(name, root)
    if not directory.is_dir():
        raise CorpusError(f"no corpus directory: {directory}")
    manifest_path = directory / "corpus.json"
    if not manifest_path.is_file():
        raise CorpusError(f"no corpus.json in {directory}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for required in ("name", "version", "license"):
        if not manifest.get(required):
            raise CorpusError(f"{manifest_path.name} is missing '{required}'")
    if manifest["name"] != name:
        raise CorpusError(f"manifest name {manifest['name']!r} does not match directory {name!r}")

    documents: list[KnowledgeDoc] = []
    for path in sorted(directory.glob("*.md")):
        meta, body = _front_matter(path.read_text(encoding="utf-8"))
        if not body.strip():
            raise CorpusError(f"{path.name} has no content")
        source = meta.pop("source", None) or manifest.get("source")
        if not source:
            raise CorpusError(
                f"{path.name} has no source, and {manifest_path.name} sets no default — "
                "a chunk that cannot be cited cannot be ingested"
            )
        documents.append(
            KnowledgeDoc(
                doc_id=path.stem,
                text=body.strip(),
                source=str(source),
                license=str(meta.pop("license", None) or manifest["license"]),
                metadata=meta,
            )
        )
    if not documents:
        raise CorpusError(f"{directory} has no .md documents")
    return manifest, documents


def ingest_corpus(session, name: str, root: pathlib.Path | None = None) -> int:
    """Read a corpus and replace it in the store; returns chunks written.

    Goes through the retriever factory rather than naming a store, because
    ingestion and retrieval have to agree: a vector retriever needs
    embeddings written at ingest time, and a corpus loaded by one store and
    searched by another would return nothing while looking fine.
    """
    from hdh.modules.careplan.retriever import build_store

    _manifest, documents = read_corpus(name, root)
    return build_store(session).ingest(name, documents)
