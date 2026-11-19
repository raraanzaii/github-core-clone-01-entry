#!/usr/bin/env python3
"""github-core-clone-01: a GitHub-compatible git forge in one Python process.

Serves, from an in-memory store and the standard library only:
  - git smart HTTP (protocol v0 and v2 upload-pack, v0 receive-pack), with the
    git object model implemented here (no git binary anywhere),
  - the GitHub REST v3 subset the suite drives (repos, contents, branches,
    commits, labels, issues, comments, pull requests and merges),
  - a GraphQL v4 endpoint answering the documents gh / github-mcp-server send,
  - an MCP endpoint (streamable HTTP, revision 2026-07-28 plus the legacy
    initialize handshake) whose tools act on the same store as REST.

Lifecycle (the make.fun trial contract): bind an ephemeral port, POST
{"port": N} to $MAKEFUN_ENV_URL/register, poll $MAKEFUN_ENV_URL/done until it
answers {"done": true}, then exit 0. Without MAKEFUN_ENV_URL the server runs
standalone (local development against the kit's sdk/run.ts).
"""

import base64
import hashlib
import http.client
import io
import json
import os
import sys
import threading
import time
import urllib.request
import urllib.error
import zlib
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# --------------------------------------------------------------------------
# Scenario identities (the contest's one fixed seeded scenario, wire.ts):
# the bearer token names the viewer; repositories belong to that login.
# --------------------------------------------------------------------------

VIEWER = {
    "login": "adaiversen",
    "id": 1000,
    "name": "ada iversen",
    "email": "adaiversen@example.invalid",
    "type": "User",
}
ZERO_OID = "0" * 40
DEFAULT_BRANCH = "main"
SERVER_NAME = "makefun-forge"
SERVER_VERSION = "1.0.0"
INSTALLED_VERSION = "makefun-forge 1.0.0 (github-core-clone-01)"


