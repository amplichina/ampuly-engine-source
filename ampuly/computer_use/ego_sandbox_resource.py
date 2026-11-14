"""Explicit operator resource factory; never import a personal browser profile.

Construction is inert. An authorized composition calls this factory only after
checking its current order grant. Native profile creation/selection is a separate
explicit operator action; an Ampuly BrowserSpaces row is not a native profile.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs,urlsplit

from ampuly.layer4_tools.browser_backend import Action
from ampuly.layer4_tools.browser_spaces import BrowserSpaces
from ampuly.layer4_tools.ego_backend import EgoBackend
from ampuly.layer4_tools.ego_sandbox_page import EgoSandboxPage

ORIGIN = 'http://127.0.0.1:8899'


class EgoResourceRefused(RuntimeError):
    pass


def create_ego_binding_for_engine(engine):
    """Optional process-only composition; disabled unless the launcher opted in.

    No setting is written. The selected project still needs fresh Owner order
    consent; this switch merely makes its already-authorized Ego resource
    factory available. Other projects retain their existing browser backend.
    """
    if os.environ.get('AMPULY_EGO_LAUNCH_SCOPE') != 'owner-sandbox-v1':
        return None
    project = os.environ.get('AMPULY_EGO_PROJECT_ID', '')
    if project != str(getattr(engine, 'project_id', '')):
        return None
    profile = os.environ.get('AMPULY_EGO_PROFILE_ID', '')
    binary = os.environ.get('AMPULY_EGO_BINARY', '')
    if not profile or not binary:
        raise EgoResourceRefused('ego_explicit_launch_parameters_missing')
    from .ego_engine_binding import EgoEngineBinding
    factory = EgoSandboxResourceFactory(native_profile_id=profile,binary=Path(binary),
        evidence_root=Path(engine.ampuly_home)/'browser'/'ego-native')
    return EgoEngineBinding(engine,resource_factory=factory)


class EgoSandboxResourceFactory:
    def __init__(self, *, native_profile_id: str, binary: Path, evidence_root: Path):
        if (type(native_profile_id) is not str or not native_profile_id.strip()
                or native_profile_id != native_profile_id.strip()
                or native_profile_id.lower() == 'default' or len(native_profile_id)>256
                or any(ord(c)<32 for c in native_profile_id)):
            raise EgoResourceRefused('ego_explicit_native_profile_required')
        self.profile_id=native_profile_id
        self.binary=Path(binary)
        self.evidence_root=Path(evidence_root)
        self._lock=asyncio.Lock()
        self._attempted:set[tuple[str,...]]=set()
        self._backends:dict[tuple[str,...],EgoBackend]={}
        self._completed:dict[tuple[str,...],dict[str,Any]]={}

    @staticmethod
    def _scope(task:Any,scope:Mapping,initial_url:str):
        if not isinstance(scope,Mapping):raise EgoResourceRefused('ego_server_scope_required')
        task_id=scope.get('task_id');project=scope.get('project_id');run=scope.get('run_id')
        batch=scope.get('batch_id');order=scope.get('order_id');budget=scope.get('budget_grant_id')
        if (any(type(v) is not str or not v or len(v)>256 for v in (task_id,project,run,batch,order,budget))
                or getattr(task,'id',None)!=task_id or getattr(task,'project_id',None)!=project):
            raise EgoResourceRefused('ego_server_scope_invalid')
        try:
            p=urlsplit(initial_url)
            if (p.scheme!='http' or p.netloc!='127.0.0.1:8899' or p.path!='/admin'
                    or p.fragment or parse_qs(p.query,keep_blank_values=True)!={'batch':[batch]}):
                raise EgoResourceRefused('ego_initial_url_invalid')
        except (ValueError,TypeError):raise EgoResourceRefused('ego_initial_url_invalid') from None
        # One native TaskSpace for this authorized order/run. Member tasks
        # share it through the Host's same-space serialization, never create
        # twelve competing browser windows for twelve employee roles.
        return (project,run,order,budget,batch),batch

    def _create(self, *, task,scope,initial_url):
        key,batch=self._scope(task,scope,initial_url)
        if not self.binary.is_file():raise EgoResourceRefused('ego_binary_unavailable')
        # File names never contain a credential/profile display name or URL.
        suffix=hashlib.sha256(('\0'.join(key)).encode('utf8')).hexdigest()[:24]
        spaces=BrowserSpaces(self.evidence_root)
        backend=EgoBackend(spaces=spaces,profile_id='egop-'+hashlib.sha256(self.profile_id.encode()).hexdigest()[:16],
            task_id='egot-'+suffix,binary=str(self.binary),ego_profile_id=self.profile_id,timeout_sec=30.0)
        self._backends[key]=backend
        initial=backend.snapshot()
        if initial.get('ok') is not True or initial.get('unknown') is True:
            raise EgoResourceRefused('ego_native_space_initialization_unconfirmed')
        navigated=backend.act(Action('navigate',target=initial_url))
        if navigated.get('ok') is not True or navigated.get('unknown') is True:
            raise EgoResourceRefused('ego_initial_navigation_unconfirmed')
        adapter=EgoSandboxPage(backend,expected_origin=ORIGIN,batch_id=batch)
        observation=adapter.inspect()
        if observation.get('ok') is not True or observation.get('url')!=initial_url:
            raise EgoResourceRefused('ego_initial_observation_unconfirmed')
        if backend.lifecycle_state!='active' or type(backend._ego_space_id) is not int:
            raise EgoResourceRefused('ego_native_space_binding_unconfirmed')
        return {'adapter':adapter,'profile_id':self.profile_id,'space_id':backend._ego_space_id,
                'inspection':observation}

    async def __call__(self, *, task,scope,initial_url):
        key,_=self._scope(task,scope,initial_url)
        async with self._lock:
            completed=self._completed.get(key)
            if completed is not None:
                if completed['adapter'].backend.lifecycle_state!='active':
                    raise EgoResourceRefused('ego_existing_resource_not_active')
                return dict(completed)
            if key in self._attempted:
                raise EgoResourceRefused('ego_resource_attempt_already_exists')
            # A failed/unknown creation is not permission to open another space.
            self._attempted.add(key)
            worker=asyncio.create_task(asyncio.to_thread(self._create,task=task,scope=scope,initial_url=initial_url))
            try:
                created=await asyncio.shield(worker)
                self._completed[key]=created
                return dict(created)
            except asyncio.CancelledError:
                # No background bootstrap may outlive this resource lock. This
                # only runs fixed GET/bootstrap operations, never business clicks.
                while not worker.done():
                    try:await asyncio.shield(worker)
                    except asyncio.CancelledError:continue
                    except Exception:break
                if worker.done() and not worker.cancelled():worker.exception()
                backend=self._backends.get(key)
                if backend is not None:backend._save_binding('unknown')
                raise
