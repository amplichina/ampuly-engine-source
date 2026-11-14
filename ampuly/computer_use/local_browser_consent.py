"""Explicit, volatile owner consent for one order on loopback business pages.

No persisted grant, model-supplied flag, global permission or automatic restore.
The existing checkpoint and Effect consumer still own each individual action.
"""
from __future__ import annotations
from dataclasses import dataclass,field
import hashlib,ipaddress,re,secrets,time
from urllib.parse import urlsplit,parse_qs,unquote


def local_page(url: str) -> tuple[str,str,dict]:
    if not isinstance(url,str) or len(url)>8192 or any(ord(c)<32 for c in url):raise ValueError('invalid local URL')
    p=urlsplit(url)
    if p.scheme!='http' or p.username or p.password or p.fragment or not p.hostname or not p.port:raise ValueError('explicit loopback origin required')
    if not ipaddress.ip_address(p.hostname).is_loopback or not 1024<=p.port<=65535:raise ValueError('loopback port required')
    path=p.path or '/'
    if '\\' in path or unquote(path)!=path or any(x in {'.','..'} for x in path.split('/')):raise ValueError('ambiguous path')
    host='['+p.hostname+']' if ':' in p.hostname else p.hostname
    return f'http://{host}:{p.port}',path,parse_qs(p.query,keep_blank_values=True)


def source_binding(url: str) -> dict[str,str]:
    """Persist only the local batch scope and a digest, never arbitrary URL queries."""
    try:
        origin,path,query=local_page(url)
        batches=query.get('batch',[])
        if len(batches)!=1 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',batches[0]):return {}
        return {'source_url':origin+path+'?batch='+batches[0],
                'source_url_hash':hashlib.sha256(url.encode('utf-8')).hexdigest()}
    except (TypeError,ValueError):return {}


@dataclass
class LocalConsent:
    project: str
    anchor_task: str
    order_id: str
    batch: str
    actor: str
    paths: dict[str,tuple[str,...]]
    expires: float
    maximum: int
    request_id: str
    duration: int
    consent_id: str=field(default_factory=lambda:secrets.token_hex(12))
    used: set[str]=field(default_factory=set)
    stopped: bool=False