def parse_trial_now() -> int:
    """MAKEFUN_TRIAL_NOW is the only 'now' a response may carry."""
    raw = os.environ.get("MAKEFUN_TRIAL_NOW", "").strip()
    if raw:
        try:
            value = float(raw)
            if value > 1e12:  # epoch milliseconds
                return int(value // 1000)
            return int(value)
        except ValueError:
            pass
        try:
            from datetime import datetime, timezone

            text = raw.replace("Z", "+00:00")
            return int(datetime.fromisoformat(text).astimezone(timezone.utc).timestamp())
        except Exception:
            pass
    return 1735689600  # 2025-01-01T00:00:00Z, the scenario epoch


BASE_TS = parse_trial_now()


_ISO_CACHE = {}


def iso_of(ts: int) -> str:
    # Timestamps repeat heavily across responses (one issue's created_at is
    # rendered on every read); strftime is slow enough to matter at 2,000
    # reads, so memoize. Distinct values are bounded by the number of
    # timestamped entities in a trial.
    text = _ISO_CACHE.get(ts)
    if text is None:
        text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
        _ISO_CACHE[ts] = text
    return text


class Clock:
    """Deterministic timestamps: the trial's opening time plus a counter."""

    def __init__(self) -> None:
        self._seq = 0
        self._lock = threading.Lock()

    def next_ts(self) -> int:
        with self._lock:
            self._seq += 1
            return BASE_TS + self._seq

    def next_iso(self) -> str:
        return iso_of(self.next_ts())


CLOCK = Clock()

# --------------------------------------------------------------------------
# Git object primitives. Objects are stored as oid -> (type, raw content),
# where the oid is SHA-1 over "<type> <len>\0" + content, exactly like git.
# --------------------------------------------------------------------------


def hash_object(otype: str, data: bytes) -> str:
    header = ("%s %d\0" % (otype, len(data))).encode("ascii")
    return hashlib.sha1(header + data).hexdigest()


def parse_commit(data: bytes):
    """Return (tree_oid, [parent_oids], author_line, committer_line, message_bytes)."""
    head, _, message = data.partition(b"\n\n")
    tree = None
    parents = []
    author = ""
    committer = ""
    for line in head.decode("utf-8", "replace").split("\n"):
        if line.startswith("tree "):
            tree = line[5:].strip()
        elif line.startswith("parent "):
            parents.append(line[7:].strip())
        elif line.startswith("author "):
            author = line[7:]
        elif line.startswith("committer "):
            committer = line[10:]
    return tree, parents, author, committer, message


def build_commit(tree: str, parents, author: str, committer: str, ts: int, message: bytes) -> bytes:
    lines = ["tree %s" % tree]
    for parent in parents:
        lines.append("parent %s" % parent)
    lines.append("author %s %d +0000" % (author, ts))
    lines.append("committer %s %d +0000" % (committer, ts))
    return ("\n".join(lines) + "\n\n").encode("utf-8") + message


def parse_tree(data: bytes):
    """Return [(mode_str, name, oid_hex)] in stored order."""
    entries = []
    pos = 0
    while pos < len(data):
        sp = data.index(b" ", pos)
        mode = data[pos:sp].decode("ascii")
        nul = data.index(b"\0", sp)
        name = data[sp + 1 : nul].decode("utf-8", "surrogateescape")
        oid = data[nul + 1 : nul + 21].hex()
        entries.append((mode, name, oid))
        pos = nul + 21
    return entries


def tree_sort_key(entry):
    mode, name, _oid = entry
    # git orders tree entries with directories as if the name ended in '/'
    return name + ("/" if mode == "40000" else "")


# --------------------------------------------------------------------------
# Repository / forge state
# --------------------------------------------------------------------------


class Repo:
    def __init__(self, repo_id: int, owner: str, name: str, private: bool, description: str):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.private = bool(private)
        self.description = description or ""
        self.default_branch = DEFAULT_BRANCH
        self.created_ts = CLOCK.next_ts()
        self.updated_ts = self.created_ts
        self.objects = {}  # oid -> (type, bytes)
        self.refs = {}  # "refs/heads/main" -> oid
        self.labels = {}  # name -> {"name","color","description"}
        self.issues = {}  # number -> issue dict
        self.pulls = {}  # number -> pull dict
        self.next_number = 1  # issues and pull requests share one sequence
        self.commit_cache = {}
        self.lock = threading.RLock()

    # -- object store ------------------------------------------------------
    def store(self, otype: str, data: bytes) -> str:
        oid = hash_object(otype, data)
        if oid not in self.objects:
            self.objects[oid] = (otype, data)
        return oid

    def get(self, oid: str):
        return self.objects.get(oid)

    def has(self, oid: str) -> bool:
        return oid in self.objects

    def commit_info(self, oid: str):
        cached = self.commit_cache.get(oid)
        if cached is not None:
            return cached
        entry = self.objects.get(oid)
        if entry is None or entry[0] != "commit":
            return None
        info = parse_commit(entry[1])
        self.commit_cache[oid] = info
        return info

    def flatten_tree(self, tree_oid: str, prefix: str = ""):
        """path -> (mode, oid) for every blob under a tree (recursive)."""
        out = {}
        entry = self.objects.get(tree_oid)
        if entry is None or entry[0] != "tree":
            return out
        for mode, name, oid in parse_tree(entry[1]):
            path = prefix + name
            if mode == "40000":
                out.update(self.flatten_tree(oid, path + "/"))
            else:
                out[path] = (mode, oid)
        return out

    def tree_of_commit(self, commit_oid: str):
        info = self.commit_info(commit_oid)
        return info[0] if info else None

    def flat_at_commit(self, commit_oid: str):
        tree = self.tree_of_commit(commit_oid)
        if tree is None:
            return {}
        return self.flatten_tree(tree)

    def build_tree_from_flat(self, flat) -> str:
        """Store nested trees for a path -> (mode, oid) map; return root tree oid."""

        def build(prefix: str) -> str:
            entries = []
            subdirs = set()
            for path, (mode, oid) in flat.items():
                if not path.startswith(prefix):
                    continue
                rest = path[len(prefix) :]
                if "/" in rest:
                    subdirs.add(rest.split("/", 1)[0])
                else:
                    entries.append((mode, rest, oid))
            for dirname in subdirs:
                sub_oid = build(prefix + dirname + "/")
                entries.append(("40000", dirname, sub_oid))
            entries.sort(key=tree_sort_key)
            data = b"".join(
                mode.encode("ascii") + b" " + name.encode("utf-8", "surrogateescape") + b"\0" + bytes.fromhex(oid)
                for mode, name, oid in entries
            )
            return self.store("tree", data)

        return build("")

    def ancestors(self, tip: str):
        """All commit oids reachable from tip (including tip), BFS order."""
        seen = []
        have = set()
        queue = [tip]
        while queue:
            oid = queue.pop(0)
            if oid in have or not self.has(oid):
                continue
            info = self.commit_info(oid)
            if info is None:
                continue
            have.add(oid)
            seen.append(oid)
            queue.extend(info[1])
        return seen

    def is_ancestor(self, maybe_ancestor: str, tip: str) -> bool:
        if maybe_ancestor == tip:
            return True
        seen = set()
        queue = [tip]
        while queue:
            oid = queue.pop()
            if oid == maybe_ancestor:
                return True
            if oid in seen:
                continue
            seen.add(oid)
            info = self.commit_info(oid)
            if info is not None:
                queue.extend(info[1])
        return False


class Forge:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.repos = {}  # (owner, name) -> Repo
        self.next_repo_id = 1
        self.next_comment_id = 1
        self.request_seq = 0

    def next_request_id(self) -> str:
        with self.lock:
            self.request_seq += 1
            return "req-%012x" % self.request_seq

    def create_repo(self, owner: str, name: str, private: bool, description: str):
        with self.lock:
            key = (owner, name)
            if key in self.repos:
                return None
            repo = Repo(self.next_repo_id, owner, name, private, description)
            self.next_repo_id += 1
            self.repos[key] = repo
            return repo

    def get_repo(self, owner: str, name: str):
        # A repository belongs to the login the token names: any other owner
        # is simply "not found" (404, never 403).
        if owner != VIEWER["login"]:
            return None
        return self.repos.get((owner, name))

    def list_repos(self):
        return [self.repos[key] for key in sorted(self.repos, key=lambda k: self.repos[k].id)]


FORGE = Forge()

# --------------------------------------------------------------------------
# Pack files: read (with delta resolution) and write (plain objects only).
# --------------------------------------------------------------------------

OBJ_TYPES = {1: "commit", 2: "tree", 3: "blob", 4: "tag"}
TYPE_CODES = {"commit": 1, "tree": 2, "blob": 3, "tag": 4}


def apply_delta(base: bytes, delta: bytes) -> bytes:
    pos = 0

    def read_varint() -> int:
        nonlocal pos
        result = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return result

    src_size = read_varint()
    if src_size != len(base):
        raise ValueError("delta base size mismatch")
    _dst_size = read_varint()
    out = bytearray()
    while pos < len(delta):
        op = delta[pos]
        pos += 1
        if op & 0x80:
            offset = 0
            size = 0
            for i in range(4):
                if op & (1 << i):
                    offset |= delta[pos] << (8 * i)
                    pos += 1
            for i in range(3):
                if op & (0x10 << i):
                    size |= delta[pos] << (8 * i)
                    pos += 1
            if size == 0:
                size = 0x10000
            out += base[offset : offset + size]
        elif op:
            out += delta[pos : pos + op]
            pos += op
        else:
            raise ValueError("delta opcode 0")
    return bytes(out)


def read_pack(data: bytes, repo: Repo):
    """Unpack a pack into a fresh {oid: (type, bytes)}; verify the trailer.

    Bases for deltas may come from the pack itself or from the repo store.
    Raises ValueError on any corruption.
    """
    if len(data) < 12 + 20 or data[:4] != b"PACK":
        raise ValueError("not a pack")
    version = int.from_bytes(data[4:8], "big")
    if version not in (2, 3):
        raise ValueError("bad pack version")
    count = int.from_bytes(data[8:12], "big")
    trailer = data[-20:]
    if hashlib.sha1(data[:-20]).digest() != trailer:
        raise ValueError("pack checksum mismatch")
    body = data[:-20]
    pos = 12
    raw = {}  # offset -> (type, data-or-delta-info)
    order = []
    for _ in range(count):
        start = pos
        byte = body[pos]
        pos += 1
        otype_code = (byte >> 4) & 0x7
        size = byte & 0x0F
        shift = 4
        while byte & 0x80:
            byte = body[pos]
            pos += 1
            size |= (byte & 0x7F) << shift
            shift += 7
        if otype_code == 6:  # OFS_DELTA
            byte = body[pos]
            pos += 1
            offset = byte & 0x7F
            while byte & 0x80:
                byte = body[pos]
                pos += 1
                offset = ((offset + 1) << 7) | (byte & 0x7F)
            base_offset = start - offset
            decomp = zlib.decompressobj()
            delta = decomp.decompress(body[pos:])
            pos = len(body) - len(decomp.unused_data)
            raw[start] = ("ofs", base_offset, delta)
        elif otype_code == 7:  # REF_DELTA
            base_oid = body[pos : pos + 20].hex()
            pos += 20
            decomp = zlib.decompressobj()
            delta = decomp.decompress(body[pos:])
            pos = len(body) - len(decomp.unused_data)
            raw[start] = ("ref", base_oid, delta)
        else:
            if otype_code not in OBJ_TYPES:
                raise ValueError("bad pack object type")
            decomp = zlib.decompressobj()
            payload = decomp.decompress(body[pos:])
            pos = len(body) - len(decomp.unused_data)
            if len(payload) != size:
                raise ValueError("pack object size mismatch")
            raw[start] = ("obj", OBJ_TYPES[otype_code], payload)
        order.append(start)
    if pos != len(body):
        raise ValueError("pack has trailing garbage")

    resolved = {}

    def resolve(offset: int):
        if offset in resolved:
            return resolved[offset]
        entry = raw[offset]
        if entry[0] == "obj":
            result = (entry[1], entry[2])
        elif entry[0] == "ofs":
            base_type, base_data = resolve(entry[1])
            result = (base_type, apply_delta(base_data, entry[2]))
        else:  # ref delta: base may be in this pack (by oid) or in the repo
            base_oid = entry[1]
            base = None
            for other_offset, other in raw.items():
                if other[0] == "obj" and hash_object(other[1], other[2]) == base_oid:
                    base = resolve(other_offset)
                    break
            if base is None:
                existing = repo.get(base_oid)
                if existing is None:
                    raise ValueError("delta base missing")
                base = existing
            result = (base[0], apply_delta(base[1], entry[2]))
        resolved[offset] = result
        return result

    objects = {}
    for offset in order:
        otype, payload = resolve(offset)
        objects[hash_object(otype, payload)] = (otype, payload)
    if len(objects) != count:
        raise ValueError("pack object count mismatch")
    return objects


def write_pack(objects) -> bytes:
    """A version-2 pack of plain (non-delta) objects: [(oid, type, data)]."""
    out = bytearray()
    out += b"PACK" + (2).to_bytes(4, "big") + len(objects).to_bytes(4, "big")
    for _oid, otype, data in objects:
        code = TYPE_CODES[otype]
        size = len(data)
        first = (code << 4) | (size & 0x0F)
        size >>= 4
        if size:
            first |= 0x80
        out.append(first)
        while size:
            byte = size & 0x7F
            size >>= 7
            if size:
                byte |= 0x80
            out.append(byte)
        out += zlib.compress(data)
    out += hashlib.sha1(bytes(out)).digest()
    return bytes(out)


EMPTY_PACK = write_pack([])


# --------------------------------------------------------------------------
# pkt-line framing
# --------------------------------------------------------------------------

PKT_FLUSH = b"0000"
PKT_DELIM = b"0001"
PKT_RESPONSE_END = b"0002"
MAX_PKT_PAYLOAD = 65516


def pkt_line(payload: bytes) -> bytes:
    return ("%04x" % (len(payload) + 4)).encode("ascii") + payload


def pkt_text(text: str) -> bytes:
    return pkt_line(text.encode("utf-8"))


class PktReader:
    """Iterate a request body as (kind, payload): kind in data/flush/delim/end."""

    def __init__(self, body: bytes, pos: int = 0):
        self.body = body
        self.pos = pos

    def next(self):
        if self.pos + 4 > len(self.body):
            return ("eof", b"")
        try:
            length = int(self.body[self.pos : self.pos + 4], 16)
        except ValueError:
            raise ValueError("bad pkt-line length")
        if length == 0:
            self.pos += 4
            return ("flush", b"")
        if length == 1:
            self.pos += 4
            return ("delim", b"")
        if length == 2:
            self.pos += 4
            return ("end", b"")
        if length < 4 or self.pos + length > len(self.body):
            raise ValueError("pkt-line overruns body")
        payload = self.body[self.pos + 4 : self.pos + length]
        self.pos += length
        return ("data", payload)


def sideband_packets(pack: bytes) -> bytes:
    """Frame pack bytes as band-1 sideband pkt-lines (each <= 65520 total)."""
    out = bytearray()
    chunk = 65500
    for i in range(0, len(pack), chunk):
        out += pkt_line(b"\x01" + pack[i : i + chunk])
    if not pack:
        out += pkt_line(b"\x01")
    return bytes(out)


# --------------------------------------------------------------------------
# Git smart HTTP
# --------------------------------------------------------------------------

UPLOAD_CAPS_V0 = (
    "side-band-64k ofs-delta shallow no-progress agent=%s/%s symref=HEAD:refs/heads/%s"
    % (SERVER_NAME, SERVER_VERSION, DEFAULT_BRANCH)
)
RECEIVE_CAPS_V0 = "report-status delete-refs side-band-64k ofs-delta agent=%s/%s" % (
    SERVER_NAME,
    SERVER_VERSION,
)


def sorted_refs(repo: Repo):
    return sorted(repo.refs.items())


def peeled_target(repo: Repo, oid: str):
    """Follow an annotated tag to the commit it names; None for non-tags."""
    entry = repo.get(oid)
    if entry is None or entry[0] != "tag":
        return None
    for line in entry[1].decode("utf-8", "replace").split("\n"):
        if line.startswith("object "):
            target = line[7:].strip()
            target_entry = repo.get(target)
            if target_entry is not None and target_entry[0] == "tag":
                return peeled_target(repo, target)
            return target
    return None


def advertise_v0(repo: Repo, service: str) -> bytes:
    caps = UPLOAD_CAPS_V0 if service == "git-upload-pack" else RECEIVE_CAPS_V0
    lines = []
    refs = sorted_refs(repo)
    if service == "git-upload-pack":
        head_ref = "refs/heads/%s" % repo.default_branch
        head_oid = repo.refs.get(head_ref, ZERO_OID)
        lines.append("%s HEAD\0%s" % (head_oid, caps))
        first_done = True
    else:
        first_done = False
    for name, oid in refs:
        if not first_done:
            lines.append("%s %s\0%s" % (oid, name, caps))
            first_done = True
        else:
            lines.append("%s %s" % (oid, name))
        peeled = peeled_target(repo, oid)
        if peeled is not None:
            lines.append("%s %s^{}" % (peeled, name))
    if not first_done:
        # Empty repository for receive-pack: the capabilities pseudo-ref.
        lines.append("%s capabilities^{}\0%s" % (ZERO_OID, caps))
    body = pkt_text("# service=%s\n" % service) + PKT_FLUSH
    body += b"".join(pkt_text(line + "\n") for line in lines) + PKT_FLUSH
    return body


_ADVERTISE_V2 = None


def advertise_v2() -> bytes:
    # The v2 capability advertisement is a constant byte string; build once.
    global _ADVERTISE_V2
    if _ADVERTISE_V2 is not None:
        return _ADVERTISE_V2
    result = _advertise_v2_uncached()
    _ADVERTISE_V2 = result
    return result


def _advertise_v2_uncached() -> bytes:
    caps = [
        "version 2",
        "agent=%s/%s" % (SERVER_NAME, SERVER_VERSION),
        "ls-refs=unborn",
        "fetch=shallow",
        "object-format=sha1",
    ]
    return b"".join(pkt_text(line + "\n") for line in caps) + PKT_FLUSH


def ls_refs_response(repo: Repo, args) -> bytes:
    peel = "peel" in args
    symrefs = "symrefs" in args
    unborn_ok = "unborn" in args
    prefixes = [a[len("ref-prefix ") :] for a in args if a.startswith("ref-prefix ")]

    def wanted(name: str) -> bool:
        return not prefixes or any(name.startswith(p) for p in prefixes)

    lines = []
    head_ref = "refs/heads/%s" % repo.default_branch
    if wanted("HEAD"):
        head_oid = repo.refs.get(head_ref)
        if head_oid is None:
            if unborn_ok:
                line = "unborn HEAD"
            else:
                line = "%s HEAD" % ZERO_OID
            if symrefs:
                line += " symref-target:%s" % head_ref
            lines.append(line)
        else:
            line = "%s HEAD" % head_oid
            if symrefs:
                line += " symref-target:%s" % head_ref
            lines.append(line)
    for name, oid in sorted_refs(repo):
        if not wanted(name):
            continue
        line = "%s %s" % (oid, name)
        if peel:
            peeled = peeled_target(repo, oid)
            if peeled is not None:
                line += " peeled:%s" % peeled
        lines.append(line)
    return b"".join(pkt_text(line + "\n") for line in lines) + PKT_FLUSH


def commit_closure(repo: Repo, tips, stop_at_shallow=frozenset()):
    """All objects (any type) reachable from commit/tag tips.

    Commit traversal does not descend past commits in stop_at_shallow (the
    client's declared shallow boundary: it holds those commits but not their
    parents). Returns (object_oid_set, commit_oid_set).
    """
    objects = set()
    commits = set()
    queue = list(tips)
    while queue:
        oid = queue.pop()
        if oid in objects:
            continue
        entry = repo.get(oid)
        if entry is None:
            continue
        objects.add(oid)
        otype, data = entry
        if otype == "commit":
            commits.add(oid)
            info = repo.commit_info(oid)
            if info is None:
                continue
            tree, parents, _a, _c, _m = info
            queue.append(tree)
            if oid not in stop_at_shallow:
                queue.extend(parents)
        elif otype == "tree":
            for _mode, _name, child in parse_tree(data):
                queue.append(child)
        elif otype == "tag":
            for line in data.decode("utf-8", "replace").split("\n"):
                if line.startswith("object "):
                    queue.append(line[7:].strip())
                    break
    return objects, commits


def compute_fetch(repo: Repo, wants, haves, client_shallow, depth):
    """Decide what a fetch sends: (pack_objects, shallow_lines, common_haves).

    pack_objects is [(oid, type, data)] sorted for a stable pack layout.
    """
    client_shallow = set(client_shallow)
    # What the client already holds: full closure of its haves, without
    # descending past the shallow boundary it declares.
    have_objects, have_commits = commit_closure(repo, haves, client_shallow)

    # Distance (in commits) of every commit from the nearest want.
    dist = {}
    queue = []
    extra_objects = set()  # non-commit wants (annotated tags) ride along
    for want in wants:
        entry = repo.get(want)
        if entry is None:
            continue
        if entry[0] == "commit":
            if want not in dist:
                dist[want] = 1
                queue.append(want)
        elif entry[0] == "tag":
            extra_objects.add(want)
            target = peeled_target(repo, want)
            if target is not None and repo.commit_info(target) is not None and target not in dist:
                dist[target] = 1
                queue.append(target)
        else:
            extra_objects.add(want)
    head = 0
    while head < len(queue):
        oid = queue[head]
        head += 1
        info = repo.commit_info(oid)
        if info is None:
            continue
        # With a depth limit, commits past the limit are discarded below
        # anyway; don't walk the whole ancestry just to throw it away (a
        # depth-1 clone of a 200-commit history used to visit all 200).
        if depth is not None and dist[oid] >= depth:
            continue
        for parent in info[1]:
            if parent not in dist:
                dist[parent] = dist[oid] + 1
                queue.append(parent)
    if depth is not None:
        include_commits = {oid for oid, d in dist.items() if d <= depth}
    else:
        include_commits = set(dist)
    include_objects = set(extra_objects)
    trees = []
    for oid in include_commits:
        include_objects.add(oid)
        info = repo.commit_info(oid)
        if info is not None:
            trees.append(info[0])
    if trees:
        tree_objects, _ = commit_closure(repo, trees)
        include_objects |= tree_objects
    send = include_objects - have_objects
    pack_objects = []
    for oid in sorted(send):
        entry = repo.get(oid)
        if entry is not None:
            pack_objects.append((oid, entry[0], entry[1]))

    # The shallow boundary after this fetch: an included (or already-held
    # shallow) commit whose parent is not available to the client.
    available_commits = include_commits | have_commits
    boundary = set()
    for oid in include_commits | client_shallow:
        info = repo.commit_info(oid)
        if info is None:
            continue
        if any(parent not in available_commits and repo.has(parent) for parent in info[1]):
            boundary.add(oid)
    shallow_lines = []
    for oid in sorted(boundary):
        shallow_lines.append("shallow %s" % oid)
    for oid in sorted(client_shallow - boundary):
        if oid in available_commits:
            shallow_lines.append("unshallow %s" % oid)
    common = [have for have in haves if repo.has(have)]
    return pack_objects, shallow_lines, common


def fetch_response_v2(repo: Repo, args) -> bytes:
    wants, haves, shallow, depth = [], [], [], None
    for arg in args:
        if arg.startswith("want "):
            wants.append(arg[5:].split(" ")[0])
        elif arg.startswith("have "):
            haves.append(arg[5:].strip())
        elif arg.startswith("shallow "):
            shallow.append(arg[8:].strip())
        elif arg.startswith("deepen "):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                depth = None
    pack_objects, shallow_lines, common = compute_fetch(repo, wants, haves, shallow, depth)
    out = bytearray()
    if haves:
        # The acknowledgments section only exists when the client sent haves
        # to negotiate over (real git's v2 reader expects the packfile section
        # first on a have-less clone; the suite's reader accepts either).
        out += pkt_text("acknowledgments\n")
        if common:
            for oid in common:
                out += pkt_text("ACK %s\n" % oid)
            out += pkt_text("ready\n")
        else:
            out += pkt_text("NAK\n")
        out += PKT_DELIM
    if shallow_lines:
        out += pkt_text("shallow-info\n")
        for line in shallow_lines:
            out += pkt_text(line + "\n")
        out += PKT_DELIM
    out += pkt_text("packfile\n")
    out += sideband_packets(write_pack(pack_objects))
    out += PKT_FLUSH
    return bytes(out)


def fetch_response_v0(repo: Repo, want_lines, have_lines) -> bytes:
    wants, shallow, depth = [], [], None
    haves = []
    for arg in want_lines:
        if arg.startswith("want "):
            wants.append(arg[5:].split(" ")[0])
        elif arg.startswith("shallow "):
            shallow.append(arg[8:].strip())
        elif arg.startswith("deepen "):
            try:
                depth = int(arg[7:].strip())
            except ValueError:
                depth = None
    for arg in have_lines:
        if arg.startswith("have "):
            haves.append(arg[5:].strip())
    pack_objects, shallow_lines, common = compute_fetch(repo, wants, haves, shallow, depth)
    out = bytearray()
    for line in shallow_lines:
        out += pkt_text(line + "\n")
    if common:
        out += pkt_text("ACK %s\n" % common[-1])
    else:
        out += pkt_text("NAK\n")
    if pack_objects:
        out += sideband_packets(write_pack(pack_objects))
    out += PKT_FLUSH
    return bytes(out)


def handle_upload_pack(repo: Repo, body: bytes, protocol_v2: bool) -> bytes:
    if protocol_v2:
        reader = PktReader(body)
        command = None
        args = []
        phase = "caps"
        while True:
            kind, payload = reader.next()
            if kind in ("flush", "eof", "end"):
                break
            if kind == "delim":
                phase = "args"
                continue
            line = payload.decode("utf-8", "replace").rstrip("\n")
            if phase == "caps":
                if line.startswith("command="):
                    command = line[len("command=") :]
            else:
                args.append(line)
        if command == "ls-refs":
            return ls_refs_response(repo, args)
        if command == "fetch":
            return fetch_response_v2(repo, args)
        raise ValueError("unsupported v2 command %r" % command)
    # v0: wants section, flush, haves section, flush.
    reader = PktReader(body)
    sections = []
    current = []
    while True:
        kind, payload = reader.next()
        if kind == "eof":
            break
        if kind == "flush":
            sections.append(current)
            current = []
            if len(sections) >= 2:
                break
            continue
        if kind == "data":
            current.append(payload.decode("utf-8", "replace").rstrip("\n"))
    want_lines = sections[0] if sections else []
    have_lines = sections[1] if len(sections) > 1 else current
    return fetch_response_v0(repo, want_lines, have_lines)


def handle_receive_pack(repo: Repo, body: bytes):
    """Apply a push; return the report-status byte stream."""
    reader = PktReader(body)
    commands = []  # (old, new, ref)
    capabilities = ""
    while True:
        kind, payload = reader.next()
        if kind in ("flush", "eof"):
            break
        if kind != "data":
            continue
        line = payload.decode("utf-8", "replace")
        if "\0" in line:
            line, capabilities = line.split("\0", 1)
            capabilities = capabilities.strip()
        parts = line.rstrip("\n").split(" ")
        if len(parts) == 3:
            commands.append((parts[0], parts[1], parts[2]))
    pack_bytes = body[reader.pos :]
    unpack_error = None
    new_objects = {}
    if pack_bytes:
        try:
            new_objects = read_pack(pack_bytes, repo)
        except ValueError as err:
            unpack_error = str(err)
    sideband = "side-band-64k" in capabilities.split(" ")

    with repo.lock:
        if unpack_error is None:
            for oid, (otype, data) in new_objects.items():
                if oid not in repo.objects:
                    repo.objects[oid] = (otype, data)
        # Validate every command against the pre-push ref state, then apply.
        pre_refs = dict(repo.refs)
        outcomes = []
        for old, new, ref in commands:
            if unpack_error is not None:
                outcomes.append((ref, False, "unpack failed"))
                continue
            if not ref.startswith("refs/"):
                outcomes.append((ref, False, "invalid ref"))
                continue
            actual = pre_refs.get(ref, ZERO_OID)
            if new == ZERO_OID:  # delete
                if actual == ZERO_OID:
                    outcomes.append((ref, False, "reference does not exist"))
                elif actual != old:
                    outcomes.append((ref, False, "stale info"))
                else:
                    outcomes.append((ref, True, ""))
                continue
            entry = repo.get(new)
            if entry is None:
                outcomes.append((ref, False, "missing object"))
                continue
            if actual != old:
                outcomes.append((ref, False, "stale info"))
                continue
            if old != ZERO_OID and ref.startswith("refs/heads/"):
                # Fast-forward only on branches.
                if entry[0] != "commit":
                    outcomes.append((ref, False, "invalid object"))
                    continue
                if not repo.is_ancestor(old, new):
                    outcomes.append((ref, False, "non-fast-forward"))
                    continue
            outcomes.append((ref, True, ""))
        for (old, new, ref), (_r, ok, _reason) in zip(commands, outcomes):
            if not ok:
                continue
            if new == ZERO_OID:
                repo.refs.pop(ref, None)
            else:
                repo.refs[ref] = new
        lines = []
        if unpack_error is not None:
            lines.append("unpack %s" % unpack_error)
        else:
            lines.append("unpack ok")
        for ref, ok, reason in outcomes:
            if ok:
                lines.append("ok %s" % ref)
            else:
                lines.append("ng %s %s" % (ref, reason))
        report = b"".join(pkt_text(line + "\n") for line in lines)
        if sideband:
            framed = bytearray()
            for i in range(0, len(report), 65500):
                framed += pkt_line(b"\x01" + report[i : i + 65500])
            # The report stream inside band 1 is itself flush-terminated,
            # then the transport flush ends the response.
            framed += pkt_line(b"\x01" + PKT_FLUSH)
            return bytes(framed) + PKT_FLUSH
        return report + PKT_FLUSH


# --------------------------------------------------------------------------
# Shared helpers: node ids, merge engine, diffs
# --------------------------------------------------------------------------


_NODE_ID_CACHE = {}


def node_id(type_name: str, key: str) -> str:
    cache_key = (type_name, key)
    value = _NODE_ID_CACHE.get(cache_key)
    if value is None:
        value = base64.b64encode(("%s:%s" % (type_name, key)).encode("utf-8")).decode("ascii")
        _NODE_ID_CACHE[cache_key] = value
    return value


def parse_node_id(value: str):
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return None
    if ":" not in raw:
        return None
    type_name, key = raw.split(":", 1)
    return type_name, key


def viewer_ident() -> str:
    return "%s <%s>" % (VIEWER["name"], VIEWER["email"])


def find_merge_base(repo: Repo, a: str, b: str):
    if a == b:
        return a
    b_ancestors = set(repo.ancestors(b))
    queue = [a]
    seen = set()
    while queue:
        oid = queue.pop(0)
        if oid in seen:
            continue
        seen.add(oid)
        if oid in b_ancestors:
            return oid
        info = repo.commit_info(oid)
        if info is not None:
            queue.extend(info[1])
    return None


def three_way_merge(repo: Repo, base_tip: str, head_tip: str):
    """Return (merged_flat, conflict_bool)."""
    mb = find_merge_base(repo, base_tip, head_tip)
    mb_flat = repo.flat_at_commit(mb) if mb else {}
    base_flat = repo.flat_at_commit(base_tip)
    head_flat = repo.flat_at_commit(head_tip)
    merged = {}
    conflict = False
    for path in set(mb_flat) | set(base_flat) | set(head_flat):
        b = mb_flat.get(path)
        s = base_flat.get(path)
        h = head_flat.get(path)
        if h == s:
            result = s
        elif b == s:
            result = h
        elif b == h:
            result = s
        else:
            conflict = True
            continue
        if result is not None:
            merged[path] = result
    return merged, conflict


def pull_is_mergeable(repo: Repo, pull) -> bool:
    base_tip = repo.refs.get("refs/heads/%s" % pull["base"])
    head_tip = repo.refs.get("refs/heads/%s" % pull["head"])
    if base_tip is None or head_tip is None:
        return False
    _flat, conflict = three_way_merge(repo, base_tip, head_tip)
    return not conflict


def merge_pull(repo: Repo, pull, method: str = "squash"):
    """Merge a pull request. Returns (http_status, merge_commit_sha | None)."""
    if pull["state"] == "closed":
        return (405, None)
    base_ref = "refs/heads/%s" % pull["base"]
    head_ref = "refs/heads/%s" % pull["head"]
    base_tip = repo.refs.get(base_ref)
    head_tip = repo.refs.get(head_ref)
    if base_tip is None or head_tip is None:
        return (405, None)
    merged_flat, conflict = three_way_merge(repo, base_tip, head_tip)
    if conflict:
        return (405, None)
    tree = repo.build_tree_from_flat(merged_flat)
    ts = CLOCK.next_ts()
    if method == "merge":
        parents = [base_tip, head_tip]
        message = "Merge pull request #%d from %s\n" % (pull["number"], pull["head"])
    else:
        parents = [base_tip]
        message = "%s (#%d)\n" % (pull["title"], pull["number"])
    commit = repo.store("commit", build_commit(tree, parents, viewer_ident(), viewer_ident(), ts, message.encode("utf-8")))
    repo.refs[base_ref] = commit
    pull["state"] = "closed"
    pull["merged"] = True
    pull["merge_commit_sha"] = commit
    pull["merged_ts"] = ts
    pull["updated_ts"] = ts
    return (200, commit)


def line_diff_counts(old: bytes, new: bytes):
    from collections import Counter

    old_lines = Counter(old.decode("utf-8", "replace").splitlines())
    new_lines = Counter(new.decode("utf-8", "replace").splitlines())
    additions = sum((new_lines - old_lines).values())
    deletions = sum((old_lines - new_lines).values())
    return additions, deletions


def pull_files(repo: Repo, pull):
    base_tip = repo.refs.get("refs/heads/%s" % pull["base"])
    head_tip = repo.refs.get("refs/heads/%s" % pull["head"])
    if base_tip is None or head_tip is None:
        return []
    mb = find_merge_base(repo, base_tip, head_tip)
    base_flat = repo.flat_at_commit(mb) if mb else repo.flat_at_commit(base_tip)
    head_flat = repo.flat_at_commit(head_tip)
    files = []
    for path in sorted(set(base_flat) | set(head_flat)):
        before = base_flat.get(path)
        after = head_flat.get(path)
        if before == after:
            continue
        if before is None:
            status = "added"
        elif after is None:
            status = "removed"
        else:
            status = "modified"
        old_data = repo.get(before[1])[1] if before else b""
        new_data = repo.get(after[1])[1] if after else b""
        additions, deletions = line_diff_counts(old_data, new_data)
        files.append(
            {
                "filename": path,
                "status": status,
                "additions": additions,
                "deletions": deletions,
                "changes": additions + deletions,
                "sha": after[1] if after else before[1],
            }
        )
    return files


# --------------------------------------------------------------------------
# REST v3
# --------------------------------------------------------------------------


_USER_JSON_CACHE = {}


def user_json(base: str):
    # The viewer document is constant for a given base URL and is embedded in
    # nearly every response; build it once per base and hand out copies.
    cached = _USER_JSON_CACHE.get(base)
    if cached is None:
        cached = {
            "login": VIEWER["login"],
            "id": VIEWER["id"],
            "node_id": node_id("User", str(VIEWER["id"])),
            "avatar_url": "",
            "gravatar_id": "",
            "url": "%s/api/v3/users/%s" % (base, VIEWER["login"]),
            "html_url": "%s/%s" % (base, VIEWER["login"]),
            "type": "User",
            "site_admin": False,
            "name": VIEWER["name"],
            "email": VIEWER["email"],
        }
        _USER_JSON_CACHE[base] = cached
    return dict(cached)


def repo_json(repo: Repo, base: str):
    open_count = sum(1 for i in repo.issues.values() if i["state"] == "open")
    open_count += sum(1 for p in repo.pulls.values() if p["state"] == "open")
    return {
        "id": repo.id,
        "node_id": node_id("Repository", str(repo.id)),
        "name": repo.name,
        "full_name": "%s/%s" % (repo.owner, repo.name),
        "owner": user_json(base),
        "private": repo.private,
        "html_url": "%s/%s/%s" % (base, repo.owner, repo.name),
        "description": repo.description,
        "fork": False,
        "url": "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name),
        "git_url": "git://github.local/%s/%s.git" % (repo.owner, repo.name),
        "ssh_url": "git@github.local:%s/%s.git" % (repo.owner, repo.name),
        "clone_url": "%s/%s/%s.git" % (base, repo.owner, repo.name),
        "created_at": iso_of(repo.created_ts),
        "updated_at": iso_of(repo.updated_ts),
        "pushed_at": iso_of(repo.updated_ts),
        "size": 0,
        "stargazers_count": 0,
        "watchers_count": 0,
        "language": None,
        "has_issues": True,
        "has_projects": True,
        "has_downloads": True,
        "has_wiki": True,
        "forks_count": 0,
        "open_issues_count": open_count,
        "archived": False,
        "disabled": False,
        "visibility": "private" if repo.private else "public",
        "default_branch": repo.default_branch,
        "permissions": {"admin": True, "maintain": True, "push": True, "triage": True, "pull": True},
    }


def label_json(label):
    return {
        "id": label["id"],
        "node_id": node_id("Label", str(label["id"])),
        "name": label["name"],
        "color": label["color"],
        "description": label["description"],
        "default": False,
    }


def issue_json(repo: Repo, issue, base: str):
    return {
        "id": repo.id * 100000 + issue["number"],
        "node_id": node_id("Issue", "%d:%d" % (repo.id, issue["number"])),
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "locked": False,
        "labels": [label_json(repo.labels[name]) for name in issue["labels"] if name in repo.labels],
        "user": user_json(base),
        "assignees": [],
        "comments": len(issue["comments"]),
        "created_at": iso_of(issue["created_ts"]),
        "updated_at": iso_of(issue["updated_ts"]),
        "closed_at": iso_of(issue["closed_ts"]) if issue.get("closed_ts") else None,
        "url": "%s/api/v3/repos/%s/%s/issues/%d" % (base, repo.owner, repo.name, issue["number"]),
        "html_url": "%s/%s/%s/issues/%d" % (base, repo.owner, repo.name, issue["number"]),
        "repository_url": "%s/api/v3/repos/%s/%s" % (base, repo.owner, repo.name),
    }


def comment_json(repo: Repo, issue, comment, base: str):
    return {
        "id": comment["id"],
        "node_id": node_id("IssueComment", "%d:%d:%d" % (repo.id, issue["number"], comment["id"])),
        "body": comment["body"],
        "user": user_json(base),
        "created_at": iso_of(comment["created_ts"]),
        "updated_at": iso_of(comment["updated_ts"]),
        "url": "%s/api/v3/repos/%s/%s/issues/comments/%d" % (base, repo.owner, repo.name, comment["id"]),
        "html_url": "%s/%s/%s/issues/%d#issuecomment-%d" % (base, repo.owner, repo.name, issue["number"], comment["id"]),
        "issue_url": "%s/api/v3/repos/%s/%s/issues/%d" % (base, repo.owner, repo.name, issue["number"]),
    }


def pull_json(repo: Repo, pull, base: str):
    head_sha = repo.refs.get("refs/heads/%s" % pull["head"], pull.get("head_sha"))
    base_sha = repo.refs.get("refs/heads/%s" % pull["base"], pull.get("base_sha"))
    repo_view = repo_json(repo, base)
    return {
        "id": repo.id * 100000 + pull["number"],
        "node_id": node_id("PullRequest", "%d:%d" % (repo.id, pull["number"])),
        "number": pull["number"],
        "state": pull["state"],
        "title": pull["title"],
        "body": pull["body"],
        "locked": False,
        "user": user_json(base),
        "head": {
            "label": "%s:%s" % (repo.owner, pull["head"]),
            "ref": pull["head"],
            "sha": head_sha,
            "user": user_json(base),
            "repo": repo_view,
        },
        "base": {
            "label": "%s:%s" % (repo.owner, pull["base"]),
            "ref": pull["base"],
            "sha": base_sha,
            "user": user_json(base),
            "repo": repo_view,
        },
        "merged": pull["merged"],
        "merged_at": iso_of(pull["merged_ts"]) if pull.get("merged_ts") else None,
        "merge_commit_sha": pull.get("merge_commit_sha"),
        "mergeable": None if pull["state"] == "closed" else pull_is_mergeable(repo, pull),
        "draft": False,
        "comments": 0,
        "commits": 0,
        "additions": 0,
        "deletions": 0,
        "changed_files": 0,
        "created_at": iso_of(pull["created_ts"]),
        "updated_at": iso_of(pull["updated_ts"]),
        "closed_at": iso_of(pull["merged_ts"]) if pull.get("merged_ts") else None,
        "url": "%s/api/v3/repos/%s/%s/pulls/%d" % (base, repo.owner, repo.name, pull["number"]),
        "html_url": "%s/%s/%s/pull/%d" % (base, repo.owner, repo.name, pull["number"]),
        "diff_url": "%s/%s/%s/pull/%d.diff" % (base, repo.owner, repo.name, pull["number"]),
        "patch_url": "%s/%s/%s/pull/%d.patch" % (base, repo.owner, repo.name, pull["number"]),
    }


def commit_json(repo: Repo, oid: str, base: str):
    info = repo.commit_info(oid)
    if info is None:
        return {"sha": oid}
    tree, parents, author, committer, message = info

    def person(line: str):
        name, _, rest = line.partition(" <")
        email, _, tail = rest.partition("> ")
        ts = 0
        try:
            ts = int(tail.split(" ")[0])
        except (ValueError, IndexError):
            ts = 0
        return {"name": name, "email": email, "date": iso_of(ts) if ts else iso_of(BASE_TS)}

    return {
        "sha": oid,
        "node_id": node_id("Commit", oid),
        "commit": {
            "author": person(author),
            "committer": person(committer),
            "message": message.decode("utf-8", "replace"),
            "tree": {"sha": tree},
            "comment_count": 0,
        },
        "url": "%s/api/v3/repos/%s/%s/commits/%s" % (base, repo.owner, repo.name, oid),
        "html_url": "%s/%s/%s/commit/%s" % (base, repo.owner, repo.name, oid),
        "parents": [{"sha": parent} for parent in parents],
    }


def not_found(message: str = "Not Found"):
    return 404, {"message": message}


def validation_error(message: str):
    return 422, {"message": "Validation Failed: %s" % message, "errors": [{"message": message}]}


def paginate(items, query, page_url):
    """Slice a list by page/per_page; return (page_items, link_header|None)."""
    try:
        per_page = int(query.get("per_page", ["30"])[0])
    except ValueError:
        per_page = 30
    per_page = max(1, min(100, per_page))
    try:
        page = int(query.get("page", ["1"])[0])
    except ValueError:
        page = 1
    page = max(1, page)
    start = (page - 1) * per_page
    sliced = items[start : start + per_page]
    link = None
    if start + per_page < len(items):
        last_page = (len(items) + per_page - 1) // per_page
        sep = "&" if "?" in page_url else "?"
        link = '<%s%spage=%d&per_page=%d>; rel="next", <%s%spage=%d&per_page=%d>; rel="last"' % (
            page_url,
            sep,
            page + 1,
            per_page,
            page_url,
            sep,
            last_page,
            per_page,
        )
    return sliced, link


def commit_on_branch(repo: Repo, branch: str, changes, message: str, ts: int):
    """Apply {path: bytes|None} changes on a branch; return the new commit oid.

    A missing branch is created from the default branch's tip (or from the
    empty tree when the repository has no commits at all).
    """
    refname = "refs/heads/%s" % branch
    tip = repo.refs.get(refname)
    if tip is None:
        default_tip = repo.refs.get("refs/heads/%s" % repo.default_branch)
        tip = default_tip
    flat = repo.flat_at_commit(tip) if tip else {}
    for path, content in changes.items():
        if content is None:
            flat.pop(path, None)
        else:
            blob_oid = repo.store("blob", content)
            flat[path] = ("100644", blob_oid)
    tree = repo.build_tree_from_flat(flat)
    parents = [tip] if tip else []
    commit = repo.store(
        "commit", build_commit(tree, parents, viewer_ident(), viewer_ident(), ts, message.encode("utf-8"))
    )
    repo.refs[refname] = commit
    repo.updated_ts = ts
    return commit


class RestError(Exception):
    def __init__(self, status: int, payload):
        super().__init__(status)
        self.status = status
        self.payload = payload


def rest_route(method: str, segments, query, body, base: str, raw_path: str):
    """Handle one /api/v3 request; return (status, payload|None, headers)."""
    headers = {"X-OAuth-Scopes": "repo, read:org"}
    if segments == [] or segments == [""]:
        if method == "GET":
            root = {
                "current_user_url": "%s/api/v3/user" % base,
                "rate_limit_url": "%s/api/v3/rate_limit" % base,
                "repository_url": "%s/api/v3/repos/{owner}/{repo}" % base,
                "current_user_repositories_url": "%s/api/v3/user/repos" % base,
            }
            return 200, root, headers
        return (*not_found(), headers)
    if segments == ["meta"]:
        if method == "GET":
            return 200, {"installed_version": INSTALLED_VERSION, "verifiable_password_authentication": False}, headers
        return (*not_found(), headers)
    if segments == ["rate_limit"]:
        return 200, {"rate": {"limit": 5000, "remaining": 5000, "reset": BASE_TS + 3600, "used": 0}}, headers
    if segments == ["user"]:
        if method == "GET":
            return 200, user_json(base), headers
        return (*not_found(), headers)
    if segments == ["user", "repos"]:
        if method == "GET":
            repos = [repo_json(repo, base) for repo in FORGE.list_repos()]
            page_items, link = paginate(repos, query, "%s/api/v3/user/repos" % base)
            if link:
                headers["Link"] = link
            return 200, page_items, headers
        if method == "POST":
            data = body or {}
            name = data.get("name")
            if not isinstance(name, str) or not name:
                return (*validation_error("name is required"), headers)
            with FORGE.lock:
                repo = FORGE.create_repo(VIEWER["login"], name, bool(data.get("private", False)), data.get("description") or "")
                if repo is None:
                    return 422, {"message": "Repository creation failed: name already exists on this account"}, headers
                if data.get("default_branch"):
                    repo.default_branch = str(data["default_branch"])
            return 201, repo_json(repo, base), headers
        return (*not_found(), headers)
    if len(segments) >= 3 and segments[0] == "repos":
        owner, name = segments[1], segments[2]
        repo = FORGE.get_repo(owner, name)
        if repo is None:
            return (*not_found(), headers)
        rest = segments[3:]
        with repo.lock:
            return repo_route(repo, method, rest, query, body, base, headers)
    return (*not_found(), headers)


def repo_route(repo: Repo, method: str, rest, query, body, base: str, headers):
    if rest == []:
        if method == "GET":
            return 200, repo_json(repo, base), headers
        if method == "PATCH":
            data = body or {}
            if "description" in data:
                repo.description = data["description"] or ""
            if "private" in data:
                repo.private = bool(data["private"])
            if "name" in data and isinstance(data["name"], str) and data["name"]:
                with FORGE.lock:
                    FORGE.repos.pop((repo.owner, repo.name), None)
                    repo.name = data["name"]
                    FORGE.repos[(repo.owner, repo.name)] = repo
            if "default_branch" in data and data["default_branch"]:
                repo.default_branch = str(data["default_branch"])
            repo.updated_ts = CLOCK.next_ts()
            return 200, repo_json(repo, base), headers
        if method == "DELETE":
            with FORGE.lock:
                FORGE.repos.pop((repo.owner, repo.name), None)
            return 204, None, headers
        return (*not_found(), headers)

    if rest == ["branches"]:
        if method == "GET":
            branches = []
            for refname, oid in sorted(repo.refs.items()):
                if refname.startswith("refs/heads/"):
                    branches.append({"name": refname[len("refs/heads/") :], "commit": {"sha": oid}, "protected": False})
            return 200, branches, headers
        return (*not_found(), headers)
    if len(rest) >= 2 and rest[0] == "branches":
        branch = "/".join(rest[1:])
        oid = repo.refs.get("refs/heads/%s" % branch)
        if oid is None:
            return (*not_found(), headers)
        if method == "GET":
            return 200, {"name": branch, "commit": commit_json(repo, oid, base), "protected": False}, headers
        return (*not_found(), headers)

    if rest == ["git", "refs"]:
        if method == "POST":
            data = body or {}
            ref = data.get("ref")
            sha = data.get("sha")
            if not isinstance(ref, str) or not ref.startswith("refs/"):
                return (*validation_error("ref is required"), headers)
            if not isinstance(sha, str) or repo.commit_info(sha) is None:
                return (*validation_error("sha is not a commit in this repository"), headers)
            if ref in repo.refs:
                return (*validation_error("Reference already exists"), headers)
            repo.refs[ref] = sha
            return 201, {"ref": ref, "url": "%s/api/v3/repos/%s/%s/git/refs/%s" % (base, repo.owner, repo.name, ref), "object": {"type": "commit", "sha": sha}}, headers
        if method == "GET":
            refs = [
                {"ref": refname, "object": {"type": repo.get(oid)[0] if repo.get(oid) else "commit", "sha": oid}}
                for refname, oid in sorted(repo.refs.items())
            ]
            return 200, refs, headers
        return (*not_found(), headers)
    if len(rest) >= 3 and rest[0] == "git" and rest[1] == "refs":
        refname = "refs/" + "/".join(rest[2:])
        oid = repo.refs.get(refname)
        if oid is None:
            return (*not_found(), headers)
        if method == "GET":
            entry = repo.get(oid)
            return 200, {"ref": refname, "object": {"type": entry[0] if entry else "commit", "sha": oid}}, headers
        if method == "DELETE":
            repo.refs.pop(refname, None)
            return 204, None, headers
        return (*not_found(), headers)

    if rest == ["commits"]:
        if method == "GET":
            ref = query.get("sha", [None])[0]
            tip = None
            if ref:
                if repo.refs.get("refs/heads/%s" % ref):
                    tip = repo.refs["refs/heads/%s" % ref]
                elif repo.refs.get(ref):
                    tip = repo.refs[ref]
                elif repo.commit_info(ref) is not None:
                    tip = ref
            if tip is None:
                tip = repo.refs.get("refs/heads/%s" % repo.default_branch)
            commits = []
            if tip is not None:
                seen = set()
                stack = [tip]
                while stack:
                    oid = stack.pop()
                    if oid in seen:
                        continue
                    seen.add(oid)
                    info = repo.commit_info(oid)
                    if info is None:
                        continue
                    commits.append(commit_json(repo, oid, base))
                    stack.extend(reversed(info[1]))
            page_items, link = paginate(commits, query, "%s/api/v3/repos/%s/%s/commits" % (base, repo.owner, repo.name))
            if link:
                headers["Link"] = link
            return 200, page_items, headers
        return (*not_found(), headers)

    if rest and rest[0] == "contents":
        return contents_route(repo, method, rest[1:], query, body, base, headers)

    if rest == ["labels"]:
        if method == "POST":
            data = body or {}
            name = data.get("name")
            if not isinstance(name, str) or not name:
                return (*validation_error("name is required"), headers)
            if name in repo.labels:
                return (*validation_error("name already exists"), headers)
            label_id = getattr(repo, "label_seq", 0) + 1
            repo.label_seq = label_id
            label = {
                "id": repo.id * 1000 + label_id,
                "name": name,
                "color": str(data.get("color") or "ededed"),
                "description": data.get("description"),
            }
            repo.labels[name] = label
            return 201, label_json(label), headers
        if method == "GET":
            return 200, [label_json(repo.labels[name]) for name in sorted(repo.labels)], headers
        return (*not_found(), headers)

    if rest == ["issues"]:
        if method == "POST":
            return create_issue(repo, body or {}, base, headers)
        if method == "GET":
            state = query.get("state", ["open"])[0]
            issues = [i for i in repo.issues.values() if state == "all" or i["state"] == state]
            direction = query.get("direction", ["desc"])[0]
            issues.sort(key=lambda i: i["number"], reverse=(direction != "asc"))
            views = [issue_json(repo, i, base) for i in issues]
            page_items, link = paginate(views, query, "%s/api/v3/repos/%s/%s/issues" % (base, repo.owner, repo.name))
            if link:
                headers["Link"] = link
            return 200, page_items, headers
        return (*not_found(), headers)
    if len(rest) >= 2 and rest[0] == "issues":
        try:
            number = int(rest[1])
        except ValueError:
            return (*not_found(), headers)
        issue = repo.issues.get(number)
        if len(rest) == 2:
            if issue is None:
                return (*not_found(), headers)
            if method == "GET":
                return 200, issue_json(repo, issue, base), headers
            if method == "PATCH":
                data = body or {}
                if "title" in data:
                    issue["title"] = data["title"]
                if "body" in data:
                    issue["body"] = data["body"]
                if "labels" in data:
                    names = [entry if isinstance(entry, str) else entry.get("name") for entry in data["labels"] or []]
                    if any(name not in repo.labels for name in names):
                        return (*validation_error("labels are invalid"), headers)
                    issue["labels"] = names
                if "state" in data and data["state"] in ("open", "closed"):
                    issue["state"] = data["state"]
                    issue["closed_ts"] = CLOCK.next_ts() if data["state"] == "closed" else None
                issue["updated_ts"] = CLOCK.next_ts()
                return 200, issue_json(repo, issue, base), headers
            return (*not_found(), headers)
        if len(rest) == 3 and rest[2] == "comments":
            if issue is None:
                return (*not_found(), headers)
            if method == "POST":
                data = body or {}
                if not isinstance(data.get("body"), str):
                    return (*validation_error("body is required"), headers)
                with FORGE.lock:
                    FORGE.next_comment_id += 0  # ids derive from a per-forge counter below
                    comment_id = getattr(FORGE, "comment_seq", 0) + 1
                    FORGE.comment_seq = comment_id
                ts = CLOCK.next_ts()
                comment = {"id": comment_id, "body": data["body"], "created_ts": ts, "updated_ts": ts}
                issue["comments"].append(comment)
                issue["updated_ts"] = ts
                return 201, comment_json(repo, issue, comment, base), headers
            if method == "GET":
                views = [comment_json(repo, issue, c, base) for c in issue["comments"]]
                page_items, link = paginate(views, query, "%s/api/v3/repos/%s/%s/issues/%d/comments" % (base, repo.owner, repo.name, number))
                if link:
                    headers["Link"] = link
                return 200, page_items, headers
            return (*not_found(), headers)
        return (*not_found(), headers)

    if rest == ["pulls"]:
        if method == "POST":
            return create_pull(repo, body or {}, base, headers)
        if method == "GET":
            state = query.get("state", ["open"])[0]
            pulls = [p for p in repo.pulls.values() if state == "all" or p["state"] == state]
            pulls.sort(key=lambda p: p["number"], reverse=True)
            views = [pull_json(repo, p, base) for p in pulls]
            page_items, link = paginate(views, query, "%s/api/v3/repos/%s/%s/pulls" % (base, repo.owner, repo.name))
            if link:
                headers["Link"] = link
            return 200, page_items, headers
        return (*not_found(), headers)
    if len(rest) >= 2 and rest[0] == "pulls":
        try:
            number = int(rest[1])
        except ValueError:
            return (*not_found(), headers)
        pull = repo.pulls.get(number)
        if len(rest) == 2:
            if pull is None:
                return (*not_found(), headers)
            if method == "GET":
                return 200, pull_json(repo, pull, base), headers
            if method == "PATCH":
                data = body or {}
                if "title" in data:
                    pull["title"] = data["title"]
                if "body" in data:
                    pull["body"] = data["body"]
                if "state" in data and data["state"] in ("open", "closed"):
                    pull["state"] = data["state"]
                pull["updated_ts"] = CLOCK.next_ts()
                return 200, pull_json(repo, pull, base), headers
            return (*not_found(), headers)
        if len(rest) == 3 and rest[2] == "files" and method == "GET":
            if pull is None:
                return (*not_found(), headers)
            files = pull_files(repo, pull)
            page_items, link = paginate(files, query, "%s/api/v3/repos/%s/%s/pulls/%d/files" % (base, repo.owner, repo.name, number))
            if link:
                headers["Link"] = link
            return 200, page_items, headers
        if len(rest) == 3 and rest[2] == "merge" and method == "PUT":
            if pull is None:
                return (*not_found(), headers)
            data = body or {}
            merge_method = str(data.get("merge_method") or "merge")
            if merge_method == "rebase":
                merge_method = "squash"
            status, sha = merge_pull(repo, pull, merge_method)
            if status != 200:
                return 405, {"message": "Pull Request is not mergeable"}, headers
            return 200, {"sha": sha, "merged": True, "message": "Pull Request successfully merged"}, headers
        return (*not_found(), headers)

    return (*not_found(), headers)


def create_issue(repo: Repo, data, base: str, headers):
    title = data.get("title")
    if not isinstance(title, str) or not title:
        return (*validation_error("title is required"), headers)
    names = []
    for entry in data.get("labels") or []:
        names.append(entry if isinstance(entry, str) else entry.get("name"))
    if any(name not in repo.labels for name in names):
        return (*validation_error("labels are invalid"), headers)
    ts = CLOCK.next_ts()
    number = repo.next_number
    repo.next_number += 1
    issue = {
        "number": number,
        "title": title,
        "body": data.get("body") or "",
        "state": "open",
        "labels": names,
        "comments": [],
        "created_ts": ts,
        "updated_ts": ts,
        "closed_ts": None,
    }
    repo.issues[number] = issue
    return 201, issue_json(repo, issue, base), headers


def create_pull(repo: Repo, data, base: str, headers):
    title = data.get("title")
    head = data.get("head")
    base_branch = data.get("base")
    if not isinstance(title, str) or not title:
        return (*validation_error("title is required"), headers)
    if not isinstance(head, str) or repo.refs.get("refs/heads/%s" % head) is None:
        return (*validation_error("head is invalid"), headers)
    if not isinstance(base_branch, str) or repo.refs.get("refs/heads/%s" % base_branch) is None:
        return (*validation_error("base is invalid"), headers)
    for existing in repo.pulls.values():
        if existing["state"] == "open" and existing["head"] == head and existing["base"] == base_branch:
            return (*validation_error("A pull request already exists for %s -> %s" % (head, base_branch)), headers)
    ts = CLOCK.next_ts()
    number = repo.next_number
    repo.next_number += 1
    pull = {
        "number": number,
        "title": title,
        "body": data.get("body") or "",
        "head": head,
        "base": base_branch,
        "head_sha": repo.refs.get("refs/heads/%s" % head),
        "base_sha": repo.refs.get("refs/heads/%s" % base_branch),
        "state": "open",
        "merged": False,
        "merge_commit_sha": None,
        "merged_ts": None,
        "created_ts": ts,
        "updated_ts": ts,
    }
    repo.pulls[number] = pull
    return 201, pull_json(repo, pull, base), headers


def contents_route(repo: Repo, method: str, path_segments, query, body, base: str, headers):
    path = "/".join(path_segments)
    data = body or {}
    branch = data.get("branch") or query.get("ref", [None])[0] or repo.default_branch
    refname = "refs/heads/%s" % branch
    tip = repo.refs.get(refname)

    if method == "GET":
        if tip is None:
            return (*not_found(), headers)
        flat = repo.flat_at_commit(tip)
        if path and path in flat:
            mode, oid = flat[path]
            entry = repo.get(oid)
            content = entry[1] if entry else b""
            return (
                200,
                {
                    "type": "file",
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode("ascii"),
                    "sha": oid,
                    "size": len(content),
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "url": "%s/api/v3/repos/%s/%s/contents/%s" % (base, repo.owner, repo.name, path),
                    "html_url": "%s/%s/%s/blob/%s/%s" % (base, repo.owner, repo.name, branch, path),
                    "git_url": "%s/api/v3/repos/%s/%s/git/blobs/%s" % (base, repo.owner, repo.name, oid),
                    "download_url": "%s/%s/%s/raw/%s/%s" % (base, repo.owner, repo.name, branch, path),
                },
                headers,
            )
        prefix = (path + "/") if path else ""
        children = {}
        for entry_path, (mode, oid) in flat.items():
            if not entry_path.startswith(prefix):
                continue
            rest_name = entry_path[len(prefix) :]
            head, sep, _tail = rest_name.partition("/")
            if sep:
                if head not in children:
                    children[head] = ("dir", None)
            else:
                children[head] = ("file", (mode, oid))
        if path and not children:
            return (*not_found(), headers)
        listing = []
        for name in sorted(children):
            kind, value = children[name]
            if kind == "dir":
                listing.append(
                    {
                        "type": "dir",
                        "name": name,
                        "path": prefix + name,
                        "sha": subtree_oid(repo, tip, prefix + name),
                        "size": 0,
                        "url": "%s/api/v3/repos/%s/%s/contents/%s" % (base, repo.owner, repo.name, prefix + name),
                    }
                )
            else:
                mode, oid = value
                entry = repo.get(oid)
                listing.append(
                    {
                        "type": "file",
                        "name": name,
                        "path": prefix + name,
                        "sha": oid,
                        "size": len(entry[1]) if entry else 0,
                        "url": "%s/api/v3/repos/%s/%s/contents/%s" % (base, repo.owner, repo.name, prefix + name),
                    }
                )
        return 200, listing, headers

    if method == "PUT":
        if not isinstance(data.get("message"), str) or not isinstance(data.get("content"), str):
            return (*validation_error("message and content are required"), headers)
        try:
            content = base64.b64decode(data["content"], validate=False)
        except Exception:
            return 400, {"message": "Problems parsing the content: invalid base64"}, headers
        flat = repo.flat_at_commit(tip) if tip else {}
        existing = flat.get(path)
        if existing is not None:
            sha_given = data.get("sha")
            if sha_given is None:
                return (*validation_error("sha wasn't supplied"), headers)
            if sha_given != existing[1]:
                return 409, {"message": "%s does not match %s" % (path, sha_given)}, headers
            status = 200
        else:
            status = 201
        ts = CLOCK.next_ts()
        commit = commit_on_branch(repo, branch, {path: content}, data["message"], ts)
        blob_oid = repo.store("blob", content)
        return (
            status,
            {
                "content": {
                    "type": "file",
                    "encoding": "base64",
                    "sha": blob_oid,
                    "size": len(content),
                    "name": path.rsplit("/", 1)[-1],
                    "path": path,
                    "url": "%s/api/v3/repos/%s/%s/contents/%s" % (base, repo.owner, repo.name, path),
                    "html_url": "%s/%s/%s/blob/%s/%s" % (base, repo.owner, repo.name, branch, path),
                    "git_url": "%s/api/v3/repos/%s/%s/git/blobs/%s" % (base, repo.owner, repo.name, blob_oid),
                    "download_url": "%s/%s/%s/raw/%s/%s" % (base, repo.owner, repo.name, branch, path),
                },
                "commit": commit_json(repo, commit, base),
            },
            headers,
        )

    if method == "DELETE":
        if tip is None:
            return (*not_found(), headers)
        flat = repo.flat_at_commit(tip)
        existing = flat.get(path)
        if existing is None:
            return (*not_found(), headers)
        sha_given = data.get("sha")
        if sha_given is not None and sha_given != existing[1]:
            return 409, {"message": "%s does not match %s" % (path, sha_given)}, headers
        message = data.get("message") or "Delete %s" % path
        ts = CLOCK.next_ts()
        commit = commit_on_branch(repo, branch, {path: None}, message, ts)
        return 200, {"content": None, "commit": commit_json(repo, commit, base)}, headers

    return (*not_found(), headers)


def subtree_oid(repo: Repo, commit_oid: str, dir_path: str):
    tree = repo.tree_of_commit(commit_oid)
    if tree is None:
        return ZERO_OID
    current = tree
    for part in dir_path.split("/"):
        entry = repo.get(current)
        if entry is None or entry[0] != "tree":
            return ZERO_OID
        nxt = None
        for mode, name, oid in parse_tree(entry[1]):
            if name == part and mode == "40000":
                nxt = oid
                break
        if nxt is None:
            return ZERO_OID
        current = nxt
    return current


# --------------------------------------------------------------------------
# GraphQL v4: a small parser + executor for the documents gh sends.
# Objects are (type_name, payload) pairs; connections and payloads are dicts.
# --------------------------------------------------------------------------

GQL_FIELD_LISTS = {
    "Query": ["viewer", "repository", "node", "rateLimit", "__type", "__schema"],
    "Mutation": ["createIssue", "closeIssue", "createPullRequest", "mergePullRequest", "addComment"],
    "User": ["id", "login", "name", "email", "url", "avatarUrl", "databaseId"],
    "Repository": [
        "id", "databaseId", "name", "nameWithOwner", "owner", "defaultBranchRef", "viewerPermission",
        "hasIssuesEnabled", "hasProjectsEnabled", "hasWikiEnabled", "isPrivate", "isFork", "description",
        "url", "createdAt", "updatedAt", "pushedAt", "issue", "issues", "pullRequest", "pullRequests",
        "labels", "primaryLanguage", "stargazerCount", "forkCount", "watchers",
    ],
    "Ref": ["id", "name", "prefix", "target"],
    "Issue": [
        "id", "databaseId", "number", "title", "body", "state", "url", "author", "repository",
        "createdAt", "updatedAt", "closedAt", "closed", "labels", "comments", "assignees",
        "viewerCanUpdate", "locked",
    ],
    "PullRequest": [
        "id", "databaseId", "number", "title", "body", "state", "merged", "mergeable", "headRefName",
        "baseRefName", "headRef", "baseRef", "files", "author", "repository", "createdAt", "updatedAt",
        "closedAt", "mergedAt", "url", "mergedBy", "additions", "deletions", "changedFiles", "commits",
    ],
    "IssueComment": ["id", "databaseId", "body", "author", "createdAt", "updatedAt", "url"],
    "Label": ["id", "name", "color", "description"],
    "PageInfo": ["hasNextPage", "hasPreviousPage", "startCursor", "endCursor"],
    "IssueConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "IssueCommentConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestChangedFileConnection": ["nodes", "edges", "pageInfo", "totalCount"],
    "PullRequestChangedFile": ["path", "additions", "deletions", "changeType"],
    "IssueEdge": ["node", "cursor"],
    "PullRequestEdge": ["node", "cursor"],
    "IssueCommentEdge": ["node", "cursor"],
    "CreateIssuePayload": ["issue", "clientMutationId"],
    "CloseIssuePayload": ["issue", "clientMutationId"],
    "CreatePullRequestPayload": ["pullRequest", "clientMutationId"],
    "MergePullRequestPayload": ["pullRequest", "clientMutationId"],
    "AddCommentPayload": ["commentEdge", "subject", "clientMutationId"],
    "__Type": ["name", "kind", "description", "fields", "enumValues", "possibleTypes", "inputFields", "ofType", "specifiedByURL"],
    "__Field": ["name", "description", "args", "type", "isDeprecated", "deprecationReason"],
    "__EnumValue": ["name", "description", "isDeprecated", "deprecationReason"],
    "__InputValue": ["name", "description", "type", "defaultValue"],
    "__Schema": ["types", "queryType", "mutationType", "directives"],
    "__Directive": ["name", "description", "locations", "args"],
}

GQL_ENUM_VALUES = {
    "SearchType": ["ISSUE", "USER", "REPOSITORY", "PULL_REQUEST", "COMMIT", "CODE", "DISCUSSION", "WIKI"],
    "IssueState": ["OPEN", "CLOSED"],
    "PullRequestState": ["OPEN", "CLOSED", "MERGED"],
    "MergeableState": ["MERGEABLE", "CONFLICTING", "UNKNOWN"],
    "PullRequestMergeMethod": ["MERGE", "SQUASH", "REBASE"],
    "RepositoryPermission": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"],
    "IssueOrderField": ["CREATED_AT", "UPDATED_AT", "COMMENTS"],
    "OrderDirection": ["ASC", "DESC"],
    "FileChangeType": ["ADDED", "DELETED", "MODIFIED", "RENAMED", "COPIED", "CHANGED"],
}

GQL_TYPE_KINDS = {
    "SearchType": "ENUM", "IssueState": "ENUM", "PullRequestState": "ENUM", "MergeableState": "ENUM",
    "PullRequestMergeMethod": "ENUM", "RepositoryPermission": "ENUM", "IssueOrderField": "ENUM",
    "OrderDirection": "ENUM", "FileChangeType": "ENUM",
    "CreateIssueInput": "INPUT_OBJECT", "CloseIssueInput": "INPUT_OBJECT",
    "CreatePullRequestInput": "INPUT_OBJECT", "MergePullRequestInput": "INPUT_OBJECT",
    "AddCommentInput": "INPUT_OBJECT", "IssueOrder": "INPUT_OBJECT",
}


class GqlSyntaxError(Exception):
    pass


def gql_tokenize(text: str):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text.startswith("...", i):
            tokens.append(("spread", "..."))
            i += 3
            continue
        if ch in "!$&():=@[]{}|":
            tokens.append((ch, ch))
            i += 1
            continue
        if ch == '"':
            if text.startswith('"""', i):
                end = text.find('"""', i + 3)
                if end < 0:
                    raise GqlSyntaxError("unterminated block string")
                tokens.append(("string", text[i + 3 : end]))
                i = end + 3
                continue
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    esc = text[j + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "/": "/", "\\": "\\", '"': '"'}.get(esc, esc))
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            if j >= n:
                raise GqlSyntaxError("unterminated string")
            tokens.append(("string", "".join(buf)))
            i = j + 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            while j < n and (text[j].isdigit() or text[j] in ".eE+-"):
                j += 1
            raw = text[i:j]
            try:
                value = float(raw) if any(c in raw for c in ".eE") else int(raw)
            except ValueError:
                raise GqlSyntaxError("bad number %r" % raw)
            tokens.append(("number", value))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i
            while j < n and (text[j].isalnum() or text[j] == "_"):
                j += 1
            tokens.append(("name", text[i:j]))
            i = j
            continue
        raise GqlSyntaxError("unexpected character %r" % ch)
    tokens.append(("eof", None))
    return tokens


