"""Service connection and log behaviour. No microphone, display or capture is accessed."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from jarvis.daemon import close_connection


class ConnectionCloseTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_client_that_vanished_is_not_an_error(self):
        for error in (ConnectionResetError(64, "network name"), BrokenPipeError(), OSError("closed")):
            with self.subTest(error=error):
                writer = MagicMock()
                writer.wait_closed = AsyncMock(side_effect=error)
                await close_connection(writer)
                writer.close.assert_called_once_with()

    async def test_other_failures_still_surface(self):
        writer = MagicMock()
        writer.wait_closed = AsyncMock(side_effect=ValueError("bug"))
        with self.assertRaises(ValueError):
            await close_connection(writer)


if __name__ == "__main__":
    unittest.main()
