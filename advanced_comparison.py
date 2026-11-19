"""Conservative historical analytics; no fuzzy identity merges."""
import math, re, unicodedata
from collections import defaultdict

def numeric(v):
    return isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)

def enrich(rows, years):
    span=int(years[-1][2:])-int(years[0][2:])
    for r in rows:
        a,b=r['values'][0],r['values'][-1]
        r['cagr']=None
        if numeric(a) and numeric(b) and a>0 and b>=0 and span>0:
            try:
                v=-100.0 if b==0 else math.expm1((math.log(b)-math.log(a))/span)*100
                if math.isfinite(v): r['cagr']=v
            except (OverflowError, ValueError): pass
    return rows

def name_key(name):
    s=unicodedata.normalize('NFKC',str(name or '')).casefold()
    s=re.sub(r'[^\w]+',' ',s).strip()
    return re.sub(r'^(?:(?:mr|mrs|ms|miss|dr|prof|hon)\s+)+','',s)

def align_directors(payloads):
    """Only unique normalized full names within each company/year/table. Keep role and source rows unchanged."""
    buckets=defaultdict(dict)
    for year,p in payloads.items():
        for r in p.get('director_rows',[]):
            key=(name_key(r.get('director_name')),str(r.get('table_kind') or ''))
            buckets[key].setdefault(year,[]).append(r)
    out=[]
    for (name,kind), by_year in buckets.items():
        if len(name.split())<2 or len(by_year)<2 or any(len(v)!=1 for v in by_year.values()): continue
        out.append({'normalized_name':name,'table_kind':kind,'status':'Candidate: unique normalized full name; verify identity','years':{y:{'filed_name':v[0].get('director_name'),'role':v[0].get('role')} for y,v in by_year.items()}})
    return out

RANK_FIELDS=('governance.score','net_profit','market_cap','turnover','ceo_monthly_salary','chairperson_annual_retainer','other_ned_annual_retainer')
def rankings(by_year):
    result=[]
    for field in RANK_FIELDS:
        groups=defaultdict(dict)
        for y,rows in by_year.items():
            for r in rows:
                v=(r.get('governance') or {}).get('score') if field=='governance.score' else r.get(field)
                if numeric(v): groups[r.get('currency') if field!='governance.score' else 'score'].setdefault(y,[]).append((v,r['company_id'],r['name']))
        for currency,years in groups.items():
            companies=defaultdict(dict); names={}
            for y,items in years.items():
                prev=None;rank=0
                for i,(v,cid,name) in enumerate(sorted(items,reverse=True),1):
                    if v!=prev: rank=i;prev=v
                    companies[cid][y]=rank;names[cid]=name
            for cid,ranks in companies.items():
                ys=list(by_year);a,b=ranks.get(ys[0]),ranks.get(ys[-1])
                result.append({'metric':field,'currency':currency,'company':names[cid],'company_id':cid,'ranks':ranks,'movement':a-b if a is not None and b is not None else None})
    return result
