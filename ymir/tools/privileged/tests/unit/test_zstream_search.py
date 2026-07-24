from unittest.mock import AsyncMock, patch

import pytest
from beeai_framework.middleware.trajectory import GlobalTrajectoryMiddleware

from ymir.tools.privileged.zstream_search import (
    ZStreamSearchResult,
    ZStreamSearchTool,
    ZStreamSearchToolInput,
    _clean_summary,
    _get_patch_url,
    _version_sort_key,
)

RHEL_CONFIG = {
    "current_y_streams": {
        "9": "rhel-9.8",
        "10": "rhel-10.2",
    },
    "current_z_streams": {
        "8": "rhel-8.10.z",
        "9": "rhel-9.7.z",
        "10": "rhel-10.1.z",
    },
    "upcoming_z_streams": {},
}


# ============================================================================
# Unit tests for helper functions
# ============================================================================


@pytest.mark.parametrize(
    "summary, expected",
    [
        (
            "fence_ibm_vpc: fix missing statuses [rhel-10.0.z]",
            "fence_ibm_vpc: fix missing statuses",
        ),
        (
            "some fix [rhel-9.6.z]",
            "some fix",
        ),
        (
            "fix bug [rhel-8.8.0.z]",
            "fix bug",
        ),
        (
            "no bracket suffix here",
            "no bracket suffix here",
        ),
        (
            "fix [CVE-2025-1234] issue [rhel-9.7.z]",
            "fix [CVE-2025-1234] issue",
        ),
    ],
)
def test_clean_summary(summary, expected):
    assert _clean_summary(summary) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        # GitLab commit URL
        (
            "https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/abc123",
            "https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/abc123.patch",
        ),
        # GitHub commit URL
        (
            "https://github.com/org/repo/commit/def456",
            "https://github.com/org/repo/commit/def456.patch",
        ),
        # Already a patch URL
        (
            "https://github.com/org/repo/commit/abc123.patch",
            "https://github.com/org/repo/commit/abc123.patch",
        ),
        # Unknown URL format
        (
            "https://example.com/some/path",
            "https://example.com/some/path",
        ),
    ],
)
def test_get_patch_url(url, expected):
    assert _get_patch_url(url) == expected


@pytest.mark.parametrize(
    "issue_version, target_major, target_minor, expected",
    [
        # Same major, z-stream, close
        (("9", "7", True), "9", 6, (0, 1)),
        # Same major, z-stream, farther
        (("9", "8", True), "9", 6, (0, 2)),
        # Same major, y-stream
        (("9", "8", False), "9", 6, (1, 2)),
        # Different major
        (("10", "1", True), "9", 6, (2, 5)),
    ],
)
def test_version_sort_key(issue_version, target_major, target_minor, expected):
    assert _version_sort_key(issue_version, target_major, target_minor) == expected


# ============================================================================
# Integration tests for ZStreamSearchTool
# ============================================================================


