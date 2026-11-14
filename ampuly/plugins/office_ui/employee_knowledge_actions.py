"""Finite owner knowledge API; injected old instance store and source only."""
from __future__ import annotations
from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .employee_instance_knowledge import PROJECTION, _reference, _VERSION, _REF_KEYS

KNOWLEDGE_WRITE = frozenset({'employee_instance_knowledge_select'})
KNOWLEDGE_READ = frozenset({'employee_instance_knowledge_candidates'})
KNOWLEDGE_FIELDS = {
    'employee_instance_knowledge_candidates': {'instance_id', 'expected_revision', 'expected_fingerprint'},
    'employee_instance_knowledge_select': {'instance_id', 'expected_revision', 'expected_fingerprint',
        'selection', 'expected_source_version', 'expected_reader_fingerprint', 'note', 'check_only'},
}
_MESSAGES = {
    'request_invalid': '知识选择请求无效。', 'unsupported': '当前项目尚未提供可核实的知识来源。',
    'owner_required': '只有当前项目所有者可以配置知识。', 'not_allowed_in_status': '当前项目不能修改知识配置。',
    'revision_conflict': '岗位已有新修订，请保留编辑并重新读取。',
    'fingerprint_mismatch': '岗位依据已变化，请重新读取。',
    'knowledge_source_unavailable': '本项目知识来源暂不可核实，没有返回内容或扩大授权。',
    'knowledge_source_changed': '知识内容或读取器已变化，请重新读取并明确选择版本。',
    'knowledge_reference_unsupported': '此岗位有其它知识引用，不能用本入口自动覆盖。',
    'outcome_unknown': '保存结果尚不确定，请核对原请求；不要重新保存。',
    'request_id_reused': '请求标识已用于不同内容，未继续保存。',
}


def dispatch_knowledge_action(message, *, service, actor, project_id, verify):
    action, request_id = message['type'], message['request_id']
    writing = action in KNOWLEDGE_WRITE
    invoked = False
    confirmed_write = False
    try:
        verify()
        if service is None:
            raise AIEmployeeContractError('unsupported', '')
        common = dict(expected_revision=message.get('expected_revision'),
            expected_fingerprint=message.get('expected_fingerprint'), verify=verify)
        if not writing:
            value = service.candidates(project_id, message.get('instance_id'), **common)
            if (type(value) is not dict or value.get('projection_version') != PROJECTION
                    or value.get('project_id') != project_id or value.get('instance_id') != message.get('instance_id')
                    or value.get('revision') != message.get('expected_revision')
                    or value.get('fingerprint') != message.get('expected_fingerprint')
                    or type(value.get('candidates')) is not list or len(value['candidates']) != 1
                    or type(value.get('selected')) is not list or len(value['selected']) > 1
                    or value.get('effects_produced') is not False):
                raise AIEmployeeContractError('knowledge_source_unavailable', '')
            _reference(value['candidates'][0], project_id)
            for row in value['selected']:
                if (type(row) is not dict or set(row) != _REF_KEYS or row.get('kind') != 'enterprise_shared'
                        or row.get('ref') != 'project-memory' or row.get('access_scope') != project_id
                        or type(row.get('version')) is not str or _VERSION.fullmatch(row['version']) is None):
                    raise AIEmployeeContractError('knowledge_source_unavailable', '')
            # Public response is constructed, never forwarding an injected
            # repository/source's extra path/content or credential fields.
            result = {key: value[key] for key in ('projection_version', 'project_id', 'instance_id',
                'revision', 'fingerprint', 'candidates', 'selected')}
            result.update(ok=True, action=action, request_id=request_id, result='success',
                effects_produced=False, problems=[], allowed_actions=[*KNOWLEDGE_READ, *KNOWLEDGE_WRITE])
        else:
            invoked = True
            value = service.select(project_id, message.get('instance_id'), **common,
                selection=message.get('selection'), expected_source_version=message.get('expected_source_version'),
                expected_reader_fingerprint=message.get('expected_reader_fingerprint'),
                actor=actor, request_id=request_id, note=message.get('note', ''), check_only=message.get('check_only', False))
            if (type(value) is not dict or value.get('action') != action or value.get('project_id') != project_id
                    or value.get('instance_id') != message.get('instance_id') or value.get('request_id') != request_id
                    or value.get('ok') is not True or value.get('result') != 'success'
                    or type(value.get('revision')) is not int or value['revision'] != message['expected_revision'] + 1
                    or value.get('effects_produced') is not True):
                raise AIEmployeeContractError('outcome_unknown', '')
            result = {key: value[key] for key in ('ok', 'action', 'project_id', 'instance_id', 'request_id',
                'revision', 'fingerprint', 'result', 'effects_produced')}
            confirmed_write = True
            result.update(retest_scope=['knowledge_refs'], problems=[], allowed_actions=['employee_instance_detail'])
        verify()
        canonical_json(result)
        return {'type': 'ack', 'payload': result}
    except AIEmployeeContractError as exc:
        code = exc.code if exc.code in _MESSAGES else 'knowledge_source_unavailable'
    except Exception:
        code = 'outcome_unknown' if invoked else 'knowledge_source_unavailable'
    if confirmed_write or invoked and code not in {'request_invalid', 'owner_required', 'not_allowed_in_status', 'revision_conflict',
            'fingerprint_mismatch', 'knowledge_source_unavailable', 'knowledge_source_changed',
            'knowledge_reference_unsupported', 'request_id_reused'}:
        code = 'outcome_unknown'
    return {'type': 'ack', 'payload': dict(ok=False, action=action, request_id=request_id,
        project_id=project_id, revision=None, effects_produced=None if code == 'outcome_unknown' else False,
        result='outcome_unknown' if code == 'outcome_unknown' else 'failed', error=code, message=_MESSAGES[code],
        problems=[], allowed_actions=[])}
