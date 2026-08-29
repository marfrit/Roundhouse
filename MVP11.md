# MVP11 — arcint takes :8080 on dirac

**A swap is only a swap if the roster can see both sides of it.** On
2026-08-29 `openarc-coder.service` (OpenArc/OpenVINO GenAI, dirac:8080,
≈43 tok/s) was replaced by `arcint.service`
([arcint](https://git.reauktion.de/marfrit/arcint) 0.2.0-1, package
`a6abb8b`) on the same IR `/models/ov/qwen36-coder-b5-ov` and the same card
GPU.0 = Arc Pro B60 (measured elsewhere: 71.3 tok/s at u8 KV, 10/10 on the
CSV acceptance task — same IR, same card, only the serving pipeline changes,
which is why that is a comparison and not a claim).

## Part 0 — the blocker that was already there (`928f2e8`, unit repo)

`dirac:~/.config/systemd/user/roundhouse.service.d/` was **untracked**: a
hand edit to Roundhouse's own `--actuate` configuration that no commit
covered. The gate only refuses on *tracked* dirty state, so it had not yet
produced the refusal restart-loop — the hole was open, not yet fallen into.
`override.conf` is now tracked. The three `override.conf.bak-*` were removed:
`.gitignore` already declares `*.bak*` non-content (Roundhouse checks for
that rule itself), git now carries the history, and all three variants are
quoted in the commit message. They were hand-made, not rollback anchors —
Roundhouse writes `.roundhouse-tmp`, never `.bak`.

## Part 1 — arcint as an engine kind (T1, `56a9925`)

A `# roundhouse: manage` marker alone is **not** enough for an unknown
basename. Measured on a scratch fixture before writing any code:

```
select_units -> ['arcint.service']   engine -> {}
port -> 8080   alias -> None   ctx -> None
```

With `engine == {}` the kind is `None`: `apply_journal_line` falls through
its `else: return`, and the unit is absent from `OPENAI_PROBE_ENGINES`. A
`Type=simple` unit is then `active` and never `ready`, which by rung rule 6
means **LOADING forever** — `switch_execute` waits for READY and would call
a perfectly healthy service a failure. (One louder hypothesis was checked
and refuted: `engine` is `{}`, not `None`, so the unguarded `.get('kind')`
call sites do not crash the watcher threads. The failure is quiet.)

- `detect_engine`: basename `arcint` → `{kind: arcint, variant: openvino}`.
- `OPENAI_PROBE_ENGINES += arcint`, probe-only like ds4. arcint prints
  `http: listening` *before* the paged executor holds its reservation, so
  that line is deliberately **not** a READY marker.
- `KNOWN_FLAG_MAP`: `--n-ctx` → ctx, `--model-id` → alias (below).
- Default port for arcint is its own 8090, not llama.cpp's 8080.

## Part 2 — the alias does not survive the swap

The brief assumed alias and port both stay put, so no consumer would need
telling. The port did. The alias did not, and no flag can save it:

```
$ arcint --stub --alias qwen3.6-coder --port 9977
arcint: unknown option '--alias' (try --help)
$ arcint --stub --model-id qwen3.6-coder --port 9978
arcint: 'qwen3.6-coder' is not in the allowlist
        (known: qwen3.6-27b-a3b-coder, qwen3.6-35b-a3b, qwen3.8-27b)
```

arcint has **no alias flag at all**, and `--model-id` asserts an allowlist
entry rather than renaming one. Its `/v1/models` reports
`qwen3.6-27b-a3b-coder`; openarc-coder reported `qwen3.6-coder`. Since the
llm-proxy discovers names from the backend's `/v1/models`, the rename
reaches clients.

Hence `--model-id qwen3.6-27b-a3b-coder` written explicitly in the unit and
mapped to `alias`: it is the one string that is simultaneously what arcint
asserts, what `/v1/models` returns, and what a client must send — so the
roster alias cannot drift from reality. The real fix belongs upstream:
`--served-model-name`, which Roundhouse's flag table **already** maps to
alias (added for vLLM), so it would need no parser change here at all.

## Part 3 — the swap (live, dirac)

Preflight refused first, which is the point of having one:

```
port 8080 will still be bound after the plan: openarc-coder.service
(READY, not ticked)                                          → 422
+ notice: systemd Conflicts=: starting arcint.service will stop
  openarc-coder.service (READY)
```

With `openarc-coder` ticked (and `llama-agent`, the A770 unit on :8087,
deliberately **not**), all five checks passed and the switch ran. arcint
served in ~60 s: paged model compiled in 41.5 s, device-resident 12.83 GiB,
reservation 22.71 GiB of the card → max ctx 606688 per lane, running at
n_ctx 262144, 1 lane, prefill chunked at 2048.

`# roundhouse: mem-estimate 8G` is **measured, not guessed**: peak 7.80 GiB
in the unit cgroup at n_ctx 262144. The 22.71 GiB from the boot log is
*card* memory and is deliberately not declared — Roundhouse's arithmetic is
host RAM, and exclusivity on GPU.0 is enforced by `Conflicts=`, not by the
fit check.

Boot policy via `set_boot`: arcint enabled, openarc-coder disabled. That
rewrites tracked symlinks under `default.target.wants/`, leaving a tracked
**deletion** in the worktree — the exact state that makes `--actuate` refuse
and restart-loop. It was committed in the same window, before the next
Roundhouse restart (`NRestarts=0` afterwards).

openarc-coder is retired, not deleted: Description prefixed
`[RETIRED 2026-08-29 -> arcint.service]`, unit installed and disabled.
Rollback stays `systemctl --user stop arcint && systemctl --user start
openarc-coder` — and note that while the tag stands, Roundhouse refuses
openarc-coder as a *switch target*, so a rollback through the MCP needs the
tag dropped first.

Verified after: port board shows :8080 with exactly one live owner (arcint
READY/enabled; llama-coder, llama-qwen38 OFF and disabled; openarc-coder
RETIRED and disabled), `llama-agent` untouched and READY on :8087, and
boltzmann's federated roster carries dirac's arcint fresh. Over the network:
`dirac.fritz.box:8080/v1/models` → one entry, `/props` →
`model.n_ctx = 262144`, `n_ctx_train = 262144`, `stub false`, build
`0.2.0 a6abb8b`.

Boot policy was verified by `is-enabled` plus the presence of the
`default.target.wants/arcint.service` symlink — **not** by an actual reboot
of the CT.

## Part 4 — two premises that did not survive measurement

1. **The `local-ctx.json` override does not take precedence.** Its call site
   is `c = _ctx_of(m) or LOCAL_CTX_OVERRIDES.get(mid)` and its own docstring
   says "Korrektur, nicht Quelle: ein vom Backend gemeldeter Wert gewinnt
   immer". It is a fallback, so it never froze anything.
2. **The proxy does not read ctx from `/props`.** `_fetch_backend_props`
   extracts `chat_template_caps` only, and is called only when `/v1/models`
   stayed silent about parameters.

Removing `{"qwen3.6-coder": 32768}` was still right, for the opposite
reason: arcint's `/v1/models` entry is bare (id/object/owned_by), so nothing
publishes a context length for it, and the day `--served-model-name` restores
the id `qwen3.6-coder`, that stale 32768 would have become **live** and
capped every client. It is now `{}` (backup
`local-ctx.json.bak-vor-arcint-20260829`); `POST /admin/recheck` returned
202 and the proxy logged `cleared caches`. Note that `LOCAL_CTX_OVERRIDES`
is read at import, so the file edit lands on the next proxy restart — it is
inert until then either way.

Open for the proxy side: nothing publishes a context length for the coder
model now. Either arcint carries a ctx field in its `/v1/models` entry, or
`local-ctx.json` is rekeyed to whatever id arcint ends up serving.

## Fleet note

dirac runs this Roundhouse build; **boltzmann and bosch were left on the
previous one** (`368e9c8d…`) — the change is additive and their restart was
not worth the blast radius for this swap. Both peers also carry untracked
files in their unit repos (boltzmann: `classifier.service`,
`default.target.wants/`, `roundhouse.service.d/`; bosch:
`default.target.wants/`), the same hygiene defect fixed on dirac in Part 0,
left for a deliberate decision rather than fixed in passing.

## Part 5 — arcint 0.2.1 closes both gaps (T2, same day)

The engineering ask went out and came back the same day: **arcint 0.2.1-2**
(`c7f84e6`) on packages.reauktion.de ships `--served-model-name` *and* a
populated `/v1/models` entry. Verified against the 0.2.1 stub before
touching production, straight from the prompt's acceptance list:

```
--stub --served-model-name qwen3.6-coder --n-ctx 40960
  -> {"id":"qwen3.6-coder","canonical_id":"qwen3.6-27b-a3b-coder",
      "n_ctx":40960,"n_ctx_train":262144,"quant":"q4","lanes":1}
  /props model.id -> qwen3.6-coder
--stub, no flag        -> id qwen3.6-27b-a3b-coder   (regression guard held)
--model-id qwen3.6-coder -> still refused as not-in-allowlist
```

`n_ctx` is the *served* value and `n_ctx_train` the artifact's — the
distinction the proxy's key order depends on. `canonical_id`, `quant` and
`lanes` came along unasked and reach clients through the same path.

**A precedence rule was needed** (`MVP11 T2`). The unit now carries both
alias sources, and both map to `alias`; plain last-wins made the roster
alias depend on flag order — measured both ways round. `--served-model-name`
now outranks `--model-id` regardless of order and keeps the alias span, so
an edit to `alias` lands on the flag that names the endpoint rather than on
the allowlist assertion. `--model-id` alone is unchanged. 930 tests green.

The production switch ran through Roundhouse (preflight all-green, fit
`8G declared`): served in ~50 s, build `0.2.1 c7f84e6`, and
`dirac.fritz.box:8080/v1/models` now reports **`qwen3.6-coder`** with
`n_ctx 262144`. Proxy recheck fired 202.

Which settles Part 4's open item without any override at all: the proxy
publishes `[local] qwen3.6-coder` with `ctx: 262144` and
`properties.context_window 262144`, read straight from the backend. The
`local-ctx.json` entry stays removed — it is not merely unnecessary now,
it would have been a live 32768 cap on exactly this id.

Net effect for consumers: the alias and the port both survived the swap
after all, one package late.
