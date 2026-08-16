# Progress is read from artifacts, not from whether a run succeeded

The obvious way to know a stage is finished is to ask its run how it ended. We
decided not to: a stage is done when its artifact exists and belongs to the
current revision of the work, and a run's recorded status is consulted only to
explain why a stage is *not* done. Run status answers "did the process end
happily", which is a different question from "did the work land", and the two
disagree in both directions — a run that pushed its fixes and then failed a
health check it should never have run, and a run that ended clean having quietly
reverted the one file it was asked to change.

## Consequences

- Every stage needs a named artifact that outlives its run, and one that is cheap
  to look for: an open pull request on the work branch, a published review
  carrying the revision it judged, a ledger entry naming the review it consumed.
  A stage with no durable artifact cannot be orchestrated.
- Artifacts are pinned to a revision. An unpinned artifact means a review of
  older code keeps a stage looking finished after the code moved on, which is how
  stale approvals turn into false progress.
- Because a fix changes the revision, the review artifact preceding it becomes
  stale by construction, and the item falls back a stage. That is correct, and it
  is why the review-and-fix cycle needs its own stopping rule rather than a
  forward-only sequence.
- Labels showing where an item sits are a projection, recomputed from artifacts
  each tick. Nothing reads them to make a decision, so a wrong one is cosmetic
  rather than a second, competing source of truth.
- Failures still have to be classified, since the run's own record is now the only
  account of them. Infrastructure failures and the work genuinely not being done
  are different events and cannot share one retry budget.

## Considered alternatives

Trusting run status alone, which is exact and trivial to read; rejected because
both observed disagreements would have been decided the wrong way. Requiring both
the artifact and a successful run; rejected because any infrastructure failure
then permanently stalls a stage whose work is already on the branch.
