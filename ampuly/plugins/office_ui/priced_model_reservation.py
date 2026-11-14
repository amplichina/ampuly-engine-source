"""Private same-transaction quote/group append; NOT a pricing authority.

This module never opens, commits, rolls back, migrates or dispatches. Its data
carriers, quote bytes, source_id and returned claims do not authorize spending.
Only a real server-registered pricing adapter observing the selected prepared
call may supply these inputs through a separately reviewed host. That host must
own BEGIN IMMEDIATE/commit confirmation and decline retained groups as fresh
dispatch permissions. No host/adapter/registration/actual-charge chain is wired
by this leaf. Missing that chain keeps production monetary dispatch refused.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import sqlite3

from .business_store import BusinessStore, OrderTransitionError

UNIT_SCALE = 1_000_000
MAX_UNITS = 2**53 - 1
MAX_TOTAL_UNITS = 2**63 - 1
MAX_QUOTE_BYTES = 64 * 1024
QUOTE_TABLE = "model_priced_call_quotes_v1"
DISPATCH_TABLE = "model_priced_call_dispatch_v1"
QUOTE_COLUMNS = (
    "call_id", "budget_id", "project_id", "customer_id", "environment_id", "order_id",
    "prepared_fingerprint", "quote_sha256", "quote_payload", "unit_scale", "max_charge_units",
    "real_hold", "model_call_reservation_id", "retry_reservation_id", "money_reservation_id",
    "created_at", "quote_valid_until",
)
DISPATCH_COLUMNS = ("call_id", "state", "revision", "claimed_at", "updated_at")
QUOTE_FIELDS = frozenset((
    "quote_version", "quote_id", "source_id", "source_revision", "provider_id", "billing_binding_id",
    "requested_model", "endpoint_class", "prepared_fingerprint", "currency", "unit_scale",
    "max_charge_units", "valid_from", "valid_until", "evidence_id", "billable_bounds", "billed_models",
))
BOUND_FIELDS = frozenset(("input_tokens", "output_tokens", "reasoning_tokens", "cache_read_tokens",
                          "cache_write_tokens", "extra_fee_units"))
_MESSAGES = {
    "invalid_source": "模型报价预约来源无法核验。",
    "unavailable": "模型报价预约暂不可用。",
    "conflict": "模型报价预约状态冲突。",
    "budget_closed": "预算已关闭。",
    "budget_held": "预算仍待核实。",
    "budget_exhausted": "预算不足以保留本次调用。",
    "quote_expired": "本次报价不在有效期。",
}


class PricedReservationError(ValueError):
    def __init__(self, code: str):
        self.code = code if type(code) is str and code in _MESSAGES else "unavailable"
        super().__init__(_MESSAGES[self.code])


@dataclass(frozen=True, slots=True, repr=False)
class PricedBudgetScope:
    budget_id: str
    project_id: str
    customer_id: str
    environment_id: str
    order_id: str


@dataclass(frozen=True, slots=True, repr=False)
class SelectedPreparedCall:
    pricing_source_id: str
    pricing_source_revision: str
    provider_id: str
    billing_binding_id: str
    requested_model: str
    endpoint_class: str
    prepared_fingerprint: str


@dataclass(frozen=True, slots=True, repr=False)
class PricedReservationGroup:
    call_id: str
    budget_id: str
    quote_sha256: str
    prepared_fingerprint: str
    max_charge_units: int
    real_hold: float
    model_call_reservation_id: str
    retry_reservation_id: str
    money_reservation_id: str
    newly_created: bool


@dataclass(frozen=True, slots=True, repr=False)
class PricedDispatchClaim:
    call_id: str
    budget_id: str
    quote_sha256: str
    revision: int


# Reference only; do not execute these automatically on an existing database.
SCHEMA_STATEMENTS = (
    f"""CREATE TABLE {QUOTE_TABLE} (
    call_id TEXT PRIMARY KEY NOT NULL,
    budget_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    environment_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    prepared_fingerprint TEXT NOT NULL CHECK(length(prepared_fingerprint)=64),
    quote_sha256 TEXT NOT NULL CHECK(length(quote_sha256)=64),
    quote_payload BLOB NOT NULL CHECK(typeof(quote_payload)='blob' AND length(quote_payload) BETWEEN 1 AND 65536),
    unit_scale INTEGER NOT NULL CHECK(typeof(unit_scale)='integer' AND unit_scale=1000000),
    max_charge_units INTEGER NOT NULL CHECK(typeof(max_charge_units)='integer' AND max_charge_units BETWEEN 1 AND 9007199254740991),
    real_hold REAL NOT NULL CHECK(real_hold>0 AND real_hold<10000000000),
    model_call_reservation_id TEXT NOT NULL UNIQUE,
    retry_reservation_id TEXT NOT NULL,
    money_reservation_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    quote_valid_until TEXT NOT NULL,
    FOREIGN KEY(budget_id) REFERENCES run_budgets(budget_id),
    FOREIGN KEY(model_call_reservation_id) REFERENCES budget_reservations(reservation_id),
    FOREIGN KEY(money_reservation_id) REFERENCES budget_reservations(reservation_id)
)""",
    f"""CREATE TRIGGER {QUOTE_TABLE}_no_update BEFORE UPDATE ON {QUOTE_TABLE}
