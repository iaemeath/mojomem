from embedding import BGEEmbedding
e = BGEEmbedding('bge-small-zh-v1.5-onnx')
v = e.embed('保供达梦CLOB陷阱')
print(f'dim={len(v)} first3={v[:3]}')
print('SMOKE_TEST_PASSED')
