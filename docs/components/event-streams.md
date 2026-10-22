# Event Streaming

_event_stream is the async generator behind the POST /chat StreamingResponse. It turns the output of chat_service.stream(...) into Server-Sent Events in OpenAI's chat.completion.chunk format.

## Steps

Setup. It creates one completion_id (chatcmpl-<uuid>) that every chunk in the response shares. It also creates a threading.Event called cancel, which it passes to the chat service so the agent can be told to stop.

Opening chunk. It sends {"role": "assistant"}, the usual first delta in OpenAI's format.

Translating service items. For each item from chat_service.stream:

Heartbeat becomes : keepalive, an SSE comment. Clients ignore it, but it stops proxies or load balancers from closing an idle connection while the agent is working.
Status becomes {"status": ...} (for example "searching" or "writing"), for progress updates in the UI.
Anything else is the answer. It sends:
{"content": text}, the whole answer in one chunk, because it is checked against the documentation before it is sent.
{"grounding_status": ...} if one is set (SUPPORTED, PARTIALLY_SUPPORTED or NOT_SUPPORTED).
{"citations": [...]} if there are any. Each citation is reshaped to {title, location: {uri}, score}.
Normal end. It sends an empty delta with finish_reason: "stop".

Errors. The HTTP 200 status and headers have already been sent, so it can't return an error status. Instead, it logs the exception and sends a {"error": ...} event inside the stream.

Cleanup. The finally block always calls cancel.set(). If the client disconnects, Starlette closes the generator, which raises GeneratorExit at the current yield. This isn't caught by except Exception, so the finally runs and the agent stops instead of continuing to work for nobody.

Terminator. It sends data: [DONE] after a normal finish or a handled error. It does not send it after a disconnect, because the GeneratorExit propagates past it.