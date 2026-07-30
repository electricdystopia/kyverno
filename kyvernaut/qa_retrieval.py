#!/usr/bin/env python3
"""Retrieve citation-backed Kyverno documentation or escalate without answering."""

import argparse
import hashlib
import html
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import yaml


WORD = re.compile(r"[a-z0-9][a-z0-9_.:/+-]*")
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
STOP_WORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "can",
    "do",
    "does",
    "for",
    "from",
    "how",
    "i",
    "in",
    "is",
    "it",
    "my",
    "of",
    "on",
    "or",
    "the",
    "this",
    "to",
    "what",
    "when",
    "where",
    "which",
    "why",
    "with",
}


def _integer(value, where: str, minimum: int, maximum: int) -> str | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not minimum <= value <= maximum
    ):
        return f"{where} must be an integer from {minimum} through {maximum}"
    return None


def validate_manifest(data: dict, repo_root: Path) -> list[str]:
    errors = []
    if not isinstance(data, dict):
        return ["document must be a mapping"]
    if data.get("version") != 1:
        errors.append("version must be 1")
    source_paths = data.get("source_paths")
    if (
        not isinstance(source_paths, list)
        or not source_paths
        or not all(isinstance(value, str) and value for value in source_paths)
    ):
        return errors + ["source_paths must be a non-empty string list"]
    if len(source_paths) != len(set(source_paths)):
        errors.append("source_paths contains duplicate entries")

    root = repo_root.resolve()
    for value in source_paths:
        posix = PurePosixPath(value)
        if posix.is_absolute() or ".." in posix.parts or "\\" in value:
            errors.append(f"source path is not repository-relative: {value!r}")
            continue
        unresolved = root / value
        candidate = unresolved.resolve()
        if not candidate.is_relative_to(root):
            errors.append(f"source path escapes repository root: {value!r}")
        elif not candidate.exists():
            errors.append(f"source path does not exist: {value!r}")
        elif unresolved.is_symlink():
            errors.append(f"source path cannot be a symlink: {value!r}")
        elif candidate.is_file() and candidate.suffix.casefold() != ".md":
            errors.append(f"source file must be Markdown: {value!r}")

    limits = data.get("limits")
    if not isinstance(limits, dict):
        return errors + ["limits must be a mapping"]
    bounds = {
        "max_source_files": (1, 1000),
        "max_source_bytes": (1024, 1048576),
        "max_section_chars": (200, 20000),
        "max_excerpt_chars": (100, 2000),
        "max_results": (1, 10),
        "max_question_bytes": (32, 8192),
    }
    for field, (minimum, maximum) in bounds.items():
        error = _integer(limits.get(field), f"limits.{field}", minimum, maximum)
        if error:
            errors.append(error)
    if (
        isinstance(limits.get("max_excerpt_chars"), int)
        and isinstance(limits.get("max_section_chars"), int)
        and limits["max_excerpt_chars"] > limits["max_section_chars"]
    ):
        errors.append("limits.max_excerpt_chars cannot exceed max_section_chars")

    confidence = data.get("confidence")
    if not isinstance(confidence, dict):
        return errors + ["confidence must be a mapping"]
    error = _integer(
        confidence.get("min_distinct_query_terms"),
        "confidence.min_distinct_query_terms",
        1,
        20,
    )
    if error:
        errors.append(error)
    coverage = confidence.get("min_weighted_coverage")
    if (
        not isinstance(coverage, (int, float))
        or isinstance(coverage, bool)
        or not 0 < coverage <= 1
    ):
        errors.append("confidence.min_weighted_coverage must be greater than 0 and at most 1")
    return errors


