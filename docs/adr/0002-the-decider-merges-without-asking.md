# The decider merges the work and closes the item, without asking

Unattended orchestration can carry work up to a finished pull request, but
something still has to decide whether that pull request is good. Reviews return
findings, not verdicts, and a meaningful share of those findings are wrong — a
reviewer calling a deliberate out-of-scope omission a critical regression, or
demanding a confirmation that already exists in the diff. We decided the decider
holds real authority: it verifies each blocking finding against the code on the
branch, merges when they do not survive that check, closes the item, and moves
on. It asks for a person only when it is genuinely stuck, or when it would be
undoing its own work a second time.

## Consequences

- The factory writes the base branch. Nothing in the hosting platform prevents
  this, so the limit is policy and must stay in the decider's own procedure.
- The decider's budget for undoing work is one. A first rewind or reset is its
  call; a second means its model of the problem is wrong, and that is the moment
  a person is worth interrupting.
- Automated gates below the decider may never discard work on a severity
  judgement — they hold, and the decider rules. A gate that enacted its own
  rewind once destroyed a pull request that had met most of its requirements and
  had no real defects, on five findings that were all false.
- Escalation needs a channel that reaches a person who is not watching. A message
  to a phone is part of the design, not an add-on.
- The decider spends metered model quota of its own, so it is subject to the same
  credit gate as the workflows it supervises.

## Considered alternatives

Letting the decider judge but stopping short of the merge, leaving a person as
the last step on every item; rejected because that person's remaining job is
clicking a button on a judgement already made, which is the part worth
automating and the part that stalls overnight. Also rejected: no decider at all,
with reviews feeding an automatic merge rule — the false-finding rate makes any
mechanical rule either merge bad work or hold good work forever.
