# ihmt_gui

`ihmt_gui/` is a separate package layered on the same public API — it never imports IHMT internals.
Its route tables map URL parameter names to method parameter names explicitly
(`{"id": "node_id"}`); `tests/test_gui.py` checks every mapping against the real signatures, because
a silent mismatch there surfaces only as a 400 in the browser.
