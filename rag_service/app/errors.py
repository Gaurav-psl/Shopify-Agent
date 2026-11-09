class ServiceError(Exception):
    """An error with an HTTP status, turned into a JSON response by main.py."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message
