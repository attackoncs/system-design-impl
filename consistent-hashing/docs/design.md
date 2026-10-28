# Chapter 6 implemented design

The source is System Design Interview.md lines1508–1734. The chapter describes
placement; systems cited as applications are not extra implementations to build.
[Review](chapter06-review.md) · [measurements](../benchmarks/REPORT.md).

| Book topic | Implementation and evidence |
|---|---|
| Modulo rehashing | chapter_comparison.py compares ten-to-eleven expansion |
| Ring space, clockwise lookup | SHA-1 full160 bits, wrapping binary search |
| Server/key mapping, local changes | RingSnapshot and add/remove locality properties |
| Virtual nodes and unequal partitions | Default/per-node counts, weight updates, distribution samples |
| Memory/balance tradeoff | 1/100/200-vnode experiment; measured construction peak |
| Find affected ranges/keys | affected_ranges, iter_migrations and get_keys |
| Scaling/hotspots | Placement only; explicitly no split of a single hot key |

## Boundaries and ownership

ring.py owns immutable _RingState and RingSnapshot. State contains sorted
(position,node,index) entries, positions and insertion-ordered node names.
Publication uses a short lock; writers serialize separately. Hashing/sorting
never hold the publication lock, so slow writers do not block old-view readers.
Writer reentry from the same hash callback raises; callbacks must not wait for
other writers either. Hashes must remain stable and thread safe for all views.

Construction freezes and validates the membership sequence, checks total budget,
hashes all entries and sorts once. Add/grow prepares a sorted added run; CPython's
native adaptive sort merges two ordered runs, avoiding the slower Python heap
iteration found in the first review measurement. Remove/shrink retain entry
order; removal separately sorts removed entries by index for API compatibility.
Only after all work succeeds is the new state published. The default1,000,000
entry budget is checked against the latest state under the writer lock.

set_virtual_nodes grows/shrinks contiguous index prefixes in one publication.
It never rehashes retained entries or exposes a remove/add intermediate state.
Old views retain topology memory; the entry cap does not cap all retained views.

## Read/analysis APIs

Live routing captures one view per call. iter_assignments captures at method call,
then consumes supplied keys lazily. get_keys filters candidates on one view;
there is no key registry. compute_distribution captures a view before reading
its input and counts actual consumed samples, supporting one-pass iterables.
Duplicate sample keys are counted, not silently deduplicated.

Hash input/output validation is shared in hash_functions.py. A callable with a
false boolean value is still used; only None selects the default. Node containers
must be sequences, not a single string or set. Custom integer positions may be
signed; built-ins use their complete digest widths.

## Migration protocol

affected_ranges requires explicit snapshots with the same callable identity.
It merges position boundaries, then walks both ordered topologies once using
monotonic cursors. Equal positions are skipped together, matching historical
strict-clockwise bisect_right. Results are disjoint [start,end) ranges; wrap and
whole-ring cases are represented directly. Empty owners are None; adjacent
ranges remain separate. No arbitrary boundary sort/hash recomputation or per-
boundary owner binary search is needed after the ordered-run merge.

iter_migrations constructs the range plan immediately and stores interval starts.
For each caller-supplied candidate it hashes once, finds the predecessor start
with binary search, and verifies interval containment before yielding immutable
KeyMigration. This covers both sides of wrap intervals and gaps between changes.
No-change plans consume none; duplicate candidates remain duplicate records.
Cost: planning plus O(keys * log(max(2,R))) for R changed ranges; output streams.

Legacy compute_redistribution ignores missing/unassigned pairs by default. Its
strict=True mode rejects missing keys/invalid owner types, and counts explicit
None transitions. It returns one moved-map entry per distinct key. Supplied maps
are caller-owned and must remain stable; use immutable-view migration selection
when analyzing concurrent ring changes.

## Costs and proof

Single lookup O(log V), distinct candidates O(log V+V) worst case, construction
O(V log V), removal O(V+K log K), shrinking O(V). Add/grow sorts K new entries
and merges retained/new ordered runs; the native CPython two-run merge is linear.
Planning merges boundaries and sweeps in O(V_before+V_after+B) on CPython for B
boundaries. Conservatively sorting costs O(V log V) on other implementations.
All costs additionally include application hash time. Published state is O(V).

154 tests include previous Hypothesis properties and46 added regressions. A
separate four-process probe compares three built-in hashes at initial/weighted/
expanded/reduced phases with different membership insertion orders. This proves
same-input placement for those cases, not distributed membership coordination.
The measured lookup/addition regressions are retained alongside build/plan gains.

Membership discovery, consensus, failure detection, transfer execution, replica
copying and cross-machine load tests are not provided. Primary range plans do
not describe all replica-set changes. Historical strict-clockwise equality
behavior is retained; changing it would repartition exact-hash keys.
