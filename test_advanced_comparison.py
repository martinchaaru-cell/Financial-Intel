import unittest,time
from advanced_comparison import enrich,align_directors,rankings
from report_jobs import ReportJobs
class AdvancedTests(unittest.TestCase):
    def test_cagr(self):
        rows=[{'values':[100,121]},{'values':[0,10]},{'values':[100,None]},{'values':[100,0]}]
        enrich(rows,['FY2022','FY2024']);self.assertAlmostEqual(rows[0]['cagr'],10);self.assertIsNone(rows[1]['cagr']);self.assertIsNone(rows[2]['cagr']);self.assertEqual(rows[3]['cagr'],-100)
    def test_match(self):
        p=lambda *names:{'director_rows':[{'director_name':n,'table_kind':'NED'} for n in names]}
        self.assertEqual(len(align_directors({'FY2024':p('Dr. Jane Doe'),'FY2025':p('Jane Doe')})),1)
        self.assertFalse(align_directors({'FY2024':p('Jane Doe','Dr Jane Doe'),'FY2025':p('Jane Doe')}))
        self.assertFalse(align_directors({'FY2024':p('J Doe'),'FY2025':p('Jane Doe')}))
    def test_rank(self):
        def r(i,v):return dict(company_id=i,name=str(i),currency='KES',net_profit=v)
        rows=rankings({'FY2024':[r(1,2),r(2,4)],'FY2025':[r(1,5),r(2,5)]})
        self.assertEqual(next(x for x in rows if x['company_id']==1)['movement'],1)
    def test_queue(self):
        q=ReportJobs();k=q.submit(1,'company','pdf',lambda:(b'pdf','name','application/pdf'))
        try:
            for _ in range(100):
                if q.get(k,1,'company')['status']=='ready':break
                time.sleep(.01)
            self.assertEqual(q.get(k,1,'company')['data'],b'pdf');self.assertIsNone(q.get(k,2,'company'));self.assertIsNone(q.get(k,1,'survey'));self.assertIsNone(q.get(k,1,'company','xlsx'))
        finally:q.pool.shutdown()
