# MR Review

Agentic merge request review support for the osci-pipelines (RoG)
pipeline: the two privileged halves of the
[safe-outputs](https://github.github.com/gh-aw/reference/safe-outputs/)
split, where the review agent itself (the
[agenthome](https://gitlab.com/redhat/centos-stream/ci-cd/osci-pipelines-config/-/tree/main/agenthome)
image) never holds a GitLab token.

## Modules

**`ymir.mr_review.gate`** — the opt-in gate, used as a dynamic
precondition. Exit 0 = the review is enabled for this MR, exit 1 = it
is not (one-line reason on stderr; callers must treat it as "skip
cleanly"). Enablement: the `feature::agentic-review::off` MR label
wins, `::on` forces, otherwise the per-package opt-in in this
repository's rules namespace
(`redhat/centos-stream/rules/<package>`, a top-level
`review: enabled: true` in `ymir.yaml`, parsed with PyYAML and failing
closed on anything malformed).

**`ymir.mr_review.post`** — the token-holding executor. Reads the
JSON review record the agent emitted via `safe-output review`
(`safe-outputs/review.json`), publishes the `## Summary`-onward body
as an MR note (editing the previous note in place — GitLab has no
"minimize"), and enforces a provenance trailer (implementation,
agenthome image, run logs) on every posted note. GitLab API calls use
backoff so rate limits cannot fail the job.

## Image

`Containerfile.mr-review` builds `quay.io/jotnar/mr-review`
(published by `.github/workflows/build-and-push.yml` on push to
`main`). Consumers should pin an immutable tag (commit SHA) — see the
osci-pipelines `rog/` wiring for the reference usage.
