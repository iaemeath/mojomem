import os,sys
os.chdir('C:\\mojomem')
sys.path.insert(0,'C:\\mojomem')
from embedding import BGEEmbedding
e = BGEEmbedding()
v = e.embed('保供达梦')
assert len(v)==512 and v[0]!=0
print('EMBED:OK dim=512')
import sqlite3,sqlite_vec,numpy as np
db = 'C:\\mojomem\\test.db'
if os.path.exists(db): os.remove(db)
c = sqlite3.connect(db)
c.enable_load_extension(True)
sqlite_vec.load(c)
c.executescript(open('C:\\mojomem\\schema.sql').read())
oid='t01'
c.execute('INSERT INTO memory_facts(obs_uuid,project,topic_key,content) VALUES(?,?,?,?)',(oid,'p1','wk','保供达梦CLOB'))
rid=c.execute('SELECT id FROM memory_facts WHERE obs_uuid=?',(oid,)).fetchone()[0]
c.execute('UPDATE memory_vectors SET embedding=? WHERE rowid=?',(np.array(v,dtype=np.float32).tobytes(),rid))
c.commit()
print('SAVE:OK rid='+str(rid))
cur=c.execute('SELECT obs_uuid,vec_distance_cosine(mv.embedding,?) as d FROM memory_vectors mv JOIN memory_facts mf ON mv.rowid=mf.id ORDER BY d',(np.array(v,dtype=np.float32).tobytes(),))
r=cur.fetchone()
assert r[0]=='t01'
print('RECALL:OK dist={:.4f}'.format(r[1]))
c.execute('UPDATE memory_facts SET is_global=1 WHERE obs_uuid=?',(oid,))
g=c.execute('SELECT is_global FROM memory_facts WHERE obs_uuid=?',(oid,)).fetchone()[0]
assert g==1
print('PROMOTE:OK')
c.execute('DELETE FROM memory_facts WHERE obs_uuid=?',(oid,))
n=c.execute('SELECT COUNT(*) FROM memory_facts').fetchone()[0]
assert n==0
print('DELETE:OK')
c.close()
os.remove(db)
print('=== ALL TESTS PASSED ===')