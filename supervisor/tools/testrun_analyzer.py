#!/usr/bin/python3
import optparse
import re
from qe import nitrate

oparser = optparse.OptionParser()
oparser.add_option(
    '--run',
    '-r',
    action='store',
    dest='run',
    default=None,
    help="The TestRun ID to analyze."
)
oparser.add_option(
    '--filter',
    '-f',
    action='store_true',
    dest='filter',
    default=False,
    help="Apply the built-in filters (CR#, =>, -files, -avc) and exclude 'Errata Workflow'."
)
opts, args = oparser.parse_args()

if not opts.run:
    oparser.error("TestRun ID (--run or -r) is required.")

# Regex to match and remove ANSI escape (color) codes
ansi_escape_pattern = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# The patterns to keep, from your grep command
include_patterns = re.compile(r'CR#|=>|-files|-avc')
# The pattern to exclude
exclude_pattern = re.compile(r'Errata Workflow')

testrun = nitrate.TestRun(int(opts.run))

for caserun in testrun.caseruns:
    # Format the output as strings
    caserun_str = str(caserun)
    notes_str = str(caserun.notes)

    # Create a clean version of the status string by stripping color codes
    clean_caserun_str = ansi_escape_pattern.sub('', caserun_str)

    # Determine if the case passed based on the clean string
    passed = clean_caserun_str.startswith('PASS')

    if opts.filter:
        # In filter mode, print the original caserun line if it matches the filter
        # Note: We still print the original caserun_str to preserve color
        if include_patterns.search(caserun_str) and not exclude_pattern.search(caserun_str):
            print(caserun_str)

        # Only print notes if the case did NOT pass and the notes match the filter
        if not passed:
            for line in notes_str.splitlines():
                if include_patterns.search(line) and not exclude_pattern.search(line):
                    print(line)
    else:
        # In unfiltered mode, always print the original caserun line
        print(caserun_str)

        # Only print notes if the case did NOT pass
        if not passed:
            print(notes_str)
