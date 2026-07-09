import os
p = os.path.join('bge-small-zh-v1.5-onnx', 'onnx', 'model.onnx')
print(f'path: {p}')
print(f'abspath: {os.path.abspath(p)}')
print(f'exists: {os.path.exists(p)}')
print(f'cwd: {os.getcwd()}')

# Try absolute path
ap = r'C:\mojomem\bge-small-zh-v1.5-onnx\onnx\model.onnx'
print(f'abs_exists: {os.path.exists(ap)}')
