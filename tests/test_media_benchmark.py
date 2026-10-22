"""Phase-3 tests for the STT benchmark gate (media/benchmark.py).

The metrics (WER, DER) and the lock-the-engine decision are pure, so they are
fully tested here with fake engines and synthetic transcripts — no GPU, no
models. This is the gate the design requires "before locking the engine"; these
tests prove the gate itself is sound, so the on-GPU run can be trusted."""

from __future__ import annotations

from hdh.modules.media.backends import Segment, Transcript
from hdh.modules.media.benchmark import (
    BenchmarkCase,
    EngineScore,
    decide_gate,
    diarization_error_rate,
    normalize_words,
    run_benchmark,
    word_error_rate,
)

# ── WER ──────────────────────────────────────────────────────────────────────


def test_word_error_rate_is_zero_for_identical_text():
    assert word_error_rate("the patient has a cough", "the patient has a cough") == 0.0


def test_word_error_rate_ignores_case_and_punctuation():
    assert word_error_rate("The patient has a cough.", "the patient, has a cough") == 0.0
    assert normalize_words("Hello, World!") == ["hello", "world"]


def test_word_error_rate_counts_one_substitution():
    # one of four reference words wrong → 0.25
    assert word_error_rate("blood pressure is high", "blood pressure is low") == 0.25


def test_word_error_rate_counts_insertions_and_deletions():
    assert word_error_rate("a b c d", "a b c") == 0.25  # one deletion
    assert word_error_rate("a b c d", "a b c d e") == 0.25  # one insertion


def test_word_error_rate_empty_reference():
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "unexpected words") == 1.0


# ── DER ────────────────────────────────────────────────────────────────────────


def _seg(speaker: str, t0: float, t1: float) -> dict:
    return {"speaker": speaker, "t0": t0, "t1": t1}


def test_diarization_error_rate_is_zero_when_identical():
    ref = [_seg("Provider", 0, 2), _seg("Patient", 2, 4)]
    assert diarization_error_rate(ref, ref) == 0.0


def test_diarization_error_rate_is_label_invariant():
    # Same turns, different arbitrary labels → the best mapping makes it perfect.
    ref = [_seg("Provider", 0, 2), _seg("Patient", 2, 4)]
    hyp = [_seg("speaker_1", 0, 2), _seg("speaker_0", 2, 4)]
    assert diarization_error_rate(ref, hyp) == 0.0


def test_diarization_error_rate_penalizes_confusion():
    # Second half credited to the wrong speaker → ~half the speech is error.
    ref = [_seg("Provider", 0, 2), _seg("Patient", 2, 4)]
    hyp = [_seg("Provider", 0, 2), _seg("Provider", 2, 4)]
    der = diarization_error_rate(ref, hyp)
    assert 0.45 < der < 0.55


def test_diarization_error_rate_penalizes_missed_speech():
    # Hypothesis only covers the first half → the rest is missed.
    ref = [_seg("Provider", 0, 2), _seg("Patient", 2, 4)]
    hyp = [_seg("Provider", 0, 2)]
    der = diarization_error_rate(ref, hyp)
    assert 0.45 < der < 0.55


def test_diarization_error_rate_empty_reference_is_zero():
    assert diarization_error_rate([], [_seg("Provider", 0, 2)]) == 0.0


# ── run_benchmark ──────────────────────────────────────────────────────────────


class _FakeEngine:
    """A Transcriber that returns a fixed transcript, ignoring the audio."""

    def __init__(self, text: str, segments: list[dict]) -> None:
        self._text = text
        self._segments = [Segment(text="", **s) for s in segments]

    def transcribe(self, data: bytes, content_type: str) -> Transcript:
        return Transcript(text=self._text, segments=self._segments, duration_s=4.0)


class _BrokenEngine:
    def transcribe(self, data: bytes, content_type: str) -> Transcript:
        raise RuntimeError("model failed to load")


def _case() -> BenchmarkCase:
    return BenchmarkCase(
        name="clip-1",
        audio=b"RIFF....",
        content_type="audio/wav",
        reference_text="blood pressure is high",
        reference_segments=[_seg("Provider", 0, 2), _seg("Patient", 2, 4)],
        duration_s=4.0,
    )


def test_run_benchmark_scores_each_engine_on_each_case():
    engines = {
        "perfect": _FakeEngine("blood pressure is high", [_seg("Provider", 0, 2), _seg("Patient", 2, 4)]),
        "wordy": _FakeEngine("blood pressure is low", [_seg("Provider", 0, 2), _seg("Patient", 2, 4)]),
    }
    scores = run_benchmark(engines, [_case()])
    by_engine = {s.engine: s for s in scores}
    assert by_engine["perfect"].wer == 0.0
    assert by_engine["perfect"].der == 0.0
    assert by_engine["wordy"].wer == 0.25  # one substituted word
    assert all(s.seconds >= 0.0 for s in scores)


def test_run_benchmark_records_a_crash_as_worst_score_without_aborting():
    engines = {
        "ok": _FakeEngine("blood pressure is high", [_seg("Provider", 0, 4)]),
        "broken": _BrokenEngine(),
    }
    scores = run_benchmark(engines, [_case()])
    broken = next(s for s in scores if s.engine == "broken")
    assert broken.wer == 1.0 and broken.der == 1.0
    assert broken.error and "model failed" in broken.error
    # the healthy engine was still scored
    assert any(s.engine == "ok" and s.wer == 0.0 for s in scores)


# ── the gate ─────────────────────────────────────────────────────────────────


def _scores(wer_nemo, der_nemo, wer_wx, der_wx) -> list[EngineScore]:
    return [
        EngineScore("nemo", "c", wer_nemo, der_nemo, 1.0, 0.1),
        EngineScore("whisperx", "c", wer_wx, der_wx, 1.0, 0.1),
    ]


def test_gate_locks_candidate_when_within_margin():
    # NeMo slightly worse on WER but within the 0.05 margin, better on DER.
    decision = decide_gate(_scores(0.12, 0.08, 0.10, 0.11), candidate="nemo", baseline="whisperx")
    assert decision.candidate_passed is True
    assert decision.locked == "nemo"
    assert "within margin" in decision.reason


def test_gate_keeps_baseline_when_candidate_trails_on_wer():
    decision = decide_gate(_scores(0.20, 0.08, 0.10, 0.10), candidate="nemo", baseline="whisperx")
    assert decision.candidate_passed is False
    assert decision.locked == "whisperx"
    assert "WER" in decision.reason and "streaming engine only" in decision.reason


def test_gate_keeps_baseline_when_candidate_trails_on_der():
    decision = decide_gate(_scores(0.10, 0.30, 0.10, 0.10), candidate="nemo", baseline="whisperx")
    assert decision.candidate_passed is False
    assert decision.locked == "whisperx"
    assert "DER" in decision.reason


def test_gate_locks_candidate_when_it_beats_the_baseline_outright():
    decision = decide_gate(_scores(0.05, 0.05, 0.15, 0.20), candidate="nemo", baseline="whisperx")
    assert decision.candidate_passed is True
    assert decision.locked == "nemo"


def test_gate_margins_are_configurable():
    scores = _scores(0.18, 0.08, 0.10, 0.10)  # 0.08 of WER slack needed
    assert (
        decide_gate(scores, candidate="nemo", baseline="whisperx", wer_margin=0.05).candidate_passed is False
    )
    assert (
        decide_gate(scores, candidate="nemo", baseline="whisperx", wer_margin=0.10).candidate_passed is True
    )
