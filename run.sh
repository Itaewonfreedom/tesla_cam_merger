#!/usr/bin/env bash
# Tesla Dashcam Merger 실행 스크립트
# 가상환경(.venv)을 사용해 GUI 앱을 실행합니다.
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "가상환경이 없습니다. 먼저 셋업을 진행합니다..."
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip
  .venv/bin/pip install -r requirements.txt
fi

exec .venv/bin/python main.py
