"""Synthetic local learning/uncertainty tests, not provider or real-user quality proof."""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from preference_learning import (FEATURE_DIMENSION, FEATURE_SCHEMA_VERSION, HALF_LIFE_SECONDS,
                                 MAX_FEEDBACK_CONTEXTS, PreferenceModel, featureize)


NOW = 1800000000.0


def sample(index, *, provider="codex", purpose="frontend", target=1, source_kind="human_explicit",
           text="responsive frontend components layout", at=NOW - 100, capability=None, weight=1.0):
    return {"id": f"evidence_{index}", "source_kind": source_kind,
            "source_ref": hashlib.sha256(("source_" + str(index)).encode()).hexdigest(),
            "event_ref": hashlib.sha256(("event_" + str(index)).encode()).hexdigest(),
            "observed_at": at, "scope": "global_style", "provider": provider, "purpose": purpose,
            "capability": capability, "features": featureize(text, purpose), "target": target, "weight": weight}


def predict(model, *, purpose="frontend", text="responsive frontend components layout", now=NOW,
            providers=("codex", "claude"), capability=None):
    return model.predict({"purpose": purpose, "features": featureize(text, purpose), "now": now},
                         [{"id": provider, "provider": provider, "capability": capability} for provider in providers])


def recheck(checkpoint):
    body = {key: value for key, value in checkpoint.items() if key != "checksum"}
    checkpoint["checksum"] = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                                       ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    return checkpoint


class FeatureTests(unittest.TestCase):
    def test_features_are_finite_normalized_deterministic_and_unicode(self):
        features = featureize("반응형 화면과 입력창 layout", "frontend")
        self.assertEqual(FEATURE_SCHEMA_VERSION, 1)
        self.assertEqual(len(features), FEATURE_DIMENSION)
        self.assertEqual(features[0], 1.0)
        self.assertTrue(all(math.isfinite(value) and -1 <= value <= 1 for value in features))
        self.assertLessEqual(sum(value * value for value in features[1:]), 1.000001)
        self.assertEqual(features, featureize("반응형 화면과 입력창 layout", "frontend"))
        self.assertNotEqual(features, featureize("데이터베이스 서버 migration", "backend"))

    def test_secret_identifier_email_url_and_private_path_values_do_not_become_features(self):
        first = "layout " + "sk-" + "A" * 40 + " https://example.invalid/?token=first one@example.invalid "
        first += "a1111111-2222-4333-8444-555555555555 " + "/".join(("", "home", "first", "private"))
        second = "layout " + "sk-" + "B" * 40 + " https://other.invalid/?token=second two@example.invalid "
        second += "b1111111-2222-4333-8444-666666666666 " + "/".join(("", "home", "second", "different"))
        self.assertEqual(featureize(first, "frontend"), featureize(second, "frontend"))
        self.assertEqual(featureize(first, "frontend"), featureize("layout", "frontend"))
        self.assertEqual(featureize("layout password=short-private", "frontend"), featureize("layout", "frontend"))
        self.assertEqual(featureize("layout Authorization: Bearer short-private", "frontend"),
                         featureize("layout", "frontend"))

    def test_invalid_feature_inputs_are_rejected(self):
        for value in (None, 42, "a" * 32769, "\ud800"):
            with self.subTest(type=type(value).__name__), self.assertRaises(ValueError):
                featureize(value)
        with self.assertRaises(ValueError):
            featureize("layout", "bad purpose")