class GqlParser:
    def __init__(self, text: str):
        self.tokens = gql_tokenize(text)
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, kind: str):
        token = self.next()
        if token[0] != kind:
            raise GqlSyntaxError("expected %r, got %r" % (kind, token))
        return token[1]

    def parse_document(self):
        operations = []
        fragments = {}
        while self.peek()[0] != "eof":
            kind, value = self.peek()
            if kind == "{":
                operations.append({"type": "query", "name": None, "vars": {}, "selection": self.parse_selection_set()})
            elif kind == "name" and value in ("query", "mutation", "subscription"):
                self.next()
                name = None
                if self.peek()[0] == "name":
                    name = self.next()[1]
                var_defs = {}
                if self.peek()[0] == "(":
                    var_defs = self.parse_var_defs()
                self.skip_directives()
                operations.append({"type": value, "name": name, "vars": var_defs, "selection": self.parse_selection_set()})
            elif kind == "name" and value == "fragment":
                self.next()
                frag_name = self.expect("name")
                if self.next() != ("name", "on"):
                    raise GqlSyntaxError("fragment without 'on'")
                type_condition = self.expect("name")
                self.skip_directives()
                fragments[frag_name] = {"type": type_condition, "selection": self.parse_selection_set()}
            else:
                raise GqlSyntaxError("unexpected token %r" % (value,))
        return operations, fragments

    def parse_var_defs(self):
        defs = {}
        self.expect("(")
        while self.peek()[0] != ")":
            self.expect("$")
            name = self.expect("name")
            self.expect(":")
            self.parse_type()
            default = None
            if self.peek()[0] == "=":
                self.next()
                default = self.parse_value()
            self.skip_directives()
            defs[name] = default
        self.expect(")")
        return defs

    def parse_type(self):
        if self.peek()[0] == "[":
            self.next()
            self.parse_type()
            self.expect("]")
        else:
            self.expect("name")
        if self.peek()[0] == "!":
            self.next()

    def skip_directives(self):
        while self.peek()[0] == "@":
            self.next()
            self.expect("name")
            if self.peek()[0] == "(":
                self.parse_arguments()

    def parse_arguments(self):
        args = {}
        self.expect("(")
        while self.peek()[0] != ")":
            name = self.expect("name")
            self.expect(":")
            args[name] = self.parse_value()
        self.expect(")")
        return args

    def parse_value(self):
        kind, value = self.next()
        if kind == "$":
            return ("var", self.expect("name"))
        if kind == "string":
            return ("lit", value)
        if kind == "number":
            return ("lit", value)
        if kind == "name":
            if value == "true":
                return ("lit", True)
            if value == "false":
                return ("lit", False)
            if value == "null":
                return ("lit", None)
            return ("enum", value)
        if kind == "[":
            items = []
            while self.peek()[0] != "]":
                items.append(self.parse_value())
            self.expect("]")
            return ("list", items)
        if kind == "{":
            obj = {}
            while self.peek()[0] != "}":
                key = self.expect("name")
                self.expect(":")
                obj[key] = self.parse_value()
            self.expect("}")
            return ("object", obj)
        raise GqlSyntaxError("unexpected value token %r" % (value,))

    def parse_selection_set(self):
        self.expect("{")
        selections = []
        while self.peek()[0] != "}":
            kind, value = self.peek()
            if kind == "spread":
                self.next()
                if self.peek() == ("name", "on"):
                    self.next()
                    type_condition = self.expect("name")
                    self.skip_directives()
                    selections.append({"kind": "inline", "type": type_condition, "selection": self.parse_selection_set()})
                else:
                    frag_name = self.expect("name")
                    self.skip_directives()
                    selections.append({"kind": "spread", "name": frag_name})
            elif kind == "name":
                self.next()
                alias = None
                name = value
                if self.peek()[0] == ":":
                    self.next()
                    alias = name
                    name = self.expect("name")
                args = {}
                if self.peek()[0] == "(":
                    args = self.parse_arguments()
                self.skip_directives()
                selection = None
                if self.peek()[0] == "{":
                    selection = self.parse_selection_set()
                selections.append({"kind": "field", "alias": alias, "name": name, "args": args, "selection": selection})
            else:
                raise GqlSyntaxError("unexpected selection token %r" % (value,))
        self.expect("}")
        return selections


