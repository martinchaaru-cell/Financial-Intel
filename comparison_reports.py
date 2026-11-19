"""Read-only comparison matrices and paginated comprehensive exports."""
import math, re, json
from io import BytesIO
from xml.sax.saxutils import escape
from pathlib import Path
from monetary_units import SCALES, pay_scale

META = {'id','company_id','fiscal_year','uploaded_at','field_sources','field_confidence','field_review_status','field_review_corrections'}

def years_arg(values):
    years = sorted(set(values))
    if not 2 <= len(years) <= 10 or any(not re.fullmatch(r'FY\d{4}', y) for y in years):
        raise ValueError('Select between 2 and 10 valid financial years.')
    return years

def flatten(obj, prefix=''):
    if isinstance(obj, dict):
        if not obj: yield prefix, 'No records on file'
        for k,v in obj.items():
            yield from flatten(v, f'{prefix} / {k}' if prefix else str(k))
    elif isinstance(obj,list):
        for i,v in enumerate(obj):
            yield from flatten(v, f'{prefix} / {i+1}')
        if not obj: yield prefix, 'No records on file'
    else: yield prefix,obj

def company_metrics(payload, pay_fields, kinds):
    sv = payload.get('survey') or {}
    cur = sv.get('currency') or 'KES'
    result = {}
    for k,v in sv.items():
        if k in META or isinstance(v,(dict,list)): continue
        kind = kinds.get(k)
        unit = None
        if k in pay_fields or k == 'ceo_annual_salary_as_stated' or kind == 'pay': unit = sv.get('director_figures_unit') or 'units'
        elif k in ('turnover','net_profit','market_cap') or kind == 'headline': unit = sv.get('unit') or ''
        elif kind == 'pershare': unit = 'units'
        if unit is not None:
            scale = SCALES.get(str(unit).strip().lower())
            result[f'{k} ({cur})'] = v*scale if isinstance(v,(int,float)) and scale is not None else None
        else: result[k] = v
    # Exact filed names + table type only; no speculative identity matching or summing subtotals.
    seen = {}
    for r in payload.get('director_rows', []):
        key = f"Director pay / {r.get('director_name')} / {r.get('table_kind')} / {r.get('role')}"
        seen[key] = seen.get(key,0)+1
        key += f" / row {seen[key]}"
        for field,v in {'total':r.get('total'), **(r.get('components') or {})}.items():
            result[f'{key} / {field} ({cur})'] = v*pay_scale(sv.get('director_figures_unit')) if isinstance(v,(int,float)) else None
    for r in payload.get('survey_director_rows',[]):
        for k in ('position','gender','nationality','independent','appointed_date','end_of_term','board_meetings_attended'):
            result[f"Director register / {r.get('director_name')} / {k}"] = r.get(k)
    for r in payload.get('committees',[]):
        for k in ('chairperson_name','member_count','meetings_held','attendance_rate'):
            result[f"Committee / {r.get('name')} / {k}"] = r.get(k)
    return result

def matrix(series):
    keys = list(dict.fromkeys(k for values in series.values() for k in values))
    years = list(series)
    out=[]
    for k in keys:
        vals=[series[y].get(k) for y in years]
        a,b=vals[0],vals[-1]
        numeric=lambda x: isinstance(x,(int,float)) and not isinstance(x,bool) and math.isfinite(x)
        delta=b-a if numeric(a) and numeric(b) else None
        out.append({'metric':k,'values':vals,'change':delta,'change_pct':delta/abs(a)*100 if delta is not None and a else None})
    return out

def fmt(v):
    if v is None: return 'Not available'
    if isinstance(v,bool): return 'Yes' if v else 'No'
    if isinstance(v,(int,float)): return f'{v:,.2f}'.rstrip('0').rstrip('.') if isinstance(v,float) else f'{v:,}'
    return str(v)

def detail_rows(payload):
    sv=payload.get('survey') or {}
    raw={k:v for k,v in sv.items() if k not in META}
    scale=pay_scale(sv.get('director_figures_unit'))
    sections={'Company profile':payload.get('company',{}),'Survey fields (stored values; see currency and units)':raw}
    for key in ('director_rows','survey_director_rows','survey_director_benefits','benefit_categories','committees','evidence_conflicts','quality_by_year','ai_items'):
        sections[key]=payload.get(key,[])
    sections['evidence_conflicts']=[{k:v for k,v in r.items() if k != 'resolved_by'} for r in payload.get('evidence_conflicts',[])]
    sections['source_documents']=[{k:v for k,v in d.items() if k not in ('url',)} for d in payload.get('source_documents',[]) if d.get('period_label')==sv.get('fiscal_year') or d.get('filename')==sv.get('source_filename')]
    for key in ('field_sources','field_confidence','field_review_status','field_review_corrections'): sections[key]=sv.get(key,{})
    sections['Director pay in whole currency']=[{'director_name':r.get('director_name'),'table_kind':r.get('table_kind'),'total':r['total']*scale if r.get('total') is not None else None,'components':{k:v*scale if isinstance(v,(int,float)) else v for k,v in (r.get('components') or {}).items()},'page':r.get('page')} for r in payload.get('director_rows',[])]
    return sections

