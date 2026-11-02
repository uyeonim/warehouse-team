"""Explicit environment selection. Importing this module never starts equipment."""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
env_path = os.environ.get('WAREHOUSE_ENV_FILE')
if env_path:
    path = Path(env_path).resolve()
    if not path.is_file():
        raise RuntimeError(f'설정 파일이 없습니다: {path}')
    load_dotenv(path, override=True)
else:
    # 기본은 전시장 설정(.env.local). demo.py / read_pose.py 도 같은 포트·좌표 설정을 쓰게 함
    load_dotenv(ROOT / '.env.local' if (ROOT / '.env.local').is_file() else ROOT / '.env')

PROFILE = os.environ.get('WAREHOUSE_PROFILE', 'legacy').strip().lower()
if PROFILE not in ('legacy', 'local'):
    raise RuntimeError('WAREHOUSE_PROFILE은 legacy 또는 local이어야 합니다.')
