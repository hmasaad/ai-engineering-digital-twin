"""Import-graph helpers: adjacency, blast radius, and simple cycles."""

from __future__ import annotations

from collections import defaultdict, deque


def adjacency(edges: list[dict], reverse: bool = False) -> dict[str, list[str]]:
    graph: dict[str, list[str]] = defaultdict(list)
    for edge in edges:
        src, dst = edge["source"], edge["target"]
        if reverse:
            src, dst = dst, src
        if dst not in graph[src]:
            graph[src].append(dst)
    return graph


def reachable(starts: list[str], graph: dict[str, list[str]], max_depth: int) -> dict[str, int]:
    dist = {node: 0 for node in starts}
    queue: deque[str] = deque(starts)
    while queue:
        node = queue.popleft()
        if dist[node] >= max_depth:
            continue
        for nxt in graph.get(node, []):
            if nxt not in dist:
                dist[nxt] = dist[node] + 1
                queue.append(nxt)
    return dist


def _canon(cycle: list[str]) -> tuple[str, ...]:
    size = len(cycle)
    pivot = min(range(size), key=lambda i: cycle[i])
    forward = tuple(cycle[pivot:] + cycle[:pivot])
    backward_src = list(reversed(cycle))
    pivot_b = min(range(size), key=lambda i: backward_src[i])
    backward = tuple(backward_src[pivot_b:] + backward_src[:pivot_b])
    return min(forward, backward)


def find_cycles(edges: list[dict], limit: int = 12) -> list[list[str]]:
    """Return simple cycles of length 2..8. Search is capped for large graphs."""
    graph: dict[str, list[str]] = defaultdict(list)
    nodes: set[str] = set()
    for edge in edges:
        graph[edge["source"]].append(edge["target"])
        nodes.add(edge["source"])
        nodes.add(edge["target"])

    found: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    steps = 0
    for start in sorted(nodes):
        stack = [(start, [start])]
        while stack:
            steps += 1
            if steps > 5000 or len(found) >= limit:
                return found
            node, path = stack.pop()
            visiting = set(path)
            for nxt in graph.get(node, []):
                if nxt == start and len(path) >= 2:
                    key = _canon(path)
                    if key not in seen:
                        seen.add(key)
                        found.append(list(key))
                        if len(found) >= limit:
                            return found
                elif nxt not in visiting and len(path) < 8:
                    stack.append((nxt, path + [nxt]))
    return found
