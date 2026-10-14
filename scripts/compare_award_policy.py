"""Offline development comparison from a frozen, local replay request.

Never queries a broker/database, calls an LLM, or writes operating state. The
new-pool experiment reuses one saved LLM proposal; it is not a fresh LLM result.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.daily_evaluation import select_daily_candidate
from backend.learning_policy import policy_id
from backend.learning_research import propose_candidate
from backend.source_version import DAILY_SOURCES, source_version


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def compare(request):
    starting_engine = source_version(DAILY_SOURCES)
    champion, frames, limits = (deepcopy(request[key]) for key in ("champion", "frames", "limits"))
    context = deepcopy(request["award_context"])
    original = deepcopy(request["original_proposal"])
    saved_llm = next((item["policy"] for item in original["candidate_pool"]
                      if item.get("method", "").startswith("llm")), None)

    class SavedProvider:
        def generate(self, *args):
            return deepcopy(saved_llm)

    regenerated = propose_candidate(champion, frames, limits, deepcopy(request["history"]),
        SavedProvider() if saved_llm else None, award_context=context)
    prepared_parent = next(item["policy"] for item in original["candidate_pool"]
                           if policy_id(item["policy"]) == request["pending_identity_before"]["candidate_id"])
    rules_pool = propose_candidate(prepared_parent, frames, limits, deepcopy(request["history"]),
                                   award_context=context)
    results = {}
    for name, parent, proposal, award in (("original_pool_original_selector", champion, original, None),
                                  ("original_pool_award_selector", champion, original, context),
                                  ("new_pool_award_selector", champion, regenerated, context),
                                  ("prepared_rules_parent_sensitivity", prepared_parent, rules_pool, context)):
        started = perf_counter()
        selected, result = select_daily_candidate(parent, proposal, frames, limits,
            evaluation_context=deepcopy(request["original_daily_decision"].get("evaluation_context")),
            award_context=award)
        audit = result.get("metrics", {}).get("award_selection", {})
        selected_id = result.get("metrics", {}).get("selection", {}).get("selected_candidate_id")
        outcomes = audit.get("outcomes", [])
        selected_evidence = next((item for item in outcomes if item["candidate_id"] == selected_id), None)
        results[name] = {"decision": result["decision"], "reason": result["reason"],
            "elapsed_seconds": round(perf_counter() - started, 3),
            "selected_candidate_id": selected_id, "selected_policy": selected["policy"] if result["decision"] == "apply" else parent,
            "parent_id": policy_id(parent), "search_plan": proposal.get("search_plan"),
            "selected_evidence": selected_evidence, "result": result,
            "pool": [{"candidate_id": policy_id(item["policy"]), "method": item.get("method"),
                       "policy": item["policy"]} for item in proposal["candidate_pool"]]}
    final_engine = source_version(DAILY_SOURCES)
    assert final_engine == starting_engine, "candidate engine changed during replay; rerun the frozen version"
    return {"version": "award-offline-comparison-v2", "created_at": datetime.now(timezone.utc).isoformat(),
        "engine_version": final_engine, "engine_unchanged_during_replay": True, "request_content_hash": digest(request),
        "provenance": request["provenance"], "pending_identity_at_capture": request["pending_identity_before"],
        "limits": limits, "competition_context": context["competition_context"],
        "limitations": ["Development data reused; no independent prospective result or win probability.",
            "New pool uses reconstructed earlier feedback and a saved LLM answer, not a fresh LLM invocation.",
            "Prepared-rules-parent arm assumes activation only for offline sensitivity; no 10/15 data or fresh LLM, and its historical window differs from legacy paired comparison.",
            "Current-book forward analogues use historical paths; incomplete historical evidence stays unknown.",
            "Preview uses observed closing prices and conditional fills, not a next-session order forecast.",
            "No production source, policy, process, account setting, order or ledger is changed."],
        "comparisons": results}


def summary(result):
    output = {}
    for name, value in result["comparisons"].items():
        evidence = value.get("selected_evidence") or {}
        preview, qualification = evidence.get("transition", {}), evidence.get("qualification", {})
        audit = value["result"].get("metrics", {}).get("award_selection", {})
        metrics = value["result"].get("metrics", {})
        output[name] = {"decision": value["decision"], "candidate_id": value["selected_candidate_id"],
            "name": value["selected_policy"]["name"], "reason": value["reason"],
            "pool_size": len(value["pool"]), "eligible_candidates": audit.get("eligible_candidates"),
            "conditional_entries": preview.get("entry_count"), "conditional_buy": preview.get("gross_buy_amount"),
            "qualification": qualification.get("status"), "known_paths": qualification.get("evaluated_scenarios"),
            "qualified_paths": qualification.get("qualifying_scenarios"),
            "net_return_pct": metrics.get("selection_return_pct") if value["decision"] == "apply" else None,
            "stress_return_pct": metrics.get("selection_stress_return_pct") if value["decision"] == "apply" else None,
            "rejection_reasons": {reason: sum(item["promotion_reason"] == reason for item in audit.get("outcomes", []))
                for reason in sorted({item["promotion_reason"] for item in audit.get("outcomes", [])})}}
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Block side effects from an accidentally imported production dependency.
    def guard(event, arguments):
        if event.startswith(("socket.", "subprocess.", "sqlite3.connect")):
            raise RuntimeError("offline comparison forbids network, processes and databases")
    sys.addaudithook(guard)
    raw = args.request.read_bytes()
    request = json.loads(gzip.decompress(raw) if args.request.suffix == ".gz" else raw)
    before = digest(request)
    result = compare(request)
    assert digest(request) == before, "input mutated"
    result["request_file_sha256"] = hashlib.sha256(raw).hexdigest()
    result["summary"] = summary(result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
