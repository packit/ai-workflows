# Rebuild Decision Logic

How Ymir determines whether a package needs to be rebuilt for a CVE dependency fix.

## Decision Flow

1. **Search Jira** for builds with "Fixed in Build" set (closed + active)
   - Closed query failure → `TransientInfrastructureError` (retry)
   - Active query failure → `TransientInfrastructureError` (retry)
   - Follow Jira's `nextPageToken` to fetch every page before selecting a build
2. **Select highest EVR** from closed builds via Koji metadata
   - Any Koji lookup failure → `TransientInfrastructureError` (retry)
   - Prevents selecting from an incomplete candidate set
3. **Verify the release stream** of the selected build
   - Dist-tag pre-filter on the parsed NVR (z-stream fix versions require the minor, e.g. `.el9_8`; y-stream fix versions accept both `.el9` and `.el9_8`; unclassifiable inputs are kept)
   - Authoritative Brew tag membership: any tag under the stream's branch prefix (e.g. `rhel-9.8.0`) proves the build is part of that stream's pipeline
   - No stream tag → clarification (`build_not_tagged_in_stream`)
   - Only candidate/gate-stage tags → fix in progress → clarification (`fix_in_progress_in_stream`)
   - Unperformable tag check (missing build id, Brew failure) → fail open
4. **Determine built architectures** from Koji RPM list
   - Noarch builds: fetch from noarch/ directory
5. **Fetch installed_pkgs.log** for each built architecture
   - Read installed RPM records in `name-[epoch:]version-release.arch timestamp size digest installed` format
   - 404/410 → log genuinely absent (retention policy or very old build), fall back to timestamp comparison
   - 5xx/401/403/429 → `TransientInfrastructureError` (retry)
   - Transport errors → `TransientInfrastructureError` (retry)
   - Server, authorization, transport, or decoding failures → retry
6. **Compare dependency versions**:
   - **installed_pkgs.log available**: Compare EVRs (used vs fixed)
     - Used >= Fixed, **single installable binary** (component produces only one binary RPM after filtering out debuginfo/debugsource, regardless of whether its name matches the source component) → Already Fixed
     - Used >= Fixed, **multiple installable binaries** (component produces more than one binary RPM after filtering out debuginfo/debugsource) → Clarification (cannot determine which binary carries the fix)
     - Used < Fixed → Check for active builds, else Rebuild
   - **installed_pkgs.log unavailable**: Compare build timestamps
     - Built before fix → Check for active builds, else Rebuild
     - Built after fix → Clarification (cannot prove fix without installed_pkgs.log)

## Active Build Handling

Before returning "rebuild needed", checks if active (in-progress) builds exist:
- If active builds exist → Clarification (might be newer rebuild in progress)
- Otherwise → Rebuild

## Multi-Architecture Verification

- Fetches installed_pkgs.log only for architectures actually built (from Koji RPM list)
- Noarch builds: fetches from the noarch/ directory only (accepting whichever logs are present)
- Normalizes epoch values (explicit `0:` equals omitted epoch)
- Verifies all architectures agree on dependency version
- Requests clarification on conflicts

## Result contract

`check_package_built_with_fixed_dependency` returns a `VerificationOutcome`:
`already_fixed` (True/False/None), the selected build's issue key and NEVR, a
`VerificationReason` enum (string-comparable with the reasons below) whose
details live in a structured `payload` (issue keys, tags, subpackages), and the
full `BuildVerificationReport` evidence for clarifications.

## Clarification Reasons

| Reason | When |
|--------|------|
| `built_after_fix_no_installed_pkgs_log` | Package built after fix but installed_pkgs.log unavailable (Brew retention policy) |
| `timestamp_comparison_failed` | Completion timestamps missing in Brew metadata, so the fallback build-order comparison cannot run |
| `architecture_dependency_conflict` | Different architectures show different dependency versions |
| `partial_architecture_coverage` | Dependency missing from some architecture logs |
| `subpackage_match_needs_verification` | Dependency component produces multiple installable binaries (after filtering debuginfo/debugsource) — cannot confirm which binary carries the fix |
| `unresolved_subpackage_needs_verification` | Installed packages from the dependency component could not be resolved to a source build in Brew |
| `invalid_build_records` | Closed Jira builds have mismatched package names in Koji — data inconsistency prevents verification |
| `no_builds_found` | No valid builds found in Jira after filtering (all belonged to other packages) |
| `active_builds_not_closed` | Builds in progress exist (might be rejected before closure) |
| `build_not_tagged_in_stream` | The selected build carries no tag of the fix version's stream (scratch, side-tag, or another stream) |
| `builds_not_in_stream` | Every completed-build reference on the ticket points at another stream while the fix version names this one — whether or not the references resolve in Brew |
| `fix_in_progress_in_stream` | The selected build is only in candidate/gate-stage tags: a fix is being worked on in the stream, not shipped — the user may have to update the referencing issue or its errata once it ships |
| `renamed_binary_lookup_failed` | Brew could not enumerate the renamed binary RPMs of the dependency, so binary RPM name coverage was incomplete when the dependency was not found |

## Infrastructure Failures

Transient infrastructure failures raise `TransientInfrastructureError` for automatic task retry:
- **Jira query failures**: Closed or active build query timeout/connection error
- **Koji metadata unavailable**: getBuild() failures, listRPMs() errors, partial candidate lookups
- **Brew HTTP errors**: 5xx, 401, 403, 429 when fetching installed_pkgs.log
- **Transport errors**: Connection failures, timeouts when fetching installed_pkgs.log

Tasks are retried automatically instead of requesting user clarification.
