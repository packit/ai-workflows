#!/bin/sh

touch ${TMT_TEST_DATA}/results.yaml

# Run the tests
make run-triage-agent-e2e-tests

# Collect the tests
JIRAS=$(make list-triage-agent-e2e-tests | sed -rn 's/.*\[(RHEL-.+)\].*/\1/gp')

for TICKET in $JIRAS; do
    curl -H 'Accept: application/json' http://localhost:8082/traces/$TICKET > ${TMT_TEST_DATA}/${TICKET}.json
    curl -H 'Accept: text/html' http://localhost:8082/traces/$TICKET > ${TMT_TEST_DATA}/${TICKET}.html
done;
