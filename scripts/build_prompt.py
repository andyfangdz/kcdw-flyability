#!/usr/bin/env python3
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kcdw.common import load_json
from kcdw.prompt import build_prompt

p = argparse.ArgumentParser()
p.add_argument("snapshot")
p.add_argument("output")
a = p.parse_args()
Path(a.output).write_text(build_prompt(load_json(a.snapshot)), encoding="utf-8")
