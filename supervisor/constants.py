from datetime import datetime, timezone

# Compares correctly - all our dates are tz-aware
DATETIME_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)
# Groups within the redhat organization where we can find issues
GITLAB_GROUPS = ["rhel/rpms", "centos-stream/rpms"]
