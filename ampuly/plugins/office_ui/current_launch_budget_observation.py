"""Observe one expected current budget on an existing owned RW transaction.

No authentication, permit, reservation, source-generation binding or dispatch
is performed. The host owns BEGIN IMMEDIATE, current-source/policy/Stop checks,
future reservations, commit/rollback/close and checks after every await. None
balances retain the original missing-ceiling meaning, never unlimited permit.
The additional 256KiB selected-data cap can refuse valid large old budgets;
original LIMIT 10001 and original money/count calculations remain unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .business_store import BusinessStore
from .current_owner_rw import _owned_rw_connection, _source_path, _OwnerFault
from .current_account_principal import _PrincipalFault
from .employee_order_launch_anchor import _ReadBudget, _MAX_TOTAL_BYTES, _MAX_JSON_NODES


_LOCAL_BYTES = 256*1024
_MAX_ROWS = 10000
_MESSAGES = {
    'invalid_source':'当前预算来源或范围无法核验。',
    'unavailable':'当前预算或读取容量暂不可用。',
}
_KINDS = ('model_call','image','retry','cost_cny')
_COUNT_KINDS = ('model_call','image','retry')
_STATES = ('held','unknown','settled','released')
_BUDGET_COLUMNS = (
    ('budget_id','TEXT',0,None,1),('project_id','TEXT',1,"'default'",0),
    ('customer_id','TEXT',1,"''",0),('environment_id','TEXT',1,"''",0),
    ('order_id','TEXT',1,"''",0),('created_at','TEXT',1,None,0),
    ('created_by','TEXT',1,"''",0),('limits_json','TEXT',1,"'{}'",0),
    ('state','TEXT',1,"'open'",0),('health','TEXT',1,"'ok'",0),
    ('state_reason','TEXT',1,"''",0),
)
_RESERVATION_COLUMNS = (
    ('reservation_id','TEXT',0,None,1),('budget_id','TEXT',1,None,0),
    ('kind','TEXT',1,None,0),('amount','REAL',1,None,0),
    ('settled_amount','REAL',1,'0',0),('state','TEXT',1,None,0),
    ('at','TEXT',1,None,0),('actor','TEXT',1,"''",0),
    ('idempotency_key','TEXT',1,"''",0),('request_fingerprint','TEXT',1,"''",0),
    ('note','TEXT',1,"''",0),
)
_INDEXES = {
    'run_budgets': (
        ('sqlite_autoindex_run_budgets_1',1,'pk',0,((0,'budget_id'),)),
        ('idx_run_budgets_order',0,'c',0,((4,'order_id'),)),
    ),
    'budget_reservations': (
        ('sqlite_autoindex_budget_reservations_1',1,'pk',0,((0,'reservation_id'),)),
        ('idx_budget_reservations_budget',0,'c',0,((1,'budget_id'),(5,'state'))),
        ('idx_budget_reservations_idem',1,'c',1,((1,'budget_id'),(8,'idempotency_key'))),
    ),
}
_COLUMN_SQL = """SELECT cid,
    CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB))<=64 THEN name END,
    CASE WHEN typeof(type)='text' AND length(CAST(type AS BLOB))<=32 THEN type END,
    "notnull",
    CASE WHEN dflt_value IS NULL OR (typeof(dflt_value)='text'
         AND length(CAST(dflt_value AS BLOB))<=64) THEN dflt_value END,
    pk, CASE WHEN dflt_value IS NULL OR (typeof(dflt_value)='text'
         AND length(CAST(dflt_value AS BLOB))<=64) THEN 1 ELSE 0 END
    FROM pragma_table_info(?, 'main') LIMIT 12"""
_INDEX_SQL = """SELECT
    CASE WHEN typeof(name)='text' AND length(CAST(name AS BLOB))<=128 THEN name END,
    "unique", origin, partial FROM pragma_index_list(?, 'main') LIMIT 17"""
_INDEX_COLUMNS_SQL = """SELECT seqno,cid,
    CASE WHEN name IS NULL OR (typeof(name)='text' AND length(CAST(name AS BLOB))<=64) THEN name END,
    "desc", CASE WHEN typeof(coll)='text' AND length(CAST(coll AS BLOB))<=32 THEN coll END,
    "key" FROM pragma_index_xinfo(?, 'main') LIMIT 9"""
_BUDGET_META_SQL = """SELECT
    typeof(budget_id),length(CAST(budget_id AS BLOB)),
    typeof(project_id),length(CAST(project_id AS BLOB)),
    typeof(order_id),length(CAST(order_id AS BLOB)),
    typeof(customer_id),length(CAST(customer_id AS BLOB)),
    typeof(environment_id),length(CAST(environment_id AS BLOB)),
    typeof(state),length(CAST(state AS BLOB)),
    typeof(health),length(CAST(health AS BLOB)),
    typeof(limits_json),length(CAST(limits_json AS BLOB))
    FROM main.run_budgets WHERE budget_id=? LIMIT 2"""
_BUDGET_SQL = """SELECT budget_id,project_id,order_id,customer_id,
    environment_id,state,health,limits_json FROM main.run_budgets
    WHERE budget_id=? LIMIT 2"""
_RESERVATION_META_SQL = """SELECT count(*),
    coalesce(sum(coalesce(length(CAST(kind AS BLOB)),0)
        +coalesce(length(CAST(state AS BLOB)),0)+16),0),
    coalesce(sum(CASE WHEN typeof(kind)!='text' OR length(CAST(kind AS BLOB))>64
        OR typeof(state)!='text' OR length(CAST(state AS BLOB))>64
        OR typeof(amount) NOT IN ('integer','real')
        OR typeof(settled_amount) NOT IN ('integer','real') THEN 1 ELSE 0 END),0),
    coalesce(sum(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0)
    FROM (SELECT kind,amount,settled_amount,state FROM main.budget_reservations
        WHERE budget_id=? LIMIT 10001)"""
_RESERVATIONS_SQL = """SELECT kind,amount,settled_amount,state
    FROM main.budget_reservations WHERE budget_id=? LIMIT 10001"""


class CurrentLaunchBudgetObservationError(ValueError):
    """Fixed context-free failure, not a spending or authorization outcome."""
    def __init__(self,code: str) -> None:
        self.code = code if type(code) is str and code in _MESSAGES else 'invalid_source'
        super().__init__(_MESSAGES[self.code])


class _BudgetFault(Exception):
    pass


@dataclass(frozen=True,slots=True,repr=False)
class CurrentLaunchBudgetExpectation:
    """Server selectors only; no actor, source pin or reusable permission."""
    budget_id: str
    project_id: str
    order_id: str
    customer_id: str
    environment_id: str


@dataclass(frozen=True,slots=True,repr=False)
class CurrentLaunchBudgetObservation:
    """Snapshot of original arithmetic; no reserve, allowance grant or price."""
    expectation: CurrentLaunchBudgetExpectation
    lifecycle: str
    health: str
    limits: tuple[tuple[str,int | float | None],...]
    held: tuple[tuple[str,int | float],...]
    consumed: tuple[tuple[str,int | float],...]
    used: tuple[tuple[str,int | float],...]
    available: tuple[tuple[str,int | float | None],...]
    reservation_count: int
    unknown_reservations: int


def _fault(code: str) -> None:
    raise _BudgetFault(code)


def _text(value: object, *, empty: bool = False, maximum: int = 256) -> str:
    if (type(value) is not str or not value and not empty or value!=value.strip()
        or any(ord(c)<32 or 127<=ord(c)<=159 for c in value)):
        _fault('invalid_source')
    try:
        size = len(value.encode('utf-8'))
    except UnicodeError:
        _fault('invalid_source')
    if size>maximum:
        _fault('invalid_source')
    return value


def _expectation(value: object) -> tuple[str,...]:
    if type(value) is not CurrentLaunchBudgetExpectation:
        _fault('invalid_source')
    return (_text(value.budget_id),_text(value.project_id),_text(value.order_id),
        _text(value.customer_id,empty=True),_text(value.environment_id,empty=True))


def _checked_budget(value: object) -> _ReadBudget:
    if type(value) is not _ReadBudget:
        _fault('invalid_source')
    if (type(value.bytes) is not int or not 0<=value.bytes<=_MAX_TOTAL_BYTES
        or type(value.nodes) is not int or not 0<=value.nodes<=_MAX_JSON_NODES):
        _fault('invalid_source')
    return value


class _SelectedReadCap:
    """Additional cap; never enlarges, resets or refunds the shared budget."""
    def __init__(self,budget: _ReadBudget) -> None:
        self.budget,self.bytes = budget,0

    def charge(self,size: int,nodes: int) -> None:
        if type(size) is not int or type(nodes) is not int or size<0 or nodes<0:
            _fault('invalid_source')
        if size>_LOCAL_BYTES-self.bytes:
            _fault('unavailable')
        _ReadBudget.charge_bytes(self.budget,size)
        self.bytes += size
        for _ in range(nodes):
            _ReadBudget.charge_node(self.budget)

    def control_rows(self,rows: list[tuple]) -> None:
        # Finite schema/metadata scalars, not unknown object coercion.
        total,nodes = 0,0
        for row in rows:
            nodes += 1
            for item in row:
                nodes += 1
                if type(item) is str:
                    total += len(item.encode('utf-8'))
                elif type(item) in (int,float):
                    total += 8
                elif item is None:
                    total += 1
                else:
                    _fault('invalid_source')
        self.charge(total,nodes)


def _native_cursor_fetchmany(cursor: sqlite3.Cursor,limit: int) -> list:
    """True C operation; tests may inject only after this real operation."""
    if type(cursor) is not sqlite3.Cursor or type(limit) is not int or not 1<=limit<=10001:
        _fault('invalid_source')
    return sqlite3.Cursor.fetchmany(cursor,limit)


def _native_cursor_close(cursor: sqlite3.Cursor) -> None:
    """True C close, no public hook, connection factory or permission callback."""
    if type(cursor) is not sqlite3.Cursor:
        _fault('invalid_source')
    return sqlite3.Cursor.close(cursor)


def _rows(conn: sqlite3.Connection,sql: str,params: tuple,limit: int) -> list[tuple]:
    # Native only; single cursor close, original primary BaseException retained.
    cursor = sqlite3.Connection.execute(conn,sql,params)
    failure,result = None,None
    try:
        fetched = _native_cursor_fetchmany(cursor,limit)
        if type(fetched) is not list or len(fetched)>limit:
            _fault('invalid_source')
        result = []
        for row in fetched:
            if type(row) is not tuple and type(row) is not sqlite3.Row:
                _fault('invalid_source')
            result.append(tuple(row))
    except BaseException as exc:
        failure = exc
    try:
        if _native_cursor_close(cursor) is not None:
            _fault('unavailable')
    except BaseException as exc:
        if failure is None:
            failure = exc
    if failure is not None:
        raise failure
    return result


def _schema(conn: sqlite3.Connection,cap: _SelectedReadCap) -> None:
    for table,expected in (('run_budgets',_BUDGET_COLUMNS),('budget_reservations',_RESERVATION_COLUMNS)):
        rows = _rows(conn,_COLUMN_SQL,(table,),12)
        cap.control_rows(rows)
        if len(rows)!=len(expected):
            _fault('unavailable')
        for cid,(row,wanted) in enumerate(zip(rows,expected)):
            if (len(row)!=7 or type(row[0]) is not int or row[0]!=cid or row[6]!=1
                or type(row[3]) is not int or type(row[5]) is not int
                or row[1:6]!=wanted):
                _fault('invalid_source')
        indexes = _rows(conn,_INDEX_SQL,(table,),17)
        cap.control_rows(indexes)
        wanted_indexes = _INDEXES[table]
        if len(indexes)!=len(wanted_indexes):
            _fault('invalid_source')
        by_name = {}
        for row in indexes:
            if (len(row)!=4 or type(row[0]) is not str or row[0] in by_name
                or type(row[1]) is not int or type(row[2]) is not str or type(row[3]) is not int):
                _fault('invalid_source')
            by_name[row[0]] = row
        for name,unique,origin,partial,columns in wanted_indexes:
            if by_name.get(name)!=(name,unique,origin,partial):
                _fault('invalid_source')
            rows = _rows(conn,_INDEX_COLUMNS_SQL,(name,),9)
            cap.control_rows(rows)
            wanted = [(i,cid,column,0,'BINARY',1) for i,(cid,column) in enumerate(columns)]
            wanted.append((len(columns),-1,None,0,'BINARY',0))
            if rows!=wanted or any(type(row[i]) is not int for row in rows for i in (0,1,3,5)):
                _fault('invalid_source')


def _read_budget(conn: sqlite3.Connection,expected: tuple[str,...],cap: _SelectedReadCap) -> tuple:
    metadata = _rows(conn,_BUDGET_META_SQL,(expected[0],),2)
    cap.control_rows(metadata)
    if not metadata:
        _fault('unavailable')
    if len(metadata)!=1 or len(metadata[0])!=16:
        _fault('invalid_source')
    sizes = []
    for i,maximum in enumerate((256,256,256,256,256,64,64,8192)):
        kind,size = metadata[0][2*i:2*i+2]
        if kind!='text' or type(size) is not int or not 0<=size<=maximum:
            _fault('invalid_source')
        sizes.append(size)
    cap.charge(sum(sizes),9)
    rows = _rows(conn,_BUDGET_SQL,(expected[0],),2)
    if len(rows)!=1 or len(rows[0])!=8:
        _fault('invalid_source')
    row = rows[0]
    if (any(type(v) is not str for v in row)
        or any(len(v.encode('utf-8'))!=size for v,size in zip(row,sizes))
        or tuple(_text(v,empty=i in (3,4)) for i,v in enumerate(row[:5]))!=expected):
        _fault('invalid_source')
    _text(row[5],maximum=64); _text(row[6],maximum=64)
    if row[5]!='open' or row[6]!='ok':
        _fault('unavailable')
    return row


def _read_usage(conn: sqlite3.Connection,budget_id: str,cap: _SelectedReadCap) -> tuple[list[dict],int]:
    metadata = _rows(conn,_RESERVATION_META_SQL,(budget_id,),1)
    cap.control_rows(metadata)
    if (len(metadata)!=1 or len(metadata[0])!=4
        or any(type(v) is not int or v<0 for v in metadata[0])):
        _fault('invalid_source')
    count,bytes_read,invalid,unknown = metadata[0]
    if invalid or unknown>count:
        _fault('invalid_source')
    if count>_MAX_ROWS:
        _fault('unavailable')
    # Metadata is evaluated first; don't materialize an over-cap data set.
    cap.charge(bytes_read,1+count*5)
    rows = _rows(conn,_RESERVATIONS_SQL,(budget_id,),10001)
    if len(rows)!=count or any(len(row)!=4 for row in rows):
        _fault('invalid_source')
    real_bytes,real_unknown = 0,0
    usage = []
    for kind,amount,settled,state in rows:
        if (type(kind) is not str or type(state) is not str
            or type(amount) not in (int,float) or type(settled) not in (int,float)
            or len(kind.encode('utf-8'))>64 or len(state.encode('utf-8'))>64):
            _fault('invalid_source')
        real_bytes += len(kind.encode('utf-8'))+len(state.encode('utf-8'))+16
        real_unknown += int(state=='unknown')
        usage.append(dict(kind=kind,amount=amount,settled_amount=settled,state=state))
    if real_bytes!=bytes_read or real_unknown!=unknown:
        _fault('invalid_source')
    return usage,unknown


def _observe(conn: sqlite3.Connection,*,business_store: BusinessStore,
    expectation: CurrentLaunchBudgetExpectation,_budget: _ReadBudget) -> CurrentLaunchBudgetObservation:
    expected = _expectation(expectation)
    budget = _checked_budget(_budget)
    if type(business_store) is not BusinessStore:
        _fault('invalid_source')
    fields = object.__getattribute__(business_store,'__dict__')
    if type(fields) is not dict or 'path' not in fields:
        _fault('invalid_source')
    captured_path = fields['path']
    cwd = Path.cwd()
    expected_main = _source_path(captured_path,cwd)
    _owned_rw_connection(conn,expected_main=expected_main)
    cap = _SelectedReadCap(budget)
    cap.charge(len(str(expected_main).encode('utf-8'))+sum(len(v.encode('utf-8')) for v in expected),6)
    if (BusinessStore.BUDGET_KINDS!=_KINDS or BusinessStore.BUDGET_COUNT_KINDS!=_COUNT_KINDS
        or tuple(BusinessStore.RESERVATION_TRANSITIONS)!=_STATES):
        _fault('invalid_source')
    _schema(conn,cap)
    row = _read_budget(conn,expected,cap)
    usage,unknown = _read_usage(conn,expected[0],cap)
    # These are the original base-class calculations, not a new money decoder.
    limits = BusinessStore._budget_limits({'limits_json':row[7]})
    held,consumed,used = BusinessStore._budget_usage(usage)
    available = BusinessStore._budget_available(limits,used)
    cap.charge(0,1+2*len(limits))
    if (object.__getattribute__(business_store,'__dict__') is not fields
        or fields.get('path') is not captured_path or Path.cwd()!=cwd
        or _expectation(expectation)!=expected or _checked_budget(budget) is not budget):
        _fault('unavailable')
    _owned_rw_connection(conn,expected_main=expected_main)
    return CurrentLaunchBudgetObservation(
        expectation=expectation,lifecycle=row[5],health=row[6],
        limits=tuple((kind,limits.get(kind)) for kind in _KINDS),
        held=tuple((kind,held.get(kind,0)) for kind in _KINDS),
        consumed=tuple((kind,consumed.get(kind,0)) for kind in _KINDS),
        used=tuple((kind,used[kind]) for kind in _KINDS),
        available=tuple((kind,available[kind]) for kind in _KINDS),
        reservation_count=len(usage),unknown_reservations=unknown,
    )


def _observe_current_launch_budget_on_connection(conn: sqlite3.Connection,*,
    business_store: BusinessStore,expectation: CurrentLaunchBudgetExpectation,
    _budget: _ReadBudget) -> CurrentLaunchBudgetObservation:
    """Private same-transaction current observation, never permission to spend.

    RW-only original main identity guard is reused unchanged. No connection,
    transaction, schema, reservation, lifecycle, token or file is mutated. The
    caller must not reuse this past snapshot as a permit after an await.
    Ordinary failures are two fixed codes outside their handlers; original
    BaseException object propagates, cursor cleanup never replaces it.
    """
    result,failure = None,None
    try:
        result = _observe(conn,business_store=business_store,expectation=expectation,_budget=_budget)
    except _BudgetFault as exc:
        failure = (exc.args[0] if type(exc) is _BudgetFault and len(exc.args)==1
            and type(exc.args[0]) is str and exc.args[0] in _MESSAGES else 'invalid_source')
    except (_OwnerFault,_PrincipalFault) as exc:
        code = (exc.args[0] if (type(exc) is _OwnerFault or type(exc) is _PrincipalFault)
            and type(exc.args) is tuple and len(exc.args)==1 and type(exc.args[0]) is str else None)
        failure = 'invalid_source' if code=='invalid_source' else 'unavailable'
    except Exception:
        failure = 'unavailable'
    if failure is not None:
        raise CurrentLaunchBudgetObservationError(failure) from None
    return result
