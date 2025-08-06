#!/bin/sh

set -e

# TODO: This should be removed when we have more than one OpenShift deployment.
oc project jotnar-prod

for filename in *.yml; do
    # This check prevents an error if no .yml files are found.
    if [ -e "$filename" ]; then
        echo "Applying $filename ..."
        # TODO: avoid accidentally applying to wrong namespace by using `-n` argument
        # https://github.com/packit/ai-workflows/pull/74#discussion_r2256759455
        oc apply -f "$filename"
    fi
done