def append_details(story, sections):
    from reportlab.platypus import Paragraph, LongTable, TableStyle, Spacer, PageBreak
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    head=ParagraphStyle('detail_head',fontName='Helvetica-Bold',fontSize=13,leading=17,textColor=colors.HexColor('#104c38'),spaceAfter=8,keepWithNext=True)
    cell=ParagraphStyle('detail_cell',fontSize=8,leading=11,wordWrap='CJK')
    white=ParagraphStyle('detail_white',parent=cell,textColor=colors.white,fontName='Helvetica-Bold')
    for title,obj in sections.items():
        story.append(Paragraph(escape(title.replace('_',' ')),head))
        entries=list(flatten(obj)) or [('Status','No records on file')]
        rows=[[Paragraph('Field / record',white),Paragraph('Value',white)]]
        for k,v in entries:
            # Split very long evidence into bounded paragraphs instead of an unbreakable table row.
            s=fmt(v); chunks=[s[i:i+700] for i in range(0,len(s),700)] or ['']
            for i,chunk in enumerate(chunks): rows.append([Paragraph(escape(k.replace('_',' ')) if i==0 else 'Continued',cell),Paragraph(escape(chunk),cell)])
        table=LongTable(rows,colWidths=[76*mm,94*mm],repeatRows=1)
        table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#104c38')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#edf5f0')]),('VALIGN',(0,0),(-1,-1),'TOP'),('BOTTOMPADDING',(0,0),(-1,-1),5)]))
        story.extend([table,Spacer(1,12)])

def export(data, extension, detail=True):
    sections={'Methodology':data['methodology'],'Comparison matrix':[{ 'metric':r['metric'],**dict(zip(data['years'],r['values'])),'absolute change':r['change'],'percentage change':r['change_pct'],'CAGR %':r.get('cagr')} for r in data['rows']]}
    sections['Historical rankings']=data.get('rankings',[])
    sections['Director identity candidates']=data.get('director_matches',[])
    if detail: sections.update(data.get('details',{}))
    out=BytesIO()
    if extension=='pdf':
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Image, Spacer, LongTable, TableStyle, PageBreak
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.pagesizes import landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.units import mm
        logo=Path(__file__).parent/'static/icons/icon-192.png'
        story=[Image(str(logo),width=16*mm,height=16*mm),Paragraph('The Remuneration Practice',getSampleStyleSheet()['Title']),Paragraph(escape(data['title']),getSampleStyleSheet()['Heading1']),Spacer(1,10)]
        def footer(c,d): c.setFont('Helvetica',8); c.drawString(20*mm,12*mm,'The Remuneration Practice'); c.drawRightString(190*mm,12*mm,str(d.page))
        append_details(story, {'Methodology':data['methodology']})
        # Split wide comparisons into year bands, retaining the metric and endpoint changes.
        cell=ParagraphStyle('matrix_cell',fontSize=8,leading=10,wordWrap='CJK')
        header=ParagraphStyle('matrix_header',parent=cell,textColor=colors.white,fontName='Helvetica-Bold')
        for start in range(0,len(data['years']),4):
            ys=data['years'][start:start+4]
            cells=[[Paragraph(escape(v),header) for v in ['Metric',*ys,'Change','Change %','CAGR %']]]
            for r in data['rows']:
                vals=[r['metric'].replace('_',' '),*r['values'][start:start+4],r['change'],r['change_pct'],r.get('cagr')]
                cells.append([Paragraph(escape(fmt(v)),cell) for v in vals])
            t=LongTable(cells,colWidths=[55*mm]+[115*mm/(len(ys)+3)]*(len(ys)+3),repeatRows=1,splitInRow=1)
            t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#104c38')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#edf5f0')]),('VALIGN',(0,0),(-1,-1),'TOP')]))
            story.extend([PageBreak(),Paragraph('Comparison matrix',getSampleStyleSheet()['Heading2']),t])
        append_details(story, {'Historical rankings':data.get('rankings',[]),'Director identity candidates':data.get('director_matches',[])})
        if detail and data.get('details'):
            story.append(PageBreak()); append_details(story,data['details'])
        SimpleDocTemplate(out,pagesize=A4,leftMargin=20*mm,rightMargin=20*mm,topMargin=20*mm,bottomMargin=20*mm).build(story,onFirstPage=footer,onLaterPages=footer)
    elif extension=='docx':
        from docx import Document
        d=Document(); d.add_heading(data['title'],0)
        for title,obj in sections.items():
            d.add_heading(title,1); t=d.add_table(rows=0,cols=2); t.style='Light Shading Accent 1'
            for k,v in flatten(obj):
                cells=t.add_row().cells; cells[0].text=k; cells[1].text=fmt(v)
        d.save(out)
    elif extension=='xlsx':
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        w=Workbook(); s=w.active; s.title='Comparison'; s.append(['Metric',*data['years'],'Absolute change','Percentage change','CAGR %'])
        for r in data['rows']: s.append([r['metric'],*r['values'],r['change'],r['change_pct'],r.get('cagr')])
        for i,(title,obj) in enumerate(sections.items()):
            if title=='Comparison matrix': continue
            sh=w.create_sheet(f'Detail {i+1}'); sh.append([title,'Value'])
            for k,v in flatten(obj): sh.append([k,v if isinstance(v,(int,float,bool)) or v is None else str(v)[:32767]])
        for sh in w:
            sh.freeze_panes='B2'; sh.column_dimensions['A'].width=65
            for c in sh[1]: c.font=Font(bold=True,color='FFFFFF'); c.fill=PatternFill('solid',fgColor='104C38')
            for row in sh.iter_rows():
                for c in row:
                    if c.data_type=='f': c.data_type='s'
        w.save(out)
    else: raise ValueError('Unsupported format')
    out.seek(0); return out

def _detail_story(sections):
    story=[]; append_details(story,sections); return story
