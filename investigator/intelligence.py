"""Measured local ML: lexical TF-IDF retrieval and descriptive Isolation Forest."""

import math

from investigator.analysis import RULES


def runbooks():
    return [{"id": r["key"], "title": r["title"], "description": r["explanation"], "steps": r["checks"], "scope": "Human-reviewed investigation checklist, not an automatic remediation"} for r in RULES]


def retrieve(query, cases, exclude=None):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
        raise ValueError("Search query must contain 1-2,000 characters.")
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
    except ImportError:
        return {"available": False, "reason": "Install requirements.txt to enable local TF-IDF search.", "matches": []}
    eligible = [c for c in cases if c["id"] != exclude]
    if not eligible:
        return {"available": True, "matches": [], "method": "TF-IDF + cosine similarity (lexical, not semantic embeddings)"}
    docs = [" ".join([c["title"], *c["report"]["services"], *[g["sample"] for g in c["report"]["groups"]]])[:20000] for c in eligible]
    vectorizer = TfidfVectorizer(ngram_range=(1, 2), stop_words="english", max_features=12000)
    try:
        matrix = vectorizer.fit_transform(docs)
        scores = cosine_similarity(vectorizer.transform([query]), matrix)[0]
    except ValueError:
        scores = [0.0] * len(eligible)
    ranked = sorted(zip(eligible, scores), key=lambda item: (-float(item[1]), item[0]["id"]))
    return {"available": True, "method": "TF-IDF + cosine similarity (lexical, not semantic embeddings)", "matches": [{"id": c["id"], "title": c["title"], "status": c["status"], "score": round(float(score), 4), "services": list(c["report"]["services"]), "evidence": [g["sample"] for g in c["report"]["groups"]][:2]} for c, score in ranked[:10] if score > 0], "limitation": "Similarity is text overlap, not a root-cause confidence score."}


def anomaly_analysis(report):
    buckets = report["timeline"]
    if len(buckets) < 12:
        return {"available": False, "reason": "At least 12 observed minute buckets are required; this investigation has " + str(len(buckets)) + ".", "windows": []}
    try:
        from sklearn.ensemble import IsolationForest
    except ImportError:
        return {"available": False, "reason": "Install requirements.txt to enable Isolation Forest.", "windows": []}
    features = [[math.log1p(b["events"]), math.log1p(b["errors"]), b["errors"] / max(1, b["events"]), math.log1p(b["warnings"])] for b in buckets]
    if len({tuple(row) for row in features}) < 2:
        return {"available": True, "method": "Isolation Forest", "windows": [], "flagged": 0, "reason": "All observed feature vectors are identical; no relative anomalies are reported."}
    model = IsolationForest(n_estimators=120, random_state=42, contamination="auto", n_jobs=1)
    model.fit(features)
    scores = model.decision_function(features)
    results = [b | {"decision_score": round(float(score), 5), "flagged": bool(score < 0)} for b, score in zip(buckets, scores)]
    results.sort(key=lambda row: row["decision_score"])
    return {"available": True, "method": "Isolation Forest / 120 trees / random_state=42", "features": ["log events", "log errors", "error-event fraction", "log warnings"], "windows": results, "flagged": sum(r["flagged"] for r in results), "limitation": "Descriptive within-investigation fit, not a future predictor. Flags are relative outliers, not verified incidents. Missing minutes are not filled with zeros."}


def compare(left, right):
    def entries(case):
        return {(g["service"], g["signature"]): g for g in case["report"]["groups"]}
    a, b = entries(left), entries(right)
    return {"left": {"id": left["id"], "title": left["title"], "summary": left["summary"]}, "right": {"id": right["id"], "title": right["title"], "summary": right["summary"]}, "changes": [{"service": key[0], "signature": key[1], "sample": (b.get(key) or a[key])["sample"], "left": a.get(key, {}).get("count", 0), "right": b.get(key, {}).get("count", 0), "kind": "new" if key not in a else "absent" if key not in b else "shared"} for key in sorted(a.keys() | b.keys())], "limitation": "Descriptive counts only. Different durations and logging volumes are not normalized; this is not a causal before/after experiment."}
