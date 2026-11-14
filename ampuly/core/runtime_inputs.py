"""Owned, immutable inputs for one isolated custom runtime.

Data only: the host verifies source/receipt and authority. This object never
issues grants, changes model settings or replaces Task/Run/Effect storage.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

_SHA = re.compile(r"[a-f0-9]{64}\Z")
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}\Z")


class RuntimeInputsError(RuntimeError):
    def __init__(self, code='runtime_inputs_unavailable'):
        self.code=code
        super().__init__(code)


def _json(raw):
    try:
        if type(raw) is not bytes or len(raw)>512*1024:raise ValueError
        value=json.loads(raw,parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if type(value) is not dict:raise ValueError
        return value
    except (ValueError,TypeError,UnicodeError,RecursionError):
        raise RuntimeInputsError() from None


def _skill_path(path):
    if (type(path) is not str or not path or len(path)>240 or '\\' in path or ':' in path
            or any(c in path for c in '?*"<>|') or any(ord(c)<32 for c in path)):
        raise RuntimeInputsError()
    parts=path.split('/')
    if len(parts)>12 or any(p in {'','.','..'} or p.endswith(('.',' ')) for p in parts):raise RuntimeInputsError()
    reserved={'CON','PRN','AUX','NUL',*(f'COM{i}' for i in range(1,10)),*(f'LPT{i}' for i in range(1,10))}
    if any(p.split('.')[0].upper() in reserved for p in parts):raise RuntimeInputsError()
    if not _NAME.fullmatch(parts[0]):raise RuntimeInputsError()
    return path


@dataclass(frozen=True)
class RuntimeInputs:
    project_id: str
    organization_id: str
    reference: Mapping
    files: Mapping[str,bytes]

    def __post_init__(self):
        try:
            ref=dict(self.reference);files=dict(self.files)
        except (ValueError,TypeError):
            raise RuntimeInputsError() from None
        if (type(self.project_id) is not str or type(self.organization_id) is not str
                or not _NAME.fullmatch(self.project_id) or not _NAME.fullmatch(self.organization_id)
                or set(ref)!={'schema_version','project_id','organization_id','generation','snapshot_sha256','source_revision'}
                or ref.get('project_id')!=self.project_id or ref.get('organization_id')!=self.organization_id
                or type(ref.get('schema_version')) is not int or ref['schema_version']!=1
                or any(type(ref.get(k)) is not str or not _SHA.fullmatch(ref[k]) for k in ['generation','snapshot_sha256','source_revision'])
                or any(type(k) is not str for k in files) or len({k.casefold() for k in files})!=len(files)
                or not 2<=len(files)<=128 or any(type(v) is not bytes or len(v)>512*1024 for v in files.values())
                or sum(map(len,files.values()))>1024*1024):
            raise RuntimeInputsError()
        if 'org/config.json' not in files or 'staffing/defaults.json' not in files:raise RuntimeInputsError()
        for path in files:
            if path in {'org/config.json','staffing/defaults.json','workflow/manifest.json','workflow/contract.json','talent/templates.json'}:continue
            if not path.startswith('skills/'):raise RuntimeInputsError()
            _skill_path(path[7:])
        names={k.casefold() for k in files}
        if any('/'.join(path.split('/')[:i]).casefold() in names for path in files for i in range(1,len(path.split('/')))):
            raise RuntimeInputsError()
        org=_json(files['org/config.json']);staffing=_json(files['staffing/defaults.json'])
        if org.get('organization_id')!=self.organization_id or type(staffing.get('scopes')) is not dict:raise RuntimeInputsError()
        object.__setattr__(self,'reference',MappingProxyType(ref))
        object.__setattr__(self,'files',MappingProxyType(files))

    @property
    def generation(self):return self.reference['generation']

    def config_for(self, base_config):
        from ampuly.core.org_config import apply_org_config_payload_to_config, validate_runnable_org_config
        # source_path=None prohibits a fallback employee-registry read. The
        # snapshot's explicit employees are the data this runtime received.
        config=apply_org_config_payload_to_config(base_config,_json(self.files['org/config.json']))
        validate_runnable_org_config(config,organization_id=self.organization_id)
        class FrozenRuntimeConfig(type(config)):
            def save(self,*args,**kwargs):
                raise RuntimeInputsError('runtime_inputs_read_only')
        frozen=FrozenRuntimeConfig.model_validate(config.model_dump())
        # Preserve the host's existing workspace trust binding. The snapshot
        # is data and must not make a previously bound config unbound.
        frozen._trusted_workspace=getattr(base_config,'_trusted_workspace',None)
        frozen._trusted_config_dir=getattr(base_config,'_trusted_config_dir',None)
        return frozen

    def wrap_file_reader(self, fallback):
        async def read(path,offset=0,limit=None,include_line_numbers=False,task=None):
            if isinstance(path,str) and path.startswith('ampuly-skill:'):
                return await self.file_read(path=path,offset=offset,limit=limit,
                    include_line_numbers=include_line_numbers,task=task)
            return await fallback(path=path,offset=offset,limit=limit,include_line_numbers=include_line_numbers,task=task)
        return read

    def validate_contract(self, contract):
        manifest=_json(self.files['workflow/manifest.json'])
        if (type(contract) is not dict or contract.get('project_id')!=self.project_id
                or contract.get('workflow_id')!=manifest.get('id')
                or contract.get('workflow_version')!=manifest.get('version')
                or contract.get('runtime_projection')!=dict(self.reference)
                or {k:v for k,v in contract.items() if k!='runtime_projection'}!=_json(self.files['workflow/contract.json'])):
            raise RuntimeInputsError('runtime_inputs_scope_mismatch')
        # Detach the stored contract, never retain a caller's mutable mapping.
        return json.loads(json.dumps(contract,allow_nan=False))

    def staffing_for(self, profile, org_id):
        if org_id!=self.organization_id:raise RuntimeInputsError('runtime_inputs_scope_mismatch')
        scopes=_json(self.files['staffing/defaults.json'])['scopes']
        row=scopes.get(f'profile:{profile}|org:{org_id}',{})
        if type(row) is not dict:raise RuntimeInputsError()
        return row

    def resource_uri(self, relative):
        return f'ampuly-skill://{self.generation}/'+_skill_path(relative)

    def resource(self, uri):
        prefix=f'ampuly-skill://{self.generation}/'
        if type(uri) is not str or not uri.startswith(prefix):raise RuntimeInputsError('runtime_inputs_scope_mismatch')
        path='skills/'+_skill_path(uri[len(prefix):])
        if path not in self.files:raise RuntimeInputsError('runtime_resource_missing')
        return self.files[path]

    async def file_read(self, *, path, offset=0, limit=None, include_line_numbers=False, task=None):
        from ampuly.layer4_tools.file_ops import _render_file_slice
        try:
            if task is None or task.project_id!=self.project_id:raise RuntimeInputsError('runtime_inputs_scope_mismatch')
            text=self.resource(path).decode('utf-8')
            lines=text.splitlines(keepends=True)
            rendered=_render_file_slice(lines,offset=offset,limit=limit,include_line_numbers=include_line_numbers)
            return {**rendered,'total_lines':len(text.splitlines()),'returned_start_line':max(0,int(offset or 0))+1 if lines else 0,
                    'path':path,'success':True}
        except (RuntimeInputsError,UnicodeError,ValueError,TypeError):
            return {'success':False,'error':'runtime_resource_unavailable'}


class RuntimeSkillLibrary:
    """Factory for the existing SkillLibrary with an immutable read source."""
    @staticmethod
    def create(ampuly_home, inputs):
        from ampuly.layer5_memory.skill_library import SkillLibrary, parse_skill_text
        class FrozenLibrary(SkillLibrary):
            def load_all(self, project_id=None):
                if project_id not in {None,inputs.project_id}:raise RuntimeInputsError('runtime_inputs_scope_mismatch')
                skills={}
                for path,raw in inputs.files.items():
                    parts=path.split('/')
                    if len(parts)!=3 or parts[0]!='skills' or parts[2]!='SKILL.md':continue
                    try:
                        skill=parse_skill_text(raw.decode('utf-8').replace('\r\n','\n'),
                            source_path=inputs.resource_uri('/'.join(parts[1:])),default_name=parts[1])
                        if type(skill.name) is not str or skill.name in skills:raise ValueError
                    except (ValueError,TypeError,UnicodeError,AttributeError):raise RuntimeInputsError() from None
                    skills[skill.name]=skill
                self._loaded_project_id=inputs.project_id;self._skills=skills
            def _frozen_skill(self,skill,sha256):
                raw=inputs.resource(skill.source_path);canonical=raw.replace(b'\r\n',b'\n') if b'\x00' not in raw else raw
                if hashlib.sha256(canonical).hexdigest()!=sha256:raise RuntimeInputsError('runtime_skill_pin_mismatch')
                return skill
            def save_skill(self,*a,**kw):raise RuntimeInputsError('runtime_inputs_read_only')
            def delete_skill(self,*a,**kw):raise RuntimeInputsError('runtime_inputs_read_only')
            def list_project_skills(self,project_id):
                if project_id!=inputs.project_id:raise RuntimeInputsError('runtime_inputs_scope_mismatch')
                return list(self._skills.values())
            def list_all_project_ids_with_skills(self):return [inputs.project_id]
        library=FrozenLibrary(ampuly_home);library.load_all(inputs.project_id)
        return library
