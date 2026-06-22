"""Telegram chat surface — the openClaw flagship demo: drive the rover by chat.

Two pieces:

* :class:`RoverChat` — a *conversational* Claude agent over the openClaw verb
  surface (:class:`~mini_plus_agent_kit.rover.RoverVerbs`). Unlike
  :class:`~mini_plus_agent_kit.agent.MiniPlusAgent` (objective-driven, runs to
  ``finish``), this answers one chat message per turn, keeping history, and
  returns text + any camera images the verbs produced (the ``MEDIA:`` idea).
* :class:`TelegramBridge` — long-polls the Telegram Bot API and pipes messages
  through ``RoverChat``, replying with text (``sendMessage``) and photos
  (``sendPhoto``). "what's your status?" → ``status_report``; "drive forward then
  tell me what you see" → ``move`` + ``look`` with the frame sent inline.

Transport is the raw Bot API over httpx (no extra dependency). Both classes take
injectable clients so the logic is testable without Telegram or Anthropic.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable

import anthropic
import httpx

from .rover import RoverVerbs, make_verbs
from .tools import dispatch, make_tools
from .agent import DEFAULT_MODEL, load_system_prompt

_CHAT_SYSTEM_EXTRA = (
    "You are operating over a chat channel. Each user message is one request — "
    "act with the verbs as needed, then reply conversationally in one or two short "
    "sentences. When you take a photo or look, the image is delivered to the user "
    "automatically; refer to it naturally. Do not call `finish` — just answer."
)


@dataclass
class ChatReply:
    text: str
    images: list[bytes] = field(default_factory=list)


class RoverChat:
    """Conversational rover agent: ``chat(message) -> ChatReply``."""

    def __init__(
        self,
        rover,
        client: anthropic.Anthropic | None = None,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        work=None,
        resource_name: str | None = None,
        max_steps: int = 8,
    ):
        self.verbs: RoverVerbs = make_verbs(rover)
        self.client = client or anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.work = work
        self.resource_name = resource_name
        self.max_steps = max_steps
        self.system = load_system_prompt(_CHAT_SYSTEM_EXTRA)
        self.tools = make_tools(self.verbs.capabilities, has_work=work is not None)
        self.history: list[dict[str, Any]] = []

    def chat(self, message: str) -> ChatReply:
        self.history.append({"role": "user", "content": message})
        images: list[bytes] = []
        last_text = ""

        for _ in range(self.max_steps):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=2048,
                system=self.system,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                tools=self.tools,
                messages=self.history,
            )
            self.history.append({"role": "assistant", "content": response.content})
            last_text = " ".join(b.text.strip() for b in response.content
                                 if b.type == "text" and b.text.strip()) or last_text

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                outcome = dispatch(self.verbs, block.name, dict(block.input),
                                   work=self.work, resource_name=self.resource_name)
                for b in outcome.blocks:
                    if isinstance(b, dict) and b.get("type") == "image":
                        try:
                            images.append(base64.b64decode(b["source"]["data"]))
                        except Exception:
                            pass
                tr: dict[str, Any] = {"type": "tool_result", "tool_use_id": block.id,
                                      "content": outcome.blocks}
                if outcome.is_error:
                    tr["is_error"] = True
                tool_results.append(tr)
            self.history.append({"role": "user", "content": tool_results})

        return ChatReply(text=last_text or "(done)", images=images)


class _TelegramAPI:
    """Thin Telegram Bot API client (getUpdates / sendMessage / sendPhoto)."""

    def __init__(self, token: str, timeout: float = 40.0):
        self.base = f"https://api.telegram.org/bot{token}"
        self._http = httpx.Client(timeout=timeout)

    def get_updates(self, offset: int | None = None, timeout: int = 30) -> list[dict]:
        r = self._http.get(f"{self.base}/getUpdates",
                           params={"offset": offset, "timeout": timeout})
        r.raise_for_status()
        return r.json().get("result", [])

    def send_message(self, chat_id: int, text: str) -> dict:
        r = self._http.post(f"{self.base}/sendMessage", json={"chat_id": chat_id, "text": text})
        r.raise_for_status()
        return r.json()

    def send_photo(self, chat_id: int, photo: bytes, caption: str = "") -> dict:
        r = self._http.post(f"{self.base}/sendPhoto",
                            data={"chat_id": chat_id, "caption": caption},
                            files={"photo": ("frame.jpg", photo, "image/jpeg")})
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._http.close()


class TelegramBridge:
    """Pipe Telegram chats through a per-chat :class:`RoverChat`."""

    def __init__(
        self,
        rover,
        token: str,
        *,
        client: anthropic.Anthropic | None = None,
        work=None,
        resource_name: str | None = None,
        api: _TelegramAPI | None = None,
        on_event: Callable[[str], None] | None = None,
    ):
        self.rover = rover
        self.client = client
        self.work = work
        self.resource_name = resource_name
        self.api = api or _TelegramAPI(token)
        self.on_event = on_event or (lambda m: None)
        self._chats: dict[int, RoverChat] = {}
        self._offset: int | None = None

    def _chat_for(self, chat_id: int) -> RoverChat:
        if chat_id not in self._chats:
            self._chats[chat_id] = RoverChat(
                self.rover, client=self.client, work=self.work,
                resource_name=self.resource_name)
        return self._chats[chat_id]

    def handle(self, chat_id: int, text: str) -> ChatReply:
        """Process one inbound message and send the reply back to the chat."""
        reply = self._chat_for(chat_id).chat(text)
        if reply.text:
            self.api.send_message(chat_id, reply.text)
        for img in reply.images:
            self.api.send_photo(chat_id, img)
        return reply

    def poll_once(self) -> int:
        """Fetch one batch of updates, handle text messages. Returns count handled."""
        updates = self.api.get_updates(offset=self._offset)
        handled = 0
        for u in updates:
            self._offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message")
            if not msg:
                continue
            text = msg.get("text")
            chat_id = msg.get("chat", {}).get("id")
            if not text or chat_id is None:
                continue
            self.on_event(f"[{chat_id}] {text}")
            try:
                self.handle(chat_id, text)
            except Exception as e:  # one bad message shouldn't kill the bot
                self.on_event(f"error handling message: {e}")
            handled += 1
        return handled

    def run_forever(self) -> None:
        self.on_event("Telegram bridge running — message your bot.")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                break
            except Exception as e:
                self.on_event(f"poll error: {e}")
