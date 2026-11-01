"""Memory counters and accounting failures; no devices or desktop actions."""
import asyncio
import ctypes
import mmap
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from murmur import process_memory as memory
from scripts.benchmark_application_memory import summarize


class AccountingTests(unittest.TestCase):
    def test_overlapping_roots_count_children_once(self):
        processes = {1: {"ppid": 0}, 2: {"ppid": 1}, 3: {"ppid": 2}, 4: {"ppid": 0}}
        self.assertEqual(memory.descendants([1, 2], processes), [1, 2, 3])

    def test_missing_or_denied_counter_never_becomes_zero_total(self):
        inventory = {1: {"ppid": 0, "name": "controller"}, 2: {"ppid": 1, "name": "worker"}}
        with patch.object(memory.sys, "platform", "linux"), \
                patch.object(memory, "linux_processes", return_value=inventory), \
                patch.object(memory, "linux_memory", side_effect=[{"rss_bytes": 100, "pss_bytes": 50}, PermissionError("denied")]):
            result = memory.snapshot([1])
        self.assertFalse(result["complete"])
        self.assertIsNone(result["totals"]["rss_bytes"])
        self.assertEqual(result["processes"][1]["error"], "denied")

    def test_platform_totals_keep_unavailable_counters_null(self):
        inventory = {1: {"ppid": 0, "name": "controller"}}
        with patch.object(memory.sys, "platform", "linux"), \
                patch.object(memory, "linux_processes", return_value=inventory), \
                patch.object(memory, "linux_memory", return_value={"rss_bytes": 100, "pss_bytes": None}):
            result = memory.snapshot([1])
        self.assertEqual(result["totals"]["rss_bytes"], 100)
        self.assertIsNone(result["totals"]["pss_bytes"])
        self.assertFalse(result["complete"])

    def test_parse_proc_names_and_kib_units(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            process = root / "17"
            process.mkdir()
            fields = ["S", "1"] + ["0"] * 17 + ["12345"]
            (process / "stat").write_text("17 (voice (worker)) " + " ".join(fields))
            (process / "smaps_rollup").write_text("Rss: 2048 kB\nPss: 1024 kB\nPss_Dirty: 7 kB\n")
            self.assertEqual(memory.linux_processes(root)[17], {"ppid": 1, "name": "voice (worker)", "started": "12345"})
            self.assertEqual(memory.linux_memory(17, root), {"rss_bytes": 2097152, "pss_bytes": 1048576})

    def test_peak_is_simultaneous_and_incomplete_sample_invalidates_summary(self):
        # Independent high-water marks could sum to 200; simultaneous peak is 120.
        samples = [{"memory": {"totals": {"rss_bytes": total, "pss_bytes": pss}}}
                   for total, pss in [(120, 100), (110, None)]]
        result = summarize(samples)
        self.assertEqual(result["rss_bytes"]["peak"], 120)
        self.assertIsNone(result["pss_bytes"])

    def test_doctor_offline_does_not_report_its_own_memory(self):
        with patch("murmur.client.request", side_effect=ConnectionError("offline")):
            self.assertEqual(memory.service_snapshot(), {"complete": False, "error": "offline"})

    @unittest.skipUnless(sys.platform == "linux", "Linux procfs")
    def test_live_child_is_included_and_missing_pid_is_incomplete(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        try:
            result = memory.snapshot([os.getpid(), child.pid])
            self.assertTrue(result["complete"])
            self.assertIn(child.pid, [p["pid"] for p in result["processes"]])
            self.assertEqual(len({p["pid"] for p in result["processes"]}), len(result["processes"]))
            self.assertEqual(result["totals"]["pss_bytes"], sum(p["pss_bytes"] for p in result["processes"]))
        finally:
            child.terminate()
            child.wait(timeout=5)
        missing = memory.snapshot([999999999])
        self.assertFalse(missing["complete"])
        self.assertIsNone(missing["totals"]["rss_bytes"])

    def test_service_only_samples_explicit_status_requests(self):
        from murmur.daemon import Service
        from murmur.config import DEFAULTS
        with patch("murmur.daemon.AppRegistry"):
            service = Service(DEFAULTS)
        with patch.object(memory, "snapshot", return_value={"complete": True}) as sample:
            service.status()
            sample.assert_not_called()
            result = asyncio.run(service.handle({"op": "status"}))
        self.assertEqual(result["pid"], os.getpid())
        self.assertEqual(result["memory"], {"complete": True})
        sample.assert_called_once_with()


class WindowsWorkingSetFallbackTests(unittest.TestCase):
    def test_retry_counts_private_pages_and_excludes_shareable_single_owner(self):
        from murmur.windows.process_memory import private_working_set
        dll = MagicMock()
        calls = []

        def query(handle, buffer, size):
            calls.append(size)
            if len(calls) == 1:
                buffer[0] = 5000
                return False
            buffer[0] = 3
            # ShareCount=1 does not make a shareable page private.
            buffer[1], buffer[2], buffer[3] = 4, 0x100 | (1 << 5), 4
            return True

        dll.K32QueryWorkingSet.side_effect = query
        with patch.object(ctypes, "get_last_error", return_value=24, create=True):
            self.assertEqual(private_working_set(123, dll), 2 * mmap.PAGESIZE)
        self.assertGreater(calls[1], calls[0])
        dll.OpenProcess.assert_called_once_with(0x0410, False, 123)
        dll.CloseHandle.assert_called_once_with(dll.OpenProcess.return_value)

    def test_denied_access_and_query_failure_stay_unavailable(self):
        from murmur.windows.process_memory import private_working_set
        dll = MagicMock()
        dll.OpenProcess.return_value = 0
        self.assertIsNone(private_working_set(123, dll))
        dll.K32QueryWorkingSet.assert_not_called()
        dll.CloseHandle.assert_not_called()
        dll.OpenProcess.return_value = 9
        dll.K32QueryWorkingSet.return_value = False
        with patch.object(ctypes, "get_last_error", return_value=5, create=True):
            self.assertIsNone(private_working_set(123, dll))
        dll.CloseHandle.assert_called_once_with(9)

    def test_oversized_or_inconsistent_working_sets_stay_unavailable(self):
        from murmur.windows.process_memory import private_working_set
        for success in (False, True):
            with self.subTest(success=success):
                dll = MagicMock()

                def query(handle, buffer, size):
                    buffer[0] = 2**32
                    return success

                dll.K32QueryWorkingSet.side_effect = query
                with patch.object(ctypes, "get_last_error", return_value=24, create=True):
                    self.assertIsNone(private_working_set(123, dll))
                self.assertEqual(dll.K32QueryWorkingSet.call_count, 1)
                dll.CloseHandle.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "native Windows APIs")
class WindowsMemoryTests(unittest.TestCase):
    def test_current_process_counters(self):
        from murmur.windows.process_memory import processes, memory as measure
        self.assertIn(os.getpid(), processes())
        counters = measure(os.getpid())
        self.assertGreater(counters["commit_bytes"], 0)
        if counters["private_working_set_bytes"] is not None:
            self.assertGreater(counters["private_working_set_bytes"], 0)

    def test_legacy_working_set_counts_a_touched_private_allocation(self):
        from murmur.windows.process_memory import kernel, private_working_set
        dll = kernel()
        before = private_working_set(os.getpid(), dll)
        self.assertIsNotNone(before)
        allocation = bytearray(8 * 1024**2)
        for offset in range(0, len(allocation), mmap.PAGESIZE):
            allocation[offset] = 1
        after = private_working_set(os.getpid(), dll)
        self.assertIsNotNone(after)
        self.assertGreaterEqual(after - before, 7 * 1024**2)


if __name__ == "__main__":
    unittest.main()
