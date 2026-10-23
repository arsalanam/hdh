"""The STT benchmark gate — measure an engine before trusting it (design §10, §11 phase 3).

The design does not take NeMo's diarization on faith: an earlier attempt (~2026-04)
found it weak, though on noisy body-cam audio and in batch — the exact failure mode
clinic audio drops. So NeMo must be **re-validated on clean clinic audio before it is
locked in**, measured against the proven WhisperX + pyannote baseline on two numbers:

- **WER** (word error rate) — did the words come out right.
- **DER** (diarization error rate) — was the right speaker credited at each moment.

This module is the gate, not the models: the metrics and the lock-the-engine decision
are PURE and fully tested here with no GPU, while the engines themselves (media/local.py)
are injected. ``hdh media-benchmark`` wires the real NeMo and WhisperX engines in and
runs this on the user's GPU box; CI exercises the same code paths with fake engines.

The rule the gate encodes: **lock NeMo only if it is within a margin of the baseline on
both WER and DER** — otherwise keep WhisperX for async and reserve NeMo for streaming
(where it is the only option), exactly as the design's fallback plan states.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import permutations
from typing import Any

from hdh.modules.media.backends import Transcriber

# A candidate engine must be no worse than the baseline by more than this on each
# metric to be locked in. 5 points of WER / DER is the default tolerance; the CLI
# can tighten or loosen it. Being *better* than the baseline always passes.
DEFAULT_WER_MARGIN = 0.05
DEFAULT_DER_MARGIN = 0.05


# ── word error rate ──────────────────────────────────────────────────────────


def normalize_words(text: str) -> list[str]:
    """Lowercase, drop punctuation, collapse whitespace — compare words, not
    capitalization or commas, which no transcriber should be penalized for."""
    cleaned = re.sub(r"[^\w\s']", " ", (text or "").lower())
    return cleaned.split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    """WER = Levenshtein word-edit distance ÷ reference length. 0.0 is perfect;
    an empty reference scores 0.0 for an empty hypothesis and 1.0 otherwise
    (there is nothing to be right about, so any words are all error)."""
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_distance(ref, hyp) / len(ref)


def _edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Classic Levenshtein (substitution/insertion/deletion each cost 1), two
    rolling rows so it is O(len b) in memory."""
    previous = list(range(len(b) + 1))
    for i, token_a in enumerate(a, start=1):
        current = [i]
        for j, token_b in enumerate(b, start=1):
            cost = 0 if token_a == token_b else 1
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost))
        previous = current
    return previous[-1]


# ── diarization error rate ───────────────────────────────────────────────────


def _speaker_at(segments: Sequence[Mapping[str, Any]], t: float) -> str | None:
    """Which speaker is talking at time ``t`` (the first segment covering it),
    or None for silence. Half-open [t0, t1) so adjacent turns do not overlap."""
    for seg in segments:
        if float(seg.get("t0", 0.0)) <= t < float(seg.get("t1", 0.0)):
            label = seg.get("speaker")
            return str(label) if label is not None else None
    return None


def _span(segments: Sequence[Mapping[str, Any]]) -> float:
    return max((float(s.get("t1", 0.0)) for s in segments), default=0.0)


def diarization_error_rate(
    reference: Sequence[Mapping[str, Any]],
    hypothesis: Sequence[Mapping[str, Any]],
    *,
    resolution: float = 0.01,
) -> float:
    """Frame-based DER over the reference's speech: sample every ``resolution``
    seconds and count frames where the hypothesis names the wrong speaker
    (missed speech or confused speaker), divided by the frames the reference
    marks as speech. Speaker labels are arbitrary strings, so the hypothesis
    labels are first mapped onto the reference's under the permutation that
    agrees most — a transcriber is judged on *who-with-whom*, not on whether it
    happened to call the provider "speaker_0". 0.0 is perfect."""
    duration = max(_span(reference), _span(hypothesis))
    if duration <= 0:
        return 0.0
    frames = [round(i * resolution, 6) for i in range(int(duration / resolution) + 1)]
    ref_at = [_speaker_at(reference, t) for t in frames]
    speech = [i for i, s in enumerate(ref_at) if s is not None]
    if not speech:
        return 0.0
    hyp_at = [_speaker_at(hypothesis, t) for t in frames]
    mapping = _best_label_mapping(ref_at, hyp_at)
    errors = sum(1 for i in speech if mapping.get(hyp_at[i]) != ref_at[i])
    return errors / len(speech)


def _best_label_mapping(
    ref_at: Sequence[str | None], hyp_at: Sequence[str | None]
) -> dict[str | None, str | None]:
    """The hypothesis→reference speaker relabeling that maximizes agreement.
    Diarization rarely has more than three speakers, so trying every assignment
    of hypothesis labels to reference labels is cheap and exact (no Hungarian)."""
    ref_labels: list[str | None] = [s for s in dict.fromkeys(ref_at) if s is not None]
    hyp_labels: list[str | None] = [s for s in dict.fromkeys(hyp_at) if s is not None]
    if not hyp_labels:
        return {}
    best: dict[str | None, str | None] = {}
    best_hits = -1
    # Pad the target labels so every hypothesis label gets a (possibly unused)
    # mapping even when the hypothesis invented more speakers than really spoke.
    targets: list[str | None] = [*ref_labels, *([None] * max(0, len(hyp_labels) - len(ref_labels)))]
    for perm in permutations(targets, len(hyp_labels)):
        candidate: dict[str | None, str | None] = dict(zip(hyp_labels, perm, strict=True))
        hits = sum(1 for r, h in zip(ref_at, hyp_at, strict=True) if r is not None and candidate.get(h) == r)
        if hits > best_hits:
            best_hits, best = hits, candidate
    return best


