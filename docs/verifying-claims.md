# Verifying claims

For any agent that rules on other agents' work — the supervisor, a reviewer, a triager. It
is also for ruling on your **own** conclusions, which is where it is hardest to apply.

Your job is not to have opinions about claims. It is to find the fact each claim stands on,
and check that fact at its source.

---

## 1. The core move

Every claim rests on one or two **load-bearing facts**: things that, if false, collapse the
claim entirely. The reasoning around them is usually fine. The facts are what go wrong.

So before accepting or acting on any claim:

1. **Name the load-bearing fact.** "This is true only if X."
2. **Check X where it is defined**, not from memory and not from the surrounding prose.
3. Only then rule.

A claim can be detailed, confident, internally consistent, and written by someone competent,
and still die to one line of code. Detail is cheap to produce and is not evidence.

> **Worked example.** A ruling said: "when the queue is full, enqueue a smaller record
> instead of dropping it." Coherent, specific, argued from a real trade-off. The
> load-bearing fact was *the queue is bounded by bytes*. The declaration said
> `queue.Queue(maxsize=32)` — Python bounds that by **item count**. A full queue rejects any
> item whatever its size. One line refuted the whole ruling. Nobody caught it for hours;
> a question from outside did.

---

## 2. Where the facts go wrong

Seven checks. Each has a trigger — when to run it — and each is cheap. Run the cheapest one
that could refute the claim.

### A. Does the cited thing exist?

**Trigger:** the claim names a file, line, symbol, function, or config key.
**Check:** open it. Grep for the symbol.

Agents fabricate specifics under pressure, and a fabricated citation looks exactly like a
real one. *Seen: a review citing a "launcher-exclusion loop" at a line number in a file
that has neither.*

### B. What happens when the input is empty, missing, or zero?

**Trigger:** anything that gates, guards, validates, matches, or compares.
**Check:** trace the empty case specifically. Read the early returns.

This is where guards fail open. The happy path is usually right; the empty path is usually
unconsidered. *Seen: a credit gate that passed when its provider list came back empty — and
the list came back empty whenever the workflow file was missing or a seat had been renamed.
Also: a staleness check returning True when the stamp it compared was absent, which made the
entire gate inert.*

### C. Is the value used, or only accepted?

**Trigger:** a parameter, flag, or field whose presence is supposed to make something safe.
**Check:** follow it from where it enters to where it is used. Every hop.

A function can take an argument, be handed it correctly by every caller, and never pass it
on. *Seen: `assemble(head_sha=...)` accepted the commit id and called its helper without it,
so every record it wrote was unstamped and the check downstream always passed.*

### D. Does the comment describe the code beneath it?

**Trigger:** reading any commented block, especially one that states a rule.
**Check:** read the code with the comment covered. Then compare.

Comments record intent at the time of writing. Code drifts; comments do not. When they
disagree, the comment tells you what someone *meant*, which is useful — but the code is what
runs. *Seen: "Completing the last stage closes a round" sitting directly above a line that
incremented on launch.*

### E. What are the real semantics of the primitive?

**Trigger:** any claim about capacity, ordering, blocking, atomicity, identity, or equality.
**Check:** the declaration, then the library's own documentation.

This is the one that catches confident experts. Bounded by items or bytes? Compared by value
or identity? Does it block, drop, or raise? Is that timestamp the author date or the commit
date? Is `resolve()` doing I/O? Assumptions here feel like knowledge.

### F. What exactly am I looking at?

**Trigger:** any diff, range, version, branch, or "the current state of X".
**Check:** verify the base and the boundaries before reading the content.

*Seen: a review run against a local `main` that was eight commits stale, so every finding it
produced was about already-merged code and none were about the branch under review. The
findings were real; they were answers to a different question.*

### G. Is the assumption written down?

**Trigger:** a design that depends on its environment — network, hardware, who is present,
what runs where.
**Check:** search the docs for it. If it is not there, that absence is itself a finding.

An undocumented constraint gets re-violated by the next agent, forever. *Seen: a device that
runs on site with no internet and nobody present — which invalidates every retry, alert and
remote-sink design in its subsystem, and appeared in no document.*

---

## 3. The shape that repeats: two states, one signal

Most of what slips past review is not a wrong answer. It is **two different states that
produce identical output**, one healthy and one broken.

Real pairs from this system, all of which printed the same thing:

| Healthy state | Broken state |
|---|---|
| Waiting two minutes for a review | Waiting forever; no review is ever coming |
| The reviewer looked and objected to nothing | Nobody has reviewed this at all |
| A run failed and recorded why | A run succeeded, and the stage re-opened underneath it |
| The environment broke, so retry is free | The agent broke, so retry burns the budget for nothing |
| A gate compared and passed | A gate had nothing to compare and passed |

None of these raised an error. Every one was caught by a person noticing that something had
been in the same state a bit too long.

**So ask, of any mechanism you are reviewing:**

> If this had failed right now, would anything look different?

If the answer is no, that is a defect worth reporting **even when it is not what you were
asked about** — and it is the defect class that matters most, because it is the one that
survives having nobody watching.

The fix is almost always to make the record distinguish the two: name which case it is, say
how long it has been that way, or fail closed so the broken state stops rather than waits.

---

## 4. When to stop checking

Verification is unbounded if you let it be. Bound it deliberately.

- **Check only what is load-bearing.** A fact is load-bearing when the decision changes if it
  is false. Everything else is context and can stay unverified.
- **Prefer the cheapest check that could refute.** One file opened, one grep, one command.
  If a check needs a full run, an environment, or more than about ten minutes, it is not a
  check — escalate instead.
- **One check per fact, not one per sentence.** Several claims usually share a single fact.
  Find the shared one.
- **Budget: about two minutes for most claims.** Of four reviewer disagreements examined in
  one session here, three were settled inside that by reading the source or running a single
  `git` command. The fourth needed a person.
- **Absence of a check is not a pass.** If you could not verify a load-bearing fact, say the
  claim is unverified. Do not round it to accepted or to rejected.

---

## 5. When a check refutes the claim

1. **Say so plainly, once.** No hedging, no ceremony, no re-litigating how it happened.
2. **Correct the durable record, not only the conversation.** A withdrawn ruling left
   standing in an issue thread will be implemented by somebody. Edit it where it lives, mark
   it withdrawn, and point at the replacement. What is in the thread is what gets built.
3. **Ask where else the same assumption is load-bearing.** A wrong belief about a primitive
   is rarely used once.
4. **Keep the reasoning if only the fact was wrong.** Usually the trade-off analysis survives
   and only the mechanism needs replacing. Do not throw away good reasoning along with the
   bad fact.

---

## 6. This applies to you

Your own rulings are claims, and they get the same treatment. There is no separate, more
trustworthy category for conclusions you reached yourself.

Two habits that help:

- **Before you commit to a ruling, state its load-bearing fact out loud and check it.** If
  you cannot name the fact, you do not have a ruling yet — you have a preference.
- **Treat a question you cannot immediately answer as a signal, not an interruption.** "Why
  does it work that way at all?" is the question that has repeatedly exposed the unchecked
  assumption here. When you cannot answer it from the source, go and read the source.

The most confident, most specific, best-argued ruling produced in this project so far was
also the wrong one. Confidence tracks how much reasoning you did, not how much of it rested
on something true.
