"""Persist process ownership before exec, even if the supervisor dies immediately."""
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from core import APP_EXECUTABLE,process_stamp,write_json

job=Path(sys.argv[1]);state=json.loads(job.read_text())
state.update(pid=os.getpid(),pid_start=process_stamp(os.getpid()),status='running')
write_json(job,state)
binary=str(APP_EXECUTABLE)
os.execv(binary,[binary,*sys.argv[2:]])
