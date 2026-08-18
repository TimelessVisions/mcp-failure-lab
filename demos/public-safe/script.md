# Public-safe demo — script

**Audience:** public.
**Source:** the public-safe viewer, built from the synthetic fixture.
**Runtime:** 3:08 (the window is 2:45–3:15).

> **Synthetic evidence demonstration. No third-party benchmark result is shown.**
>
> Every implementation in this recording is invented and every number was
> produced by a deterministic fixture. What is real is the method: the schema,
> the scenario library, the applicability rules, and everything the tool refuses
> to claim.

This is not the private demo with the names removed. It is a different
presentation with a different subject — the private demo shows findings, this
one shows *how a finding would be established and what would stop it being
overstated*. Nothing here is an anonymised version of a real result.

The narration below is what the captions say, generated from `timeline.json`,
which is also what drives the capture.

---

## 00:00–00:22 — What this demonstration is

> PUBLIC-SAFE SYNTHETIC EVIDENCE. This demonstration uses invented
> implementations and synthetic measurements. No third-party benchmark result is
> displayed.

The first stage exists to make the rest unmistakable. The build states it in the
chrome of every screen as well, so a frame taken out of context still carries it.

**Say it in your own words too.** The most likely way this demo does harm is
someone screenshotting a matrix cell and circulating it as a result about a real
project.

## 00:22–00:42 — The problem being measured

> Two servers can both pass a happy-path conformance run and still disagree once
> the wire is malformed, the profile is older than the requirement, or the
> process is killed.

The real problem statement. This part is not synthetic — it is the reason the
tool exists.

## 00:42–01:02 — The shape of a measurement

> The scenario library and the repetition policy are the real ones. Which
> invented implementation lands where is a deterministic fixture.

Draw the line clearly: the *structure* is real and inspectable; the *contents*
are invented. Say which is which rather than letting the viewer guess.

## 01:02–01:26 — Applicability, the rule that carries the project

> A requirement dated after the profile a server negotiated is never asserted
> against it: NOT_APPLICABLE with a reason, never a pass, never a failure.

The longest stage in the demo, and the one worth the time. This rule is
identical in the private benchmark; it is the strongest thing that can honestly
be shown to a public audience, because it is a property of the method rather
than of anyone's results.

## 01:26–01:48 — Outcome and security relevance are different fields

> A result can be a normative failure and security-relevant at once, and a
> scenario can be written to probe a security property without anything being
> observed.

Three records — purpose, observation, confirmation — and no execution path
writes the third.

**Say explicitly:** nothing in this demonstration is a security finding about
anyone. The rows are invented.

## 01:48–02:10 — Stability is signature equality

> Two runs that both said "failed" for different reasons are not a stable
> result, and this is the mechanism that notices.

A methodological point that costs nothing to show publicly and is the part most
often got wrong elsewhere.

## 02:10–02:30 — Timings show every sample

> A median with the samples withheld cannot be checked for the one thing that
> would invalidate it.

The sample strip, warm-ups visible and excluded from the median.

## 02:30–02:48 — What was never measured is listed

> A metric absent from a report is one a reader cannot tell was never taken.

The coverage ledger. Note aloud that this is about *timing metrics*: a scenario
with no recorded result is a different state.

## 02:48–03:08 — What the tool refuses to say

> No ranking, no aggregate score, no best implementation. The private benchmark
> that shares this schema is not represented here.

End on the refusals, and on the statement that the private work is not in this
recording in any form.

---

## What must not appear, in the picture or the narration

- Any real implementation, project, package, maintainer or repository.
- Any version, commit, digest, fingerprint or finding identifier.
- Any branch name, workflow run, artifact URL or internal path.
- Any claim that the synthetic results resemble, approximate, or are derived
  from real ones.
- Any suggestion that a private finding exists, including by implication.

The build enforces most of this — the public-safe dataset has been through the
denylist scan, and the walkthrough is asserted to contain no commit-shaped or
finding-shaped string. The narration is the part no scanner checks.
