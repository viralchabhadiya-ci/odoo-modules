class QuickBooksError(Exception):
    """QuickBooks refused or could not do something. `code` is Intuit's error code when it gave one."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class QuickBooksAuthError(QuickBooksError):
    """The connection is no longer authorised: the user has to connect again."""
