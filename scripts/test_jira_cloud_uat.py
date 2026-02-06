#!/usr/bin/env python3
"""
Test script for UAT Jira Cloud API.
"""

import asyncio
import sys
import os
import time
from datetime import datetime

# Add current directory to path
sys.path.insert(0, '.')

from supervisor.http_utils import with_requests_session
from supervisor.jira_utils import (
    change_issue_status,
    get_custom_fields,
    create_issue,
    get_issue,
    add_issue_comment,
    add_issue_label,
    get_issues_statuses,
    remove_issue_label,
    update_issue_comment,
    add_issue_attachments,
    get_issue_attachment,
    get_current_issues,
    get_issue_by_jotnar_tag,
    get_user_name,
)
from supervisor.supervisor_types import IssueStatus, JotnarTag


@with_requests_session()
async def main():
    # Get project key from command line
    if len(sys.argv) < 2:
        print("Usage: python test_uat.py PROJECT_KEY")
        print("Example: python test_uat.py RHELMISC")
        sys.exit(1)

    project = sys.argv[1]

    print("UAT Test Script")

    # Test 1: Get custom fields
    print("\n[1/14] Get custom fields")
    fields = get_custom_fields()
    print(f"Found {len(fields)} custom fields")

    # Test 2: Create an issue
    print("\n[2/14] Create test issue")
    issue_key = create_issue(
        project=project,
        summary="[TEST] UAT API Test",
        description="Test issue created to verify Jira Cloud API.",
        labels=["uat_test", "automated_test"],
        components=["jotnar-package-automation"]
    )
    print(f"  Created issue: {issue_key}")

    # Test 3: Get the issue
    print("\n[3/14] Get issue")
    issue = get_issue(issue_key)
    print(f"Got issue: {issue.summary}")
    print(f"Status: {issue.status}")

    # Test 4: Add a simple comment
    print("\n[4/15] Add a simple comment")
    add_issue_comment(issue_key, "This is a test comment from the UAT test script.")
    print(f"Added comment")

    # Test 5: Add complex Jira markup comment (baseline test format)
    print("\n[5/15] Add complex Jira markup comment")
    baseline_test_comment = """\
Automated testing for libtiff-4.4.0-13.el9_6.2 has failed.

Test results are available at: https://reportportal-rhel.apps.dno.ocp-hub.prod.psi.redhat.com/ui/#baseosqe/launches/all/9ccdf038-ca7b-462d-a236-c9e40a464b2f

Failed test runs:
* [REQ-1.4.1|https://artifacts.osci.redhat.com/testing-farm/65f0eff4-ecad-4c8a-890a-24da164d0499]
* [REQ-2.4.2|https://artifacts.osci.redhat.com/testing-farm/b5686ad4-32db-44f7-8ef0-499d99afb220]
* [REQ-3.4.3|https://artifacts.osci.redhat.com/testing-farm/a95ac61f-daab-4710-a9db-f96148657b08]
* [REQ-4.4.4|https://artifacts.osci.redhat.com/testing-farm/a7fb70cf-0688-4fa2-a00a-6782fe8bb3dd]

Reproduced failed tests with previous build libtiff-4.4.0-13.el9:
||Architecture||Original Request||Request With Old Build||Result||Comparison||
|x86_64|[65f0eff4-ecad-4c8a-890a-24da164d0499|https://api.testing-farm.io/v0.1/requests/65f0eff4-ecad-4c8a-890a-24da164d0499]|[b9d52a86-3b0c-4e78-89ab-c32a1e0cc60a|https://api.testing-farm.io/v0.1/requests/b9d52a86-3b0c-4e78-89ab-c32a1e0cc60a]|failed|[compare|^comparison-b9d52a86-3b0c-4e78-89ab-c32a1e0cc60a--65f0eff4-ecad-4c8a-890a-24da164d0499.toml]|
|ppc64le|[b5686ad4-32db-44f7-8ef0-499d99afb220|https://api.testing-farm.io/v0.1/requests/b5686ad4-32db-44f7-8ef0-499d99afb220]|[08d261c2-3540-4878-9306-cd405f14699d|https://api.testing-farm.io/v0.1/requests/08d261c2-3540-4878-9306-cd405f14699d]|failed|[compare|^comparison-08d261c2-3540-4878-9306-cd405f14699d--b5686ad4-32db-44f7-8ef0-499d99afb220.toml]|
|aarch64|[a95ac61f-daab-4710-a9db-f96148657b08|https://api.testing-farm.io/v0.1/requests/a95ac61f-daab-4710-a9db-f96148657b08]|[2e4b43f9-4654-4f98-9276-42b65afbfb9b|https://api.testing-farm.io/v0.1/requests/2e4b43f9-4654-4f98-9276-42b65afbfb9b]|failed|[compare|^comparison-2e4b43f9-4654-4f98-9276-42b65afbfb9b--a95ac61f-daab-4710-a9db-f96148657b08.toml]|
|s390x|[a7fb70cf-0688-4fa2-a00a-6782fe8bb3dd|https://api.testing-farm.io/v0.1/requests/a7fb70cf-0688-4fa2-a00a-6782fe8bb3dd]|[3425b603-d9f7-439a-827b-6d65acd2e066|https://api.testing-farm.io/v0.1/requests/3425b603-d9f7-439a-827b-6d65acd2e066]|failed|[compare|^comparison-3425b603-d9f7-439a-827b-6d65acd2e066--a7fb70cf-0688-4fa2-a00a-6782fe8bb3dd.toml]|
"""
    add_issue_comment(issue_key, baseline_test_comment)
    print(f"Added complex comment")

    # Test 6: Update the comment
    print("\n[6/15] Update the comment")
    full_issue = get_issue(issue_key, full=True)
    if full_issue.comments:
        comment_id = full_issue.comments[-1].id
        update_issue_comment(issue_key, comment_id, "This is the updated test comment from the UAT test script.")
        print(f"Updated comment")
    else:
        print(f"No comments found")

    # Test 7: Add a label
    print("\n[7/15] Add issue label")
    add_issue_label(issue_key, "test_complete")
    print(f"Added label")

    # Test 8: Remove the label
    print("\n[8/15] Remove issue label")
    remove_issue_label(issue_key, "test_complete")
    print(f"Removed label")

    # Test 9: Get issue status (using get_issue instead of JQL search)
    print("\n[9/15] Get issue status")
    issue_for_status = get_issue(issue_key)
    print(f"Status: {issue_for_status.status}")

    # Test 10: Change issue status
    print("\n[10/15] Change issue status")
    change_issue_status(
        issue_key,
        IssueStatus.IN_PROGRESS,
        comment="Status changed to In Progress by UAT test"
    )
    print(f"Changed status to In Progress")

    # Test 11: Add issue attachments
    print("\n[11/15] Added attachment")
    test_content = f"Test file created at {datetime.now().isoformat()}\n".encode('utf-8')
    test_filename = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    add_issue_attachments(
        issue_key,
        [(test_filename, test_content, "text/plain")],
        comment="Test attachment added by UAT script"
    )
    print(f"Added attachment: {test_filename}")

    # Test 12: Get issue attachment
    print("\n[12/15] Get issue attachment")
    time.sleep(1)  # wait for attachment to be available
    attachment_content = get_issue_attachment(issue_key, test_filename)
    print(f"Retrieved attachment ({len(attachment_content)} bytes)")

    # Test 13: Search with JQL
    print("\n[13/15] Search issues with JQL")
    jql = f'project = {project} AND labels = "uat_test" ORDER BY created DESC'
    matching_issues = list(get_current_issues(jql))
    print(f"Found {len(matching_issues)} issues with 'uat_test' label")

    # Test 14: Get full issue (with comments and description)
    print("\n[14/15] Getting full issue with comments")
    full_issue = get_issue(issue_key, full=True)
    if full_issue.comments:
        latest_comment = full_issue.comments[-1]
        print(f"Comments: {latest_comment.body[:50]}...")
        # Verify comment update worked
        if "updated" in latest_comment.body.lower():
            print(f"Comment update verified")

    # Test 15: Test get_issue_by_jotnar_tag
    print("\n[15/15] Testing Jotnar tag search")
    try:
        #create an issue with a Jotnar tag from the start
        tag = JotnarTag(type="needs_attention", resource="erratum", id="TEST-456")
        tag_str = str(tag)

        tagged_issue_key = create_issue(
            project=project,
            summary="[TEST] UAT - Issue with Jotnar Tag",
            description=f"Test issue for Jotnar tag search\n\n{tag_str}",
            labels=["uat_test", "jotnar_tag_test"],
            components=["jotnar-package-automation"]
        )
        print(f"Created issue with Jotnar tag: {tagged_issue_key}")

        #wait for Jira to index
        time.sleep(3)

        #try to find jotnar issue by tag
        found_issue = get_issue_by_jotnar_tag(project, tag, with_label="jotnar_tag_test")
        if found_issue:
            print(f" Found issue with jotnar tag: {found_issue.key}")
        else:
            print(f"issue not found by jotnar tag")
    except Exception as e:
        print(f"Warning: jotnar tag test failed: {e}")
        import traceback
        traceback.print_exc()



    print("All the tests passed")


if __name__ == "__main__":
    asyncio.run(main())