def gql_value(node, variables):
    kind = node[0]
    if kind == "var":
        return variables.get(node[1])
    if kind in ("lit", "enum"):
        return node[1]
    if kind == "list":
        return [gql_value(item, variables) for item in node[1]]
    if kind == "object":
        return {key: gql_value(value, variables) for key, value in node[1].items()}
    return None


class GqlContext:
    def __init__(self, variables, fragments, base):
        self.variables = variables
        self.fragments = fragments
        self.base = base
        self.errors = []


def gql_cursor(number: int) -> str:
    return base64.b64encode(("cursor:%d" % number).encode("ascii")).decode("ascii")


def gql_cursor_number(cursor):
    if not isinstance(cursor, str):
        return None
    try:
        raw = base64.b64decode(cursor.encode("ascii")).decode("ascii")
        if raw.startswith("cursor:"):
            return int(raw[len("cursor:") :])
    except Exception:
        return None
    return None


def gql_connection(items, first, after, cursor_of):
    """items: [(cursor_key, wrapped_node)] already in final order."""
    start = 0
    if after is not None:
        after_key = gql_cursor_number(after)
        for index, (key, _node) in enumerate(items):
            if key == after_key:
                start = index + 1
                break
    total = len(items)
    page = items[start:] if first is None else items[start : start + max(0, first)]
    has_next = start + len(page) < total
    end_cursor = gql_cursor(page[-1][0]) if page else None
    start_cursor = gql_cursor(page[0][0]) if page else None
    return {
        "__conn__": True,
        "nodes": [node for _key, node in page],
        "edges": [{"node": node, "cursor": gql_cursor(key)} for key, node in page],
        "pageInfo": {
            "hasNextPage": has_next,
            "hasPreviousPage": start > 0,
            "startCursor": start_cursor,
            "endCursor": end_cursor,
        },
        "totalCount": total,
    }


