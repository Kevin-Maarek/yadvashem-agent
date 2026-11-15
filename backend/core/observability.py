import json
import logging
import sys
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "time": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            **getattr(record, "metrics", {}),
        }, ensure_ascii=False)


def configure_logging():
    for name in ("api", "core", "db", "domain", "integrations", "prompts",
                 "schemas", "services", "main", "worker", "__main__"):
        logger = logging.getLogger(name)
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(JsonFormatter())
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
