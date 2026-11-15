from datetime import datetime


class StageError(Exception):
    def __init__(self, code: str, *, retry: bool = False, wait_until: datetime | None = None):
        super().__init__(code)
        self.code = code
        self.retry = retry
        self.wait_until = wait_until

