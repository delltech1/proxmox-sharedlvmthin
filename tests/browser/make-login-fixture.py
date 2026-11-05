"""Render real login/TFA pages with a temporary, nonfunctional test config."""
import json, os, runpy, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[2]
with tempfile.NamedTemporaryFile('w',encoding='utf-8',delete=False) as f:
    f.write('[web]\naddress=127.0.0.1\nport=9443\n[tls]\ncertificate=/none\nprivate_key=/none\n')
    name=f.name
try:
    os.environ['SHAREDLVMTHIN_WEB_CONFIG']=name
    web=runpy.run_path(str(root/'usr/libexec/pve-sharedlvmthin/sharedlvmthin-web'),run_name='local_fixture')
    print(json.dumps({'login':web['login']('Authentication failed.','example@pve'),'tfa':web['tfa']('fixture','example@pve')}))
finally:
    os.unlink(name)