BEGIN SELECT RAISE(ABORT, 'priced quote immutable'); END""",
    f"""CREATE TRIGGER {QUOTE_TABLE}_no_delete BEFORE DELETE ON {QUOTE_TABLE}
BEGIN SELECT RAISE(ABORT, 'priced quote immutable'); END""",
    f"""CREATE TRIGGER {QUOTE_TABLE}_no_duplicate BEFORE INSERT ON {QUOTE_TABLE}
WHEN EXISTS (SELECT 1 FROM {QUOTE_TABLE} WHERE call_id=NEW.call_id) OR
     EXISTS (SELECT 1 FROM {QUOTE_TABLE} WHERE model_call_reservation_id=NEW.model_call_reservation_id) OR
     EXISTS (SELECT 1 FROM {QUOTE_TABLE} WHERE money_reservation_id=NEW.money_reservation_id) OR
     (NEW.retry_reservation_id!='' AND EXISTS
      (SELECT 1 FROM {QUOTE_TABLE} WHERE retry_reservation_id=NEW.retry_reservation_id))
BEGIN SELECT RAISE(ABORT, 'priced quote duplicate'); END""",
    f"CREATE UNIQUE INDEX {QUOTE_TABLE}_retry ON {QUOTE_TABLE}(retry_reservation_id) WHERE retry_reservation_id!=''",
    f"""CREATE TABLE {DISPATCH_TABLE} (
    call_id TEXT PRIMARY KEY NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('reserved','dispatching','released','outcome_unknown')),
    revision INTEGER NOT NULL CHECK(typeof(revision)='integer' AND revision BETWEEN 0 AND 9223372036854775807),
    claimed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(call_id) REFERENCES {QUOTE_TABLE}(call_id)
)""",
    f"""CREATE TRIGGER {DISPATCH_TABLE}_initial BEFORE INSERT ON {DISPATCH_TABLE}
WHEN NEW.state!='reserved' OR NEW.revision!=0 OR NEW.claimed_at!='' OR
     EXISTS (SELECT 1 FROM {DISPATCH_TABLE} WHERE call_id=NEW.call_id)
BEGIN SELECT RAISE(ABORT, 'priced dispatch initial'); END""",
    f"""CREATE TRIGGER {DISPATCH_TABLE}_transition BEFORE UPDATE ON {DISPATCH_TABLE}
WHEN NEW.call_id!=OLD.call_id OR NEW.revision!=OLD.revision+1 OR
     NOT ((OLD.state='reserved' AND NEW.state IN ('dispatching','released')) OR
          (OLD.state='dispatching' AND NEW.state='outcome_unknown'))
BEGIN SELECT RAISE(ABORT, 'priced dispatch transition'); END""",
    f"""CREATE TRIGGER {DISPATCH_TABLE}_no_delete BEFORE DELETE ON {DISPATCH_TABLE}