def gql_issue_order(issues, order_by):
    field = "CREATED_AT"
    direction = "ASC"
    if isinstance(order_by, dict):
        field = order_by.get("field") or field
        direction = order_by.get("direction") or direction
    if field == "UPDATED_AT":
        key = lambda i: (i["updated_ts"], i["number"])
    elif field == "COMMENTS":
        key = lambda i: (len(i["comments"]), i["number"])
    else:
        key = lambda i: i["number"]
    return sorted(issues, key=key, reverse=(direction == "DESC"))


def gql_find_repo_by_node(key: str):
    try:
        repo_id = int(key)
    except ValueError:
        return None
    for repo in FORGE.repos.values():
        if repo.id == repo_id:
            return repo
    return None


def gql_resolve_field(ctx: GqlContext, type_name: str, obj, field: str, args):
    """Resolve one field; obj is the type's payload (see module comment)."""
    base = ctx.base
    if type_name == "Query":
        if field == "viewer":
            return ("User", None)
        if field == "repository":
            repo = FORGE.get_repo(str(args.get("owner")), str(args.get("name")))
            return ("Repository", repo) if repo is not None else None
        if field == "node":
            return gql_node(ctx, args.get("id"))
        if field == "__type":
            return ("__Type", str(args.get("name")))
        if field == "__schema":
            return ("__Schema", None)
    if type_name == "Mutation":
        return gql_mutation(ctx, field, args)
    if type_name == "User":
        if field == "id":
            return node_id("User", str(VIEWER["id"]))
        if field == "databaseId":
            return VIEWER["id"]
        return VIEWER.get(field)
    if type_name == "Repository":
        repo = obj
        if field == "id":
            return node_id("Repository", str(repo.id))
        if field == "databaseId":
            return repo.id
        if field == "name":
            return repo.name
        if field == "nameWithOwner":
            return "%s/%s" % (repo.owner, repo.name)
        if field == "owner":
            return ("User", None)
        if field == "defaultBranchRef":
            if "refs/heads/%s" % repo.default_branch in repo.refs:
                return ("Ref", repo.default_branch)
            return None
        if field == "viewerPermission":
            return "ADMIN"
        if field in ("hasIssuesEnabled", "hasProjectsEnabled", "hasWikiEnabled"):
            return True
        if field == "isPrivate":
            return repo.private
        if field == "isFork":
            return False
        if field == "description":
            return repo.description
        if field == "url":
            return "%s/%s/%s" % (base, repo.owner, repo.name)
        if field == "createdAt":
            return iso_of(repo.created_ts)
        if field == "updatedAt":
            return iso_of(repo.updated_ts)
        if field == "issue":
            issue = repo.issues.get(int(args.get("number") or 0))
            return ("Issue", (repo, issue)) if issue is not None else None
        if field == "issues":
            states = args.get("states")
            issues = list(repo.issues.values())
            if states:
                wanted = {str(s).lower() for s in states}
                issues = [i for i in issues if i["state"] in wanted]
            issues = gql_issue_order(issues, args.get("orderBy"))
            items = [(i["number"], ("Issue", (repo, i))) for i in issues]
            return gql_connection(items, args.get("first"), args.get("after"), None)
        if field == "pullRequest":
            pull = repo.pulls.get(int(args.get("number") or 0))
            return ("PullRequest", (repo, pull)) if pull is not None else None
        if field == "pullRequests":
            states = args.get("states") or ["OPEN"]
            pulls = []
            for pull in repo.pulls.values():
                gql_state = "MERGED" if pull["merged"] else ("CLOSED" if pull["state"] == "closed" else "OPEN")
                if gql_state in states:
                    pulls.append(pull)
            pulls.sort(key=lambda p: p["number"])
            items = [(p["number"], ("PullRequest", (repo, p))) for p in pulls]
            return gql_connection(items, args.get("first"), args.get("after"), None)
    if type_name == "Ref":
        if field == "name":
            return obj
        if field == "prefix":
            return "refs/heads/"
        if field == "id":
            return node_id("Ref", obj)
    if type_name == "Issue":
        repo, issue = obj
        if field == "id":
            return node_id("Issue", "%d:%d" % (repo.id, issue["number"]))
        if field == "databaseId":
            return repo.id * 100000 + issue["number"]
        if field == "number":
            return issue["number"]
        if field == "title":
            return issue["title"]
        if field == "body":
            return issue["body"]
        if field == "state":
            return issue["state"].upper()
        if field == "closed":
            return issue["state"] == "closed"
        if field == "url":
            return "%s/%s/%s/issues/%d" % (base, repo.owner, repo.name, issue["number"])
        if field == "author":
            return ("User", None)
        if field == "repository":
            return ("Repository", repo)
        if field == "createdAt":
            return iso_of(issue["created_ts"])
        if field == "updatedAt":
            return iso_of(issue["updated_ts"])
        if field == "closedAt":
            return iso_of(issue["closed_ts"]) if issue.get("closed_ts") else None
        if field == "labels":
            items = [(idx, ("Label", repo.labels[name])) for idx, name in enumerate(issue["labels"]) if name in repo.labels]
            return gql_connection(items, args.get("first"), args.get("after"), None)
        if field == "comments":
            items = [
                (c["id"], ("IssueComment", (repo, issue, c))) for c in issue["comments"]
            ]
            return gql_connection(items, args.get("first"), args.get("after"), None)
    if type_name == "PullRequest":
        repo, pull = obj
        if field == "id":
            return node_id("PullRequest", "%d:%d" % (repo.id, pull["number"]))
        if field == "databaseId":
            return repo.id * 100000 + pull["number"]
        if field == "number":
            return pull["number"]
        if field == "title":
            return pull["title"]
        if field == "body":
            return pull["body"]
        if field == "state":
            return "MERGED" if pull["merged"] else pull["state"].upper()
        if field == "merged":
            return pull["merged"]
        if field == "mergeable":
            if pull["state"] == "closed":
                return "UNKNOWN"
            with repo.lock:
                return "MERGEABLE" if pull_is_mergeable(repo, pull) else "CONFLICTING"
        if field == "headRefName":
            return pull["head"]
        if field == "baseRefName":
            return pull["base"]
        if field == "author":
            return ("User", None)
        if field == "repository":
            return ("Repository", repo)
        if field == "createdAt":
            return iso_of(pull["created_ts"])
        if field == "updatedAt":
            return iso_of(pull["updated_ts"])
        if field == "mergedAt":
            return iso_of(pull["merged_ts"]) if pull.get("merged_ts") else None
        if field == "closedAt":
            return iso_of(pull["merged_ts"]) if pull.get("merged_ts") else None
        if field == "url":
            return "%s/%s/%s/pull/%d" % (base, repo.owner, repo.name, pull["number"])
        if field == "files":
            with repo.lock:
                files = pull_files(repo, pull)
            items = [
                (idx, ("PullRequestChangedFile", {"path": f["filename"], "additions": f["additions"], "deletions": f["deletions"], "changeType": f["status"].upper()}))
                for idx, f in enumerate(files)
            ]
            return gql_connection(items, args.get("first"), args.get("after"), None)
        if field in ("additions", "deletions", "changedFiles"):
            with repo.lock:
                files = pull_files(repo, pull)
            if field == "additions":
                return sum(f["additions"] for f in files)
            if field == "deletions":
                return sum(f["deletions"] for f in files)
            return len(files)
    if type_name == "IssueComment":
        repo, issue, comment = obj
        if field == "id":
            return node_id("IssueComment", "%d:%d:%d" % (repo.id, issue["number"], comment["id"]))
        if field == "databaseId":
            return comment["id"]
        if field == "body":
            return comment["body"]
        if field == "author":
            return ("User", None)
        if field == "createdAt":
            return iso_of(comment["created_ts"])
        if field == "updatedAt":
            return iso_of(comment["updated_ts"])
        if field == "url":
            return "%s/%s/%s/issues/%d#issuecomment-%d" % (base, repo.owner, repo.name, issue["number"], comment["id"])
    if type_name == "Label":
        if field == "id":
            return node_id("Label", str(obj["id"]))
        return obj.get(field)
    if type_name == "__Type":
        name = obj
        if field == "name":
            return name
        if field == "kind":
            return GQL_TYPE_KINDS.get(name, "OBJECT")
        if field == "description":
            return None
        if field == "fields":
            return [("__Field", {"name": fname}) for fname in GQL_FIELD_LISTS.get(name, [])]
        if field == "enumValues":
            return [("__EnumValue", {"name": vname}) for vname in GQL_ENUM_VALUES.get(name, [])]
        if field == "possibleTypes":
            return []
        if field == "inputFields":
            return []
        return None
    if type_name in ("__Field", "__EnumValue"):
        if field == "name":
            return obj["name"]
        if field == "description":
            return None
        if field == "isDeprecated":
            return False
        if field == "deprecationReason":
            return None
        if field == "args":
            return []
        if field == "type":
            return ("__Type", "String")
        return None
    if type_name == "__Schema":
        if field == "queryType":
            return ("__Type", "Query")
        if field == "mutationType":
            return ("__Type", "Mutation")
        if field == "types":
            return [("__Type", name) for name in GQL_FIELD_LISTS]
        if field == "directives":
            return []
    if isinstance(obj, dict):
        return obj.get(field)
    return None


