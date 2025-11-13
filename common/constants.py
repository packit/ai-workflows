from enum import Enum

BREWHUB_URL = "https://brewhub.engineering.redhat.com/brewhub"


class RedisQueues(Enum):
    """Constants for Redis queue names used by Jotnar agents"""
    TRIAGE_QUEUE = "triage_queue"
    REBASE_QUEUE_C9S = "rebase_queue_c9s"
    REBASE_QUEUE_C10S = "rebase_queue_c10s"
    BACKPORT_QUEUE_C9S = "backport_queue_c9s"
    BACKPORT_QUEUE_C10S = "backport_queue_c10s"
    CLARIFICATION_NEEDED_QUEUE = "clarification_needed_queue"
    ERROR_LIST = "error_list"
    NO_ACTION_LIST = "no_action_list"
    COMPLETED_REBASE_LIST = "completed_rebase_list"
    COMPLETED_BACKPORT_LIST = "completed_backport_list"
    REBASE_QUEUE = "rebase_queue"
    BACKPORT_QUEUE = "backport_queue"

    @classmethod
    def all_queues(cls) -> set[str]:
        """Return all Redis queue names for operations that need to check all queues"""
        return {queue.value for queue in cls}

    @classmethod
    def input_queues(cls) -> set[str]:
        """Return input queue names that contain Task objects with metadata"""
        return {cls.TRIAGE_QUEUE.value, cls.REBASE_QUEUE_C9S.value, cls.REBASE_QUEUE_C10S.value,
                cls.BACKPORT_QUEUE_C9S.value, cls.BACKPORT_QUEUE_C10S.value, cls.CLARIFICATION_NEEDED_QUEUE.value,
                cls.REBASE_QUEUE.value, cls.BACKPORT_QUEUE.value}

    @classmethod
    def data_queues(cls) -> set[str]:
        """Return data queue names that contain schema objects"""
        return {cls.ERROR_LIST.value,
                cls.NO_ACTION_LIST.value, cls.COMPLETED_REBASE_LIST.value,
                cls.COMPLETED_BACKPORT_LIST.value}

    @classmethod
    def get_rebase_queue_for_branch(cls, target_branch: str | None) -> str:
        """Return appropriate rebase queue based on target branch"""
        if target_branch and cls._use_c9s_branch(target_branch):
            return cls.REBASE_QUEUE_C9S.value
        return cls.REBASE_QUEUE_C10S.value

    @classmethod
    def get_backport_queue_for_branch(cls, target_branch: str | None) -> str:
        """Return appropriate backport queue based on target branch"""
        if target_branch and cls._use_c9s_branch(target_branch):
            return cls.BACKPORT_QUEUE_C9S.value
        return cls.BACKPORT_QUEUE_C10S.value

    @classmethod
    def _use_c9s_branch(cls, branch: str) -> bool:
        """Check if branch should use c9s container"""
        branch_lower = branch.lower()
        # use c9s for both RHEL 8 and 9
        return any(pattern in branch_lower for pattern in ['rhel-9', 'c9s', 'rhel-8', 'c8s'])


class JiraLabels(Enum):
    """Constants for Jira labels used by Jotnar agents"""
    REBASE_IN_PROGRESS = "jotnar_rebase_in_progress"
    BACKPORT_IN_PROGRESS = "jotnar_backport_in_progress"
    NEEDS_ATTENTION = "jotnar_needs_attention"
    NO_ACTION_NEEDED = "jotnar_no_action_needed"

    REBASED = "jotnar_rebased"
    BACKPORTED = "jotnar_backported"
    MERGED = "jotnar_merged"

    REBASE_ERRORED = "jotnar_rebase_errored"
    BACKPORT_ERRORED = "jotnar_backport_errored"
    TRIAGE_ERRORED = "jotnar_triage_errored"

    REBASE_FAILED = "jotnar_rebase_failed"
    BACKPORT_FAILED = "jotnar_backport_failed"

    RETRY_NEEDED = "jotnar_retry_needed"
    FUSA = "jotnar_fusa"

    @classmethod
    def all_labels(cls) -> set[str]:
        """Return all Jotnar labels for cleanup operations"""
        return {label.value for label in cls}


