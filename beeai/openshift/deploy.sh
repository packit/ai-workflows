#!/bin/sh

set -e

for filename in *.yml; do
    # This check prevents an error if no .yml files are found.
    if [ -e "$filename" ]; then
        echo "Applying $filename ..."
        oc apply -f "$filename"
    fi
done
