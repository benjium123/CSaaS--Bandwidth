"""Pure IVR call-flow interpreter (P12 DR-1/DR-2/DR-4).

No DB, no IO. Executors translate actions to carrier commands or room operations.
Flash draft, Fable-reviewed."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Speak:
    text: str


@dataclass(frozen=True)
class GatherDigit:
    max_digits: int
    timeout_seconds: int


@dataclass(frozen=True)
class EvaluateHours:
    business_hours_id: str


@dataclass(frozen=True)
class RingGroup:
    ring_group_id: str


@dataclass(frozen=True)
class Enqueue:
    queue_id: str


@dataclass(frozen=True)
class RecordVoicemail:
    greeting: str


@dataclass(frozen=True)
class Hangup:
    pass


Action = Speak | GatherDigit | EvaluateHours | RingGroup | Enqueue | RecordVoicemail | Hangup


@dataclass(frozen=True)
class StepResult:
    actions: tuple[Action, ...]
    state: dict
    awaiting: str | None
    terminal: str | None


class FlowError(Exception):
    pass


def _enter(flow: dict, node_id: str) -> StepResult:
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict):
        raise FlowError("flow has no nodes dict")

    actions: list[Action] = []
    seen: set[str] = set()
    current = node_id

    while True:
        if current in seen:
            raise FlowError(f"infinite speak loop detected at runtime: {', '.join(seen)}")
        seen.add(current)

        if current not in nodes:
            raise FlowError(f"unknown node '{current}'")
        node = nodes[current]
        if not isinstance(node, dict) or "type" not in node:
            raise FlowError(f"node '{current}' has no type")

        ntype = node["type"]

        if ntype == "speak":
            try:
                text = node["text"]
                nxt = node["next"]
            except KeyError:
                raise FlowError(f"malformed speak node '{current}'") from None
            actions.append(Speak(text=text))
            current = nxt
            continue

        if ntype == "menu":
            try:
                prompt = node["prompt"]
                node["options"]
            except KeyError:
                raise FlowError(f"malformed menu node '{current}'") from None
            actions.append(Speak(text=prompt))
            actions.append(GatherDigit(max_digits=1, timeout_seconds=10))
            return StepResult(tuple(actions), {"node": current, "retries": 0}, "digit", None)

        if ntype == "hours":
            business_hours_id = node.get("business_hours_id")
            if not isinstance(business_hours_id, str):
                raise FlowError(f"malformed hours node '{current}'")
            actions.append(EvaluateHours(business_hours_id=business_hours_id))
            return StepResult(tuple(actions), {"node": current, "retries": 0}, "hours", None)

        if ntype == "ring_group":
            ring_group_id = node.get("ring_group_id")
            if not isinstance(ring_group_id, str):
                raise FlowError(f"malformed ring_group node '{current}'")
            actions.append(RingGroup(ring_group_id=ring_group_id))
            return StepResult(tuple(actions), {"node": current, "retries": 0}, "ring_result", None)

        if ntype == "queue":
            queue_id = node.get("queue_id")
            if not isinstance(queue_id, str):
                raise FlowError(f"malformed queue node '{current}'")
            actions.append(Enqueue(queue_id=queue_id))
            return StepResult(tuple(actions), {"node": current, "retries": 0}, None, "queued")

        if ntype == "voicemail":
            greeting = node.get("greeting")
            if not isinstance(greeting, str):
                raise FlowError(f"malformed voicemail node '{current}'")
            actions.append(RecordVoicemail(greeting=greeting))
            return StepResult(tuple(actions), {"node": current, "retries": 0}, None, "voicemail")

        if ntype == "hangup":
            actions.append(Hangup())
            return StepResult(tuple(actions), {"node": current, "retries": 0}, None, "hangup")

        raise FlowError(f"node '{current}' has unknown type '{ntype}'")


def start(flow: dict) -> StepResult:
    entry = flow.get("entry")
    if entry is None:
        raise FlowError("flow has no entry")
    return _enter(flow, entry)


def step(flow: dict, state: dict, event: dict) -> StepResult:
    if not isinstance(state, dict) or "node" not in state:
        raise FlowError("state must contain 'node'")

    node_id = state["node"]
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict) or node_id not in nodes:
        raise FlowError(f"unknown node '{node_id}'")

    node = nodes[node_id]
    if not isinstance(node, dict) or "type" not in node:
        raise FlowError(f"node '{node_id}' has no type")

    ntype = node["type"]
    retries = state.get("retries", 0)
    event_kind = event.get("kind") if isinstance(event, dict) else None

    if ntype == "menu":
        if event_kind not in ("digit", "timeout"):
            raise FlowError(f"unexpected event '{event_kind}' for menu node")

        try:
            prompt = node["prompt"]
            options = node["options"]
            invalid_retries = node.get("invalid_retries", 2)
        except (KeyError, TypeError):
            raise FlowError(f"malformed menu node '{node_id}'") from None

        timeout_node = node.get("timeout_node")
        invalid_node = node.get("invalid_node", timeout_node)

        if event_kind == "digit":
            digit = event.get("digit")
            if digit in options:
                return _enter(flow, options[digit])

            retries += 1
            if retries <= invalid_retries:
                actions = (Speak(text=prompt), GatherDigit(max_digits=1, timeout_seconds=10))
                return StepResult(actions, {"node": node_id, "retries": retries}, "digit", None)

            target = invalid_node
            if target is None:
                return StepResult(
                    (Hangup(),), {"node": node_id, "retries": retries}, None, "hangup"
                )
            return _enter(flow, target)

        if timeout_node is not None:
            return _enter(flow, timeout_node)

        target = invalid_node
        if target is not None:
            return _enter(flow, target)
        return StepResult((Hangup(),), {"node": node_id, "retries": retries}, None, "hangup")

    if ntype == "hours":
        if event_kind != "hours":
            raise FlowError(f"unexpected event '{event_kind}' for hours node")
        result = event.get("result")
        if result not in ("open", "closed", "holiday"):
            raise FlowError(f"invalid hours result '{result}'")
        target = node.get(result)
        if not isinstance(target, str):
            raise FlowError(f"malformed hours node '{node_id}'")
        return _enter(flow, target)

    if ntype == "ring_group":
        if event_kind != "ring_result":
            raise FlowError(f"unexpected event '{event_kind}' for ring_group node")
        result = event.get("result")
        if result == "answered":
            return StepResult((), {"node": node_id, "retries": retries}, None, "connected")
        if result == "no_answer":
            no_answer = node.get("no_answer")
            if no_answer is not None:
                return _enter(flow, no_answer)
            return StepResult((Hangup(),), {"node": node_id, "retries": retries}, None, "hangup")
        raise FlowError(f"invalid ring_result '{result}'")

    raise FlowError(f"unexpected event '{event_kind}' for {ntype} node")


def validate_flow(flow: dict) -> list[str]:
    errors: list[str] = []
    if not isinstance(flow, dict):
        return ["flow definition must be a dict"]

    nodes = flow.get("nodes")
    if not isinstance(nodes, dict):
        errors.append("'nodes' must be a dict")
        if "entry" not in flow:
            errors.append("missing 'entry'")
        return errors

    if "entry" not in flow:
        errors.append("missing 'entry'")

    entry = flow.get("entry")
    node_ids = set(nodes.keys())
    if entry not in node_ids:
        errors.append(f"entry node '{entry}' not found")

    references: dict[str, set[str]] = {}
    known_types = {"menu", "hours", "ring_group", "queue", "voicemail", "speak", "hangup"}

    for node_id, node in nodes.items():
        if not isinstance(node, dict):
            errors.append(f"node '{node_id}' is not a dict")
            references[node_id] = set()
            continue

        ntype = node.get("type")
        if ntype is None:
            errors.append(f"node '{node_id}' missing 'type'")
            references[node_id] = set()
            continue
        if ntype not in known_types:
            errors.append(f"node '{node_id}' has unknown type '{ntype}'")
            references[node_id] = set()
            continue

        refs: set[str] = set()

        if ntype == "menu":
            for field in ("prompt", "options"):
                if field not in node or node[field] is None:
                    errors.append(f"node '{node_id}' missing required field '{field}'")
            options = node.get("options")
            if not isinstance(options, dict):
                if options is not None:
                    errors.append(f"node '{node_id}' options must be a dict")
            else:
                for target in options.values():
                    if isinstance(target, str):
                        refs.add(target)
                    else:
                        errors.append(f"node '{node_id}' option target must be a string")

            timeout_node = node.get("timeout_node")
            if timeout_node is not None:
                if isinstance(timeout_node, str):
                    refs.add(timeout_node)
                else:
                    errors.append(f"node '{node_id}' timeout_node must be a string")

            invalid_node = node.get("invalid_node", timeout_node)
            if invalid_node is not None:
                if isinstance(invalid_node, str):
                    refs.add(invalid_node)
                else:
                    errors.append(f"node '{node_id}' invalid_node must be a string")

        elif ntype == "hours":
            for field in ("business_hours_id", "open", "closed", "holiday"):
                if field not in node or node[field] is None:
                    errors.append(f"node '{node_id}' missing required field '{field}'")
            for field in ("open", "closed", "holiday"):
                val = node.get(field)
                if isinstance(val, str):
                    refs.add(val)
                elif val is not None:
                    errors.append(f"node '{node_id}' {field} must be a string")

        elif ntype == "ring_group":
            if "ring_group_id" not in node or node["ring_group_id"] is None:
                errors.append(f"node '{node_id}' missing required field 'ring_group_id'")
            no_answer = node.get("no_answer")
            if no_answer is not None:
                if isinstance(no_answer, str):
                    refs.add(no_answer)
                else:
                    errors.append(f"node '{node_id}' no_answer must be a string")

        elif ntype == "queue":
            if "queue_id" not in node or node["queue_id"] is None:
                errors.append(f"node '{node_id}' missing required field 'queue_id'")

        elif ntype == "voicemail":
            if "greeting" not in node or node["greeting"] is None:
                errors.append(f"node '{node_id}' missing required field 'greeting'")

        elif ntype == "speak":
            for field in ("text", "next"):
                if field not in node or node[field] is None:
                    errors.append(f"node '{node_id}' missing required field '{field}'")
            nxt = node.get("next")
            if isinstance(nxt, str):
                refs.add(nxt)
            elif nxt is not None:
                errors.append(f"node '{node_id}' next must be a string")

        references[node_id] = refs

    for node_id, refs in references.items():
        for target in refs:
            if target not in node_ids:
                errors.append(f"node '{node_id}' references missing node '{target}'")

    if entry in node_ids:
        reachable: set[str] = set()
        stack = [entry]
        while stack:
            current = stack.pop()
            if current in reachable:
                continue
            reachable.add(current)
            for target in references.get(current, set()):
                if target in node_ids and target not in reachable:
                    stack.append(target)

        for node_id in node_ids:
            if node_id not in reachable:
                errors.append(f"node '{node_id}' is unreachable")

    speak_next: dict[str, str] = {}
    for node_id, node in nodes.items():
        if isinstance(node, dict) and node.get("type") == "speak":
            nxt = node.get("next")
            if isinstance(nxt, str) and nxt in node_ids:
                speak_next[node_id] = nxt

    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(speak_next, WHITE)

    for start_node in speak_next:
        if color.get(start_node, WHITE) != WHITE:
            continue
        current = start_node
        stack: list[str] = []
        while current in speak_next and color.get(current, WHITE) == WHITE:
            color[current] = GRAY
            stack.append(current)
            current = speak_next[current]
        if current in speak_next and color.get(current) == GRAY:
            idx = stack.index(current)
            cycle = stack[idx:] + [current]
            errors.append("infinite speak loop through nodes " + ", ".join(cycle))
        for node_id in stack:
            color[node_id] = BLACK

    return errors