class LocalConsentRegistry:
    def __init__(self,clock=time.monotonic):self.clock=clock;self._grants={};self._effects={}

    def authorize(self,*,project,anchor_task,order_id,batch,actor,paths,request_id,ttl_seconds=1800,max_actions=40):
        if any(not isinstance(x,str) or not x.strip() or len(x)>128 for x in [project,anchor_task,order_id,batch,actor,request_id]):raise ValueError('explicit order identity required')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}',batch):raise ValueError('explicit batch identifier required')
        if type(ttl_seconds) is not int or not 60<=ttl_seconds<=7200 or type(max_actions) is not int or not 1<=max_actions<=80:raise ValueError('bounded consent required')
        if not isinstance(paths,dict) or not paths or len(paths)>4:raise ValueError('local sites required')
        clean={}
        for origin,prefixes in paths.items():
            canonical,path,_=local_page(origin+'/')
            if canonical!=origin or path!='/' or not isinstance(prefixes,(list,tuple)) or not prefixes or len(prefixes)>8:raise ValueError('invalid local sites')
            values=[]
            for prefix in prefixes:
                if not isinstance(prefix,str) or not prefix.startswith('/') or prefix=='/' or '?' in prefix or '#' in prefix:raise ValueError('explicit business paths required')
                _,normalized,_=local_page(origin+prefix)
                values.append(normalized.rstrip('/'))
            clean[origin]=tuple(sorted(set(values)))
        for key,g in list(self._grants.items()):
            if g.stopped or g.expires<=self.clock():self._grants.pop(key)
        existing=self._grants.get((project,anchor_task))
        if existing is not None:
            if (existing.request_id==request_id and existing.order_id==order_id and existing.batch==batch
                    and existing.actor==actor and existing.paths==clean and existing.maximum==max_actions
                    and existing.duration==ttl_seconds):return existing
            raise ValueError('revoke the current local consent before replacing it')
        if len(self._grants)>=8 and (project,anchor_task) not in self._grants:raise ValueError('local consent capacity reached')
        if len(self._effects)>=4096:raise ValueError('local consent process capacity reached')
        g=LocalConsent(project,anchor_task,order_id,batch,actor,clean,self.clock()+ttl_seconds,max_actions,request_id,ttl_seconds)
        self._grants[(project,anchor_task)]=g
        return g

    def revoke(self,project,anchor_task):
        g=self._grants.get((project,anchor_task))
        if g:g.stopped=True

    def inspect(self,project,anchor_task):return self._grants.get((project,anchor_task))

    def reserve(self,publication):
        from ampuly.core.interaction_protocol import owner_interaction_actor_identity
        cp,effect=publication.checkpoint,publication.effect
        if cp.checkpoint_type!='action_permission':return None
        if dict((cp.payload or {}).get('effect_approval') or {}).get('protocol')!='superones.effect-approval/v1':return None
        anchor,_=owner_interaction_actor_identity(cp)
        g=self._grants.get((cp.project_id,anchor))
        if not g or g.stopped or self.clock()>=g.expires or len(g.used)>=g.maximum:return None
        if effect.project_id!=g.project or effect.kind!='browser.submit' or effect.provider!='browser:submit':return None
        if str(getattr(effect.state,'value',effect.state))!='awaiting_approval' or cp.status!='pending':return None
        if effect.effect_id in g.used:return None
        payload=dict(effect.payload or {})
        if payload.get('operation','click') not in {'click','press_enter'}:return None
        try:
            origin,path,query=local_page(payload.get('source_url',''))
            if origin not in g.paths or not any(path==p or path.startswith(p+'/') for p in g.paths[origin]):return None
            if query.get('batch')!=[g.batch]:return None
            for target in [payload.get('destination'),payload.get('frame_url')]:
                if not target:continue
                to,where,q=local_page(target)
                if to!=origin or not any(where==p or where.startswith(p+'/') for p in g.paths[origin]):return None
                if 'batch' in q and q['batch']!=[g.batch]:return None
        except (TypeError,ValueError):return None
        label=str(dict(payload.get('element') or {}).get('text','')).lower()
        if any(word in label for word in ('支付','付款','退款','删除','清空','权限','密码','pay','delete','refund','permission')):return None
        if not label.strip():return None
        g.used.add(effect.effect_id)
        self._effects[effect.effect_id]=g
        return g

    def effect_allowed(self,effect_id):
        g=self._effects.get(effect_id)
        return g is None or (not g.stopped and self.clock()<g.expires)

    def effect_is_local(self,effect_id):
        return effect_id in self._effects


REGISTRY=LocalConsentRegistry()
POLICY_VERSION='loopback-order-v1.2'


async def apply_local_consent(engine,publication):
    """Use canonical owner-decision storage and the existing once-only consumer."""
    grant=REGISTRY.reserve(publication)
    if grant is None:return publication
    service=engine.effect_approval_service
    try:
        decision={'option_id':'approve_once','text':'依本单限时本地网页授权自动确认；仍逐次核验页面、表单及执行结果。',
                  'authorization_actor':{'actor_id':grant.actor,'role':'owner'},'local_consent_id':grant.consent_id}
        receipt=await service.coordinator.submit(checkpoint_id=publication.checkpoint.checkpoint_id,
            checkpoint_type=publication.checkpoint.checkpoint_type,decision=decision,
            client_request_id='local-consent-'+publication.effect.effect_id,checkpoint=publication.checkpoint)
        if not receipt.acknowledged:raise RuntimeError('local consent decision not accepted')
        result=await service.consume_answered(checkpoint_id=publication.checkpoint.checkpoint_id,
            consumer_id='local-browser-consent',authorized_by=grant.actor,claimed_by='local-browser-consent')
        if result.effect is not None:
            await engine._drain_effect_outbox(effect_id=result.effect.effect_id)
        evidence=dict((getattr(result.checkpoint,'payload',None) or {}).get('effect_result') or {})
        needs_expectation = (result.effect is not None and result.status=='failed'
            and str(result.effect.last_error or '').startswith('local_consent_expectation_required:'))
        if not needs_expectation and (result.effect is None or result.status!='applied' or evidence.get('verification_status')!='complete'):grant.stopped=True
        if result.effect is None:return publication
        from ampuly.application.effect_approval import EffectApprovalPublication
        return EffectApprovalPublication(result.effect,result.checkpoint or publication.checkpoint,publication.checkpoint_created)
    except BaseException:
        grant.stopped=True
        raise
