class MarketingError(Exception):
    """A problem the user can understand and fix. The message is shown as it is."""


class MarketingAuthError(MarketingError):
    """The provider rejected the credentials: the account is marked as disconnected."""