# ── the benchmark run ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BenchmarkCase:
    """One labeled recording: the audio bytes plus ground truth — what was said
    (``reference_text``) and who spoke when (``reference_segments``)."""

    name: str
    audio: bytes
    content_type: str
    reference_text: str
    reference_segments: list[dict[str, Any]] = field(default_factory=list)
    duration_s: float = 0.0


@dataclass(frozen=True)
class EngineScore:
    """How one engine did on one case."""

    engine: str
    case: str
    wer: float
    der: float
    seconds: float  # wall-clock to transcribe
    rtf: float  # real-time factor: seconds ÷ audio duration (lower is faster)
    error: str | None = None  # the engine failed on this case; wer/der are 1.0


@dataclass(frozen=True)
class GateDecision:
    """The verdict: which engine to lock in, and why."""

    locked: str
    candidate_passed: bool
    reason: str
    candidate: str
    baseline: str
    candidate_wer: float
    baseline_wer: float
    candidate_der: float
    baseline_der: float


def run_benchmark(engines: Mapping[str, Transcriber], cases: Iterable[BenchmarkCase]) -> list[EngineScore]:
    """Transcribe every case with every engine, scoring WER and DER and timing
    each run. An engine that raises on a case scores 1.0/1.0 with the error
    recorded, so one bad engine never aborts the comparison."""
    scores: list[EngineScore] = []
    for case in cases:
        for name, engine in engines.items():
            started = time.perf_counter()
            try:
                transcript = engine.transcribe(case.audio, case.content_type)
            except Exception as err:  # noqa: BLE001 — any crash is a 1.0 score, not an abort of the run
                elapsed = time.perf_counter() - started
                scores.append(EngineScore(name, case.name, 1.0, 1.0, elapsed, 0.0, error=str(err)))
                continue
            elapsed = time.perf_counter() - started
            segs = [s.model_dump() if hasattr(s, "model_dump") else dict(s) for s in transcript.segments]
            duration = case.duration_s or transcript.duration_s or _span(case.reference_segments)
            scores.append(
                EngineScore(
                    engine=name,
                    case=case.name,
                    wer=word_error_rate(case.reference_text, transcript.text),
                    der=diarization_error_rate(case.reference_segments, segs),
                    seconds=elapsed,
                    rtf=(elapsed / duration) if duration > 0 else 0.0,
                )
            )
    return scores


def mean_wer(scores: Iterable[EngineScore], engine: str) -> float:
    vals = [s.wer for s in scores if s.engine == engine]
    return sum(vals) / len(vals) if vals else 1.0


def mean_der(scores: Iterable[EngineScore], engine: str) -> float:
    vals = [s.der for s in scores if s.engine == engine]
    return sum(vals) / len(vals) if vals else 1.0


def decide_gate(
    scores: Iterable[EngineScore],
    *,
    candidate: str,
    baseline: str,
    wer_margin: float = DEFAULT_WER_MARGIN,
    der_margin: float = DEFAULT_DER_MARGIN,
) -> GateDecision:
    """Lock the candidate (NeMo) in only if its mean WER **and** mean DER are
    each within ``*_margin`` of the baseline (WhisperX). Otherwise keep the
    baseline for async — NeMo still owns streaming, where it is the only option,
    but it does not become the batch engine on a worse score. This is the
    design's "benchmark before locking the engine", made executable."""
    scores = list(scores)
    cand_wer, base_wer = mean_wer(scores, candidate), mean_wer(scores, baseline)
    cand_der, base_der = mean_der(scores, candidate), mean_der(scores, baseline)
    wer_ok = cand_wer <= base_wer + wer_margin
    der_ok = cand_der <= base_der + der_margin
    passed = wer_ok and der_ok
    if passed:
        reason = (
            f"{candidate} is within margin of {baseline} on both metrics "
            f"(WER {cand_wer:.3f} vs {base_wer:.3f} ≤ +{wer_margin}; "
            f"DER {cand_der:.3f} vs {base_der:.3f} ≤ +{der_margin}) — locked for async."
        )
    else:
        failed = ", ".join(
            part
            for part, ok in (
                (f"WER {cand_wer:.3f} > {base_wer:.3f}+{wer_margin}", wer_ok),
                (f"DER {cand_der:.3f} > {base_der:.3f}+{der_margin}", der_ok),
            )
            if not ok
        )
        reason = (
            f"{candidate} did not clear the gate ({failed}) — keeping {baseline} for async; "
            f"{candidate} remains the streaming engine only."
        )
    return GateDecision(
        locked=candidate if passed else baseline,
        candidate_passed=passed,
        reason=reason,
        candidate=candidate,
        baseline=baseline,
        candidate_wer=cand_wer,
        baseline_wer=base_wer,
        candidate_der=cand_der,
        baseline_der=base_der,
    )
