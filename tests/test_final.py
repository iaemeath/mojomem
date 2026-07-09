import os
os.chdir("C:\mojomem")
from embedding import BGEEmbedding
e = BGEEmbedding()
v = e.embed("保供达梦CLOB陷阱")
print(f"dim={len(v)} first3={v[:3]}")
print("MODEL_LOADED_OK")
