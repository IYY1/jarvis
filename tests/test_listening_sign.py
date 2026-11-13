import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from jarvis.daemon import Service
from jarvis.windows.listening_sign import CORNERS, Look, ListeningSign, corner_position

from tests.test_core import FakeWorker, until


class LookTests(unittest.TestCase):
    def test_it_appears_turns_and_breathes_while_listening(self):
        look = Look()
        look.set("listening", 10.0)
        visible, alpha, angle, glow, ring = look.frame(10.0)
        self.assertTrue(visible)      # at once, even before its fade-in shows anything
        self.assertEqual((alpha, ring), (0.0, None))
        visible, alpha, angle, glow, ring = look.frame(11.0)
        self.assertEqual(alpha, 1.0)
        self.assertAlmostEqual(angle, Look.TURN)
        self.assertTrue(0 <= glow <= 1)

    def test_a_command_flashes_its_result_then_the_sign_fades_away(self):
        for ok, ring in ((True, "ok"), (False, "fail")):
            with self.subTest(ok=ok):
                look = Look()
                look.set("listening", 0.0)
                look.frame(1.0)
                look.set("busy", 1.0)
                look.frame(1.2)
                look.set("done", 1.2, ok)
                self.assertEqual(look.frame(1.3)[4], ring)
                self.assertEqual(look.frame(1.3)[1], 1.0)
                self.assertTrue(look.frame(1.2 + Look.FLASH + Look.FADE_OUT / 2)[0])
                self.assertFalse(look.frame(1.2 + Look.FLASH + Look.FADE_OUT + 0.01)[0])
                self.assertEqual(look.phase, "hidden")

    def test_no_command_fades_and_nothing_to_end_stays_hidden(self):
        look = Look()
        look.set("fading", 0.0)
        look.set("done", 0.0, True)
        self.assertFalse(look.frame(0.1)[0])
        look.set("listening", 1.0)
        look.frame(2.0)
        look.set("fading", 2.0)
        self.assertTrue(look.frame(2.0 + Look.FADE_OUT / 2)[0])
        self.assertFalse(look.frame(2.0 + Look.FADE_OUT + 0.01)[0])

    def test_listening_again_while_it_fades_brings_it_back(self):
        look = Look()
        look.set("listening", 0.0)
        look.frame(1.0)
        look.set("fading", 1.0)
        look.frame(1.2)
        look.set("listening", 1.2)
        self.assertEqual(look.phase, "listening")
        self.assertTrue(look.frame(5.0)[0])


class PlaceTests(unittest.TestCase):
    def test_each_corner_keeps_a_margin_from_the_edges(self):
        area = (0, 0, 1920, 1040)
        self.assertEqual(corner_position("top_right", area, 80, 1.0), (1920 - 22 - 80, 22))
        self.assertEqual(corner_position("top_left", area, 80, 1.0), (22, 22))
        self.assertEqual(corner_position("bottom_right", area, 80, 1.0), (1920 - 22 - 80, 1040 - 22 - 80))
        self.assertEqual(corner_position("bottom_left", (-1920, 0, 0, 1080), 160, 2.0), (-1920 + 44, 1080 - 44 - 160))
        self.assertEqual(set(CORNERS), {"top_right", "top_left", "bottom_right", "bottom_left"})


class SwitchedOffTests(unittest.TestCase):
    def test_switched_off_makes_no_window_at_all(self):
        sign = ListeningSign(lambda: None)
        with patch.object(ListeningSign, "_start") as start:
            sign.listen()
            sign.busy()
            sign.done(True)
            sign.hide()
        start.assert_not_called()
        self.assertIsNone(sign.thread)


class ServiceSignTests(unittest.IsolatedAsyncioTestCase):
    def make_service(self):
        registry = MagicMock()
        registry.index = {}
        registry.list.return_value = []
        with patch("jarvis.daemon.AppRegistry", return_value=registry):
            service = Service({"privacy": {"onboarded": True}, "activation": {"mode": "wake_word"}})
        service.loop = asyncio.get_running_loop()
        service.sign = MagicMock()
        return service

    async def test_the_sign_follows_wake_command_result_and_timeout(self):
        for result, shown in (({"ok": True}, True), ({"ok": False, "error": "no"}, False)):
            with self.subTest(result=result):
                service = self.make_service()
                worker = FakeWorker()
                service.worker = worker
                with patch.object(service, "execute", AsyncMock(return_value=result)):
                    reading = asyncio.create_task(service.read_voice(worker, "wake_word"))
                    worker.send(event="ready", engine="test")
                    worker.send(event="wake")
                    await until(lambda: call.listen() in service.sign.mock_calls)
                    worker.send(event="command", text="open firefox", confidence=1.0)
                    await until(lambda: any(c[0] == "done" for c in service.sign.mock_calls))
                    worker.send(event="wake")
                    worker.send(event="timeout")
                    await until(lambda: call.hide() in service.sign.mock_calls)
                    worker.terminate()
                    await reading
                shown_calls = [c for c in service.sign.mock_calls if c != call.hide()]
                self.assertEqual(shown_calls, [call.listen(), call.busy(), call.done(shown), call.listen()])
                self.assertEqual(service.sign.mock_calls[-1], call.hide())   # the timeout took it away
                # No hide between the wake and the result: it stays while Jarvis listens and works.
                calls = service.sign.mock_calls
                self.assertNotIn(call.hide(), calls[calls.index(call.listen()):calls.index(call.done(shown))])

    async def test_the_sign_never_stays_up_once_jarvis_just_waits(self):
        service = self.make_service()
        worker = FakeWorker()
        service.worker = worker
        reading = asyncio.create_task(service.read_voice(worker, "wake_word"))
        worker.send(event="ready", engine="test")
        worker.send(event="wake")
        await until(lambda: call.listen() in service.sign.mock_calls)
        service.sign.reset_mock()
        worker.send(event="ready", engine="test", listening=False)   # Cancel, or the session re-armed
        await until(lambda: call.hide() in service.sign.mock_calls)
        self.assertEqual(service.activation.state, "idle")
        worker.terminate()
        await reading

    async def test_muting_hides_the_sign(self):
        service = self.make_service()
        with patch.object(service, "stop_worker", new_callable=AsyncMock), \
                patch.object(service, "end_dictation", new_callable=AsyncMock):
            await service.set_mute(True)
        service.sign.hide.assert_called_with()   # (publishing the muted state hides it again: harmless)


if __name__ == "__main__":
    unittest.main()
