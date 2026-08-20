# The factory asks the external reviewer to look again, rather than waiting for it

The external reviewer is incremental. After a push it decides for itself whether
the new commit is worth a review, and frequently concludes it is not, saying
nothing. [ADR-0003](./0003-progress-is-read-from-artifacts.md) requires artifacts
pinned to the current revision, so a pipeline that waits passively for a review of
the current head waits forever on any such commit — with no error, no failed run,
and nothing in the log except a stage that never becomes ready. Two items sat that
way for over six hours before anyone noticed.

We decided the factory asks. When the head has moved and the reviewer has not
spoken since it landed, the decider posts a request naming the revision and holds
until an answer arrives. The request is for a **full** review, not the default
incremental one, because the incremental form looks only at what came after the
last commit it covered and will not revisit findings it raised earlier — which is
exactly what a verification pass needs it to do.

Asking is treated as a move, not as a stall. A hold that has just posted a request
is explicitly not terminal and must not park the item, or the request and the stop
would land in the same tick and the answer could never be acted on.

## Consequences

- A promise is not a review. The reviewer answers a request within seconds with an
  acknowledgement that it will begin, and that acknowledgement must never be
  counted as coverage — reading one as a delivered review once let the factory
  attempt a merge while the review was still being written.
- The request is posted once per head, deduplicated on the revision. A request per
  tick would spend the reviewer's quota and bury the pull request in noise.
- The decider spends external-reviewer quota on items it did not itself launch, so
  the cost of a stuck item is no longer zero. The one-request-per-head rule is what
  bounds it.
- Findings raised against an older commit are not softened when the reviewer
  declines to re-pin. They still block; the factory asks for a ruling on the current
  code instead of quietly ageing them out.
- The request belongs at the moment the head moves, not at the moment the decider
  notices. Posted late, the reviewer's turnaround is serialised after the review
  stage instead of overlapping it, and the review stage can spend a full run judging
  a head the reviewer has not seen.

## Considered alternatives

Requiring a review pinned to the head before the fix stage may run; rejected because
it is precisely the condition the reviewer will not always satisfy, and it produces a
silent indefinite hang rather than a failure anyone can see. Timing out the wait and
proceeding; rejected because how long the reviewer takes is not knowable, so any
timeout either merges unreviewed code or fires on a reviewer that was merely slow —
the trigger has to be the terminal state, not a clock.
