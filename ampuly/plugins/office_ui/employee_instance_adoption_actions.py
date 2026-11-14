"""Explicit project adoption on the existing repository and CAS transaction.

Creation/configuration is not execution readiness. Private configuration is
intent; actual package, scope, model/tool/human authorization and pinned runs
remain server facts. No background task, model, new store or grant is created.
"""
from copy import deepcopy
import inspect
import re

from .ai_employee_contract import AIEmployeeContractError, canonical_json
from .ai_employee_storage import AIEmployeeFileStore
from .employee_instances import _model_configuration
from .employee_lifecycle_actions import _plain

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9_-]{2,63}\Z")
_INSTANCE = re.compile(r"ei-[a-f0-9]{16}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_COMMON = {'type', 'project_id', 'request_id'}
INSTANCE_ADOPTION_FIELDS = {
    'employee_instance_create': frozenset({'package_id', 'package_version', 'expected_fingerprint', 'config', 'note', 'check_only'}),
    'employee_instance_adopt': frozenset({'instance_id', 'package_version', 'expected_fingerprint', 'expected_revision', 'note', 'check_only'}),
}
INSTANCE_ADOPTION_WRITE = frozenset(INSTANCE_ADOPTION_FIELDS)
_CONFIG_INTENT = {'display_name', 'params', 'knowledge_refs', 'model_config', 'connector_refs', 'human_assignments', 'limits'}
_MESSAGES = {
    'request_invalid': '采用参数无效；不能自报实例身份、状态或能力授权。',
    'owner_required': '当前项目所有者授权已失效或发生变化。',
    'not_allowed_in_status': '项目或固定岗位版本当前不能被采用。',
    'version_not_available': '指定的不可变岗位版本不可用。',
    'publication_index_unverified': '固定岗位版本的当前发布锚不能核实。',
    'revision_conflict': '项目岗位已变化，请保留设置后重新查看。',
    'fingerprint_mismatch': '岗位版本与固定指纹不一致，或采用前已变化。',
    'request_id_reused': '同一请求标识不能更换内容或操作人。',
    'validation_failed': '配置与固定岗位合同不一致。',
    'unsupported': '项目岗位采用入口尚未接通。',
    'outcome_unknown': '采用结果未知；只能核对原请求，不要换标识或重复创建。',
    'storage_record_invalid': '采用记录暂不能核实。',
}


def _fail(code='request_invalid'):
    raise AIEmployeeContractError(code, _MESSAGES.get(code, _MESSAGES['storage_record_invalid']))


def _valid(value, pattern):
    return type(value) is str and pattern.fullmatch(value) is not None


def _text(value, maximum, *, empty=False):
    return (type(value) is str and len(value) <= maximum and (empty or bool(value.strip()))
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _request(message):
    _plain(message, 128 * 1024)
    if type(message) is not dict or type(message.get('type')) is not str or message['type'] not in INSTANCE_ADOPTION_FIELDS:
        _fail('unsupported')
    action = message['type']
    expected = _COMMON | set(INSTANCE_ADOPTION_FIELDS[action])
    if (set(message) not in (expected, expected - {'check_only'})
            or not _valid(message.get('project_id'), _ID) or not _valid(message.get('request_id'), _ID)
            or not _valid(message.get('expected_fingerprint'), _SHA)
            or not _text(message.get('package_version'), 128)
            or message['package_version'].strip() != message['package_version']
            or not _text(message.get('note'), 4000, empty=True)
            or type(message.get('check_only', False)) is not bool):
        _fail()
    if action == 'employee_instance_create':
        config = message['config']
        if (not _valid(message['package_id'], _PACKAGE) or type(config) is not dict
                or set(config) - _CONFIG_INTENT):
            _fail()
        if 'display_name' in config and not _text(config['display_name'], 256):
            _fail()
        if 'model_config' in config:
            _model_configuration(config['model_config'])  # Selection intent only; never credential/grant.
        for key in ('params', 'human_assignments', 'limits'):
            if key in config and type(config[key]) is not dict:
                _fail()
        for key in ('knowledge_refs', 'connector_refs'):
            if key in config and (type(config[key]) is not list or len(config[key]) > 500):
                _fail()
    elif (not _valid(message['instance_id'], _INSTANCE)
            or type(message['expected_revision']) is not int or not 1 <= message['expected_revision'] < 2**53 - 1):
        _fail()
    return deepcopy(message)


def _error(action, project_id, request_id, code, *, invoked=False, check_only=False):
    code = code if type(code) is str and code in _MESSAGES else 'storage_record_invalid'
    unknown = invoked or code == 'outcome_unknown'
    return {'type': 'ack', 'payload': {'ok': False, 'action': action, 'project_id': project_id,
        'request_id': request_id, 'revision': None, 'result': 'outcome_unknown' if unknown else 'unsupported' if code == 'unsupported' else 'failed',
        'effects_produced': False if check_only or not invoked else None,
        'original_effects_produced': None, 'external_effects_produced': False if check_only or not invoked else None,
        'error': code, 'message': _MESSAGES[code], 'allowed_actions': [],
        'problems': [{'code': code, 'severity': 'error', 'path': '', 'message': _MESSAGES[code],
                      'hint': '保留原请求和配置；仅用 check_only 核对，不自动继续写入。'}]}}


def dispatch_instance_adoption_action(message, *, repository, actor: str, project_id: str, verify):
    """Server-owned verify() must freshly confirm socket owner + active project.

    It is passed into the ORIGINAL file store's pre-intent callback and receipt
    checks. check_only uses that same original payload and cannot start a write.
    """
    action = message.get('type') if type(message) is dict else None
    action = action if type(action) is str and action in INSTANCE_ADOPTION_FIELDS else ''
    request_id = message.get('request_id') if type(message) is dict else None
    request_id = request_id if _valid(request_id, _ID) else None
    invoked, check_only = False, False
    try:
        request = _request(message)
        check_only = request.get('check_only', False)
        frozen = canonical_json(message)
        if request['project_id'] != project_id or not _text(actor, 128) or not callable(verify):
            _fail('owner_required')
        def guard():
            AIEmployeeFileStore._before_commit(verify)
            if canonical_json(message) != frozen:
                _fail()
        guard()
        method = getattr(repository, 'create' if action == 'employee_instance_create' else 'adopt', None)
        if not callable(method) or inspect.iscoroutinefunction(method):
            _fail('unsupported')
        arguments = {key: deepcopy(request[key]) for key in INSTANCE_ADOPTION_FIELDS[action]
                     if key in request and key != 'check_only'}
        arguments.update(actor=actor, request_id=request_id, before_commit=guard, check_only=check_only)
        guard()
        invoked = True
        raw = method(project_id, **arguments)
        if inspect.isawaitable(raw):
            if inspect.iscoroutine(raw):
                raw.close()
            _fail('storage_record_invalid')
        guard()
        if type(raw) is not dict:
            _fail('storage_record_invalid')
        if raw.get('result') == 'outcome_unknown' and raw.get('ok') is False:
            if raw.get('request_id') != request_id:
                _fail('storage_record_invalid')
            return _error(action, project_id, request_id, 'outcome_unknown', invoked=True, check_only=check_only)
        expected_revision = 1 if action == 'employee_instance_create' else request['expected_revision'] + 1
        expected_retest = [] if action == 'employee_instance_create' else ['package_version', 'model_config', 'capability_impls']
        if (raw.get('ok') is not True or raw.get('action') != action or raw.get('project_id') != project_id
                or raw.get('request_id') != request_id or raw.get('result') != 'success'
                or raw.get('effects_produced') is not True or not _valid(raw.get('instance_id'), _INSTANCE)
                or action == 'employee_instance_adopt' and raw['instance_id'] != request['instance_id']
                or type(raw.get('revision')) is not int or raw['revision'] != expected_revision
                or not _valid(raw.get('fingerprint'), _SHA) or raw.get('retest_scope') != expected_retest
                or raw.get('allowed_actions') != ['employee_instance_detail']):
            _fail('storage_record_invalid')
        result = {'ok': True, 'action': action, 'project_id': project_id, 'request_id': request_id,
            'instance_id': raw['instance_id'], 'revision': raw['revision'], 'fingerprint': raw['fingerprint'],
            'package_version': request['package_version'], 'package_fingerprint': request['expected_fingerprint'],
            'result': 'success', 'effects_produced': not check_only,
            'original_effects_produced': True, 'external_effects_produced': False,
            'retest_scope': expected_retest, 'allowed_actions': ['employee_instance_detail'], 'problems': []}
        if action == 'employee_instance_create':
            result['package_id'] = request['package_id']
        if raw.get('idempotent') is True:
            result['idempotent'] = True
        # A saved instance is not green/ready and does not authorize execution.
        return {'type': 'ack', 'payload': result}
    except AIEmployeeContractError as exc:
        return _error(action, project_id, request_id, exc.code, invoked=invoked, check_only=check_only)
    except Exception:
        return _error(action, project_id, request_id, 'storage_record_invalid', invoked=invoked, check_only=check_only)
