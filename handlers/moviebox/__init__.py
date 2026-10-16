
__all__ = ["moviebox_cmd", "moviebox_callback"]


def __getattr__(name):
    if name == "moviebox_cmd":
        from .commands import moviebox_cmd
        return moviebox_cmd
    if name == "moviebox_callback":
        from .callbacks import moviebox_callback
        return moviebox_callback
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
