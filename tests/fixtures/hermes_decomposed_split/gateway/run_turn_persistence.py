class GatewayTurnPersistenceMixin:
    async def _hmwa_post_turn_hooks(self, hook_ctx, agent_result, response):
        await self.hooks.emit("agent:end", {**hook_ctx, "response": response})

    async def _hmwa_deliver_turn_response(
        self, event, source, session_entry, session_key, run_generation,
        agent_result, agent_messages, response, _footer_line, _intentional_silence,
    ):
        if _intentional_silence:
            response = ""
        if agent_result.get("already_sent") and not agent_result.get("failed"):
            await self._deliver_media_from_response(response, event, None)
            return None
        return response
