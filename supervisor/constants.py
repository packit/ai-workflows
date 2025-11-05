from datetime import datetime, timedelta, timezone

# Compares correctly - all our dates are tz-aware
DATETIME_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)
# Groups within the redhat organization where we can find issues
GITLAB_GROUPS = ["rhel/rpms", "centos-stream/rpms"]
# Timeout for post-push testing (e.g., CAT tests) after stage push completes
POST_PUSH_TESTING_TIMEOUT = timedelta(hours=3)
POST_PUSH_TESTING_TIMEOUT_STR = "3 hours"
# Defines the Jotnar bot's user identity for Errata and Jira
ERRATA_JOTNAR_BOT_EMAIL = "jotnar-bot@IPA.REDHAT.COM"
JIRA_JOTNAR_BOT_EMAIL = "jotnar+bot@redhat.com"
