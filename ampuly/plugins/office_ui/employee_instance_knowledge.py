"""Owner selection over the original instance store; no new asset store.

The source is the existing engine's EmployeeProjectKnowledge, not a client
path/ref. Current owner verification must run before any source/instance read,
under the original file commit lock, and again before returning a receipt.
"""
from __future__ import annotations

from copy import deepcopy
import re

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_instance_contract import instance_revision_fingerprint
from .employee_instances import _now

_SHA = re.compile(r'[a-f0-9]{64}\Z')
_VERSION = re.compile(r'sha256:[a-f0-9]{64}\Z')
_INSTANCE = re.compile(r'ei-[a-f0-9]{16}\Z')
_PROJECT = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z')
_REF_KEYS = {'kind', 'ref', 'version', 'access_scope'}
PROJECTION = 'employee-project-memory-selection.v1'


def _error(code='storage_record_invalid'):
    raise AIEmployeeContractError(code, '本项目知识来源或当前岗位修订无法核实。') from None


def _reference(descriptor, project):
    keys = _REF_KEYS | {'label', 'content_bytes', 'reader_fingerprint'}
    if (type(descriptor) is not dict or set(descriptor) != keys
            or descriptor.get('kind') != 'enterprise_shared' or descriptor.get('ref') != 'project-memory'
            or descriptor.get('access_scope') != project or descriptor.get('label') != '本项目共享知识'
            or type(descriptor.get('version')) is not str or _VERSION.fullmatch(descriptor['version']) is None
            or type(descriptor.get('reader_fingerprint')) is not str or _SHA.fullmatch(descriptor['reader_fingerprint']) is None
            or type(descriptor.get('content_bytes')) is not int or not 0 < descriptor['content_bytes'] <= 256 * 1024):
        _error('knowledge_source_unavailable')
    return {key: descriptor[key] for key in _REF_KEYS}


