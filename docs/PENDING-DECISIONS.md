# Pending Decisions

**One single file, edited in place — not one per branch, not scattered across ticket docs.**
When I (the agent) hit a fork where the call is genuinely yours — not an engineering design
note (those still go in `docs/decisions/`, per CLAUDE.md's ADR convention) but a real
tradeoff, priority, or policy call — I add a numbered entry below using the template. I fill
in sections 1-3 (the question, the constraints, and an honest scoring of the realistic
options). You fill in, edit, or overwrite section 4 with the actual call. I don't act on an
entry until section 4 has your decision in it; until then it just sits here as a live,
readable to-do rather than blocking chat. Once I've acted on a decision, I move it to
"Resolved" below with a one-line note on what happened.

Template, for reference:

1. **Core Question** - the issue (<=4 sentences) and what success looks like.
2. **Non-Negotiables** (max 3) - hard boundaries any choice must respect.
3. **Score the Top 3 Options** - rated 1-5 on the factors that actually matter here.
4. **Pull the Trigger & Mitigate** - the choice, the rollback/safety net, the first concrete
   next step and who/when.

---

## Open

### D1 - Residential data retention & access-tier defaults for P3-12

**Raised:** 2026-09-30, from P0-12's self-conducted synthetic review (branch
`P0-12-privacy-security-review`, commit `06ba566`, Part 3).

**1. Core Question**
- *The Issue:* No real privacy/security review has happened for P0-12 (booking one is human-owned
  and hasn't occurred). Its synthetic substitute proposes concrete retention periods and an
  access-tier design for residential data, but they're this agent's defaults, not policy.
  P3-12 ("Privacy controls for residential data," Gate 3) depends on P0-12 and needs real
  numbers to build against, not a placeholder.
- *The Goal:* A retention/access policy P3-12 can implement once, that's defensible enough to
  survive a real review later without a redesign - even if specific numbers get corrected.

**2. Non-Negotiables**
- Must not require any real residential data outside prod regardless of numbers chosen
  (invariant 8 is already satisfied either way).
- Must give P3-12 one specific number per data category to implement against, not a range.
- Must be stored as config, not hard-coded, so a real reviewer's correction later is a
  one-line change, not a re-implementation.

**3. Score the Top 3 Options** (Ease of adopting now / Legal-risk-if-wrong / Downstream unblock value; 1-5 each)

| Option | Ease | Risk-if-wrong (5=safest) | Unblock value | Total |
| --- | --- | --- | --- | --- |
| A: Adopt the agent's proposed defaults as-is (13mo raw / 24mo features / 90d post-closure grace / min cohort n=20) | 5 | 3 | 5 | 13 |
| B: Use shorter, more conservative defaults (6mo / 12mo / 30d / n=30) pending real review | 4 | 4 | 4 | 12 |
| C: Block P3-12 entirely until a real privacy review is booked and held | 1 | 5 | 1 | 7 |

**4. Pull the Trigger & Mitigate**
- *The Choice:* **[awaiting your call - A, B, C, or your own numbers]**
- *The Safety Net:* *(my suggestion, overwrite freely)* store retention/cohort-size as config
  values (not hard-coded), so tightening or loosening them later is a one-line change and never
  requires touching already-purged data.
- *Next Step:* Once filled in, I'll write a real `docs/decisions/NNNN-...md` ADR encoding the
  chosen numbers and wire P3-12's design against it.

---

### D2 - Start P2-01 / P2-02 against P0-11's new synthetic fixtures now, or wait?

**Raised:** 2026-09-30, from P0-11's synthetic substitute (branch `P0-11-data-access-requests`,
commit `34990e3`: fixture generators for RMA, tickets, dispatch, outage, provisioning).

**1. Core Question**
- *The Issue:* P2-01 (device history dimension) and P2-02 (external source ingest) are both
  "To do" and both `Depends: P0-11` only. Real P0-11 access still doesn't exist, but synthetic
  sample extracts now do. I can build P2-01/P2-02 against the synthetic fixtures the same way
  every other synthetic-proxy ticket this session was built, or hold off.
- *The Goal:* Decide whether Phase 2 work should keep expanding now, in parallel with you
  reviewing everything else, or pause here.

**2. Non-Negotiables**
- Any P2-01/P2-02 work built this way is explicitly validated only against synthetic P0-11
  data and must be re-validated once real extracts land - same caveat pattern as everything
  else this session.
- Stays within this session's standing rules: separate worktree/branch per ticket, no push,
  no merge, `Build backlog.md` not touched without your review.

**3. Score the Top 3 Options** (Ease / Impact / Session-cost-if-wrong; 1-5 each, higher=better except cost is reversed - shown as "Cost" where 5=cheapest)

| Option | Ease | Impact | Cost (5=cheapest) | Total |
| --- | --- | --- | --- | --- |
| A: Start both P2-01 and P2-02 now, same pattern as this batch | 4 | 5 | 3 | 12 |
| B: Wait for you to review this file and the last batch first | 5 | 2 | 5 | 12 |
| C: Do just one (P2-01, the smaller of the two) as a trial | 4 | 3 | 4 | 11 |

**4. Pull the Trigger & Mitigate**
- *The Choice:* **[awaiting your call - A, B, C]**
- *The Safety Net:* *(my suggestion)* same as always - separate branches, nothing pushed or
  merged, trivially discardable if the synthetic P0-11 shape turns out wrong.
- *Next Step:* Once filled in with A or C, I'll open the relevant worktree(s) next turn.

---

## Resolved

*(none yet)*
