"""Service connection and log behaviour. No microphone, display or capture is accessed."""

import asyncio
import contextlib
import io
import re
import unittest
from unittest.mock import AsyncMock, MagicMock

from jarvis.daemon import Service, TimestampedLog, close_connection, exit_description, worker_error_tail
from jarvis import config


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


class WorkerExitTests(unittest.IsolatedAsyncioTestCase):
    async def exits(self, returncode, stderr=b"", mode="wake_word", stdout=b""):
        """Run read_voice for a worker that ends by itself; return what was logged."""
        service = Service(config.validate({"privacy": {"onboarded": True}}))
        service.loop = asyncio.get_running_loop()
        service.start_worker = AsyncMock()   # a finished "once" worker restarts wake listening
        proc = MagicMock(returncode=returncode)
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_data(stdout)
        proc.stdout.feed_eof()
        proc.wait = AsyncMock(return_value=returncode)
        errors = asyncio.StreamReader()
        errors.feed_data(stderr)
        errors.feed_eof()
        service.worker = proc
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            await service.read_voice(proc, mode, asyncio.create_task(worker_error_tail(errors)))
        self.service = service
        return log.getvalue()

    async def test_a_crash_records_its_exit_code_and_stderr(self):
        log = await self.exits(3221225477, b'{"event":"error","message":"no model"}\n')
        self.assertIn("0xC0000005", log)
        self.assertIn("no model", log)
        self.assertEqual(self.service.activation.state, "error")

    async def test_an_error_the_worker_reports_is_logged_with_its_exit(self):
        log = await self.exits(1, stdout=b'{"event":"error","message":"device unavailable"}\n')
        self.assertIn("reported: device unavailable", log)
        self.assertIn("exit code 1", log)
        self.assertEqual(self.service.message, "device unavailable")

    async def test_a_clean_silent_exit_logs_nothing(self):
        # asyncio's debug mode may log slow callbacks here, so look for our own line.
        self.assertNotIn("voice worker", await self.exits(0, mode="once"))

    async def test_a_worker_we_stopped_logs_nothing(self):
        service = Service(config.validate({"privacy": {"onboarded": True}}))
        service.loop = asyncio.get_running_loop()
        proc = MagicMock(returncode=1)
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_eof()
        proc.wait = AsyncMock(return_value=1)
        log = io.StringIO()
        with contextlib.redirect_stderr(log):
            await service.read_voice(proc, "wake_word")   # service.worker is not proc
        self.assertNotIn("voice worker", log.getvalue())

    async def test_stderr_kept_in_memory_is_bounded(self):
        errors = asyncio.StreamReader(limit=1 << 20)
        errors.feed_data(b"x" * 100000 + b"newest")
        errors.feed_eof()
        tail = await worker_error_tail(errors)
        self.assertEqual(len(tail), 4096)
        self.assertTrue(tail.endswith(b"newest"))

    def test_windows_crash_codes_read_as_hex(self):
        self.assertEqual(exit_description(3221225477), "3221225477 (0xC0000005)")
        self.assertEqual(exit_description(1), "1")
        self.assertEqual(exit_description(None), "None")


class TimestampedLogTests(unittest.TestCase):
    def test_each_line_is_stamped_once_even_when_written_in_pieces(self):
        output = io.StringIO()
        log = TimestampedLog(output)
        print("first", "line", file=log)
        log.write("second\nthird")
        log.write(" continues\n")
        lines = output.getvalue().splitlines()
        self.assertEqual([re.sub(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d ", "", line) for line in lines],
                         ["first line", "second", "third continues"])
        self.assertTrue(all(re.match(r"^\d{4}-", line) for line in lines))


if __name__ == "__main__":
    unittest.main()
