"""Evaluate relevance and sentiment models on a reviewed sample of real PSX headlines.

The evaluation file (config/news_eval_set.csv) has columns:
  article_id, symbol, title, relevant (1/0), sentiment (positive|neutral|negative|mixed|na), reviewer
Rows with relevant=0 are used for the relevance check only. Labels describe the headline's tone for
the company/market, not a predicted price move. The reviewer column records who labelled the row;
labels produced by an AI reviewer are marked as such and must be spot-checked by a human.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import Any

from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

from app.services.news.relevance import RELEVANCE_THRESHOLD, load_news_config, relevance
from app.services.news.sentiment import get_model

SENT_LABELS = ["negative", "neutral", "positive"]


def load_eval_set(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def evaluate_models(path: Path, artifacts_dir: Path, models: tuple[str, ...] = ("lexicon_v1", "phrasebank_lr_v1",
                                                                                 "finbert"),
                    news_config: Path = Path("config/news.yaml")) -> dict[str, Any]:
    rows = load_eval_set(path)
    cfg = load_news_config(news_config)
    publishers = _publishers([r["article_id"] for r in rows])
    out: dict[str, Any] = {"n_rows": len(rows), "reviewers": dict(Counter(r["reviewer"] for r in rows))}

    # Relevance classifier vs labels.
    y_true, y_pred = [], []
    for r in rows:
        entity = cfg.entities.get(r["symbol"])
        if entity is None:
            continue
        y_true.append(int(r["relevant"]))
        y_pred.append(int(relevance(r["title"], entity, publishers.get(r["article_id"]), cfg.pk_context)[0]
                          >= RELEVANCE_THRESHOLD))
    out["relevance"] = {"n": len(y_true), "accuracy": round(accuracy_score(y_true, y_pred), 4),
                        "precision_relevant": _precision(y_true, y_pred), "recall_relevant": _recall(y_true, y_pred)}

    # Sentiment on relevant rows with a 3-class label (mixed is rare; folded into neutral for scoring).
    sent_rows = [r for r in rows if r["relevant"] == "1" and r["sentiment"] in ("positive", "neutral", "negative",
                                                                                "mixed")]
    truth = [("neutral" if r["sentiment"] == "mixed" else r["sentiment"]) for r in sent_rows]
    out["sentiment_n"] = len(sent_rows)
    out["label_distribution"] = dict(Counter(truth))
    out["majority_class_accuracy"] = round(max(Counter(truth).values()) / len(truth), 4) if truth else None
    out["models"] = {}
    for name in models:
        try:
            model = get_model(name, artifacts_dir)
        except Exception as exc:  # noqa: BLE001 - an unavailable backend is reported, not fatal
            out["models"][name] = {"available": False, "reason": str(exc)}
            continue
        preds = [("neutral" if p.label == "mixed" else p.label) for p in model.score([r["title"] for r in sent_rows])]
        out["models"][name] = {
            "available": True, "accuracy": round(accuracy_score(truth, preds), 4),
            "macro_f1": round(f1_score(truth, preds, labels=SENT_LABELS, average="macro", zero_division=0), 4),
            "confusion_matrix": {"labels": SENT_LABELS,
                                 "matrix": confusion_matrix(truth, preds, labels=SENT_LABELS).tolist()},
            "errors": [{"title": r["title"], "label": t, "predicted": p}
                       for r, t, p in zip(sent_rows, truth, preds, strict=True) if t != p][:15],
        }
    best = max((m for m in out["models"].items() if m[1].get("available")), key=lambda kv: kv[1]["macro_f1"],
               default=None)
    out["best_model"] = best[0] if best else None
    return out


def _precision(y: list[int], p: list[int]) -> float | None:
    tp = sum(1 for a, b in zip(y, p, strict=True) if a == b == 1)
    pp = sum(p)
    return round(tp / pp, 4) if pp else None


def _recall(y: list[int], p: list[int]) -> float | None:
    tp = sum(1 for a, b in zip(y, p, strict=True) if a == b == 1)
    pos = sum(y)
    return round(tp / pos, 4) if pos else None


def _publishers(ids: list[str]) -> dict[str, str | None]:
    from sqlalchemy import select

    from app.extensions import db
    from app.models.research import NewsArticle

    rows = db.session.execute(select(NewsArticle.id, NewsArticle.publisher_domain)
                              .where(NewsArticle.id.in_([int(i) for i in ids]))).all()
    return {str(i): d for i, d in rows}
