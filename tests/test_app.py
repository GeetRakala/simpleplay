from __future__ import annotations

import unittest

from simpleplay.app import SimplePlayApp


class SimplePlayAppTests(unittest.TestCase):
    def test_starts_out_of_search_mode_without_initial_query(self) -> None:
        app = SimplePlayApp()
        self.assertFalse(app.search_mode)

    def test_starts_out_of_search_mode_with_initial_query(self) -> None:
        app = SimplePlayApp(initial_query="daft punk")
        self.assertFalse(app.search_mode)