class LearningTests(unittest.TestCase):
    def test_untrained_is_honest_and_no_candidate_is_implicitly_negative(self):
        model = PreferenceModel(clock=lambda: NOW)
        initial = predict(model)
        self.assertTrue(all(row["score"] == 0.5 and row["confidence"] == 0 and not row["model_trained"]
                            and row["training_state"] == "untrained" for row in initial))
        model.fit([sample(index) for index in range(6)])
        trained, absent = predict(model)
        self.assertGreater(trained["score"], 0.7)
        self.assertEqual(absent["score"], 0.5)
        self.assertFalse(absent["model_trained"])
        self.assertEqual(absent["evidence_refs"], [])
        self.assertFalse(trained["dispatch_authority"])

    def test_independent_positive_heads_can_both_be_high_without_softmax(self):
        model = PreferenceModel(clock=lambda: NOW)
        rows = [sample(f"{provider}_{index}", provider=provider) for provider in ("codex", "claude") for index in range(8)]
        report = model.fit(rows)
        predictions = predict(model)
        self.assertEqual(report["heads"], 2)
        self.assertTrue(all(row["score"] > 0.75 for row in predictions))
        self.assertGreater(sum(row["score"] for row in predictions), 1.5)
        self.assertTrue(all(row["training_state"] == "feedback_trained" for row in predictions))

    def test_actual_weights_and_prediction_change_after_known_feedback(self):
        model = PreferenceModel(clock=lambda: NOW)
        positive = [sample(index) for index in range(6)]
        model.fit(positive)
        before = predict(model)[0]["score"]
        first_checkpoint = model.to_dict()
        first_confidence = predict(model)[0]["confidence"]
        report = model.fit(positive + [sample("correction", target=0, at=NOW)])
        after = predict(model)[0]["score"]
        self.assertTrue(report["changed"])
        self.assertEqual(report["model_revision"], 2)
        self.assertGreater(before, 0.7)
        self.assertLess(after, 0.5)
        self.assertLess(predict(model)[0]["confidence"], first_confidence)
        self.assertNotEqual(first_checkpoint["heads"], model.to_dict()["heads"])
        self.assertTrue(any(head["change_at"] == NOW for head in model.to_dict()["heads"].values()))

    def test_current_explicit_preference_outranks_many_weak_uses(self):
        model = PreferenceModel(clock=lambda: NOW)
        rows = [sample(index, source_kind="observed_usage", weight=0.1, at=NOW - 1000) for index in range(120)]
        model.fit(rows)
        self.assertEqual(predict(model)[0]["training_state"], "inferred")
        model.fit(rows + [sample("explicit_no", target=0, at=NOW)])
        self.assertLess(predict(model)[0]["score"], 0.5)

    def test_current_explicit_provider_switch_outranks_old_usage_and_reloads(self):
        model = PreferenceModel(clock=lambda: NOW)
        text = "Design the current frontend"
        rows = [sample(index, purpose="design", text=text, source_kind="observed_usage",
                       weight=0.1, at=NOW - 86400) for index in range(1000)]
        model.fit(rows)
        original = model.to_dict()
        rows.append(sample("current_agy", provider="antigravity", purpose="design", text=text, at=NOW - 1))
        model.fit(rows)
        predictions = predict(model, purpose="design", text=text, providers=("codex", "antigravity"))
        self.assertEqual(max(predictions, key=lambda row: (row["lower_bound"], row["score"]))["provider"],
                         "antigravity")
        self.assertGreater(predictions[0]["score"], 0.5)  # Retain positive usage, never fabricate a negative.
        self.assertLess(predictions[0]["confidence"], predictions[1]["confidence"])
        codex_key = next(iter(original["heads"]))
        self.assertNotEqual(original["heads"][codex_key]["weights"], model.to_dict()["heads"][codex_key]["weights"])
        with patch("preference_learning.time.time", return_value=NOW):
            restored = PreferenceModel.from_dict(json.loads(json.dumps(model.to_dict())))
            self.assertFalse(restored.fit(rows)["changed"])
        self.assertEqual(predictions, predict(restored, purpose="design", text=text,
                                             providers=("codex", "antigravity")))
        self.assertTrue(all(not row["dispatch_authority"] for row in predictions))
        self.assertEqual(model.to_dict()["fit_report"]["moderated_usage_samples"], 1000)
        self.assertTrue(predictions[0]["preference"]["context_moderated"])
        self.assertIn(hashlib.sha256("evidence_current_agy".encode()).hexdigest(), predictions[0]["evidence_refs"])
        self.assertLessEqual(len(predictions[0]["evidence_refs"]), 16)

    def test_provider_switch_preserves_unrelated_context_and_verified_quality(self):
        model = PreferenceModel(clock=lambda: NOW)
        current = "Design the current frontend"
        unrelated = "database schema indexes transactions replication"
        rows = [sample(f"current_{index}", purpose="design", text=current, source_kind="observed_usage",
                       weight=0.1, at=NOW - 86400) for index in range(1000)]
        rows += [sample(f"other_{index}", purpose="design", text=unrelated, source_kind="observed_usage",
                        weight=0.1, at=NOW - 86400) for index in range(1000)]
        rows += [sample(f"quality_{index}", purpose="design", text=current,
                        source_kind="verified_outcome", at=NOW - 86400) for index in range(6)]
        model.fit(rows)
        quality = predict(model, purpose="design", text=current)[0]["quality"]
        rows.append(sample("current_agy", provider="antigravity", purpose="design", text=current, at=NOW - 1))
        model.fit(rows)
        predictions = predict(model, purpose="design", text=current, providers=("codex", "antigravity"))
        self.assertEqual(max(predictions, key=lambda row: (row["lower_bound"], row["score"]))["provider"],
                         "antigravity")
        self.assertEqual(predictions[0]["quality"], quality)
        other = predict(model, purpose="design", text=unrelated)[0]
        self.assertGreater(other["score"], 0.7)
        self.assertFalse(other["preference"]["context_moderated"])
        alone = predict(model, purpose="design", text=current, providers=("codex",))[0]
        self.assertEqual(alone, predictions[0])

    def test_features_learn_different_contexts_within_one_provider_and_purpose(self):
        model = PreferenceModel(clock=lambda: NOW)
        positive_text = "responsive forms components keyboard navigation"
        negative_text = "database schema indexes transactions replication"
        rows = []
        for index in range(8):
            rows.append(sample(f"yes_{index}", text=positive_text, at=NOW - 1000 + index * 2))
            rows.append(sample(f"no_{index}", text=negative_text, target=0, at=NOW - 999 + index * 2))
        model.fit(rows)
        positive = predict(model, text=positive_text)[0]
        negative = predict(model, text=negative_text)[0]
        self.assertGreater(positive["score"], 0.65)
        self.assertLess(negative["score"], 0.35)
        self.assertEqual(len(model.to_dict()["heads"]), 1)
        self.assertIsNone(next(iter(model.to_dict()["heads"].values()))["change_at"])

    def test_quality_is_trained_only_from_verified_outcome_and_does_not_replace_preference(self):
        model = PreferenceModel(clock=lambda: NOW)
        rows = [sample(index) for index in range(6)]
        rows += [sample(f"quality_{index}", target=0, source_kind="verified_outcome") for index in range(6)]
        model.fit(rows)
        prediction = predict(model)[0]
        self.assertGreater(prediction["preference"]["probability"], 0.7)
        self.assertLess(prediction["quality"]["probability"], 0.5)
        self.assertEqual(prediction["quality"]["training_state"], "outcome_trained")
        self.assertEqual(prediction["score"], prediction["preference"]["probability"])

    def test_missing_feedback_peer_and_negative_usage_are_not_ground_truth(self):
        model = PreferenceModel(clock=lambda: NOW)
        report = model.fit([sample(1, target=None), sample(2, source_kind="peer"),
                            sample(3, source_kind="system_recommendation"),
                            sample(4, source_kind="observed_usage", target=0)])
        self.assertEqual(report["accepted_samples"], 0)
        self.assertEqual(report["heads"], 0)
        self.assertEqual(report["skipped"], {"unlabelled": 1, "untrusted_source": 2,
                                            "usage_is_not_negative_feedback": 1})
        self.assertFalse(predict(model)[0]["model_trained"])

    def test_refit_deduplicates_and_is_idempotent_without_changing_input(self):
        rows = [sample(index) for index in range(6)]
        unchanged = deepcopy(rows)
        model = PreferenceModel(clock=lambda: NOW)
        first = model.fit(rows + [deepcopy(rows[0])])
        checkpoint = model.to_dict()
        second = model.fit(list(reversed(rows)))
        self.assertEqual(first["accepted_samples"], 6)
        self.assertFalse(second["changed"])
        self.assertEqual(checkpoint, model.to_dict())
        self.assertEqual(rows, unchanged)
        conflict = deepcopy(rows[0]); conflict["target"] = 0
        with self.assertRaises(ValueError):
            model.fit(rows + [conflict])
        self.assertEqual(checkpoint, model.to_dict())

    def test_stale_evidence_lowers_confidence_and_moves_score_toward_neutral(self):
        model = PreferenceModel(clock=lambda: NOW)
        model.fit([sample(index, at=NOW) for index in range(8)])
        fresh = predict(model)[0]
        stale = predict(model, now=NOW + 6 * HALF_LIFE_SECONDS)[0]
        self.assertLess(stale["confidence"], fresh["confidence"] / 10)
        self.assertGreater(stale["uncertainty"], fresh["uncertainty"])
        self.assertLess(abs(stale["score"] - 0.5), abs(fresh["score"] - 0.5) / 10)

    def test_capability_specific_head_and_generic_fallback_are_separate(self):
        model = PreferenceModel(clock=lambda: NOW)
        rows = [sample(index) for index in range(6)]
        rows += [sample(f"cap_{index}", capability="computer_use", target=0) for index in range(6)]
        model.fit(rows)
        self.assertGreater(predict(model, capability="code_edit")[0]["score"], 0.7)
        self.assertLess(predict(model, capability="computer_use")[0]["score"], 0.5)

    def test_invalid_data_fails_atomically_and_cannot_archive_raw_text(self):
        model = PreferenceModel(clock=lambda: NOW)
        model.fit([sample(1)])
        checkpoint = model.to_dict()
        variants = []
        bad = sample(2); bad["raw_text"] = "not retained"; variants.append(bad)
        bad = sample(3); bad["features"] = [1.0] * 64; variants.append(bad)
        bad = sample(4); bad["features"][1] = float("nan"); variants.append(bad)
        bad = sample(6); bad["provider"] = "bad/provider"; variants.append(bad)
        bad = sample(7); bad["weight"] = 1000; variants.append(bad)
        bad = sample(8); bad["source_ref"] = "literal_reference"; variants.append(bad)
        bad = sample(9); bad["features"][1] = 10 ** 1000; variants.append(bad)
        for row in variants:
            with self.subTest(index=row["id"]), self.assertRaises(ValueError):
                model.fit([sample(1), row])
            self.assertEqual(checkpoint, model.to_dict())

    def test_future_dated_import_is_rejected_without_poisoning_valid_learning(self):
        valid = [sample(index) for index in range(6)]
        future = [sample("future", target=0, at=NOW + 365 * 86400),
                  sample("outside_tolerance", target=0, at=NOW + 301)]
        model = PreferenceModel(clock=lambda: NOW)
        report = model.fit(valid + future)
        reference = PreferenceModel(clock=lambda: NOW)
        reference.fit(valid)
        self.assertEqual(report["skipped"], {"future_evidence": 2})
        self.assertEqual(report["accepted_samples"], 6)
        self.assertEqual(predict(model), predict(reference))
        self.assertEqual(model.to_dict()["heads"], reference.to_dict()["heads"])
        repeat = model.fit(valid + future)
        self.assertFalse(repeat["changed"])
        self.assertEqual(repeat["skipped"], {"future_evidence": 2})
        self.assertEqual(predict(model), predict(PreferenceModel.from_dict(model.to_dict())))
        tolerated = PreferenceModel(clock=lambda: NOW)
        self.assertEqual(tolerated.fit([sample("clock_skew", at=NOW + 300)])["accepted_samples"], 1)

    def test_synthetic_chronological_heldout_choices_and_checkpoint_parity(self):
        # Known synthetic labels, disjoint evidence IDs and later held-out times.
        model = PreferenceModel(clock=lambda: NOW)
        rows = []
        for index in range(12):
            rows.append(sample(f"frontend_yes_{index}", provider="codex", at=NOW - 1000 - index,
                               text=f"responsive frontend component layout variant {index}"))
            rows.append(sample(f"frontend_no_{index}", provider="claude", target=0, at=NOW - 1000 - index,
                               text=f"responsive frontend component layout variant {index}"))
            rows.append(sample(f"backend_yes_{index}", purpose="backend", provider="claude", at=NOW - 1000 - index,
                               text=f"backend database server migration variant {index}"))
            rows.append(sample(f"backend_no_{index}", purpose="backend", provider="codex", target=0,
                               at=NOW - 1000 - index, text=f"backend database server migration variant {index}"))
        model.fit(rows)
        restored = PreferenceModel.from_dict(json.loads(json.dumps(model.to_dict())))
        correct = total = 0
        for purpose, text, preferred in (("frontend", "responsive frontend component layout new screen", "codex"),
                                         ("backend", "backend database server migration new endpoint", "claude")):
            observed = predict(model, purpose=purpose, text=text)
            self.assertEqual(observed, predict(restored, purpose=purpose, text=text))
            for row in observed:
                correct += (row["score"] >= 0.5) == (row["provider"] == preferred)
                total += 1
        self.assertEqual(correct, total)
        self.assertGreater(correct / total, 0.5)  # Neutral positive-only baseline is 50% here.


