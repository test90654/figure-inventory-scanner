import ast,json,sqlite3,tempfile,time,types,unittest
from pathlib import Path
root=Path(__file__).resolve().parent
tree=ast.parse((root/'trader_integration.py').read_text(encoding='utf-8'))
cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='TraderService')
methods=[n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name in ('stock_quotes','quote_version')]
ns={'json':json,'time':time,'TTL':21600,'normalize':lambda s:s.strip().lower()}
exec(compile(ast.Module(body=methods,type_ignores=[]),'trader_integration.py','exec'),ns)
class QuotesTest(unittest.TestCase):
 def test_saved_quotes_and_identity_checks(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'test.db'
   connections=[]
   def connect():
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;connections.append(db);return db
   service=types.SimpleNamespace(connect=connect,ensure_restored=lambda:None)
   with connect() as db:
    db.executescript('CREATE TABLE items(jan,title);CREATE TABLE trader_links(jan,expected,inventory_title,master_id);CREATE TABLE trader_products(master_id,data,updated);CREATE TABLE trader_history(id INTEGER);')
    for jan,price,title,model,status in [('1',1200,'Figure','M','ok'),('2',0,'Figure','M','ok'),('3',500,'Changed','M','ok'),('4',500,'Figure','OTHER','ok'),('5',None,'Figure','M','needs_review')]:
     db.execute('INSERT INTO items VALUES(?,?)',(jan,title))
     db.execute('INSERT INTO trader_links VALUES(?,?,?,?)',(jan,json.dumps({'model':'M','name':'Figure'}),'Figure',jan))
     db.execute('INSERT INTO trader_products VALUES(?,?,?)',(jan,json.dumps({'observed':{'model':model,'name':'Figure'},'price_yen':price,'status':status,'checked_at':'2026-10-17T12:00:00+09:00'}),time.time() if jan=='1' else 0))
   quotes=ns['stock_quotes'](service,['1','2','3','4','5','6'])
   self.assertEqual(quotes['1']['price_yen'],1200);self.assertFalse(quotes['1']['stale'])
   self.assertEqual(quotes['2']['price_yen'],0);self.assertTrue(quotes['2']['stale'])
   for jan in ('3','4','5'):self.assertIsNone(quotes[jan]['price_yen']);self.assertEqual(quotes[jan]['status'],'needs_review')
   self.assertEqual(quotes['6']['status'],'unlinked')
   self.assertEqual(ns['quote_version'](service),0)
   with connect() as db:db.execute('INSERT INTO trader_history VALUES(7)')
   self.assertEqual(ns['quote_version'](service),7)
   for connection in connections:connection.close()
if __name__=='__main__':unittest.main()