def load_manifest(path: Path, repo_root: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid Q&A source YAML: {error}") from error
    errors = validate_manifest(data, repo_root)
    if errors:
        raise ValueError("invalid Q&A source manifest:\n  - " + "\n  - ".join(errors))
    return data


def discover_sources(repo_root: Path, manifest: dict) -> list[Path]:
    root = repo_root.resolve()
    sources = set()
    for value in manifest["source_paths"]:
        source = (root / value).resolve()
        candidates = [source] if source.is_file() else source.rglob("*.md")
        for candidate in candidates:
            resolved = candidate.resolve()
            if (
                resolved.is_file()
                and not candidate.is_symlink()
                and resolved.is_relative_to(root)
            ):
                sources.add(resolved)
    ordered = sorted(sources, key=lambda path: path.relative_to(root).as_posix())
    maximum = manifest["limits"]["max_source_files"]
    if len(ordered) > maximum:
        raise ValueError(
            f"documentation source count {len(ordered)} exceeds configured cap {maximum}"
        )
    byte_cap = manifest["limits"]["max_source_bytes"]
    oversized = [
        path.relative_to(root).as_posix()
        for path in ordered
        if path.stat().st_size > byte_cap
    ]
    if oversized:
        raise ValueError(
            f"documentation source(s) exceed the {byte_cap}-byte cap: "
            + ", ".join(oversized)
        )
    return ordered


def tokenize(text: str) -> list[str]:
    return [
        token
        for token in WORD.findall(text.casefold())
        if token not in STOP_WORDS and len(token) > 1
    ]


def _anchor(heading: str) -> str:
    value = re.sub(r"[^\w\- ]", "", heading.casefold(), flags=re.UNICODE)
    return re.sub(r"\s+", "-", value.strip())


def _excerpt(text: str, matched_terms: set[str], maximum: int) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= maximum:
        return compact
    normalized = compact.casefold()
    positions = [normalized.find(term) for term in matched_terms]
    positions = [position for position in positions if position >= 0]
    center = min(positions) if positions else 0
    start = max(0, center - maximum // 4)
    end = min(len(compact), start + maximum)
    start = max(0, end - maximum)
    excerpt = compact[start:end].strip()
    if start:
        excerpt = "…" + excerpt
    if end < len(compact):
        excerpt += "…"
    return excerpt


def build_chunks(repo_root: Path, sources: list[Path], manifest: dict) -> list[dict]:
    root = repo_root.resolve()
    maximum = manifest["limits"]["max_section_chars"]
    chunks = []
    for path in sources:
        relative = path.relative_to(root).as_posix()
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        source_hash = hashlib.sha256(content.encode()).hexdigest()
        sections = []
        heading = path.name
        start = 1
        body = []
        for line_number, line in enumerate(lines, start=1):
            match = HEADING.match(line)
            if match and body:
                sections.append((heading, start, line_number - 1, body))
                body = []
            if match:
                heading = match.group(2).strip()
                start = line_number
            body.append(line)
        if body:
            sections.append((heading, start, len(lines), body))

        for heading, line_start, line_end, section_lines in sections:
            section = "\n".join(section_lines).strip()
            if not section:
                continue
            for offset in range(0, len(section), maximum):
                text = section[offset : offset + maximum]
                tokens = tokenize(f"{heading}\n{text}")
                if not tokens:
                    continue
                chunks.append(
                    {
                        "path": relative,
                        "heading": heading,
                        "anchor": _anchor(heading),
                        "line_start": line_start,
                        "line_end": line_end,
                        "source_sha256": source_hash,
                        "text": text,
                        "tokens": tokens,
                        "heading_tokens": tokenize(heading),
                    }
                )
    return chunks


def retrieve(question: str, repo_root: Path, manifest: dict) -> dict:
    encoded = question.encode("utf-8")
    maximum = manifest["limits"]["max_question_bytes"]
    if not question.strip():
        raise ValueError("question must not be empty")
    if len(encoded) > maximum:
        raise ValueError(f"question exceeds configured {maximum}-byte cap")

    query_terms = list(dict.fromkeys(tokenize(question)))
    sources = discover_sources(repo_root, manifest)
    chunks = build_chunks(repo_root, sources, manifest)
    document_frequency = Counter()
    for chunk in chunks:
        document_frequency.update(set(chunk["tokens"]))
    query_weights = {
        term: math.log((len(chunks) + 1) / (document_frequency.get(term, 0) + 1)) + 1
        for term in query_terms
    }
    total_weight = sum(query_weights.values()) or 1
    ranked = []
    for chunk in chunks:
        counts = Counter(chunk["tokens"])
        heading_terms = set(chunk["heading_tokens"])
        matched = {term for term in query_terms if counts[term]}
        if not matched:
            continue
        score = sum(
            query_weights[term]
            * (1 + math.log(counts[term]))
            * (3 if term in heading_terms else 1)
            for term in matched
        )
        weighted_coverage = sum(query_weights[term] for term in matched) / total_weight
        ranked.append((score, weighted_coverage, chunk, matched))
    ranked.sort(
        key=lambda item: (
            -item[0],
            -item[1],
            item[2]["path"],
            item[2]["line_start"],
        )
    )

    minimum_terms = manifest["confidence"]["min_distinct_query_terms"]
    minimum_coverage = manifest["confidence"]["min_weighted_coverage"]
    top = ranked[0] if ranked else None
    grounded = bool(
        len(query_terms) >= minimum_terms
        and top
        and len(top[3]) >= minimum_terms
        and top[1] >= minimum_coverage
    )
    citations = []
    if grounded:
        seen_sections = set()
        for score, coverage, chunk, matched in ranked:
            section_key = (chunk["path"], chunk["line_start"])
            if section_key in seen_sections:
                continue
            seen_sections.add(section_key)
            citations.append(
                {
                    "path": chunk["path"],
                    "heading": chunk["heading"],
                    "anchor": chunk["anchor"],
                    "line_start": chunk["line_start"],
                    "line_end": chunk["line_end"],
                    "source_sha256": chunk["source_sha256"],
                    "score": round(score, 6),
                    "weighted_coverage": round(coverage, 6),
                    "matched_terms": sorted(matched),
                    "excerpt": _excerpt(
                        chunk["text"],
                        matched,
                        manifest["limits"]["max_excerpt_chars"],
                    ),
                }
            )
            if len(citations) >= manifest["limits"]["max_results"]:
                break

    confidence = round(top[1], 6) if top else 0.0
    if grounded:
        status = "grounded"
        reason = "retrieval met the configured term and weighted-coverage thresholds"
    elif len(query_terms) < minimum_terms:
        status = "escalate"
        reason = "question has too few meaningful terms for reliable retrieval"
    else:
        status = "escalate"
        reason = "repository documentation did not meet the confidence threshold"
    material = {
        "question_sha256": hashlib.sha256(encoded).hexdigest(),
        "manifest_version": manifest["version"],
        "source_hashes": sorted({chunk["source_sha256"] for chunk in chunks}),
        "status": status,
        "citations": citations,
    }
    return {
        "schema_version": 1,
        "decision_id": hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:20],
        "status": status,
        "reason": reason,
        "confidence": confidence,
        "question": question,
        "question_sha256": material["question_sha256"],
        "query_terms": query_terms,
        "source_file_count": len(sources),
        "indexed_chunk_count": len(chunks),
        "citations": citations,
    }


def _citation_url(citation: dict, source_url_prefix: str | None) -> str:
    suffix = (
        f"{quote(citation['path'], safe='/')}"
        f"#L{citation['line_start']}-L{citation['line_end']}"
    )
    return f"{source_url_prefix.rstrip('/')}/{suffix}" if source_url_prefix else suffix


def render_answer(result: dict, source_url_prefix: str | None = None) -> str:
    lines = ["# Kyvernaut documentation retrieval", ""]
    if result["status"] != "grounded":
        lines.extend(
            [
                "No answer was generated because the repository documentation did "
                "not provide sufficiently strong support.",
                "",
                "Escalate this question to a maintainer.",
            ]
        )
    else:
        lines.extend(
            [
                "The following trusted repository passages are the grounded answer "
                "context:",
                "",
            ]
        )
        for index, citation in enumerate(result["citations"], start=1):
            url = _citation_url(citation, source_url_prefix)
            lines.extend(
                [
                    f"## {index}. {html.escape(citation['heading'])}",
                    "",
                    f"[Source: `{citation['path']}`]({url})",
                    "",
                ]
            )
            excerpt = citation["excerpt"].replace("\n", " ")
            lines.append("> " + html.escape(excerpt))
            lines.append("")
        lines.append(
            "This retrieval output quotes trusted context only; it does not synthesize "
            "claims beyond the cited documentation."
        )
    lines.extend(
        [
            "",
            f"Decision ID: `{result['decision_id']}`",
            f"Confidence: `{result['confidence']:.3f}`",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    root = Path(__file__).parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=root)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).parent / "qa-sources.yaml",
    )
    parser.add_argument("--validate", action="store_true")
    question_group = parser.add_mutually_exclusive_group()
    question_group.add_argument("--question")
    question_group.add_argument("--question-env")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--source-url-prefix")
    args = parser.parse_args()
    try:
        manifest = load_manifest(args.manifest, args.repo)
        sources = discover_sources(args.repo, manifest)
        if args.validate:
            build_chunks(args.repo, sources, manifest)
            print(f"Q&A source manifest is valid ({len(sources)} Markdown files).")
            return 0
        question = args.question
        if args.question_env:
            question = os.environ.get(args.question_env)
            if question is None:
                raise ValueError(f"question environment variable is unset: {args.question_env}")
        if question is None or args.output_dir is None:
            raise ValueError(
                "--output-dir and either --question or --question-env are required"
            )
        result = retrieve(question, args.repo, manifest)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "qa-result.json").write_text(
            json.dumps(result, indent=2) + "\n",
            encoding="utf-8",
        )
        (args.output_dir / "answer.md").write_text(
            render_answer(result, args.source_url_prefix),
            encoding="utf-8",
        )
        print(render_answer(result, args.source_url_prefix), end="")
    except (OSError, UnicodeError, ValueError) as error:
        print(f"documentation retrieval failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