def gql_node(ctx: GqlContext, raw_id):
    parsed = parse_node_id(raw_id) if isinstance(raw_id, str) else None
    if parsed is None:
        return None
    type_name, key = parsed
    if type_name == "Repository":
        repo = gql_find_repo_by_node(key)
        return ("Repository", repo) if repo is not None else None
    if type_name == "User":
        return ("User", None)
    if type_name in ("Issue", "PullRequest"):
        parts = key.split(":")
        if len(parts) != 2:
            return None
        repo = gql_find_repo_by_node(parts[0])
        if repo is None:
            return None
        try:
            number = int(parts[1])
        except ValueError:
            return None
        if type_name == "Issue":
            issue = repo.issues.get(number)
            return ("Issue", (repo, issue)) if issue is not None else None
        pull = repo.pulls.get(number)
        return ("PullRequest", (repo, pull)) if pull is not None else None
    return None


def gql_decode_scoped_id(raw_id, expected_type):
    parsed = parse_node_id(raw_id) if isinstance(raw_id, str) else None
    if parsed is None or parsed[0] != expected_type:
        return None
    return parsed[1]


def gql_mutation(ctx: GqlContext, field: str, args):
    inp = args.get("input") or {}
    client_mutation_id = inp.get("clientMutationId")
    if field == "createIssue":
        key = gql_decode_scoped_id(inp.get("repositoryId"), "Repository")
        repo = gql_find_repo_by_node(key) if key else None
        if repo is None:
            ctx.errors.append({"message": "Could not resolve the repository for createIssue"})
            return {"issue": None, "clientMutationId": client_mutation_id}
        with repo.lock:
            ts = CLOCK.next_ts()
            number = repo.next_number
            repo.next_number += 1
            issue = {
                "number": number,
                "title": inp.get("title") or "",
                "body": inp.get("body") or "",
                "state": "open",
                "labels": [],
                "comments": [],
                "created_ts": ts,
                "updated_ts": ts,
                "closed_ts": None,
            }
            repo.issues[number] = issue
        return {"issue": ("Issue", (repo, issue)), "clientMutationId": client_mutation_id}
    if field == "closeIssue":
        key = gql_decode_scoped_id(inp.get("issueId"), "Issue")
        target = gql_node(ctx, inp.get("issueId")) if key else None
        if target is None:
            ctx.errors.append({"message": "Could not resolve the issue for closeIssue"})
            return {"issue": None, "clientMutationId": client_mutation_id}
        repo, issue = target[1]
        with repo.lock:
            issue["state"] = "closed"
            issue["closed_ts"] = CLOCK.next_ts()
            issue["updated_ts"] = issue["closed_ts"]
        return {"issue": ("Issue", (repo, issue)), "clientMutationId": client_mutation_id}
    if field == "createPullRequest":
        key = gql_decode_scoped_id(inp.get("repositoryId"), "Repository")
        repo = gql_find_repo_by_node(key) if key else None
        if repo is None:
            ctx.errors.append({"message": "Could not resolve the repository for createPullRequest"})
            return {"pullRequest": None, "clientMutationId": client_mutation_id}
        head = inp.get("headRefName")
        base_branch = inp.get("baseRefName")
        with repo.lock:
            if repo.refs.get("refs/heads/%s" % head) is None or repo.refs.get("refs/heads/%s" % base_branch) is None:
                ctx.errors.append({"message": "Head or base branch does not exist"})
                return {"pullRequest": None, "clientMutationId": client_mutation_id}
            ts = CLOCK.next_ts()
            number = repo.next_number
            repo.next_number += 1
            pull = {
                "number": number,
                "title": inp.get("title") or "",
                "body": inp.get("body") or "",
                "head": head,
                "base": base_branch,
                "head_sha": repo.refs.get("refs/heads/%s" % head),
                "base_sha": repo.refs.get("refs/heads/%s" % base_branch),
                "state": "open",
                "merged": False,
                "merge_commit_sha": None,
                "merged_ts": None,
                "created_ts": ts,
                "updated_ts": ts,
            }
            repo.pulls[number] = pull
        return {"pullRequest": ("PullRequest", (repo, pull)), "clientMutationId": client_mutation_id}
    if field == "mergePullRequest":
        target = gql_node(ctx, inp.get("pullRequestId"))
        if target is None or target[0] != "PullRequest":
            ctx.errors.append({"message": "Could not resolve the pull request for mergePullRequest"})
            return {"pullRequest": None, "clientMutationId": client_mutation_id}
        repo, pull = target[1]
        method = str(inp.get("mergeMethod") or "SQUASH").lower()
        if method == "rebase":
            method = "squash"
        with repo.lock:
            status, _sha = merge_pull(repo, pull, method)
        if status != 200:
            ctx.errors.append({"message": "Pull Request is not mergeable"})
        return {"pullRequest": ("PullRequest", (repo, pull)), "clientMutationId": client_mutation_id}
    if field == "addComment":
        target = gql_node(ctx, inp.get("subjectId"))
        if target is None or target[0] != "Issue":
            ctx.errors.append({"message": "Could not resolve the subject for addComment"})
            return {"commentEdge": None, "clientMutationId": client_mutation_id}
        repo, issue = target[1]
        with repo.lock:
            comment_id = getattr(FORGE, "comment_seq", 0) + 1
            FORGE.comment_seq = comment_id
            ts = CLOCK.next_ts()
            comment = {"id": comment_id, "body": inp.get("body") or "", "created_ts": ts, "updated_ts": ts}
            issue["comments"].append(comment)
            issue["updated_ts"] = ts
        edge = {"node": ("IssueComment", (repo, issue, comment)), "cursor": gql_cursor(comment_id)}
        return {"commentEdge": edge, "subject": ("Issue", (repo, issue)), "clientMutationId": client_mutation_id}
    ctx.errors.append({"message": "Unknown mutation %s" % field})
    return None


