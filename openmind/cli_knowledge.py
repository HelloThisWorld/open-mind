"""CLI adapters for the Phase 5 canonical Knowledge Graph: the ``graph``,
``promotion``, ``entity``, ``claim``, ``relation`` and ``bundle`` command
groups, plus the history subcommands of the existing ``knowledge`` group.

The CONTRACT is exactly the parent CLI's: ``--json`` prints one object on
stdout, humans read stderr, no ANSI, the shared exit codes, bounded output,
no secrets. Graph mutations all take explicit ``--actor`` and ``--note`` —
identity is never inferred — and there is no flag anywhere here that skips
an eligibility rule (no ``--accept-stale``, no ``--force-unverified``).
"""
from __future__ import annotations

import argparse
import json
from typing import Any, Dict, List, Tuple

from .domain.errors import InvalidRequest


def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = {"ok": True}
    out.update(payload)
    return out


def _knowledge():
    from .runtime import get_runtime
    return get_runtime().knowledge


def _evidence_from_args(args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Evidence references from ``--evidence`` (repeatable ids) and/or
    ``--evidence-json`` (a JSON array of {evidence_id, quote?, role?})."""
    refs: List[Dict[str, Any]] = []
    for evidence_id in getattr(args, "evidence", None) or []:
        refs.append({"evidence_id": str(evidence_id).strip()})
    raw = getattr(args, "evidence_json", None)
    if raw:
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise InvalidRequest(f"--evidence-json is not valid JSON: {exc}")
        if not isinstance(data, list):
            raise InvalidRequest("--evidence-json must be a JSON array")
        for entry in data:
            if not isinstance(entry, dict) or not entry.get("evidence_id"):
                raise InvalidRequest(
                    "--evidence-json entries must be objects with an "
                    "evidence_id")
            refs.append(entry)
    return refs


def _actor_note(args: argparse.Namespace) -> Tuple[str, str]:
    return str(getattr(args, "actor", "") or ""), \
        str(getattr(args, "note", "") or "")


def _source_command(args: argparse.Namespace, verb: str) -> str:
    return f"cli:{verb}"


# ---------------------------------------------------------------------------
# graph commands
# ---------------------------------------------------------------------------
def cmd_graph_stats(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_knowledge().get_stats(args.workspace))

    def human(p: Dict[str, Any]) -> None:
        print(f"knowledge revision: {p['knowledge_revision']}")
        print(f"entities: {p['entities_active']} active / "
              f"{p['entities_total']} total")
        for etype, count in sorted(p.get("entities_by_type", {}).items()):
            print(f"  {etype:24} {count}")
        print(f"claims: {p['claims_active']} active / {p['claims_total']}")
        print(f"relations: {p['relations_active']} active / "
              f"{p['relations_total']}")
        print(f"decisions: {p['decisions']}  promotions: {p['promotions']}")

    out.emit(payload, human)
    return 0, payload


def cmd_graph_seed_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"plan": _knowledge().plan_seed(args.workspace)})

    def human(p: Dict[str, Any]) -> None:
        plan = p["plan"]
        print(f"unchanged: {plan['unchanged']}")
        print(f"desired: {plan['desired_entities']} entities, "
              f"{plan['desired_relations']} relations")
        print(f"would create: {plan['would_create_entities']} entities, "
              f"{plan['would_create_relations']} relations")
        print(f"would stale: {plan['would_stale_entities']} entities, "
              f"{plan['would_stale_relations']} relations")

    out.emit(payload, human)
    return 0, payload


def cmd_graph_seed(args, out) -> Tuple[int, Dict[str, Any]]:
    if not getattr(args, "workspace", None):
        raise InvalidRequest("--workspace is required")
    actor, _ = _actor_note(args)
    payload = _ok(_knowledge().seed(args.workspace, actor=actor))
    out.emit(payload, lambda p: print(
        f"{p['action']}: knowledge revision {p['knowledge_revision']}"))
    return 0, payload


def cmd_graph_sync(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, _ = _actor_note(args)
    payload = _ok(_knowledge().sync(args.workspace, actor=actor))
    out.emit(payload, lambda p: print(
        f"{p['action']}: knowledge revision {p['knowledge_revision']}"))
    return 0, payload


def cmd_graph_reconcile(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, _ = _actor_note(args)
    payload = _ok(_knowledge().reconcile_staleness(args.workspace,
                                                   actor=actor))
    out.emit(payload, lambda p: print(
        f"changed: {p['changed']} (revision {p['knowledge_revision']})"))
    return 0, payload


def cmd_graph_search(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_knowledge().search_entities(
        args.workspace, args.query, limit=args.limit,
        include_stale=bool(getattr(args, "include_stale", False))))

    def human(p: Dict[str, Any]) -> None:
        for hit in p["entities"]:
            print(f"  entity {hit['id']}  {hit['canonical_key']}  "
                  f"[{hit['matched_via']} {hit['score']}]")
        for hit in p["claims"]:
            print(f"  claim  {hit['id']}  {hit['statement'][:80]}  "
                  f"[{hit['matched_via']} {hit['score']}]")
        if not p["entities"] and not p["claims"]:
            print("no graph objects matched")

    out.emit(payload, human)
    return 0, payload


def cmd_graph_node(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"node": _knowledge().get_node(args.workspace, args.node)})
    out.emit(payload, lambda p: print(json.dumps(p["node"], indent=2,
                                                 default=str)))
    return 0, payload


def cmd_graph_expand(args, out) -> Tuple[int, Dict[str, Any]]:
    types = [t.strip() for t in str(getattr(args, "types", "") or "").split(
        ",") if t.strip()]
    payload = _ok(_knowledge().expand_node(
        args.workspace, args.node, depth=args.depth,
        direction=args.direction, relation_types=types or None,
        include_stale=bool(getattr(args, "include_stale", False))))

    def human(p: Dict[str, Any]) -> None:
        print(f"{len(p['nodes'])} nodes, {len(p['edges'])} edges"
              + (" (truncated)" if p["truncated"] else ""))
        for edge in p["edges"][:50]:
            print(f"  {edge['sourceEntityId']} -{edge['relationType']}-> "
                  f"{edge['targetEntityId']} [{edge['relationState']}]")

    out.emit(payload, human)
    return 0, payload


def cmd_graph_path(args, out) -> Tuple[int, Dict[str, Any]]:
    types = [t.strip() for t in str(getattr(args, "types", "") or "").split(
        ",") if t.strip()]
    payload = _ok(_knowledge().find_path(
        args.workspace, getattr(args, "from"), args.to,
        max_depth=args.max_depth, direction=args.direction,
        relation_types=types or None,
        include_stale=bool(getattr(args, "include_stale", False))))

    def human(p: Dict[str, Any]) -> None:
        print(f"outcome: {p['outcome']}")
        for path in p.get("paths", []):
            chain = path["entities"][0]
            for i, edge in enumerate(path["edges"]):
                chain += (f" -{edge['relationType']}-> "
                          + path["entities"][i + 1])
            print(f"  [{path['length']}] {chain}")

    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# promotion commands
# ---------------------------------------------------------------------------
def cmd_promotion_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"plan": _knowledge().plan_candidate_promotion(
        args.workspace, args.candidate)})

    def human(p: Dict[str, Any]) -> None:
        plan = p["plan"]
        print(f"eligible: {plan['eligible']}")
        print(f"expected action: {plan['expected_action']}")
        for reason in plan.get("blocking_reasons", []):
            print(f"  blocked: {reason}")

    out.emit(payload, human)
    return 0, payload


def cmd_promotion_promote(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().promote_candidate(
        args.workspace, args.candidate, actor=actor, note=note,
        source_command=_source_command(args, "promotion promote")))

    def human(p: Dict[str, Any]) -> None:
        print(f"status: {p['status']}")
        if p.get("entity"):
            print(f"entity: {p['entity']['id']}  "
                  f"{p['entity']['canonical_key']}")
        if p.get("claim"):
            print(f"claim: {p['claim']['id']}")
        print(f"knowledge revision: {p['knowledge_revision']}")

    out.emit(payload, human)
    return 0, payload


def cmd_promotion_relation_plan(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"plan": _knowledge().plan_relation_promotion(
        args.workspace, args.relation)})

    def human(p: Dict[str, Any]) -> None:
        plan = p["plan"]
        print(f"eligible: {plan['eligible']}")
        print(f"expected action: {plan['expected_action']}")
        for reason in plan.get("blocking_reasons", []):
            print(f"  blocked: {reason}")

    out.emit(payload, human)
    return 0, payload


def cmd_promotion_promote_relation(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().promote_relation(
        args.workspace, args.relation, actor=actor, note=note,
        source_command=_source_command(args, "promotion promote-relation")))

    def human(p: Dict[str, Any]) -> None:
        print(f"status: {p['status']}")
        if p.get("relation"):
            print(f"relation: {p['relation']['id']} "
                  f"[{p['relation']['relation_state']}]")
        print(f"knowledge revision: {p['knowledge_revision']}")

    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# entity commands
# ---------------------------------------------------------------------------
def cmd_entity_list(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_knowledge().list_entities(
        args.workspace, entity_type=getattr(args, "type", None),
        lifecycle_status=getattr(args, "lifecycle", "active") or None,
        origin=getattr(args, "origin", None), limit=args.limit,
        offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for entity in p["entities"]:
            print(f"  {entity['id']}  {entity['entity_type']:18} "
                  f"{entity['canonical_key']}")
        print(f"{p['count']} of {p['total']}")

    out.emit(payload, human)
    return 0, payload


def cmd_entity_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"entity": _knowledge().get_entity(args.workspace,
                                                     args.entity)})
    out.emit(payload, lambda p: print(json.dumps(p["entity"], indent=2,
                                                 default=str)))
    return 0, payload


def cmd_entity_create(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().create_entity(
        args.workspace, entity_type=args.type, canonical_key=args.key,
        display_name=args.name, description=getattr(args, "description",
                                                    "") or "",
        evidence=_evidence_from_args(args), actor=actor, note=note,
        source_command=_source_command(args, "entity create")))
    out.emit(payload, lambda p: print(
        f"created {p['entity']['id']} (revision "
        f"{p['knowledge_revision']})"))
    return 0, payload


def cmd_entity_alias_add(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().add_alias(
        args.workspace, entity_id=args.entity, alias=args.alias,
        alias_type=getattr(args, "type", "manual") or "manual",
        evidence_id=getattr(args, "evidence", None) or "",
        actor=actor, note=note,
        source_command=_source_command(args, "entity alias-add")))
    out.emit(payload, lambda p: print(
        "alias added" if not p.get("deduplicated") else "alias already "
                                                        "present"))
    return 0, payload


def cmd_entity_alias_remove(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().remove_alias(
        args.workspace, entity_id=args.entity, alias=args.alias,
        actor=actor, note=note,
        source_command=_source_command(args, "entity alias-remove")))
    out.emit(payload, lambda p: print(f"removed {p['removed']} alias(es)"))
    return 0, payload


def cmd_entity_merge(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().merge_entities(
        args.workspace, source_entity_id=args.source,
        target_entity_id=args.target, actor=actor, note=note,
        source_command=_source_command(args, "entity merge")))

    def human(p: Dict[str, Any]) -> None:
        print(f"merged {p['source_entity_id']} into "
              f"{p['target_entity_id']}")
        print(f"moved: {p['aliases']} aliases, {p['bindings']} bindings, "
              f"{p['claims']} claims; rewired {p['relations_rewired']} "
              f"relations")
        for collision in p.get("alias_collisions", []):
            print(f"  alias collision kept on source: {collision['alias']}")

    out.emit(payload, human)
    return 0, payload


def cmd_entity_split(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    rewrites = []
    for spec in getattr(args, "rewrite", None) or []:
        relation_id, _, end = str(spec).partition(":")
        rewrites.append({"relation_id": relation_id.strip(),
                         "end": end.strip() or "source"})
    payload = _ok(_knowledge().split_entity(
        args.workspace, source_entity_id=args.source,
        new_entity_type=args.new_type, new_canonical_key=args.new_key,
        new_display_name=getattr(args, "new_name", "") or args.new_key,
        claim_ids=getattr(args, "claim", None) or [],
        binding_ids=getattr(args, "binding", None) or [],
        relation_rewrites=rewrites, actor=actor, note=note,
        source_command=_source_command(args, "entity split")))
    out.emit(payload, lambda p: print(
        f"split -> {p['new_entity']['id']} ({p['moved_claims']} claims, "
        f"{p['moved_bindings']} bindings moved)"))
    return 0, payload


def _cmd_authority(kind: str):
    def handler(args, out) -> Tuple[int, Dict[str, Any]]:
        actor, note = _actor_note(args)
        object_id = getattr(args, kind)
        payload = _ok(_knowledge().set_authority(
            args.workspace, kind=kind, object_id=object_id,
            authority=args.status, actor=actor, note=note,
            source_command=_source_command(args, f"{kind} authority")))
        out.emit(payload, lambda p: print(
            f"{kind} {object_id}: authority = {p['authority_status']}"))
        return 0, payload
    return handler


def _cmd_supersede(kind: str):
    def handler(args, out) -> Tuple[int, Dict[str, Any]]:
        actor, note = _actor_note(args)
        payload = _ok(_knowledge().supersede_object(
            args.workspace, kind=kind, object_id=getattr(args, kind),
            replacement_id=args.by, actor=actor, note=note,
            source_command=_source_command(args, f"{kind} supersede")))
        out.emit(payload, lambda p: print(
            f"{kind} {p['object_id']} superseded by {p['replacement_id']}"))
        return 0, payload
    return handler


def _cmd_withdraw(kind: str):
    def handler(args, out) -> Tuple[int, Dict[str, Any]]:
        actor, note = _actor_note(args)
        payload = _ok(_knowledge().withdraw_object(
            args.workspace, kind=kind, object_id=getattr(args, kind),
            actor=actor, note=note,
            source_command=_source_command(args, f"{kind} withdraw")))
        out.emit(payload, lambda p: print(f"{kind} withdrawn"))
        return 0, payload
    return handler


# ---------------------------------------------------------------------------
# claim commands
# ---------------------------------------------------------------------------
def cmd_claim_list(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_knowledge().list_claims(
        args.workspace, entity_id=getattr(args, "entity", None),
        claim_type=getattr(args, "type", None),
        lifecycle_status=getattr(args, "lifecycle", "active") or None,
        limit=args.limit, offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for claim in p["claims"]:
            print(f"  {claim['id']}  {claim['claim_type']:22} "
                  f"{claim['statement'][:70]}")
        print(f"{p['count']} of {p['total']}")

    out.emit(payload, human)
    return 0, payload


def cmd_claim_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"claim": _knowledge().get_claim(args.workspace,
                                                   args.claim)})
    out.emit(payload, lambda p: print(json.dumps(p["claim"], indent=2,
                                                 default=str)))
    return 0, payload


def cmd_claim_create(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().create_claim(
        args.workspace, entity_id=args.entity, claim_type=args.type,
        statement=args.statement, evidence=_evidence_from_args(args),
        actor=actor, note=note,
        source_command=_source_command(args, "claim create")))
    out.emit(payload, lambda p: print(
        ("deduplicated to " if p.get("deduplicated") else "created ")
        + p["claim"]["id"]))
    return 0, payload


# ---------------------------------------------------------------------------
# relation commands
# ---------------------------------------------------------------------------
def cmd_relation_list(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_knowledge().list_relations(
        args.workspace, entity_id=getattr(args, "entity", None),
        relation_type=getattr(args, "type", None),
        relation_state=getattr(args, "state", None),
        lifecycle_status=getattr(args, "lifecycle", "active") or None,
        limit=args.limit, offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for rel in p["relations"]:
            print(f"  {rel['id']}  {rel['source_entity_id']} "
                  f"-{rel['relation_type']}-> {rel['target_entity_id']} "
                  f"[{rel['relation_state']}]")
        print(f"{p['count']} relation(s)")

    out.emit(payload, human)
    return 0, payload


def cmd_relation_show(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"relation": _knowledge().get_relation(args.workspace,
                                                         args.relation)})
    out.emit(payload, lambda p: print(json.dumps(p["relation"], indent=2,
                                                 default=str)))
    return 0, payload


def cmd_relation_create(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().create_relation(
        args.workspace, source_entity_id=args.source,
        target_entity_id=args.target, relation_type=args.type,
        relation_state=getattr(args, "state", "confirmed") or "confirmed",
        confidence=getattr(args, "confidence", "medium") or "medium",
        evidence=_evidence_from_args(args), actor=actor, note=note,
        source_command=_source_command(args, "relation create")))
    out.emit(payload, lambda p: print(
        ("deduplicated to " if p.get("deduplicated") else "created ")
        + p["relation"]["id"]))
    return 0, payload


def cmd_relation_reject(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().reject_relation(
        args.workspace, relation_id=args.relation, actor=actor, note=note,
        source_command=_source_command(args, "relation reject")))
    out.emit(payload, lambda p: print("relation rejected"))
    return 0, payload


def cmd_relation_restore(args, out) -> Tuple[int, Dict[str, Any]]:
    actor, note = _actor_note(args)
    payload = _ok(_knowledge().restore_relation(
        args.workspace, relation_id=args.relation, actor=actor, note=note,
        source_command=_source_command(args, "relation restore")))
    out.emit(payload, lambda p: print(
        f"relation restored to {p['relation_state']}"))
    return 0, payload


# ---------------------------------------------------------------------------
# knowledge history commands (added to the EXISTING knowledge group)
# ---------------------------------------------------------------------------
def cmd_knowledge_revisions(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_knowledge().list_knowledge_revisions(
        args.workspace, limit=args.limit, offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for revision in p["revisions"]:
            print(f"  #{revision['revision_number']:>4}  "
                  f"{revision['action']:22} {revision['created_at']}  "
                  f"{revision['summary'][:50]}")
        print(f"current: {p['knowledge_revision']}")

    out.emit(payload, human)
    return 0, payload


def cmd_knowledge_revision(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok({"revision": _knowledge().get_knowledge_revision(
        args.workspace, args.number)})
    out.emit(payload, lambda p: print(json.dumps(p["revision"], indent=2,
                                                 default=str)))
    return 0, payload


def cmd_knowledge_decisions(args, out) -> Tuple[int, Dict[str, Any]]:
    payload = _ok(_knowledge().list_decisions(
        args.workspace, target_kind=getattr(args, "target_kind", None),
        target_id=getattr(args, "target", None),
        decision_type=getattr(args, "type", None), limit=args.limit,
        offset=args.offset))

    def human(p: Dict[str, Any]) -> None:
        for decision in p["decisions"]:
            print(f"  {decision['id']}  {decision['decision_type']:22} "
                  f"{decision['target_kind']}:{decision['target_id']}  "
                  f"actor={decision['actor'] or '-'}")
        print(f"{p['count']} decision(s)")

    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# bundle commands
# ---------------------------------------------------------------------------
def cmd_bundle_export(args, out) -> Tuple[int, Dict[str, Any]]:
    from .knowledge.bundle import export_bundle
    from .runtime import get_runtime
    get_runtime()          # bootstrap: migrations must have run
    current_only = not bool(getattr(args, "include_history", False))
    manifest = export_bundle(
        args.workspace, args.output, current_only=current_only,
        knowledge_revision=getattr(args, "knowledge_revision", None),
        include_traceability=bool(getattr(args, "include_traceability",
                                          False)),
        include_conflicts=bool(getattr(args, "include_conflicts", False)),
        generated_at=getattr(args, "generated_at", None) or "")
    payload = _ok({"manifest": manifest, "output": args.output})

    def human(p: Dict[str, Any]) -> None:
        m = p["manifest"]
        print(f"bundle {m['bundleSchemaVersion']} -> {p['output']}")
        print(f"knowledge revision: {m['knowledgeRevision']}")
        for key, value in sorted(m["counts"].items()):
            print(f"  {key:20} {value}")
        for warning in m.get("warnings", []):
            print(f"  warning: {warning}")

    out.emit(payload, human)
    return 0, payload


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def _ws(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", required=True, help="workspace id")


def _actor_note_flags(parser: argparse.ArgumentParser,
                      require: bool = True) -> None:
    parser.add_argument("--actor", required=require,
                        help="who is making this governance decision "
                             "(recorded verbatim, never inferred)")
    parser.add_argument("--note", required=require,
                        help="why (bounded; recorded on the Human Decision)")


def _paging(parser: argparse.ArgumentParser, limit: int = 100) -> None:
    parser.add_argument("--limit", type=int, default=limit, metavar="N")
    parser.add_argument("--offset", type=int, default=0, metavar="N")


def register(sub: argparse._SubParsersAction,
             common: argparse.ArgumentParser,
             knowledge_sub: argparse._SubParsersAction) -> None:
    """Attach the Phase 5 command groups. ``knowledge_sub`` is the EXISTING
    ``knowledge`` group's subparsers — history commands are added beside the
    Phase 3 ``knowledge search`` without touching it."""
    # -- graph -------------------------------------------------------------
    graph = sub.add_parser("graph", parents=[common],
                           help="canonical knowledge-graph queries and "
                                "deterministic projection")
    graph_sub = graph.add_subparsers(dest="graph_command",
                                     metavar="<subcommand>")

    g_stats = graph_sub.add_parser("stats", parents=[common],
                                   help="graph statistics + current "
                                        "knowledge revision")
    _ws(g_stats)
    g_stats.set_defaults(func=cmd_graph_stats)

    g_seed = graph_sub.add_parser(
        "seed", parents=[common],
        help="deterministic graph seed (model-free); `graph seed plan` "
             "for the dry run")
    # NOT required at this level: `graph seed plan` satisfies --workspace on
    # the sub-subparser, and argparse would otherwise demand it twice. The
    # handler enforces it for the bare `graph seed` form.
    g_seed.add_argument("--workspace", help="workspace id")
    g_seed.add_argument("--actor", default="", help="recorded actor")
    g_seed.set_defaults(func=cmd_graph_seed)
    g_seed_sub = g_seed.add_subparsers(dest="seed_command",
                                       metavar="<subcommand>")
    g_seed_plan = g_seed_sub.add_parser("plan", parents=[common],
                                        help="dry-run: what a seed would "
                                             "change (no write)")
    _ws(g_seed_plan)
    g_seed_plan.set_defaults(func=cmd_graph_seed_plan)

    g_sync = graph_sub.add_parser("sync", parents=[common],
                                  help="incremental deterministic sync "
                                       "(unchanged source = no-op)")
    _ws(g_sync)
    g_sync.add_argument("--actor", default="", help="recorded actor")
    g_sync.set_defaults(func=cmd_graph_sync)

    g_reconcile = graph_sub.add_parser("reconcile", parents=[common],
                                       help="graph staleness "
                                            "reconciliation")
    _ws(g_reconcile)
    g_reconcile.add_argument("--actor", default="", help="recorded actor")
    g_reconcile.set_defaults(func=cmd_graph_reconcile)

    g_search = graph_sub.add_parser("search", parents=[common],
                                    help="search canonical entities and "
                                         "claims")
    _ws(g_search)
    g_search.add_argument("--query", required=True)
    g_search.add_argument("--limit", type=int, default=20, metavar="N")
    g_search.add_argument("--include-stale", dest="include_stale",
                          action="store_true")
    g_search.set_defaults(func=cmd_graph_search)

    g_node = graph_sub.add_parser("node", parents=[common],
                                  help="one graph node in the stable read "
                                       "shape")
    _ws(g_node)
    g_node.add_argument("--node", required=True, help="node id (entity/"
                        "claim/asset/revision/segment/evidence)")
    g_node.set_defaults(func=cmd_graph_node)

    g_expand = graph_sub.add_parser("expand", parents=[common],
                                    help="bounded BFS expansion around one "
                                         "entity")
    _ws(g_expand)
    g_expand.add_argument("--node", required=True)
    g_expand.add_argument("--depth", type=int, default=2)
    g_expand.add_argument("--direction", default="both",
                          choices=["outgoing", "incoming", "both"])
    g_expand.add_argument("--types", help="comma-separated relation types")
    g_expand.add_argument("--include-stale", dest="include_stale",
                          action="store_true")
    g_expand.set_defaults(func=cmd_graph_expand)

    g_path = graph_sub.add_parser(
        "path", parents=[common],
        help="bounded shortest-path discovery (generic reachability, not "
             "formal traceability)")
    _ws(g_path)
    g_path.add_argument("--from", required=True, help="source entity id")
    g_path.add_argument("--to", required=True, help="target entity id")
    g_path.add_argument("--max-depth", dest="max_depth", type=int, default=6)
    g_path.add_argument("--direction", default="both",
                        choices=["outgoing", "incoming", "both"])
    g_path.add_argument("--types", help="comma-separated relation types")
    g_path.add_argument("--include-stale", dest="include_stale",
                        action="store_true")
    g_path.set_defaults(func=cmd_graph_path)
    graph.set_defaults(func=None, _parser=graph)

    # -- promotion ---------------------------------------------------------
    promotion = sub.add_parser(
        "promotion", parents=[common],
        help="explicit candidate promotion (review never promotes)")
    promotion_sub = promotion.add_subparsers(dest="promotion_command",
                                             metavar="<subcommand>")

    p_plan = promotion_sub.add_parser("plan", parents=[common],
                                      help="deterministic promotion "
                                           "dry-run (no write)")
    _ws(p_plan)
    p_plan.add_argument("--candidate", required=True,
                        help="semantic candidate id (sc_...)")
    p_plan.set_defaults(func=cmd_promotion_plan)

    p_promote = promotion_sub.add_parser(
        "promote", parents=[common],
        help="promote one confirmed, active, verified candidate")
    _ws(p_promote)
    p_promote.add_argument("--candidate", required=True)
    _actor_note_flags(p_promote)
    p_promote.set_defaults(func=cmd_promotion_promote)

    p_rplan = promotion_sub.add_parser("relation-plan", parents=[common],
                                       help="relation-candidate promotion "
                                            "dry-run")
    _ws(p_rplan)
    p_rplan.add_argument("--relation", required=True,
                         help="relation candidate id (sr_...)")
    p_rplan.set_defaults(func=cmd_promotion_relation_plan)

    p_rpromote = promotion_sub.add_parser(
        "promote-relation", parents=[common],
        help="promote one confirmed relation candidate")
    _ws(p_rpromote)
    p_rpromote.add_argument("--relation", required=True)
    _actor_note_flags(p_rpromote)
    p_rpromote.set_defaults(func=cmd_promotion_promote_relation)
    promotion.set_defaults(func=None, _parser=promotion)

    # -- entity ------------------------------------------------------------
    entity = sub.add_parser("entity", parents=[common],
                            help="canonical engineering entities")
    entity_sub = entity.add_subparsers(dest="entity_command",
                                       metavar="<subcommand>")

    e_list = entity_sub.add_parser("list", parents=[common],
                                   help="list entities (bounded)")
    _ws(e_list)
    e_list.add_argument("--type", help="filter by entity type")
    e_list.add_argument("--lifecycle", default="active",
                        help="lifecycle filter (default active; '' for all)")
    e_list.add_argument("--origin", help="filter by origin")
    _paging(e_list)
    e_list.set_defaults(func=cmd_entity_list)

    e_show = entity_sub.add_parser("show", parents=[common],
                                   help="one entity with aliases, bindings, "
                                        "claims and relations")
    _ws(e_show)
    e_show.add_argument("--entity", required=True)
    e_show.set_defaults(func=cmd_entity_show)

    e_create = entity_sub.add_parser(
        "create", parents=[common],
        help="manual entity (requires evidence, actor and note)")
    _ws(e_create)
    e_create.add_argument("--type", required=True, help="entity type")
    e_create.add_argument("--key", required=True, help="canonical key")
    e_create.add_argument("--name", required=True, help="display name")
    e_create.add_argument("--description", default="")
    e_create.add_argument("--evidence", action="append", metavar="EVID",
                          help="evidence id (repeatable)")
    e_create.add_argument("--evidence-json", dest="evidence_json",
                          help="JSON array of {evidence_id, quote?, role?}")
    _actor_note_flags(e_create)
    e_create.set_defaults(func=cmd_entity_create)

    e_alias_add = entity_sub.add_parser("alias-add", parents=[common],
                                        help="add an alias (collisions are "
                                             "reported, never silent)")
    _ws(e_alias_add)
    e_alias_add.add_argument("--entity", required=True)
    e_alias_add.add_argument("--alias", required=True)
    e_alias_add.add_argument("--type", default="manual", help="alias type")
    e_alias_add.add_argument("--evidence", help="optional evidence id")
    _actor_note_flags(e_alias_add)
    e_alias_add.set_defaults(func=cmd_entity_alias_add)

    e_alias_remove = entity_sub.add_parser("alias-remove", parents=[common],
                                           help="remove an alias "
                                                "(kept auditable)")
    _ws(e_alias_remove)
    e_alias_remove.add_argument("--entity", required=True)
    e_alias_remove.add_argument("--alias", required=True)
    _actor_note_flags(e_alias_remove)
    e_alias_remove.set_defaults(func=cmd_entity_alias_remove)

    e_merge = entity_sub.add_parser("merge", parents=[common],
                                    help="merge source entity into target "
                                         "(source stays addressable)")
    _ws(e_merge)
    e_merge.add_argument("--source", required=True, help="source entity id")
    e_merge.add_argument("--target", required=True, help="target entity id")
    _actor_note_flags(e_merge)
    e_merge.set_defaults(func=cmd_entity_merge)

    e_split = entity_sub.add_parser(
        "split", parents=[common],
        help="explicit split: the caller lists exactly what moves")
    _ws(e_split)
    e_split.add_argument("--source", required=True, help="source entity id")
    e_split.add_argument("--new-type", dest="new_type", required=True)
    e_split.add_argument("--new-key", dest="new_key", required=True)
    e_split.add_argument("--new-name", dest="new_name", default="")
    e_split.add_argument("--claim", action="append", metavar="CLAIM_ID",
                         help="claim id to move (repeatable)")
    e_split.add_argument("--binding", action="append", metavar="BINDING_ID",
                         help="binding id to move (repeatable)")
    e_split.add_argument("--rewrite", action="append",
                         metavar="RELATION_ID:END",
                         help="repoint a relation endpoint "
                              "(END = source|target; repeatable)")
    _actor_note_flags(e_split)
    e_split.set_defaults(func=cmd_entity_split)

    e_authority = entity_sub.add_parser("authority", parents=[common],
                                        help="explicit authority marking")
    _ws(e_authority)
    e_authority.add_argument("--entity", required=True)
    e_authority.add_argument("--status", required=True,
                             help="authoritative / non-authoritative / "
                                  "informational / unknown")
    _actor_note_flags(e_authority)
    e_authority.set_defaults(func=_cmd_authority("entity"))

    e_supersede = entity_sub.add_parser("supersede", parents=[common],
                                        help="mark superseded by a "
                                             "replacement entity")
    _ws(e_supersede)
    e_supersede.add_argument("--entity", required=True)
    e_supersede.add_argument("--by", required=True,
                             help="replacement entity id")
    _actor_note_flags(e_supersede)
    e_supersede.set_defaults(func=_cmd_supersede("entity"))

    e_withdraw = entity_sub.add_parser("withdraw", parents=[common],
                                       help="withdraw (history preserved)")
    _ws(e_withdraw)
    e_withdraw.add_argument("--entity", required=True)
    _actor_note_flags(e_withdraw)
    e_withdraw.set_defaults(func=_cmd_withdraw("entity"))
    entity.set_defaults(func=None, _parser=entity)

    # -- claim -------------------------------------------------------------
    claim = sub.add_parser("claim", parents=[common],
                           help="canonical claims")
    claim_sub = claim.add_subparsers(dest="claim_command",
                                     metavar="<subcommand>")

    c_list = claim_sub.add_parser("list", parents=[common],
                                  help="list claims (bounded)")
    _ws(c_list)
    c_list.add_argument("--entity", help="filter by entity id")
    c_list.add_argument("--type", help="filter by claim type")
    c_list.add_argument("--lifecycle", default="active")
    _paging(c_list)
    c_list.set_defaults(func=cmd_claim_list)

    c_show = claim_sub.add_parser("show", parents=[common],
                                  help="one claim with its evidence")
    _ws(c_show)
    c_show.add_argument("--claim", required=True)
    c_show.set_defaults(func=cmd_claim_show)

    c_create = claim_sub.add_parser(
        "create", parents=[common],
        help="manual claim (evidence required; quotes verified)")
    _ws(c_create)
    c_create.add_argument("--entity", required=True)
    c_create.add_argument("--type", required=True, help="claim type")
    c_create.add_argument("--statement", required=True)
    c_create.add_argument("--evidence", action="append", metavar="EVID")
    c_create.add_argument("--evidence-json", dest="evidence_json")
    _actor_note_flags(c_create)
    c_create.set_defaults(func=cmd_claim_create)

    c_authority = claim_sub.add_parser("authority", parents=[common],
                                       help="explicit authority marking")
    _ws(c_authority)
    c_authority.add_argument("--claim", required=True)
    c_authority.add_argument("--status", required=True)
    _actor_note_flags(c_authority)
    c_authority.set_defaults(func=_cmd_authority("claim"))

    c_supersede = claim_sub.add_parser("supersede", parents=[common],
                                       help="supersede by a replacement "
                                            "claim")
    _ws(c_supersede)
    c_supersede.add_argument("--claim", required=True)
    c_supersede.add_argument("--by", required=True)
    _actor_note_flags(c_supersede)
    c_supersede.set_defaults(func=_cmd_supersede("claim"))

    c_withdraw = claim_sub.add_parser("withdraw", parents=[common],
                                      help="withdraw (history preserved)")
    _ws(c_withdraw)
    c_withdraw.add_argument("--claim", required=True)
    _actor_note_flags(c_withdraw)
    c_withdraw.set_defaults(func=_cmd_withdraw("claim"))
    claim.set_defaults(func=None, _parser=claim)

    # -- relation ----------------------------------------------------------
    relation = sub.add_parser("relation", parents=[common],
                              help="canonical relations")
    relation_sub = relation.add_subparsers(dest="relation_command",
                                           metavar="<subcommand>")

    r_list = relation_sub.add_parser("list", parents=[common],
                                     help="list relations (bounded)")
    _ws(r_list)
    r_list.add_argument("--entity", help="either endpoint")
    r_list.add_argument("--type", help="relation type")
    r_list.add_argument("--state", help="relation state")
    r_list.add_argument("--lifecycle", default="active")
    _paging(r_list)
    r_list.set_defaults(func=cmd_relation_list)

    r_show = relation_sub.add_parser("show", parents=[common],
                                     help="one relation with evidence")
    _ws(r_show)
    r_show.add_argument("--relation", required=True)
    r_show.set_defaults(func=cmd_relation_show)

    r_create = relation_sub.add_parser(
        "create", parents=[common],
        help="manual relation (explicit|confirmed only; evidence required)")
    _ws(r_create)
    r_create.add_argument("--source", required=True)
    r_create.add_argument("--target", required=True)
    r_create.add_argument("--type", required=True)
    r_create.add_argument("--state", default="confirmed",
                          choices=["explicit", "confirmed"])
    r_create.add_argument("--confidence", default="medium")
    r_create.add_argument("--evidence", action="append", metavar="EVID")
    r_create.add_argument("--evidence-json", dest="evidence_json")
    _actor_note_flags(r_create)
    r_create.set_defaults(func=cmd_relation_create)

    r_authority = relation_sub.add_parser("authority", parents=[common],
                                          help="explicit authority marking")
    _ws(r_authority)
    r_authority.add_argument("--relation", required=True)
    r_authority.add_argument("--status", required=True)
    _actor_note_flags(r_authority)
    r_authority.set_defaults(func=_cmd_authority("relation"))

    r_reject = relation_sub.add_parser("reject", parents=[common],
                                       help="reject (kept as governance "
                                            "history)")
    _ws(r_reject)
    r_reject.add_argument("--relation", required=True)
    _actor_note_flags(r_reject)
    r_reject.set_defaults(func=cmd_relation_reject)

    r_restore = relation_sub.add_parser("restore", parents=[common],
                                        help="restore a rejected relation")
    _ws(r_restore)
    r_restore.add_argument("--relation", required=True)
    _actor_note_flags(r_restore)
    r_restore.set_defaults(func=cmd_relation_restore)

    r_supersede = relation_sub.add_parser("supersede", parents=[common],
                                          help="supersede by a replacement "
                                               "relation")
    _ws(r_supersede)
    r_supersede.add_argument("--relation", required=True)
    r_supersede.add_argument("--by", required=True)
    _actor_note_flags(r_supersede)
    r_supersede.set_defaults(func=_cmd_supersede("relation"))

    r_withdraw = relation_sub.add_parser("withdraw", parents=[common],
                                         help="withdraw (history preserved)")
    _ws(r_withdraw)
    r_withdraw.add_argument("--relation", required=True)
    _actor_note_flags(r_withdraw)
    r_withdraw.set_defaults(func=_cmd_withdraw("relation"))
    relation.set_defaults(func=None, _parser=relation)

    # -- knowledge history (beside the existing `knowledge search`) --------
    k_revisions = knowledge_sub.add_parser(
        "revisions", parents=[common],
        help="the workspace's knowledge revision ledger")
    _ws(k_revisions)
    _paging(k_revisions, limit=50)
    k_revisions.set_defaults(func=cmd_knowledge_revisions)

    k_revision = knowledge_sub.add_parser(
        "revision", parents=[common], help="one knowledge revision with "
                                           "its decisions")
    _ws(k_revision)
    k_revision.add_argument("--number", type=int, required=True)
    k_revision.set_defaults(func=cmd_knowledge_revision)

    k_decisions = knowledge_sub.add_parser(
        "decisions", parents=[common],
        help="the immutable human-decision audit")
    _ws(k_decisions)
    k_decisions.add_argument("--target-kind", dest="target_kind")
    k_decisions.add_argument("--target", help="target object id")
    k_decisions.add_argument("--type", help="decision type")
    _paging(k_decisions)
    k_decisions.set_defaults(func=cmd_knowledge_decisions)

    # -- bundle ------------------------------------------------------------
    bundle = sub.add_parser("bundle", parents=[common],
                            help="Knowledge Bundle 2.0 Draft export")
    bundle_sub = bundle.add_subparsers(dest="bundle_command",
                                       metavar="<subcommand>")
    b_export = bundle_sub.add_parser(
        "export", parents=[common],
        help="export the workspace's canonical knowledge as a "
             "2.0.0-draft.2 bundle (separate from .openmind 1.x)")
    _ws(b_export)
    b_export.add_argument("--output", required=True,
                          help="directory to write (e.g. ./.openmind-v2)")
    mode = b_export.add_mutually_exclusive_group()
    mode.add_argument("--current-only", dest="current_only",
                      action="store_true",
                      help="active objects only (the default)")
    mode.add_argument("--include-history", dest="include_history",
                      action="store_true",
                      help="include stale/superseded/withdrawn/merged "
                           "history")
    b_export.add_argument("--knowledge-revision", dest="knowledge_revision",
                          type=int, metavar="N",
                          help="cap records at creation revision N "
                               "(stamp filter; not point-in-time state)")
    b_export.add_argument("--include-traceability",
                          dest="include_traceability", action="store_true",
                          help="add trace policies/runs/paths/steps/gaps/"
                               "coverage snapshots (v2 Phase 6)")
    b_export.add_argument("--include-conflicts", dest="include_conflicts",
                          action="store_true",
                          help="add canonical conflicts with object/"
                               "evidence/decision joins (v2 Phase 6)")
    b_export.add_argument("--generated-at", dest="generated_at",
                          metavar="ISO8601",
                          help="override generatedAt (reproducible builds)")
    b_export.set_defaults(func=cmd_bundle_export)
    bundle.set_defaults(func=None, _parser=bundle)


__all__ = ["register"]
