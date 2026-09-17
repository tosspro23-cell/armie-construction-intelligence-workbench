"""SPEC-M16: a live, business-scenario stress test for the V2 tool-calling
agent -- explicit and ambiguous phrasings, English and Chinese, run against
a real project and a real deployed model, each answer cross-checked against
ground truth computed independently (straight from the IFC file via
`IfcRepository`, never the LLM).

Owner-requested, 2026-09-17, after a manually-found bug (a storey name
passed to a tool untranslated) turned out to be one instance of a broader
pattern this script is designed to keep finding: real model behavior that
no unit test (which scripts the model's own tool calls with
`FakeModelProvider`) can exercise, because the whole point is discovering
what a *real* model actually does with an ambiguous or adversarial
question. The first run (this same day) found one real, confirmed bug this
way: `invoke_v2`'s disposition line was dead code (`"answered" if
all_citations else "answered"`, both branches identical) -- see
docs/reports/2026-09-16-m16-v1-vs-v2-benchmark.md's "known gaps" section
and commit 8332940.

Deliberately NOT a pytest test: it needs a running local API server
(engine="v2" wired to a real Azure OpenAI deployment, not
`FakeModelProvider`), makes real, metered model calls against that
deployment's own rate limit, and its assertions are "does this look
right," not a fixed pass/fail oracle -- matching this repo's own
established split between CI-safe tests (tests/) and live-data scripts
(scripts/, e.g. index_document_corpus.py).

Prerequisites:
    1. A local API server running with a real Azure OpenAI deployment, e.g.:
        LLM_PROVIDER=azure AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/ \\
        AZURE_OPENAI_TEXT_DEPLOYMENT=<deployment> AZURE_OPENAI_VISION_DEPLOYMENT=<deployment> \\
        ADLS_ACCOUNT_URL=https://<account>.dfs.core.windows.net \\
        API_SHARED_SECRET=<secret> \\
        PYTHONPATH=apps/api python3 -m uvicorn app.main:app --app-dir apps/api --port 8001
    2. That server's project registry must include the project this script
       targets (ADLS multi-project mode, or the single implicit project if
       --project is omitted).

Usage:
    API_SHARED_SECRET=<secret> python3 scripts/v2_domain_sweep.py --project duplex
    API_SHARED_SECRET=<secret> python3 scripts/v2_domain_sweep.py --project duplex --out /tmp/sweep.json
    API_SHARED_SECRET=<secret> python3 scripts/v2_domain_sweep.py --project duplex --skip-ground-truth

The question set is fixed content designed to probe universal V2 behavior
classes (ambiguous entity, out-of-scope attribute, nonexistent storey,
ordinary counts, multi-entity, reconciliation) -- portable across any
project. The two storey-scoped questions are generated from whichever real
storey names the target project's own IFC file actually has, so they stay
meaningful without editing this file per project.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))


def compute_ground_truth(ifc_path: Path) -> dict:
    """Independent oracle: the same deterministic code the tools call,
    invoked directly -- no model, no HTTP, no chance of the thing being
    tested also being the thing doing the checking.
    """
    from app.agent.router import SUPPORTED_ENTITY_TYPES
    from app.schemas.models import IfcQueryInput
    from app.tools.ifc.repository import IfcRepository

    repo = IfcRepository(ifc_path)
    meta = repo.metadata()
    storeys = [s["name"] for s in meta["storeys"] if s.get("name")]
    ground_truth: dict = {"storeys": storeys, "entities": {}}
    for entity in sorted(SUPPORTED_ENTITY_TYPES):
        try:
            total = repo.execute(IfcQueryInput(operation="count", entity_type=entity, filters={}))
            by_storey = repo.execute(IfcQueryInput(operation="group_by", entity_type=entity, filters={}, group_by="storey"))
            if total.value:
                ground_truth["entities"][entity] = {"total": total.value, "by_storey": by_storey.value}
        except Exception:
            continue
    return ground_truth


def _english_alias(entity_type: str) -> str:
    """The natural plural noun a user would actually type for an entity
    type (door, window, furniture...), the same vocabulary the router's
    own ELEMENT_ALIASES teaches the model -- reused here instead of a
    naive string transform, which mangles compounds like
    IfcFurnishingElement into "furnishingelements."
    """
    from app.agent.router import ELEMENT_ALIASES

    for alias, canonical in ELEMENT_ALIASES.items():
        if canonical == entity_type and alias.isascii() and alias.endswith("s"):
            return alias
    return entity_type.removeprefix("Ifc").lower() + "s"


def build_questions(ground_truth: dict) -> list[tuple[str, str]]:
    storeys = ground_truth["storeys"]
    # Prefer two storeys that actually differ in count for the entity with
    # the most total elements, so the storey-scoped questions have a real,
    # checkable, non-trivial answer regardless of which project this runs
    # against.
    entities = ground_truth["entities"]
    biggest_entity = max(entities, key=lambda e: entities[e]["total"]) if entities else None
    biggest_alias = _english_alias(biggest_entity) if biggest_entity else "doors"
    # For the storey-scoped question specifically, an entity whose IFC data
    # doesn't expose real storey relationships at all (every match lands in
    # "Unassigned" -- true of furnishing elements in some real exports)
    # makes for an uninteresting probe. Prefer the biggest entity that
    # actually varies by storey; fall back to the overall biggest.
    storey_capable = {
        e: v for e, v in entities.items()
        if not (len(v["by_storey"]) == 1 and "Unassigned" in v["by_storey"])
    }
    storey_entity = max(storey_capable, key=lambda e: storey_capable[e]["total"]) if storey_capable else biggest_entity
    storey_alias = _english_alias(storey_entity) if storey_entity else biggest_alias
    storey_a = storeys[0] if storeys else "Level 1"
    storey_b = storeys[1] if len(storeys) > 1 else storey_a

    questions = [
        ("count-total-EN", f"How many {biggest_alias} are there in total?"),
        ("count-total-ZH", "这栋楼一共有多少堵墙？"),
        ("count-total-EN2", "How many stairs are in the building?"),
        ("count-storey-EN", f"How many {storey_alias} are on {storey_a}?"),
        # A fixed Chinese noun (窗户/windows), not a translation of
        # `biggest_alias` -- machine-translating an arbitrary entity name
        # mid-sentence reads unnaturally and isn't what a real user would
        # type; the point of this question is testing storey-name
        # resolution, not vocabulary breadth (already covered by the fixed
        # 墙/门/家具 questions elsewhere in this list).
        ("count-storey-ZH", f"{storey_b}有多少扇窗户？"),
        ("group-argmax-EN", "Which storey has the most windows?"),
        ("group-argmin-ZH", "哪个楼层的墙最少？"),
        ("aggregate-max-EN", "What is the maximum door height?"),
        ("aggregate-min-ZH", "最小的窗户宽度是多少？"),
        ("properties-EN", "What properties does the stair have?"),
        ("furniture-total-ZH", "家具一共有多少件？"),
        ("reconcile-EN", "Compare the doors and windows against the PDF schedule."),
        ("vague-entity-ZH", "这个房子里有多少东西？"),
        ("open-ended-EN", "Tell me about this building."),
        ("unsupported-entity-EN", "How many elevators are there?"),
        ("unsupported-entity-ZH", "有多少个电梯？"),
        ("out-of-scope-attribute-EN", "Check if the door fire ratings match the PDF schedule."),
        ("nonexistent-storey-EN", "How many doors are on Level 97?"),
        ("nonexistent-storey-ZH", "三楼有几扇窗户？"),
        ("multi-entity-EN", "How many doors, windows, and walls are there?"),
        ("multi-entity-ZH", "门、窗户、墙分别有多少个？"),
    ]
    return questions


def ask(base_url: str, api_key: str, project_id: str, question: str) -> dict:
    body = {"project_id": project_id, "question": question, "engine": "v2"}
    req = urllib.request.Request(
        f"{base_url}/api/v1/chat",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=150) as resp:
            raw = resp.read().decode()
    except Exception as e:
        return {"error": f"HTTP_EXCEPTION: {e}"}
    final = None
    for line in raw.split("\n"):
        if line.startswith("data: ") and '"type": "final"' in line:
            final = json.loads(line[6:])["response"]
        if line.startswith("data: ") and '"type": "error"' in line:
            final = {"error": json.loads(line[6:])["message"]}
    return final or {"error": "NO_FINAL_EVENT", "raw_tail": raw[-500:]}


def fetch_trace(base_url: str, api_key: str, trace_id: str) -> list[dict]:
    req = urllib.request.Request(
        f"{base_url}/api/v1/traces/{trace_id}", headers={"Authorization": f"Bearer {api_key}"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def run_sweep(base_url: str, api_key: str, project_id: str, questions: list[tuple[str, str]], delay_seconds: float) -> list[dict]:
    results = []
    for label, q in questions:
        r = ask(base_url, api_key, project_id, q)
        entry: dict = {"label": label, "question": q}
        if "error" in r:
            entry["status"] = "ERROR"
            entry["detail"] = r["error"]
        else:
            entry["status"] = r["disposition"]
            entry["answer"] = r["answer_markdown"]
            entry["verification"] = r["verification"]["status"]
            entry["citation_count"] = len(r["citations"])
            entry["citation_storeys"] = sorted({c["locator"].get("storey") for c in r["citations"] if c["locator"].get("storey")})
            entry["citation_entity_types"] = sorted({c["locator"].get("entity_type") for c in r["citations"] if c["locator"].get("entity_type")})
            entry["tool_call_count"] = r["execution_metadata"].get("tool_call_count")
            entry["trace_id"] = r["trace_id"]
            try:
                trace = fetch_trace(base_url, api_key, r["trace_id"])
                entry["queries"] = [e["payload"]["query"] for e in trace if "query" in e.get("payload", {})]
            except Exception as e:
                entry["trace_fetch_error"] = str(e)
        results.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
        time.sleep(delay_seconds)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project", default="duplex", help="project_id to target (must exist in the running server's registry)")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001", help="local API server base URL")
    parser.add_argument("--api-key", default=None, help="shared secret; defaults to $API_SHARED_SECRET")
    parser.add_argument("--ifc-path", default=None, help="path to the project's IFC file, for ground truth; defaults to demo_data/projects/<project>/*.ifc")
    parser.add_argument("--skip-ground-truth", action="store_true", help="skip the ground-truth computation (storey-scoped questions fall back to 'Level 1')")
    parser.add_argument("--delay", type=float, default=4.0, help="seconds to sleep between questions, to stay under the deployment's own rate limit")
    parser.add_argument("--out", default=None, help="path to write the full JSON results to")
    args = parser.parse_args()

    import os

    api_key = args.api_key or os.environ.get("API_SHARED_SECRET")
    if not api_key:
        raise SystemExit("Pass --api-key or set API_SHARED_SECRET.")

    ground_truth = {"storeys": [], "entities": {}}
    if not args.skip_ground_truth:
        ifc_path = Path(args.ifc_path) if args.ifc_path else next((ROOT / "demo_data" / "projects" / args.project).glob("*.ifc"), None)
        if ifc_path and ifc_path.exists():
            ground_truth = compute_ground_truth(ifc_path)
            print("Ground truth:", json.dumps(ground_truth, ensure_ascii=False))
        else:
            print(f"No IFC file found for project '{args.project}' -- skipping ground truth.", file=sys.stderr)

    questions = build_questions(ground_truth)
    results = run_sweep(args.base_url, api_key, args.project, questions, args.delay)

    out_path = Path(args.out) if args.out else ROOT / f"v2_domain_sweep_{args.project}_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out_path.write_text(json.dumps({"ground_truth": ground_truth, "results": results}, ensure_ascii=False, indent=2))
    print(f"\nWrote {len(results)} results to {out_path}")


if __name__ == "__main__":
    main()
