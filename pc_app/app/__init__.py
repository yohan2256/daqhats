"""Floor impact sound meter GUI (PySide6)."""

__all__ = ["main", "build_app", "MainWindow"]


def __getattr__(name):
    # Import Qt lazily, so the package can be imported without a display.
    if name in __all__:
        from .main import MainWindow, build_app, main

        return {"main": main, "build_app": build_app, "MainWindow": MainWindow}[name]
    raise AttributeError(name)
