from gateway.run_turn_progress import GatewayTurnProgressMixin


class TurnRunner(GatewayTurnProgressMixin):
    _approval_owner = None

    def __init__(self, runner, ctx):
        self._runner, self._ctx = runner, ctx

    def _setup_stream_consumer(self, platform_key):
        ctx = self._ctx
        stream_consumer = self._runner.stream_consumer
        delta_sinks = [stream_consumer]

        def stream_delta_cb(text):
            if ctx._run_still_current():
                for sink in delta_sinks:
                    sink.on_delta(text)

        def interim_assistant_cb(text, *, already_streamed=False):
            if not ctx._run_still_current():
                return
            stream_consumer.on_segment_break() if already_streamed else stream_consumer.on_commentary(text)

        return stream_consumer, stream_delta_cb, interim_assistant_cb, True

    def _wire_turn_agent_callbacks(self, agent, turn_route, reasoning_config,
                                   stream_delta_cb, interim_assistant_cb, want_interim_messages):
        ctx = self._ctx
        agent.tool_progress_callback = ctx.progress_callback
        agent.tool_start_callback = ctx.native_tool_start_callback
        agent.tool_complete_callback = ctx.native_tool_complete_callback
        agent.stream_delta_callback = stream_delta_cb
        agent.interim_assistant_callback = interim_assistant_cb if want_interim_messages else None

    def _clarify_callback_sync(self, question, choices, multi_select=False):
        ctx = self._ctx
        return ctx.wait_for_clarify(question, choices)

    def _approval_notify_sync(self, approval_data):
        if self._approval_owner is not None:
            self._approval_owner.check_approval_generation()
        self._render_approval_sync(approval_data)
        if self._approval_owner is not None:
            self._approval_owner.register_approval(self._ctx.session_key, approval_data)

    def _render_approval_sync(self, approval_data):
        ctx = self._ctx
        ctx.send_approval(ctx.session_key or "", approval_data)
