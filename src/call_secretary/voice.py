"""Realtime WebSocket transport; provider events are reduced to text, usage and tools."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

from call_secretary.audio import RATE, AudioEndpoint, Playback
from call_secretary.budget import RateCard
from call_secretary.routing import REPORT_RECIPIENT_CONFLICT

TRANSCRIPTION_DONE = "conversation.item.input_audio_transcription.completed"
TRANSCRIPTION_FAILED = "conversation.item.input_audio_transcription.failed"


class VoiceError(RuntimeError):
    pass


class Socket(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...


@dataclass
class ToolReply:
    result: str
    instructions: str | None = None
    tools: list[dict[str, object]] | None = None
    finish: bool = False


class VoiceHooks(Protocol):
    async def activity(self, speaking: bool | None = None) -> None: ...
    async def text(
        self, item_id: str, role: str, text: str, interrupted: bool, created: float
    ) -> None: ...
    async def usage(self, response_id: str, usage: dict[str, Any]) -> None: ...
    async def tool(self, name: str, arguments: dict[str, Any]) -> ToolReply: ...
    async def interrupted(self, item_id: str) -> None: ...


def session_update(
    prompt: str, tools: list[dict[str, object]], voice: str, rates: RateCard
) -> dict[str, Any]:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "model": rates.model,
            "instructions": prompt,
            "output_modalities": ["audio"],
            "tools": tools,
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": RATE},
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {
                        "type": "server_vad",
                        "create_response": False,
                        "interrupt_response": True,
                        "silence_duration_ms": 600,
                    },
                },
                "output": {"format": {"type": "audio/pcm", "rate": RATE}, "voice": voice},
            },
            "truncation": {
                "type": "retention_ratio",
                "retention_ratio": 0.5,
                "token_limits": {"post_instructions": rates.context_tokens},
            },
        },
    }


@asynccontextmanager
async def connect_api(api_key: str, model: str) -> AsyncIterator[Socket]:
    from websockets.asyncio.client import connect

    logger = logging.getLogger("call_secretary.private_websocket")
    logger.handlers = [logging.NullHandler()]
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)
    url = "wss://api.openai.com/v1/realtime?" + urlencode({"model": model})
    async with connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=15,
        close_timeout=3,
        max_size=262144,
        max_queue=8,
        compression=None,
        logger=logger,
    ) as ws:
        yield ws


class Realtime:
    def __init__(
        self,
        socket: Socket,
        endpoint: AudioEndpoint,
        hooks: VoiceHooks,
        rates: RateCard,
        prompt: str,
        tools: list[dict[str, object]],
        voice: str,
    ):
        self.socket = socket
        self.endpoint = endpoint
        self.hooks = hooks
        self.rates = rates
        self.prompt = prompt
        self.tools = tools
        self.voice = voice
        self.playback = Playback(endpoint)
        self.responses = 0
        self.pending = False
        self.user_pending = False
        self.ready = False
        self.usage_complete = True
        self.interrupted_items: set[str] = set()
        self.discarded_items: set[str] = set()
        self.discarded_responses: set[str] = set()
        self.active_response_id: str | None = None
        self._interrupt_before_created = False
        self._audio_responses: dict[str, str] = {}
        self._cancel_event_id: str | None = None
        self.item_times: dict[str, float] = {}
        self.committed_items: set[str] = set()
        self.transcribed_items: set[str] = set()
        self.failed_transcriptions: set[str] = set()
        self._transcription_changed = asyncio.Event()
        self.seen_responses: set[str] = set()
        self.finish_event = asyncio.Event()
        self._finish_task: asyncio.Task[None] | None = None
        self.input_bytes = 0
        self.closing = False
        self.cleanup_timeout = 2.0
        self.transcription_grace = 2.0
        self.disconnected = False

    async def send(self, event: dict[str, Any]) -> None:
        if not self.disconnected:
            await self.socket.send(json.dumps(event, ensure_ascii=False))

    async def request_response(self, *, initial: bool = False) -> None:
        if self.pending:
            self.user_pending = True
            return
        if self.responses >= self.rates.max_responses:
            self.finish_event.set()
            return
        self.responses += 1
        self.pending = True
        self.user_pending = False
        instructions = self.prompt
        if initial:
            instructions += "\nPrima risposta: pronuncia soltanto il saluto iniziale indicato."
        if self.closing or self.responses == self.rates.max_responses:
            instructions += "\nTermina ora: breve saluto, poi finish_call; nessuna nuova domanda."
        await self.send(
            {
                "type": "response.create",
                "response": {
                    "max_output_tokens": self.rates.max_output_tokens,
                    "instructions": instructions,
                },
            }
        )

    async def close_soon(self) -> None:
        if self.disconnected:
            return
        self.closing = True
        if self.pending:
            self.user_pending = True
            cancel_id = f"close-{self.responses}"
            if self._cancel_event_id != cancel_id:
                self._cancel_event_id = cancel_id
                event = {"type": "response.cancel", "event_id": cancel_id}
                if self.active_response_id is not None:
                    event["response_id"] = self.active_response_id
                await self.send(event)
        else:
            await self.request_response()

    @property
    def transcript_complete(self) -> bool:
        return self.committed_items <= self.transcribed_items

    async def _wait_for_transcriptions(self) -> None:
        with suppress(TimeoutError):
            async with asyncio.timeout(self.transcription_grace):
                while self.committed_items - self.transcribed_items - self.failed_transcriptions:
                    self._transcription_changed.clear()
                    await self._transcription_changed.wait()

    def _cancel_finish(self) -> None:
        if self._finish_task:
            self._finish_task.cancel()
            self._finish_task = None

    async def drain_after_disconnect(self) -> None:
        self.disconnected = True
        self._cancel_finish()
        if self.ready:
            try:
                await asyncio.wait_for(self.playback.interrupt(), self.cleanup_timeout)
            except TimeoutError:
                raise VoiceError("Audio cleanup timed out") from None
            await self._wait_for_transcriptions()

    async def _capture(self) -> None:
        while True:
            pcm = await self.endpoint.read()
            if self.disconnected:
                # Keep the receive task alive for already accepted transcription.
                await self.finish_event.wait()
                return
            if not pcm:
                self.finish_event.set()
                return
            if len(pcm) > 48000 or len(pcm) % 2:
                raise VoiceError("Invalid bounded input frame")
            self.input_bytes += len(pcm)
            await self.send(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )

    async def _finish_after_playback(self) -> None:
        await self.playback.queue.join()
        await asyncio.sleep(min(2, max(0, self.endpoint.latency_ms / 1000)))
        # The receiver remains active while trailing input transcription completes.
        await self._wait_for_transcriptions()
        self.finish_event.set()

    async def _call_tool(self, item: dict[str, Any]) -> ToolReply:
        try:
            arguments = json.loads(item.get("arguments", "{}"))
        except (ValueError, TypeError):
            raise VoiceError("Malformed tool arguments") from None
        if not isinstance(arguments, dict):
            raise VoiceError("Tool arguments must be an object")
        return await self.hooks.tool(str(item["name"]), arguments)

    async def handle(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if self.disconnected and kind not in {
            "response.done",
            TRANSCRIPTION_DONE,
            TRANSCRIPTION_FAILED,
        }:
            return
        if kind == "error":
            error = event.get("error")
            if (
                isinstance(error, dict)
                and error.get("code") == "response_cancel_not_active"
                and self._cancel_event_id is not None
                and error.get("event_id") == self._cancel_event_id
            ):
                return
            raise VoiceError("Voice provider rejected a request")
        if kind == "response.created":
            self.active_response_id = str(event["response"]["id"])
            if self._interrupt_before_created:
                self.discarded_responses.add(self.active_response_id)
                self._interrupt_before_created = False
        if kind == "session.updated" and not self.ready:
            self.ready = True
            await self.request_response(initial=True)
        elif kind == "input_audio_buffer.speech_started":
            await self.hooks.activity(speaking=True)
            if self.active_response_id:
                self.discarded_responses.add(self.active_response_id)
            elif self.pending:
                self._interrupt_before_created = True
            self._cancel_finish()
            item_id = str(event.get("item_id", ""))
            self.item_times.setdefault(item_id, time.time())
            current_item = self.playback.current_item
            still_generating = (
                current_item is not None
                and self.active_response_id is not None
                and self._audio_responses.get(current_item) == self.active_response_id
            )
            interrupted = await self.playback.interrupt(still_generating=still_generating)
            for iid, duration in interrupted:
                self.interrupted_items.add(iid)
                self.discarded_items.add(iid)
                await self.hooks.interrupted(iid)
                await self.send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": iid,
                        "content_index": 0,
                        "audio_end_ms": duration,
                    }
                )
        elif kind == "input_audio_buffer.speech_stopped":
            await self.hooks.activity(speaking=False)
        elif kind == "input_audio_buffer.committed":
            item_id = str(event.get("item_id", ""))
            if item_id not in self.committed_items:
                self.committed_items.add(item_id)
                await self.request_response()
        elif kind == "response.output_audio.delta":
            iid = str(event["item_id"])
            response_id = event.get("response_id")
            if isinstance(response_id, str):
                self._audio_responses[iid] = response_id
            self.item_times.setdefault(iid, time.time())
            if response_id in self.discarded_responses and iid not in self.discarded_items:
                self.discarded_items.add(iid)
                self.interrupted_items.add(iid)
                await self.hooks.interrupted(iid)
                await self.send(
                    {
                        "type": "conversation.item.truncate",
                        "item_id": iid,
                        "content_index": 0,
                        "audio_end_ms": 0,
                    }
                )
            if iid not in self.discarded_items:
                try:
                    pcm = base64.b64decode(event["delta"], validate=True)
                except (ValueError, TypeError):
                    raise VoiceError("Malformed audio payload") from None
                self.playback.enqueue(iid, pcm)
        elif kind == TRANSCRIPTION_FAILED:
            iid = str(event["item_id"])
            if iid in self.committed_items:
                self.failed_transcriptions.add(iid)
                self._transcription_changed.set()
        elif kind in {TRANSCRIPTION_DONE, "response.output_audio_transcript.done"}:
            iid = str(event["item_id"])
            if self.disconnected and iid not in self.committed_items:
                return
            text = event.get("transcript", "")
            if not isinstance(text, str) or len(text) > 16000:
                raise VoiceError("Transcript exceeds text limit")
            if text:
                await self.hooks.text(
                    iid,
                    "caller" if kind == TRANSCRIPTION_DONE else "assistant",
                    text,
                    iid in self.interrupted_items,
                    self.item_times.get(iid, time.time()),
                )
            if kind == TRANSCRIPTION_DONE:
                self.transcribed_items.add(iid)
                self._transcription_changed.set()
        elif kind == "response.done":
            response = event["response"]
            rid = str(response["id"])
            if rid in self.seen_responses:
                return
            self.seen_responses.add(rid)
            self._interrupt_before_created = False
            if self.active_response_id == rid:
                self.active_response_id = None
            self.pending = False
            output = response.get("output", [])
            holds: dict[int, ToolReply] = {}
            if response.get("status") == "completed" and rid not in self.discarded_responses:
                # Record the delivery hold before billing or a finish tool can end the call.
                for index, item in enumerate(output):
                    if (
                        item.get("type") == "function_call"
                        and item.get("name") == REPORT_RECIPIENT_CONFLICT
                    ):
                        holds[index] = await self._call_tool(item)
            usage = response.get("usage")
            if not isinstance(usage, dict):
                self.usage_complete = False
                raise VoiceError("Provider usage is unavailable; reservation remains held")
            await self.hooks.usage(rid, usage)
            await self.hooks.activity()
            if self.disconnected:
                return
            if response.get("status") not in {"completed", "cancelled"}:
                self.usage_complete = False
                raise VoiceError("Voice response failed")
            if response.get("status") == "cancelled" or rid in self.discarded_responses:
                if response.get("status") == "completed":
                    for item in output:
                        if item.get("type") == "function_call":
                            await self.send(
                                {
                                    "type": "conversation.item.create",
                                    "item": {
                                        "type": "function_call_output",
                                        "call_id": item["call_id"],
                                        "output": json.dumps(
                                            {"error": "interrupted_response_ignored"}
                                        ),
                                    },
                                }
                            )
                if self.user_pending:
                    await self.request_response()
                return
            called = False
            for index, item in enumerate(output):
                if item.get("type") != "function_call":
                    continue
                reply = holds[index] if index in holds else await self._call_tool(item)
                await self.send(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": item["call_id"],
                            "output": reply.result,
                        },
                    }
                )
                if reply.instructions is not None:
                    self.prompt = reply.instructions
                    self.tools = reply.tools or []
                    await self.send(
                        {
                            "type": "session.update",
                            "session": {
                                "type": "realtime",
                                "instructions": self.prompt,
                                "tools": self.tools,
                            },
                        }
                    )
                if reply.finish:
                    self._finish_task = asyncio.create_task(self._finish_after_playback())
                    return
                called = True
            if called or self.user_pending:
                await self.request_response()

    async def _receive(self) -> None:
        while True:
            raw = await self.socket.recv()
            if len(raw) > 262144:
                raise VoiceError("Voice event exceeds size limit")
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise VoiceError("Malformed voice event")
            await self.handle(event)

    async def run(self) -> None:
        await self.send(session_update(self.prompt, self.tools, self.voice, self.rates))
        # Wait for acknowledgement before exposing microphone audio to the provider.
        async with asyncio.timeout(15):
            while not self.ready:
                raw = await self.socket.recv()
                await self.handle(json.loads(raw))
        tasks = [
            asyncio.create_task(self._capture()),
            asyncio.create_task(self._receive()),
            asyncio.create_task(self.playback.run()),
            asyncio.create_task(self.finish_event.wait()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            if self.pending:
                self.usage_complete = False
            if self._finish_task:
                self._finish_task.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.wait_for(self.endpoint.interrupt(), timeout=self.cleanup_timeout)
            except TimeoutError:
                raise VoiceError("Audio cleanup timed out") from None
