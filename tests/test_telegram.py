"""Telegram chat surface: RoverChat turn + TelegramBridge polling."""

import _bootstrap  # noqa: F401
import base64

from mini_plus_agent_kit.rover import RoverVerbs, Scene
from mini_plus_agent_kit.client import Telemetry
from mini_plus_agent_kit.telegram import (
    RoverChat, TelegramBridge, redact_token, _parse_allowed_chats,
)


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
    bridge = TelegramBridge(FakeVerbs(), token="x", client=_FakeAnthropic(script), api=api,
                            allowed_chats=[42])
    n = bridge.poll_once()
    assert n == 1
    assert api.sent_msgs == [(42, "A desk and a window.")]
    assert api.sent_photos and api.sent_photos[0] == (42, b"IMG")
    assert bridge._offset == 101                # update_id + 1


def test_bridge_skips_non_text_updates():
    api = FakeAPI([[
        {"update_id": 5, "message": {"chat": {"id": 1}}},   # no text (e.g. a sticker)
    ]])
    bridge = TelegramBridge(FakeVerbs(), token="x", client=_FakeAnthropic([]), api=api,
                            allowed_chats=[1])
    assert bridge.poll_once() == 0
    assert bridge._offset == 6 and api.sent_msgs == []


# --- authorization (allowlist, fail-closed) --------------------------------
def test_bridge_allowed_chat_is_dispatched():
    script = [_Resp([_Block("text", text="hello there")], "end_turn")]
    api = FakeAPI([[
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": "hi"}},
    ]])
    bridge = TelegramBridge(FakeVerbs(), token="x", client=_FakeAnthropic(script), api=api,
                            allowed_chats=[7])
    assert bridge.poll_once() == 1
    assert api.sent_msgs == [(7, "hello there")]


def test_bridge_disallowed_chat_is_ignored():
    api = FakeAPI([[
        {"update_id": 1, "message": {"chat": {"id": 999}, "text": "let me in"}},
    ]])
    bridge = TelegramBridge(FakeVerbs(), token="x", client=_FakeAnthropic([]), api=api,
                            allowed_chats=[7])
    assert bridge.poll_once() == 0          # not dispatched
    assert api.sent_msgs == []
    assert bridge._offset == 2              # but the offset still advances (don't re-poll it)


def test_bridge_fails_closed_when_allowlist_empty():
    events = []
    api = FakeAPI([[
        {"update_id": 1, "message": {"chat": {"id": 7}, "text": "hi"}},
    ]])
    bridge = TelegramBridge(FakeVerbs(), token="x", client=_FakeAnthropic([]), api=api,
                            allowed_chats=[], on_event=events.append)
    assert bridge.poll_once() == 0          # empty allowlist => deny all
    assert api.sent_msgs == []
    # exactly one fail-closed warning, even across chats/polls
    assert sum("fail closed" in e for e in events) == 1


def test_parse_allowed_chats():
    assert _parse_allowed_chats("1, 2 ,3") == {1, 2, 3}
    assert _parse_allowed_chats("") == set()
    assert _parse_allowed_chats(None) == set()
    assert _parse_allowed_chats("9, bad, 10") == {9, 10}   # junk ignored


# --- token redaction --------------------------------------------------------
def test_redact_token_scrubs_secret():
    leaked = "404 Not Found for https://api.telegram.org/bot123456:AA-bcDEF_ghi/getUpdates"
    out = redact_token(leaked)
    assert "123456:AA-bcDEF_ghi" not in out
    assert "bot***" in out


def test_api_error_redacts_token():
    import mini_plus_agent_kit.telegram as tg

    class _Boom:
        def get(self, *a, **k):
            raise RuntimeError("connect error to https://api.telegram.org/bot77:SEKRET_tok/getUpdates")
        def close(self):
            pass

    api = tg._TelegramAPI.__new__(tg._TelegramAPI)
    api.base = "https://api.telegram.org/bot77:SEKRET_tok"
    api._http = _Boom()
    try:
        api.get_updates()
        assert False, "expected error"
    except Exception as e:
        assert "SEKRET_tok" not in str(e) and "bot***" in str(e)


if __name__ == "__main__":
    import _runner
    _runner.run(globals())
