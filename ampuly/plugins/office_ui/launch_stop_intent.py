"""Protected original-source Stop candidates on the host's existing transaction.

No connect/BEGIN/commit/rollback/schema installation, external cleanup or grant.
The host must own BEGIN IMMEDIATE and confirm commit/close before publishing.
Dependency: Root must integrate the source reader's frozen _budget keyword seam.
Original chain-helper cursor cleanup retains its separately documented limits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import sqlite3

from .business_store import BusinessStore
from .accounts import AccountStore
from .current_account_principal import _login, _PrincipalFault
from .current_owner_rw import CurrentOwnerObservationError, _observe_current_owner_on_rw_connection, _token as _owner_token
from .employee_order_launch_anchor import (
    OrderLaunchAnchorError, _ReadBudget, _identity, _one_record, _json_dict,
    _EVENT_COLUMNS, _MAX_JSON_BYTES, _MAX_TOTAL_BYTES, _MAX_JSON_NODES, _SHA256,
)
from .employee_order_claim_pins import _read_claim_events
from .order_launch_claim_source import _legacy_payload, _canonical
from .launch_stop_source import LaunchStopSourceHeaders, _expected_headers, _seal_schema, read_launch_stop_source_headers


_TABLE = 'order_launch_stop_intents_v1'
_COLUMNS = (
    'stop_fact_id','project_id','order_id','outbox_id','workflow_id','workflow_version',
    'launch_payload_sha256','claim_event_id','claim_event_chain_head_sha256','stop_event_id',
    'requested_at','actor_login','reason','from_order_status','from_outbox_status','to_order_status',
    'record_bytes','record_sha256',
)
_SOURCE_NAMES = ('project_id','order_id','outbox_id','workflow_id','workflow_version',
                 'launch_payload_sha256','claim_event_id','event_chain_head_sha256')
_MISSING = object()
_MAXIMUMS = (69,512,512,512,512,512,64,512,64,512,128,64,4096,32,32,32,65536,64)
_FIXED_ERRORS = {
    'invalid_source':'取消请求记录或来源无法核验。',
    'unavailable':'取消请求暂时无法处理。',
    'forbidden':'当前会话没有有效的所有者身份。',
    'conflict':'取消请求的来源或状态已变化。',
    'outcome_unknown':'取消请求的持久化结果尚无法确认。',
}
_TABLE_SQL = """CREATE TABLE order_launch_stop_intents_v1 (
    stop_fact_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    outbox_id TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    workflow_version TEXT NOT NULL,
    launch_payload_sha256 TEXT NOT NULL,
    claim_event_id TEXT NOT NULL,
    claim_event_chain_head_sha256 TEXT NOT NULL,
    stop_event_id TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    actor_login TEXT NOT NULL,
    reason TEXT NOT NULL,
    from_order_status TEXT NOT NULL,
    from_outbox_status TEXT NOT NULL,
    to_order_status TEXT NOT NULL,
    record_bytes BLOB NOT NULL,
    record_sha256 TEXT NOT NULL,
    UNIQUE(project_id,order_id,outbox_id)
)"""
_TRIGGERS = {
    'trg_launch_stop_no_update_v1':"""CREATE TRIGGER trg_launch_stop_no_update_v1
BEFORE UPDATE ON order_launch_stop_intents_v1
BEGIN SELECT RAISE(ABORT,'launch_stop_immutable'); END""",
    'trg_launch_stop_no_delete_v1':"""CREATE TRIGGER trg_launch_stop_no_delete_v1
BEFORE DELETE ON order_launch_stop_intents_v1
BEGIN SELECT RAISE(ABORT,'launch_stop_immutable'); END""",
    'trg_launch_stop_no_replace_v1':"""CREATE TRIGGER trg_launch_stop_no_replace_v1
BEFORE INSERT ON order_launch_stop_intents_v1
WHEN EXISTS(SELECT 1 FROM order_launch_stop_intents_v1
 WHERE stop_fact_id=NEW.stop_fact_id OR
 (project_id=NEW.project_id AND order_id=NEW.order_id AND outbox_id=NEW.outbox_id))
