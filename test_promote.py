import json
from python.mcp_server import QMemMCP
import os

mcp = QMemMCP()
# 1. Save a memory
req_save = {
    'name': 'mem_save',
    'arguments': {
        'project_id': 'TEST_PROJECT',
        'title': 'Test Q2 Feature',
        'content': 'This is a test of the physical extraction.'
    }
}
res_save = mcp._tools_call(req_save)
obs_id = json.loads(res_save['content'][0]['text'])['obs_id']
print(f'Saved obs_id: {obs_id}')

# 2. Promote the memory
test_proj_dir = '/tmp/test_q2_proj'
os.makedirs(test_proj_dir, exist_ok=True)
req_promote = {
    'name': 'memory_promote',
    'arguments': {
        'obs_id': obs_id,
        'project_path': test_proj_dir
    }
}
res_promote = mcp._tools_call(req_promote)
print(f'Promoted: {res_promote}')

# 3. Verify
skill_file = os.path.join(test_proj_dir, '.agents/skills/q2-consensus/SKILL.md')
if os.path.exists(skill_file):
    print('Skill file created!')
    with open(skill_file) as f:
        print(f.read())
else:
    print('FAIL: Skill file not created.')

