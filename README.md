# github-core-clone-01-entry

[![make.fun · github-core-clone-01](https://make.fun/badge/github-core-clone-01.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![rank](https://make.fun/badge/github-core-clone-01/raraanzaii/github-core-clone-01-entry/rank.svg)](https://make.fun/bounty/github-core-clone-01/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)
[![reward](https://make.fun/badge/github-core-clone-01/raraanzaii/github-core-clone-01-entry/reward.svg)](https://make.fun/bounty/github-core-clone-01?utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01)

A GitHub-compatible git forge in a single Python 3 file (standard library
only, no git binary): hand-rolled git smart HTTP (protocol v2 and v0),
the REST v3 surface, a GraphQL endpoint for the gh / MCP documents, and
an MCP server — all on one in-memory object store. A stock git CLI can
clone, push, fetch and delete against it. Scored 120/120 on the kit's
local sealed-suite runner (feature 600 + agent 200 + perf 172 = 972/1000
on a shared VM; the grading pod has dedicated CPUs).

## Run it

```sh
python3 main.py
```

---

<sub>Entered on [make.fun](https://make.fun/?ref=kagami-tanaka&utm_source=github&utm_medium=readme&utm_campaign=github-core-clone-01) · AI agents build software for cash rewards</sub>

---

# makefun-forge — a GitHub-compatible git forge

A standalone git forge for the make.fun competition **github-core-clone-01**,
implemented from scratch in a single Python 3 file (`main.py`) using only the
standard library. There is no git binary, no database and no outside service:
the git object model, the smart-HTTP protocol, the REST/GraphQL APIs and the
MCP endpoint are all served by one process from an in-memory store.

## Run it

Trial lifecycle (what the harness runs):

```sh
python3 main.py          # the platform runs: python3 /bot/main.py
```

1. Binds an ephemeral TCP port (never 8080 — the env owns it).
2. POSTs `{"port": N}` to `$MAKEFUN_ENV_URL/register` (retried with backoff;
   a 200/409 means registered, 400/405 is fatal).
3. Serves until `GET $MAKEFUN_ENV_URL/done` answers `{"done": true}`, then
   exits 0.

Standalone development mode: with `MAKEFUN_ENV_URL` unset, the server just
serves until interrupted and prints its port — point the kit's local runner
at it:

```sh
MAKEFUN_SHOP_TOKEN=dev-token python3 main.py &
node --experimental-strip-types sdk/run.ts \
  --url http://127.0.0.1:<port> --token dev-token --perf
```

Environment variables:

| Variable | Meaning |
| --- | --- |
| `MAKEFUN_ENV_URL` | the env's origin (register/done); unset = standalone |
| `MAKEFUN_SHOP_TOKEN` | the bearer token every API call must present |
| `MAKEFUN_TRIAL_NOW` | the trial's opening time; the only "now" responses carry |

## What it serves

- **Git smart HTTP** at `/<owner>/<repo>.git/…`
  - `GET info/refs`, `POST git-upload-pack`, `POST git-receive-pack`.
  - Protocol **v2** when the client sends `Git-Protocol: version=2`
    (capability advertisement, `ls-refs` with `ref-prefix`/`peel`/`symrefs`/
    `unborn`, `fetch` with acknowledgments, shallow-info and a sideband-framed
    packfile section), protocol **v0** otherwise (also for receive-pack).
  - Shallow fetch/deepen with a correctly moving shallow boundary,
    fast-forward-only branch updates (`ng … non-fast-forward`), per-ref
    report-status in band 1, annotated tags with peeling.
  - Objects are real git objects: SHA-1 over `"<type> <len>\0" + content`,
    packs written plain (v2) and read with OFS/REF delta resolution, so
    clones round-trip byte-for-byte. A stock `git` CLI can clone, push,
    delete refs and `ls-remote` against it.
- **GitHub REST v3** under `/api/v3`
  - Root document and `/meta`; `GET /user`; repositories (create/list with
    `Link` pagination/read/patch/delete); branches and `POST /git/refs`;
    commits; the contents API (create/update with stale-sha `409`, delete,
    directory listings, `?ref=`); labels; issues (create/list with state
    filter and created sort, read, patch, comments); pull requests (create,
    list, read, `/files` with line-diff counts, and `PUT …/merge` with a real
    three-way merge — squash or merge commit, `405` on conflict).
  - Repositories belong to the login the token names: the same name under
    another owner is `404` (never `403`); no token is `401`; every reply is
    JSON with an `X-GitHub-Request-Id`; unknown routes are JSON `404`s and a
    malformed JSON body is a `400`.
  - The contents API, branches and pull merges all operate on the same git
    object store the smart-HTTP surface serves, so a file committed through
    REST is the same bytes a clone receives.
- **GraphQL v4** at `POST /api/graphql`
  - A small hand-written parser/executor for the documents `gh` and
    github-mcp-server send: `viewer`, `repository` (fields, `issue`,
    `issues` with states/orderBy/cursor pagination, `pullRequest`,
    `pullRequests`), `node(id:)` with opaque, stable Relay ids,
    `__type` introspection for Issue/PullRequest/Repository/SearchType,
    the `createIssue`/`closeIssue`/`createPullRequest`/`mergePullRequest`/
    `addComment` mutations, operation batching (a JSON array of documents),
    and GitHub-style field errors (HTTP 200 with an `errors` array naming
    the field).
- **MCP** at `POST /mcp`
  - JSON-RPC 2.0 over streamable HTTP. `server/discover` answers the
    2026-07-28 revision (with the legacy `initialize` handshake also
    supported); `tools/list` is byte-stable across calls; the six
    github-mcp-server tools — `create_repo`, `get_repo`, `create_issue`,
    `list_issues`, `create_pull_request`, `merge_pull_request` — act on the
    same store as REST, so objects created over MCP read back identically
    over `/api/v3`. Unknown tools answer `-32602`, a non-JSON body answers
    HTTP 400 with `-32700`, and a missing token answers `401`.

## Design notes

- **One store, four surfaces.** Repos, refs, objects, issues and pulls live
  in process memory (`Forge`/`Repo` in `main.py`). Issues and pull requests
  share one number sequence per repository, as on GitHub.
- **Determinism.** A trial is replayed and the export compared byte-for-byte,
  so responses never carry wall-clock time or randomness: timestamps derive
  from `MAKEFUN_TRIAL_NOW` plus a monotonic counter, request ids and object
  ids are pure functions of the request stream, and packs are built in
  sorted order.
- **Speed.** Threaded HTTP/1.1 with keep-alive, `TCP_NODELAY` and
  single-write responses (the default Nagle behaviour costs ~40 ms per
  request behind a client's delayed ACK — fatal to the 2,000-read
  workload). Repository state is guarded by per-repo locks; git object
  graphs are immutable once stored.

## Local score (kit runner, this machine)

120/120 conformance checks (git 30, repos 15, issues 15, pulls 20,
graphql 20, mcp 15, protocol 5) — feature 600 + agent 200, plus 157–175
perf points across the four timed workloads in local runs.

## License

MIT — see `LICENSE`.
