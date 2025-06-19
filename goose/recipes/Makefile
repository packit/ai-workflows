PACKAGE ?= podman
JIRA_ISSUES ?= ""
VERSION ?= ""
.PHONY: update-centos-package
update-centos-package:
	goose recipe validate ./update-centos-package.yaml
	GOOSE_MODE="auto" goose run --recipe ./update-centos-package.yaml --params package=$(PACKAGE) --params version=$(VERSION) --params jira_issues=$(JIRA_ISSUES)