GITLAB_MR_CHECKLIST = """ # Jötnar MR Review Checklist

## ✅ Pre-Merge Tasks

### 📋 Jira Fields Verification

- [ ] **Fix Version/s**: Verify that the fix version chosen by Jötnar is correct
- [ ] **Testing**
  - [ ] **Test Coverage**: Required for Release Pending status (Manual, Automated, or RegressionOnly)
  - [ ] **Preliminary Testing**: Set to "Pass" after pre-merge testing has been done by a team member according to the test coverage choice.
  - [ ] If **Preliminary Testing** was *Manual*, add a note to the issue that describes the justification for this value and any manual test done.
- [ ] **Product Documentation Required**: Set to "Yes" or "No" (No if no user facing change  - use this value almost always for Project Jötnar)
- [ ] **Release Note Fields**: If documentation is required, ensure Release Note Type, Text, and Status are set.

### Automated Checks

- [ ] **ROG CI Pipeline**: All automated tests pass
   - for any failures, it might be useful to check the previous MRs to see if the test failures are not expected, based on the comments
- [ ] **CentOS Stream Build**: Successful scratch build
- [ ] **RHEL Build**: Successful scratch build
- [ ] **Gitbz Check**: Commit messages correctly associated with approved Jira ticket (commit messages use "Resolves: RHEL-XXXXX" format)
- [ ] **Labels**: `target::latest` is set, exception or zstream are set only for exceptions or 0day, please consult Veronika Kabatova for that
    - you can utilise draft builds if you don’t want to trigger builds after merging manually, by labelling the MR with `feature::draft-builds::enabled` and clicking `Run pipeline` to rerun the pipeline
- [ ] **Branch References**: depending on the existing branch references and the process (RHEL-X Z-stream/0day workflow checklist, CentOS Stream X (RHEL-X Y-stream) workflow checklist, RHEL maintenance phase, RHEL Hotfix Build) you want/need to follow you may need to create new branches.
- [ ] **Upstream Alignment**: Changes align with upstream practices

### 🔍 MR Code Review

- [ ] **Upstream Patch Verification** (Jötnar-specific):
  - [ ] Source of upstream patch is trustworthy and patch is correct
  - [ ] Patch is applied correctly (does it capture the important parts of the change, and not add anything)
  - [ ] Patch picked by triaging is complete (e.g. not just one commit from pull request addressing the issue)
- [ ] **Rebases**
  - [ ] (especially on the Z-stream), is this rebase fully backwards compatible, or are there changes that might affect packages that depend on this one or customers
- [ ] Specfile related checks
  - [ ] %release incremented correctly
  - [ ] the newly added patch set correctly? (number, placement)
  - [ ] are added/removed patches from specfile also added/removed as files in dist-git
  - [ ] new %changelog entry is valid
- [ ] If any patch is removed it has to be removed both from the specfile and the dist-git
- [ ] For FuSa packages (related Jira issues have `jotnar_fusa` label) request review and wait for approval from a package maintainer.

## ✅ Post-Merge Tasks

- [ ] **Trigger Builds**: manually trigger regular builds (or/and hotfix build if needed)
   - if using draft builds (feature::draft-builds::enabled label), this step is not needed
- [ ] **Gating Results**: reviewed and waive/fix failures

### Verify automated steps

- [ ] **Gating Process**: Build picked up for gating
- [ ] **Errata Tool**: errata created (requires Preliminary Testing: Pass)
  - [ ] **Release Date**: check the errata `Release Date` which in case of CVE should be ASAP, if not ask in [#forum-rhel-program](https://redhat.enterprise.slack.com/archives/C04S8PHPXH7)

## 🤖 Jötnar specific tasks

If everything went well:
- [ ] Remove `jotnar_needs_inspection` label from issue and merge request if any
- [ ] Remove the issue from the [jotnar todo list](https://issues.redhat.com/issues/?filter=12480549)
- [ ] Add the issue to the [jotnar handpicked list](https://issues.redhat.com/browse/RHEL-118425?filter=12481077 )

## 📋 Process specific workflow checklists

### [RHEL-X Z-stream/0day workflow checklist: Z stream branch has not been forked from the rhel x main branch](https://one.redhat.com/rhel-development-guide/#_z_stream_branch_has_not_been_forked_from_the_rhel_x_main_branch)

  - [ ] **write**: commit goes to cXs
  - [ ] **pre-merge**: *verify that the rhel-X-main branch is not also referencing the Z-stream branch.* If the Z-stream branch has not been forked yet, you need to create (fork it from `rhel-X-main`) and push it to the RHEL-X repository: `git push origin rhel-X-main:rhel-X.Y.0` (RHEL 10 and newer do not use the last number e.g. rhel-10.0).
  - [ ] **build**: **create a CentOS Stream build with the RHEL `rhel-<Y-stream-pre-GA-version>-z-candidate` tag**. Use command `centpkg build --rhel-target=zstream` for it. The RHEL build will be created automatically

### [RHEL-X Z-stream/0day workflow checklist: Z-stream branch HAS been forked from the rhel-X-main branch](https://one.redhat.com/rhel-development-guide/#_z_stream_branch_has_been_forked_from_the_rhel_x_main_branch)
  - [ ] **write**: one MR against RHEL repository Z-stream branch and one against cXs (to be merged after the rhel one is shipped)
  - [ ] **rhel builds**: **build the package from the Z-stream** branch using the `rhpkg build` command in the RHEL repository of the package.
  - [ ] **verify**: check an advisory has been created
  - [ ] **centos build**: **once the erratum is shipped**, submit the CentOS Stream build from the `cXs` branch using the `centpkg` command  with the option `--rhel-target=none`

### [CentOS Stream X (RHEL-X Y-stream) workflow checklist](https://one.redhat.com/rhel-development-guide/#_centos_stream_x_rhel_x_y_stream_workflow_checklist)
  - [ ] **write**: the commit goes to `cXs`
  - [ ] **pre-merge**: *verify that the rhel-X-main branch is not also referencing the Z-stream branch*. If the Z-stream branch has not been forked yet, you need to create (fork it from rhel-X-main) and push it to the RHEL-X repository. In the example you can achieve it by using a command like `git push origin rhel-8-main:rhel-8.8.0` (RHEL 10 and newer do not use the last number e.g. rhel-10.0).
  - [ ] **build**: create a build from the `cXs` production branch using `centpkg build` command. (When the CentOS Stream X build is successful, automation will trigger an automated RHEL-X Y-stream brew build).
  - [ ] **verify**: check an advisory has been created

### [RHEL (8 as of now) maintenance phase](https://one.redhat.com/rhel-development-guide/#_rhel_x_10_z_specific)
  - [ ] **write**: the commit goes to `cXs`
  - [ ] **build**: create only the RHEL build from `rhel-X-main` with `rhpkg build --target=rhel-Y.10.0-z-candidate`
  - [ ] **verify**: after the build is complete, you need to **manually create an Errata Advisory**

### [RHEL Hotfix Build](https://source.redhat.com/groups/public/release-engineering/release_engineering_rcm_wiki/rhel_hotfix_build_process_description)
Jötnar shouldn’t create hotfixes. If it happens follow linked document.

You can find even more detailed checklists in [this document](https://docs.google.com/document/d/1GA_JFyO1unlL74LElaFpB6W7iDWudBocPSzMro6xIOA/edit?tab=t.0), it depends on the process you chose to follow.
Update the MR and pre/post tasks status in [this document](https://docs.google.com/spreadsheets/d/1E4tx1LFdOTEq5lJOiF-JudMLYrc-zKIqgkJx4m7zBnc/edit?gid=1662692521#gid=1662692521).
"""