def gql_execute_selection(ctx: GqlContext, type_name: str, obj, selections):
    out = {}
    for selection in selections:
        if selection["kind"] == "inline":
            condition = selection["type"]
            if condition == type_name or condition == "Node":
                out.update(gql_execute_selection(ctx, type_name, obj, selection["selection"]))
            continue
        if selection["kind"] == "spread":
            fragment = ctx.fragments.get(selection["name"])
            if fragment is not None and (fragment["type"] == type_name or fragment["type"] == "Node"):
                out.update(gql_execute_selection(ctx, type_name, obj, fragment["selection"]))
            continue
        field = selection["name"]
        key = selection["alias"] or field
        args = {name: gql_value(value, ctx.variables) for name, value in selection["args"].items()}
        known = GQL_FIELD_LISTS.get(type_name)
        if field in ("__type", "__schema"):
            value = gql_resolve_field(ctx, "Query", None, field, args)
        elif known is not None and field not in known and not (isinstance(obj, dict) and field in obj):
            ctx.errors.append({"message": "Field '%s' doesn't exist on type '%s'" % (field, type_name)})
            out[key] = None
            continue
        else:
            value = gql_resolve_field(ctx, type_name, obj, field, args)
        out[key] = gql_complete(ctx, value, selection["selection"])
    return out


def gql_complete(ctx: GqlContext, value, selection):
    if value is None:
        return None
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str) and value[0] in GQL_FIELD_LISTS:
        if selection is None:
            return None
        return gql_execute_selection(ctx, value[0], value[1], selection)
    if isinstance(value, dict) and value.get("__conn__"):
        if selection is None:
            return value["nodes"]
        # A connection's own type is inferred from its use site by the caller:
        # resolve the requested subfields against the connection dict.
        return gql_execute_connection(ctx, value, selection)
    if isinstance(value, list):
        return [gql_complete(ctx, item, selection) for item in value]
    if isinstance(value, dict):
        if selection is None:
            return value
        out = {}
        for sub in selection:
            if sub["kind"] != "field":
                continue
            sub_key = sub["alias"] or sub["name"]
            out[sub_key] = gql_complete(ctx, value.get(sub["name"]), sub["selection"])
        return out
    return value


def gql_execute_connection(ctx: GqlContext, conn, selections):
    out = {}
    for selection in selections:
        if selection["kind"] != "field":
            continue
        field = selection["name"]
        key = selection["alias"] or field
        if field == "nodes":
            out[key] = [gql_complete(ctx, node, selection["selection"]) for node in conn["nodes"]]
        elif field == "edges":
            edges = []
            for edge in conn["edges"]:
                edge_out = {}
                for sub in selection["selection"] or []:
                    if sub["kind"] != "field":
                        continue
                    sub_key = sub["alias"] or sub["name"]
                    if sub["name"] == "node":
                        edge_out[sub_key] = gql_complete(ctx, edge["node"], sub["selection"])
                    elif sub["name"] == "cursor":
                        edge_out[sub_key] = edge["cursor"]
                edges.append(edge_out)
            out[key] = edges
        elif field == "pageInfo":
            info = conn["pageInfo"]
            info_out = {}
            for sub in selection["selection"] or []:
                if sub["kind"] == "field":
                    info_out[sub["alias"] or sub["name"]] = info.get(sub["name"])
            out[key] = info_out
        elif field == "totalCount":
            out[key] = conn["totalCount"]
        else:
            ctx.errors.append({"message": "Field '%s' doesn't exist on a connection" % field})
            out[key] = None
    return out


def graphql_handle(payload, base: str):
    """Execute one GraphQL operation document; return the response object."""
    query = payload.get("query")
    if not isinstance(query, str):
        return {"errors": [{"message": "No query document was supplied"}]}
    variables = payload.get("variables") or {}
    if not isinstance(variables, dict):
        variables = {}
    try:
        operations, fragments = GqlParser(query).parse_document()
    except GqlSyntaxError as err:
        return {"errors": [{"message": "Syntax error: %s" % err}]}
    if not operations:
        return {"errors": [{"message": "No operation in the document"}]}
    operation = operations[0]
    op_name = payload.get("operationName")
    if op_name:
        for candidate in operations:
            if candidate["name"] == op_name:
                operation = candidate
                break
    merged_vars = {}
    for name, default in operation["vars"].items():
        if default is not None:
            merged_vars[name] = gql_value(default, variables)
    merged_vars.update(variables)
    ctx = GqlContext(merged_vars, fragments, base)
    root_type = "Mutation" if operation["type"] == "mutation" else "Query"
    data = gql_execute_selection(ctx, root_type, None, operation["selection"])
    result = {"data": data}
    if ctx.errors:
        result["errors"] = ctx.errors
    return result


# --------------------------------------------------------------------------
# MCP: JSON-RPC 2.0 over streamable HTTP. server/discover (2026-07-28) plus
# the legacy initialize handshake; the tools act on the same store as REST.
# --------------------------------------------------------------------------

