# Security and limits — this branch

No threat-model rewrite. Limits that this change does not remove:

- Official conformance and Failure Lab findings stay different vocabularies. A requirement-set failure is not by itself a vulnerability, and a `SECURITY_RELEVANT` harness classification is not a CVE.
- The adapter trusts the pinned CLI's exit code for `--requirements`. It does not keep a second copy of the not-scored list. A bug in that exit code would be repeated here. Check totals still include not-scored failures, so the counts and the outcome can disagree in the not-scored direction. See `A-P1-6`.
- Client scenarios, Docker containment, and newer SDK releases were not executed. Absence of a failing run is `NOT_MEASURED`.
- `npm audit` reported a critical advisory in the locked conformance install and in the locked TypeScript target install. The advisories were not reviewed and the lockfiles were not changed.
- The public CI secret scan false red is a scanner defect. It is not evidence that a live credential is in the tree. This audit did not print the matched strings.
- Branch protection was not read (HTTP 403) and was not changed.
- No secrets were added. Release docs avoid workflow-run URLs, private finding identifiers, and absolute home-directory paths.
