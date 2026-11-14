"""Exact-version project memory as an explicitly adopted employee data source.

Only the already-created engine MarkdownMemoryStore is used. Its constructor
is never called here: that constructor owns legacy migration/deletion. No
global memory, customer bucket, filename supplied by a client or remote source
is inferred from an opaque employee reference.
"""
from __future__ import annotations

import hashlib
import inspect
import marshal
from pathlib import Path
import re
from types import FunctionType

from ampuly.layer4_tools.workspace_fs import SecureWorkspace
from ampuly.layer5_memory.markdown_memory import MarkdownMemoryStore
from ampuly.layer5_memory.project_knowledge_versions import ProjectKnowledgeVersions
from ampuly.llm.budget import BudgetStopSurfaced
from .ai_employee_contract import canonical_json
from .ai_employee_run_envelope import EmployeeRunIdentity

MAX_KNOWLEDGE_BYTES = 256 * 1024
_VERSION = re.compile(r'sha256:[a-f0-9]{64}\Z')


def _fail(code='employee_knowledge_unavailable'):
    raise BudgetStopSurfaced(code, '本岗位的固定知识版本或当前读取授权无法核实，请负责人核对。') from None


class _BoundedProjectView(MarkdownMemoryStore):
    """Original visible-text semantics over an original pinned file reader."""
    def __init__(self, source, project_id):
        # Deliberately do not call MarkdownMemoryStore.__init__.
        self.source, self.project_id = source, project_id

    def load_raw_text(self, project_id=None, customer=''):
        if project_id != self.project_id or customer:
            _fail('employee_knowledge_scope_invalid')
        path = MarkdownMemoryStore.memory_path(self.source, project_id)
        expected = self.source.global_memory_dir / 'projects' / (project_id + '.md')
        if path != expected:
            _fail('employee_knowledge_scope_invalid')
        root = self.source.global_memory_dir
        with SecureWorkspace.for_roots(str(root), str(root)) as workspace:
            if type(workspace).__name__ == 'LexicalWorkspace':
                _fail('employee_knowledge_native_boundary_required')
            text = workspace.read_text(workspace.resolve('projects/' + project_id + '.md'),
                                       max_bytes=MAX_KNOWLEDGE_BYTES)
        # Native bounded readers use replacement on malformed UTF-8. An
        # uncertain decoding cannot silently become a new certified version.
        if '\ufffd' in text:
            _fail('employee_knowledge_encoding_invalid')
        # Path.read_text in the original store uses universal-newline mode.
        # The bounded native reader decodes bytes without translating CRLF/CR;
        # preserve the original visible-text/version semantics on Windows too.
        return text.replace('\r\n', '\n').replace('\r', '\n')