class CheckpointTests(unittest.TestCase):
    def test_untrained_and_trained_roundtrip_retain_only_numeric_feature_model(self):
        model = PreferenceModel(clock=lambda: NOW)
        self.assertEqual(model.to_dict(), PreferenceModel.from_dict(model.to_dict()).to_dict())
        model.fit([sample(index, text="unique private sentence not stored verbatim") for index in range(6)])
        encoded = json.dumps(model.to_dict())
        self.assertNotIn("unique private sentence", encoded)
        self.assertNotIn("evidence_0", encoded)
        restored = PreferenceModel.from_dict(json.loads(encoded))
        self.assertEqual(model.to_dict(), restored.to_dict())

    def test_checksum_and_recomputed_shape_target_and_extra_field_tampering_fail(self):
        model = PreferenceModel(clock=lambda: NOW)
        model.fit([sample(index) for index in range(6)])
        original = model.to_dict()
        key = next(iter(original["heads"]))
        changed = deepcopy(original); changed["heads"][key]["weights"][0] += 1
        with self.assertRaises(ValueError):
            PreferenceModel.from_dict(changed)
        variants = []
        changed = deepcopy(original); changed["heads"][key]["weights"].pop(); variants.append(changed)
        changed = deepcopy(original); changed["heads"][key]["source_counts"] = {"verified_outcome": 6}; variants.append(changed)
        changed = deepcopy(original); changed["fit_report"]["raw_transcript"] = "forbidden"; variants.append(changed)
        changed = deepcopy(original); changed["heads"][key]["variances"][0] = -1; variants.append(changed)
        changed = deepcopy(original); changed["dimension"] = True; variants.append(changed)
        changed = deepcopy(original); changed["prior_precision"] = True; variants.append(changed)
        for changed in variants:
            with self.assertRaises(ValueError):
                PreferenceModel.from_dict(recheck(changed))

    def test_prediction_evidence_is_bounded_and_output_has_no_execution_authority(self):
        model = PreferenceModel(clock=lambda: NOW)
        model.fit([sample(index) for index in range(30)])
        row = predict(model)[0]
        self.assertEqual(len(row["evidence_refs"]), 16)
        self.assertEqual(row["preference"]["evidence_count"], 30)
        self.assertFalse(row["dispatch_authority"])
        self.assertTrue(0 <= row["lower_bound"] <= row["score"] <= row["upper_bound"] <= 1)

    def test_feedback_context_checkpoint_is_bounded_strict_and_rejects_old_policy(self):
        model = PreferenceModel(clock=lambda: NOW)
        rows = [sample(index, text=f"distinct style context {chr(0x4e00 + index)}", at=NOW - 1000 + index)
                for index in range(MAX_FEEDBACK_CONTEXTS * 2)]
        self.assertGreater(len({tuple(row["features"]) for row in rows}), MAX_FEEDBACK_CONTEXTS)
        model.fit(rows)
        original = model.to_dict()
        self.assertEqual(len(original["feedback_contexts"]), MAX_FEEDBACK_CONTEXTS)
        self.assertLess(len(json.dumps(original).encode()), 4 * 1024 * 1024)
        self.assertEqual(original, PreferenceModel.from_dict(original).to_dict())
        variants = []
        changed = deepcopy(original); changed["schema"] = "avalokita-preference-posterior/1"; variants.append(changed)
        changed = deepcopy(original); changed["feedback_contexts"][0]["features"][0] = 0; variants.append(changed)
        changed = deepcopy(original); changed["feedback_contexts"][0]["raw_text"] = "forbidden"; variants.append(changed)
        changed = deepcopy(original); changed["feedback_contexts"][0]["evidence_ref"] = "0" * 64; variants.append(changed)
        changed = deepcopy(original); changed["feedback_contexts"][0]["observed_at"] = NOW + 365 * 86400; variants.append(changed)
        changed = deepcopy(original); changed["feedback_contexts"].append(deepcopy(changed["feedback_contexts"][0])); variants.append(changed)
        for changed in variants:
            with self.assertRaises(ValueError):
                PreferenceModel.from_dict(recheck(changed))


if __name__ == "__main__":
    unittest.main()
