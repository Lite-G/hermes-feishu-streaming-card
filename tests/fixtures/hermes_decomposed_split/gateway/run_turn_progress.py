class GatewayTurnProgressMixin:
    def _status_callback_sync(self, event_type, message):
        ctx = self._ctx
        ctx.status_queue.put((event_type, message))
