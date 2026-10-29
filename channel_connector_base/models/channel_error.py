from odoo.exceptions import UserError


class ChannelError(UserError):
    """A problem we can explain to the user in plain words.

    Connectors raise it for everything that has a fix the user can apply
    (wrong credentials, missing scope, unknown SKU...). Anything else is
    shown as an unexpected error with the traceback kept on the job.
    """


class ChannelAuthError(ChannelError):
    """The channel rejected our credentials: the store needs to be reconnected."""
