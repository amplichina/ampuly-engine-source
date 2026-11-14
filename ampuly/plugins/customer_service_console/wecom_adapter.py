"""Exact-command text transport over the original authenticated relay."""
from copy import deepcopy
from hashlib import sha256

from ampuly.components.customer_service.core.contracts import canonical_json


class WecomAdapter:
    VERSION = "wecom-cs-v1"

    def __init__(self, channel, account, turn, context, check):
        self.channel, self.account, self.turn, self.context = channel, account, turn, context
        self.check, self.runtime, self.proofs = check, None, {}

    def _local(self):
        from ampuly.plugins.wecom_service.wecom_channel import _window
        self.check()
        current = self.channel.store.conversation(self.turn.conv["conversation_id"])
        if (not current or any(current.get(name) != self.turn.conv.get(name)
                for name in ("project_id", "conversation_id", "open_kfid", "external_userid"))
                or current.get("stage") in {"handoff", "closed"}):
            raise ValueError("customer_service_conversation_changed")
        if (self.channel.transport is None or not self.channel.transport.connected
                or not self.channel._lock.locked()):
            raise ValueError("customer_service_transport_unavailable")
        self.turn.window = _window(current)

    async def snapshot(self, context):
        from ampuly.plugins.wecom_service.wecom_kf import build_service_state_get, parse_service_state_get_response
        if context != self.context:
            raise ValueError("customer_service_scope_changed")
        self._local()
        transport = self.channel.transport
        result = await transport.service_state_get(build_service_state_get(
            open_kfid=context.account_id, external_userid=self.turn.conv["external_userid"]))
        self._local()
        if self.channel.transport is not transport:
            raise ValueError("customer_service_transport_changed")
        state = parse_service_state_get_response(result).state
        self.turn.window = self.turn.window.with_service_state(state)
        self.channel._save(self.turn)
        return {"context": context, "version": self.VERSION,
                "ready": self.turn.window.can_send(self.channel._clock()).ok,
                "manual_active": state in (2, 3)}

    async def send(self, command):
        from ampuly.components.customer_service.core.api_acceptance import ApiAcceptedReceipt, request_msgid
        from ampuly.plugins.wecom_service.wecom_kf import build_send_text, TEXT_MAX_BYTES
        from ampuly.plugins.wecom_service.wecom_relay_client import WecomRelayClient
        self._local()
        if type(self.channel.transport) is not WecomRelayClient:
            raise ValueError("customer_service_authenticated_relay_required")
        transport = self.channel.transport
        if command["context"] != self.context or len(command["text"].encode("utf-8")) > TEXT_MAX_BYTES:
            raise ValueError("customer_service_exact_reply_invalid")
        msgid = request_msgid(command["command_id"])
        payload = build_send_text(touser=self.turn.conv["external_userid"], open_kfid=self.context.account_id,
                                  content=command["text"], msgid=msgid)
        # Do not fit/trim the frozen text after policy authorization.
        async def final_check():
            self._local()
            if not self.turn.window.can_send(self.channel._clock()).ok:
                raise ValueError("customer_service_window_closed")
            await self.runtime.validate_execution_fence_async(command["execution_fence"], command)
            self._local()
            if self.channel.transport is not transport:
                raise ValueError("customer_service_transport_changed")
            if not self.turn.window.can_send(self.channel._clock()).ok:
                raise ValueError("customer_service_window_closed")
            self.channel.store.record_outbound(project_id=self.account["project_id"],
                conversation_id=self.turn.conv["conversation_id"], open_kfid=self.context.account_id,
                external_userid=self.turn.conv["external_userid"], msgid=msgid, msgtype="text",
                text=command["text"], kind="cs_autonomous", send_time=int(self.channel._clock()))
            self.turn.window = self.turn.window.record_sent()
            self.channel._save(self.turn)
        observed = await transport.send_msg_observed(payload, before_send=final_check)
        answer = observed["response"]
        if (type(answer) is not dict or type(answer.get("errcode")) is not int
                or answer["errcode"] != 0 or answer.get("msgid") != msgid):
            return None  # Never refund a potentially spent send or retry it.
        proof = {"schema_version": "ampuly.cs.api-acceptance/1", "context": self.context.to_dict(),
            "command_id": command["command_id"], "body_sha256": sha256(command["text"].encode()).hexdigest(),
            "request_msgid": msgid, "response_msgid": answer["msgid"], "errcode": 0,
            **{name: observed[name] for name in ("relay_request_id", "request_sha256", "response_sha256", "response_envelope_sha256")}}
        proof_hash = sha256(canonical_json(proof)).hexdigest()
        self.proofs[proof_hash] = proof
        return ApiAcceptedReceipt(self.context, command["command_id"], proof_hash)

    def read_proof(self, receipt):
        return deepcopy(self.proofs.get(receipt.proof_sha256))

    async def lookup(self, command):
        from ampuly.components.customer_service.core.api_acceptance import ApiAcceptedReceipt
        proof = self.runtime.read_api_acceptance(command["command_id"])
        if proof is None:
            return None  # No platform lookup API; unresolved stays unknown.
        proof_hash = sha256(canonical_json(proof)).hexdigest()
        self.proofs[proof_hash] = proof
        return ApiAcceptedReceipt(self.context, command["command_id"], proof_hash)
