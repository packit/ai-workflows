#!/usr/bin/python3
import typer
import re
import nitrate

# This script extracts information from the TCMS notes fields as generated
# by Errata Workflow Automation (EWA). An example entry looks like:
#
# ```
# [structured-field-start]
# This is StructuredField version 1. Please, edit with care.
#
# [errata-resolution]
# Old PASSED & New PASSED => WORKING
#
# [result-summary]
# old-files = PASSED [3-0/3]
# new-files = PASSED [3-0/3]
# old-avc = PASSED [3-0/3]
# new-avc = PASSED [3-0/3]
# old-duration = 0:00:21 [0:00:19 - 0:00:37]
# new-duration = 0:00:30 [0:00:18 - 0:00:38]
#
# [result-details]
# beaker-task = https://beaker.engineering.redhat.com/tasks/executed?recipe_task_id=203402088&recipe_task_id=203402051&recipe_task_id=203402393&recipe_task_id=203402430&recipe_task_id=203402125&recipe_task_id=203402467&old_pkg_tasks=203402393,203402467,203402430&new_pkg_tasks=203402088,203402125,203402051
# tcms-results-version = 3.0
#
# [structured-field-end]
# ```
# Rather than formally parsing this (using qe.py) we just pull out the lines
# we are interested in using a regular expression.

# Include only meaningful lines from the notes
NOTES_INCLUDE_PATTERN = re.compile(r'CR#|=>|-files|-avc|beaker-task')
# Unless called with --full, skip the Errata Workflow caseruns
CASERUN_EXCLUDE_PATTERN = re.compile(r'Errata Workflow')

# get the run details from the run specified by its id and return
# a multiline string containing the results and for tests which didn't
# PASS, also usable information from caserun.notes:
# 1) errata resulotion (comparison of results with unfixed / old and
#    fixed / new builds
# 2) link to results in Beaker
# optionally you can enable:
# full output (include Errata Workflow and print full notes for all tests)
# color output (include escape codes for colored test status)
def get_tcms_run_details(run_id: str, *, full: bool = False, color: bool = False) -> str:
    """
    Fetches and filters TCMS test run details.
    """
    if not color:
        nitrate.set_color_mode(nitrate.COLOR_OFF)
    else:
        nitrate.set_color_mode(nitrate.COLOR_ON)

    testrun = nitrate.TestRun(int(run_id))
    output = []

    for caserun in testrun.caseruns:
        caserun_str = str(caserun)
        notes_str = str(caserun.notes)

        passed = (caserun.status == nitrate.Status('PASSED'))

        output_entry = []
        if full:
            output_entry.append(caserun_str)
            # The original script used print() which added a newline.
            # To replicate that, we split the notes into lines and add them
            # individually to our list.
            output_entry.extend(notes_str.splitlines())
        else:
            # filter out Errata Workflow and not useful lines from notes
            if CASERUN_EXCLUDE_PATTERN.search(caserun_str):
                continue
            output_entry.append(caserun_str)
            # add the details from notes only for tests that do not pass
            if not passed:
                for line in notes_str.splitlines():
                    if NOTES_INCLUDE_PATTERN.search(line):
                        output_entry.append(line)
        if output_entry:
            output.append(output_entry)

    # Flatten the list of lists into a single list of strings
    flattened_output = [item for sublist in output for item in sublist]
    # Join the strings into a single multiline string
    return "\n".join(flattened_output)

def main(
    test_run_id: str,
    full: bool = typer.Option(False, help="Show the full, unfiltered output."),
    color: bool = typer.Option(False, help="Enable colorized output.")
    ):

    # get_tcms_run_details returns a single string.
    result = get_tcms_run_details(test_run_id, full=full, color=color)
    if result:
        print(result)

if __name__ == "__main__":
    typer.run(main)
