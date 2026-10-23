"""Headline sentiment models behind one interface.

Backends (selected with NEWS_SENTIMENT_MODEL):
  lexicon_v1       transparent financial-headline lexicon with negation handling (no dependencies)
  phrasebank_lr_v1 TF-IDF (word 1-2 grams) + logistic regression trained on Financial PhraseBank
                   (75%-agreement subset; CC BY-NC-SA 3.0, non-commercial research use)
  finbert          ProsusAI/finbert via transformers (optional; needs requirements-ml.txt and ~2 GB disk)

All return SentimentResult with a label in {positive, neutral, negative, mixed}, a score in
[-1, 1] (P(pos) - P(neg) for probabilistic models), and a confidence (max class probability, or
evidence strength for the lexicon). Confidence describes the model's certainty about the
headline's tone; it is NOT a probability that the share price will move.

English only. Urdu / Roman Urdu headlines are detected heuristically and returned as
insufficient-evidence rather than scored by an English model.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import joblib
import numpy as np

URDU_RE = re.compile(r"[؀-ۿ]")


@dataclass(frozen=True)
class SentimentResult:
    label: str
    score: float
    confidence: float
    probs: dict[str, float] = field(default_factory=dict)
    explanation: str = ""


class SentimentModel(Protocol):
    model_id: str

    def score(self, texts: list[str]) -> list[SentimentResult]: ...


def unsupported_language(text: str) -> bool:
    return bool(URDU_RE.search(text))


# --------------------------------------------------------------------------------------------------
# Lexicon


POSITIVE = {
    "profit", "profits", "surge", "surges", "surged", "soar", "soars", "soared", "jump", "jumps", "jumped", "rise",
    "rises", "rose", "gain", "gains", "gained", "growth", "grows", "grew", "record", "high", "beat", "beats",
    "upgrade", "upgraded", "boost", "boosts", "boosted", "strong", "robust", "rally", "rallies", "rallied",
    "expand", "expands", "expansion", "win", "wins", "won", "award", "awarded", "approve", "approves", "approved",
    "improve", "improves", "improved", "recovery", "recovers", "rebound", "rebounds", "dividend", "bonus",
    "outperform", "outperforms", "up", "higher", "increase", "increases", "increased", "milestone", "discovery",
    "bullish", "positive", "cut rates", "easing",
}
NEGATIVE = {
    "loss", "losses", "fall", "falls", "fell", "drop", "drops", "dropped", "decline", "declines", "declined",
    "plunge", "plunges", "plunged", "slump", "slumps", "slumped", "crash", "crashes", "weak", "weaker", "down",
    "lower", "downgrade", "downgraded", "cut", "cuts", "penalty", "fine", "fined", "probe", "lawsuit", "suspend",
    "suspends", "suspended", "suspension", "default", "ban", "banned", "fraud", "concern", "concerns", "risk",
    "risks", "pressure", "slowdown", "shortage", "halt", "halts", "halted", "decrease", "decreases", "decreased",
    "bearish", "negative", "selloff", "sell-off", "dip", "dips", "tumble", "tumbles", "tumbled", "worst", "hike",
}
NEGATORS = {"not", "no", "never", "without", "fails", "failed", "despite"}
_TOKEN = re.compile(r"[a-z][a-z\-]*")


class LexiconSentiment:
    model_id = "lexicon_v1"

    def score(self, texts: list[str]) -> list[SentimentResult]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> SentimentResult:
        tokens = _TOKEN.findall(text.lower())
        pos, neg, hits = 0, 0, []
        for i, tok in enumerate(tokens):
            polarity = 1 if tok in POSITIVE else -1 if tok in NEGATIVE else 0
            if polarity == 0:
                continue
            if any(t in NEGATORS for t in tokens[max(0, i - 3):i]):
                polarity = -polarity
            hits.append(f"{tok}{'+' if polarity > 0 else '-'}")
            pos, neg = pos + (polarity > 0), neg + (polarity < 0)
        total = pos + neg
        if total == 0:
            return SentimentResult("neutral", 0.0, 0.0, {}, "no sentiment terms")
        score = (pos - neg) / total
        label = "mixed" if pos and neg and abs(score) < 0.34 else "positive" if score > 0 else "negative" \
            if score < 0 else "neutral"
        confidence = min(1.0, total / 3) * abs(score) if label in ("positive", "negative") else 0.0
        return SentimentResult(label, round(score, 4), round(confidence, 4), {}, "terms: " + ", ".join(hits))


# --------------------------------------------------------------------------------------------------
# PhraseBank-trained logistic regression


LABELS = ("negative", "neutral", "positive")


def load_phrasebank(path: Path) -> tuple[list[str], list[str]]:
    texts, labels = [], []
    for line in path.read_text(encoding="latin-1").splitlines():
        if "@" not in line:
            continue
        text, label = line.rsplit("@", 1)
        if label.strip() in LABELS:
            texts.append(text.strip())
            labels.append(label.strip())
    return texts, labels


def train_phrasebank_lr(dataset: Path, out_path: Path, *, seed: int = 0) -> dict:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import classification_report
    from sklearn.model_selection import train_test_split
    from sklearn.pipeline import make_pipeline

    texts, labels = load_phrasebank(dataset)
    x_tr, x_te, y_tr, y_te = train_test_split(texts, labels, test_size=0.2, random_state=seed, stratify=labels)
    pipe = make_pipeline(TfidfVectorizer(ngram_range=(1, 2), min_df=2, sublinear_tf=True, lowercase=True),
                         LogisticRegression(C=4.0, max_iter=2000, class_weight="balanced", random_state=seed))
    pipe.fit(x_tr, y_tr)
    report = classification_report(y_te, pipe.predict(x_te), output_dict=True, zero_division=0)
    pipe.fit(texts, labels)  # final model on all data; the held-out report above is the evidence
    out_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(pipe, out_path)
    sha = hashlib.sha256(out_path.read_bytes()).hexdigest()
    return {"n": len(texts), "holdout_macro_f1": round(report["macro avg"]["f1-score"], 4),
            "holdout_accuracy": round(report["accuracy"], 4), "artifact": str(out_path), "sha256": sha}


class PhraseBankLR:
    model_id = "phrasebank_lr_v1"

    def __init__(self, artifact: Path) -> None:
        if not artifact.exists():
            raise FileNotFoundError(f"{artifact} missing: run `flask --app wsgi news train-sentiment`")
        self._pipe = joblib.load(artifact)
        self._classes = list(self._pipe.classes_)

    def score(self, texts: list[str]) -> list[SentimentResult]:
        if not texts:
            return []
        probs = self._pipe.predict_proba(texts)
        return [_from_probs(dict(zip(self._classes, map(float, p), strict=True))) for p in probs]


class FinBert:
    model_id = "finbert_prosusai"

    def __init__(self) -> None:
        try:
            from transformers import pipeline  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError("FinBERT needs `pip install -r requirements-ml.txt` (torch, transformers)") from exc
        self._pipe = pipeline("text-classification", model="ProsusAI/finbert", top_k=None, truncation=True)

    def score(self, texts: list[str]) -> list[SentimentResult]:
        out = []
        for scores in self._pipe(texts, batch_size=16):
            out.append(_from_probs({s["label"].lower(): float(s["score"]) for s in scores}))
        return out


def _from_probs(probs: dict[str, float]) -> SentimentResult:
    p_pos, p_neg, p_neu = probs.get("positive", 0.0), probs.get("negative", 0.0), probs.get("neutral", 0.0)
    score = p_pos - p_neg
    top = max(probs, key=probs.get)
    label = top
    if top != "neutral" and min(p_pos, p_neg) > 0.3:
        label = "mixed"
    return SentimentResult(label, round(score, 5), round(float(np.max(list(probs.values()))), 5),
                           {k: round(v, 5) for k, v in probs.items()},
                           f"P(pos)={p_pos:.2f} P(neu)={p_neu:.2f} P(neg)={p_neg:.2f}")


# Config name -> stored model_id (stored on each scored mention; aggregation filters by it).
MODEL_IDS = {"lexicon_v1": "lexicon_v1", "phrasebank_lr_v1": "phrasebank_lr_v1", "finbert": "finbert_prosusai"}


def get_model(name: str, artifacts_dir: Path) -> SentimentModel:
    if name == "lexicon_v1":
        return LexiconSentiment()
    if name == "phrasebank_lr_v1":
        return PhraseBankLR(artifacts_dir / "sentiment" / "phrasebank_lr_v1.joblib")
    if name == "finbert":
        return FinBert()
    raise ValueError(f"unknown sentiment model {name!r}")