MCP_SUPPORTED_VERSIONS = ["2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]


def _tool(name: str, description: str, properties, required):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


def _string_prop(description: str):
    return {"type": "string", "description": description}


MCP_TOOLS = [
    _tool(
        "create_repo",
        "Create a new repository for the authenticated user.",
        {
            "name": _string_prop("The name of the repository to create."),
            "description": _string_prop("A short description of the repository."),
            "private": {"type": "boolean", "description": "Whether the repository is private."},
        },
        ["name"],
    ),
    _tool(
        "get_repo",
        "Get the details of a repository.",
        {"owner": _string_prop("The owner of the repository."), "repo": _string_prop("The name of the repository.")},
        ["owner", "repo"],
    ),
    _tool(
        "create_issue",
        "Create a new issue in a repository.",
        {
            "owner": _string_prop("The owner of the repository."),
            "repo": _string_prop("The name of the repository."),
            "title": _string_prop("The title of the issue."),
            "body": _string_prop("The body of the issue."),
        },
        ["owner", "repo", "title"],
    ),
    _tool(
        "list_issues",
        "List the issues of a repository.",
        {
            "owner": _string_prop("The owner of the repository."),
            "repo": _string_prop("The name of the repository."),
            "state": _string_prop("Filter by state: open, closed or all."),
        },
        ["owner", "repo"],
    ),
    _tool(
        "create_pull_request",
        "Create a new pull request in a repository.",
        {
            "owner": _string_prop("The owner of the repository."),
            "repo": _string_prop("The name of the repository."),
            "title": _string_prop("The title of the pull request."),
            "body": _string_prop("The body of the pull request."),
            "head": _string_prop("The name of the branch to merge from."),
            "base": _string_prop("The name of the branch to merge into."),
        },
        ["owner", "repo", "title", "head", "base"],
    ),
    _tool(
        "merge_pull_request",
        "Merge a pull request.",
        {
            "owner": _string_prop("The owner of the repository."),
            "repo": _string_prop("The name of the repository."),
            "pull_number": {"type": "integer", "description": "The number of the pull request to merge."},
        },
        ["owner", "repo", "pull_number"],
    ),
]

MCP_TOOLS_BY_NAME = {tool["name"]: tool for tool in MCP_TOOLS}


class McpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def mcp_tool_result(payload) -> dict:
    text = json.dumps(payload)
    return {"content": [{"type": "text", "text": text}], "structuredContent": payload, "isError": False}


def mcp_tool_error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def mcp_call_tool(name: str, arguments, base: str):
    tool = MCP_TOOLS_BY_NAME.get(name)
    if tool is None:
        raise McpError(-32602, "Tool not found: %s" % name)
    if not isinstance(arguments, dict):
        arguments = {}
    for required in tool["inputSchema"]["required"]:
        if arguments.get(required) is None:
            raise McpError(-32602, "Missing required argument: %s" % required)

    if name == "create_repo":
        repo_name = str(arguments["name"])
        with FORGE.lock:
            repo = FORGE.create_repo(
                VIEWER["login"], repo_name, bool(arguments.get("private", False)), arguments.get("description") or ""
            )
            if repo is None:
                repo = FORGE.get_repo(VIEWER["login"], repo_name)
        return mcp_tool_result(repo_json(repo, base))

    owner = str(arguments.get("owner"))
    repo_name = str(arguments.get("repo"))
    repo = FORGE.get_repo(owner, repo_name)
    if repo is None:
        return mcp_tool_error("Repository not found: %s/%s" % (owner, repo_name))

    if name == "get_repo":
        return mcp_tool_result(repo_json(repo, base))
    if name == "create_issue":
        with repo.lock:
            ts = CLOCK.next_ts()
            number = repo.next_number
            repo.next_number += 1
            issue = {
                "number": number,
                "title": str(arguments["title"]),
                "body": arguments.get("body") or "",
                "state": "open",
                "labels": [],
                "comments": [],
                "created_ts": ts,
                "updated_ts": ts,
                "closed_ts": None,
            }
            repo.issues[number] = issue
        return mcp_tool_result(issue_json(repo, issue, base))
    if name == "list_issues":
        state = str(arguments.get("state") or "open")
        with repo.lock:
            issues = [i for i in repo.issues.values() if state == "all" or i["state"] == state]
            issues.sort(key=lambda i: i["number"])
            return mcp_tool_result([issue_json(repo, i, base) for i in issues])
    if name == "create_pull_request":
        with repo.lock:
            status, payload, _headers = create_pull(
                repo,
                {
                    "title": arguments.get("title"),
                    "body": arguments.get("body") or "",
                    "head": arguments.get("head"),
                    "base": arguments.get("base"),
                },
                base,
                {},
            )
        if status != 201:
            return mcp_tool_error("Could not create the pull request: %s" % payload.get("message", status))
        return mcp_tool_result(payload)
    if name == "merge_pull_request":
        number = arguments.get("pull_number")
        try:
            number = int(number)
        except (TypeError, ValueError):
            raise McpError(-32602, "pull_number must be an integer")
        with repo.lock:
            pull = repo.pulls.get(number)
            if pull is None:
                return mcp_tool_error("Pull request not found: %d" % number)
            status, _sha = merge_pull(repo, pull, "squash")
            if status != 200:
                return mcp_tool_error("Pull request %d is not mergeable" % number)
            return mcp_tool_result(pull_json(repo, pull, base))
    return mcp_tool_error("Tool not implemented: %s" % name)


def mcp_handle_one(message, base: str):
    """One JSON-RPC message -> response object, or None for notifications."""
    if not isinstance(message, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method = message.get("method")
    msg_id = message.get("id")
    if method is None:
        return None
    if msg_id is None:
        return None  # a notification: nothing to answer
    try:
        if method == "server/discover":
            result = {
                "supportedVersions": MCP_SUPPORTED_VERSIONS,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "initialize":
            params = message.get("params") or {}
            offered = params.get("protocolVersion")
            result = {
                "protocolVersion": offered if offered in MCP_SUPPORTED_VERSIONS else "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": MCP_TOOLS}
        elif method == "tools/call":
            params = message.get("params") or {}
            result = mcp_call_tool(str(params.get("name")), params.get("arguments") or {}, base)
        else:
            raise McpError(-32601, "Method not found: %s" % method)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    except McpError as err:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": err.code, "message": err.message}}


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

TOKEN = os.environ.get("MAKEFUN_SHOP_TOKEN", "")


def json_bytes(payload) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


# The Server header value never changes; format it once.
_SERVER_LINE = "Server: %s %s\r\n" % (
    "%s/%s" % (SERVER_NAME, SERVER_VERSION),
    BaseHTTPRequestHandler.sys_version,
)
_DATE_CACHE = [0, ""]


def _date_line() -> str:
    # The Date header has one-second granularity; formatting it (a time
    # syscall plus email.utils.formatdate) on every one of thousands of
    # responses is pure overhead, so cache the rendered line per second.
    now = int(time.time())
    if _DATE_CACHE[0] != now:
        _DATE_CACHE[0] = now
        _DATE_CACHE[1] = "Date: %s\r\n" % formatdate(now, usegmt=True)
    return _DATE_CACHE[1]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "%s/%s" % (SERVER_NAME, SERVER_VERSION)
    # TCP_NODELAY: without it, small responses stall ~40 ms behind the peer's
    # delayed ACK, which is fatal to the timed workloads (2,000 reads on one
    # keep-alive connection).
    disable_nagle_algorithm = True

    def log_message(self, fmt, *args):  # keep stdout clean and fast
        pass

    def parse_request(self):
        """Stdlib parse_request with a fast path for the header block.

        http.client.parse_headers drives an email parser over every request;
        at thousands of small requests (2,000 reads, 200 pushes x 2) that is
        the largest single per-request cost in the server. The request-line,
        version, Connection and Expect handling below mirror the stdlib
        exactly; only the header block is parsed by hand when it has the one
        shape real clients send (one "Name: value" line per header), with a
        fallback to the stdlib parser for anything else (folded lines,
        colon-less lines), so observable behavior is unchanged.
        """
        self.command = None
        self.request_version = version = self.default_request_version
        self.close_connection = True
        requestline = str(self.raw_requestline, "iso-8859-1")
        requestline = requestline.rstrip("\r\n")
        self.requestline = requestline
        words = requestline.split()
        if len(words) == 0:
            return False

        if len(words) >= 3:  # Enough to determine protocol version
            version = words[-1]
            try:
                if not version.startswith("HTTP/"):
                    raise ValueError
                base_version_number = version.split("/", 1)[1]
                version_number = base_version_number.split(".")
                if len(version_number) != 2:
                    raise ValueError
                if any(not component.isdigit() for component in version_number):
                    raise ValueError("non digit in http version")
                if any(len(component) > 10 for component in version_number):
                    raise ValueError("unreasonable length http version")
                version_number = int(version_number[0]), int(version_number[1])
            except (ValueError, IndexError):
                self.send_error(400, "Bad request version (%r)" % version)
                return False
            if version_number >= (1, 1) and self.protocol_version >= "HTTP/1.1":
                self.close_connection = False
            if version_number >= (2, 0):
                self.send_error(505, "Invalid HTTP version (%s)" % base_version_number)
                return False
            self.request_version = version

        if not 2 <= len(words) <= 3:
            self.send_error(400, "Bad request syntax (%r)" % requestline)
            return False
        command, path = words[:2]
        if len(words) == 2:
            self.close_connection = True
            if command != "GET":
                self.send_error(400, "Bad HTTP/0.9 request type (%r)" % command)
                return False
        self.command, self.path = command, path

        # gh-87389: reduce '//' to a single '/' (open-redirect guard).
        if self.path.startswith("//"):
            self.path = "/" + self.path.lstrip("/")

        # --- header block: manual fast path, stdlib fallback ---
        raw_lines = []
        while True:
            line = self.rfile.readline(65537)
            if len(line) > 65536:
                self.send_error(431, "Line too long")
                return False
            if line in (b"\r\n", b"\n", b""):
                break
            raw_lines.append(line)
            if len(raw_lines) > 100:
                self.send_error(431, "Too many headers")
                return False
        headers = None
        if all(b":" in line and line[:1] not in (b" ", b"\t") for line in raw_lines):
            headers = self.MessageClass()
            for line in raw_lines:
                name, _, value = line.partition(b":")
                headers[name.decode("iso-8859-1").strip()] = value.decode("iso-8859-1").strip()
        if headers is None:
            try:
                headers = http.client.parse_headers(io.BytesIO(b"".join(raw_lines)))
            except http.client.LineTooLong as err:
                self.send_error(431, "Line too long", str(err))
                return False
            except http.client.HTTPException as err:
                self.send_error(431, "Too many headers", str(err))
                return False
        self.headers = headers

        conntype = self.headers.get("Connection", "")
        if conntype.lower() == "close":
            self.close_connection = True
        elif conntype.lower() == "keep-alive" and self.protocol_version >= "HTTP/1.1":
            self.close_connection = False
        # Examine the headers and look for an Expect directive
        expect = self.headers.get("Expect", "")
        if (
            expect.lower() == "100-continue"
            and self.protocol_version >= "HTTP/1.1"
            and self.request_version >= "HTTP/1.1"
        ):
            if not self.handle_expect_100():
                return False
        return True

    # -- plumbing ----------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra_headers=None):
        # One write for status line + headers + body: a second segment for the
        # body is exactly what Nagle/delayed-ACK stalls on.
        lines = [
            "HTTP/1.1 %d %s\r\n" % (status, self.responses.get(status, ("", ""))[0]),
            "Content-Type: %s\r\n" % content_type,
            "Content-Length: %d\r\n" % len(body),
            "X-GitHub-Request-Id: %s\r\n" % FORGE.next_request_id(),
            _SERVER_LINE,
            _date_line(),
        ]
        if extra_headers:
            for key, value in extra_headers.items():
                lines.append("%s: %s\r\n" % (key, value))
        lines.append("\r\n")
        head = "".join(lines).encode("latin-1")
        if self.command == "HEAD":
            self.wfile.write(head)
        else:
            self.wfile.write(head + body)

    def _send_json(self, status: int, payload, extra_headers=None):
        if status == 204 or payload is None:
            self._send(status, b"", "application/json", extra_headers)
            return
        self._send(status, json_bytes(payload), "application/json", extra_headers)

    def _read_body(self) -> bytes:
        length = self.headers.get("Content-Length")
        if length is None:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        if size <= 0:
            return b""
        return self.rfile.read(size)

    # -- routing -----------------------------------------------------------
    def _handle(self):
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            try:
                self._send_json(500, {"message": "Internal Server Error"})
            except Exception:
                pass

    def _route(self):
        # Fast path for the overwhelmingly common origin-form target: split
        # off the query by hand instead of paying for urlparse, and only pay
        # for unquote/parse_qs when the characters that need them are there.
        target = self.path
        if target.startswith("/") and "#" not in target:
            path_part, _, query_string = target.partition("?")
            raw_path = unquote(path_part) if "%" in path_part else path_part
            query = parse_qs(query_string) if query_string else {}
        else:
            parsed = urlparse(target)
            raw_path = unquote(parsed.path)
            query = parse_qs(parsed.query)
        host = self.headers.get("Host") or "forge.local"
        base = "http://%s" % host
        method = self.command
        # Always drain the request body first: answering (401, 404, ...) with
        # bytes still unread would desync the keep-alive connection and the
        # next request on it would be parsed as garbage.
        body = self._read_body()

        if raw_path == "/healthz":
            self._send_json(200, {"ok": True})
            return

        # Everything else needs the trial's bearer token.
        if TOKEN:
            auth = self.headers.get("Authorization") or ""
            if auth != "Bearer %s" % TOKEN:
                self._send_json(401, {"message": "Bad credentials"})
                return

        if raw_path == "/api/v3" or raw_path.startswith("/api/v3/"):
            payload = None
            if body:
                try:
                    payload = json.loads(body.decode("utf-8"))
                except Exception:
                    self._send_json(400, {"message": "Problems parsing JSON"})
                    return
            segments = [seg for seg in raw_path[len("/api/v3") :].split("/") if seg != ""]
            status, obj, extra = rest_route(method, segments, query, payload, base, raw_path)
            self._send_json(status, obj, extra)
            return

        if raw_path == "/api/graphql":
            if method != "POST":
                self._send_json(400, {"errors": [{"message": "GraphQL only accepts POST"}]})
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json(400, {"errors": [{"message": "Problems parsing JSON"}]})
                return
            if isinstance(payload, list):
                results = [graphql_handle(item, base) if isinstance(item, dict) else {"errors": [{"message": "Invalid operation"}]} for item in payload]
                self._send_json(200, results)
            elif isinstance(payload, dict):
                self._send_json(200, graphql_handle(payload, base))
            else:
                self._send_json(400, {"errors": [{"message": "Expected a JSON object or array"}]})
            return

        if raw_path == "/mcp":
            if method != "POST":
                self._send(405, b"", "application/json")
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            if isinstance(payload, list):
                results = []
                for item in payload:
                    answer = mcp_handle_one(item, base)
                    if answer is not None:
                        results.append(answer)
                self._send_json(200, results)
                return
            answer = mcp_handle_one(payload, base)
            if answer is None:
                self._send(202, b"", "application/json")
                return
            self._send_json(200, answer)
            return

        if self._route_git(raw_path, query, method, body):
            return

        self._send_json(404, {"message": "Not Found"})

    def _route_git(self, raw_path: str, query, method: str, body: bytes) -> bool:
        parts = raw_path.split("/")
        # /<owner>/<repo>.git/<tail...>
        if len(parts) < 4 or not parts[2].endswith(".git"):
            return False
        owner, repo_name = parts[1], parts[2][: -len(".git")]
        tail = parts[3:]
        repo = FORGE.get_repo(owner, repo_name)
        if tail == ["info", "refs"] and method == "GET":
            service = query.get("service", [""])[0]
            if service not in ("git-upload-pack", "git-receive-pack"):
                self._send(400, b"missing or bad service\n", "text/plain; charset=utf-8")
                return True
            if repo is None:
                self._send(404, b"repository not found\n", "text/plain; charset=utf-8")
                return True
            protocol = self.headers.get("Git-Protocol") or ""
            if service == "git-upload-pack" and "version=2" in protocol:
                self._send(200, advertise_v2(), "application/x-git-upload-pack-advertisement")
            elif service == "git-upload-pack":
                self._send(200, advertise_v0(repo, service), "application/x-git-upload-pack-advertisement")
            else:
                self._send(200, advertise_v0(repo, service), "application/x-git-receive-pack-advertisement")
            return True
        if tail == ["git-upload-pack"] and method == "POST":
            if repo is None:
                self._send(404, b"repository not found\n", "text/plain; charset=utf-8")
                return True
            protocol = self.headers.get("Git-Protocol") or ""
            try:
                # No repo.lock here: the object store is append-only and dict
                # reads/stores are atomic under the GIL, so concurrent fetches
                # (the 50-clone workload) can proceed in parallel instead of
                # serializing on one lock; receive-pack already runs lock-free
                # for its read phase and takes the lock only to commit refs.
                answer = handle_upload_pack(repo, body, "version=2" in protocol)
            except ValueError:
                self._send(400, b"bad upload-pack request\n", "text/plain; charset=utf-8")
                return True
            self._send(200, answer, "application/x-git-upload-pack-result")
            return True
        if tail == ["git-receive-pack"] and method == "POST":
            if repo is None:
                self._send(404, b"repository not found\n", "text/plain; charset=utf-8")
                return True
            try:
                answer = handle_receive_pack(repo, body)
            except ValueError:
                self._send(400, b"bad receive-pack request\n", "text/plain; charset=utf-8")
                return True
            self._send(200, answer, "application/x-git-receive-pack-result")
            return True
        return False

    do_GET = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_HEAD = _handle


# --------------------------------------------------------------------------
# Lifecycle: bind, register with the env, serve until it says done.
# --------------------------------------------------------------------------


def env_request(env_url: str, path: str, payload=None):
    url = env_url.rstrip("/") + path
    if payload is None:
        request = urllib.request.Request(url, method="GET")
    else:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read()


def register(env_url: str, port: int) -> None:
    delay = 0.1
    while True:
        try:
            status, _body = env_request(env_url, "/register", {"port": port})
            if status in (200, 409):
                return
            if status in (400, 405):
                print("register refused fatally: HTTP %d" % status, file=sys.stderr)
                sys.exit(1)
        except urllib.error.HTTPError as err:
            if err.code in (200, 409):
                return
            if err.code in (400, 405):
                print("register refused fatally: HTTP %d" % err.code, file=sys.stderr)
                sys.exit(1)
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 2, 2.0)


def watch_done(env_url: str, server: ThreadingHTTPServer) -> None:
    delay = 0.25
    while True:
        try:
            _status, body = env_request(env_url, "/done")
            answer = json.loads(body.decode("utf-8"))
            if answer.get("done") is True:
                break
        except Exception:
            pass
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)
    server.shutdown()


class ForgeHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 512
    allow_reuse_address = True


def main() -> int:
    server = ForgeHTTPServer(("0.0.0.0", 0), Handler)
    port = server.server_address[1]
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").strip()
    print("%s listening on port %d" % (SERVER_NAME, port), flush=True)
    if not env_url:
        # Standalone development mode: serve until interrupted.
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0
    register(env_url, port)
    watcher = threading.Thread(target=watch_done, args=(env_url, server), daemon=True)
    watcher.start()
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
