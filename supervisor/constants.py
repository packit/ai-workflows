from datetime import datetime, timezone

DATETIME_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)