BEGIN SELECT RAISE(ABORT, 'priced dispatch retained'); END""",
)
_SCHEMA_OBJECTS = (
    ("table", QUOTE_TABLE), ("trigger", QUOTE_TABLE+"_no_update"),
    ("trigger", QUOTE_TABLE+"_no_delete"), ("trigger", QUOTE_TABLE+"_no_duplicate"),
    ("index", QUOTE_TABLE+"_retry"), ("table", DISPATCH_TABLE),
    ("trigger", DISPATCH_TABLE+"_initial"),
    ("trigger", DISPATCH_TABLE+"_transition"), ("trigger", DISPATCH_TABLE+"_no_delete"),
)
_SCHEMA_TABLES = (QUOTE_TABLE,QUOTE_TABLE,QUOTE_TABLE,QUOTE_TABLE,QUOTE_TABLE,
                  DISPATCH_TABLE,DISPATCH_TABLE,DISPATCH_TABLE,DISPATCH_TABLE)


def _fail(code="invalid_source"):
    raise PricedReservationError(code)


def _text(value, *, empty=False):
    if (type(value) is not str or (not value and not empty) or value != value.strip()
            or len(value)>512 or any(ord(c)<32 or 127<=ord(c)<=159 for c in value)):
        _fail()
    try:
        if len(value.encode("utf-8"))>512: _fail()
    except UnicodeError:
        _fail()
    return value


def _digest(value):
    if type(value) is not str or len(value)!=64 or any(c not in "0123456789abcdef" for c in value): _fail()
    return value


def _units(value, *, positive=False, maximum=MAX_TOTAL_UNITS):
    if type(value) is not int or value < (1 if positive else 0) or value>maximum: _fail()
    return value


def _scope(scope):
    if type(scope) is not PricedBudgetScope: _fail()
    return (_text(scope.budget_id),_text(scope.project_id),_text(scope.customer_id,empty=True),
            _text(scope.environment_id,empty=True),_text(scope.order_id,empty=True))


def _selection(selected):
    if type(selected) is not SelectedPreparedCall: _fail()
    return tuple(_text(getattr(selected,n)) for n in (
        "pricing_source_id","pricing_source_revision","provider_id","billing_binding_id",
        "requested_model","endpoint_class")) + (_digest(selected.prepared_fingerprint),)


def _clock(value):
    if type(value) is not datetime or value.tzinfo is not timezone.utc: _fail()
    return value


def _time(value):
    _text(value)
    try:
        result=datetime.fromisoformat(value)
        if result.utcoffset() is None or result.utcoffset().total_seconds()!=0: _fail()
        return result.astimezone(timezone.utc)
    except (ValueError,OverflowError):
        _fail()


def _rows(conn, sql, args=(), *, cap=2):
    cursor=conn.execute(sql,args)
    try:
        rows=cursor.fetchmany(cap+1)
        if len(rows)>cap: _fail()
    except BaseException:
        try: cursor.close()
        except BaseException: pass
        raise
    else:
        cursor.close()
        return rows


def _one_change(conn,sql,args):
    cursor=conn.execute(sql,args)
    try:
        if type(cursor.rowcount) is not int or cursor.rowcount!=1: _fail("conflict")
    except BaseException:
        try: cursor.close()
        except BaseException: pass
        raise
    else:
        cursor.close()


def _connection(conn):
    if (type(conn) is not sqlite3.Connection or not conn.in_transaction
            or conn.row_factory is not sqlite3.Row or conn.text_factory is not str): _fail()
    for pragma,expected in (("query_only",0),("foreign_keys",1)):
        rows=_rows(conn,"PRAGMA "+pragma)
        if len(rows)!=1 or tuple(rows[0])!=(expected,): _fail("unavailable")
    # in_transaction alone does not prove BEGIN IMMEDIATE: the host owns that.


def _schema(conn):
    # Budget tables have no approved triggers. Extra main/TEMP triggers may
    # mutate history inside our strict INSERT/UPDATE; positive object checks
    # alone cannot establish this schema. Also refuse TEMP table/view shadows.
    tables=(QUOTE_TABLE,DISPATCH_TABLE,"run_budgets","budget_reservations")
    names=tuple(name for _,name in _SCHEMA_OBJECTS)
    table_slots=",".join("?" for _ in tables)
    name_slots=",".join("?" for _ in names)
    main_where=f"((type='trigger' AND tbl_name IN ({table_slots})) OR name IN ({name_slots}))"
    temp_where=f"((type='trigger' AND tbl_name IN ({table_slots})) OR (type IN ('table','view') AND name IN ({table_slots})))"
    args=(*tables,*names,*tables,*tables)
    def inventory(fields):
        return (f"SELECT 'main' AS origin,rowid AS object_rowid,{fields} FROM main.sqlite_master WHERE {main_where} "
                f"UNION ALL SELECT 'temp' AS origin,rowid AS object_rowid,{fields} FROM temp.sqlite_master WHERE {temp_where} "
                "ORDER BY origin,object_rowid LIMIT 10")
    sizes=_rows(conn,inventory("typeof(type),length(CAST(type AS BLOB)),typeof(name),length(CAST(name AS BLOB)),"
                              "typeof(tbl_name),length(CAST(tbl_name AS BLOB)),typeof(sql),length(CAST(sql AS BLOB))"),args,cap=9)
    if len(sizes)!=len(_SCHEMA_OBJECTS): _fail("unavailable")
    total=0
    for row in sizes:
        if row[0]!="main": _fail()
        if type(row[1]) is not int or row[1]<=0: _fail()
        for offset,limit in ((2,32),(4,512),(6,512),(8,8192)):
            length=row[offset+1]
            if row[offset]!="text" or type(length) is not int or not 0<length<=limit: _fail()
            total+=length
            if total>64*1024: _fail()
    expected={key:(table,sql) for key,table,sql in zip(_SCHEMA_OBJECTS,_SCHEMA_TABLES,SCHEMA_STATEMENTS)}
    seen=set()
    for size in sizes:
        # Use only previously approved byte limits, even if a caller violates
        # owned-connection serialization. Recheck lengths in the same SELECT.
        rows=_rows(conn,"SELECT typeof(type),length(CAST(type AS BLOB)),typeof(name),length(CAST(name AS BLOB)),"
                        "typeof(tbl_name),length(CAST(tbl_name AS BLOB)),typeof(sql),length(CAST(sql AS BLOB)),"
                        "substr(CAST(type AS BLOB),1,?),substr(CAST(name AS BLOB),1,?),"
                        "substr(CAST(tbl_name AS BLOB),1,?),substr(CAST(sql AS BLOB),1,?) "
                        "FROM main.sqlite_master WHERE rowid=? LIMIT 2",
                   (size[3],size[5],size[7],size[9],size[1]))
        if len(rows)!=1 or tuple(rows[0][:8])!=tuple(size[2:]): _fail()
        row=rows[0]
        decoded=[]
        for index,offset in ((8,2),(9,4),(10,6),(11,8)):
            value=row[index]
            if type(value) is not bytes or len(value)!=size[offset+1]: _fail()
            try: decoded.append(value.decode("utf-8"))
            except UnicodeError: _fail()
        kind,name,table,sql=decoded
        key=(kind,name)
        if key not in expected or key in seen: _fail()
        expected_table,expected_sql=expected[key]
        if table!=expected_table or sql.strip().removesuffix(";").rstrip()!=expected_sql.strip().removesuffix(";").rstrip(): _fail()
        seen.add(key)
    if seen!=set(expected): _fail("unavailable")
    for table,columns in ((QUOTE_TABLE,QUOTE_COLUMNS),(DISPATCH_TABLE,DISPATCH_COLUMNS)):
        rows=_rows(conn,"SELECT name FROM pragma_table_info(?)",(table,),cap=len(columns))
        if tuple(row[0] for row in rows)!=columns: _fail()


def _quote(payload,digest,selected=None):
    if type(payload) is not bytes or not 0<len(payload)<=MAX_QUOTE_BYTES: _fail()
    _digest(digest)
    if hashlib.sha256(payload).hexdigest()!=digest: _fail()
    def pairs(items):
        if len(items)>len(QUOTE_FIELDS): _fail()
        result={}
        for key,value in items:
            if type(key) is not str or key in result: _fail()
            result[key]=value
        return result
    def reject(_): _fail()
    try:
        data=json.loads(payload,object_pairs_hook=pairs,parse_constant=reject)
    except (ValueError,UnicodeError,RecursionError): _fail()
    if type(data) is not dict or frozenset(data)!=QUOTE_FIELDS: _fail()
    if data["quote_version"]!="model_pricing_quote.v1" or data["currency"]!="CNY" or type(data["unit_scale"]) is not int or data["unit_scale"]!=UNIT_SCALE: _fail()
    for key in ("quote_id","source_id","source_revision","provider_id","billing_binding_id",
                "requested_model","endpoint_class","evidence_id"):
        _text(data[key])
    _digest(data["prepared_fingerprint"])
    _units(data["max_charge_units"],positive=True,maximum=MAX_UNITS)
    bounds=data["billable_bounds"]
    if type(bounds) is not dict or frozenset(bounds)!=BOUND_FIELDS: _fail()
    for value in bounds.values(): _units(value)
    models=data["billed_models"]
    if type(models) is not list or not 1<=len(models)<=16: _fail()
    if len(set(_text(model) for model in models))!=len(models): _fail()
    if _time(data["valid_from"])>=_time(data["valid_until"]): _fail()
    try:
        canonical=json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",",":"),allow_nan=False).encode("utf-8")
    except (ValueError,UnicodeError,TypeError): _fail()
    if canonical!=payload: _fail()
    if selected is not None:
        values=_selection(selected)
        if values!=tuple(data[key] for key in ("source_id","source_revision","provider_id",
            "billing_binding_id","requested_model","endpoint_class","prepared_fingerprint")): _fail()
    return data


def _valid(quote,now):
    if not _time(quote["valid_from"])<=now<_time(quote["valid_until"]): _fail("quote_expired")


def _fraction(value):
    if type(value) is int: return Fraction(value)
    if type(value) is float and math.isfinite(value): return Fraction.from_float(value)
    _fail()


def _ceil_units(value):
    rational=_fraction(value)*UNIT_SCALE
    if rational<0: _fail()
    result=-(-rational.numerator//rational.denominator)
    return _units(result)


def _hold(units):
    q=Fraction(_units(units,positive=True,maximum=MAX_UNITS),UNIT_SCALE)
    f=float(q)
    if Fraction.from_float(f)<q: f=math.nextafter(f,math.inf)
    if type(f) is not float or not math.isfinite(f) or f<=0 or Fraction.from_float(f)<q: _fail()
    return f,_ceil_units(f)


def _budget(conn,scope,*,live):
    values=_scope(scope)
    meta=_rows(conn,"SELECT typeof(limits_json),length(CAST(limits_json AS BLOB)),length(CAST(project_id AS BLOB)),length(CAST(customer_id AS BLOB)),length(CAST(environment_id AS BLOB)),length(CAST(order_id AS BLOB)),length(CAST(state AS BLOB)),length(CAST(health AS BLOB)) FROM run_budgets INDEXED BY sqlite_autoindex_run_budgets_1 WHERE budget_id=? LIMIT 2",(values[0],))
    if len(meta)!=1: _fail("unavailable")
    checks=tuple(meta[0])
    if (checks[0]!="text" or type(checks[1]) is not int or not 1<=checks[1]<=8192
            or any(type(n) is not int or not 0<=n<=512 for n in checks[2:])): _fail()
    rows=_rows(conn,"SELECT budget_id,project_id,customer_id,environment_id,order_id,limits_json,state,health FROM run_budgets INDEXED BY sqlite_autoindex_run_budgets_1 WHERE budget_id=? LIMIT 2",(values[0],))
    if len(rows)!=1: _fail("unavailable")
    row=rows[0]
    if tuple(row[key] for key in ("budget_id","project_id","customer_id","environment_id","order_id"))!=values: _fail()
    if row["state"] not in ("open","closed") or row["health"] not in ("ok","pending_verification"): _fail()
    if live:
        if row["state"]!="open": _fail("budget_closed")
        if row["health"]!="ok": _fail("budget_held")
    return row


def _history(conn,budget_id):
    # Invalid nonnumeric/oversized values become NULL and the original codec
    # rejects them; never materialize an unbounded kind/state string first.
    return _rows(conn,"SELECT substr(kind,1,65) AS kind,CASE WHEN typeof(amount) IN ('integer','real') THEN amount END AS amount,CASE WHEN typeof(settled_amount) IN ('integer','real') THEN settled_amount END AS settled_amount,substr(state,1,65) AS state FROM budget_reservations INDEXED BY idx_budget_reservations_budget WHERE budget_id=? LIMIT 10001",(budget_id,),cap=10001)


def _ids(call_id,retrying):
    _text(call_id)
    prefix="pr-"+hashlib.sha256(("priced_model_reservation.v1|"+call_id).encode("utf-8")).hexdigest()
    return prefix+"-model",prefix+"-retry" if retrying else "",prefix+"-money"


def _used_units(history):
    total=0
    for row in history:
        if row["kind"]=="cost_cny" and row["state"]!="released":
            value=row["settled_amount"] if row["state"]=="settled" else row["amount"]
            total=_units(total+_ceil_units(value))
    return total


def _retained(conn,scope,call_id,quote_sha256,selected=None):
    text_columns=QUOTE_COLUMNS[:8]+QUOTE_COLUMNS[12:]
    bounded=','.join('length(CAST('+column+' AS BLOB))' for column in text_columns)
    meta=_rows(conn,f"SELECT typeof(quote_payload),length(quote_payload),{bounded} FROM {QUOTE_TABLE} WHERE call_id=? LIMIT 2",(call_id,))
    if not meta: return None
    if len(meta)!=1 or meta[0][0]!="blob" or type(meta[0][1]) is not int or not 1<=meta[0][1]<=MAX_QUOTE_BYTES: _fail()
    if any(type(size) is not int or not 0<=size<=512 for size in tuple(meta[0])[2:]): _fail()
    rows=_rows(conn,f"SELECT {','.join(QUOTE_COLUMNS)} FROM {QUOTE_TABLE} WHERE call_id=? LIMIT 2",(call_id,))
    if len(rows)!=1: _fail()
    row=rows[0]
    if tuple(row[k] for k in ("budget_id","project_id","customer_id","environment_id","order_id"))!=_scope(scope): _fail()
    if row["call_id"]!=call_id or row["quote_sha256"]!=quote_sha256: _fail("conflict")
    quote=_quote(row["quote_payload"],row["quote_sha256"],selected)
    if (row["prepared_fingerprint"]!=quote["prepared_fingerprint"] or type(row["unit_scale"]) is not int
            or row["unit_scale"]!=UNIT_SCALE or row["max_charge_units"]!=quote["max_charge_units"]
            or type(row["max_charge_units"]) is not int or row["quote_valid_until"]!=quote["valid_until"]): _fail()
    f,_=_hold(row["max_charge_units"])
    if type(row["real_hold"]) is not float or row["real_hold"]!=f: _fail()
    retrying=bool(row["retry_reservation_id"])
    if (row["model_call_reservation_id"],row["retry_reservation_id"],row["money_reservation_id"])!=_ids(call_id,retrying): _fail()
    state_meta=_rows(conn,f"SELECT length(CAST(state AS BLOB)),length(CAST(claimed_at AS BLOB)),length(CAST(updated_at AS BLOB)) FROM {DISPATCH_TABLE} WHERE call_id=? LIMIT 2",(call_id,))
    if len(state_meta)!=1 or any(type(size) is not int or not 0<=size<=512 for size in tuple(state_meta[0])): _fail()
    states=_rows(conn,f"SELECT state,revision,claimed_at,updated_at FROM {DISPATCH_TABLE} WHERE call_id=? LIMIT 2",(call_id,))
    if len(states)!=1 or states[0]["state"] not in ("reserved","dispatching","released","outcome_unknown"): _fail()
    _units(states[0]["revision"])
    return row,quote,states[0]


def _reservations(conn,scope,row,*,held=False):
    result=[]
    for key,kind,amount in (("model_call_reservation_id","model_call",1.0),
                            ("retry_reservation_id","retry",1.0),
                            ("money_reservation_id","cost_cny",row["real_hold"])):
        rid=row[key]
        if not rid:
            if kind!="retry": _fail()
            continue
        rows=_rows(conn,"SELECT substr(budget_id,1,513) AS budget_id,substr(kind,1,65) AS kind,CASE WHEN typeof(amount) IN ('integer','real') THEN amount END AS amount,substr(state,1,65) AS state,substr(idempotency_key,1,513) AS idempotency_key,substr(request_fingerprint,1,65) AS request_fingerprint FROM budget_reservations INDEXED BY sqlite_autoindex_budget_reservations_1 WHERE reservation_id=? LIMIT 2",(rid,))
        if (len(rows)!=1 or tuple(rows[0])[:3]!=(scope.budget_id,kind,amount)
                or rows[0]["state"] not in BusinessStore.RESERVATION_TRANSITIONS): _fail()
        if (rows[0]["idempotency_key"]!=rid or rows[0]["request_fingerprint"]!=BusinessStore._reservation_fingerprint(kind,amount,"priced_model_quote.v1:"+row["quote_sha256"])): _fail()
        if held and rows[0]["state"]!="held": _fail("conflict")
        result.append((rid,kind,rows[0]["state"]))
    return result


def _group(row,*,new):
    return PricedReservationGroup(row["call_id"],row["budget_id"],row["quote_sha256"],
        row["prepared_fingerprint"],row["max_charge_units"],row["real_hold"],
        row["model_call_reservation_id"],row["retry_reservation_id"],row["money_reservation_id"],new)


def _translate(method):
    # The owning host must rollback on any failure, including BaseException.
    # A caught error is not proof that earlier statements produced zero effects.
    def call(*args,**kwargs):
        code=None
        try: return method(*args,**kwargs)
        except PricedReservationError as exc:
            code=exc.code if type(exc) is PricedReservationError and exc.code in _MESSAGES else "unavailable"
        except sqlite3.IntegrityError: code="conflict"
        except sqlite3.Error: code="unavailable"
        except OrderTransitionError: code="invalid_source"
        except Exception: code="unavailable"
        if code is not None: raise PricedReservationError(code) from None
    return call


@_translate
def append_priced_model_group(conn,*,scope:PricedBudgetScope,selected:SelectedPreparedCall,
                              call_id:str,quote_payload:bytes,quote_sha256:str,retrying:bool,
                              checked_at:datetime)->PricedReservationGroup:
    """Append on caller-owned BEGIN IMMEDIATE. Returned group is NOT permission."""
    _connection(conn);_schema(conn);_scope(scope);_selection(selected);_text(call_id);_digest(quote_sha256)
    now=_clock(checked_at)
    if type(retrying) is not bool: _fail()
    quote=_quote(quote_payload,quote_sha256,selected)
    old=_retained(conn,scope,call_id,quote_sha256,selected)
    budget=_budget(conn,scope,live=old is None)
    if old is not None:
        row,_,_=old
        if row["quote_payload"]!=quote_payload or bool(row["retry_reservation_id"])!=retrying: _fail("conflict")
        _reservations(conn,scope,row)
        return _group(row,new=False)  # historical read; host MUST NOT dispatch
    _valid(quote,now)
    limits=BusinessStore._budget_limits(budget)
    history=_history(conn,scope.budget_id)
    _,_,used=BusinessStore._budget_usage(history)
    BusinessStore._budget_available(limits,used)
    new_count=3 if retrying else 2
    if len(history)+new_count>10000: _fail()
    if "cost_cny" not in limits: _fail()
    limit_fraction=_fraction(limits["cost_cny"])*UNIT_SCALE
    floor_limit=limit_fraction.numerator//limit_fraction.denominator
    _units(floor_limit)
    integer_used=_used_units(history)
    real_hold,hold_units=_hold(quote["max_charge_units"])
    if _units(integer_used+hold_units)>floor_limit: _fail("budget_exhausted")
    for kind in (("model_call","retry") if retrying else ("model_call",)):
        next_used=used[kind]+1
        if not math.isfinite(next_used): _fail()
        if kind in limits and next_used>int(limits[kind]): _fail("budget_exhausted")
    model_id,retry_id,money_id=_ids(call_id,retrying)
    stamp=now.isoformat()
    for rid,kind,amount in ((model_id,"model_call",1.0),(retry_id,"retry",1.0),(money_id,"cost_cny",real_hold)):
        if not rid: continue
        note="priced_model_quote.v1:"+quote_sha256
        fingerprint=BusinessStore._reservation_fingerprint(kind,amount,note)
        _one_change(conn,"INSERT INTO budget_reservations(reservation_id,budget_id,kind,amount,settled_amount,state,at,actor,idempotency_key,request_fingerprint,note) VALUES(?,?,?,?,0,'held',?,'runtime',?,?,?)",
                    (rid,scope.budget_id,kind,amount,stamp,rid,fingerprint,note))
    values=(call_id,scope.budget_id,scope.project_id,scope.customer_id,scope.environment_id,scope.order_id,
            selected.prepared_fingerprint,quote_sha256,quote_payload,UNIT_SCALE,quote["max_charge_units"],
            real_hold,model_id,retry_id,money_id,stamp,quote["valid_until"])
    _one_change(conn,f"INSERT INTO {QUOTE_TABLE} ({','.join(QUOTE_COLUMNS)}) VALUES ({','.join('?' for _ in QUOTE_COLUMNS)})",values)
    _one_change(conn,f"INSERT INTO {DISPATCH_TABLE} (call_id,state,revision,claimed_at,updated_at) VALUES (?,'reserved',0,'',?)",(call_id,stamp))
    retained=_retained(conn,scope,call_id,quote_sha256,selected)
    if retained is None: _fail("unavailable")
    _reservations(conn,scope,retained[0],held=True)
    return _group(retained[0],new=True)


@_translate
def claim_priced_model_dispatch(conn,*,scope:PricedBudgetScope,selected:SelectedPreparedCall,
                                call_id:str,quote_sha256:str,checked_at:datetime)->PricedDispatchClaim:
    """CAS once. This uncommitted carrier is NOT a host/adapter authorization."""
    _connection(conn);_schema(conn);_scope(scope);_selection(selected);_text(call_id);_digest(quote_sha256)
    now=_clock(checked_at);budget=_budget(conn,scope,live=True)
    retained=_retained(conn,scope,call_id,quote_sha256,selected)
    if retained is None: _fail("unavailable")
    row,quote,state=retained;_valid(quote,now);_reservations(conn,scope,row,held=True)
    if state["state"]!="reserved" or state["revision"]!=0: _fail("conflict")
    limits=BusinessStore._budget_limits(budget)
    history=_history(conn,scope.budget_id)
    _,_,used=BusinessStore._budget_usage(history)
    available=BusinessStore._budget_available(limits,used)
    if "cost_cny" not in limits: _fail()
    bound=_fraction(limits["cost_cny"])*UNIT_SCALE
    cap=_units(bound.numerator//bound.denominator)
    if _used_units(history)>cap or any(value is not None and value<0 for value in available.values()): _fail("budget_exhausted")
    _one_change(conn,f"UPDATE {DISPATCH_TABLE} SET state='dispatching',revision=revision+1,claimed_at=?,updated_at=? WHERE call_id=? AND state='reserved' AND revision=0",
                (now.isoformat(),now.isoformat(),call_id))
    return PricedDispatchClaim(call_id,scope.budget_id,quote_sha256,1)


@_translate
def release_priced_model_group(conn,*,scope:PricedBudgetScope,call_id:str,quote_sha256:str,
                               checked_at:datetime)->None:
    """Host must independently prove no dispatch. Unknown/claimed never released."""
    _connection(conn);_schema(conn);_scope(scope);_text(call_id);_digest(quote_sha256)
    now=_clock(checked_at);_budget(conn,scope,live=False)
    retained=_retained(conn,scope,call_id,quote_sha256)
    if retained is None: _fail("unavailable")
    row,_,state=retained;items=_reservations(conn,scope,row)
    if state["state"]=="released":
        if state["revision"]!=1 or any(status!="released" for _,_,status in items): _fail()
        return None
    if state["state"]!="reserved" or state["revision"]!=0 or any(status!="held" for _,_,status in items): _fail("conflict")
    for rid,_,_ in items:
        _one_change(conn,"UPDATE budget_reservations SET state='released',note='priced quote refused before dispatch' WHERE reservation_id=? AND budget_id=? AND state='held'",(rid,scope.budget_id))
    _one_change(conn,f"UPDATE {DISPATCH_TABLE} SET state='released',revision=revision+1,updated_at=? WHERE call_id=? AND state='reserved' AND revision=0",(now.isoformat(),call_id))
    return None


@_translate
def mark_priced_model_group_unknown(conn,*,scope:PricedBudgetScope,call_id:str,quote_sha256:str,
                                    checked_at:datetime)->None:
    """Money only: do not settle counts or treat quote as an actual charge."""
    _connection(conn);_schema(conn);_scope(scope);_text(call_id);_digest(quote_sha256)
    now=_clock(checked_at);_budget(conn,scope,live=False)
    retained=_retained(conn,scope,call_id,quote_sha256)
    if retained is None: _fail("unavailable")
    row,_,state=retained;items=_reservations(conn,scope,row)
    money=[item for item in items if item[1]=="cost_cny"]
    if len(money)!=1: _fail()
    if state["state"]=="outcome_unknown":
        if state["revision"]!=2 or money[0][2]!="unknown": _fail()
    else:
        if state["state"]!="dispatching" or state["revision"]!=1 or money[0][2]!="held": _fail("conflict")
        _one_change(conn,"UPDATE budget_reservations SET state='unknown',note='priced model actual charge unknown' WHERE reservation_id=? AND budget_id=? AND state='held'",(money[0][0],scope.budget_id))
        _one_change(conn,f"UPDATE {DISPATCH_TABLE} SET state='outcome_unknown',revision=revision+1,updated_at=? WHERE call_id=? AND state='dispatching' AND revision=1",(now.isoformat(),call_id))
    _one_change(conn,"UPDATE run_budgets SET health='pending_verification',state_reason='priced model actual charge unknown' WHERE budget_id=? AND project_id=? AND customer_id=? AND environment_id=? AND order_id=?",_scope(scope))
    return None
