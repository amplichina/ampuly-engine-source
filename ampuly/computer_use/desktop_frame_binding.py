"""Original driver/helper/bridge image composition, with owner authority required."""
from __future__ import annotations
from ampuly.computer_use.desktop_frames import DesktopFrameVault, FrameRefused, FrameApprovalPending, current_route, scope_for


class DesktopFramesBinding:
    def __init__(self, manager):
        self.manager = manager
        # Source is deliberately dynamic for withdrawal/reconfiguration. No
        # metadata field or enabled=True becomes a disclosure authorization.
        def authorize(scope, window, route, nonce):
            source = getattr(manager.engine, 'desktop_frame_authority', None)
            if source is None:
                from ampuly.computer_use.desktop_frame_policy import ensure_policy_authority
                source = ensure_policy_authority(manager)
            if not callable(source): raise FrameRefused('desktop_frame_owner_approval_unavailable')
            if not manager.enabled(): raise FrameRefused('desktop_channel_closed')
            return source(scope, window, route, nonce)
        self.vault = DesktopFrameVault(authorize=authorize)

    async def prepare(self, task, view, helper):
        self.manager._scope(task)
        route = current_route(task)
        from ampuly.computer_use.desktop_frame_policy import ensure_policy_authority
        authority = ensure_policy_authority(self.manager)
        try:
            grant = self.vault.grant(task, view.geometry, route)
        except FrameRefused as exc:
            if exc.reason != 'desktop_frame_owner_approval_required': raise
            raise FrameApprovalPending(await authority.propose(task, view.geometry, route)) from None
        capture = getattr(helper, 'capture_frame', None)
        if not callable(capture): raise FrameRefused('desktop_frame_capture_unavailable')
        result = await capture(view.geometry, grant.masks)
        if type(result) is not tuple or len(result) != 3:
            raise FrameRefused('desktop_frame_capture_unavailable')
        current_view, png, masks = result
        if not view.geometry.matches(current_view.geometry): raise FrameRefused('desktop_frame_observation_changed')
        # The original bridge uses a 30-second coordinate lease. Capturing
        # still uses the helper's five-second geometry; only the already
        # captured immutable frame gets this bounded model-response window.
        from dataclasses import replace
        from ampuly.computer_use.desktop_bridge import coordinate_ttl_sec
        current_view = replace(current_view, geometry=replace(current_view.geometry,
            expires_at=min(grant.expires_at, self.vault.clock() + coordinate_ttl_sec())))
        ref = self.vault.stage(task, current_view.geometry, png, masks, route, grant)
        projection = getattr(self.manager, 'ui_frames', None)
        if projection is not None:
            projection.observe(task, current_view.geometry, png, masks)
        return current_view, ref

    async def action_view(self, task, view, helper):
        sent = self.vault.last_sent
        if sent is None: raise FrameRefused('desktop_frame_not_delivered')
        self.manager._scope(task)
        if not view.geometry.matches(sent['geometry']): raise FrameRefused('desktop_frame_observation_changed')
        # Refuse a dead lease before re-granting. grant() would otherwise
        # report the stored approval as invalid once the vault clock is past
        # it, and would recapture before require_action could say expired.
        now = self.vault.clock()
        grant = sent.get('grant')
        if grant is None or not now < getattr(grant, 'expires_at', 0):
            raise FrameRefused('desktop_frame_expired')
        if not now < getattr(sent['geometry'], 'expires_at', 0):
            raise FrameRefused('coordinate_expired')
        self.vault.grant(task, sent['geometry'], sent['route'])
        capture = getattr(helper, 'capture_frame', None)
        if not callable(capture): raise FrameRefused('desktop_frame_capture_unavailable')
        result = await capture(view.geometry, sent['grant'].masks)
        if type(result) is not tuple or len(result) != 3:
            raise FrameRefused('desktop_frame_capture_unavailable')
        current, png, masks = result
        from ampuly.computer_use.desktop_frames import validate_png, clean_masks
        import hashlib
        validate_png(png, current.geometry)
        if (hashlib.sha256(png).hexdigest() != current.screen_hash
                or clean_masks(masks, current.geometry) != sent['grant'].masks):
            raise FrameRefused('desktop_frame_observation_changed')
        self.vault.require_action(task, current)
        return current

    def input_allowed(self, task):
        if not self.manager.enabled(): return False
        sent = self.vault.last_sent
        if sent is None: return False
        try:
            self.manager._scope(task)
            if sent['scope'] != scope_for(task): return False
            now = self.vault.clock()
            grant = sent.get('grant')
            if grant is None or not now < getattr(grant, 'expires_at', 0): return False
            if not now < sent['geometry'].expires_at: return False
            if self.vault.grant(task, sent['geometry'], sent['route']) != sent['grant']: return False
            return True
        except Exception:
            return False