class EmployeeInstanceKnowledge:
    def __init__(self, *, repository, source):
        self.repository, self.source = repository, source

    @staticmethod
    def _scope(project, instance_id, revision, fingerprint, verify):
        if (type(project) is not str or _PROJECT.fullmatch(project) is None
                or type(instance_id) is not str or _INSTANCE.fullmatch(instance_id) is None
                or type(revision) is not int or not 1 <= revision < 2**53 - 1
                or type(fingerprint) is not str or _SHA.fullmatch(fingerprint) is None or not callable(verify)):
            _error('request_invalid')
        verify()

    def _instance(self, project, instance_id, revision, fingerprint):
        old = self.repository._load(project, instance_id)
        instance = old[0]
        if instance['instance_revision'] != revision:
            _error('revision_conflict')
        if instance['revision_fingerprint'] != fingerprint:
            _error('fingerprint_mismatch')
        refs = instance.get('knowledge_refs', [])
        if type(refs) is not list or len(refs) > 1:
            _error('knowledge_reference_unsupported')
        if refs:
            # Do not silently overwrite unrelated/private/remote references.
            row = refs[0]
            if (type(row) is not dict or set(row) != _REF_KEYS or row.get('kind') != 'enterprise_shared'
                    or row.get('ref') != 'project-memory' or row.get('access_scope') != project
                    or type(row.get('version')) is not str or _VERSION.fullmatch(row['version']) is None):
                _error('knowledge_reference_unsupported')
        return old

    def candidates(self, project, instance_id, *, expected_revision, expected_fingerprint, verify):
        self._scope(project, instance_id, expected_revision, expected_fingerprint, verify)
        old = self._instance(project, instance_id, expected_revision, expected_fingerprint)
        try:
            first = self.source.descriptor()
            _reference(first, project)
            verify()
            second = self.source.descriptor()
            if canonical_json(first) != canonical_json(second):
                _error('knowledge_source_changed')
            if self.repository._load(project, instance_id)[1] != old[1]:
                _error('revision_conflict')
            verify()
            return {'projection_version': PROJECTION, 'project_id': project, 'instance_id': instance_id,
                'revision': expected_revision, 'fingerprint': expected_fingerprint,
                'candidates': [deepcopy(first)], 'selected': deepcopy(old[0].get('knowledge_refs', [])),
                'effects_produced': False, 'allowed_actions': ['employee_instance_knowledge_candidates',
                    'employee_instance_knowledge_select']}
        except AIEmployeeContractError:
            raise
        except Exception:
            _error('knowledge_source_unavailable')

    def select(self, project, instance_id, *, expected_revision, expected_fingerprint,
               selection, expected_source_version, expected_reader_fingerprint,
               actor, request_id, note='', check_only=False, verify):
        self._scope(project, instance_id, expected_revision, expected_fingerprint, verify)
        if (type(selection) is not str or selection not in {'project-memory', 'none'}
                or type(note) is not str or len(note) > 4000 or type(check_only) is not bool
                or type(actor) is not str or not 0 < len(actor) <= 128
                or type(request_id) is not str or _PROJECT.fullmatch(request_id) is None
                or selection == 'none' and (expected_source_version is not None or expected_reader_fingerprint is not None)
                or selection == 'project-memory' and (type(expected_source_version) is not str
                    or _VERSION.fullmatch(expected_source_version) is None
                    or type(expected_reader_fingerprint) is not str or _SHA.fullmatch(expected_reader_fingerprint) is None)):
            _error('request_invalid')
        action = 'employee_instance_knowledge_select'
        payload = dict(instance_id=instance_id, expected_revision=expected_revision,
            expected_fingerprint=expected_fingerprint, selection=selection,
            expected_source_version=expected_source_version, expected_reader_fingerprint=expected_reader_fingerprint,
            note=note)
        frozen = canonical_json(payload)
        possible_write = retained_receipt = checking_receipt = False
        def guard():
            verify()
            if canonical_json(payload) != frozen:
                _error('request_invalid')
        def current_reference():
            guard()
            if selection == 'none':
                return []
            descriptor = self.source.descriptor()
            row = _reference(descriptor, project)
            if (row['version'] != expected_source_version
                    or descriptor['reader_fingerprint'] != expected_reader_fingerprint):
                _error('knowledge_source_changed')
            guard()
            return [row]
        try:
            guard()
            checking_receipt = True
            receipt = self.repository._receipt(action, project, actor, request_id, payload, require_committed=True)
            checking_receipt = False
            if receipt is not None:
                retained_receipt = True
                guard()
                return receipt
            if check_only:
                _error('outcome_unknown')
            old = self._instance(project, instance_id, expected_revision, expected_fingerprint)
            package_identity = (old[0]['package_id'], old[0]['package_version'], old[0]['package_fingerprint'])
            manifest = self.repository._package(*package_identity)
            manifest_bytes = canonical_json(manifest)
            refs = current_reference()
            instance = {**deepcopy(old[0]), 'knowledge_refs': refs,
                'instance_revision': expected_revision + 1, 'updated_at': _now(), 'updated_by': actor, 'change_note': note}
            instance['revision_fingerprint'] = instance_revision_fingerprint(instance)
            self.repository._validate(instance, manifest)
            instance_bytes = canonical_json(instance)
            def before_commit():
                nonlocal possible_write
                guard()
                if (canonical_json(self.repository._package(*package_identity)) != manifest_bytes
                        or canonical_json(current_reference()) != canonical_json(refs)
                        or canonical_json(instance) != instance_bytes):
                    _error('knowledge_source_changed')
                self.repository._validate(instance, manifest)
                guard()
                possible_write = True
            result = self.repository._save(instance, action=action, actor=actor, request_id=request_id,
                payload=payload, old=old, retest=['knowledge_refs'], before_commit=before_commit)
            guard()
            return result
        except AIEmployeeContractError as exc:
            if possible_write or retained_receipt or checking_receipt and exc.code != 'request_id_reused':
                _error('outcome_unknown')
            raise
        except Exception:
            _error('outcome_unknown' if possible_write or retained_receipt or checking_receipt else 'knowledge_source_unavailable')
