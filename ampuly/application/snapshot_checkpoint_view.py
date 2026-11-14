"""Ephemeral project-scoped checkpoint rows for a single read-only UI request."""
from collections import defaultdict

class SnapshotCheckpointView:
    def __init__(self, project_id, rows):
        self.project_id=str(project_id)
        self.rows=tuple(cp for cp in rows if str(cp.project_id)==self.project_id and cp.status in {'pending','resuming'})
        self.pending=tuple(cp for cp in self.rows if cp.status=='pending')
        self.by_session=defaultdict(list)
        self.pending_by_session=defaultdict(list)
        self.waiting_results={}
        self.work_items={}
        self._pending_member_index=None
        self._runtime_candidates={}
        self._candidate_identity_indexes={}
        for cp in self.rows:
            session=str(cp.session_id or '').strip()
            self.by_session[session].append(cp)
            if cp.status=='pending': self.pending_by_session[session].append(cp)

    def runtime_pending_candidates(self, identity_index, runtime_identity):
        """Ordered membership candidates, local to this immutable read view.

        Build reverse membership buckets once for the request, not once per
        runtime. Visibility, suspension and waiting-state rules stay with the
        existing engine selector. Retain index references so object ids cannot
        be reused within this view's lifetime.
        """
        if self._pending_member_index is False:
            return None  # Unknown row shape keeps the legacy ordered selector.
        task_ids=tuple(runtime_identity.runtime_task_ids)
        membership=tuple((tid, str(getattr(identity_index.task(tid), 'session_id', '') or '').strip())
                         for tid in task_ids if identity_index.task(tid) is not None)
        key=(id(identity_index), str(runtime_identity.runtime_session_id), task_ids, membership)
        cached=self._runtime_candidates.get(key)
        if cached is not None:
            return cached
        if self._pending_member_index is None:
            by_session=defaultdict(list)
            by_task=defaultdict(list)
            for position, checkpoint in enumerate(self.pending):
                if not checkpoint.task_id and not isinstance(checkpoint.payload, dict):
                    # Do not inspect or normalize a row which the legacy selector
                    # may hide before reading membership (manager/suspend rows).
                    # Visible malformed rows must retain their original failure.
                    self._pending_member_index=False
                    return None
                session=str(checkpoint.session_id or '').strip()
                task=str(checkpoint.task_id or checkpoint.payload.get('waiting_task_id')
                         or checkpoint.payload.get('task_id') or '').strip()
                by_session[session].append((position, checkpoint))
                by_task[task].append((position, checkpoint))
            self._pending_member_index=(by_session, by_task)
        by_session, by_task=self._pending_member_index
        visible_tasks=frozenset(task_ids)
        visible_sessions=frozenset(session for _, session in membership)
        matches={}
        for session in visible_sessions:
            matches.update(by_session.get(session, ()))
        for task in visible_tasks:
            matches.update(by_task.get(task, ()))
        result=(visible_tasks, visible_sessions, tuple(matches[position] for position in sorted(matches)))
        self._candidate_identity_indexes[id(identity_index)]=identity_index
        self._runtime_candidates[key]=result
        return result

    async def task_is_waiting(self, engine, checkpoint, identity_index):
        """Pure stale-card filtering; UI reads never lazily resolve rows."""
        from ampuly.layer2_organization.company_runtime_identity import card_waiting_task_id
        from ampuly.layer2_organization.work_item_links import linked_work_item_id_for_task
        if checkpoint.checkpoint_type not in engine._TASK_WAIT_CHECKPOINT_TYPES:
            return True
        task_id=card_waiting_task_id(checkpoint)
        if not task_id:
            return True
        if task_id in self.waiting_results:
            return self.waiting_results[task_id]
        task=identity_index.tasks_by_id.get(task_id) if identity_index is not None else None
        live=task is not None and str(getattr(task.status, 'value', task.status)) not in {'done','failed','cancelled'}
        if live:
            work_item_id=linked_work_item_id_for_task(task)
            if work_item_id:
                if work_item_id not in self.work_items:
                    self.work_items[work_item_id]=await engine.store.get_delegation_work_item(work_item_id)
                item=self.work_items[work_item_id]
                live=item is not None and str(getattr(item.phase,'value',item.phase)) not in {'approved','failed','cancelled'}
        self.waiting_results[task_id]=live
        return live
