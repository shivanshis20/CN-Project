"""
topology.py - the server's "map" of the network.

It must match the Mininet topology your teammate builds:

    h1 --+                    +-- h3
         s1 ==== (10 Mbps) === s2
    h2 --+                    +-- h4

Host-to-switch links: 100 Mbps.  s1-s2 link: 10 Mbps (the bottleneck).
If your team changes the topology, change ONLY this file.
"""

from collections import deque

# Each entry: (node_a, node_b) -> capacity in Mbps
LINKS = {
    ("h1", "s1"): 100,
    ("h2", "s1"): 100,
    ("h3", "s2"): 100,
    ("h4", "s2"): 100,
    ("s1", "s2"): 10,
}


# IP address of each host inside Mininet (used by the MSG command to reach a host)
HOST_IPS = {"h1": "10.0.0.1", "h2": "10.0.0.2", "h3": "10.0.0.3", "h4": "10.0.0.4"}


def link_key(a, b):
    """A link is the same whether we write (s1, s2) or (s2, s1).
    Sorting the two names gives every link ONE canonical name."""
    return tuple(sorted((a, b)))


# Capacity lookup using canonical link names
CAPACITY = {link_key(a, b): cap for (a, b), cap in LINKS.items()}

# Graph as an adjacency list: node -> list of neighbouring nodes
NEIGHBOURS = {}
for (a, b) in LINKS:
    NEIGHBOURS.setdefault(a, []).append(b)
    NEIGHBOURS.setdefault(b, []).append(a)

HOSTS = sorted(n for n in NEIGHBOURS if n.startswith("h"))


def find_path(src, dst):
    """Shortest path (fewest hops) from src to dst using Breadth-First Search.
    Returns a list of nodes like ['h1', 's1', 's2', 'h3'], or None."""
    if src not in NEIGHBOURS or dst not in NEIGHBOURS:
        return None

    queue = deque([[src]])          # queue of partial paths
    visited = {src}

    while queue:
        path = queue.popleft()
        node = path[-1]
        if node == dst:
            return path
        for nxt in NEIGHBOURS[node]:
            if nxt not in visited:
                visited.add(nxt)
                queue.append(path + [nxt])
    return None


def path_links(path):
    """Turn a node path into the list of links it uses.
    ['h1','s1','s2','h3'] -> [('h1','s1'), ('s1','s2'), ('h3','s2')]"""
    return [link_key(path[i], path[i + 1]) for i in range(len(path) - 1)]
