# Change management

We deploy Ymir to production with `make deploy` at least once a week, usually a
few times per week. We communicate user-facing changes through:

- **News:** Record every user-facing change from a successful production
  deployment in the [Ymir News archive](https://ymir.pages.redhat.com/news/).
- **Slack:** Share larger changes and major features in
  `#forum-ymir-package-automation`.
- **Mailing list:** Send a monthly digest of major features and notable changes
  from News to `rhel-devel@redhat.com`. Send a separate email for bigger
  announcements.

## For developers

For each PR with a user-facing change, describe it in the PR description between
`RELEASE NOTES BEGIN` and `RELEASE NOTES END`. `make deploy` collects this text
to prepare the News entry.

```text
RELEASE NOTES BEGIN
Describe the user-facing change here.
RELEASE NOTES END
```

## After a successful `make deploy`

1. Copy the printed News entry into `content/news/_index.md` in the website
   repo. For another deployment on the same date, keep one date heading and add
   the new tag comment and bullets below it.
2. Submit a website merge request. It is published when merged to `main`.
3. Share larger changes in Slack.
