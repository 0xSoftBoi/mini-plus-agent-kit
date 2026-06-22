"""Telegram chat surface: RoverChat turn + TelegramBridge polling."""

import _bootstrap  # noqa: F401
import base64

from mini_plus_agent_kit.rover import RoverVerbs, Scene
from mini_plus_agent_kit.client import Telemetry
from mini_plus_agent_kit.telegram import RoverChat, TelegramBridge


# --- scripted model stand-in ------------------------------------------------
class _Block:
    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class _Resp:
    def __init__(self, content, stop_reason):
        self.content, self.stop_reason = content, stop_reason


class _FakeAnthropic:
    def __init__(self, script):
        self._script, self._i = script, 0
        self.messages = self

    def create(self, **kw):
        r = self._script[self._i]; self._i += 1; return r


class FakeVerbs(RoverVerbs):
    name = "fake"
    capabilities = frozenset({"status_report", "look", "photo", "move", "turn"})

    def status_report(self): return {"reply": "Battery 80%, all clear."}
    def telemetry(self): return Telemetry(battery=80.0)
    def look(self): return Scene(caption="a desk and a window", image_b64=base64.b64encode(b"IMG").decode())
    def photo(self): return b"IMG"
    def move(self, distance_ft=1.0, backward=False): return {"ok": True}
    def turn(self, degrees): return {"ok": True}
    def stop(self): return {"ok": True}


def _tu(name, inp, id): return _Block("tool_use", name=name, input=inp, id=id)


def test_chat_status_textonly():
    script = [
        _Resp([_tu("status_report", {}, "s1")], "tool_use"),
        _Resp([_Block("text", text="I'm at 80% battery and the path is clear.")], "end_turn"),
    ]
    chat = RoverChat(FakeVerbs(), client=_FakeAnthropic(script))
    reply = chat.chat("how are you doing?")
    assert "80%" in reply.text and reply.images == []


def test_chat_look_returns_image():
    script = [
        _Resp([_tu("look", {}, "l1")], "tool_use"),
        _Resp([_Block("text", text="I see a desk and a window.")], "end_turn"),
    ]
    chat = RoverChat(FakeVerbs(), client=_FakeAnthropic(script))
    reply = chat.chat("what do you see?")
    assert reply.text == "I see a desk and a window."
    assert reply.images == [b"IMG"]            # the frame surfaced from the look verb


def test_chat_keeps_history_across_turns():
    script = [
        _Resp([_Block("text", text="Hi! I'm your rover.")], "end_turn"),
        _Resp([_Block("text", text="Yes, still here.")], "end_turn"),
    ]
    chat = RoverChat(FakeVerbs(), client=_FakeAnthropic(script))
    chat.chat("hello")
    chat.chat("you there?")
    # user+assistant for turn 1, user+assistant for turn 2
    roles = [m["role"] for m in chat.history]
    assert roles == ["user", "assistant", "user", "assistant"]


# --- bridge: fake Telegram API ---------------------------------------------
class FakeAPI:
    def __init__(self, updates_batches):
        self._batches = list(updates_batches)
        self.sent_msgs = []
        self.sent_photos = []
        self.offsets = []

    def get_updates(self, offset=None, timeout=30):
        self.offsets.append(offset)
        return self._batches.pop(0) if self._batches else []

    def send_message(self, chat_id, text):
        self.sent_msgs.append((chat_id, text)); return {"ok": True}

    def send_photo(self, chat_id, photo, caption=""):
        self.sent_photos.append((chat_id, photo)); return {"ok": True}


def test_bridge_poll_once_routes_and_tracks_offset():
    script = [
        _Resp([_tu("look", {}, "l1")], "tool_use"),
        _Resp([_Block("text", text="A desk and a window.")], "end_turn"),
    ]
    api = FakeAPI([[
        {"update_id": 100, "message": {"chat": {"id": 42}, "text": "what do you see?"}},
    ]])
    bridge = TelegramBridge(FakeVerbs(), token="x", client=_FakeAnthropic(script), api=api)
    n = bridge.poll_once()
    assert n == 1
    assert api.sent_msgs == [(42, "A desk and a window.")]
    assert api.sent_photos and api.sent_photos[0] == (42, b"IMG")
    assert bridge._offset == 101                # update_id + 1


def test_bridge_skips_non_text_updates():
    api = FakeAPI([[
        {"update_id": 5, "message": {"chat": {"id": 1}}},   # no text (e.g. a sticker)
    ]])
    bridge = TelegramBridge(FakeVerbs(), token="x", client=_FakeAnthropic([]), api=api)
    assert bridge.poll_once() == 0
    assert bridge._offset == 6 and api.sent_msgs == []


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