class EmployeeProjectKnowledge:
    def __init__(self, *, engine, read_current_instance, project_active):
        if not callable(read_current_instance) or not callable(project_active):
            _fail()
        self.engine = engine
        self.read_current_instance, self.project_active = read_current_instance, project_active
        self.project_id = getattr(engine, 'project_id', None)

    def profile(self):
        """Code-only interpreter identity; no knowledge bytes or credentials."""
        projection = {'protocol': 'employee-project-memory-v2', 'max_bytes': MAX_KNOWLEDGE_BYTES}
        from ampuly.layer4_tools import workspace_fs
        from ampuly.layer4_tools.employee_builtin_read import _source_inventory
        from .employee_tool_fingerprint import local_implementation_fingerprint
        if SecureWorkspace is not workspace_fs.SecureWorkspace:
            _fail('employee_knowledge_source_unavailable')
        files, versions = _source_inventory()
        projection['native_reader'] = local_implementation_fingerprint(files, versions).fingerprint
        if projection['native_reader'] is None:
            _fail('employee_knowledge_source_unavailable')
        for cls in (MarkdownMemoryStore, ProjectKnowledgeVersions, _BoundedProjectView, EmployeeProjectKnowledge):
            path = Path(inspect.getsourcefile(cls) or '')
            if not path.is_absolute() or path.stat().st_size > 1024 * 1024:
                _fail()
            raw = path.read_bytes()
            if not raw or len(raw) > 1024 * 1024:
                _fail()
            methods = {}
            for name, value in vars(cls).items():
                if isinstance(value, (classmethod, staticmethod)):
                    value = value.__func__
                if type(value) is FunctionType:
                    methods[name] = hashlib.sha256(marshal.dumps(value.__code__)).hexdigest()
            projection[cls.__name__] = {'source_sha256': hashlib.sha256(raw).hexdigest(), 'loaded_methods': methods}
        return hashlib.sha256(canonical_json(projection)).hexdigest()

    def _row(self, refs):
        if type(refs) is not list or len(refs) != 1:
            _fail('employee_knowledge_reference_unsupported')
        row = refs[0]
        if (type(row) is not dict or set(row) != {'kind', 'ref', 'version', 'access_scope'}
                or row.get('kind') != 'enterprise_shared' or row.get('ref') != 'project-memory'
                or row.get('access_scope') != self.project_id
                or type(row.get('version')) is not str or _VERSION.fullmatch(row['version']) is None):
            _fail('employee_knowledge_reference_unsupported')
        return row

    def _current(self, instance_id, row):
        project = self.project_id
        if (type(instance_id) is not str or re.fullmatch(r'ei-[a-f0-9]{16}', instance_id) is None
                or type(project) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', project) is None
                or getattr(self.engine, 'project_id', None) != project
                or self.project_active(project) is not True):
            _fail('employee_knowledge_scope_invalid')
        current = self.read_current_instance(project, instance_id)
        if (type(current) is not dict or current.get('project_id') != project
                or current.get('instance_id') != instance_id or current.get('execution_state') != 'active'
                or type(current.get('knowledge_refs')) is not list or row not in current['knowledge_refs']):
            _fail('employee_knowledge_access_revoked')
        return current

    def _source(self):
        if (type(self.project_id) is not str
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', self.project_id) is None
                or getattr(self.engine, 'project_id', None) != self.project_id
                or self.project_active(self.project_id) is not True):
            _fail('employee_knowledge_scope_invalid')
        source = getattr(getattr(self.engine, 'memory', None), 'markdown_store', None)
        home = Path(self.engine.ampuly_home).absolute()
        if (type(source) is not MarkdownMemoryStore or Path(source.ampuly_home).absolute() != home
                or source.global_memory_dir != Path(source.ampuly_home) / 'memory'
                or source.project_memory_dir != source.global_memory_dir / 'projects'):
            _fail('employee_knowledge_source_unavailable')
        return source

    def _source_content(self):
        source = self._source()
        # This is the existing reader's exact visible-text function, with its
        # I/O supplied by a bounded native view. It cannot select a new path.
        content = MarkdownMemoryStore.load_visible_text(_BoundedProjectView(source, self.project_id), self.project_id)
        if not content or len(content.encode('utf-8')) > MAX_KNOWLEDGE_BYTES:
            _fail('employee_knowledge_content_invalid')
        if self.project_active(self.project_id) is not True:
            _fail('employee_knowledge_scope_invalid')
        return content

    def descriptor(self):
        """Owner service only: no content, user title, filename or new grant.

        Callers must use the existing authenticated owner/project dispatcher.
        This is a fixed canonical source, not a directory enumeration API.
        """
        try:
            content = self._source_content()
            return {'kind': 'enterprise_shared', 'ref': 'project-memory',
                'version': 'sha256:' + hashlib.sha256(content.encode('utf-8')).hexdigest(),
                'access_scope': self.project_id, 'label': '本项目共享知识',
                'content_bytes': len(content.encode('utf-8')), 'reader_fingerprint': self.profile()}
        except BudgetStopSurfaced:
            raise
        except Exception:
            _fail()

    def _read(self, instance_id, refs):
        row = self._row(refs)
        self._current(instance_id, row)
        source = self._source()
        try:
            content = ProjectKnowledgeVersions(source.ampuly_home).read(self.project_id, row['version'])
        except FileNotFoundError:
            # Legacy references can still read the identical current content.
            # Never fabricate old v1 bytes from a different current v2, and
            # never create version files from a read/descriptor call.
            content = self._source_content()
        version = 'sha256:' + hashlib.sha256(content.encode('utf-8')).hexdigest()
        if version != row['version']:
            _fail('employee_knowledge_version_changed')
        self._current(instance_id, row)
        return {row['ref']: {'version': row['version'], 'content': content}}

    def accessible(self, instance):
        if type(instance) is not dict:
            return False
        refs = instance.get('knowledge_refs')
        if refs in (None, []):
            return True
        try:
            return bool(self._read(instance['instance_id'], refs))
        except Exception:
            return False

    def resolve(self, identity, refs, *, instance_id):
        if (type(identity) is not EmployeeRunIdentity or identity.project_id != self.project_id
                or not identity.order_id or not identity.step_task_id):
            _fail('employee_knowledge_scope_invalid')
        # An empty frozen declaration grants no source read. Keep the original
        # knowledge-free execution path inert rather than requiring a file.
        if type(refs) is list and not refs:
            return {}
        try:
            return self._read(instance_id, refs)
        except BudgetStopSurfaced:
            raise
        except Exception:
            _fail()
