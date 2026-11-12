from uuid import UUID


class BackendError(Exception):
    pass


class ResourceNotFoundError(BackendError):
    def __init__(self, resource: str):
        self.resource = resource
        super().__init__(f"{resource} not found")


class RunInProgressError(BackendError):
    def __init__(self, run_id: UUID):
        self.run_id = run_id
        super().__init__(f"Agent run {run_id} is already active")


class QueueUnavailableError(BackendError):
    def __init__(self, run_id: UUID):
        self.run_id = run_id
        super().__init__("Agent queue is unavailable")