def _patches(*mock_calls):
    """Common patches for ZStreamSearchTool tests."""
    return (
        patch(
            "ymir.tools.privileged.zstream_search.is_older_zstream",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ymir.tools.privileged.zstream_search.run_tool", AsyncMock(side_effect=mock_calls)),
        patch("ymir.tools.privileged.zstream_search._fetch_mr_commits", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_not_applicable_y_stream():
    """Y-stream fixVersion should return NOT_APPLICABLE."""
    tool = ZStreamSearchTool()
    output = await tool.run(
        input=ZStreamSearchToolInput(
            component="fence-agents",
            summary="fix something",
            fix_version="rhel-9.8",
        ),
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_not_applicable_current_zstream():
    """Current z-stream fixVersion should return NOT_APPLICABLE."""
    tool = ZStreamSearchTool()
    with patch(
        "ymir.tools.privileged.zstream_search.is_older_zstream",
        new_callable=AsyncMock,
        return_value=False,
    ):
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="fence-agents",
                summary="fix something [rhel-9.7.z]",
                fix_version="rhel-9.7.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_not_applicable_invalid_version():
    """Invalid fixVersion should return NOT_APPLICABLE."""
    tool = ZStreamSearchTool()
    output = await tool.run(
        input=ZStreamSearchToolInput(
            component="fence-agents",
            summary="fix something",
            fix_version="invalid",
        ),
    ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.NOT_APPLICABLE


@pytest.mark.asyncio
async def test_found_in_closest_stream():
    """Commits found via merged MR in closest related issue."""
    search_result = [
        {
            "key": "RHEL-99999",
            "id": "12345",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    pr_result = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/fence-agents/-/merge_requests/42",
                "status": "MERGED",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result)
    with p_older, p_run_tool, p_fetch as mock_fetch:
        mock_fetch.return_value = [
            "https://gitlab.com/redhat/rhel/rpms/fence-agents/-/commit/abc123.patch",
        ]
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="fence-agents",
                summary="fence_ibm_vpc: fix missing statuses [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.FOUND
    assert result.source_issue == "RHEL-99999"
    assert result.source_version == "rhel-9.7.z"
    assert len(result.related_commits) == 1
    assert result.related_commits[0].endswith(".patch")


@pytest.mark.asyncio
async def test_cascade_to_further_version():
    """Cascade to y-stream when z-stream has no merged MRs."""
    search_result = [
        {
            "key": "RHEL-11111",
            "id": "11111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
        {
            "key": "RHEL-22222",
            "id": "22222",
            "fields": {
                "fixVersions": [{"name": "rhel-9.8"}],
            },
        },
    ]
    pr_result_open_only = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/fence-agents/-/merge_requests/99",
                "status": "OPEN",
            },
        ]
    }
    pr_result_with_merged = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/centos-stream/rpms/fence-agents/-/merge_requests/10",
                "status": "MERGED",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result_open_only, pr_result_with_merged)
    with p_older, p_run_tool, p_fetch as mock_fetch:
        mock_fetch.return_value = [
            "https://gitlab.com/redhat/centos-stream/rpms/fence-agents/-/commit/def456.patch",
        ]
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="fence-agents",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.FOUND
    assert result.source_issue == "RHEL-22222"
    assert result.source_version == "rhel-9.8"


@pytest.mark.asyncio
async def test_not_found_anywhere():
    """Returns NOT_FOUND when all related issues have only open MRs."""
    search_result = [
        {
            "key": "RHEL-11111",
            "id": "11111",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    pr_result_open_only = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/fence-agents/-/merge_requests/99",
                "status": "OPEN",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result_open_only)
    with p_older, p_run_tool, p_fetch:
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="fence-agents",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.NOT_FOUND


@pytest.mark.asyncio
async def test_no_related_issues_found():
    """Returns NOT_FOUND when Jira search returns empty results."""
    search_result = []

    mock_run_tool = AsyncMock(return_value=search_result)

    tool = ZStreamSearchTool()
    with (
        patch(
            "ymir.tools.privileged.zstream_search.is_older_zstream",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ymir.tools.privileged.zstream_search.run_tool", mock_run_tool),
    ):
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="fence-agents",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.NOT_FOUND


@pytest.mark.asyncio
async def test_version_proximity_sorting():
    """Verify issues are tried in order of version proximity."""
    search_result = [
        {
            "key": "RHEL-FAR",
            "id": "33333",
            "fields": {
                "fixVersions": [{"name": "rhel-9.8"}],
            },
        },
        {
            "key": "RHEL-CLOSE",
            "id": "44444",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    # The closer issue (9.7.z) should be tried first and has a merged MR
    pr_result_with_merged = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/repo/-/merge_requests/1",
                "status": "MERGED",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result_with_merged)
    with p_older, p_run_tool, p_fetch as mock_fetch:
        mock_fetch.return_value = [
            "https://gitlab.com/repo/-/commit/abc.patch",
        ]
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="fence-agents",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.FOUND
    assert result.source_issue == "RHEL-CLOSE"
    assert result.source_version == "rhel-9.7.z"


@pytest.mark.asyncio
async def test_older_issues_excluded():
    """Issues with versions older than target should be excluded."""
    search_result = [
        {
            "key": "RHEL-OLDER",
            "id": "55555",
            "fields": {
                "fixVersions": [{"name": "rhel-9.4.z"}],
            },
        },
        {
            "key": "RHEL-SAME",
            "id": "66666",
            "fields": {
                "fixVersions": [{"name": "rhel-9.6.z"}],
            },
        },
    ]

    mock_run_tool = AsyncMock(return_value=search_result)

    tool = ZStreamSearchTool()
    with (
        patch(
            "ymir.tools.privileged.zstream_search.is_older_zstream",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch("ymir.tools.privileged.zstream_search.run_tool", mock_run_tool),
    ):
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="fence-agents",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))
    result = output.to_json_safe()
    # Both issues should be excluded (older and same version)
    assert result.result == ZStreamSearchResult.NOT_FOUND


# ============================================================================
# New tests for merged MR commit behavior
# ============================================================================


@pytest.mark.asyncio
async def test_multiple_merged_mrs():
    """Commits from all merged MRs are returned."""
    search_result = [
        {
            "key": "RHEL-77777",
            "id": "77777",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    pr_result = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/10",
                "status": "MERGED",
            },
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/11",
                "status": "MERGED",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result)
    with p_older, p_run_tool, p_fetch as mock_fetch:
        mock_fetch.side_effect = [
            ["https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/aaa.patch"],
            [
                "https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/bbb.patch",
                "https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/ccc.patch",
            ],
        ]
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="pkg",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.FOUND
    assert len(result.related_commits) == 3


@pytest.mark.asyncio
async def test_open_mrs_only_returns_not_found():
    """Only OPEN MRs with no merged MRs returns NOT_FOUND."""
    search_result = [
        {
            "key": "RHEL-88888",
            "id": "88888",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    pr_result = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/99",
                "status": "OPEN",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result)
    with p_older, p_run_tool, p_fetch:
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="pkg",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.NOT_FOUND


@pytest.mark.asyncio
async def test_mixed_merged_and_open_mrs():
    """Only merged MR commits are returned when both merged and open MRs exist."""
    search_result = [
        {
            "key": "RHEL-99000",
            "id": "99000",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    pr_result = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/50",
                "status": "MERGED",
            },
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/51",
                "status": "OPEN",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result)
    with p_older, p_run_tool, p_fetch as mock_fetch:
        mock_fetch.return_value = [
            "https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/merged1.patch",
        ]
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="pkg",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.FOUND
    assert len(result.related_commits) == 1
    mock_fetch.assert_called_once_with("https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/50")


@pytest.mark.asyncio
async def test_fetch_mr_commits_error_handling():
    """GitLab API error for one MR doesn't prevent finding commits from another."""
    search_result = [
        {
            "key": "RHEL-99001",
            "id": "99001",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    pr_result = {
        "pull_requests": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/60",
                "status": "MERGED",
            },
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/merge_requests/61",
                "status": "MERGED",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result)
    with p_older, p_run_tool, p_fetch as mock_fetch:
        # First MR fails, second succeeds
        mock_fetch.side_effect = [
            [],
            ["https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/ok.patch"],
        ]
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="pkg",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.FOUND
    assert len(result.related_commits) == 1


@pytest.mark.asyncio
async def test_no_mrs_falls_back_to_dev_status():
    """No MRs at all triggers dev-status fallback for direct-push commits."""
    search_result = [
        {
            "key": "RHEL-99002",
            "id": "99002",
            "fields": {
                "fixVersions": [{"name": "rhel-9.7.z"}],
            },
        },
    ]
    pr_result_none = {"pull_requests": []}
    dev_status_result = {
        "commits": [
            {
                "url": "https://gitlab.com/redhat/rhel/rpms/pkg/-/commit/direct1",
                "message": "direct push fix",
            },
        ]
    }

    p_older, p_run_tool, p_fetch = _patches(search_result, pr_result_none, dev_status_result)
    with p_older, p_run_tool, p_fetch:
        tool = ZStreamSearchTool()
        output = await tool.run(
            input=ZStreamSearchToolInput(
                component="pkg",
                summary="fix issue [rhel-9.6.z]",
                fix_version="rhel-9.6.z",
            ),
        ).middleware(GlobalTrajectoryMiddleware(pretty=True))

    result = output.to_json_safe()
    assert result.result == ZStreamSearchResult.FOUND
    assert result.source_issue == "RHEL-99002"
    assert len(result.related_commits) == 1
    assert result.related_commits[0].endswith(".patch")