BEGIN SELECT RAISE(ABORT,'launch_stop_immutable'); END""",
}
# Reference only. No runtime function executes these declarations.
LAUNCH_STOP_SCHEMA_SQL = _TABLE_SQL+';\n'+';\n'.join(_TRIGGERS.values())+';\n'
_INDEXES = {
    'sqlite_autoindex_order_launch_stop_intents_v1_1':('pk',((0,'stop_fact_id'),)),
    'sqlite_autoindex_order_launch_stop_intents_v1_2':('u',((1,'project_id'),(2,'order_id'),(3,'outbox_id'))),
}
_META_SQL = 'SELECT '+','.join('typeof('+name+'),length(CAST('+name+' AS BLOB))' for name in _COLUMNS)+(
    ' FROM main.order_launch_stop_intents_v1 WHERE stop_fact_id=? OR (project_id=? AND order_id=? AND outbox_id=?) LIMIT 2')
_VALUE_SQL = 'SELECT '+','.join(
    'CASE WHEN typeof('+name+")='"+('blob' if name=='record_bytes' else 'text')+(
        "' AND length(CAST("+name+' AS BLOB))<='+str(maximum)+' THEN '+name+' ELSE NULL END')
    for name,maximum in zip(_COLUMNS,_MAXIMUMS))+(
    ' FROM main.order_launch_stop_intents_v1 WHERE stop_fact_id=? OR (project_id=? AND order_id=? AND outbox_id=?) LIMIT 2')
_OUTBOX_TEXT = (('outbox_id',512),('aggregate_type',512),('aggregate_id',512),('event_type',512),
                ('status',32),('payload_json',_MAX_JSON_BYTES),('last_error',8000),('created_at',128),('published_at',128))
_ORDER_TEXT = (('order_id',512),('workflow_id',512),('status',32),('launched_task_id',512),('fields_json',_MAX_JSON_BYTES))


class LaunchStopIntentError(ValueError):
    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in _FIXED_ERRORS:
            raise ValueError('取消请求错误代码不符合要求。')
        self.code = code
        super().__init__(_FIXED_ERRORS[code])


class _StopFault(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True, repr=False)
class LaunchStopIntentFact:
    stop_fact_id: str
    project_id: str
    order_id: str
    outbox_id: str
    workflow_id: str
    workflow_version: str
    launch_payload_sha256: str
    claim_event_id: str
    claim_event_chain_head_sha256: str
    stop_event_id: str
    requested_at: str
    actor_login: str
    reason: str
    from_order_status: str
    from_outbox_status: str
    to_order_status: str
    record_bytes: bytes
    record_sha256: str


def _fail(code: str) -> None:
    raise _StopFault(code)


def _error_code(exc: Exception) -> str:
    if type(exc) in (_StopFault,LaunchStopIntentError,CurrentOwnerObservationError):
        return exc.code if type(exc.code) is str and exc.code in _FIXED_ERRORS else 'unavailable'
    if type(exc) is OrderLaunchAnchorError:
        return 'unavailable' if type(exc.code) is not str or exc.code in ('unavailable','read_limit') else 'invalid_source'
    if type(exc) is sqlite3.IntegrityError:
        return 'conflict'
    if type(exc) is _PrincipalFault:
        return exc.code if type(exc.code) is str and exc.code in ('unavailable','invalid_source') else 'unavailable'
    return 'unavailable'


def _rows(conn,sql,params=(),maximum=2):
    """Exact native cursor, finite rows, primary-preserving close."""
    cursor = None
    primary = None
    data = None
    try:
        cursor = sqlite3.Connection.execute(conn,sql,params)
        data = sqlite3.Cursor.fetchmany(cursor,maximum+1)
    except BaseException as exc:
        primary = exc
    finally:
        if cursor is not None:
            try:
                sqlite3.Cursor.close(cursor)
            except BaseException as exc:
                if primary is None:
                    primary = exc
    if primary is not None:
        raise primary
    if type(data) is not list or len(data)>maximum:
        _fail('invalid_source')
    result = []
    for row in data:
        if type(row) is sqlite3.Row:
            row = tuple(row)
        elif type(row) is not tuple:
            _fail('invalid_source')
        result.append(row)
    return result


def _execute(conn,sql,params):
    cursor = None
    primary = None
    changed = None
    try:
        cursor = sqlite3.Connection.execute(conn,sql,params)
        changed = cursor.rowcount
    except BaseException as exc:
        primary = exc
    finally:
        if cursor is not None:
            try:
                sqlite3.Cursor.close(cursor)
            except BaseException as exc:
                if primary is None:
                    primary = exc
    if primary is not None:
        raise primary
    if type(changed) is not int or changed!=1:
        _fail('conflict')


def _transaction(conn, *, rw=False):
    if (type(conn) is not sqlite3.Connection or conn.in_transaction is not True
            or conn.row_factory is not None and conn.row_factory is not sqlite3.Row or conn.text_factory is not str):
        _fail('invalid_source')
    mode = _rows(conn,'PRAGMA query_only',maximum=1)
    if (len(mode)!=1 or len(mode[0])!=1 or type(mode[0][0]) is not int
            or mode[0][0] not in ((0,) if rw else (0,1))):
        _fail('invalid_source')
    databases = _rows(conn,'SELECT seq,name FROM pragma_database_list LIMIT 3',maximum=3)
    if not 1<=len(databases)<=2:
        _fail('invalid_source')
    names = set()
    for row in databases:
        if (len(row)!=2 or type(row[0]) is not int or type(row[1]) is not str
                or (row[0],row[1]) not in ((0,'main'),(1,'temp')) or row[1] in names):
            _fail('invalid_source')
        names.add(row[1])
    if 'main' not in names or _rows(conn,'SELECT 1 FROM temp.sqlite_schema LIMIT 1',maximum=1):
        _fail('invalid_source')


def _budget(value):
    if type(value) is not _ReadBudget or type(value.__dict__) is not dict:
        _fail('invalid_source')
    fields = value.__dict__
    if len(fields)!=2 or any(type(key) is not str for key in fields) or set(fields)!={'bytes','nodes'}:
        _fail('invalid_source')
    if (type(value.bytes) is not int or not 0<=value.bytes<=_MAX_TOTAL_BYTES
            or type(value.nodes) is not int or not 0<=value.nodes<=_MAX_JSON_NODES):
        _fail('invalid_source')
    return value


def _schema(conn):
    _transaction(conn)
    table = _rows(conn,"""SELECT type,name,tbl_name,
        CASE WHEN typeof(sql)='text' AND length(CAST(sql AS BLOB))<=16384 THEN sql ELSE NULL END
        FROM main.sqlite_schema WHERE name=? LIMIT 2""",(_TABLE,))
    if not table:
        _fail('unavailable')
    if table!=[('table',_TABLE,_TABLE,_TABLE_SQL)]:
        _fail('invalid_source')
    expected = [(index,name,'BLOB' if name=='record_bytes' else 'TEXT',1,None,1 if index==0 else 0,0)
                for index,name in enumerate(_COLUMNS)]
    columns = _rows(conn,"PRAGMA main.table_xinfo('order_launch_stop_intents_v1')",maximum=19)
    if columns!=expected or any(type(row[i]) is not int for row in columns for i in (0,3,5,6)):
        _fail('invalid_source')
    sql_indexes = _rows(conn,"""SELECT
        CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB))<=128 THEN name ELSE NULL END,
        type,tbl_name,CASE WHEN sql IS NULL THEN 1 ELSE 0 END FROM main.sqlite_schema
        WHERE type='index' AND tbl_name=? LIMIT 3""",(_TABLE,),3)
    if (len(sql_indexes)!=2 or {row[0] for row in sql_indexes if len(row)==4 and type(row[0]) is str}!=set(_INDEXES)
            or any(row[1:]!=('index',_TABLE,1) or type(row[3]) is not int for row in sql_indexes)):
        _fail('invalid_source')
    indexes = _rows(conn,"PRAGMA main.index_list('order_launch_stop_intents_v1')",maximum=3)
    seen = set()
    for row in indexes:
        if (len(row)!=5 or type(row[0]) is not int or not 0<=row[0]<2 or type(row[1]) is not str
                or row[1] not in _INDEXES or row[1] in seen or type(row[2]) is not int or row[2]!=1
                or row[3]!=_INDEXES[row[1]][0] or type(row[4]) is not int or row[4]!=0):
            _fail('invalid_source')
        seen.add(row[1])
        key = _INDEXES[row[1]][1]
        info = _rows(conn,'PRAGMA main.index_xinfo("'+row[1]+'")',maximum=5)
        wanted = [(number,cid,name,0,'BINARY',1) for number,(cid,name) in enumerate(key)]+[(len(key),-1,None,0,'BINARY',0)]
        if info!=wanted or any(type(item[i]) is not int for item in info for i in (0,1,3,5)):
            _fail('invalid_source')
    if seen!=set(_INDEXES):
        _fail('invalid_source')
    triggers = _rows(conn,"""SELECT
        CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB))<=128 THEN name ELSE NULL END,
        type,tbl_name,
        CASE WHEN typeof(sql)='text' AND length(CAST(sql AS BLOB))<=16384 THEN sql ELSE NULL END
        FROM main.sqlite_schema WHERE type='trigger' AND (tbl_name=? OR name IN (?,?,?)) LIMIT 4""",
        (_TABLE,*_TRIGGERS),4)
    if (len(triggers)!=3 or {row[0] for row in triggers if len(row)==4 and type(row[0]) is str}!=set(_TRIGGERS)
            or any(row[1:3]!=('trigger',_TABLE) or row[3]!=_TRIGGERS[row[0]] for row in triggers)):
        _fail('invalid_source')
    _transaction(conn)


def validate_launch_stop_schema(conn: sqlite3.Connection) -> None:
    failure = None
    try:
        _schema(conn)
    except Exception as exc:
        failure = _error_code(exc)
    if failure is not None:
        raise LaunchStopIntentError(failure) from None


def _headers(source):
    values = _expected_headers(source)
    if values is None:
        _fail('invalid_source')
    return values


def _digest(value):
    if type(value) is not str or _SHA256.fullmatch(value) is None:
        _fail('invalid_source')
    return value


def _reason(value):
    if (type(value) is not str or len(value)>4096 or len(value.strip())<2
            or any(ord(char)<32 and char not in '\r\n\t' or 127<=ord(char)<=159 for char in value)):
        _fail('invalid_source')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        size = 4097
    if size>4096:
        _fail('invalid_source')
    return value


def _at(value):
    value = _identity(value)
    if len(value)>128:
        _fail('invalid_source')
    failed = False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        failed = True
        parsed = None
    if failed:
        _fail('invalid_source')
    if parsed.tzinfo is not None or parsed.isoformat()!=value:
        _fail('invalid_source')
    return value


def _fact_id(values):
    return 'stop:'+hashlib.sha256(_canonical({'domain':'order_launch_stop.v1','source':dict(zip(_SOURCE_NAMES,values))})).hexdigest()


def _note(values,fact_id,delivery):
    return dict(schema_version=1,stop_fact_id=fact_id,stop_phase='requested',
                from_order_status='launching',from_outbox_status=delivery,to_order_status='rejected',
                runtime_stopped='unknown',effects='not_rolled_back',source=dict(zip(_SOURCE_NAMES,values)))


def _record(values):
    result = _canonical({**dict(zip(_COLUMNS[:16],values)), 'schema_version':1,'phase':'stop_requested'})
    if len(result)>65536:
        _fail('invalid_source')
    return result


def _lookup(conn,values,budget):
    params = (_fact_id(values),*values[:3])
    metadata = _rows(conn,_META_SQL,params)
    if not metadata:
        return None
    if len(metadata)!=1 or len(metadata[0])!=36:
        _fail('conflict')
    sizes = []
    for index,maximum in enumerate(_MAXIMUMS):
        kind,size = metadata[0][index*2:index*2+2]
        if (kind!=('blob' if index==16 else 'text') or type(size) is not int or not 0<=size<=maximum):
            _fail('invalid_source')
        sizes.append(size)
    budget.charge_bytes(sum(sizes))
    rows = _rows(conn,_VALUE_SQL,params)
    if len(rows)!=1 or len(rows[0])!=18:
        _fail('invalid_source')
    row = rows[0]
    for index,value in enumerate(row):
        if index==16:
            if type(value) is not bytes or len(value)!=sizes[index]:
                _fail('invalid_source')
        elif type(value) is not str or len(value.encode('utf-8'))!=sizes[index]:
            _fail('invalid_source')
    if tuple(row[1:9])!=values or row[0]!=_fact_id(values):
        _fail('conflict')
    _identity(row[9]); _at(row[10]); _login(row[11]); _reason(row[12]); _digest(row[17])
    if row[13]!='launching' or row[14] not in ('pending','inflight','outcome_unknown') or row[15]!='rejected':
        _fail('invalid_source')
    if hashlib.sha256(row[16]).hexdigest()!=row[17] or _record(row[:16])!=row[16]:
        _fail('invalid_source')
    parsed = _json_dict(row[16].decode('utf-8'),budget)
    if _canonical(parsed)!=row[16]:
        _fail('invalid_source')
    return LaunchStopIntentFact(*row)


def _position(conn,order_id,*,event_id=None,digest=None):
    if event_id is not None:
        rows = _rows(conn,'SELECT id FROM main.order_events WHERE order_id=? AND event_id=? LIMIT 2',(order_id,event_id))
    else:
        rows = _rows(conn,'SELECT id FROM main.order_events WHERE order_id=? AND event_hash=? LIMIT 2',(order_id,digest))
    if (len(rows)!=1 or len(rows[0])!=1 or type(rows[0][0]) is not int or not 0<rows[0][0]<=2**63-1):
        _fail('invalid_source')
    return rows[0][0]


def _outbox(conn,values,budget):
    row = _one_record(conn,table='main.business_outbox',selector='outbox_id = ?',params=(values[2],),
                      columns=_OUTBOX_TEXT,budget=budget)
    if row[:4]!=(values[2],'work_order',values[1],'workflow_launch_requested'):
        _fail('invalid_source')
    if hashlib.sha256(row[5].encode('utf-8')).hexdigest()!=values[5]:
        _fail('conflict')
    count = _rows(conn,"""SELECT CASE WHEN typeof(attempt_count)='integer' THEN attempt_count ELSE NULL END
        FROM main.business_outbox WHERE outbox_id=? LIMIT 2""",(values[2],))
    if (len(count)!=1 or len(count[0])!=1 or type(count[0][0]) is not int or not 0<=count[0][0]<=2**63-1):
        _fail('invalid_source')
    payload = _json_dict(row[5],budget)
    version,fields,_ = _legacy_payload(payload,project_id=values[0],order_id=values[1],workflow_id=values[3])
    if version!=values[4] or type(fields.get('project_id')) is not str or fields['project_id']!=values[0]:
        _fail('invalid_source')
    return row,count[0][0]


def _history(conn,values,fact,budget):
    _seal_schema(conn)
    outbox,count = _outbox(conn,values,budget)
    if fact.from_outbox_status=='pending':
        if (outbox[4]!='cancelled' or count!=0 or outbox[6]!='launch_stop_requested' or outbox[8]!=fact.requested_at):
            _fail('conflict')
    elif outbox[4] not in ('inflight','outcome_unknown') or outbox[8]:
        _fail('conflict')
    claim,pins,head = _read_claim_events(conn,order_id=values[1],outbox_id=values[2],payload_sha=values[5],
        budget=budget,pins_source=None,project_id=values[0],workflow_id=values[3],workflow_version=values[4])
    if claim!=values[6] or pins!='':
        _fail('invalid_source')
    _digest(head)
    claim_id = _position(conn,values[1],event_id=values[6])
    prefix_id = _position(conn,values[1],digest=values[7])
    stop_id = _position(conn,values[1],event_id=fact.stop_event_id)
    if not claim_id<=prefix_id<stop_id:
        _fail('invalid_source')
    event = _one_record(conn,table='main.order_events',selector='order_id = ? AND event_id = ?',
                        params=(values[1],fact.stop_event_id),columns=_EVENT_COLUMNS,budget=budget)
    if (event[0:4]!=(values[1],fact.requested_at,'rejected',fact.actor_login)
            or event[5:8]!=(fact.stop_event_id,values[1],values[2])):
        _fail('invalid_source')
    if _canonical(_json_dict(event[4],budget))!=_canonical(_note(values,fact.stop_fact_id,fact.from_outbox_status)):
        _fail('invalid_source')


def _read(conn,source,budget):
    values = _headers(source)
    _budget(budget); _schema(conn); _seal_schema(conn)
    result = _lookup(conn,values,budget)
    if result is not None:
        _history(conn,values,result,budget)
    _schema(conn)
    return result


def read_launch_stop_intent_on_connection(
    conn: sqlite3.Connection, *, source: LaunchStopSourceHeaders, _budget: _ReadBudget,
) -> LaunchStopIntentFact | None:
    """Historical exact-source receipt; never stop a new generation or authorize.

    Missing means absent only in this owned snapshot; no prior-outcome inference.
    No current-order launching requirement and no mutation/cleanup/retry lives here.
    """
    failure = None
    result = None
    try:
        result = _read(conn,source,_budget)
    except Exception as exc:
        failure = _error_code(exc)
    if failure is not None:
        raise LaunchStopIntentError(failure) from None
    return result


def _request(conn,business,accounts,token,source,reason,at,budget):
    values = _headers(source); _reason(reason); _at(at); _budget(budget)
    _transaction(conn,rw=True)
    # Capture the original session across the entire write, not just each observation.
    if type(accounts) is not AccountStore or type(business) is not BusinessStore:
        _fail('invalid_source')
    token = _owner_token(token)
    account_fields = accounts.__dict__
    business_fields = business.__dict__
    if type(account_fields) is not dict or type(business_fields) is not dict or account_fields.get('business') is not business:
        _fail('invalid_source')
    home,path,tokens = account_fields.get('home'),business_fields.get('path'),account_fields.get('_tokens')
    if type(tokens) is not dict:
        _fail('invalid_source')
    entry = tokens.get(token,_MISSING)

    def session_unchanged():
        if (type(accounts) is not AccountStore or type(business) is not BusinessStore
                or accounts.__dict__ is not account_fields or business.__dict__ is not business_fields
                or accounts.business is not business or accounts.home is not home or business.path is not path
                or accounts._tokens is not tokens or tokens.get(token,_MISSING) is not entry):
            _fail('unavailable')

    owner = _observe_current_owner_on_rw_connection(conn,account_store=accounts,business_store=business,token=token)
    session_unchanged()

    def owner_unchanged():
        if type(accounts) is not AccountStore or type(business) is not BusinessStore:
            _fail('unavailable')
        session_unchanged()
        final = _observe_current_owner_on_rw_connection(conn,account_store=accounts,business_store=business,token=token)
        session_unchanged()
        if final.login!=owner.login:
            _fail('unavailable')
        _transaction(conn,rw=True)

    retained = _read(conn,source,budget)
    if retained is not None:
        owner_unchanged()
        return retained
    # Requires Root's source-budget patch; there is deliberately no old-API fallback.
    if _rows(conn,"SELECT 1 FROM main.sqlite_schema WHERE type='trigger' AND tbl_name IN ('work_orders','business_outbox','order_events') LIMIT 1",maximum=1):
        _fail('invalid_source')
    observed = read_launch_stop_source_headers(conn,project_id=values[0],order_id=values[1],outbox_id=values[2],
                                              expected=source,_budget=budget)
    if _headers(observed)!=values:
        _fail('conflict')
    claim_id = _position(conn,values[1],event_id=values[6])
    later = _rows(conn,"""SELECT id FROM main.order_events WHERE order_id=? AND kind='launching'
        AND id>? ORDER BY id ASC LIMIT 1""",(values[1],claim_id),1)
    if later:
        _fail('conflict')
    order = _one_record(conn,table='main.work_orders',selector='order_id = ?',params=(values[1],),
                        columns=_ORDER_TEXT,budget=budget)
    if order[:4]!=(values[1],values[3],'launching',''):
        _fail('conflict')
    outbox,count = _outbox(conn,values,budget)
    payload = _json_dict(outbox[5],budget)
    _,original_fields,_ = _legacy_payload(payload,project_id=values[0],order_id=values[1],workflow_id=values[3])
    if _canonical(_json_dict(order[4],budget))!=_canonical(original_fields):
        _fail('conflict')
    delivery = outbox[4]
    if delivery not in ('pending','inflight','outcome_unknown'):
        _fail('conflict')
    if delivery=='pending' and (count!=0 or outbox[6] or outbox[8]):
        _fail('conflict')
    if delivery!='pending' and outbox[8]:
        _fail('conflict')
    _execute(conn,"""UPDATE main.work_orders SET status='rejected',updated_at=?,review_note=?
        WHERE order_id=? AND workflow_id=? AND status='launching' AND launched_task_id='' AND fields_json=?""",
        (at,reason,values[1],values[3],order[4]))
    if delivery=='pending':
        _execute(conn,"""UPDATE main.business_outbox SET status='cancelled',published_at=?,last_error='launch_stop_requested'
            WHERE outbox_id=? AND aggregate_type='work_order' AND aggregate_id=?
            AND event_type='workflow_launch_requested' AND payload_json=? AND status='pending'
            AND attempt_count=0 AND last_error='' AND published_at=''""",(at,values[2],values[1],outbox[5]))
    fact_id = _fact_id(values)
    event_id = BusinessStore._append_event(conn,table='order_events',aggregate_column='order_id',
        aggregate_id=values[1],at=at,kind='rejected',actor=owner.login,note=_note(values,fact_id,delivery),
        correlation_id=values[1],causation_id=values[2])
    _identity(event_id)
    first = (fact_id,*values[:7],values[7],event_id,at,owner.login,reason,'launching',delivery,'rejected')
    raw = _record(first)
    data = (*first,raw,hashlib.sha256(raw).hexdigest())
    _execute(conn,'INSERT INTO main.order_launch_stop_intents_v1 ('+','.join(_COLUMNS)+') VALUES ('+','.join('?' for _ in _COLUMNS)+')',data)
    result = _read(conn,source,budget)
    if result is None or tuple(getattr(result,name) for name in _COLUMNS)!=data:
        _fail('invalid_source')
    current_order = _one_record(conn,table='main.work_orders',selector='order_id = ?',params=(values[1],),
                                columns=_ORDER_TEXT,budget=budget)
    current_outbox,current_count = _outbox(conn,values,budget)
    if current_order!=(order[0],order[1],'rejected',order[3],order[4]):
        _fail('conflict')
    wanted = (*outbox[:4],'cancelled',outbox[5],'launch_stop_requested',outbox[7],at) if delivery=='pending' else outbox
    if current_outbox!=wanted or current_count!=count:
        _fail('conflict')
    owner_unchanged()
    return result


def request_launch_stop_on_connection(
    conn: sqlite3.Connection, *, business_store: BusinessStore, account_store: AccountStore,
    token: str, source: LaunchStopSourceHeaders, reason: str, requested_at: str, _budget: _ReadBudget,
) -> LaunchStopIntentFact:
    """Return an UNCOMMITTED Stop candidate; the host owns rollback/commit/close.

    Current owner and original session/source are rechecked; no authorization
    callback, latest-state fallback, token write, runtime cleanup or grant.
    Host BEGIN IMMEDIATE cannot be proven merely by conn.in_transaction.
    A failure after SQL means the host MUST rollback; never continue/commit it.
    """
    failure = None
    result = None
    try:
        result = _request(conn,business_store,account_store,token,source,reason,requested_at,_budget)
    except Exception as exc:
        failure = _error_code(exc)
    if failure is not None:
        raise LaunchStopIntentError(failure) from None
    return result
