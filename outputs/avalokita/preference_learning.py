"""Local, independent preference posteriors; neither a planner nor dispatch authority.

Weighted Bernoulli logistic heads use a Gaussian prior, MAP fitting and a diagonal
Laplace approximation. This is newly implemented stdlib math, not private Habit
code or subscribed frontier-model weight training. No original text is retained.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
import math
import re
import time
import unicodedata


FEATURE_SCHEMA_VERSION = 1
FEATURE_DIMENSION = 64
CHECKPOINT_SCHEMA = "avalokita-preference-posterior/2"
MAX_SAMPLES = 5000
MAX_HEADS = 256
MAX_FEEDBACK_CONTEXTS = 256
HALF_LIFE_SECONDS = 30 * 86400.0
PRIOR_PRECISION = 1.0
_SLUG = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_OPAQUE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SAMPLE_FIELDS = {"id", "source_kind", "source_ref", "event_ref", "observed_at", "scope",
                  "provider", "purpose", "capability", "features", "target", "weight"}
_SOURCES = {"human_explicit", "observed_usage", "verified_outcome"}
_SENSITIVE = re.compile(
    r"(?:https?|wss?)://[^\s<>\"']+|"
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|"
    r"\b(?:token|password|api[_-]?key|secret|authorization|cookie)\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+|"
    r"\b(?:sk-(?:proj-|ant-)?|gh[pousr]_|github_pat_|AIza|ya29\.)[A-Z0-9_.-]{12,}|"
    r"\b[A-Z0-9_=-]{28,}\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|"
    r"\b[A-Z]:[\\/][^\s<>\"']+|/(?:Users|home)/[^\s<>\"']+",
    re.IGNORECASE,
)


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _number(value, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise ValueError("Invalid bounded numeric value")
    try:
        number = float(value)
    except OverflowError:
        raise ValueError("Invalid bounded numeric value") from None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError("Invalid bounded numeric value")
    return number


def _slug(value, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise ValueError("Invalid preference label")
    return value


def _vector(value) -> list[float]:
    if not isinstance(value, list) or len(value) != FEATURE_DIMENSION:
        raise ValueError("Invalid feature schema")
    result = [_number(item, -1.0, 1.0) for item in value]
    if result[0] != 1.0 or sum(item * item for item in result[1:]) > 1.000001:
        raise ValueError("Feature vector is not normalized")
    return result


def featureize(text: str, purpose: str | None = None) -> list[float]:
    """Redact sensitive spans, then make bounded signed lexical hash features.

    These features learn local lexical preference associations. They are not a
    semantic encoder and must not be presented as natural-language task planning.
    """
    if not isinstance(text, str) or len(text) > 32768:
        raise ValueError("Invalid feature input")
    try:
        text.encode("utf-8")
    except UnicodeError:
        raise ValueError("Invalid feature input") from None
    if purpose is not None:
        _slug(purpose)
    cleaned = _SENSITIVE.sub(" ", unicodedata.normalize("NFKC", text))
    tokens = [token.casefold() for token in re.findall(r"[^\W_]+", cleaned, re.UNICODE)
              if 1 <= len(token) <= 32 and not token.isdecimal()][:256]
    features = [0.0] * FEATURE_DIMENSION
    features[0] = 1.0

    def add(value: str, weight: float):
        encoded = hashlib.sha256(("avalokita-features-v1:" + value).encode("utf-8")).digest()
        index = 1 + int.from_bytes(encoded[:2], "big") % (FEATURE_DIMENSION - 1)
        features[index] += weight if encoded[2] & 1 else -weight

    for token in tokens:
        add("word:" + token, 1.0)
        for offset in range(min(len(token) - 2, 16)):
            add("gram:" + token[offset:offset + 3], 0.25)
    for left, right in zip(tokens, tokens[1:]):
        add("pair:" + left + " " + right, 0.5)
    if purpose is not None:
        add("purpose:" + purpose, 1.0)
    norm = math.sqrt(sum(item * item for item in features[1:]))
    if norm:
        features[1:] = [item / norm for item in features[1:]]
    return features


def _sigmoid(value: float) -> float:
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-min(value, 700)))
    exponent = math.exp(max(value, -700))
    return exponent / (1.0 + exponent)


def _dot(left, right) -> float:
    return sum(a * b for a, b in zip(left, right))


def _same_context(left, right) -> bool:
    return left == right or _dot(left[1:], right[1:]) >= 0.9


def _feedback_contexts(rows) -> list[dict]:
    contexts = {}
    for row in rows:
        if row["source_kind"] == "human_explicit":
            key = (row["purpose"], row["provider"], row["capability"], tuple(row["features"]))
            contexts[key] = {name: row[name] for name in
                             ("purpose", "provider", "capability", "features", "target", "observed_at")}
            contexts[key]["evidence_ref"] = _hash(row["id"])
    return sorted(contexts.values(), key=lambda row: (row["observed_at"], row["evidence_ref"]))[-MAX_FEEDBACK_CONTEXTS:]


def _moderate_usage(rows, contexts, now) -> int:
    cohorts = defaultdict(list)
    for row in rows:
        if row["source_kind"] != "observed_usage":
            continue
        matching = [context for context in contexts if context["purpose"] == row["purpose"]
                    and context["capability"] == row["capability"]
                    and context["observed_at"] > row["observed_at"]
                    and (context["target"] == 1 or context["provider"] == row["provider"])
                    and _same_context(context["features"], row["features"])]
        if matching:
            latest = max(matching, key=lambda context: (context["observed_at"], context["evidence_ref"]))
            # Shared context mass, independent of which candidates are presented.
            key = (latest["purpose"], latest["capability"], tuple(latest["features"]))
            cohorts[key].append(row)
    moderated = 0
    for cohort in cohorts.values():
        total = sum(min(row["weight"] * 2 ** (-max(0.0, now - row["observed_at"]) / HALF_LIFE_SECONDS), 0.2)
                    * 0.25 for row in cohort)
        scale = min(1.0, 0.5 / total) if total else 1.0
        for row in cohort:
            row["usage_scale"] = scale
            moderated += scale < 1.0
    return moderated


def _objective(weights, rows) -> float:
    loss = 0.5 * PRIOR_PRECISION * _dot(weights, weights)
    for row in rows:
        logit = _dot(weights, row["features"])
        loss += row["effective_weight"] * (
            max(logit, 0) + math.log1p(math.exp(-abs(logit))) - row["target"] * logit)
    return loss


def _fit_head(rows: list[dict], now: float) -> dict:
    explicit = [row for row in rows if row["source_kind"] == "human_explicit"]
    explicit.sort(key=lambda row: (row["observed_at"], row["id"]))
    recent: list[dict] = []
    corrections = []
    change_at = None
    for current in explicit:
        previous = next((row for row in reversed(recent)
                         if _same_context(row["features"], current["features"])), None)
        if (previous is not None and previous["target"] != current["target"]
                and previous["observed_at"] < current["observed_at"]):
            change_at = current["observed_at"]
            corrections.append(current)
        if previous is not None:
            recent.remove(previous)
        recent.append(current)
        recent = recent[-16:]
    corrections = corrections[-32:]
    for row in rows:
        recency = 2 ** (-max(0.0, now - row["observed_at"]) / HALF_LIFE_SECONDS)
        weight = row["weight"] * recency
        if row["source_kind"] == "human_explicit":
            weight *= 4.0
        elif row["source_kind"] == "observed_usage":
            weight = min(weight, 0.2) * 0.25 * row.get("usage_scale", 1.0)
        if any(row["observed_at"] < correction["observed_at"] and row["target"] != correction["target"]
               and _same_context(row["features"], correction["features"]) for correction in corrections):
            weight *= 0.05
        row["effective_weight"] = weight
    weights = [0.0] * FEATURE_DIMENSION
    iterations = 0
    for iterations in range(1, 81):
        gradient = [PRIOR_PRECISION * value for value in weights]
        precision = [PRIOR_PRECISION] * FEATURE_DIMENSION
        for row in rows:
            probability = _sigmoid(_dot(weights, row["features"]))
            residual = row["effective_weight"] * (probability - row["target"])
            curvature = row["effective_weight"] * probability * (1.0 - probability)
            for index, value in enumerate(row["features"]):
                gradient[index] += residual * value
                precision[index] += curvature * value * value
        if max(abs(item) for item in gradient) < 1e-7:
            break
        direction = [gradient[index] / precision[index] for index in range(FEATURE_DIMENSION)]
        previous_loss = _objective(weights, rows)
        slope = _dot(gradient, direction)
        step = 1.0
        for _ in range(20):
            proposed = [weights[index] - step * direction[index] for index in range(FEATURE_DIMENSION)]
            if _objective(proposed, rows) <= previous_loss - 0.0001 * step * slope:
                weights = proposed
                break
            step *= 0.5
        else:
            break
    precision = [PRIOR_PRECISION] * FEATURE_DIMENSION
    for row in rows:
        probability = _sigmoid(_dot(weights, row["features"]))
        curvature = row["effective_weight"] * probability * (1 - probability)
        for index, value in enumerate(row["features"]):
            precision[index] += curvature * value * value
    counts = dict(sorted(Counter(row["source_kind"] for row in rows).items()))
    return {"weights": weights, "variances": [1.0 / value for value in precision],
            "samples": len(rows), "effective_weight": sum(row["effective_weight"] for row in rows),
            "last_observed_at": max(row["observed_at"] for row in rows),
            "source_counts": counts, "evidence_refs": sorted(_hash(row["id"]) for row in rows),
            "change_at": change_at, "iterations": iterations}


class PreferenceModel:
    """Independent suitability heads with separate preference and outcome targets."""
    def __init__(self, *, clock=None):
        self._clock = clock or time.time
        self._heads: dict[str, dict] = {}
        self._feedback_contexts: list[dict] = []
        self.revision = 0
        self._lineage = None
        self._trained_at = None
        self._report: dict = {"accepted_samples": 0, "training_scope": "untrained"}

    @staticmethod
    def _key(kind, purpose, provider, capability):
        return json.dumps([kind, purpose, provider, capability], separators=(",", ":"))

    def fit(self, samples: list[dict]) -> dict:
        if not isinstance(samples, list) or len(samples) > 100000:
            raise ValueError("Invalid bounded sample collection")
        now = _number(self._clock(), 0, 1e12)
        accepted: dict[str, dict] = {}
        skipped: Counter[str] = Counter()
        for original in samples:
            if not isinstance(original, dict) or set(original) != _SAMPLE_FIELDS:
                raise ValueError("Invalid evidence schema; raw text and authority fields are forbidden")
            row = dict(original)
            if not isinstance(row["source_kind"], str) or row["source_kind"] not in _SOURCES:
                skipped["untrusted_source"] += 1
                continue
            if row["target"] is None:
                skipped["unlabelled"] += 1
                continue
            if type(row["target"]) not in {int, float} or row["target"] not in {0, 1}:
                raise ValueError("Invalid evidence target")
            if row["source_kind"] == "observed_usage" and row["target"] != 1:
                skipped["usage_is_not_negative_feedback"] += 1
                continue
            for name in ("id", "scope"):
                if not isinstance(row[name], str) or not _OPAQUE.fullmatch(row[name]):
                    raise ValueError("Invalid opaque evidence reference")
            for name in ("source_ref", "event_ref"):
                if not isinstance(row[name], str) or not re.fullmatch(r"[0-9a-f]{64}", row[name]):
                    raise ValueError("Invalid evidence provenance hash")
            for name in ("provider", "purpose"):
                row[name] = _slug(row[name])
            row["capability"] = _slug(row["capability"], nullable=True)
            row["features"] = _vector(row["features"])
            row["observed_at"] = _number(row["observed_at"], 0, 1e12)
            if row["observed_at"] > now + 300:
                skipped["future_evidence"] += 1
                continue
            row["weight"] = _number(row["weight"], 0, 4)
            if row["weight"] == 0:
                skipped["zero_weight"] += 1
                continue
            previous = accepted.get(row["id"])
            if previous is not None and previous != row:
                raise ValueError("Conflicting evidence identity")
            accepted[row["id"]] = row
        all_rows = sorted(accepted.values(), key=lambda row: (row["observed_at"], row["id"]))
        # A bounded recent window is declared, never represented as all history.
        rows = all_rows[-MAX_SAMPLES:]
        lineage = hashlib.sha256(_canonical({"samples": rows})).hexdigest()
        if lineage == self._lineage:
            return {**deepcopy(self._report), "skipped": dict(sorted(skipped.items())), "changed": False}
        contexts = _feedback_contexts(rows)
        moderated = _moderate_usage(rows, contexts, now)
        groups = defaultdict(list)
        for row in rows:
            kind = "quality" if row["source_kind"] == "verified_outcome" else "preference"
            groups[self._key(kind, row["purpose"], row["provider"], row["capability"])].append(row)
        if len(groups) > MAX_HEADS:
            raise ValueError("Preference head limit exceeded")
        heads = {key: _fit_head(group, now) for key, group in sorted(groups.items())}
        self._heads, self._lineage, self._trained_at = heads, lineage, now
        self._feedback_contexts = contexts
        self.revision += 1
        self._report = {"accepted_samples": len(rows), "unique_samples": len(all_rows),
                        "truncated_samples": len(all_rows) - len(rows), "skipped": dict(sorted(skipped.items())),
                        "heads": len(heads), "model_revision": self.revision, "lineage_sha256": lineage,
                        "training_scope": "bounded_local_feature_feedback",
                        "method": "independent_logistic_map_diagonal_laplace",
                        "drift_method": "explicit_feedback_change_and_age_decay",
                        "usage_policy": "current_explicit_context/1", "moderated_usage_samples": moderated,
                        "semantic_planning": False, "dispatch_authority": False}
        return {**deepcopy(self._report), "changed": True}

    def _prediction(self, kind, purpose, provider, capability, features, now):
        key = self._key(kind, purpose, provider, capability)
        head = self._heads.get(key)
        if head is None and capability is not None:
            head = self._heads.get(self._key(kind, purpose, provider, None))
        if head is None:
            return {"probability": 0.5, "lower_bound": 0.0, "upper_bound": 1.0,
                    "confidence": 0.0, "uncertainty": 1.0, "training_state": "untrained",
                    "model_trained": False, "effective_samples": 0.0, "evidence_refs": [], "evidence_count": 0,
                    "context_moderated": False}
        freshness = 2 ** (-max(0.0, now - head["last_observed_at"]) / HALF_LIFE_SECONDS)
        logit = _dot(head["weights"], features)
        variance = sum(value * value * precision for value, precision in zip(features, head["variances"]))
        variance = min(16.0, variance / max(freshness, 1e-9))
        probability = _sigmoid(logit / math.sqrt(1.0 + math.pi * variance / 8.0))
        probability = 0.5 + freshness * (probability - 0.5)
        lower = _sigmoid(logit - 1.96 * math.sqrt(variance))
        upper = _sigmoid(logit + 1.96 * math.sqrt(variance))
        lower, upper = min(lower, probability), max(upper, probability)
        strength = head["effective_weight"] * freshness
        confidence = freshness * (1 - (upper - lower)) * strength / (strength + 2.0)
        references = list(head["evidence_refs"][-16:])
        moderated = False
        if kind == "preference" and head["source_counts"].get("observed_usage"):
            matching = [context for context in self._feedback_contexts if context["purpose"] == purpose
                        and context["capability"] in {capability, None}
                        and _same_context(context["features"], features)]
            if matching and not any(context["provider"] == provider for context in matching):
                positive = [context for context in matching if context["target"] == 1]
                if positive:
                    latest = max(positive, key=lambda context: (context["observed_at"], context["evidence_ref"]))
                    age = 2 ** (-max(0.0, now - latest["observed_at"]) / HALF_LIFE_SECONDS)
                    support = 1.0 - 0.9 * age
                    # A matching current human choice withdraws trust in usage-only
                    # generalization; it never labels the other provider negative.
                    probability = 0.5 + support * (probability - 0.5)
                    lower, upper = support * lower, 1.0 - support * (1.0 - upper)
                    confidence *= support
                    strength *= support
                    own_refs = [ref for ref in references if ref != latest["evidence_ref"]]
                    references = [latest["evidence_ref"]] + own_refs[:15]
                    moderated = True
        state = ("feedback_trained" if head["source_counts"].get("human_explicit")
                 else "outcome_trained" if kind == "quality" else "inferred")
        return {"probability": round(probability, 12), "lower_bound": round(lower, 12),
                "upper_bound": round(upper, 12), "confidence": round(confidence, 12),
                "uncertainty": round(1.0 - confidence, 12), "training_state": state,
                "model_trained": True, "effective_samples": round(strength, 12),
                "evidence_refs": references, "evidence_count": head["samples"], "context_moderated": moderated}

    def predict(self, context: dict, candidates: list[dict]) -> list[dict]:
        if not isinstance(context, dict) or not isinstance(candidates, list) or len(candidates) > 128:
            raise ValueError("Invalid prediction input")
        purpose = _slug(context.get("purpose"))
        features = (_vector(context["features"]) if "features" in context
                    else featureize(context.get("text", ""), purpose))
        now = _number(context.get("now", self._clock()), 0, 1e12)
        result = []
        seen = set()
        for candidate in candidates:
            if not isinstance(candidate, dict):
                raise ValueError("Invalid candidate")
            identifier = candidate.get("id")
            if not isinstance(identifier, str) or not _OPAQUE.fullmatch(identifier) or identifier in seen:
                raise ValueError("Invalid candidate identity")
            seen.add(identifier)
            provider = _slug(candidate.get("provider"))
            capability = _slug(candidate.get("capability"), nullable=True)
            preference = self._prediction("preference", purpose, provider, capability, features, now)
            quality = self._prediction("quality", purpose, provider, capability, features, now)
            result.append({"id": identifier, "provider": provider, "purpose": purpose, "capability": capability,
                           "score": preference["probability"], "confidence": preference["confidence"],
                           "uncertainty": preference["uncertainty"], "lower_bound": preference["lower_bound"],
                           "upper_bound": preference["upper_bound"], "training_state": preference["training_state"],
                           "model_trained": preference["model_trained"], "evidence_refs": preference["evidence_refs"],
                           "model_revision": self.revision, "preference": preference, "quality": quality,
                           "dispatch_authority": False})
        return result

    def to_dict(self) -> dict:
        result = {"schema": CHECKPOINT_SCHEMA, "feature_schema": FEATURE_SCHEMA_VERSION,
                  "dimension": FEATURE_DIMENSION, "revision": self.revision,
                  "lineage_sha256": self._lineage, "trained_at": self._trained_at,
                  "prior_precision": PRIOR_PRECISION, "half_life_seconds": HALF_LIFE_SECONDS,
                  "heads": deepcopy(self._heads), "feedback_contexts": deepcopy(self._feedback_contexts),
                  "fit_report": deepcopy(self._report)}
        result["checksum"] = hashlib.sha256(_canonical(result)).hexdigest()
        return result

    @classmethod
    def from_dict(cls, checkpoint: dict) -> "PreferenceModel":
        fields = {"schema", "feature_schema", "dimension", "revision", "lineage_sha256", "trained_at",
                  "prior_precision", "half_life_seconds", "heads", "feedback_contexts", "fit_report", "checksum"}
        if not isinstance(checkpoint, dict) or set(checkpoint) != fields:
            raise ValueError("Invalid model checkpoint schema")
        body = {key: value for key, value in checkpoint.items() if key != "checksum"}
        try:
            encoded = _canonical(body)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("Invalid model checkpoint encoding") from None
        if len(encoded) > 4 * 1024 * 1024 or checkpoint["checksum"] != hashlib.sha256(encoded).hexdigest():
            raise ValueError("Model checkpoint checksum mismatch")
        if (checkpoint["schema"] != CHECKPOINT_SCHEMA or type(checkpoint["feature_schema"]) is not int
                or checkpoint["feature_schema"] != FEATURE_SCHEMA_VERSION or type(checkpoint["dimension"]) is not int
                or checkpoint["dimension"] != FEATURE_DIMENSION or type(checkpoint["revision"]) is not int
                or not 0 <= checkpoint["revision"] <= 1000000
                or _number(checkpoint["prior_precision"], PRIOR_PRECISION, PRIOR_PRECISION) != PRIOR_PRECISION
                or _number(checkpoint["half_life_seconds"], HALF_LIFE_SECONDS, HALF_LIFE_SECONDS) != HALF_LIFE_SECONDS):
            raise ValueError("Unsupported model checkpoint")
        lineage = checkpoint["lineage_sha256"]
        if lineage is not None and (not isinstance(lineage, str) or not re.fullmatch(r"[0-9a-f]{64}", lineage)):
            raise ValueError("Invalid model lineage")
        if checkpoint["trained_at"] is not None:
            _number(checkpoint["trained_at"], 0, 1e12)
        heads = checkpoint["heads"]
        if not isinstance(heads, dict) or len(heads) > MAX_HEADS or not isinstance(checkpoint["fit_report"], dict):
            raise ValueError("Invalid model checkpoint heads")
        head_fields = {"weights", "variances", "samples", "effective_weight", "last_observed_at",
                       "source_counts", "evidence_refs", "change_at", "iterations"}
        for key, head in heads.items():
            try:
                labels = json.loads(key)
            except (TypeError, ValueError):
                raise ValueError("Invalid model head identity") from None
            if (not isinstance(labels, list) or len(labels) != 4 or not isinstance(labels[0], str)
                    or labels[0] not in {"preference", "quality"}
                    or key != cls._key(*labels)):
                raise ValueError("Invalid model head identity")
            _slug(labels[1]); _slug(labels[2]); _slug(labels[3], nullable=True)
            if not isinstance(head, dict) or set(head) != head_fields:
                raise ValueError("Invalid model head schema")
            for name, low, high in (("weights", -1000, 1000), ("variances", 1e-12, 1.0)):
                if not isinstance(head[name], list) or len(head[name]) != FEATURE_DIMENSION:
                    raise ValueError("Invalid model parameter dimensions")
                for value in head[name]:
                    _number(value, low, high)
            if type(head["samples"]) is not int or not 1 <= head["samples"] <= MAX_SAMPLES:
                raise ValueError("Invalid model sample count")
            _number(head["effective_weight"], 0, MAX_SAMPLES * 16)
            last_limit = checkpoint["trained_at"] + 300 if checkpoint["trained_at"] is not None else 1e12
            _number(head["last_observed_at"], 0, last_limit)
            if head["change_at"] is not None:
                _number(head["change_at"], 0, head["last_observed_at"])
            if type(head["iterations"]) is not int or not 1 <= head["iterations"] <= 80:
                raise ValueError("Invalid model fit iterations")
            if (not isinstance(head["source_counts"], dict) or not set(head["source_counts"]) <= _SOURCES
                    or any(type(count) is not int or count < 1 for count in head["source_counts"].values())
                    or sum(head["source_counts"].values()) != head["samples"]
                    or not isinstance(head["evidence_refs"], list) or len(head["evidence_refs"]) != head["samples"]
                    or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                           for value in head["evidence_refs"])):
                raise ValueError("Invalid model evidence lineage")
            kinds = set(head["source_counts"])
            if (labels[0] == "quality" and kinds != {"verified_outcome"}
                    or labels[0] == "preference" and "verified_outcome" in kinds):
                raise ValueError("Outcome and preference targets are mixed")
        report = checkpoint["fit_report"]
        contexts = checkpoint["feedback_contexts"]
        if not isinstance(contexts, list) or len(contexts) > MAX_FEEDBACK_CONTEXTS:
            raise ValueError("Invalid model feedback contexts")
        context_fields = {"purpose", "provider", "capability", "features", "target", "observed_at", "evidence_ref"}
        seen = set()
        for context in contexts:
            if not isinstance(context, dict) or set(context) != context_fields:
                raise ValueError("Invalid feedback context schema")
            _slug(context["purpose"]); _slug(context["provider"]); _slug(context["capability"], nullable=True)
            _vector(context["features"])
            if type(context["target"]) not in {int, float} or context["target"] not in {0, 1}:
                raise ValueError("Invalid feedback context target")
            _number(context["observed_at"], 0, 1e12)
            owner = heads.get(cls._key("preference", context["purpose"], context["provider"], context["capability"]))
            identity = (context["purpose"], context["provider"], context["capability"], tuple(context["features"]))
            if (owner is None or not owner["source_counts"].get("human_explicit")
                    or context["evidence_ref"] not in owner["evidence_refs"]
                    or context["observed_at"] > owner["last_observed_at"] or identity in seen):
                raise ValueError("Invalid feedback context lineage")
            seen.add(identity)
        if checkpoint["revision"] == 0:
            if (heads or contexts or lineage is not None or checkpoint["trained_at"] is not None
                    or report != {"accepted_samples": 0, "training_scope": "untrained"}):
                raise ValueError("Invalid untrained checkpoint")
        else:
            report_fields = {"accepted_samples", "unique_samples", "truncated_samples", "skipped", "heads",
                             "model_revision", "lineage_sha256", "training_scope", "method", "drift_method",
                             "usage_policy", "moderated_usage_samples",
                             "semantic_planning", "dispatch_authority"}
            if (set(report) != report_fields or lineage is None or checkpoint["trained_at"] is None
                    or report["model_revision"] != checkpoint["revision"] or report["lineage_sha256"] != lineage
                    or report["heads"] != len(heads)
                    or report["training_scope"] != "bounded_local_feature_feedback"
                    or report["method"] != "independent_logistic_map_diagonal_laplace"
                    or report["drift_method"] != "explicit_feedback_change_and_age_decay"
                    or report["usage_policy"] != "current_explicit_context/1"
                    or report["semantic_planning"] is not False or report["dispatch_authority"] is not False):
                raise ValueError("Invalid model fit report")
            for name in ("accepted_samples", "unique_samples", "truncated_samples", "heads", "model_revision", "moderated_usage_samples"):
                if type(report[name]) is not int or report[name] < 0:
                    raise ValueError("Invalid model fit counts")
            if (report["accepted_samples"] > MAX_SAMPLES or report["unique_samples"] > 100000
                    or report["accepted_samples"] != sum(head["samples"] for head in heads.values())
                    or report["unique_samples"] - report["truncated_samples"] != report["accepted_samples"]
                    or report["moderated_usage_samples"] > sum(head["source_counts"].get("observed_usage", 0) for head in heads.values())
                    or not isinstance(report["skipped"], dict)
                    or not set(report["skipped"]) <= {"untrusted_source", "unlabelled", "usage_is_not_negative_feedback", "zero_weight", "future_evidence"}
                    or any(type(value) is not int or value < 1 for value in report["skipped"].values())):
                raise ValueError("Invalid model evidence counts")
        model = cls()
        model._heads = deepcopy(heads)
        model._feedback_contexts = deepcopy(contexts)
        model.revision = checkpoint["revision"]
        model._lineage, model._trained_at = lineage, checkpoint["trained_at"]
        model._report = deepcopy(checkpoint["fit_report"])
        return model
