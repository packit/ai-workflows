import pytest

from ymir.agents.triage_agent import _should_update_jira
from ymir.common.models import Resolution


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.REBASE,
        Resolution.BACKPORT,
        Resolution.REBUILD,
        Resolution.NOT_AFFECTED,
        Resolution.POSTPONED,
        Resolution.OPEN_ENDED_ANALYSIS,
        Resolution.CLARIFICATION_NEEDED,
        Resolution.ERROR,
    ],
)
def test_user_triggered_always_posts(resolution):
    """A maintainer-triggered run always gets a comment, regardless of resolution."""
    assert _should_update_jira(resolution=resolution, user_triggered=True) is True


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.REBASE,
        Resolution.BACKPORT,
        Resolution.REBUILD,
    ],
)
def test_non_user_triggered_skips_comment_when_mr_will_be_opened(resolution):
    """Without ymir_todo, runs do not comment when an MR will be opened —
    the MR itself is the user-visible artifact."""
    assert _should_update_jira(resolution=resolution, user_triggered=False) is False


@pytest.mark.parametrize(
    "resolution",
    [
        Resolution.NOT_AFFECTED,
        Resolution.POSTPONED,
        Resolution.OPEN_ENDED_ANALYSIS,
        Resolution.CLARIFICATION_NEEDED,
    ],
)
def test_non_user_triggered_still_posts_when_no_mr_will_open(resolution):
    """Resolutions that do not produce an MR must still post a comment —
    otherwise the result is invisible to the requester."""
    assert _should_update_jira(resolution=resolution, user_triggered=False) is True


def test_non_user_triggered_error_does_not_post():
    """ERROR is handled by separate error-path machinery, not this helper."""
    assert _should_update_jira(resolution=Resolution.ERROR, user_triggered=False) is False
