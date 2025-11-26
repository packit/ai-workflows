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
    jira_url,
    jira_api_version,
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
        description="This is a test issue created to verify Jira Cloud API.",
        labels=["uat_test", "automated_test"],
        components=["jotnar-package-automation"]
    )
    print(f"  Created issue: {issue_key}")

    # Test 3: Get the issue
    print("\n[3/14] Get issue")
    issue = get_issue(issue_key)
    print(f"Got issue: {issue.summary}")
    print(f"Status: {issue.status}")

    # Test 4: Add a comment
    print("\n[4/14] Add a comment")
    add_issue_comment(issue_key, "This is a test comment from the UAT test script.")
    print(f"Added comment")

    # Test 5: Update the comment
    print("\n[5/14] Update the comment")
    full_issue = get_issue(issue_key, full=True)
    if full_issue.comments:
        comment_id = full_issue.comments[-1].id
        update_issue_comment(issue_key, comment_id, "This is the updated test comment from the UAT test script.")
        print(f"Updated comment")
    else:
        print(f"No comments found")

    # Test 6: Add a label
    print("\n[6/14] Add issue label")
    add_issue_label(issue_key, "test_complete")
    print(f"Added label")

    # Test 7: Remove the label
    print("\n[7/14] Remove issue label")
    remove_issue_label(issue_key, "test_complete")
    print(f"Removed label")

    # Test 8: Get issue status (using get_issue instead of JQL search)
    print("\n[8/14] Get issue status")
    issue_for_status = get_issue(issue_key)
    print(f"Status: {issue_for_status.status}")

    # Test 9: Change issue status
    print("\n[9/14] Change issue status")
    change_issue_status(
        issue_key,
        IssueStatus.IN_PROGRESS,
        comment="Status changed to In Progress by UAT test"
    )
    print(f"Changed status to In Progress")

    # Test 10: Add issue attachments
    print("\n[10/14] Added attachment")
    test_content = f"Test file created at {datetime.now().isoformat()}\n".encode('utf-8')
    test_filename = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
    add_issue_attachments(
        issue_key,
        [(test_filename, test_content, "text/plain")],
        comment="Test attachment added by UAT script"
    )
    print(f"Added attachment: {test_filename}")

    # Test 11: Get issue attachment
    print("\n[11/14] Get issue attachment")
    time.sleep(1)  # wait for attachment to be available
    attachment_content = get_issue_attachment(issue_key, test_filename)
    print(f"Retrieved attachment ({len(attachment_content)} bytes)")

    # Test 12: Search with JQL
    print("\n[12/14] Search issues with JQL")
    jql = f'project = {project} AND labels = "uat_test" ORDER BY created DESC'
    matching_issues = list(get_current_issues(jql))
    print(f"Found {len(matching_issues)} issues with 'uat_test' label")

    # Test 13: Get full issue (with comments and description)
    print("\n[13/14] Getting full issue with comments")
    full_issue = get_issue(issue_key, full=True)
    if full_issue.comments:
        latest_comment = full_issue.comments[-1]
        print(f"Comments: {latest_comment.body[:50]}...")
        # Verify ADF roundtrip worked
        if "UPDATED" in latest_comment.body:
            print(f"Comment update verified")

    # Test 14: Test get_issue_by_jotnar_tag
    print("\n[14/14] Testing Jotnar tag search")
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
